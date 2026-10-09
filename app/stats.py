"""统计看板模块（三期）：汇总合同/审查/风险分布/审查趋势/任务完成率"""
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .config import STORAGE_DIR
from . import audit as audit_mod

COLLAB_FILE = STORAGE_DIR / "collab" / "tasks.json"
RANK_ORDER = ["高", "中", "低"]
DIMS = ["违法违规", "缺失条款", "风险条款", "格式规范"]


def _count_review_findings():
    """从内存合同对象统计发现总数与分布（由main.py注入合同字典）"""
    return None  # 占位，实际由stats_overview接收参数


def stats_overview(contracts: dict, ) -> dict:
    """contracts: {cid: Contract}；输出看板数据"""
    total = len(contracts)
    reviewed = sum(1 for c in contracts.values() if c.review is not None)
    findings = []
    for c in contracts.values():
        if c.review:
            findings.extend(c.review.findings)
    by_level = Counter(f.risk_level for f in findings)
    by_dim = Counter(f.dimension for f in findings)

    # 最近7天每日审查次数（action=review按日聚合）
    trend = defaultdict(int)
    today = datetime.now().date()
    days = [(today - timedelta(days=i)).strftime("%m-%d") for i in range(6, -1, -1)]
    for r in audit_mod.query(limit=10000):
        if r.get("action") == "review":
            d = (r.get("time") or "")[:10]
            trend[d] += 1
    trend_list = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        trend_list.append({"date": d.strftime("%m-%d"), "count": trend.get(d.strftime("%Y-%m-%d"), 0)})

    return {
        "contract_total": total,
        "reviewed_total": reviewed,
        "finding_total": len(findings),
        "by_level": {k: by_level.get(k, 0) for k in RANK_ORDER},
        "by_dimension": {k: by_dim.get(k, 0) for k in DIMS},
        "review_trend": trend_list,
        "task": task_stats(),
    }


def task_stats() -> dict:
    """任务完成率：从数据库tasks表统计"""
    from . import db
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), count(*) FILTER (WHERE status='done') FROM tasks")
                total, done = cur.fetchone()
    except Exception:
        return {"total": 0, "done": 0, "completion_rate": 0}
    rate = round(done / total * 100, 1) if total else 0
    return {"total": total, "done": done, "completion_rate": rate}
