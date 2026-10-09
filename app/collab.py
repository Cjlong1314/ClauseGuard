"""协同流程模块（三期）：审查任务分配/流转/催办+消息通知（PostgreSQL存储）
任务状态机：pending→reviewing→review_done→done，任意未完成状态可cancelled，cancelled可回到pending
"""
import json
import uuid
from datetime import datetime

from . import db
from . import audit as audit_mod

VALID_STATUS = {"pending", "reviewing", "review_done", "done", "cancelled"}
# 允许的状态流转
TRANSITIONS = {
    "pending": {"reviewing", "cancelled"},
    "reviewing": {"review_done", "cancelled"},
    "review_done": {"done", "reviewing"},
    "done": set(),
    "cancelled": {"pending"},
}

TASK_ST_NAMES = {"pending": "待处理", "reviewing": "审查中", "review_done": "待复核",
                 "done": "已完成", "cancelled": "已取消"}

TASK_COLS = ("task_id", "contract_id", "contract_filename", "title", "assignee",
             "reviewer", "due_date", "status", "created_by", "created_at",
             "updated_at", "history")


def _row_to_task(r):
    t = dict(zip(TASK_COLS, r))
    t["history"] = t["history"] or []
    return t


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _notify(username: str, title: str, content: str):
    """给指定用户发通知"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notifications (notify_id, username, title, content, read, time) VALUES (%s,%s,%s,%s,false,%s)",
                (uuid.uuid4().hex[:10], username, title, content, _now()))
        conn.commit()


def create_task(username: str, data: dict, contract_filename: str = ""):
    for k in ("contract_id", "title", "assignee"):
        if not data.get(k):
            raise ValueError(f"缺少必填字段: {k}")
    task = {
        "task_id": uuid.uuid4().hex[:10],
        "contract_id": data["contract_id"],
        "contract_filename": contract_filename,
        "title": data["title"],
        "assignee": data["assignee"],
        "reviewer": data.get("reviewer", ""),
        "due_date": data.get("due_date", ""),
        "status": "pending",
        "created_by": username,
        "created_at": _now(),
        "updated_at": _now(),
        "history": [{"time": _now(), "user": username, "action": "create", "detail": "创建任务"}],
    }
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO tasks (task_id, contract_id, contract_filename, title, assignee,
                   reviewer, due_date, status, created_by, created_at, updated_at, history)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (task["task_id"], task["contract_id"], task["contract_filename"], task["title"],
                 task["assignee"], task["reviewer"], task["due_date"], task["status"],
                 task["created_by"], task["created_at"], task["updated_at"], db.j(task["history"])))
        conn.commit()
    audit_mod.log(username, "task_create", data["contract_id"], f"创建任务《{task['title']}》")
    _notify(data["assignee"], "新任务分配", f"您有新的审查任务《{task['title']}》，截止{task['due_date'] or '未设置'}")
    if task["reviewer"]:
        _notify(task["reviewer"], "复核任务", f"《{task['title']}》指定您为复核人")
    return task


def list_tasks():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM tasks ORDER BY created_at DESC" % ",".join(TASK_COLS))
            rows = cur.fetchall()
    return [_row_to_task(r) for r in rows]


def _get_task(task_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM tasks WHERE task_id=%%s" % ",".join(TASK_COLS), (task_id,))
            r = cur.fetchone()
    return _row_to_task(r) if r else None


def change_status(username: str, task_id: str, new_status: str):
    if new_status not in VALID_STATUS:
        raise ValueError("无效状态")
    task = _get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    cur = task["status"]
    if new_status == cur:
        raise ValueError("状态未变化")
    if new_status not in TRANSITIONS.get(cur, set()):
        raise ValueError(f"不允许从{TASK_ST_NAMES[cur]}流转到{TASK_ST_NAMES[new_status]}")
    history = task["history"] + [{"time": _now(), "user": username, "action": "status",
                                  "detail": f"{TASK_ST_NAMES[cur]}→{TASK_ST_NAMES[new_status]}"}]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET status=%s, updated_at=%s, history=%s WHERE task_id=%s",
                        (new_status, _now(), db.j(history), task_id))
        conn.commit()
    task["status"] = new_status
    task["history"] = history
    audit_mod.log(username, "task_status", task["contract_id"],
                  f"任务《{task['title']}》{TASK_ST_NAMES[cur]}→{TASK_ST_NAMES[new_status]}")
    # F7：发布任务状态流转事件
    from .integrations import hub
    hub.publish("task.status_changed",
                {"task_id": task_id, "title": task["title"], "from": TASK_ST_NAMES[cur],
                 "to": TASK_ST_NAMES[new_status], "notify_users": [task["created_by"]]},
                actor=username, contract_id=task["contract_id"])
    _notify(task["created_by"], "任务状态变更",
            f"任务《{task['title']}》已流转为{TASK_ST_NAMES[new_status]}（操作人：{username}）")
    return task


def remind(username: str, task_id: str):
    """催办：向审查人发通知"""
    task = _get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    _notify(task["assignee"], "任务催办", f"任务《{task['title']}》被催办，请尽快处理（催办人：{username}）")
    audit_mod.log(username, "task_remind", task["contract_id"], f"催办任务《{task['title']}》")
    # F7：发布催办事件
    from .integrations import hub
    hub.publish("task.remind",
                {"task_id": task_id, "title": task["title"], "assignee": task["assignee"]},
                actor=username, contract_id=task["contract_id"])
    return task


def list_notifications(username: str, unread_only: bool = False):
    sql = "SELECT notify_id, username, title, content, read, time FROM notifications WHERE username=%s"
    if unread_only:
        sql += " AND read=false"
    sql += " ORDER BY time DESC"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (username,))
            rows = cur.fetchall()
    return [{"notify_id": r[0], "username": r[1], "title": r[2], "content": r[3],
             "read": r[4], "time": r[5]} for r in rows]


def mark_read(username: str, notify_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read=true WHERE notify_id=%s AND username=%s",
                        (notify_id, username))
            if cur.rowcount == 0:
                raise ValueError("通知不存在")
        conn.commit()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT notify_id, username, title, content, read, time FROM notifications WHERE notify_id=%s",
                        (notify_id,))
            r = cur.fetchone()
    return {"notify_id": r[0], "username": r[1], "title": r[2], "content": r[3],
            "read": r[4], "time": r[5]}
