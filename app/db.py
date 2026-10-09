"""PostgreSQL连接层：连接池、建表、合同数据CRUD辅助

连接参数优先读环境变量：
- DATABASE_URL：postgres://user:pwd@host:port/dbname
- 或PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
默认连接本机clauseguard库（账号clauseguard/Cg@2026pg）
"""
import json
import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

DB_URL = os.environ.get("DATABASE_URL", "postgresql://clauseguard:Cg%402026pg@127.0.0.1:5432/clauseguard")

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(1, 10, dsn=DB_URL)
    return _pool


@contextmanager
def get_conn():
    """借出连接，用完自动归还；异常时调用方自行决定是否rollback"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def j(value):
    """对象转JSONB所需的json字符串"""
    return json.dumps(value, ensure_ascii=False)


# ---------- 建表 ----------

DDL = """
CREATE TABLE IF NOT EXISTS users (
    username   varchar(64) PRIMARY KEY,
    salt       varchar(64) NOT NULL,
    password   varchar(128) NOT NULL,
    role       varchar(32) NOT NULL,
    created_at varchar(32) NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token     varchar(64) PRIMARY KEY,
    username  varchar(64) NOT NULL,
    role      varchar(32) NOT NULL,
    expire    double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id  varchar(32) PRIMARY KEY,
    filename     varchar(256) NOT NULL,
    upload_time  varchar(32) NOT NULL,
    raw_text     text NOT NULL DEFAULT '',
    clauses      jsonb NOT NULL DEFAULT '[]',
    extracted    jsonb,
    review       jsonb
);
CREATE TABLE IF NOT EXISTS kb_laws (
    law_id         varchar(32) PRIMARY KEY,
    law_name       varchar(256) NOT NULL,
    article_no     varchar(64) NOT NULL DEFAULT '',
    content        text NOT NULL,
    category       varchar(64) NOT NULL DEFAULT '',
    effective_from varchar(16) NOT NULL DEFAULT '',
    effective_to   varchar(16) NOT NULL DEFAULT '',
    valid          boolean NOT NULL DEFAULT true,
    version        integer NOT NULL DEFAULT 1,
    updated_at     varchar(32) NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_templates (
    tpl_id        varchar(32) PRIMARY KEY,
    name          varchar(256) NOT NULL,
    contract_type varchar(64) NOT NULL DEFAULT '',
    content       text NOT NULL DEFAULT '',
    updated_at    varchar(32) NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id           varchar(32) PRIMARY KEY,
    contract_id       varchar(32) NOT NULL,
    contract_filename varchar(256) NOT NULL DEFAULT '',
    title             varchar(256) NOT NULL,
    assignee          varchar(64) NOT NULL,
    reviewer          varchar(64) NOT NULL DEFAULT '',
    due_date          varchar(32) NOT NULL DEFAULT '',
    status            varchar(32) NOT NULL DEFAULT 'pending',
    created_by        varchar(64) NOT NULL,
    created_at        varchar(32) NOT NULL,
    updated_at        varchar(32) NOT NULL,
    history           jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS notifications (
    notify_id varchar(32) PRIMARY KEY,
    username  varchar(64) NOT NULL,
    title     varchar(256) NOT NULL,
    content   text NOT NULL DEFAULT '',
    read      boolean NOT NULL DEFAULT false,
    time      varchar(32) NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    time        varchar(32) NOT NULL,
    username    varchar(64) NOT NULL,
    action      varchar(64) NOT NULL,
    contract_id varchar(32) NOT NULL DEFAULT '',
    detail      text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_log(contract_id);
-- F2：审计扩展字段（研判过程：模型版本/提示词版本/召回依据/轨迹），幂等加列
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS ext jsonb;
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(username);
CREATE TABLE IF NOT EXISTS rag_laws_custom (
    law_id varchar(64) PRIMARY KEY,
    law    jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS extraction_results (
    contract_id     varchar(32) PRIMARY KEY,
    extracted       jsonb NOT NULL,
    regex_extracted jsonb NOT NULL DEFAULT '{}',
    llm_extracted   jsonb,
    conflicts       jsonb NOT NULL DEFAULT '[]',
    source          varchar(16) NOT NULL DEFAULT 'regex',
    llm_status      varchar(16) NOT NULL DEFAULT 'disabled',
    llm_error       text NOT NULL DEFAULT '',
    created_at      varchar(32) NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS extraction_corrections (
    id           bigserial PRIMARY KEY,
    contract_id  varchar(32) NOT NULL,
    corrected    jsonb NOT NULL,
    origin       jsonb,
    operator     varchar(64) NOT NULL DEFAULT '',
    comment      text NOT NULL DEFAULT '',
    time         varchar(32) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_extract_corr_cid ON extraction_corrections(contract_id);
-- F7：集成外发失败重试队列（outbox模式，异步派发不阻塞主流程）
CREATE TABLE IF NOT EXISTS integration_outbox (
    id          bigserial PRIMARY KEY,
    connector   varchar(32) NOT NULL,
    event_type  varchar(64) NOT NULL,
    payload     jsonb NOT NULL,
    status      varchar(16) NOT NULL DEFAULT 'pending',
    retry_count integer NOT NULL DEFAULT 0,
    next_retry  double precision,
    last_error  text NOT NULL DEFAULT '',
    created_at  varchar(32) NOT NULL,
    updated_at  varchar(32) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON integration_outbox(status, next_retry);
-- F8：履约义务台账（回款计划/交付里程碑/续约窗口/违约触发）
CREATE TABLE IF NOT EXISTS obligations (
    oblig_id        varchar(32) PRIMARY KEY,
    contract_id     varchar(32) NOT NULL,
    oblig_type      varchar(32) NOT NULL DEFAULT 'payment',
    title           varchar(256) NOT NULL DEFAULT '',
    amount          numeric,
    due_date        varchar(32) NOT NULL DEFAULT '',
    lead_days       integer NOT NULL DEFAULT 7,
    status          varchar(16) NOT NULL DEFAULT 'pending',
    source          varchar(8) NOT NULL DEFAULT 'rule',
    description     text NOT NULL DEFAULT '',
    manually_edited boolean NOT NULL DEFAULT false,
    last_notified   varchar(32) NOT NULL DEFAULT '',
    created_at      varchar(32) NOT NULL DEFAULT '',
    updated_at      varchar(32) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_obligations_cid ON obligations(contract_id);
CREATE INDEX IF NOT EXISTS idx_obligations_due ON obligations(due_date);
-- F9：审查质检抽样任务（法务负责人抽样复核审查结论）
CREATE TABLE IF NOT EXISTS qc_reviews (
    qc_id         bigserial PRIMARY KEY,
    batch_id      varchar(32) NOT NULL DEFAULT '',
    contract_id   varchar(32) NOT NULL,
    finding_id    varchar(32) NOT NULL DEFAULT '',
    rule_name     varchar(256) NOT NULL DEFAULT '',
    dimension     varchar(32)  NOT NULL DEFAULT '',
    risk_level    varchar(16)  NOT NULL DEFAULT '',
    excerpt       text         NOT NULL DEFAULT '',
    ai_conclusion text         NOT NULL DEFAULT '',
    verdict       varchar(16)  NOT NULL DEFAULT '',   -- correct/wrong/partial，空为待复核
    comment       text         NOT NULL DEFAULT '',
    reviewer      varchar(64)  NOT NULL DEFAULT '',
    status        varchar(16)  NOT NULL DEFAULT 'pending',
    assigned_at   varchar(32)  NOT NULL DEFAULT '',
    reviewed_at   varchar(32)  NOT NULL DEFAULT '',
    UNIQUE (contract_id, finding_id)
);
CREATE INDEX IF NOT EXISTS idx_qc_status ON qc_reviews(status);
CREATE INDEX IF NOT EXISTS idx_qc_rule ON qc_reviews(rule_name);
CREATE INDEX IF NOT EXISTS idx_qc_batch ON qc_reviews(batch_id);
-- F9：质检批次记录（抽样策略与比例）
CREATE TABLE IF NOT EXISTS qc_batches (
    batch_id   varchar(32) PRIMARY KEY,
    strategy   varchar(16) NOT NULL DEFAULT 'sample',   -- sample/all
    rate       numeric    NOT NULL DEFAULT 0.2,
    total      integer    NOT NULL DEFAULT 0,
    sampled    integer    NOT NULL DEFAULT 0,
    created_by varchar(64) NOT NULL DEFAULT '',
    created_at varchar(32) NOT NULL DEFAULT ''
);
-- F9：各规则准确率/误报率统计视图（wrong即误报，partial计半对不计误报）
CREATE OR REPLACE VIEW v_qc_rule_stats AS
SELECT
    rule_name,
    dimension,
    count(*)                                        AS sampled_total,
    count(*) FILTER (WHERE status='reviewed')       AS reviewed_total,
    count(*) FILTER (WHERE verdict='correct')       AS correct_total,
    count(*) FILTER (WHERE verdict='wrong')         AS wrong_total,
    count(*) FILTER (WHERE verdict='partial')       AS partial_total,
    ROUND(100.0 * count(*) FILTER (WHERE verdict IN ('correct','partial'))
          / NULLIF(count(*) FILTER (WHERE status='reviewed'), 0), 1) AS accuracy_rate,
    ROUND(100.0 * count(*) FILTER (WHERE verdict='wrong')
          / NULLIF(count(*) FILTER (WHERE status='reviewed'), 0), 1) AS false_positive_rate
FROM qc_reviews
GROUP BY rule_name, dimension;
-- F9：审查人效率排行视图（处理量=审查次数，平均耗时取audit ext.elapsed_ms）
CREATE OR REPLACE VIEW v_reviewer_efficiency AS
SELECT
    username,
    count(*) AS review_count,
    ROUND(AVG(NULLIF((ext->>'elapsed_ms')::bigint, 0)) / 1000.0, 1) AS avg_elapsed_s
FROM audit_log
WHERE action='review'
GROUP BY username
ORDER BY review_count DESC;
"""


# ---------- F2 审查点知识库 + 向量存储 ----------

# 向量存储模式：pgvector（vector列）/ array（float8[]数组+余弦兜底）/ none（未初始化）
VECTOR_MODE = "none"
# pgvector索引维度：首次建列时记录，之后保持一致
VECTOR_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))


def init_vector_support():
    """检测pgvector扩展可用性并建向量相关表（幂等）。
    - 可用：CREATE EXTENSION vector，kb_checkpoints/kb_law_vectors用vector列
    - 不可用：降级为float8[]数组列，召回层用Python余弦计算兜底
    安装pgvector指引（Windows）：下载预编译包 https://github.com/pgvector/pgvector/releases
    或StackBuilder安装pgvector for PostgreSQL，然后在库中执行 CREATE EXTENSION vector;
    """
    global VECTOR_MODE
    with get_conn() as conn:
        with conn.cursor() as cur:
            mode = "array"
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.commit()
                cur.execute(
                    "SELECT 1 FROM pg_extension WHERE extname='vector'")
                if cur.fetchone():
                    mode = "pgvector"
            except Exception:
                conn.rollback()
                mode = "array"
            emb_col = ("vector" if mode == "pgvector"
                       else "double precision[]")
            # pgvector可用但旧表是数组列时，升级列类型（幂等）
            if mode == "pgvector":
                for tbl in ("kb_checkpoints", "kb_law_vectors"):
                    cur.execute(
                        """SELECT data_type FROM information_schema.columns
                           WHERE table_name=%s AND column_name='embedding'""", (tbl,))
                    r = cur.fetchone()
                    if r and r[0] == "ARRAY":
                        cur.execute(
                            f"ALTER TABLE {tbl} ALTER COLUMN embedding TYPE vector USING embedding::text::vector")
                conn.commit()
            cur.execute(f"""
CREATE TABLE IF NOT EXISTS kb_checkpoints (
    cp_id         varchar(32) PRIMARY KEY,
    rule_id       varchar(32) NOT NULL DEFAULT '',
    name          varchar(256) NOT NULL,
    contract_type varchar(64)  NOT NULL DEFAULT '*',
    dimension     varchar(32)  NOT NULL DEFAULT '',
    risk_level    varchar(16)  NOT NULL DEFAULT '中',
    missing_of    varchar(128) NOT NULL DEFAULT '',
    description   text         NOT NULL DEFAULT '',
    legal_basis   jsonb        NOT NULL DEFAULT '[]',
    suggestion    text         NOT NULL DEFAULT '',
    stance        jsonb        NOT NULL DEFAULT '[]',
    patterns      jsonb        NOT NULL DEFAULT '[]',
    embedding     {emb_col},
    emb_model     varchar(64)  NOT NULL DEFAULT '',
    text_hash     varchar(64)  NOT NULL DEFAULT '',
    enabled       boolean      NOT NULL DEFAULT true,
    updated_at    varchar(32)  NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kb_law_vectors (
    law_id     varchar(64) PRIMARY KEY,
    law_name   varchar(256) NOT NULL,
    article_no varchar(64)  NOT NULL DEFAULT '',
    content    text         NOT NULL,
    source     varchar(16)  NOT NULL DEFAULT 'seed',
    embedding  {emb_col},
    emb_model  varchar(64)  NOT NULL DEFAULT '',
    text_hash  varchar(64)  NOT NULL DEFAULT '',
    updated_at varchar(32)  NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cp_type ON kb_checkpoints(contract_type);
CREATE INDEX IF NOT EXISTS idx_lawv_name ON kb_law_vectors(law_name);
""")
        conn.commit()
    VECTOR_MODE = mode
    return mode


def init_db():
    """建表（幂等），在应用启动时调用；附带向量支持初始化"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    try:
        init_vector_support()
    except Exception:
        VECTOR_MODE = "none"


# ---------- 合同CRUD（替代原内存_CONTRACTS） ----------

def save_contract(contract: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO contracts (contract_id, filename, upload_time, raw_text, clauses, extracted, review)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (contract_id) DO UPDATE SET
                     filename=EXCLUDED.filename, upload_time=EXCLUDED.upload_time,
                     raw_text=EXCLUDED.raw_text, clauses=EXCLUDED.clauses,
                     extracted=EXCLUDED.extracted, review=EXCLUDED.review""",
                (contract["contract_id"], contract["filename"], contract["upload_time"],
                 contract.get("raw_text", ""), j(contract.get("clauses", [])),
                 j(contract["extracted"]) if contract.get("extracted") else None,
                 j(contract["review"]) if contract.get("review") else None))
        conn.commit()


def get_contract_row(cid: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM contracts WHERE contract_id=%s", (cid,))
            return cur.fetchone()


def list_contract_rows():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM contracts ORDER BY upload_time DESC")
            return cur.fetchall()


def update_contract_review(cid: str, review: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE contracts SET review=%s WHERE contract_id=%s",
                        (j(review), cid))
            affected = cur.rowcount
        conn.commit()
        return affected > 0


def contract_exists(cid: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM contracts WHERE contract_id=%s", (cid,))
            return cur.fetchone() is not None


# ---------- F6：要素抽取结果与人工修正 ----------

def upsert_extraction(result: dict):
    """保存/更新要素抽取结果（按合同id幂等）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extraction_results
                   (contract_id, extracted, regex_extracted, llm_extracted,
                    conflicts, source, llm_status, llm_error, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (contract_id) DO UPDATE SET
                     extracted=EXCLUDED.extracted, regex_extracted=EXCLUDED.regex_extracted,
                     llm_extracted=EXCLUDED.llm_extracted, conflicts=EXCLUDED.conflicts,
                     source=EXCLUDED.source, llm_status=EXCLUDED.llm_status,
                     llm_error=EXCLUDED.llm_error, created_at=EXCLUDED.created_at""",
                (result["contract_id"], j(result["extracted"]), j(result["regex_extracted"]),
                 j(result["llm_extracted"]) if result.get("llm_extracted") else None,
                 j(result.get("conflicts", [])), result.get("source", "regex"),
                 result.get("llm_status", "disabled"), result.get("llm_error", ""),
                 result.get("created_at", "")))
        conn.commit()


def get_extraction(cid: str):
    """读取要素抽取结果，不存在返回None"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM extraction_results WHERE contract_id=%s", (cid,))
            row = cur.fetchone()
            if not row:
                return None
            row["extracted"] = row["extracted"] if isinstance(row["extracted"], dict) else json.loads(row["extracted"] or "{}")
            row["regex_extracted"] = row["regex_extracted"] if isinstance(row["regex_extracted"], dict) else json.loads(row["regex_extracted"] or "{}")
            if row.get("llm_extracted") is not None and not isinstance(row["llm_extracted"], dict):
                row["llm_extracted"] = json.loads(row["llm_extracted"])
            row["conflicts"] = row["conflicts"] if isinstance(row["conflicts"], list) else json.loads(row["conflicts"] or "[]")
            return row


def save_extraction_correction(record: dict):
    """保存人工修正记录，并同步更新抽取结果表的最终结果"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extraction_corrections
                   (contract_id, corrected, origin, operator, comment, time)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (record["contract_id"], j(record["corrected"]),
                 j(record["origin"]) if record.get("origin") else None,
                 record.get("operator", ""), record.get("comment", ""),
                 record.get("time", "")))
            cur.execute(
                """UPDATE extraction_results SET extracted=%s
                   WHERE contract_id=%s""",
                (j(record["corrected"]), record["contract_id"]))
        conn.commit()
