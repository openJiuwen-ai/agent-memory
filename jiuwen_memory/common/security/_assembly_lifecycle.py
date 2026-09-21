# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配根的创建回滚与缓存纪元绑定；不从插件私有状态猜测所有权。"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.capabilities import (
    CloseableSecurityCapability,
    SecurityLifecycleCapability,
    SecurityResourceOwner,
)

_LOG = logging.getLogger(__name__)


class _AssemblyLifecycle:
    def __init__(self) -> None:
        self.created: list[object] = []
        self.failure_cleanup: list[Callable[[], None]] = []

    @staticmethod
    def bind(owner: SecurityResourceOwner) -> None:
        Factory.bind_cache_lifecycle(owner, _share_owners)
        owner.add_close_callback(partial(Factory.reset_if_current_epoch, Factory.cache_epoch()))

    def rollback(self) -> None:
        """只关闭新建对象；不级联包装根而误关其命中的既有共享依赖。"""
        for cleanup in reversed(self.failure_cleanup):
            self._cleanup(cleanup)
        seen: set[int] = set()
        for resource in reversed(self.created):
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            if isinstance(resource, SecurityLifecycleCapability):
                continue
            if isinstance(resource, CloseableSecurityCapability):
                self._cleanup(resource.close)
        Factory.discard_created(tuple(self.created))

    @staticmethod
    def _cleanup(cleanup: Callable[[], None]) -> None:
        try:
            cleanup()
        except BaseException:
            # 仅失败装配的清理边界：继续释放其余资源，保留原启动错误/取消。
            _LOG.error("assembly resource cleanup failed", exc_info=True)


_CURRENT_ASSEMBLY: ContextVar[_AssemblyLifecycle | None] = ContextVar(
    "security_assembly_lifecycle", default=None
)


def _share_owners(left: object, right: object) -> None:
    if not isinstance(left, SecurityResourceOwner) or not isinstance(right, SecurityResourceOwner):
        raise ValidationError("assembly cache requires security resource owners")
    left.share_with(right)


@contextmanager
def _assembly_lifecycle() -> Iterator[_AssemblyLifecycle]:
    existing = _CURRENT_ASSEMBLY.get()
    if existing is not None:
        yield existing
        return
    lifecycle = _AssemblyLifecycle()
    token = _CURRENT_ASSEMBLY.set(lifecycle)
    try:
        with Factory.observe_builds(lifecycle.created.append):
            try:
                yield lifecycle
            except BaseException:
                lifecycle.rollback()
                raise
    finally:
        _CURRENT_ASSEMBLY.reset(token)
