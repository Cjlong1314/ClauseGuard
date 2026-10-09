"""e签宝Connector（F7）

能力（走e签宝开放平台API风格）：
- access token获取与缓存（POST /v1/oauth2/token，client_credentials模式）
- 签署流程详情查询（GET /v1/signflows/{flow_id}，Bearer鉴权）
- 签署状态回写（处理esign.status_changed事件：查询流程详情核实→审计留痕→
  可选站内通知；状态映射e签宝signFlowStatus数字→内部枚举
  INITIATED/SIGNING/COMPLETED/REJECTED/CANCELLED）
- 回调验签（/api/integrations/esignbao/callback，HMAC-SHA256，key=回调密钥）
- 健康检查（token获取探活）

网络不可达时抛异常→事件总线落outbox重试队列，不阻塞主流程；
外发/日志内容统一走dingtalk.connector.mask_sensitive()脱敏。
"""
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import urllib.request

from ..base import ConnectorBase
from .. import events as ev
from ... import audit as iaudit
from ..dingtalk.connector import mask_sensitive

# e签宝签署流程状态signFlowStatus数字→内部状态枚举映射
ESIGN_FLOW_STATUS_MAP = {
    0: "INITIATED",    # 已创建/待签署（发起）
    1: "SIGNING",      # 签署中
    2: "COMPLETED",    # 已完成（全部签署完毕）
    3: "REJECTED",     # 已拒签
    5: "CANCELLED",    # 已撤销
}


class ESignBaoConnector(ConnectorBase):
    name = "esignbao"
    CONFIG_KEYS = ["ESIGNBAO_APP_ID", "ESIGNBAO_SECRET", "ESIGNBAO_API_BASE",
                   "ESIGNBAO_CALLBACK_SECRET", "ESIGNBAO_NOTIFY_USERS"]
    REQUIRED_KEYS = ["ESIGNBAO_APP_ID", "ESIGNBAO_SECRET"]

    def __init__(self):
        super().__init__()
        self._token = ""
        self._token_expire = 0.0
        self._token_lock = threading.Lock()

    # ---------- e签宝开放平台API ----------
    def get_access_token(self) -> str:
        """获取并缓存access_token（client_credentials，提前60s过期）"""
        with self._token_lock:
            if self._token and time.time() < self._token_expire:
                return self._token
            url = f"{self.esignbao_api_base}/v1/oauth2/token"
            payload = {"app_id": self.esignbao_app_id, "secret": self.esignbao_secret,
                       "grant_type": "client_credentials"}
            data = self._http_json(url, payload)
            if data.get("code") not in (0, "0", None):
                raise RuntimeError(f"获取e签宝token失败: {data}")
            self._token = data["access_token"]
            self._token_expire = time.time() + int(data.get("expires_in", 7200)) - 60
            return self._token

    def _http_json(self, url: str, payload: dict = None, token: str = "") -> dict:
        """POST/GET JSON；网络异常向上抛（由总线落重试队列）"""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Tsign-Open-Api-Auth"] = f"Bearer {token}"
        if payload is not None:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST")
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def query_flow(self, flow_id: str) -> dict:
        """查询签署流程详情：GET /v1/signflows/{flow_id}，返回{status, raw}"""
        token = self.get_access_token()
        url = f"{self.esignbao_api_base}/v1/signflows/{urllib.parse.quote(flow_id)}"
        data = self._http_json(url, token=token)
        if data.get("code") not in (0, "0", None):
            raise RuntimeError(f"查询签署流程失败: {data}")
        flow = data.get("data", {}) or {}
        status = ESIGN_FLOW_STATUS_MAP.get(flow.get("signFlowStatus"), "SIGNING")
        return {"status": status, "raw": flow}

    # ---------- Connector统一接口 ----------
    def send_event(self, event: dict) -> bool:
        """按事件类型分发；仅处理esign.status_changed，其余返回False跳过"""
        et = event.get("event_type")
        if et != ev.EVENT_ESIGN_STATUS_CHANGED:
            return False
        p = event.get("payload", {})
        flow_id = p.get("flow_id", "")
        if not flow_id:
            iaudit.log("integration", "esignbao_status_skip", event.get("contract_id", ""),
                       "esign.status_changed事件缺少flow_id，跳过",
                       ext={"event_id": event.get("event_id")})
            return False
        # 向e签宝查询流程详情核实回调/上报状态（查询失败抛异常→落重试队列）
        flow = self.query_flow(flow_id)
        status = p.get("status") or flow["status"]
        if status not in ev.ESIGN_STATUSES:
            status = "SIGNING"
        # 状态回写：审计留痕 + 可选站内通知
        iaudit.log("integration", "esignbao_status_writeback",
                   event.get("contract_id", ""),
                   f"签署流程{mask_sensitive(flow_id)}状态回写为{status}",
                   ext={"event_id": event.get("event_id"), "flow_id": flow_id,
                        "status": status,
                        "verify_status": flow["status"]})
        self._notify_users(event, flow_id, status)
        return True

    def _notify_users(self, event: dict, flow_id: str, status: str):
        """签署完成/拒签/撤销时给配置用户发站内通知（失败不阻塞）"""
        users = [u.strip() for u in (self.esignbao_notify_users or "").split(",") if u.strip()]
        if not users or status not in ("COMPLETED", "REJECTED", "CANCELLED"):
            return
        try:
            from ... import collab
            title = f"合同签署状态：{status}"
            content = (f"签署流程{mask_sensitive(flow_id)}状态为{status}\n"
                       f"合同：{mask_sensitive(event.get('payload', {}).get('contract_filename', ''))}")
            for u in users:
                collab._notify(u, title, content)
            iaudit.log("integration", "esignbao_notify", event.get("contract_id", ""),
                       f"签署状态站内通知已发送（{len(users)}人）",
                       ext={"flow_id": flow_id, "status": status})
        except Exception as e:
            iaudit.log("integration", "esignbao_notify_failed", event.get("contract_id", ""),
                       f"签署状态站内通知发送失败：{e}", ext={"flow_id": flow_id})

    def send_message(self, user: str, title: str, content: str, **kw) -> bool:
        """发站内消息（e签宝无IM通道，消息落站内通知表并审计）"""
        try:
            from ... import collab
            collab._notify(self.map_user(user), mask_sensitive(title), mask_sensitive(content))
            iaudit.log("integration", "esignbao_message", kw.get("contract_id", ""),
                       f"站内消息已发送给{self.map_user(user)}",
                       ext={"title": title})
            return True
        except Exception as e:
            iaudit.log("integration", "esignbao_message_failed", kw.get("contract_id", ""),
                       f"站内消息发送失败：{e}")
            return False

    def health_check(self) -> dict:
        try:
            self.get_access_token()
            return {"ok": True, "detail": "access_token获取成功"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # ---------- 回调验签 ----------
    def verify_callback_signature(self, body: str, signature: str) -> bool:
        """e签宝回调验签：HMAC-SHA256(key=回调密钥, msg=body原文) hex比对；未配置密钥则不校验"""
        if not self.esignbao_callback_secret:
            return True
        expect = hmac.new(self.esignbao_callback_secret.encode("utf-8"),
                          body.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, (signature or "").lower())
