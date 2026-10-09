"""Connector插件基类与注册机制（F7）

新增一个Connector只需三步（以e签宝为例）：
1. 新建app/integrations/esignbao/包，实现Connector子类
2. 在本文件CONNECTOR_CLASSES登记 {"esignbao": "app.integrations.esignbao:ESignBaoConnector"}
3. 环境变量INTEGRATIONS_ENABLED=esignbao启用

基类统一接口：
- send_event(event: dict) -> bool          处理总线事件（实现按event_type分发）
- send_message(user: str, title: str, content: str, **kw) -> bool   发消息/待办
- health_check() -> dict                   健康检查（凭证/连通性），供运维接口调用
声明式配置：子类声明CONFIG_KEYS与REQUIRED_KEYS，实例化时自动从config注入并校验必填项。
"""
import importlib
import os
import traceback
from abc import ABC, abstractmethod

from .. import config

# Connector注册表：name -> "模块路径:类名"（字符串形式避免未安装依赖时import失败）
# e签宝Connector由后续同事按此登记；未启用时不加载
CONNECTOR_CLASSES = {
    "dingtalk": "app.integrations.dingtalk:DingTalkConnector",
    "esignbao": "app.integrations.esignbao:ESignBaoConnector",
}

_registry = {}      # name -> 实例（仅已启用且配置合法的）
_load_errors = {}   # name -> 加载失败原因


def enabled_connectors() -> list:
    """INTEGRATIONS_ENABLED解析出的已启用Connector名单（实时读环境变量，便于测试切换）"""
    raw = os.environ.get("INTEGRATIONS_ENABLED", config.INTEGRATIONS_ENABLED or "")
    return [s.strip() for s in raw.split(",") if s.strip()]


class ConnectorBase(ABC):
    """Connector插件基类，所有外部集成实现此接口"""

    name = ""                       # 注册名，如dingtalk/esignbao
    # 声明式配置：CONFIG_KEYS为从config读取的配置项名列表，REQUIRED_KEYS为其中必填项
    CONFIG_KEYS = []
    REQUIRED_KEYS = []

    def __init__(self):
        for key in self.CONFIG_KEYS:
            setattr(self, key.lower(), getattr(config, key, ""))
        missing = [k.lower() for k in self.REQUIRED_KEYS if not getattr(self, k.lower(), "")]
        if missing:
            raise ValueError(f"Connector[{self.name}]缺少必填配置: {', '.join(missing)}")

    # ---- 必须实现的统一接口 ----
    @abstractmethod
    def send_event(self, event: dict) -> bool:
        """处理总线事件；返回False表示跳过（不算失败），抛异常表示失败（落重试队列）"""

    @abstractmethod
    def send_message(self, user: str, title: str, content: str, **kw) -> bool:
        """发送消息/待办到外部系统；user为ClauseGuard用户名，实现方负责映射"""

    @abstractmethod
    def health_check(self) -> dict:
        """健康检查，返回{ok: bool, detail: str, ...}"""

    # ---- 通用工具 ----
    @staticmethod
    def map_user(user: str) -> str:
        """ClauseGuard用户名→外部系统标识映射；默认原样返回（可用手机号/工号环境变量扩展）"""
        return os.environ.get(f"INTEGRATION_USER_MAP_{user}", user)


def load_connectors():
    """按INTEGRATIONS_ENABLED加载Connector实例（应用启动时与测试时调用，幂等）"""
    for name in enabled_connectors():
        if name in _registry:
            continue
        spec = CONNECTOR_CLASSES.get(name)
        if not spec:
            _load_errors[name] = f"未注册的Connector: {name}"
            continue
        try:
            mod_path, cls_name = spec.split(":")
            cls = getattr(importlib.import_module(mod_path), cls_name)
            _registry[name] = cls()
            print(f"[integrations] Connector[{name}]已加载")
        except Exception as e:
            _load_errors[name] = f"{type(e).__name__}: {e}"
            traceback.print_exc()


def get_connector(name: str):
    """获取已启用Connector实例；未启用/加载失败返回None"""
    load_connectors()
    return _registry.get(name)


def status() -> dict:
    """全部Connector运行状态（含未启用与失败原因），供运维/健康检查接口"""
    load_connectors()
    out = {"enabled": enabled_connectors(), "loaded": {}, "errors": dict(_load_errors),
           "not_enabled": [n for n in CONNECTOR_CLASSES if n not in enabled_connectors()]}
    for name, conn in _registry.items():
        try:
            out["loaded"][name] = {"config_ok": True, "health": conn.health_check()}
        except Exception as e:
            out["loaded"][name] = {"config_ok": True, "health": {"ok": False, "detail": str(e)}}
    return out
