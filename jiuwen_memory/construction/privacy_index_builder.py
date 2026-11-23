"""Protect truth and index projections once per composed IndexBuilder operation."""

from contextlib import contextmanager
from contextvars import ContextVar

from jiuwen_memory.common.security.privacy.base import PrivacyProtection, privacy_operation
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction.index_builder import (
    IndexBuilder,
    UnitCommitState,
    UnitCommitStatus,
)
from jiuwen_memory.storage.domain_store import DomainStore
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

    def read_domain_store(self) -> DomainStore | None:
        return self._delegate.read_domain_store()

    def supports_grouped_build(self) -> bool:
        return self._delegate.supports_grouped_build()

    @privacy_operation
    def build_grouped(self, units: list[MemoryUnit]) -> list[UnitCommitStatus]:
        """透传批量建索引能力；只为已提交的条目刷新隐私修订号（F10 决策 4）。"""
        active = _ACTIVE.get()
        owned = [unit for unit in units if (id(self._privacy), id(unit)) not in active]
        if not owned:
            return self._delegate.build_grouped(units)
        owned_ids = {id(unit) for unit in owned}
        token = _ACTIVE.set(active | {(id(self._privacy), uid) for uid in owned_ids})
        try:
            for unit in owned:
                self._privacy.prepare(unit)
            statuses = self._delegate.build_grouped(units)
            for unit, status in zip(units, statuses):
                if status.state is UnitCommitState.COMMITTED and id(unit) in owned_ids:
                    self._privacy.refresh_revision(unit)
            return statuses
        finally:
            _ACTIVE.reset(token)

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
