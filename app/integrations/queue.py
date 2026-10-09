"""失败重试队列（F7 outbox模式）

外发失败→integration_outbox落库→后台线程按INTEGRATIONS_RETRY_INTERVAL轮询重试，
超过INTEGRATIONS_MAX_RETRY次标记failed终态。所有状态变更写审计。
"""
import threading
import time
import traceback
from datetime import datetime

from .. import config
from .. import db
from .. import audit as iaudit
from .base import get_connector

_worker_started = False
_worker_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def enqueue_outbox(connector: str, event_type: str, event: dict, error: str = ""):
    """失败事件落库（connector+event_id幂等：同connector同event_id只留一条待重试）"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO integration_outbox (connector, event_type, payload, status,
                   retry_count, next_retry, last_error, created_at, updated_at)
                   SELECT %s,%s,%s,'pending',0,%s,%s,%s,%s
                   WHERE NOT EXISTS (SELECT 1 FROM integration_outbox
                     WHERE connector=%s AND payload->>'event_id'=%s AND status='pending')""",
                (connector, event_type, db.j(event), time.time(), error,
                 _now(), _now(), connector, event.get("event_id", "")))
        conn.commit()


def outbox_items(status: str = None, limit: int = 100) -> list:
    sql = """SELECT id, connector, event_type, payload, status, retry_count, last_error,
             created_at, updated_at FROM integration_outbox"""
    args = []
    if status:
        sql += " WHERE status=%s"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return [dict(zip(("id", "connector", "event_type", "payload", "status", "retry_count",
                      "last_error", "created_at", "updated_at"), r)) for r in rows]


def process_pending() -> dict:
    """扫描待重试条目并重试一次；由后台线程与自测脚本调用"""
    stats = {"retried": 0, "sent": 0, "failed": 0, "exhausted": 0}
    now = time.time()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, connector, event_type, payload, retry_count
                           FROM integration_outbox WHERE status='pending' AND next_retry<=%s
                           ORDER BY id""", (now,))
            rows = cur.fetchall()
    for rid, connector, event_type, payload_json, retry_count in rows:
        stats["retried"] += 1
        event = payload_json if isinstance(payload_json, dict) else __import__("json").loads(payload_json)
        err = ""
        try:
            conn_obj = get_connector(connector)
            if conn_obj is None:
                err = f"Connector[{connector}]未启用或加载失败，暂停重试"
                new_status, next_retry = "pending", now + config.INTEGRATIONS_RETRY_INTERVAL
            else:
                conn_obj.send_event(event)  # 成功；失败抛异常
                new_status, next_retry = "sent", None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            retry_count += 1
            if retry_count >= config.INTEGRATIONS_MAX_RETRY:
                new_status, next_retry = "failed", None
                stats["exhausted"] += 1
            else:
                new_status, next_retry = "pending", now + config.INTEGRATIONS_RETRY_INTERVAL
            stats["failed"] += 1
        else:
            stats["sent"] += 1
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE integration_outbox SET status=%s, retry_count=%s,
                               next_retry=%s, last_error=%s, updated_at=%s WHERE id=%s""",
                            (new_status, retry_count, next_retry, err, _now(), rid))
            conn.commit()
        iaudit.log("integration", "outbox_retry", event.get("contract_id", ""),
                   f"outbox#{rid} connector={connector} event={event_type} 重试结果={new_status}"
                   + (f"（{err}）" if err else ""))
    return stats


def _worker():
    while True:
        try:
            process_pending()
        except Exception:
            traceback.print_exc()
        time.sleep(config.INTEGRATIONS_RETRY_INTERVAL)


def start_worker():
    """启动后台重试线程（应用启动时调用一次）"""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_worker, daemon=True).start()
        print(f"[integrations] 重试队列线程已启动（间隔{config.INTEGRATIONS_RETRY_INTERVAL}s）")
