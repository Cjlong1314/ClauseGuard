"""集成回调webhook路由（F7）

回调接口不要求登录（外部系统调用），以验签代替鉴权；所有回调写审计。
"""
from fastapi import APIRouter, Request, HTTPException

from .. import audit as audit_mod
from .base import get_connector
from .dingtalk.connector import DingTalkConnector
from .esignbao.connector import ESignBaoConnector, ESIGN_FLOW_STATUS_MAP
from . import events as ev
import json as _json

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.post("/dingtalk/callback")
async def dingtalk_callback(request: Request):
    """钉钉审查结果回传回调：验签→处理→写审计。

    钉钉回调请求头：x-dingtalk-signature（HMAC-SHA256，msg=timestamp\\nnonce\\nbody，key=Token），
    query含timestamp/nonce。验签失败返回403。
    """
    body = (await request.body()).decode("utf-8")
    conn = get_connector("dingtalk")
    if not isinstance(conn, DingTalkConnector):
        raise HTTPException(503, "钉钉Connector未启用")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    signature = request.headers.get("x-dingtalk-signature", "")
    if not conn.verify_callback_signature(timestamp, nonce, body, signature):
        audit_mod.log("dingtalk", "dingtalk_callback_rejected", "",
                      "回调验签失败")
        raise HTTPException(403, "签名校验失败")
    try:
        data = __import__("json").loads(body) if body else {}
    except ValueError:
        raise HTTPException(400, "非法JSON")
    audit_mod.log("dingtalk", "dingtalk_callback", str(data.get("contract_id", "")),
                  f"收到钉钉回调：{str(data)[:300]}",
                  ext={"event": data.get("event_type", ""), "source": "dingtalk"})
    # 回调结果回写：此处只留痕+透传事件；签署类状态回写由对应Connector处理
    return {"code": 0, "msg": "ok"}


@router.post("/esignbao/callback")
async def esignbao_callback(request: Request):
    """e签宝签署状态回调：验签→审计→解析签署状态→发布esign.status_changed事件。

    e签宝回调请求头：x-esignbao-signature（HMAC-SHA256(key=回调密钥, msg=body原文) hex）。
    回调body示例：{"flowId": "...", "signFlowStatus": 2, "contract_id": "..."}。
    验签失败返回403；事件派发失败由总线落重试队列，接口始终返回200（e签宝重推机制）。
    """
    body = (await request.body()).decode("utf-8")
    conn = get_connector("esignbao")
    if not isinstance(conn, ESignBaoConnector):
        raise HTTPException(503, "e签宝Connector未启用")
    signature = request.headers.get("x-esignbao-signature", "")
    if not conn.verify_callback_signature(body, signature):
        audit_mod.log("esignbao", "esignbao_callback_rejected", "",
                      "回调验签失败")
        raise HTTPException(403, "签名校验失败")
    try:
        data = _json.loads(body) if body else {}
    except ValueError:
        raise HTTPException(400, "非法JSON")
    # 解析签署状态：signFlowStatus数字→内部枚举，非法值按SIGNING兜底
    status = ESIGN_FLOW_STATUS_MAP.get(data.get("signFlowStatus"), "SIGNING")
    if status not in ev.ESIGN_STATUSES:
        status = "SIGNING"
    flow_id = str(data.get("flowId", ""))
    contract_id = str(data.get("contract_id", ""))
    audit_mod.log("esignbao", "esignbao_callback", contract_id,
                  f"收到e签宝回调：flow={flow_id}，signFlowStatus={data.get('signFlowStatus')}→{status}",
                  ext={"flow_id": flow_id, "status": status, "source": "esignbao"})
    # 按事件处理：发布esign.status_changed，由Connector完成状态查询核实与回写
    from . import hub
    hub.publish(ev.EVENT_ESIGN_STATUS_CHANGED,
                {"flow_id": flow_id, "status": status,
                 "contract_filename": data.get("contract_filename", ""),
                 "callback": True},
                actor="esignbao", contract_id=contract_id)
    return {"code": 0, "msg": "ok"}


@router.get("/status")
async def integrations_status(request: Request):
    """Connector运行状态（健康检查汇总）"""
    from .base import status as hub_status
    return hub_status()
