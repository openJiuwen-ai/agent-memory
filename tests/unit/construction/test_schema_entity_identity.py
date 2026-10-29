"""Focused contracts for canonical Schema entity identity."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import SchemaEntityRegistry
from jiuwen_memory.construction.evolver_impl.schema_entity_resolver import SchemaEntityResolver
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


def _property(
    unit_id: str,
    name: str,
    *,
    entity_id: str,
    identity_kind: str = "",
    aliases: list[str] | None = None,
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
    }
    if identity_kind:
        metadata["schema_entity_identity_kind"] = identity_kind
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=f"{name} is an engineer")],
        temporal=Temporal(t_valid=datetime.now(UTC)),
        system_metadata=metadata,
        lifecycle=LifecycleState.ACTIVE,
    )


def _resolver(kv: MemoryKVStore, llm: _LLM) -> SchemaEntityResolver:
    registry = SchemaEntityRegistry(kv)
    return SchemaEntityResolver(
        kv=kv,
        registry=registry,
        embedder=_Embedder(),
        llm=llm,
    )


def test_alias_observation_reuses_registry_entity_without_llm() -> None:
    kv = MemoryKVStore()
    llm = _LLM({"action": "create"})
    resolver = _resolver(kv, llm)
    first = _property("p1", "Caroline", entity_id="temporary-1", aliases=["Caro"])
    resolver.resolve([first])
    resolver.sync([first])
    second = _property("p2", "Caro", entity_id="temporary-2", aliases=["Caroline"])

    resolver.resolve([second])

    assert second.system_metadata["schema_entity_id"] == first.system_metadata["schema_entity_id"]
    assert second.system_metadata["schema_entity_resolution"] == "exact"
    assert llm.calls == 0


def test_different_explicit_speakers_cannot_merge() -> None:
    kv = MemoryKVStore()
    first = _property(
        "p1",
        "Caroline",
        entity_id="temporary-1",
        identity_kind="explicit_speaker",
    )
    resolver = _resolver(kv, _LLM({"action": "create"}))
    resolver.resolve([first])
    resolver.sync([first])
    malicious = _LLM(
        {"action": "update", "target_entity_id": first.system_metadata["schema_entity_id"]}
    )
    resolver = _resolver(kv, malicious)
    second = _property(
        "p2",
        "Melanie",
        entity_id="temporary-2",
        identity_kind="explicit_speaker",
    )

    resolver.resolve([second])

    assert second.system_metadata["schema_entity_id"] != first.system_metadata["schema_entity_id"]
    assert second.system_metadata["schema_entity_resolution"] == "create"
    assert malicious.calls == 0


def test_registry_limit_counts_only_active_entities_in_requested_schema() -> None:
    kv = MemoryKVStore()
    registry = SchemaEntityRegistry(kv)
    unrelated = _property("p1", "Other", entity_id="other")
    unrelated.system_metadata.update(schema_name="other", schema_entity_id="other")
    first = _property("p2", "Caroline", entity_id="first")
    first.system_metadata["schema_entity_id"] = "first"
    second = _property("p3", "Melanie", entity_id="second")
    second.system_metadata["schema_entity_id"] = "second"
    registry.sync([unrelated, first, second])

    found = registry.list(first.scope, "persona", limit=2)

    assert [unit.id for unit in found] == ["first", "second"]


def test_registry_selects_latest_message_time_across_naive_and_aware_values() -> None:
    kv = MemoryKVStore()
    registry = SchemaEntityRegistry(kv)
    earlier = _property("p1", "Caroline", entity_id="entity-1")
    later = _property("p2", "Caroline", entity_id="entity-1")
    for unit in (earlier, later):
        unit.system_metadata["schema_entity_id"] = "entity-1"
    earlier.temporal.t_message = datetime(2026, 1, 1, 8, tzinfo=UTC)
    later.temporal.t_message = datetime(2026, 1, 1, 9)

    record = registry.sync([earlier, later])[0]

    assert record.temporal.t_message is later.temporal.t_message


def test_overlapping_alias_does_not_merge_incompatible_explicit_speakers() -> None:
    kv = MemoryKVStore()
    llm = _LLM({"action": "create"})
    resolver = _resolver(kv, llm)
    first = _property(
        "p1",
        "Caroline",
        entity_id="temporary-1",
        identity_kind="explicit_speaker",
        aliases=["Caro"],
    )
    resolver.resolve([first])
    resolver.sync([first])
    second = _property(
        "p2",
        "Melanie",
        entity_id="temporary-2",
        identity_kind="explicit_speaker",
        aliases=["Caro"],
    )

    resolver.resolve([second])

    assert second.system_metadata["schema_entity_id"] != first.system_metadata["schema_entity_id"]
    assert second.system_metadata["schema_entity_resolution"] == "create"
    assert llm.calls == 0
