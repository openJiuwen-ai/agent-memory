"""Additive security capability; MemoryAPI and the machine surfaces stay unchanged."""

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

from jiuwen_memory.common.security.types import RequestSecurityContext


class HumanView(Protocol):
    def admission(self, peer: str) -> AbstractContextManager[None]:
        """Apply the independent human-view request budget."""

    @property
    def origin(self) -> str:
        """Return the exact configured browser origin."""

    def begin(
        self, payload: dict, browser: str, *, security: RequestSecurityContext | None = None
    ) -> dict[str, Any]:
        """Query and stage an authorized snapshot before disclosure; enrollment is independent."""

    def finish(self, payload: dict, browser: str, *, peer: str = "") -> dict[str, Any]:
        """Consume proof and recheck resource access before disclosure."""


@runtime_checkable
class HumanViewRuntime(Protocol):
    @property
    def human_view(self) -> HumanView | None:
        """Return the optional independently authenticated human-view capability."""
