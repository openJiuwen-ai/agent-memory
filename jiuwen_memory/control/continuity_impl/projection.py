"""Relation→graph projection port.

The graph layer speaks ``MemoryUnit``; relations are not memory units any more. This
module is the **single** adapter that still knows that mapping, so the continuity
application service only depends on :class:`RelationProjector`, which takes a
:class:`RelationRecord`.

A deployment that has a native relation projector can inject it directly: anything whose
``project()`` accepts ``(scope, relation, revision)`` is used as-is.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import RelationRecord, canonical
from jiuwen_memory.storage.types import IndexWriteMode


class RelationProjector(Protocol):
    """Projects one relation revision into the evidence graph."""

    def project(self, scope: Scope, relation: RelationRecord, revision: int) -> None: ...


class IndexBuilderRelationProjector:
    """Adapter: drives an ``IndexBuilder`` with a synthetic unit carrying the relation.

    The unit is a transport shape for the existing builder, not a second source of truth:
    it is never persisted, and the relation KV head remains the only truth.
    """

    def __init__(self, index_builder) -> None:
        self._index_builder = index_builder

    def project(self, scope: Scope, relation: RelationRecord, revision: int) -> None:
        synthetic = MemoryUnit(
            id=relation.relation_id,
            scope=scope,
            segments=[Segment(content=canonical(asdict(relation)))],
        )
        self._index_builder.build([synthetic], mode=IndexWriteMode.RETRIEVAL_ONLY)
