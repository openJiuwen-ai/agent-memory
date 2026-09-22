"""Focused contracts for Schema Property Merge."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction.evolver_impl.schema_property_merge import (
    SchemaPropertyMergeExecutor,
    SchemaPropertyMergePlanner,
)
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from tests.unit.construction.fixtures import MemoryKVStore


class _Embedder:
    @staticmethod
    def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(text) % 7)] for text in texts]


class _LLM:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0

    def chat(self, _messages, **_options) -> str:
        self.calls += 1
        return json.dumps(self.response)


class _Index:
    def __init__(self) -> None:
        self.built: list[MemoryUnit] = []
        self.updated: list[tuple[MemoryUnit, IndexWriteMode]] = []
        self.removed: list[tuple[MemoryUnit, IndexRemoveMode]] = []

    def build(self, units, *, mode=IndexWriteMode.ALL) -> None:
        del mode
        self.built.extend(units)

    def update(self, units, *, mode=IndexWriteMode.ALL) -> None:
        self.updated.extend((unit, mode) for unit in units)

    def remove(self, units, *, mode=IndexRemoveMode.HARD) -> None:
        self.removed.extend((unit, mode) for unit in units)


def _property(
    unit_id: str,
    name: str,
    *,
    entity_id: str,
    operation: str = "set",
    identity_kind: str = "",
    aliases: list[str] | None = None,
    event_time: datetime | None = None,
) -> MemoryUnit:
    metadata = {
        "extraction_mode": "schema",
        "schema_name": "persona",
        "schema_entity_key": entity_id,
        "schema_entity_name": name,
        "schema_entity_normalized_name": name.casefold(),
        "schema_entity_type": "person",
        "schema_entity_description": f"Facts about {name}",
        "schema_entity_aliases": aliases or [],
        "schema_property_name": "occupation",
        "schema_property_operation": operation,
    }
    if identity_kind:
        metadata["schema_entity_identity_kind"] = identity_kind
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=f"{name} is an engineer")],
        temporal=Temporal(t_event=event_time, t_valid=datetime.now(timezone.utc)),
        system_metadata=metadata,
        lifecycle=LifecycleState.ACTIVE,
    )


def test_property_merge_disabled_keeps_append_semantics() -> None:
    kv = MemoryKVStore()
    existing = _property("old", "Alice", entity_id="entity-1")
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    incoming = _property("new", "Alice", entity_id="entity-1")
    planner = SchemaPropertyMergePlanner(
        kv=kv,
        embedder=_Embedder(),
        llm=_LLM({"existing": [], "new": []}),
        merge_enabled=False,
    )

    plan = planner.plan([incoming])

    assert plan.additions == [incoming]
    assert not plan.updates
    assert not plan.archives


def test_explicit_delete_archives_matching_property_through_index_builder() -> None:
    kv = MemoryKVStore()
    existing = _property("old", "Alice", entity_id="entity-1")
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    command = _property(
        "delete-command",
        "Alice",
        entity_id="entity-1",
        operation="delete",
    )
    planner = SchemaPropertyMergePlanner(
        kv=kv,
        embedder=_Embedder(),
        llm=_LLM({"archive": ["p1"]}),
    )
    plan = planner.plan([command])
    index = _Index()

    execution = SchemaPropertyMergeExecutor(index).apply(plan)

    assert execution.archived_ids == [existing.id]
    assert not index.built
    assert index.updated[0][0].lifecycle is LifecycleState.ARCHIVED
    assert index.updated[0][1] is IndexWriteMode.FORWARD_ONLY
    assert index.removed[0][1] is IndexRemoveMode.SOFT


def test_same_event_update_creates_replacement_and_supersedes_old() -> None:
    kv = MemoryKVStore()
    event = datetime(2023, 8, 3, tzinfo=timezone.utc)
    old_message_time = datetime(2023, 8, 3, 9, tzinfo=timezone.utc)
    new_message_time = datetime(2023, 8, 3, 11, tzinfo=timezone.utc)
    existing = _property("old", "Alice", entity_id="entity-1", event_time=event)
    incoming = _property("new", "Alice", entity_id="entity-1", event_time=event)
    existing.source_ref = "source-old"
    existing.provenance = ["source-old"]
    existing.temporal.t_message = old_message_time
    incoming.source_ref = "source-new"
    incoming.provenance = ["source-new"]
    incoming.temporal.t_message = new_message_time
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    planner = SchemaPropertyMergePlanner(
        kv=kv,
        embedder=_Embedder(),
        llm=_LLM(
            {
                "existing": [],
                "new": [
                    {
                        "id": "n1",
                        "op": "update",
                        "target": "p1",
                        "value": "On 2023-08-03, Alice became a senior engineer",
                    }
                ],
            }
        ),
        merge_enabled=True,
    )
    index = _Index()

    execution = SchemaPropertyMergeExecutor(index).apply(planner.plan([incoming]))

    assert execution.created_ids == [incoming.id]
    assert execution.superseded_ids == [existing.id]
    assert index.built[0].supersedes == existing.id
    assert index.built[0].provenance == ["source-new", "source-old"]
    assert index.built[0].temporal.t_message == new_message_time
    assert index.updated[0][0].lifecycle is LifecycleState.SUPERSEDED


def test_one_existing_property_is_replaced_at_most_once_per_batch() -> None:
    kv = MemoryKVStore()
    event = datetime(2023, 8, 3, tzinfo=timezone.utc)
    existing = _property("old", "Alice", entity_id="entity-1", event_time=event)
    incoming = _property("new", "Alice", entity_id="entity-1", event_time=event)
    kv.insert(existing.scope, memory_key(existing.id), dumps(existing))
    planner = SchemaPropertyMergePlanner(
        kv=kv,
        embedder=_Embedder(),
        llm=_LLM(
            {
                "existing": [
                    {
                        "id": "p1",
                        "op": "update",
                        "value": "On 2023-08-03, Alice became a senior engineer",
                    }
                ],
                "new": [
                    {
                        "id": "n1",
                        "op": "update",
                        "target": "p1",
                        "value": "Duplicate instruction must not create a second replacement",
                    }
                ],
            }
        ),
        merge_enabled=True,
    )

    plan = planner.plan([incoming])

    assert len(plan.updates) == 1
