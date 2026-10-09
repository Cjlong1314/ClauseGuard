"""LLM客户端：OpenAI兼容chat/completions，支持Ollama/vLLM/OpenAI三类运行时

三类运行时协议完全一致（OpenAI兼容），仅base_url/模型名配置差异：
- ollama: http://127.0.0.1:11434/v1，模型名如 qwen2.5:14b
- vllm:   http://127.0.0.1:8000/v1，模型名如 Qwen2.5-14B-Instruct
- openai: https://api.openai.com/v1

未配置或调用失败抛LLMNotConfigured/LLError，绝不能影响审查主流程。
降级判定：is_configured()为False时调用方应走规则引擎基线路径（可测试）。
"""
import json
import os
import urllib.error
import urllib.request

# 配置来源：环境变量优先，其次config.py（默认空=未配置）
try:
    from . import config as _cfg
except ImportError:
    _cfg = None

KNOWN_RUNTIMES = ("ollama", "vllm", "openai")


class LLMNotConfigured(Exception):
    """LLM服务未配置"""


class LLError(Exception):
    """LLM调用失败"""


class LLMResponseError(LLError):
    """LLM返回内容不符合预期（如JSON Schema校验失败），可触发重试"""


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _cfgval(name: str) -> str:
    return getattr(_cfg, name, "") if _cfg else ""


def _pick(env_name: str, cfg_name: str) -> str:
    return _env(env_name) or _cfgval(cfg_name)


def _guess_runtime(base_url: str, explicit: str) -> str:
    """运行时类型：显式配置优先，否则按base_url启发式推断"""
    rt = (explicit or "").strip().lower()
    if rt in KNOWN_RUNTIMES:
        return rt
    if ":11434" in base_url:
        return "ollama"
    if ":8000" in base_url or base_url.rstrip("/").endswith("/v1"):
        return "vllm"
    return "openai"


def get_config(prefix: str = "") -> dict:
    """读取一组LLM配置。prefix为""取主模型，为"CROSS_"取交叉验证第二模型。

    返回{base_url, api_key, model, runtime}；未配置抛LLMNotConfigured。
    """
    base = _pick(f"{prefix}LLM_BASE_URL", f"LLM{prefix}BASE_URL" if prefix else "LLM_BASE_URL")
    # 环境变量名统一：主模型LLM_*，第二模型LLM_CROSS_*；config.py字段LLM_CROSS_BASE_URL
    base = _env(f"{prefix}LLM_BASE_URL") or _cfgval(f"LLM_{prefix}BASE_URL") if prefix else \
        _env("LLM_BASE_URL") or _cfgval("LLM_BASE_URL")
    key = _env(f"{prefix}LLM_API_KEY") or _cfgval(f"LLM_{prefix}API_KEY") if prefix else \
        _env("LLM_API_KEY") or _cfgval("LLM_API_KEY")
    model = _env(f"{prefix}LLM_MODEL") or _cfgval(f"LLM_{prefix}MODEL") if prefix else \
        _env("LLM_MODEL") or _cfgval("LLM_MODEL")
    runtime_explicit = _env(f"{prefix}LLM_RUNTIME") or _cfgval(f"LLM_{prefix}RUNTIME")
    if not base or not model:
        raise LLMNotConfigured(f"未配置大模型服务（{prefix}LLM_BASE_URL/{prefix}LLM_MODEL）")
    return {
        "base_url": base.rstrip("/"),
        "api_key": key,
        "model": model,
        "runtime": _guess_runtime(base, runtime_explicit),
    }


def is_configured(prefix: str = "") -> bool:
    """LLM是否可用（整体降级开关）：未配置返回False，调用方走规则引擎基线"""
    try:
        get_config(prefix)
        return True
    except LLMNotConfigured:
        return False


def model_info() -> dict:
    """已配置时的模型元信息（写入审计扩展字段）"""
    try:
        cfg = get_config()
        return {"runtime": cfg["runtime"], "base_url": cfg["base_url"], "model": cfg["model"]}
    except LLMNotConfigured:
        return {}


