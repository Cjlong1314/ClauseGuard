"""范本基线管理与企业差异审查模块（F4）
- 范本基线：司内范本按合同类型沉淀为基线，支持业务线/主体立场多套与版本管理
- 差异审查：待审合同对照基线做条款级差异（复用compare切条+相似度、diff.py对齐/高危标记），
  标出缺失/改写(偏离)/不利于己方条款，并引用基线建议措辞生成修改建议
- compare.py原有相似度接口不改动，仅复用其切分与打分函数
"""
import json
import uuid
from datetime import datetime

from . import db
from . import compare as compare_mod
from .diff import _sim, _is_high_risk, _para_diff

BASELINE_COLS = ("baseline_id", "name", "contract_type", "business_line", "stance",
                 "content", "suggestions", "version", "status",
                 "created_by", "created_at", "updated_at")
VERSION_COLS = ("baseline_id", "version", "content", "comment", "updated_by", "updated_at")

# 不利于己方的关键词（辅助判断，叠加审查点库patterns）
_UNFAVORABLE_KEYWORDS = (
    "免除", "不承担", "概不负责", "无偿", "无限期", "单方", "无需", "自行",
    "不得异议", "放弃", "全部责任", "一切损失", "滞纳金", "最终解释权",
)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_tables():
    """幂等建表（基线主表+版本历史表），随模块加载执行"""
    ddl = """
    CREATE TABLE IF NOT EXISTS kb_template_baselines (
        baseline_id   varchar(32) PRIMARY KEY,
        name          varchar(256) NOT NULL,
        contract_type varchar(64) NOT NULL DEFAULT '',
        business_line varchar(64) NOT NULL DEFAULT '',
        stance        varchar(32) NOT NULL DEFAULT '',
        content       text NOT NULL DEFAULT '',
        suggestions   jsonb NOT NULL DEFAULT '{}',
        version       integer NOT NULL DEFAULT 1,
        status        varchar(16) NOT NULL DEFAULT 'active',
        created_by    varchar(64) NOT NULL DEFAULT '',
        created_at    varchar(32) NOT NULL,
        updated_at    varchar(32) NOT NULL
    );
    CREATE TABLE IF NOT EXISTS kb_baseline_versions (
        baseline_id varchar(32) NOT NULL,
        version     integer NOT NULL,
        content     text NOT NULL DEFAULT '',
        comment     varchar(256) NOT NULL DEFAULT '',
        updated_by  varchar(64) NOT NULL DEFAULT '',
        updated_at  varchar(32) NOT NULL,
        PRIMARY KEY (baseline_id, version)
    );
    """
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


_ensure_tables()


def _row_to_baseline(r):
    b = dict(zip(BASELINE_COLS, r))
    try:
        b["suggestions"] = json.loads(b["suggestions"]) if isinstance(b["suggestions"], str) else (b["suggestions"] or {})
    except (ValueError, TypeError):
        b["suggestions"] = {}
    return b


# ---------- 基线CRUD（含版本管理） ----------

