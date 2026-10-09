"""钉钉Connector（F7）

能力：
- access_token获取与缓存（企业内部应用gettoken）
- 工作通知推送（待办审批卡片：审查任务分配/完成等，敏感字段脱敏后外发）
- 回调验签（/api/integrations/dingtalk/callback，HMAC-SHA256）
- 健康检查（token获取探活）

网络不可达时抛异常→事件总线落outbox重试队列，不阻塞主流程。
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


def mask_sensitive(text: str) -> str:
    """敏感字段脱敏：手机号/身份证/银行卡打码后外发"""
    if not text:
        return text
    import re
    # 身份证（18位含X）先于手机号/银行卡处理，避免内部子串被误伤
    text = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)",
                  lambda m: m.group(0)[:4] + "**********" + m.group(0)[-4:], text)
    # 银行卡（13-19位连续数字，前后无数字）
    text = re.sub(r"(?<!\d)\d{13,19}(?!\d)",
                  lambda m: m.group(0)[:4] + "*********" + m.group(0)[-4:], text)
    # 手机号（含+86/86前缀），最后处理
    def _mask_phone(m):
        d = re.sub(r"\D", "", m.group(0))[-11:]
        return d[:3] + "****" + d[7:] if len(d) == 11 else m.group(0)
    text = re.sub(r"(?:\+?86[-\s]?)?1[3-9]\d{9}", _mask_phone, text)
    return text


class DingTalkConnector(ConnectorBase):
    name = "dingtalk"
    CONFIG_KEYS = ["DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_AGENT_ID",
                   "DINGTALK_API_BASE", "DINGTALK_CALLBACK_TOKEN", "DINGTALK_CALLBACK_AES_KEY"]
    REQUIRED_KEYS = ["DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DINGTALK_AGENT_ID"]

    def __init__(self):
        super().__init__()
        self._token = ""
        self._token_expire = 0.0
        self._token_lock = threading.Lock()

    # ---------- 钉钉API ----------
    def get_access_token(self) -> str:
        """获取并缓存access_token（提前60s过期）"""
        with self._token_lock:
            if self._token and time.time() < self._token_expire:
                return self._token
            url = (f"{self.dingtalk_api_base}/gettoken"
                   f"?appkey={urllib.parse.quote(self.dingtalk_app_key)}"
                   f"&appsecret={urllib.parse.quote(self.dingtalk_app_secret)}")
            data = self._http_json(url)
            if data.get("errcode") != 0:
                raise RuntimeError(f"gettoken失败: {data}")
            self._token = data["access_token"]
            self._token_expire = time.time() + int(data.get("expires_in", 7200)) - 60
            return self._token

    def _http_json(self, url: str, payload: dict = None) -> dict:
        """POST/GET JSON；网络异常向上抛（由总线落重试队列）"""
        if payload is not None:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        else:
            req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_work_notification(self, userid_list: list, content: str, title: str) -> dict:
        """发送工作通知（待办卡片走asyncsend_v2工作通知通道）"""
        token = self.get_access_token()
        url = f"{self.dingtalk_api_base}/topapi/message/corpconversation/asyncsend_v2?access_token={token}"
        payload = {
            "agent_id": int(self.dingtalk_agent_id),
            "userid_list": ",".join(userid_list),
            "msg": {"msgtype": "action_card",
                    "action_card": {"title": title, "markdown": content,
                                    "single_title": "查看详情", "single_url": ""}},
        }
        return self._http_json(url, payload)

    # ---------- Connector统一接口 ----------
    def send_event(self, event: dict) -> bool:
        """按事件类型分发；敏感字段脱敏后外发"""
        et = event.get("event_type")
        p = event.get("payload", {})
        if et == ev.EVENT_TASK_ASSIGNED:
            title = f"待办审查任务：{p.get('title', '')}"
            body = (f"您有新的合同审查任务\n\n**合同**：{p.get('contract_filename', '')}\n"
                    f"**任务**：{mask_sensitive(p.get('title', ''))}\n"
                    f"**截止**：{p.get('due_date') or '未设置'}\n"
                    f"**分派人**：{event.get('actor', '')}")
            return bool(self._push(p.get("assignee", ""), title, body, event))
        if et == ev.EVENT_REVIEW_COMPLETED:
            title = f"审查完成：{p.get('contract_filename', '')}"
            body = (f"合同审查已完成\n\n**合同**：{p.get('contract_filename', '')}\n"
                    f"**发现风险**：{p.get('finding_count', 0)}项\n"
                    f"**摘要**：{mask_sensitive(p.get('summary', ''))[:200]}")
            return bool(self._push(p.get("notify_users", []) or p.get("assignee", ""),
                                   title, body, event))
        if et == ev.EVENT_TASK_STATUS_CHANGED:
            title = f"任务流转：{p.get('title', '')}"
            body = f"任务《{p.get('title', '')}》状态：{p.get('from', '')}→{p.get('to', '')}"
            return bool(self._push(p.get("notify_users", []), title, body, event))
        # 未支持的事件类型：跳过（返回False，不算失败）
        return False

    def _push(self, users, title: str, content: str, event: dict) -> bool:
        users = [users] if isinstance(users, str) else list(users or [])
        if not users:
            return False
        userid_list = [self.map_user(u) for u in users]
        data = self.send_work_notification(userid_list, mask_sensitive(content), mask_sensitive(title))
        iaudit.log("integration", "dingtalk_send", event.get("contract_id", ""),
                   f"钉钉工作通知已发送（task_id={data.get('task_id')}），接收人{len(userid_list)}人",
                   ext={"event_id": event.get("event_id"), "errcode": data.get("errcode")})
        return data.get("errcode") == 0

    def send_message(self, user: str, title: str, content: str, **kw) -> bool:
        return bool(self._push(user, title, mask_sensitive(content), {"event_id": "", "contract_id": kw.get("contract_id", "")}))

    def health_check(self) -> dict:
        try:
            self.get_access_token()
            return {"ok": True, "detail": "access_token获取成功"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # ---------- 回调验签 ----------
    def verify_callback_signature(self, timestamp: str, nonce: str, body: str, signature: str) -> bool:
        """钉钉回调验签：HMAC-SHA256(key=token, msg='timestamp\\nnonce\\nbody') hex与sign比对"""
        if not self.dingtalk_callback_token:
            return True  # 未配置Token则不校验
        raw = f"{timestamp}\n{nonce}\n{body}".encode("utf-8")
        expect = hmac.new(self.dingtalk_callback_token.encode("utf-8"), raw,
                          hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, (signature or "").lower())
