"""Privacy protection contracts; optional and independent of the primary authenticator."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.text_split import split_within_limit
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Scope

PRIVACY_TOKEN = re.compile(r"\[JW-PRIVATE:([a-f0-9]{48})\]")
_TOKEN_WIDTH = len("[JW-PRIVATE:") + 48 + 1


def protected_text_boundary(text: str, position: int, *, right: bool = False) -> int:
    """Keep an opaque reference whole; this pure operation grants no access to its value."""
    for match in PRIVACY_TOKEN.finditer(
        text, max(0, position - _TOKEN_WIDTH), min(len(text), position + _TOKEN_WIDTH)
    ):
        if match.start() < position < match.end():
            return match.end() if right else match.start()
    return position


def split_protected_text(text: str, limit: int) -> list[str]:
    """Use the shared paragraph splitter without bisecting opaque references."""
    from jiuwen_memory.common.errors import ValidationError

    def boundary(remaining: str, cut: int) -> int:
        cut = protected_text_boundary(remaining, cut)
        if cut == 0:
            raise ValidationError("privacy placeholder exceeds configured text limit")
        return cut

    return split_within_limit(text, limit, boundary=boundary)


class PrivacyProducer(Factory):
    TOP_NAME = "privacy"


class PrivacyProtection(ABC):
    @abstractmethod
    def staging_scope(self, *, retain_on_success: bool = False) -> AbstractContextManager[None]:
        """Own new volatile ciphertext; release it when a write/processing scope finishes."""

    def bind_audit(self, audit) -> None:
        """Bind the final shared AuditLogger, including its optional integrity wrapper."""

    def health(self) -> None:
        """Check the vault and cryptography dependency."""

    def preview_echo(self, value: Any, *, label: str = "") -> Any:
        """Sanitize a caller-input echo, including invalid placeholder attempts."""
        if isinstance(value, str):
            return self.preview(value.replace("[JW-PRIVATE:", "[PRIVATE:"), label=label)
        if isinstance(value, list):
            return [self.preview_echo(item, label=label) for item in value]
        if isinstance(value, dict):
            return {
                self.preview_echo(key): self.preview_echo(item, label=str(key))
                for key, item in value.items()
            }
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            protected = self.preview(str(value), label=label)
            return protected if protected != str(value) else value
        return value

    @abstractmethod
    def inherit_classification(
        self,
        unit: MemoryUnit | RawPayload,
        sources: list[MemoryUnit | RawPayload],
    ) -> None:
        """Retain the strongest original classification when processing removes evidence."""

    @abstractmethod
    def preview(self, text: str, *, label: str = "") -> str:
        """Mask untrusted routing/query text without retaining originals."""

    @abstractmethod
    def ingress(self, payload: RawPayload) -> RawPayload:
        """Return a protected copy; may stage ciphertext in bounded volatile memory only."""

    @abstractmethod
    def prepare(self, unit: MemoryUnit) -> None:
        """Protect every supported textual projection in place before processing/persistence."""

    @abstractmethod
    def relocate(self, unit: MemoryUnit, target: Scope) -> None:
        """Stage ciphertext references after a trusted router has validated the target."""

    @abstractmethod
    def validate_existing(self, unit: MemoryUnit, *, channel: str = "memory") -> None:
        """Refuse a legacy/unmigrated namespace before the runtime is published."""

    @abstractmethod
    def persist(self, unit: MemoryUnit, *, channel: str = "memory") -> None:
        """Persist encrypted fragments and a scope/version/content-bound manifest."""

    @abstractmethod
    def write(
        self,
        unit: MemoryUnit,
        commit: Callable[[MemoryUnit], None],
        read: Callable[[], MemoryUnit | None],
        *,
        channel: str = "memory",
    ) -> None:
        """Publish protected truth; reconcile failures without destroying surviving revisions."""

    @abstractmethod
    def refresh_revision(self, unit: MemoryUnit) -> None:
        """Copy a matching committed manifest revision to the caller's protected unit."""

    @abstractmethod
    def discard(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        channel: str = "memory",
        commit: Callable[[], None] | None = None,
    ) -> None:
        """Delete truth first when supplied, then revoke its private state under the same lock."""

    @abstractmethod
    def discard_scope(self, scope: Scope) -> None:
        """Hard-delete private records under the selected complete or prefix Scope."""

    @abstractmethod
    def render(self, unit: MemoryUnit) -> str:
        """Private service operation, callable only after human proof and normal resource PEP."""


def optional_privacy(config: Any) -> PrivacyProtection | None:
    specs = config.ctx.namespaces.get(PrivacyProducer.TOP_NAME, {})
    enabled = config.ctx.globals.get("privacy_enabled", bool(specs))
    from jiuwen_memory.common.errors import ValidationError

    if not isinstance(enabled, bool):
        raise ValidationError("globals.privacy_enabled must be bool")
    if not enabled:
        return None
    if not specs:
        raise ValidationError("privacy_enabled requires privacy.default configuration")

    if set(specs) != {"default"} or specs["default"].new_instance:
        raise ValidationError("privacy requires one shared instance named default")
    return PrivacyProducer.build_named("default", config.ctx)


def privacy_operation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Preserve public signatures while bounding volatile staging to one complete operation."""

    def scope(owner):
        privacy = getattr(owner, "_privacy", None)
        return privacy.staging_scope() if privacy is not None else nullcontext()

    if iscoroutinefunction(method):

        @wraps(method)
        async def asynchronous(self, *args, **kwargs):
            with scope(self):
                return await method(self, *args, **kwargs)

        return asynchronous

    @wraps(method)
    def synchronous(self, *args, **kwargs):
        with scope(self):
            return method(self, *args, **kwargs)

    return synchronous
