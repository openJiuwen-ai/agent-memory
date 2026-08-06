"""统一 Storage 授权与 Store 数据保护的基础契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from common.type_def import Scope


class StorageAction(str, Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    GET = "get"
    LIST = "list"
    SEARCH = "search"
    ADMIN = "admin"


@dataclass(frozen=True)
class StorageAccessContext:
    """可插拔授权实现消费的显式访问上下文。"""

    actor: Scope | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class StorageSecurity(ABC):
    @abstractmethod
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        """允许时返回 None，拒绝时抛 PermissionDeniedError。"""

    def health(self) -> None:
        return None


class AllowAllStorageSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        return None


class StoreSecurity(ABC):
    """后端自有数据保护模块；保护动作由具体 Store 实现内部调用。"""

    @abstractmethod
    def enabled(self) -> bool:
        """是否启用了实际数据保护。"""

    def health(self) -> None:
        return None


class PassthroughStoreSecurity(StoreSecurity):
    def enabled(self) -> bool:
        return False


class EnabledStoreSecurity(StoreSecurity):
    """由既有后端安全组件支撑的已启用数据保护标记。"""

    def __init__(self, health_check: Callable[[], None]) -> None:
        self._health_check = health_check

    def enabled(self) -> bool:
        return True

    def health(self) -> None:
        self._health_check()


PASSTHROUGH_STORE_SECURITY = PassthroughStoreSecurity()
