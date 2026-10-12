"""Focused contracts for canonical Schema entity identity."""

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
        temporal=Temporal(t_valid=datetime.now(timezone.utc)),
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
