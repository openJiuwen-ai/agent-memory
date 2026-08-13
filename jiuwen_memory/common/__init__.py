"""共享组件：跨层复用的能力插件接口、通用结构体与异常类型（见 base.py / errors.py / log/）。"""

from .base import Plugin, PluginType
from .errors import (
    BackendError,
    AgentMemoryError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    PermissionDeniedError,
    PolicyError,
    ValidationError,
)
from .log import get_logger, setup_logging

__all__ = [
    "Plugin",
    "PluginType",
    # 异常类型（errors）
    "AgentMemoryError",
    "NotFoundError",
    "ConflictError",
    "PermissionDeniedError",
    "ValidationError",
    "PolicyError",
    "HealthCheckError",
    "BackendError",
    # 统一日志（log）
    "get_logger",
    "setup_logging",
]
