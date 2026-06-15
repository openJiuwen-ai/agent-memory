"""共享组件：跨层复用的能力插件接口、通用结构体与异常类型（见 base.py / errors.py）。"""

from .base import Plugin, PluginType
from .errors import (
    BackendError,
    BlinkMemError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    PermissionDeniedError,
    PolicyError,
    ValidationError,
)

__all__ = [
    "Plugin",
    "PluginType",
    # 异常类型（errors）
    "BlinkMemError",
    "NotFoundError",
    "ConflictError",
    "PermissionDeniedError",
    "ValidationError",
    "PolicyError",
    "HealthCheckError",
    "BackendError",
]