def _post_chat(cfg: dict, messages: list, timeout: int, extra: dict = None) -> str:
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0.2}
    if extra:
        payload.update(extra)
    req = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if cfg["api_key"]:
        req.add_header("Authorization", f"Bearer {cfg['api_key']}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLError(f"LLM接口返回{e.code}: {e.read()[:200]}") from e
    except Exception as e:
        raise LLError(f"LLM调用失败: {e}") from e
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLError(f"LLM响应格式异常: {e}") from e


def chat(system: str, user: str, timeout: int = 60, prefix: str = "") -> str:
    """单轮调用，返回assistant回复文本"""
    cfg = get_config(prefix)
    return _post_chat(cfg, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout)


def chat_messages(messages: list, timeout: int = 60, prefix: str = "") -> str:
    """多轮调用：messages为[{role,content}]完整列表（含system），返回assistant回复文本"""
    cfg = get_config(prefix)
    return _post_chat(cfg, messages, timeout)


def extract_json(text: str):
    """从LLM回复中稳健提取JSON对象（容忍markdown代码块/前后杂文）"""
    text = text.strip()
    # 先尝试整体解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 剥离```json ... ```或``` ... ```
    if "```" in text:
        for seg in text.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{") or seg.startswith("["):
                try:
                    return json.loads(seg)
                except json.JSONDecodeError:
                    continue
    # 兜底：截取首个{到最后一个}
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMResponseError("回复中未找到合法JSON")


def chat_json(system: str, user: str, schema: dict = None, max_retry: int = None,
              timeout: int = 60, prefix: str = "") -> dict:
    """JSON Schema约束输出+失败重试：
    将schema注入system提示词，要求仅输出JSON；解析或校验失败自动重试（附错误反馈）。
    校验通过返回dict；重试耗尽抛LLMResponseError（调用方降级处理，不影响主流程）。
    """
    if max_retry is None:
        max_retry = int(_cfgval("LLM_MAX_RETRY") or 2)
    schema_hint = ""
    if schema:
        schema_hint = ("\n输出必须是严格符合以下JSON Schema的对象，只输出JSON本身，"
                       "不要任何解释或markdown标记：\n" + json.dumps(schema, ensure_ascii=False))
    messages = [{"role": "system", "content": system + schema_hint},
                {"role": "user", "content": user}]
    cfg = get_config(prefix)
    last_err = None
    for attempt in range(max_retry + 1):
        try:
            text = _post_chat(cfg, messages, timeout)
            obj = extract_json(text)
            if not isinstance(obj, dict):
                raise LLMResponseError("JSON顶层必须是对象")
            if schema:
                _validate_minimal(obj, schema)
            return obj
        except (LLMResponseError, LLError) as e:
            last_err = e
            # 带错误反馈重试，提升自纠成功率
            messages = messages[:2] + [
                {"role": "assistant", "content": str(e)},
                {"role": "user", "content": "上面的输出不符合要求，请修正后重新仅输出合规JSON。"},
            ]
    raise LLMResponseError(f"JSON解析/校验在{max_retry + 1}次尝试后仍失败: {last_err}")


def _validate_minimal(obj: dict, schema: dict):
    """轻量校验：required字段存在性+基本类型（避免引入jsonschema依赖）"""
    for name in schema.get("required", []):
        if name not in obj:
            raise LLMResponseError(f"缺少必填字段: {name}")
    props = schema.get("properties", {})
    for k, v in obj.items():
        if k not in props:
            continue
        t = props[k].get("type")
        pytype = {"object": dict, "array": list, "string": str,
                  "number": (int, float), "integer": int, "boolean": bool}.get(t)
        if pytype and not isinstance(v, pytype):
            raise LLMResponseError(f"字段{k}类型应为{t}")
        if "enum" in props[k] and v not in props[k]["enum"]:
            raise LLMResponseError(f"字段{k}取值{v}不在枚举内")
