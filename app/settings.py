# -*- coding: utf-8 -*-
"""运行时系统设置：真实embedding服务与LLM的在线配置

配置持久化到storage/runtime_settings.json，优先级：环境变量 > 本模块保存值 > config.py默认值。
保存时同步写入os.environ与config模块属性，实现不重启即生效（热更新）。
"""
import json
import os
import threading

from . import config

_SETTINGS_FILE = config.STORAGE_DIR / "runtime_settings.json"
_lock = threading.Lock()

# 允许在线配置的键及其写入config的属性名（env直接用同名键）
_FIELDS = {
    "LLM_BASE_URL": "LLM_BASE_URL",
    "LLM_API_KEY": "LLM_API_KEY",
    "LLM_MODEL": "LLM_MODEL",
    "LLM_RUNTIME": "LLM_RUNTIME",
    "LLM_CROSS_BASE_URL": "LLM_CROSS_BASE_URL",
    "LLM_CROSS_MODEL": "LLM_CROSS_MODEL",
    "EMBEDDING_BASE_URL": None,   # embedding无config默认值，仅走env
    "EMBEDDING_API_KEY": None,
    "EMBEDDING_MODEL": None,
    "LLM_MAX_RETRY": "LLM_MAX_RETRY",
    "RECALL_MIN_SCORE": "RECALL_MIN_SCORE",
}
_MASK = "******"  # api_key掩码，回显时不泄露明文


def load():
    """应用启动时调用：把已保存的配置应用到运行时（env+config属性）"""
    data = _read_file()
    for k, v in data.items():
        if k in _FIELDS and v not in ("", None):
            _apply(k, v)


def get_all(mask_key: bool = True) -> dict:
    """读取当前生效配置（合并config默认值+已保存值）；api_key可掩码回显"""
    saved = _read_file()
    out = {}
    for k in _FIELDS:
        v = os.environ.get(k, "") or getattr(config, _FIELDS[k], "") if _FIELDS[k] else \
            os.environ.get(k, "") or saved.get(k, "")
        out[k] = _MASK if (mask_key and k.endswith("API_KEY") and v) else v
    return out


def update(data: dict) -> dict:
    """保存配置并热更新；api_key传掩码"******"时保留原值不覆盖"""
    data = data or {}
    with _lock:
        saved = _read_file()
        for k in _FIELDS:
            if k not in data:
                continue
            v = str(data[k]).strip()
            # 掩码值：保留已保存/已存在的原api_key
            if k.endswith("API_KEY") and v == _MASK:
                existing = os.environ.get(k, "") or saved.get(k, "") or \
                    (getattr(config, _FIELDS[k], "") if _FIELDS[k] else "")
                v = existing
            saved[k] = v
            _apply(k, v)
        _write_file(saved)
    return get_all()


def _apply(key: str, value: str):
    """把单个配置项写入env与config属性（数值字段做类型转换）"""
    if value == "":
        return
    os.environ[key] = str(value)
    attr = _FIELDS.get(key)
    if not attr:
        return
    if key in ("LLM_MAX_RETRY",):
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return
    elif key == "RECALL_MIN_SCORE":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
    try:
        setattr(config, attr, value)
    except Exception:
        pass


def _read_file() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_file(data: dict):
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
