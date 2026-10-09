"""条款级版本比对模块（F1）
对两个版本合同的条款树做对齐（clause_id匹配+文本相似度兜底），
段落级diff用difflib；标出新增/删除/修改条款；
金额/期限/责任条款变更单独标记为高危。
"""
import difflib
import re

# 高危关键词：金额/期限/责任类变更视为高危改动
HIGH_RISK_KEYWORDS = (
    "金额", "价款", "费用", "元", "万元", "违约金", "定金", "保证金",
    "期限", "工期", "到期", "终止", "解除", "责任", "赔偿", "承担",
    "连带", "担保", "利息", "罚款", "逾期",
)

_CHANGE_PAT = re.compile(r"[\u4e00-\u9fa50-9]")


def _norm(s: str) -> str:
    return "".join((s or "").split())


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a[:200], b[:200]).ratio()


def _is_high_risk(clause) -> bool:
    text = f"{clause.get('title', '')} {clause.get('content', '')}"
    return any(k in text for k in HIGH_RISK_KEYWORDS)


def _para_diff(old_text: str, new_text: str) -> list:
    """段落级diff，返回[{type, text}]，type: same/insert/delete"""
    old_lines = [l for l in (old_text or "").splitlines() if l.strip()]
    new_lines = [l for l in (new_text or "").splitlines() if l.strip()]
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for l in old_lines[i1:i2]:
                out.append({"type": "same", "text": l})
        else:
            for l in old_lines[i1:i2]:
                out.append({"type": "delete", "text": l})
            for l in new_lines[j1:j2]:
                out.append({"type": "insert", "text": l})
    return out


def diff_clauses(old_clauses: list, new_clauses: list,
                 sim_threshold: float = 0.55) -> dict:
    """条款树对齐比对。

    old/new_clauses: [{clause_id,title,content,level}]
    返回{summary, items}，item:
      {change_type: added/removed/modified/unchanged, clause_id, title,
       old_title, new_title, content_similarity, is_high_risk, para_diff}
    对齐策略：1) clause_id相同优先配对；2) 剩余按相似度贪心配对。
    """
    old_by_id = {_norm(c.get("clause_id", "")): c for c in old_clauses}
    new_by_id = {_norm(c.get("clause_id", "")): c for c in new_clauses}

    matched = []          # [(old, new)]
    used_old, used_new = set(), set()

    # 1) id相同
    for k, oc in old_by_id.items():
        if k and k in new_by_id:
            matched.append((oc, new_by_id[k]))
            used_old.add(id(oc))
            used_new.add(id(new_by_id[k]))

    # 2) 相似度贪心配对（阈值以上才视为同一条款的修改）
    rest_old = [c for c in old_clauses if id(c) not in used_old]
    rest_new = [c for c in new_clauses if id(c) not in used_new]
    pairs = []
    for oc in rest_old:
        best, bs = None, sim_threshold
        for nc in rest_new:
            s = _sim(f"{oc.get('title', '')} {oc.get('content', '')}",
                     f"{nc.get('title', '')} {nc.get('content', '')}")
            if s > bs:
                best, bs = nc, s
        if best is not None:
            pairs.append((bs, oc, best))
    pairs.sort(key=lambda x: -x[0])
    for _, oc, nc in pairs:
        if id(oc) in used_old or id(nc) in used_new:
            continue
        matched.append((oc, nc))
        used_old.add(id(oc))
        used_new.add(id(nc))

    items = []
    for oc, nc in matched:
        sim = _sim(oc.get("content", ""), nc.get("content", ""))
        title_changed = _norm(oc.get("title", "")) != _norm(nc.get("title", ""))
        changed = sim < 0.98 or title_changed
        items.append({
            "change_type": "modified" if changed else "unchanged",
            "clause_id": nc.get("clause_id") or oc.get("clause_id"),
            "old_title": oc.get("title", ""),
            "new_title": nc.get("title", ""),
            "title_changed": title_changed,
            "content_similarity": round(sim, 4),
            "is_high_risk": changed and (_is_high_risk(oc) or _is_high_risk(nc)),
            "para_diff": _para_diff(oc.get("content", ""), nc.get("content", "")) if changed else [],
            "old_clause": oc, "new_clause": nc,
        })
    for c in new_clauses:
        if id(c) not in used_new:
            items.append({
                "change_type": "added", "clause_id": c.get("clause_id"),
                "old_title": "", "new_title": c.get("title", ""),
                "title_changed": False, "content_similarity": 0.0,
                "is_high_risk": _is_high_risk(c),
                "para_diff": [{"type": "insert", "text": l}
                              for l in (c.get("content", "") or "").splitlines() if l.strip()],
                "old_clause": None, "new_clause": c,
            })
    for c in old_clauses:
        if id(c) not in used_old:
            items.append({
                "change_type": "removed", "clause_id": c.get("clause_id"),
                "old_title": c.get("title", ""), "new_title": "",
                "title_changed": False, "content_similarity": 0.0,
                "is_high_risk": _is_high_risk(c),
                "para_diff": [{"type": "delete", "text": l}
                              for l in (c.get("content", "") or "").splitlines() if l.strip()],
                "old_clause": c, "new_clause": None,
            })

    # 排序：按旧版在前新版在后的文档顺序近似（用clause_id数值化粗排）
    def _sort_key(it):
        cid = _norm(it.get("clause_id", "") or "0")
        nums = re.findall(r"\d+", cid)
        return tuple(int(n) for n in nums) if nums else (9999,)

    items.sort(key=_sort_key)

    summary = {
        "total": len(items),
        "added": sum(1 for i in items if i["change_type"] == "added"),
        "removed": sum(1 for i in items if i["change_type"] == "removed"),
        "modified": sum(1 for i in items if i["change_type"] == "modified"),
        "unchanged": sum(1 for i in items if i["change_type"] == "unchanged"),
        "high_risk": sum(1 for i in items if i["is_high_risk"]),
    }
    return {"summary": summary, "items": items}
