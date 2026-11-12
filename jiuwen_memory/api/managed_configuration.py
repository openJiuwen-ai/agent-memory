# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""配置管理公开门面；宿主提供已装配服务与可信授权能力。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security.types import RequestSecurityContext
from jiuwen_memory.config.managed_service import ManagedConfigService


class ManagedConfiguration:
    """每次读取、修改及发布重试都委托宿主可信授权；不接收客户端声明的身份。"""

    def __init__(
        self,
        service: ManagedConfigService,
        authorize: Callable[[RequestSecurityContext, str], bool],
    ) -> None:
        self._service = service
        self._authorize = authorize

    def _check(self, security: RequestSecurityContext, operation: str) -> None:
        if self._authorize(security, operation) is not True:
            raise PermissionDeniedError(message="configuration access denied")

    def read(self, *, security: RequestSecurityContext) -> dict[str, Any]:
        self._check(security, "read")
        return self._service.read()

    def save(
        self,
        expected_version: int,
        changes: Mapping[str, Any],
        *,
        security: RequestSecurityContext,
        restore: Sequence[str] = (),
    ) -> dict[str, Any]:
        self._check(security, "write")
        result = self._service.save(expected_version, changes, restore=restore)
        return {"version": result.snapshot.version, "applied": result.applied}

    def retry_publication(self, *, security: RequestSecurityContext) -> dict[str, Any]:
        self._check(security, "write")
        result = self._service.retry_publication()
        return {"version": result.snapshot.version, "applied": result.applied}
