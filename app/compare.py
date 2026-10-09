"""模板比对模块（三期）：合同与标准模板的条款差异比对
比对逻辑：
- 模板content按行/编号切分为必备条款条目
- 对模板每一条，在合同各条款中做相似度匹配（字符2-gram Jaccard + 关键词包含加权）
- 相似度>=阈值(40%)视为命中，否则视为缺失
- 整体相似度 = 命中条款加权平均
"""
import re

_THRESHOLD = 40  # 命中相似度阈值(%)


def _bigrams(s: str) -> set:
    s = re.sub(r"[\s，。、；：！？（）()【】\[\]\"\"'']", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s} if s else set()


def _similarity(a: str, b: str) -> float:
    """2-gram Jaccard相似度，返回0~100"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return round(inter / len(ga | gb) * 100, 1)


def split_template(content: str) -> list:
    """把模板文本切分成条目：优先按条款编号，否则按非空行"""
    items = []
    cur_num, cur_title, buf = "", "", []

    def flush():
        if buf or cur_title:
            items.append({"num": cur_num, "title": cur_title, "content": "\n".join(buf).strip()})

    num_pat = re.compile(r"^(第[一二三四五六七八九十百\d]+条|\d+(?:\.\d+)*)[、\s]?(.*)$")
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = num_pat.match(line)
        if m:
            flush()
            cur_num, cur_title = m.group(1), m.group(2).strip()
            buf = [m.group(2).strip()] if m.group(2).strip() else []
        else:
            buf.append(line)
    flush()
    if not items:  # 无结构模板，整段作为一条
        items = [{"num": "", "title": "模板要求", "content": (content or "").strip()}]
    return items


def compare(contract_clauses: list, template_content: str) -> dict:
    """contract_clauses: [{clause_id,title,content}]"""
    tpl_items = split_template(template_content)
    matched, missing = [], []
    sims = []
    for t in tpl_items:
        t_text = (t["title"] + " " + t["content"]).strip()
        best, best_clause = 0.0, ""
        for cl in contract_clauses:
            c_text = ((cl.get("title") or "") + " " + (cl.get("content") or "")).strip()
            s = _similarity(t_text, c_text)
            # 主题词包含提升：模板条目前10字出现在合同条款中加成
            key = t["title"][:8]
            if key and key in c_text:
                s = min(100.0, s + 15)
            if s > best:
                best, best_clause = s, cl.get("clause_id", "")
        entry = {"title": t["title"] or t["num"] or "条款", "similarity": best,
                 "contract_clause_id": best_clause}
        if best >= _THRESHOLD:
            matched.append(entry)
            sims.append(best)
        else:
            missing.append(entry)
    overall = round(sum(sims) / len(sims), 1) if sims else 0
    return {"matched": matched, "missing": missing, "similarity": overall,
            "template_items": len(tpl_items)}


# 兼容别名：main.py中以compare_to_template调用
compare_to_template = compare
