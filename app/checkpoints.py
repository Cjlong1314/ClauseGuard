# -*- coding: utf-8 -*-
"""F2 审查点知识库 + pgvector向量召回层

结构化审查点模型：合同类型 → 审查点 → 法条依据 → 建议措辞 → 适用主体立场
表（db.init_vector_support幂等创建）：
- kb_checkpoints：审查点（legal_basis为法条列表jsonb，embedding为向量列）
- kb_law_vectors：法条文本向量（种子法条+kb_laws库同步）

召回降级链（逐条款召回）：
1. pgvector（VECTOR_MODE=pgvector，SQL侧<=>排序）
2. 数组兜底（VECTOR_MODE=array，Python余弦计算）
3. 2-gram关键词召回（embedding未配置/无向量数据时，复用rag._tokens打分）

强制溯源：召回结果必须携带"审查点+法条原文+相似度"，无召回依据时返回空列表，
上层（LLM研判）不得在无依据情况下输出结论。
"""
import hashlib
import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

from . import db, embedding

RULES_JSON = Path(__file__).resolve().parent / "rules_data" / "rules.json"

# 法条引用解析：《法名》第X条：内容（一段legal_basis可含多条）
_LAW_REF = re.compile(
    r"《(?P<name>[^》]+)》(?P<no>第[一二三四五六七八九十百千零\d]+条)?[：:，,]?\s*(?P<content>[^；;《]*)")

# 相似度阈值（余弦）；低于该值视为无有效召回依据
MIN_SIMILARITY = 0.30


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _parse_legal_basis(basis: str):
    """把规则里的legal_basis长文本解析为结构化法条列表"""
    laws = []
    for m in _LAW_REF.finditer(basis or ""):
        laws.append({
            "law_name": m.group("name").strip(),
            "article_no": (m.group("no") or "").strip(),
            "content": (m.group("content") or "").strip(),
        })
    if not laws and basis:
        laws.append({"law_name": "", "article_no": "", "content": basis.strip()})
    return laws


# ---------- 审查点CRUD ----------

