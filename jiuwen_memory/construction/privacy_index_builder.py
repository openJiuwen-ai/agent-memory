"""Protect truth and index projections once per composed IndexBuilder operation."""

from contextlib import contextmanager
from contextvars import ContextVar

from jiuwen_memory.common.security.privacy.base import PrivacyProtection, privacy_operation
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

_ACTIVE: ContextVar[frozenset[tuple[int, int]]] = ContextVar(
    "privacy_index_operation", default=frozenset()
)


class PrivacyIndexBuilder(IndexBuilder):
    def __init__(self, delegate: IndexBuilder, privacy: PrivacyProtection) -> None:
        self._delegate, self._privacy = delegate, privacy

    @property
    def delegate(self) -> IndexBuilder:
        return self._delegate

    def operator_type(self):
        return self._delegate.operator_type()

    def health(self) -> None:
        self._privacy.health()
        self._delegate.health()

    @contextmanager
    def _operation(self, units: list[MemoryUnit]):
        active = _ACTIVE.get()
        owned = [unit for unit in units if (id(self._privacy), id(unit)) not in active]
        if not owned:
            yield
            return
        token = _ACTIVE.set(active | {(id(self._privacy), id(unit)) for unit in owned})
        try:
            for unit in owned:
                self._privacy.prepare(unit)
            yield
            for unit in owned:
                self._privacy.refresh_revision(unit)
        finally:
            _ACTIVE.reset(token)

    @privacy_operation
    def build(self, units, *, mode=IndexWriteMode.ALL) -> None:
        with self._operation(units):
            self._delegate.build(units, mode=mode)

    @privacy_operation
    def update(self, units, *, mode=IndexWriteMode.ALL) -> None:
        with self._operation(units):
            self._delegate.update(units, mode=mode)

    def remove(self, units, *, mode=IndexRemoveMode.HARD) -> None:
        # Only the truth KV boundary owns private-state deletion. Some delegates
        # remove projections alone, and partial failures can leave truth intact.
        self._delegate.remove(units, mode=mode)

    def remove_with_scope(self, unit_ids: list[str], scope: Scope) -> None:
        self._delegate.remove_with_scope(unit_ids, scope)

    def rebuild(self) -> None:
        # Delegates must rebuild from protected truth; never decrypt for recall.
        self._delegate.rebuild()
