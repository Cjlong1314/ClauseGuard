"""审查留痕模块：audit_log表（PostgreSQL）

F2扩展：log支持ext扩展字段（JSONB），用于记录AI研判过程
（模型版本、提示词版本、召回依据、阶段轨迹），query返回时一并带出。
"""
import json
from datetime import datetime

from . import db


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(username: str, action: str, contract_id: str = "", detail: str = "", ext: dict = None):
    """追加一条审计记录；ext为可选扩展字段（dict，序列化为JSONB存储）"""
    ext_json = json.dumps(ext, ensure_ascii=False) if ext else None
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (time, username, action, contract_id, detail, ext) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (_now(), username, action, contract_id, detail, ext_json))
        conn.commit()


def query(contract_id: str = None, limit: int = 200) -> list:
    """查询审计记录，可按contract_id过滤，返回最新limit条（时间倒序）"""
    sql = "SELECT time, username, action, contract_id, detail, ext FROM audit_log"
    args = []
    if contract_id:
        sql += " WHERE contract_id=%s"
        args.append(contract_id)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return [{"time": r[0], "username": r[1], "action": r[2],
             "contract_id": r[3], "detail": r[4], "ext": r[5]} for r in rows]
