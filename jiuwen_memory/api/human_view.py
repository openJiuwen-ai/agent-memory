"""User access-control transport adapter for the optional MemoryAPI security entries."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

from jiuwen_memory.common.security.types import RequestSecurityContext

PrimaryAuthentication = Callable[[], AbstractContextManager[RequestSecurityContext]]


class HumanView(Protocol):
    def admission(self, peer: str) -> AbstractContextManager[None]:
        """Apply the independent user access-control request budget."""

    @property
    def origin(self) -> str:
        """Return the exact configured browser origin."""

    def begin(
        self,
        payload: dict,
        browser: str,
        *,
        security: RequestSecurityContext | None = None,
        primary_auth: PrimaryAuthentication | None = None,
    ) -> dict[str, Any]:
        """Query and stage an authorized snapshot before disclosure; enrollment is independent."""

    def finish(self, payload: dict, browser: str, *, peer: str = "") -> dict[str, Any]:
        """Consume proof and recheck resource access before disclosure."""


@runtime_checkable
class HumanViewRuntime(Protocol):
    @property
    def human_view(self) -> HumanView | None:
        """Return the optional independently authenticated user access-control capability."""
