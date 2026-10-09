# -*- coding: utf-8 -*-
"""Embedding服务客户端：OpenAI兼容/embeddings接口

配置优先级：环境变量 > config.py
- EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL
- 未设置EMBEDDING_*时回退复用LLM_BASE_URL/LLM_API_KEY（EMBEDDING_MODEL仍需单独配置）
- 支持本地bge-m3：LLM_BASE_URL=http://127.0.0.1:11434/v1, EMBEDDING_MODEL=bge-m3
- EMBEDDING_FAKE=1：确定性伪向量（哈希模拟），仅用于自测，不调用任何服务
未配置/调用失败一律返回None，由上层降级为2-gram关键词召回，绝不影响审查主流程。
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

try:
    from .config import LLM_BASE_URL as _CFG_BASE, LLM_API_KEY as _CFG_KEY
except ImportError:
    _CFG_BASE = _CFG_KEY = ""


class EmbeddingNotConfigured(Exception):
    """embedding服务未配置"""


def _get_cfg():
    """返回(base_url, api_key, model)；未配置抛EmbeddingNotConfigured"""
    if os.environ.get("EMBEDDING_FAKE", "").strip() == "1":
        return "fake://local", "", "fake-embedding"
    base = os.environ.get("EMBEDDING_BASE_URL") or _CFG_BASE
    key = os.environ.get("EMBEDDING_API_KEY") or _CFG_KEY
    model = os.environ.get("EMBEDDING_MODEL") or ""
    if not base or not model:
        raise EmbeddingNotConfigured(
            "未配置embedding服务（EMBEDDING_BASE_URL/EMBEDDING_MODEL，"
            "本地bge-m3示例：EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1 EMBEDDING_MODEL=bge-m3）")
    return base.rstrip("/"), key, model


def is_configured() -> bool:
    try:
        _get_cfg()
        return True
    except EmbeddingNotConfigured:
        return False


def _fake_embed(texts):
    """确定性伪向量：sha256哈希驱动，64维L2归一化（自测专用，模式可复现）"""
    dim = int(os.environ.get("EMBEDDING_FAKE_DIM", "64"))
    out = []
    for t in texts:
        raw = hashlib.sha256(t.encode("utf-8")).digest()
        vec = []
        while len(vec) < dim:
            raw = hashlib.sha256(raw + b"|cg").digest()
            vec.extend(b / 255.0 - 0.5 for b in raw)
        vec = vec[:dim]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        out.append([round(v / norm, 7) for v in vec])
    return out


def embed(texts, timeout: int = 30):
    """批量向量化。成功返回list[list[float]]；未配置/失败返回None（不抛异常）"""
    if not texts:
        return []
    try:
        base, key, model = _get_cfg()
    except EmbeddingNotConfigured:
        return None
    if base == "fake://local":
        return _fake_embed(list(texts))
    payload = {"model": model, "input": list(texts)}
    req = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]
    except (EmbeddingNotConfigured, urllib.error.HTTPError, Exception):
        # 网络失败/格式异常：静默降级，由上层走关键词召回
        return None
