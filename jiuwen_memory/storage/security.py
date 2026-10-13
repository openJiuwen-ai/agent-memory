# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一 Storage 授权与 Store 数据保护的基础契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.type_def import Scope

if TYPE_CHECKING:
    from jiuwen_memory.common.security.cryptography import CryptographyProvider
    from jiuwen_memory.storage.base import BaseStore


@dataclass(frozen=True)
class StoreEncryptionBinding:
    """加密装饰器公开依赖；相同 inner/provider/format 才能复用保护层。"""

    inner: BaseStore
    provider: CryptographyProvider
    format: str


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

    @property
    def encryption_binding(self) -> StoreEncryptionBinding | None:
        """装饰器声明原始依赖；原生保护实现可返回 None。"""
        return None


class PassthroughStoreSecurity(StoreSecurity):
    def enabled(self) -> bool:
        return False


class EnabledStoreSecurity(StoreSecurity):
    """由既有后端安全组件支撑的已启用数据保护标记。"""

    def __init__(
        self,
        health_check: Callable[[], None],
        *,
        binding: StoreEncryptionBinding | None = None,
    ) -> None:
        self._health_check = health_check
        self._binding = binding

    @property
    def encryption_binding(self) -> StoreEncryptionBinding | None:
        return self._binding

    def enabled(self) -> bool:
        return True

    def health(self) -> None:
        self._health_check()


PASSTHROUGH_STORE_SECURITY = PassthroughStoreSecurity()
