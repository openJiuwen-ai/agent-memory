# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""API-owned user access-control authentication and disclosure boundary."""

from contextlib import AbstractContextManager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from jiuwen_memory.api.human_view import HumanView, PrimaryAuthentication
from jiuwen_memory.common.audit import AuditLogger
from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security.human_authentication.base import HumanAuthenticator
from jiuwen_memory.common.security.privacy.base import HumanViewContent, HumanViewRenderer
from jiuwen_memory.common.security.privacy.disclosure import HumanDisclosure
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard
from jiuwen_memory.common.security.types import RequestSecurityContext
from jiuwen_memory.common.type_def import MemoryUnit

if TYPE_CHECKING:
    from jiuwen_memory.api.memory_api import MemoryAPI


class HumanViewOpsMixin:
    def _init_human_view(self) -> None:
        self._human_disclosure: HumanDisclosure | None = None
        # Control-flow guard only; identities still pass through the explicit get PEP.
        self._human_finish_active: ContextVar[bool] = ContextVar(
            "memory_api_human_finish", default=False
        )

    def bind_human_view(
        self,
        human: HumanAuthenticator,
        audit: AuditLogger,
        limiter: RateLimiter,
        guard: WorkloadGuard,
    ) -> HumanView:
        """Bind trusted assembly dependencies without exporting the render callback."""
        if self._human_disclosure is not None:
            raise ValidationError("user access control is already bound")
        if self._privacy is None or not isinstance(self._engine, HumanViewRenderer):
            raise ValidationError(
                "user access control requires privacy and a dedicated engine renderer"
            )
        service = HumanDisclosure(
            self._privacy, human, self.get, audit, limiter, guard, render=self._render_human_view
        )
        self._human_disclosure = service
        return _APIHumanView(self, service)

    def _require_human_view(self) -> HumanDisclosure:
        if self._human_disclosure is None:
            raise ValidationError("user access control unavailable")
        return self._human_disclosure

    def begin_human_view(
        self,
        payload: dict,
        browser: str,
        *,
        security: RequestSecurityContext | None = None,
        primary_auth: PrimaryAuthentication | None = None,
    ) -> dict[str, Any]:
        service = self._require_human_view()
        if security is not None and primary_auth is not None:
            raise ValidationError("provide only one primary authentication source")
        if payload.get("invite") or primary_auth is None:
            return service.begin(payload, browser, security=security)
        with primary_auth() as authenticated:
            return service.begin(payload, browser, security=authenticated)

    def finish_human_view(self, payload: dict, browser: str, *, peer: str = "") -> dict[str, Any]:
        service = self._require_human_view()
        token = self._human_finish_active.set(True)
        try:
            return service.finish(payload, browser, peer=peer)
        finally:
            self._human_finish_active.reset(token)

    async def _render_human_view(self, operation: str, current: MemoryUnit) -> HumanViewContent:
        if not self._human_finish_active.get():
            raise AuthenticationError("human disclosure requires the MemoryAPI finish entry")
        return await self._engine.render_human_view(self._privacy, operation, current)


class _APIHumanView:
    """Compatibility transport capability; all begin/finish work enters MemoryAPI."""

    def __init__(self, api: "MemoryAPI", service: HumanDisclosure) -> None:
        self._api, self._service = api, service

    @property
    def origin(self) -> str:
        return self._service.origin

    def admission(self, peer: str) -> AbstractContextManager[None]:
        return self._service.admission(peer)

    def security_dependencies(self) -> tuple:
        return self._service.security_dependencies()

    def begin(
        self,
        payload: dict,
        browser: str,
        *,
        security: RequestSecurityContext | None = None,
        primary_auth: PrimaryAuthentication | None = None,
    ) -> dict[str, Any]:
        return self._api.begin_human_view(
            payload, browser, security=security, primary_auth=primary_auth
        )

    def finish(self, payload: dict, browser: str, *, peer: str = "") -> dict[str, Any]:
        return self._api.finish_human_view(payload, browser, peer=peer)
