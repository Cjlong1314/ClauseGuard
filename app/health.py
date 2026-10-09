"""健康检查探针（/health）：db连通、LLM配置状态、embedding状态。

设计原则：任一组件不可用均不抛异常，以降级可报的JSON返回，
便于docker healthcheck与运维脚本三探针直接消费。
"""
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import config, settings as settings_mod

router = APIRouter()


def _llm_status() -> dict:
    """LLM配置状态：base_url/model/runtime，未配置即degraded（降级为规则引擎基线）"""
    # settings.get_all合并env+持久化配置，即当前生效值
    eff = settings_mod.get_all(mask_key=False)
    base_url = (eff.get("LLM_BASE_URL") or "").strip()
    model = (eff.get("LLM_MODEL") or "").strip()
    runtime = (eff.get("LLM_RUNTIME") or "").strip()
    if not runtime:  # 与llm.py一致的推断规则
        if ":11434" in base_url:
            runtime = "ollama"
        elif ":8000" in base_url or base_url.endswith("/v1"):
            runtime = "vllm"
        elif base_url:
            runtime = "openai"
    configured = bool(base_url and model)
    return {
        "component": "llm",
        "status": "ok" if configured else "degraded",
        "configured": configured,
        "runtime": runtime or "unknown",
        "model": model,
        "base_url": base_url,
        "message": "" if configured else "LLM未配置，AI研判降级为规则引擎基线",
    }


def _embedding_status() -> dict:
    """embedding状态：base_url/model，未配置即degraded（降级为2-gram关键词召回）"""
    eff = settings_mod.get_all(mask_key=False)
    base_url = (eff.get("EMBEDDING_BASE_URL") or "").strip()
    model = (eff.get("EMBEDDING_MODEL") or "").strip()
    fake = str(eff.get("EMBEDDING_FAKE", "") or "").strip() in ("1", "true")
    configured = bool(base_url and model) or fake
    return {
        "component": "embedding",
        "status": "ok" if configured else "degraded",
        "configured": configured,
        "model": model,
        "base_url": base_url,
        "fake": fake,
        "message": "" if configured else "embedding未配置，向量召回降级为关键词召回",
    }


def _db_status() -> dict:
    """数据库连通性：SELECT 1探针，失败返回degraded且带原因"""
    try:
        from . import db as db_mod
        with db_mod.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        # pgvector可用性（部署验收关注项）
        vector_mode = "unknown"
        try:
            with db_mod.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")
                    vector_mode = "pgvector" if cur.fetchone()[0] else "array-fallback"
        except Exception:
            pass
        return {"component": "database", "status": "ok", "vector_mode": vector_mode, "message": ""}
    except Exception as e:  # 任何异常都降级可报
        return {"component": "database", "status": "degraded", "message": str(e)[:200]}


@router.get("/health")
def health():
    """三探针健康检查：整体status为ok/degraded，永不返回500，便于探活脚本统一判断"""
    checks = [_db_status(), _llm_status(), _embedding_status()]
    overall = "ok" if all(c["status"] == "ok" for c in checks) else "degraded"
    return JSONResponse(
        status_code=200,  # 降级可报：探活脚本按body里的status字段判断
        content={"status": overall, "time": datetime.now().isoformat(timespec="seconds"), "checks": checks},
    )