def create_baseline(data: dict, username: str = ""):
    """创建范本基线；同类型/业务线/立场可存在多套（按业务线/主体立场区分）"""
    if not str(data.get("name", "")).strip():
        raise ValueError("缺少必填字段: name")
    b = {
        "baseline_id": data.get("baseline_id") or uuid.uuid4().hex[:10].upper(),
        "name": data["name"],
        "contract_type": str(data.get("contract_type", "")),
        "business_line": str(data.get("business_line", "")),
        "stance": str(data.get("stance", "")),
        "content": str(data.get("content", "")),
        "suggestions": data.get("suggestions") or {},
        "version": 1,
        "status": "active",
        "created_by": username,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO kb_template_baselines
                   (baseline_id,name,contract_type,business_line,stance,content,suggestions,
                    version,status,created_by,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (b["baseline_id"], b["name"], b["contract_type"], b["business_line"],
                 b["stance"], b["content"], db.j(b["suggestions"]), b["version"],
                 b["status"], b["created_by"], b["created_at"], b["updated_at"]))
            cur.execute(
                """INSERT INTO kb_baseline_versions
                   (baseline_id,version,content,comment,updated_by,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (b["baseline_id"], 1, b["content"], "初始版本", username, b["updated_at"]))
        conn.commit()
    return b


def list_baselines(q: str = "", contract_type: str = "", status: str = ""):
    sql = "SELECT %s FROM kb_template_baselines" % ",".join(BASELINE_COLS)
    where, args = [], []
    if q:
        where.append("lower(name||' '||contract_type||' '||business_line||' '||stance) LIKE %s")
        args.append("%" + q.lower() + "%")
    if contract_type:
        where.append("contract_type=%s")
        args.append(contract_type)
    if status:
        where.append("status=%s")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY contract_type, name"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return [_row_to_baseline(r) for r in rows]


def get_baseline(baseline_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM kb_template_baselines WHERE baseline_id=%%s"
                        % ",".join(BASELINE_COLS), (baseline_id,))
            r = cur.fetchone()
    return _row_to_baseline(r) if r else None


def update_baseline(baseline_id: str, data: dict, username: str = ""):
    """更新基线；content变更时版本号自增并写入版本历史，实现范本版本管理"""
    b = get_baseline(baseline_id)
    if not b:
        raise ValueError(f"基线不存在: {baseline_id}")
    fields, args = [], []
    for field in ("name", "contract_type", "business_line", "stance", "status"):
        if field in data:
            fields.append(f"{field}=%s")
            args.append(str(data[field]))
    if "suggestions" in data:
        fields.append("suggestions=%s")
        args.append(db.j(data["suggestions"] or {}))
    new_version = b["version"]
    content_changed = "content" in data and str(data["content"]) != b["content"]
    if content_changed:
        new_version = b["version"] + 1
        fields += ["content=%s", "version=%s"]
        args += [str(data["content"]), new_version]
    if not fields:
        raise ValueError("无有效更新字段")
    fields.append("updated_at=%s")
    args += [_now(), baseline_id]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE kb_template_baselines SET %s WHERE baseline_id=%%s"
                        % ",".join(fields), args)
            if content_changed:
                cur.execute(
                    """INSERT INTO kb_baseline_versions
                       (baseline_id,version,content,comment,updated_by,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (baseline_id, new_version, str(data["content"]),
                     str(data.get("comment", ""))[:256], username, _now()))
        conn.commit()
    return get_baseline(baseline_id)


def list_versions(baseline_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM kb_baseline_versions WHERE baseline_id=%%s ORDER BY version DESC"
                        % ",".join(VERSION_COLS), (baseline_id,))
            rows = cur.fetchall()
    return [dict(zip(VERSION_COLS, r)) for r in rows]


def get_version(baseline_id: str, version: int):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT %s FROM kb_baseline_versions WHERE baseline_id=%%s AND version=%%s"
                        % ",".join(VERSION_COLS), (baseline_id, version))
            r = cur.fetchone()
    return dict(zip(VERSION_COLS, r)) if r else None


def delete_baseline(baseline_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_template_baselines WHERE baseline_id=%s", (baseline_id,))
            cur.execute("DELETE FROM kb_baseline_versions WHERE baseline_id=%s", (baseline_id,))
            if cur.rowcount == 0:
                raise ValueError(f"基线不存在: {baseline_id}")
        conn.commit()
    return True


# ---------- 差异审查 ----------

def _checkpoint_risk_patterns(contract_type: str):
    """从审查点库取风险patterns辅助判断不利条款；异常时静默降级为空"""
    try:
        from . import checkpoints
        cps = checkpoints.list_checkpoints(contract_type=contract_type) if hasattr(checkpoints, "list_checkpoints") else []
        pats = []
        for cp in cps or []:
            for p in (cp.get("patterns") or []):
                if p:
                    pats.append(str(p))
        return pats
    except Exception:
        return []


def _unfavorable(text: str, extra_patterns) -> bool:
    """判断条款是否不利于己方：关键词 + 审查点库patterns"""
    if any(k in text for k in _UNFAVORABLE_KEYWORDS):
        return True
    for p in extra_patterns:
        if p and p in text:
            return True
    return False


def _find_suggestion(item: dict, suggestions: dict) -> str:
    """按基线条目标题/内容匹配建议措辞（suggestions为{标题子串或条目num: 建议文本}）"""
    for key, sug in (suggestions or {}).items():
        if not key:
            continue
        if key in (item.get("title") or "") or key in (item.get("content") or "")[:60]:
            return str(sug)
    return ""


def baseline_diff(contract_clauses: list, baseline: dict,
                  match_threshold: float = 40.0, deviation_threshold: float = 0.98) -> dict:
    """待审合同对照范本基线的条款级差异审查。

    判定逻辑：
    1) 基线按compare.split_template切条，逐条在合同条款中找2-gram相似度最佳匹配；
       低于match_threshold视为"缺失"（合同没有对应条款）
    2) 命中的合同条款与基线条目做文本相似度：低于deviation_threshold（与diff.py的
       "修改"判定0.98口径一致，仅数字/措辞微调也会命中）视为"改写/偏离"；
       改写且命中diff高危关键词或不利关键词的，标记"不利于己方"
    3) 合同中存在、但基线没有对应的条款（新增内容）且含不利关键词，也提示"不利于己方"
    4) 每条偏离/缺失引用基线建议措辞（suggestions）生成修改建议
    """
    suggestions = baseline.get("suggestions") or {}
    extra = _checkpoint_risk_patterns(baseline.get("contract_type", ""))
    tpl_items = compare_mod.split_template(baseline.get("content", ""))

    items = []
    used_clause_ids = set()
    for t in tpl_items:
        t_text = (t["title"] + " " + t["content"]).strip()
        best, best_clause = 0.0, None
        for cl in contract_clauses:
            c_text = ((cl.get("title") or "") + " " + (cl.get("content") or "")).strip()
            s = compare_mod._similarity(t_text, c_text)
            key = t["title"][:8]
            if key and key in c_text:
                s = min(100.0, s + 15)
            if s > best:
                best, best_clause = s, cl
        entry = {
            "baseline_item": {"num": t["num"], "title": t["title"] or t["num"] or "条款",
                              "content": t["content"]},
            "similarity": best,
            "contract_clause_id": (best_clause or {}).get("clause_id", ""),
            "suggestion": "",
        }
        if best < match_threshold:
            entry["type"] = "missing"
            entry["is_high_risk"] = _is_high_risk(t)
            sug = _find_suggestion(t, suggestions) or t["content"]
            entry["suggestion"] = f"合同缺失该必备条款，建议参照范本补充：{sug}"
        else:
            used_clause_ids.add(id(best_clause))
            sim = _sim(t["content"], best_clause.get("content", ""))
            unfavorable = _unfavorable(best_clause.get("content", ""), extra)
            entry["contract_clause"] = {
                "clause_id": best_clause.get("clause_id", ""),
                "title": best_clause.get("title", ""),
                "content": best_clause.get("content", ""),
            }
            if sim < deviation_threshold:
                entry["type"] = "deviation"
                entry["content_similarity"] = round(sim, 4)
                entry["is_high_risk"] = unfavorable or _is_high_risk(best_clause)
                sug = _find_suggestion(t, suggestions)
                prefix = "条款已被改写且可能不利于己方" if unfavorable else "条款与范本存在改写偏离"
                entry["suggestion"] = f"{prefix}，建议按范本措辞调整为：{sug or t['content']}"
            else:
                entry["type"] = "ok"
                entry["is_high_risk"] = False
                if unfavorable:
                    entry["type"] = "unfavorable"
                    entry["is_high_risk"] = True
                    sug = _find_suggestion(t, suggestions)
                    entry["suggestion"] = (f"条款内容基本一致但含不利于己方表述，建议替换为：{sug}"
                                           if sug else "条款含不利于己方表述，建议参照范本措辞删除或改写该表述")
        items.append(entry)

    # 合同新增且不利的条款（基线没有对应要求）
    for cl in contract_clauses:
        if id(cl) in used_clause_ids:
            continue
        c_text = ((cl.get("title") or "") + " " + (cl.get("content") or "")).strip()
        if _unfavorable(c_text, extra):
            items.append({
                "type": "unfavorable", "baseline_item": None,
                "contract_clause_id": cl.get("clause_id", ""),
                "contract_clause": {"clause_id": cl.get("clause_id", ""),
                                    "title": cl.get("title", ""),
                                    "content": cl.get("content", "")},
                "title": cl.get("title", ""), "similarity": 0.0,
                "is_high_risk": _is_high_risk(cl),
                "suggestion": f"该条款不在范本基线内且含不利于己方表述，建议删除或改写：「{cl.get('title', '')}」",
            })

    summary = {
        "baseline_total": len(tpl_items),
        "missing": sum(1 for i in items if i["type"] == "missing"),
        "deviation": sum(1 for i in items if i["type"] == "deviation"),
        "unfavorable": sum(1 for i in items if i["type"] == "unfavorable"),
        "ok": sum(1 for i in items if i["type"] == "ok"),
        "high_risk": sum(1 for i in items if i["is_high_risk"]),
    }
    # 偏离率：非ok条目占基线条目比例
    summary["deviation_rate"] = round(
        (summary["missing"] + summary["deviation"] + summary["unfavorable"]) /
        max(1, summary["baseline_total"]) * 100, 1)
    return {"summary": summary, "items": items}
