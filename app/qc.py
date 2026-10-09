"""F9 统计与质检看板增强：审查质检抽样复核 + 规则准确率/误报率统计 + 业务看板增强

- 质检抽样：法务负责人对审查结论抽样复核，每条finding标记correct/wrong/partial+复核意见
- 抽样策略：QC_SAMPLE_RATE环境变量控制抽样比例（0-1），>=1为全量，默认0.2；按比例随机抽样
- 统计：按规则名聚合（db.py视图v_qc_rule_stats），wrong即计误报，形成规则迭代闭环数据
- 业务看板：按部门/合同类型/风险等级多维统计、审查人效率排行（视图v_reviewer_efficiency）
"""
import os
import random
import time
from collections import Counter
from datetime import datetime, timedelta

from . import db

QC_SAMPLE_RATE = float(os.environ.get("QC_SAMPLE_RATE", "0.2"))
VERDICTS = ("correct", "wrong", "partial")


# ---------- 质检抽样 ----------

def assign_batch(contracts_rows: list, created_by: str, rate: float = None) -> dict:
    """生成抽样任务。contracts_rows: db.list_contract_rows()结果（含review jsonb）。
    rate>=1全量；否则每条finding以rate概率随机入选。同一contract+finding幂等不重复。"""
    rate = QC_SAMPLE_RATE if rate is None else max(0.0, min(1.0, float(rate)))
    strategy = "all" if rate >= 1 else "sample"
    batch_id = time.strftime("QCB%Y%m%d%H%M%S") + os.urandom(2).hex()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    total, candidates = 0, []
    for row in contracts_rows:
        review = row.get("review")
        if not review:
            continue
        findings = review.get("findings", []) if isinstance(review, dict) else []
        for f in findings:
            total += 1
            fid = str(f.get("finding_id", ""))
            if rate >= 1 or random.random() < rate:
                candidates.append((row["contract_id"], fid, f))

    inserted = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for cid, fid, f in candidates:
                cur.execute(
                    """INSERT INTO qc_reviews
                       (batch_id, contract_id, finding_id, rule_name, dimension, risk_level,
                        excerpt, ai_conclusion, status, assigned_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                       ON CONFLICT (contract_id, finding_id) DO NOTHING""",
                    (batch_id, cid, fid, str(f.get("rule_name", "")),
                     str(f.get("dimension", "")), str(f.get("risk_level", "")),
                     str(f.get("excerpt", ""))[:500],
                     str(f.get("ai_conclusion", f.get("comment", "")))[:500], now))
                inserted += cur.rowcount
            cur.execute(
                """INSERT INTO qc_batches (batch_id, strategy, rate, total, sampled, created_by, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (batch_id, strategy, rate, total, inserted, created_by, now))
        conn.commit()
    return {"batch_id": batch_id, "strategy": strategy, "rate": rate,
            "finding_total": total, "sampled": inserted}


# ---------- 复核 ----------

def submit_review(qc_id: int, verdict: str, comment: str, reviewer: str) -> bool:
    """提交复核结论：correct/wrong/partial + 复核意见"""
    if verdict not in VERDICTS:
        raise ValueError("复核结论必须是correct/wrong/partial")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE qc_reviews SET verdict=%s, comment=%s, reviewer=%s,
                       status='reviewed', reviewed_at=%s
                   WHERE qc_id=%s""",
                (verdict, comment or "", reviewer,
                 time.strftime("%Y-%m-%d %H:%M:%S"), qc_id))
            ok = cur.rowcount > 0
        conn.commit()
    return ok


# ---------- 统计（视图查询，异常时降级为直接聚合） ----------

def _q(sql: str, params=()):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def rule_stats() -> list:
    """各规则准确率/误报率（读db.py视图v_qc_rule_stats）"""
    return _q("SELECT * FROM v_qc_rule_stats ORDER BY sampled_total DESC")


def qc_progress() -> dict:
    """复核进度：待复核/已复核计数"""
    rows = _q("""SELECT count(*) AS total,
                        count(*) FILTER (WHERE status='reviewed') AS reviewed
                 FROM qc_reviews""")
    r = rows[0]
    total = int(r["total"] or 0)
    reviewed = int(r["reviewed"] or 0)
    return {"total": total, "reviewed": reviewed, "pending": total - reviewed,
            "progress_rate": round(reviewed / total * 100, 1) if total else 0}


def pending_list(limit: int = 50) -> list:
    """待复核列表"""
    return _q("""SELECT qc_id, batch_id, contract_id, finding_id, rule_name, dimension,
                        risk_level, excerpt, ai_conclusion, assigned_at
                 FROM qc_reviews WHERE status='pending'
                 ORDER BY qc_id DESC LIMIT %s""", (limit,))


def qc_overview() -> dict:
    """质检看板汇总：各规则准确率/误报率 + 复核进度 + 待复核列表"""
    return {"progress": qc_progress(), "rule_stats": rule_stats(),
            "pending": pending_list()}


# ---------- 业务看板增强（数据不足返回空集合不报错） ----------

def biz_dimensions(contracts_rows: list) -> dict:
    """按部门/合同类型/风险等级多维统计。
    部门字段当前抽取结果未沉淀（owner/dept缺失），返回空集合占位不报错。"""
    by_type, by_level = Counter(), Counter()
    for row in contracts_rows:
        extracted = row.get("extracted") or {}
        if isinstance(extracted, str):
            try:
                import json as _json
                extracted = _json.loads(extracted)
            except Exception:
                extracted = {}
        by_type[str(extracted.get("contract_type") or "未知")] += 1
        review = row.get("review")
        if isinstance(review, dict):
            for f in review.get("findings", []):
                by_level[str(f.get("risk_level") or "未分级")] += 1
    # 部门维度：数据不足，预留字段（extraction沉淀owner/dept后启用）
    by_dept = {}
    return {"by_department": by_dept,
            "by_contract_type": dict(by_type.most_common()),
            "by_risk_level": dict(by_level)}


def reviewer_efficiency() -> list:
    """审查人效率排行（处理量、平均耗时；视图v_reviewer_efficiency）"""
    try:
        return _q("SELECT * FROM v_reviewer_efficiency LIMIT 20")
    except Exception:
        return []
