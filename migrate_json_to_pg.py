"""一次性迁移：旧JSON数据导入PostgreSQL，成功后清除旧文件

覆盖范围（合同数据原在内存中，无历史可迁）：
- users.json -> users
- sessions.json -> sessions（仅保留未过期会话）
- kb/laws.json -> kb_laws（冲突跳过）
- kb/templates.json -> kb_templates
- collab/tasks.json -> tasks
- collab/notifications.json -> notifications
- audit/audit.jsonl -> audit_log
- laws_custom.json -> rag_laws_custom（存在时）
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import db  # noqa: E402

STORAGE = Path(__file__).resolve().parent / "storage"


def out(s):
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def migrate_users():
    users = load(STORAGE / "users.json", {})
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for name, u in users.items():
                cur.execute(
                    """INSERT INTO users (username, salt, password, role, created_at)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING""",
                    (name, u.get("salt", ""), u.get("password", ""),
                     u.get("role", "assistant"), u.get("created_at", "")))
                n += cur.rowcount
        conn.commit()
    return n


def migrate_sessions():
    now = time.time()
    sessions = {t: s for t, s in load(STORAGE / "sessions.json", {}).items()
                if s.get("expire", 0) > now}
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for token, s in sessions.items():
                cur.execute(
                    "INSERT INTO sessions (token, username, role, expire) VALUES (%s,%s,%s,%s) ON CONFLICT (token) DO NOTHING",
                    (token, s.get("username", ""), s.get("role", "assistant"), s.get("expire", 0)))
                n += cur.rowcount
        conn.commit()
    return n


def migrate_laws():
    laws = load(STORAGE / "kb" / "laws.json", [])
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for l in laws:
                cur.execute(
                    """INSERT INTO kb_laws (law_id, law_name, article_no, content, category,
                       effective_from, effective_to, valid, version, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (law_id) DO NOTHING""",
                    (l.get("law_id"), l.get("law_name", ""), l.get("article_no", ""),
                     l.get("content", ""), l.get("category", ""),
                     l.get("effective_from", ""), l.get("effective_to", ""),
                     bool(l.get("valid", True)), int(l.get("version", 1)),
                     l.get("updated_at", "")))
                n += cur.rowcount
        conn.commit()
    return n


def migrate_templates():
    tpls = load(STORAGE / "kb" / "templates.json", [])
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for t in tpls:
                cur.execute(
                    """INSERT INTO kb_templates (tpl_id, name, contract_type, content, updated_at)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tpl_id) DO NOTHING""",
                    (t.get("tpl_id"), t.get("name", ""), t.get("contract_type", ""),
                     t.get("content", ""), t.get("updated_at", "")))
                n += cur.rowcount
        conn.commit()
    return n


def migrate_tasks():
    tasks = load(STORAGE / "collab" / "tasks.json", [])
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for t in tasks:
                cur.execute(
                    """INSERT INTO tasks (task_id, contract_id, contract_filename, title, assignee,
                       reviewer, due_date, status, created_by, created_at, updated_at, history)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (task_id) DO NOTHING""",
                    (t.get("task_id"), t.get("contract_id", ""), t.get("contract_filename", ""),
                     t.get("title", ""), t.get("assignee", ""), t.get("reviewer", ""),
                     t.get("due_date", ""), t.get("status", "pending"),
                     t.get("created_by", ""), t.get("created_at", ""), t.get("updated_at", ""),
                     db.j(t.get("history", []))))
                n += cur.rowcount
        conn.commit()
    return n


def migrate_notifications():
    items = load(STORAGE / "collab" / "notifications.json", [])
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for x in items:
                cur.execute(
                    """INSERT INTO notifications (notify_id, username, title, content, read, time)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (notify_id) DO NOTHING""",
                    (x.get("notify_id"), x.get("username", ""), x.get("title", ""),
                     x.get("content", ""), bool(x.get("read", False)), x.get("time", "")))
                n += cur.rowcount
        conn.commit()
    return n


def migrate_audit():
    path = STORAGE / "audit" / "audit.jsonl"
    n = 0
    if not path.exists():
        return n
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cur.execute(
                    """INSERT INTO audit_log (time, username, action, contract_id, detail)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (r.get("time", ""), r.get("username", ""), r.get("action", ""),
                     r.get("contract_id", ""), r.get("detail", "")))
                n += 1
        conn.commit()
    return n


def migrate_rag_custom():
    path = STORAGE / "laws_custom.json"
    n = 0
    laws = load(path, [])
    if not laws:
        return n
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for i, l in enumerate(laws):
                cur.execute(
                    "INSERT INTO rag_laws_custom (law_id, law) VALUES (%s,%s) ON CONFLICT (law_id) DO NOTHING",
                    (str(l.get("law_id", "custom_%d" % i)), db.j(l)))
                n += cur.rowcount
        conn.commit()
    return n


def main():
    counts = {
        "users": migrate_users(),
        "sessions": migrate_sessions(),
        "kb_laws": migrate_laws(),
        "kb_templates": migrate_templates(),
        "tasks": migrate_tasks(),
        "notifications": migrate_notifications(),
        "audit_log": migrate_audit(),
        "rag_laws_custom": migrate_rag_custom(),
    }
    for k, v in counts.items():
        out("%s: 迁移%d条" % (k, v))

    # 数据核对无误后清除旧文件
    import shutil
    removed = []
    targets = [STORAGE / "users.json", STORAGE / "sessions.json",
               STORAGE / "audit", STORAGE / "collab", STORAGE / "kb",
               STORAGE / "laws_custom.json"]
    for t in targets:
        if t.exists():
            if t.is_dir():
                shutil.rmtree(t)
            else:
                t.unlink()
            removed.append(t.name)
    out("已清除旧文件: %s" % ", ".join(removed))
    out("迁移完成")


if __name__ == "__main__":
    main()
