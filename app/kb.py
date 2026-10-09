"""知识库管理模块（二期，PostgreSQL存储）
表：
- kb_laws：law_id,law_name,article_no,content,category,
  effective_from,effective_to(可空),valid，带version和updated_at
- kb_templates：tpl_id,name,contract_type,content,updated_at
- 风险规则复用app/rules_data/rules.json，本模块不改动
"""
import uuid
from datetime import date, datetime

from . import db

LAW_FIELDS = {"law_name", "article_no", "content", "category",
              "effective_from", "effective_to", "valid"}
LAW_COLS = ("law_id", "law_name", "article_no", "content", "category",
            "effective_from", "effective_to", "valid", "version", "updated_at")
TPL_COLS = ("tpl_id", "name", "contract_type", "content", "updated_at")


def _row_to_law(r):
    return dict(zip(LAW_COLS, r))


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 法律法规库 ----------

def list_laws(q: str = "", valid: str = ""):
    """列表查询：q按法规名/条款号/内容/分类关键词过滤；valid取值in/out/空"""
    sql = "SELECT %s FROM kb_laws" % ",".join(LAW_COLS)
    where, args = [], []
    if q:
        where.append("lower(law_name||' '||article_no||' '||content||' '||category) LIKE %s")
        args.append("%" + q.lower() + "%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY law_name, article_no"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            laws = [_row_to_law(r) for r in cur.fetchall()]
    result = []
    for law in laws:
        if valid == "in" and not check_effective(law)["effective"]:
            continue
        if valid == "out" and check_effective(law)["effective"]:
            continue
        result.append(law)
    return result


def get_law(law_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM kb_laws WHERE law_id=%%s" % ",".join(LAW_COLS), (law_id,))
            r = cur.fetchone()
    return _row_to_law(r) if r else None


def create_law(data: dict):
    """新增法条；校验必填字段与日期格式，初始version=1"""
    for field in ("law_name", "content"):
        if not str(data.get(field, "")).strip():
            raise ValueError(f"缺少必填字段: {field}")
    law = {
        "law_id": data.get("law_id") or uuid.uuid4().hex[:10].upper(),
        "law_name": data["law_name"],
        "article_no": str(data.get("article_no", "")),
        "content": data["content"],
        "category": str(data.get("category", "")),
        "effective_from": _norm_date(data.get("effective_from", "")),
        "effective_to": _norm_date(data.get("effective_to", "")) if data.get("effective_to") else "",
        "valid": bool(data.get("valid", True)),
        "version": 1,
        "updated_at": _now(),
    }
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO kb_laws (law_id, law_name, article_no, content, category,
                   effective_from, effective_to, valid, version, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (law["law_id"], law["law_name"], law["article_no"], law["content"],
                 law["category"], law["effective_from"], law["effective_to"],
                 law["valid"], law["version"], law["updated_at"]))
        conn.commit()
    return law


def update_law(law_id: str, data: dict):
    """更新法条；version自增，updated_at刷新"""
    fields, args = [], []
    for field in LAW_FIELDS:
        if field not in data:
            continue
        if field in ("effective_from", "effective_to"):
            fields.append(f"{field}=%s")
            args.append(_norm_date(data[field]) if data[field] else "")
        elif field == "valid":
            fields.append(f"{field}=%s")
            args.append(bool(data[field]))
        else:
            fields.append(f"{field}=%s")
            args.append(data[field])
    if not fields:
        raise ValueError("无有效更新字段")
    fields += ["version=version+1", "updated_at=%s"]
    args += [_now(), law_id]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE kb_laws SET %s WHERE law_id=%%s" % ",".join(fields), args)
            if cur.rowcount == 0:
                raise ValueError(f"法条不存在: {law_id}")
        conn.commit()
    return get_law(law_id)


def delete_law(law_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_laws WHERE law_id=%s", (law_id,))
            if cur.rowcount == 0:
                raise ValueError(f"法条不存在: {law_id}")
        conn.commit()
    return True


def check_effective(law: dict):
    """引用时效校验：当前日期是否在[effective_from, effective_to]内且valid为真"""
    today = date.today()
    try:
        frm = date.fromisoformat(law.get("effective_from") or "0001-01-01")
    except ValueError:
        frm = date.min
    to_str = law.get("effective_to") or ""
    try:
        to = date.fromisoformat(to_str) if to_str else date.max
    except ValueError:
        to = date.max
    in_range = frm <= today <= to
    effective = bool(law.get("valid", False)) and in_range
    return {
        "law_id": law.get("law_id"),
        "today": today.isoformat(),
        "effective": effective,
        "in_range": in_range,
        "valid_flag": bool(law.get("valid", False)),
        "reason": "" if effective else (
            "标记为失效" if not law.get("valid", False) else
            ("尚未生效" if today < frm else "已过有效期")),
    }


def _norm_date(value) -> str:
    """规范化日期：接受YYYY-MM-DD或YYYY/MM/DD或空"""
    if value in (None, ""):
        return ""
    s = str(value).strip().replace("/", "-")
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"日期格式无效: {value}，应为YYYY-MM-DD")


# ---------- 合同模板库 ----------

def list_templates(q: str = ""):
    sql = "SELECT %s FROM kb_templates" % ",".join(TPL_COLS)
    args = []
    if q:
        sql += " WHERE lower(name||' '||contract_type||' '||content) LIKE %s"
        args.append("%" + q.lower() + "%")
    sql += " ORDER BY name"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return [dict(zip(TPL_COLS, r)) for r in rows]


def get_template(tpl_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM kb_templates WHERE tpl_id=%%s" % ",".join(TPL_COLS), (tpl_id,))
            r = cur.fetchone()
    return dict(zip(TPL_COLS, r)) if r else None


def create_template(data: dict):
    if not str(data.get("name", "")).strip():
        raise ValueError("缺少必填字段: name")
    tpl = {
        "tpl_id": data.get("tpl_id") or uuid.uuid4().hex[:10].upper(),
        "name": data["name"],
        "contract_type": str(data.get("contract_type", "")),
        "content": str(data.get("content", "")),
        "updated_at": _now(),
    }
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kb_templates (tpl_id, name, contract_type, content, updated_at) VALUES (%s,%s,%s,%s,%s)",
                (tpl["tpl_id"], tpl["name"], tpl["contract_type"], tpl["content"], tpl["updated_at"]))
        conn.commit()
    return tpl


def update_template(tpl_id: str, data: dict):
    fields, args = [], []
    for field in TPL_COLS[1:-1]:  # 除tpl_id和updated_at
        if field in data:
            fields.append(f"{field}=%s")
            args.append(data[field])
    if not fields:
        raise ValueError("无有效更新字段")
    fields.append("updated_at=%s")
    args += [_now(), tpl_id]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE kb_templates SET %s WHERE tpl_id=%%s" % ",".join(fields), args)
            if cur.rowcount == 0:
                raise ValueError(f"模板不存在: {tpl_id}")
        conn.commit()
    return get_template(tpl_id)


def delete_template(tpl_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_templates WHERE tpl_id=%s", (tpl_id,))
            if cur.rowcount == 0:
                raise ValueError(f"模板不存在: {tpl_id}")
        conn.commit()
    return True