def upsert_checkpoint(data: dict) -> dict:
    """新增/更新审查点（按cp_id或rule_id幂等）"""
    cp_id = data.get("cp_id") or uuid.uuid4().hex[:10].upper()
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO kb_checkpoints
                   (cp_id, rule_id, name, contract_type, dimension, risk_level,
                    missing_of, description, legal_basis, suggestion, stance,
                    patterns, embedding, emb_model, text_hash, enabled, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (cp_id) DO UPDATE SET
                     rule_id=EXCLUDED.rule_id, name=EXCLUDED.name,
                     contract_type=EXCLUDED.contract_type, dimension=EXCLUDED.dimension,
                     risk_level=EXCLUDED.risk_level, missing_of=EXCLUDED.missing_of,
                     description=EXCLUDED.description, legal_basis=EXCLUDED.legal_basis,
                     suggestion=EXCLUDED.suggestion, stance=EXCLUDED.stance,
                     patterns=EXCLUDED.patterns, embedding=EXCLUDED.embedding,
                     emb_model=EXCLUDED.emb_model, text_hash=EXCLUDED.text_hash,
                     enabled=EXCLUDED.enabled, updated_at=EXCLUDED.updated_at""",
                (cp_id, data.get("rule_id", ""), data["name"],
                 data.get("contract_type", "*"), data.get("dimension", ""),
                 data.get("risk_level", "中"), data.get("missing_of", ""),
                 data.get("description", ""), db.j(data.get("legal_basis", [])),
                 data.get("suggestion", ""), db.j(data.get("stance", ["*"])),
                 db.j(data.get("patterns", [])),
                 data.get("embedding"), data.get("emb_model", ""),
                 data.get("text_hash", ""), bool(data.get("enabled", True)),
                 _now()))
        conn.commit()
    return dict(data, cp_id=cp_id)


def list_checkpoints(contract_type: str = "", enabled_only: bool = True):
    sql = """SELECT cp_id, rule_id, name, contract_type, dimension, risk_level,
             missing_of, description, legal_basis, suggestion, stance, patterns,
             emb_model, text_hash, enabled, updated_at
             FROM kb_checkpoints WHERE 1=1"""
    args = []
    if enabled_only:
        sql += " AND enabled"
    if contract_type and contract_type != "*":
        sql += " AND (contract_type=%s OR contract_type='*')"
        args.append(contract_type)
    sql += " ORDER BY cp_id"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["legal_basis"] = r["legal_basis"] if isinstance(r["legal_basis"], list) else []
        r["stance"] = r["stance"] if isinstance(r["stance"], list) else []
        r["patterns"] = r["patterns"] if isinstance(r["patterns"], list) else []
    return rows


def delete_checkpoint(cp_id: str) -> bool:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_checkpoints WHERE cp_id=%s", (cp_id,))
            n = cur.rowcount
        conn.commit()
    return n > 0


# ---------- 数据迁移：rules.json → 审查点表 ----------

def migrate_rules(rules_path=RULES_JSON) -> dict:
    """将旧规则JSON升级为结构化审查点库（幂等，按rule_id覆盖更新）。
    通用规则（applies_to含'*'）标记contract_type='*'。
    """
    rules = json.loads(Path(rules_path).read_text(encoding="utf-8"))
    migrated, laws = 0, set()
    for rule in rules:
        basis = _parse_legal_basis(rule.get("legal_basis", ""))
        for b in basis:
            laws.add((b["law_name"], b["article_no"], b["content"]))
        cp = {
            "cp_id": "CP" + rule.get("id", "").lstrip("R").zfill(4) or None,
            "rule_id": rule.get("id", ""),
            "name": rule.get("name", ""),
            "contract_type": "*",
            "dimension": rule.get("dimension", ""),
            "risk_level": rule.get("risk_level", "中"),
            "missing_of": rule.get("missing_of", ""),
            "description": rule.get("name", ""),
            "legal_basis": basis,
            "suggestion": rule.get("suggestion", ""),
            "stance": rule.get("applies_to", ["*"]),
            "patterns": rule.get("patterns", []),
        }
        if not cp["cp_id"]:
            cp["cp_id"] = uuid.uuid4().hex[:10].upper()
        upsert_checkpoint(cp)
        migrated += 1
    # 法条原文同步进向量表（source=rule，去重）
    for name, no, content in laws:
        sync_law_vector(name, no, content, source="rule")
    return {"checkpoints": migrated, "unique_laws": len(laws)}


# ---------- 向量化存储 ----------

def sync_law_vector(law_name: str, article_no: str, content: str,
                    source: str = "kb", law_id: str = ""):
    """单条法条向量化入库（文本未变化则跳过重复调用embedding）"""
    law_id = law_id or ("L" + _hash(law_name + article_no + content)[:12])
    text = f"《{law_name}》{article_no}：{content}"
    th = _hash(text)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT text_hash, emb_model FROM kb_law_vectors WHERE law_id=%s", (law_id,))
            r = cur.fetchone()
            if r and r[0] == th and r[1] == (os_model() or ""):
                return False
            vecs = embedding.embed([text])
            if vecs is None:
                emb, model = None, ""
            else:
                emb, model = vecs[0], (os_model() or "fake-embedding")
            cur.execute(
                """INSERT INTO kb_law_vectors
                   (law_id, law_name, article_no, content, source,
                    embedding, emb_model, text_hash, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (law_id) DO UPDATE SET
                     law_name=EXCLUDED.law_name, article_no=EXCLUDED.article_no,
                     content=EXCLUDED.content, source=EXCLUDED.source,
                     embedding=EXCLUDED.embedding, emb_model=EXCLUDED.emb_model,
                     text_hash=EXCLUDED.text_hash, updated_at=EXCLUDED.updated_at""",
                (law_id, law_name, article_no, content, source,
                 emb, model, th, _now()))
        conn.commit()
    return True


def os_model():
    """当前embedding模型标识（写入向量表用于失效判断）"""
    try:
        return embedding._get_cfg()[2]
    except embedding.EmbeddingNotConfigured:
        return ""


def sync_embeddings(batch_size: int = 32) -> dict:
    """为所有缺失向量/文本已变化的审查点与法条补算向量（增量）"""
    if not embedding.is_configured():
        return {"skipped": "embedding未配置，保持关键词召回基线"}
    # 法条：kb_law_vectors里哈希不一致的 + kb_laws全量同步
    synced_laws = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT law_id, law_name, article_no, content FROM kb_laws")
            for lid, name, no, content in cur.fetchall():
                if sync_law_vector(name, no, content, source="kb", law_id=lid):
                    synced_laws += 1
    # 审查点
    synced_cps = 0
    cps = list_checkpoints(enabled_only=False)
    model = os_model() or ""
    todo = [c for c in cps if c["text_hash"] != _hash(_cp_text(c)) or c["emb_model"] != model]
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        vecs = embedding.embed([_cp_text(c) for c in batch])
        if vecs is None:
            break
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                for c, v in zip(batch, vecs):
                    cur.execute(
                        """UPDATE kb_checkpoints SET embedding=%s, emb_model=%s,
                           text_hash=%s, updated_at=%s WHERE cp_id=%s""",
                        (v, model, _hash(_cp_text(c)), _now(), c["cp_id"]))
            conn.commit()
            synced_cps += len(batch)
    return {"law_vectors_synced": synced_laws, "checkpoint_vectors_synced": synced_cps}


def _cp_text(cp: dict) -> str:
    """审查点向量化文本：名称+维度+缺失对象+建议（语义核心）"""
    parts = [cp.get("name", ""), cp.get("dimension", ""), cp.get("missing_of", ""),
             cp.get("suggestion", "")]
    for b in cp.get("legal_basis", []):
        parts.append(f"《{b.get('law_name','')}》{b.get('article_no','')}")
    return "；".join(p for p in parts if p)


# ---------- 余弦工具 ----------

def _cos(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ---------- 逐条款召回（三级降级） ----------

def recall_for_clause(clause_text: str, contract_type: str = "", top_k: int = 5) -> dict:
    """逐条款召回审查点+法条，强制溯源。
    返回 {"mode": "pgvector|array|keyword", "checkpoints": [...], "laws": [...]}
    每项含 similarity；无有效召回时列表为空（上层不得输出无依据结论）。
    """
    if not (clause_text or "").strip():
        return {"mode": _mode(), "checkpoints": [], "laws": []}

    cps = _recall_checkpoints(clause_text, contract_type, top_k)
    laws = _recall_laws(clause_text, top_k)
    mode = _mode()
    # 两条路径都拿到向量结果时用向量模式标注；审查点走了向量而法条没有时按实际来源
    if cps and cps[0].get("_source") == "vector" and laws and laws[0].get("_source") == "vector":
        mode = "pgvector" if db.VECTOR_MODE == "pgvector" else "array"
    # 阈值过滤（强制溯源有效性）
    cps = [c for c in cps if c.get("similarity", 0) >= MIN_SIMILARITY or c.get("_source") == "keyword"]
    laws = [l for l in laws if l.get("similarity", 0) >= MIN_SIMILARITY or l.get("_source") == "keyword"]
    return {"mode": mode, "checkpoints": cps, "laws": laws}


def _mode() -> str:
    if not embedding.is_configured():
        return "keyword"
    return "pgvector" if db.VECTOR_MODE == "pgvector" else "array"


def _recall_checkpoints(clause_text, contract_type, top_k):
    if embedding.is_configured():
        rows = _cp_rows_with_vectors(contract_type)
        if rows:
            qv = embedding.embed([clause_text])
            if qv:
                q = qv[0]
                scored = []
                for cp, vec in rows:
                    if not vec:
                        continue
                    if db.VECTOR_MODE == "pgvector" and isinstance(vec, str):
                        vec = json.loads(vec)
                    sim = _cos(q, vec)
                    if sim >= MIN_SIMILARITY:
                        scored.append((sim, cp))
                scored.sort(key=lambda x: -x[0])
                out = []
                for sim, cp in scored[:top_k]:
                    d = dict(cp)
                    d["similarity"] = round(sim, 4)
                    d["_source"] = "vector"
                    out.append(d)
                if out:
                    return out
                # 向量结果均低于阈值（语义不相关）：降级关键词召回保证可用性
    # 降级：2-gram关键词对审查点文本打分
    return _keyword_recall_checkpoints(clause_text, contract_type, top_k)


def _cp_rows_with_vectors(contract_type):
    """取审查点及其向量（pgvector模式向量以text返回便于json解析）"""
    emb_expr = ("embedding::text" if db.VECTOR_MODE == "pgvector" else "embedding")
    sql = f"""SELECT cp_id, rule_id, name, contract_type, dimension, risk_level,
              missing_of, description, legal_basis, suggestion, stance, {emb_expr}
              FROM kb_checkpoints WHERE enabled"""
    args = []
    if contract_type and contract_type != "*":
        sql += " AND (contract_type=%s OR contract_type='*')"
        args.append(contract_type)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = []
    for r in rows:
        for k in ("legal_basis", "stance"):
            if not isinstance(r.get(k), list):
                r[k] = []
        out.append((r, r.pop("embedding")))
    return out


def _keyword_recall_checkpoints(clause_text, contract_type, top_k):
    """审查点关键词降级召回：复用rag的2-gram打分思想"""
    from .rag import _tokens
    words, grams = _tokens(clause_text)
    word_set, gram_set = set(words), set(grams)
    scored = []
    for cp in list_checkpoints(contract_type):
        text = _cp_text(cp) + "；".join(
            f"《{b.get('law_name','')}》{b.get('article_no','')}：{b.get('content','')}"
            for b in cp.get("legal_basis", []))
        score = 0.0
        for w in word_set:
            if w in text:
                score += 5 * text.count(w)
        t_norm = re.sub(r"\s+", "", text)
        for g in gram_set:
            if g in t_norm:
                score += 1.5
        if score > 0:
            d = dict(cp)
            d["similarity"] = round(min(score / 20.0, 1.0), 4)  # 归一化为0-1
            d["_source"] = "keyword"
            scored.append((d["similarity"], d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:top_k]]


def _recall_laws(clause_text, top_k):
    """法条召回：向量模式查kb_law_vectors；向量不可用降级rag.search（关键词）"""
    if embedding.is_configured() and db.VECTOR_MODE in ("pgvector", "array"):
        emb_expr = ("embedding::text" if db.VECTOR_MODE == "pgvector" else "embedding")
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""SELECT law_id, law_name, article_no, content, {emb_expr}
                                FROM kb_law_vectors""")
                rows = cur.fetchall()
        if rows:
            qv = embedding.embed([clause_text])
            if qv:
                q = qv[0]
                scored = []
                for lid, name, no, content, vec in rows:
                    if not vec:
                        continue
                    if isinstance(vec, str):
                        vec = json.loads(vec)
                    sim = _cos(q, vec)
                    if sim >= MIN_SIMILARITY:
                        scored.append((sim, {
                            "law_id": lid, "law_name": name, "article_no": no,
                            "content": content, "similarity": round(sim, 4),
                            "_source": "vector"}))
                scored.sort(key=lambda x: -x[0])
                if scored:
                    return [d for _, d in scored[:top_k]]
    # 降级：现有2-gram关键词召回（向后兼容）
    from .rag import search as keyword_search
    out = []
    for law in keyword_search(clause_text, top_k):
        d = {"law_id": law.get("law_id", ""), "law_name": law.get("law_name", ""),
             "article_no": law.get("article_no", ""), "content": law.get("content", ""),
             "similarity": round(min(law.get("_score", 0) / 20.0, 1.0), 4),
             "_source": "keyword"}
        out.append(d)
    return out
