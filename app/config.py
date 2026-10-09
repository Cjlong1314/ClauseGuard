"""全局配置"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
REPORT_DIR = STORAGE_DIR / "reports"
RULES_DIR = Path(__file__).resolve().parent / "rules_data"

# 确保目录存在
for d in (UPLOAD_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB，与方案性能要求一致
ALLOWED_EXT = {".docx", ".pdf", ".txt",
               # F5：图片型合同（OCR通道），OCR未启用或不可用时解析层返回明确中文提示
               ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

# 大模型服务配置（OpenAI兼容接口；为空表示未配置，可用同名环境变量覆盖）
LLM_BASE_URL = ""   # 如 http://127.0.0.1:8000/v1
LLM_API_KEY = ""
LLM_MODEL = ""
# LLM运行时类型：ollama/vllm/openai（均为OpenAI兼容协议，仅base_url/模型名差异）
# 留空时自动按base_url推断：含":11434"视为ollama，含":8000"或"/v1"结尾视为vllm，否则openai
LLM_RUNTIME = ""
# 可选第二模型（交叉验证用），留空则仅用规则引擎复核
LLM_CROSS_BASE_URL = ""
LLM_CROSS_MODEL = ""
# 流水线研判重试次数与召回分数阈值（低于阈值的召回结果不作为输出依据，防幻觉）
LLM_MAX_RETRY = 2
RECALL_MIN_SCORE = 0.1
PROMPT_VERSION = "pipeline-v1"

# ---------- F7 集成适配层（IntegrationHub） ----------
# 启用的Connector，逗号分隔，如 dingtalk,esignbao；留空表示不启用任何外部集成
INTEGRATIONS_ENABLED = os.environ.get("INTEGRATIONS_ENABLED", "")
# 外发失败重试队列：最大重试次数与轮询间隔秒数
INTEGRATIONS_MAX_RETRY = int(os.environ.get("INTEGRATIONS_MAX_RETRY", "5"))
INTEGRATIONS_RETRY_INTERVAL = int(os.environ.get("INTEGRATIONS_RETRY_INTERVAL", "300"))

# 钉钉Connector配置（企业内部应用：工作通知）
DINGTALK_APP_KEY = os.environ.get("DINGTALK_APP_KEY", "")
DINGTALK_APP_SECRET = os.environ.get("DINGTALK_APP_SECRET", "")
DINGTALK_AGENT_ID = os.environ.get("DINGTALK_AGENT_ID", "")
# 回调验签Token（钉钉回调应用配置），留空则不校验回调签名
DINGTALK_CALLBACK_TOKEN = os.environ.get("DINGTALK_CALLBACK_TOKEN", "")
# 可选AES Key（钉钉回调加解密），留空则回调报文明文
DINGTALK_CALLBACK_AES_KEY = os.environ.get("DINGTALK_CALLBACK_AES_KEY", "")
# 钉钉API网关，可指向mock服务器（自测用）
DINGTALK_API_BASE = os.environ.get("DINGTALK_API_BASE", "https://oapi.dingtalk.com")

# e签宝Connector配置（开放平台：签署状态查询/回写）
# 应用ID与密钥（e签宝开放平台-应用管理获取）
ESIGNBAO_APP_ID = os.environ.get("ESIGNBAO_APP_ID", "")
ESIGNBAO_SECRET = os.environ.get("ESIGNBAO_SECRET", "")
# 回调验签密钥，留空则不校验回调签名
ESIGNBAO_CALLBACK_SECRET = os.environ.get("ESIGNBAO_CALLBACK_SECRET", "")
# 开放平台网关，可指向mock服务器（自测用）
ESIGNBAO_API_BASE = os.environ.get("ESIGNBAO_API_BASE", "https://openapi.esign.cn")
# 可选：签署完成后接收站内通知的用户列表（逗号分隔），留空只写审计
ESIGNBAO_NOTIFY_USERS = os.environ.get("ESIGNBAO_NOTIFY_USERS", "")

# ---------- F5 OCR/PDF版式解析（PaddleOCR，可选依赖） ----------
# 总开关：False时扫描件直接返回不支持提示（不影响DOCX/TXT/文本层PDF）
OCR_ENABLED = os.environ.get("OCR_ENABLED", "1") not in ("0", "false", "False")
# 识别语言，PaddleOCR模型标识（ch=中英文混合）
OCR_LANG = os.environ.get("OCR_LANG", "ch")
# 低置信阈值：段落平均置信度低于该值标记"需人工复核"
OCR_MIN_CONF = float(os.environ.get("OCR_MIN_CONF", "0.85"))
# 文本层字符数判定：PDF全文文本层低于该值视为扫描件走OCR
OCR_SCAN_MIN_CHARS = int(os.environ.get("OCR_SCAN_MIN_CHARS", "50"))
# OCR渲染DPI（CPU下150为速度/精度折中）
OCR_DPI = int(os.environ.get("OCR_DPI", "150"))
# 扫描件上传是否异步解析（先落"解析中"状态+daemon线程处理）
OCR_ASYNC = os.environ.get("OCR_ASYNC", "1") not in ("0", "false", "False")
