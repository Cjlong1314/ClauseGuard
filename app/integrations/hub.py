"""事件总线（F7）：系统事件的发布/订阅中枢

- publish(event_type, payload)：发布事件到总线；内置订阅器将事件异步派发给所有已启用的
  Connector（不阻塞调用方主流程，失败落integration_outbox重试队列+审计记录）
- subscribe(event_type, handler)：供业务代码或Connector订阅特定事件

设计原则：publish永远不抛异常（集成失败不影响审查主流程）。
"""
import threading
import traceback
import uuid
from datetime import datetime

from . import events as ev
from .base import get_connector, enabled_connectors
from .. import audit as iaudit
from .queue import enqueue_outbox

_bus_lock = threading.Lock()
_subscribers = {}  # event_type -> [handler]


def subscribe(event_type: str, handler):
    """注册同步订阅器；handler(event: dict)异常会被吞掉并打印"""
    with _bus_lock:
        _subscribers.setdefault(event_type, []).append(handler)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def publish(event_type: str, payload: dict, actor: str = "system", contract_id: str = ""):
    """发布事件：校验类型→通知本地订阅器→异步派发给已启用Connector。

    payload为业务数据dict；公共字段由总线补齐（event_id/event_type/time）。
    永不抛异常。
    """
    if event_type not in ev.ALL_EVENTS:
        print(f"[integrations] 未知事件类型{event_type}，忽略")
        return None
    event = {
        "event_id": uuid.uuid4().hex[:16],
        "event_type": event_type,
        "time": _now(),
        "actor": actor,
        "contract_id": contract_id,
        "payload": payload or {},
    }
    # 1) 本地订阅器（同步、尽力而为）
    for h in list(_subscribers.get(event_type, [])):
        try:
            h(event)
        except Exception:
            traceback.print_exc()
    # 2) Connector异步派发
    for name in enabled_connectors():
        _dispatch_async(name, event)
    return event


def _dispatch(connector_name: str, event: dict):
    """同步派发给单个Connector；失败落outbox重试队列并审计"""
    conn = get_connector(connector_name)
    if not conn:
        return
    try:
        ok = conn.send_event(event)
        iaudit.log("integration", "integration_event", event.get("contract_id", ""),
                   f"Connector[{connector_name}]处理事件{event['event_type']}："
                   f"{'成功' if ok else '拒绝/跳过'}",
                   ext={"event_id": event["event_id"], "connector": connector_name,
                        "event_type": event["event_type"]})
    except Exception as e:
        # 失败：落重试队列（含首次失败本次的记录），审计留痕，不阻塞
        enqueue_outbox(connector_name, event["event_type"], event, error=str(e))
        iaudit.log("integration", "integration_event_failed", event.get("contract_id", ""),
                   f"Connector[{connector_name}]处理事件{event['event_type']}失败：{e}",
                   ext={"event_id": event["event_id"], "connector": connector_name})


def _dispatch_async(connector_name: str, event: dict):
    threading.Thread(target=_dispatch, args=(connector_name, event), daemon=True).start()
