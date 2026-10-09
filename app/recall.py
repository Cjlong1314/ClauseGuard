"""召回层适配：为Agent审查流水线提供审查点/法条召回

接口约定（与召回层同事的联调契约）：
    recall(clause_text: str, contract_type: str = "", top_k: int = 5)
        -> [{"checkpoint": str,   # 审查点描述
             "law_text": str,     # 法条原文（溯源用，输出必附）
             "law_id": str,       # 法条编号（可选，便于跳转）
             "source": str,       # 依据来源标识，如"vector"/"keyword"/"mock"
             "score": float}]     # 相关度0~1，低于RECALL_MIN_SCORE不作为输出依据

联调后：默认走正式召回层 rag.recall_with_sources()
（三级降级：pgvector→数组余弦→2-gram关键词，由checkpoints.py实现），
RECALL_BACKEND=stub保留旧关键词桩、=mock保留确定性mock（自测用）。
pipeline.py与联调契约不变。
"""
import os

from . import rag
from .config import RECALL_MIN_SCORE


def _official_recall(clause_text: str, contract_type: str = "", top_k: int = 5) -> list:
    """正式召回：适配rag.recall_with_sources()输出为联调契约字段"""
    try:
        result = rag.recall_with_sources(clause_text, contract_type, top_k)
    except Exception:
        return []  # 召回失败不阻断流水线，交由降级路径
    out = []
    # 审查点：法条依据取其关联法条列表；law_text取第一条法条原文（溯源必附）
    for cp in result.get("checkpoints", []):
        basis = cp.get("legal_basis") or []
        if isinstance(basis, str):  # 兼容未解析为列表的数据
            basis = [{"law_name": "", "article_no": "", "content": basis}]
        first = basis[0] if basis else {}
        out.append({
            "checkpoint": cp.get("name") or cp.get("description", ""),
            "law_text": first.get("content", ""),
            "law_id": f"{first.get('law_name', '')}{first.get('article_no', '')}".strip(),
            "source": cp.get("_source", "vector"),
            "score": float(cp.get("similarity", 0.0)),
        })
    # 法条召回项
    for l in result.get("laws", []):
        out.append({
            "checkpoint": l.get("law_name", ""),
            "law_text": l.get("content", ""),
            "law_id": f"{l.get('law_name', '')}{l.get('article_no', '')}".strip(),
            "source": l.get("_source", "keyword"),
            "score": float(l.get("similarity", 0.0)),
        })
    # 去重（同一law_id+checkpoint只留最高分）并按分数降序
    seen = {}
    for i in out:
        key = (i["checkpoint"], i["law_id"])
        if key not in seen or i["score"] > seen[key]["score"]:
            seen[key] = i
    return sorted(seen.values(), key=lambda x: -x["score"])[:top_k]


def _stub_recall(clause_text: str, contract_type: str = "", top_k: int = 5) -> list:
    """旧桩实现（RECALL_BACKEND=stub时启用）：复用现有rag 2-gram关键词召回"""
    try:
        rag.build_index()
    except Exception:
        return []  # 召回失败不阻断流水线，交由降级路径
    query = f"{contract_type} {clause_text[:200]}".strip()
    try:
        laws = rag.search(query, top_k=top_k)
    except Exception:
        return []
    out = []
    for l in laws:
        out.append({
            "checkpoint": l.get("law_name", ""),
            "law_text": l.get("content", ""),
            "law_id": f"{l.get('law_name', '')}{l.get('article_no', '')}",
            "source": "rag_keyword",
            "score": min(1.0, max(0.0, l.get("score", 0.0) / 10.0)),  # rag打分归一到0~1
        })
    return out


def _mock_recall(clause_text: str, contract_type: str = "", top_k: int = 5) -> list:
    """确定性mock（自测用）：按关键词给出固定召回"""
    return [{
        "checkpoint": "违约责任条款应当明确",
        "law_text": "《中华人民共和国民法典》第五百七十七条：当事人一方不履行合同义务"
                    "或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
        "law_id": "民法典577",
        "source": "mock",
        "score": 0.9,
    }]


def get_backend():
    """返回召回实现；RECALL_BACKEND环境变量切换（默认正式向量召回层）"""
    backend = os.environ.get("RECALL_BACKEND", "")
    if backend == "mock":
        return _mock_recall
    if backend == "stub":
        return _stub_recall
    return _official_recall


def recall(clause_text: str, contract_type: str = "", top_k: int = 5) -> list:
    """流水线统一召回入口（联调契约见模块docstring）"""
    if not clause_text:
        return []
    items = get_backend()(clause_text, contract_type, top_k)
    # 防幻觉：低于阈值的召回不返回，避免LLM以弱依据编造结论
    return [i for i in items if float(i.get("score", 0)) >= RECALL_MIN_SCORE]


def is_mock() -> bool:
    return os.environ.get("RECALL_BACKEND", "") == "mock"
