"""法条召回与LLM研判（RAG）
- build_index()/search()：纯Python关键词打分召回（2-gram+整词匹配，无重依赖）
- analyze_with_llm()：组装提示词调LLM研判，失败返回None
"""
import json
import re
from pathlib import Path

from . import llm

DATA_DIR = Path(__file__).resolve().parent / "rag_data"
_LAWS = None
_STOP = set("的及其或与在和对于本该等约按依照据由自为以从至之日前后内之间中的属于如有双方当事人合同可以不得应当".split())


def _load_laws():
    """加载法条库（内置种子+数据库rag_laws_custom自定义扩充，带缓存）"""
    global _LAWS
    if _LAWS is None:
        laws = []
        seed = DATA_DIR / "laws_seed.json"
        if seed.exists():
            laws.extend(json.loads(seed.read_text(encoding="utf-8")))
        try:
            from . import db
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT law FROM rag_laws_custom")
                    for (law,) in cur.fetchall():
                        laws.append(law)
        except Exception:
            pass
        _LAWS = [l for l in laws if l.get("effective", True)]
    return _LAWS


def reload_laws():
    """清空缓存，下次search时重新加载（法规库更新后调用）"""
    global _LAWS
    _LAWS = None


def _tokens(text: str):
    """2-gram切分+整词保留，过滤单字停用词"""
    text = re.sub(r"\s+", "", text)
    words = []
    for w in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text):
        if len(w) > 1 and w not in _STOP:
            words.append(w)
    grams = [text[i:i + 2] for i in range(len(text) - 1)]
    return words, grams


def build_index():
    """MVP无向量索引，预加载法条即可（与向量方案接口兼容）"""
    _load_laws()
    return len(_load_laws())


def search(query: str, top_k: int = 5) -> list:
    """关键词打分召回：整词命中5分/字、2-gram命中1.5分，法条名称与内容加权

    F2起推荐使用 recall_with_sources()（向量召回+强制溯源）；本函数保留原行为。
    """
    laws = _load_laws()
    if not query.strip():
        return []
    words, grams = _tokens(query)
    word_set = set(words)
    gram_set = set(grams)

    scored = []
    for law in laws:
        name = law.get("law_name", "") + law.get("article_no", "")
        content = law.get("content", "")
        name_words, name_grams = _tokens(name)
        score = 0.0
        for w in word_set:
            if w in content:
                score += 5 * content.count(w)
            if w in name:
                score += 8
        for g in gram_set:
            if g in name_grams:
                score += 1.5
            elif g in content:
                score += 1.5
        if score > 0:
            scored.append((score, law))
    scored.sort(key=lambda x: -x[0])
    return [dict(l, _score=round(s, 1)) for s, l in scored[:top_k]]


def _extract_json(text: str):
    """从LLM回复中提取JSON（容忍```json包裹等）"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def analyze_with_llm(clause_text: str, query: str, recalled_laws: list) -> dict:
    """RAG研判：引用召回法条原文，输出风险等级/结论/修改建议。失败返回None"""
    laws_text = "\n".join(
        f"《{l['law_name']}》{l['article_no']}：{l['content']}" for l in recalled_laws
    ) or "（无召回法条）"
    system = (
        "你是资深合同审查律师。必须严格依据提供的法条原文进行研判，不得编造法条。"
        "输出严格的JSON对象，格式："
        '{"risk_level":"高|中|低|无","conclusion":"研判结论（须引用所依据的法条名称与条号）",'
        '"suggestion":"修改建议","basis":["引用的法条名称+条号"]}'
    )
    user = (
        f"待研判条款：\n{clause_text}\n\n"
        f"关注点：{query or '总体合规性'}\n\n"
        f"召回的相关法条原文：\n{laws_text}\n\n"
        "请依据上述法条原文对该条款进行研判，输出JSON。"
    )
    try:
        reply = llm.chat(system, user)
        result = _extract_json(reply)
        if not isinstance(result, dict) or "risk_level" not in result:
            return None
        return {
            "risk_level": str(result.get("risk_level", "")),
            "conclusion": str(result.get("conclusion", "")),
            "suggestion": str(result.get("suggestion", "")),
            "basis": result.get("basis", []),
        }
    except (llm.LLMNotConfigured, llm.LLError, ValueError, KeyError):
        return None


# ---------- F2：逐条款溯源召回（向量优先，关键词兜底） ----------

def recall_with_sources(clause_text: str, contract_type: str = "", top_k: int = 5) -> dict:
    """逐条款召回审查点/法条，强制溯源。

    返回 {"mode": "pgvector|array|keyword", "checkpoints": [...], "laws": [...]}
    - 优先走向量召回（checkpoints.recall_for_clause：pgvector→数组余弦）
    - embedding未配置或召回层异常时，自动降级为现有2-gram关键词召回（仅法条）
    - 每条结果携带 similarity 与来源；无召回依据时返回空列表，
      上层LLM研判必须据此不输出无依据结论（强制溯源）
    """
    if not (clause_text or "").strip():
        return {"mode": "keyword", "checkpoints": [], "laws": []}
    try:
        from . import checkpoints
        result = checkpoints.recall_for_clause(clause_text, contract_type, top_k)
        if result.get("checkpoints") or result.get("laws"):
            return result
    except Exception as e:
        import sys
        print(f"[rag] 溯源召回降级到关键词模式: {e}", file=sys.stderr)
    # 降级：现有关键词召回（向后兼容，保持基线可用）
    recalled = search(clause_text, top_k)
    return {
        "mode": "keyword",
        "checkpoints": [],
        "laws": [{"law_id": l.get("law_id", ""), "law_name": l.get("law_name", ""),
                  "article_no": l.get("article_no", ""), "content": l.get("content", ""),
                  "similarity": round(min(l.get("_score", 0) / 20.0, 1.0), 4),
                  "_source": "keyword"} for l in recalled],
    }
