"""审查引擎：规则匹配 + 缺失条款检查 + 汇总统计"""
import json
import re
from pathlib import Path

from .config import RULES_DIR

_RULES_CACHE = None


def load_rules():
    """加载内置规则库（带缓存）"""
    global _RULES_CACHE
    if _RULES_CACHE is None:
        path = RULES_DIR / "rules.json"
        with open(path, encoding="utf-8") as f:
            _RULES_CACHE = json.load(f)
    return _RULES_CACHE


def _applies(rule, contract_type: str) -> bool:
    applies = rule.get("applies_to") or ["*"]
    return "*" in applies or (contract_type in applies)


def _compile(pattern: str):
    try:
        return re.compile(pattern)
    except re.error:
        return None


def review(text: str, clauses: list, extracted) -> list:
    """执行审查，返回findings列表（ReviewFinding字段）"""
    findings = []
    seq = 0
    contract_type = getattr(extracted, "contract_type", "未知") if extracted else "未知"
    full_text = text or ""

    for rule in load_rules():
        if not _applies(rule, contract_type):
            continue
        dimension = rule.get("dimension", "")
        risk_level = rule.get("risk_level", "中")
        name = rule.get("name", "")
        legal_basis = rule.get("legal_basis", "")
        suggestion = rule.get("suggestion", "")

        # (a) patterns逐条款正则匹配
        matched = False
        for clause in clauses:
            clause_text = clause.get("content", "") if isinstance(clause, dict) else clause.content
            clause_id = clause.get("clause_id", "") if isinstance(clause, dict) else clause.clause_id
            if not clause_text:
                continue
            for pat in rule.get("patterns", []):
                rx = _compile(pat)
                if rx and rx.search(clause_text):
                    seq += 1
                    findings.append({
                        "finding_id": f"F{seq:03d}",
                        "dimension": dimension,
                        "risk_level": risk_level,
                        "clause_id": clause_id,
                        "excerpt": clause_text[:120],
                        "rule_name": name,
                        "legal_basis": legal_basis,
                        "suggestion": suggestion,
                    })
                    matched = True
                    break  # 同一条款命中该规则一次即可
            if matched:
                break  # 同一规则整体只报一次，避免刷屏

        # (b) 缺失条款规则：检查全文是否包含missing_of关键词
        missing_of = rule.get("missing_of", "")
        if missing_of:
            rx = re.compile(missing_of)
            if not rx.search(full_text):
                seq += 1
                findings.append({
                    "finding_id": f"F{seq:03d}",
                    "dimension": dimension,
                    "risk_level": risk_level,
                    "clause_id": "",
                    "excerpt": "",
                    "rule_name": name,
                    "legal_basis": legal_basis,
                    "suggestion": suggestion,
                })
    return findings


def build_summary(findings: list) -> dict:
    """按风险等级与审查维度统计"""
    by_level = {}
    by_dimension = {}
    for f in findings:
        lv = f.get("risk_level", "中") if isinstance(f, dict) else f.risk_level
        dim = f.get("dimension", "") if isinstance(f, dict) else f.dimension
        by_level[lv] = by_level.get(lv, 0) + 1
        by_dimension[dim] = by_dimension.get(dim, 0) + 1
    return {"by_level": by_level, "by_dimension": by_dimension}
