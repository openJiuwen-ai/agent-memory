"""Entity Identity and Property Merge contracts for the Schema Evolver."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    ChatMessage,
    LifecycleState,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import (
    SchemaEntityRegistry,
    schema_entity_key,
)
from jiuwen_memory.construction.evolver_impl.schema_entity_resolver import (
    SchemaEntityResolver,
)
from jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver import (
    SchemaOrchestratingEvolver,
    _build_schema_components,
)
from jiuwen_memory.construction.evolver_impl.schema_property_merge import (
    SchemaPropertyMergeExecutor,
    SchemaPropertyMergePlanner,
    _same_property_event,
)
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.types import VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="org", user="alice", agent="agent", session="s1")
_EVENT = datetime(2024, 1, 1, tzinfo=timezone.utc)


class _ConstantEmbedder(Embedder):
    def plugin_type(self) -> PluginType:
        return PluginType.EMBEDDER

    def health(self) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def dimension(self) -> int:
        return 2


class _QueueLLM(LLM):
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del options
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("no queued response")
        return self.responses.pop(0)


class _RecordingIndexBuilder(IndexBuilder):
    def __init__(self) -> None:
        self.built: list[str] = []
        self.updated: list[str] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit]) -> None:
        self.built.extend(unit.id for unit in units)

    def update(self, units: list[MemoryUnit]) -> None:
        self.updated.extend(unit.id for unit in units)

    def remove(self, units: list[MemoryUnit]) -> None:
        del units

    def rebuild(self) -> None:
        return None


class _StaticExtractor:
    def __init__(self, candidates: list[MemoryUnit]) -> None:
        self.candidates = candidates

    def extract(self, _units, *, context=None) -> list[MemoryUnit]:
        del context
        return self.candidates


class _SchemaOrchestratingHarness(SchemaOrchestratingEvolver):
    def __init__(
        self,
        *,
        storage: CompositeStorage,
        index_builder: IndexBuilder,
        extractor: _StaticExtractor,
        procedural: bool = False,
        planner: SchemaPropertyMergePlanner | None = None,
        executor: SchemaPropertyMergeExecutor | None = None,
    ) -> None:
        self.procedural = procedural
        super().__init__(
            extractor=extractor,
            abstractor=SimpleNamespace(),
            associator=SimpleNamespace(),
            index_builder=index_builder,
            storage=storage,
            dedup=SimpleNamespace(),
            llm=SimpleNamespace(),
            schema_property_merge_planner=planner,
            schema_property_merge_executor=executor,
        )

    def _persist_and_maintain_messages(self, _units):
        return []

    def _maybe_collect_extract_context(self, _units, _recent):
        return None

    def _is_procedural(self, _units):
        return self.procedural

    @staticmethod
    def _annotate_layers(_units):
        return None


def _storage(*, vector: bool = False) -> CompositeStorage:
    return CompositeStorage(
        kv=InMemoryKVStore(),
        vector=InMemoryVectorStore() if vector else None,
    )


def _property(
    unit_id: str,
    value: str,
    *,
    entity_key: str = "entity-alice",
    entity_name: str = "Alice",
    property_name: str = "occupation",
    event_time: datetime | None = _EVENT,
    operation: str = "set",
    provenance: list[str] | None = None,
    identity_kind: str = "",
    scope: Scope = _SCOPE,
    entity_type: str = "person",
    entity_id: str = "",
) -> MemoryUnit:
    metadata: dict[str, object] = {
        "extraction_mode": "schema",
        "schema_name": "people.json",
        "schema_entity_key": entity_key,
        "schema_entity_name": entity_name,
        "schema_entity_normalized_name": entity_name.casefold(),
        "schema_entity_type": entity_type,
        "schema_entity_aliases": [],
        "schema_property_name": property_name,
        "schema_property_operation": operation,
    }
    if identity_kind:
        metadata["schema_entity_identity_kind"] = identity_kind
    if entity_id:
        metadata["schema_entity_id"] = entity_id
    return MemoryUnit(
        id=unit_id,
        scope=scope,
        segments=[Segment(content=value, source=Modality.TEXT)],
        source_ref=(provenance or [""])[0],
        temporal=Temporal(t_event=event_time),
        provenance=list(provenance or []),
        system_metadata=metadata,
        entities=[entity_name],
    )


def _persist(storage: CompositeStorage, *units: MemoryUnit) -> None:
    for unit in units:
        storage.add(unit.scope, [unit])
        if storage.has_vector():
            storage.vector.insert(
                unit.scope,
                [
                    VectorRecord(
                        id=f"{unit.id}-0",
                        vector=[1.0, 0.0],
                        metadata={
                            "unit_id": unit.id,
                            "lifecycle": unit.lifecycle.value,
                            "system_metadata.extraction_mode": unit.system_metadata.get(
                                "extraction_mode"
                            ),
                            "system_metadata.schema_entity_key": unit.system_metadata.get(
                                "schema_entity_key"
                            ),
                            "system_metadata.schema_property_name": unit.system_metadata.get(
                                "schema_property_name"
                            ),
                        },
                    )
                ],
            )


def _planner(
    storage: CompositeStorage,
    llm: LLM,
    *,
    merge_enabled: bool = True,
) -> SchemaPropertyMergePlanner:
    return SchemaPropertyMergePlanner(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=llm,
        merge_enabled=merge_enabled,
    )


def test_month_interval_is_a_correctable_same_property_event() -> None:
    old = _property("old", "Alice joined Acme in 2023-08", event_time=None)
    new = _property("new", "Alice joined Beta in 2023-08", event_time=None)
    for unit in (old, new):
        unit.system_metadata.update(
            {
                "schema_event_time": "2023-08",
                "schema_event_time_precision": "month",
                "schema_event_time_start": "2023-08-01T00:00:00+00:00",
                "schema_event_time_end": "2023-09-01T00:00:00+00:00",
            }
        )

    assert _same_property_event(old, new)
    new.system_metadata["schema_event_time_end"] = "2023-10-01T00:00:00+00:00"
    assert not _same_property_event(old, new)


def test_merge_update_creates_replacement_and_supersedes_old_version() -> None:
    storage = _storage()
    old = _property("old", "Alice is an engineer", provenance=["source-old"])
    candidate = _property(
        "new",
        "Alice is a staff engineer",
        provenance=["source-new"],
    )
    _persist(storage, old)
    llm = _QueueLLM(
        json.dumps(
            {
                "existing": [],
                "new": [
                    {
                        "id": "n1",
                        "op": "update",
                        "target": "p1",
                        "value": "Alice is a staff engineer",
                    }
                ],
            }
        )
    )
    index = _RecordingIndexBuilder()

    execution = SchemaPropertyMergeExecutor(storage=storage, index_builder=index).apply(
        _planner(storage, llm).plan([candidate])
    )

    assert execution.created_ids == ["new"]
    assert execution.superseded_ids == ["old"]
    loaded_old, loaded_new = storage.get(_SCOPE, ["old", "new"])
    assert loaded_old.lifecycle is LifecycleState.SUPERSEDED
    assert loaded_new.supersedes == "old"
    assert loaded_new.provenance == ["source-new", "source-old"]
    assert loaded_new.system_metadata["property_merge_action"] == "supersede"
    assert index.built == ["new"]
    assert index.updated == ["old"]


def test_invalid_merge_answer_appends_new_property_without_information_loss() -> None:
    storage = _storage()
    old = _property("old", "Alice is an engineer")
    candidate = _property("new", "Alice became a manager")
    _persist(storage, old)

    execution = SchemaPropertyMergeExecutor(
        storage=storage,
        index_builder=_RecordingIndexBuilder(),
    ).apply(_planner(storage, _QueueLLM("not json")).plan([candidate]))

    assert execution.created_ids == ["new"]
    assert storage.get(_SCOPE, ["old"])[0].lifecycle is LifecycleState.ACTIVE
    assert storage.get(_SCOPE, ["new"])[0].content == "Alice became a manager"


def test_vector_recall_uses_mem2_system_metadata_filter_paths() -> None:
    storage = _storage(vector=True)
    old = _property("old", "Alice is an engineer")
    candidate = _property("new", "Alice is a staff engineer")
    _persist(storage, old)
    llm = _QueueLLM(
        json.dumps(
            {
                "existing": [],
                "new": [
                    {
                        "id": "n1",
                        "op": "update",
                        "target": "p1",
                        "value": "Alice is a staff engineer",
                    }
                ],
            }
        )
    )

    plan = _planner(storage, llm).plan([candidate])

    assert plan.additions == []
    assert [operation.target.id for operation in plan.updates] == ["old"]


def test_explicit_delete_archives_match_and_never_persists_command() -> None:
    storage = _storage()
    old = _property("old", "Alice is an engineer")
    command = _property("delete-command", "Alice is an engineer", operation="delete")
    _persist(storage, old)

    execution = SchemaPropertyMergeExecutor(
        storage=storage,
        index_builder=_RecordingIndexBuilder(),
    ).apply(_planner(storage, _QueueLLM()).plan([command]))

    assert execution.archived_ids == ["old"]
    assert storage.get(_SCOPE, ["old"])[0].lifecycle is LifecycleState.ARCHIVED
    assert storage.get(_SCOPE, ["delete-command"]) == []


def test_distinct_explicit_speakers_receive_distinct_entity_ids() -> None:
    storage = _storage()
    caroline = _property(
        "old",
        "Caroline likes hiking",
        entity_key="entity-caroline",
        entity_name="Caroline",
        identity_kind="explicit_speaker",
    )
    caroline.system_metadata["schema_entity_id"] = "entity-caroline"
    melanie = _property(
        "new",
        "Melanie started a job",
        entity_key="provisional-melanie",
        entity_name="Melanie",
        identity_kind="explicit_speaker",
    )
    _persist(storage, caroline)
    llm = _QueueLLM(json.dumps({"action": "update", "target_entity": "Caroline"}))

    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=llm,
    ).resolve([melanie])

    assert melanie.system_metadata["schema_entity_id"] != "entity-caroline"
    assert melanie.system_metadata["schema_entity_resolution"] == "create"
    assert melanie.entities == ["Melanie"]
    assert llm.calls == []


def test_schema_evolver_routes_properties_through_merge_planner() -> None:
    storage = _storage()
    index = _RecordingIndexBuilder()
    old = _property("old", "Alice is an engineer", provenance=["source-old"])
    candidate = _property("new", "Alice is a staff engineer", provenance=["source-new"])
    _persist(storage, old)
    llm = _QueueLLM(
        json.dumps(
            {
                "existing": [],
                "new": [
                    {
                        "id": "n1",
                        "op": "update",
                        "target": "p1",
                        "value": "Alice is a staff engineer",
                    }
                ],
            }
        )
    )
    evolver = _SchemaOrchestratingHarness(
        storage=storage,
        index_builder=index,
        extractor=_StaticExtractor([candidate]),
        planner=_planner(storage, llm),
        executor=SchemaPropertyMergeExecutor(storage=storage, index_builder=index),
    )
    source = MemoryUnit(
        id="source-current",
        scope=_SCOPE,
        segments=[Segment(content="Alice changed roles")],
    )

    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == ["source-current", "new"]
    assert result.superseded_ids == ["old"]
    assert storage.get(_SCOPE, ["old"])[0].lifecycle is LifecycleState.SUPERSEDED


def test_property_merge_applies_add_update_and_archive_as_one_plan() -> None:
    storage = _storage()
    obsolete = _property("old-1", "Alice worked at Acme", provenance=["source-old-1"])
    target = _property("old-2", "Alice is an engineer", provenance=["source-old-2"])
    candidate = _property(
        "new-1",
        "Alice is a staff engineer at Beta",
        provenance=["source-new-1"],
    )
    _persist(storage, obsolete, target)
    llm = _QueueLLM(
        json.dumps(
            {
                "existing": [{"id": "p2", "op": "delete"}],
                "new": [
                    {
                        "id": "n1",
                        "op": "update",
                        "target": "p1",
                        "value": "Alice is a staff engineer at Beta",
                    }
                ],
            }
        )
    )

    execution = SchemaPropertyMergeExecutor(
        storage=storage,
        index_builder=_RecordingIndexBuilder(),
    ).apply(_planner(storage, llm).plan([candidate]))

    assert execution.created_ids == ["new-1"]
    assert execution.superseded_ids == ["old-2"]
    assert execution.archived_ids == ["old-1"]
    old_obsolete, old_target, replacement = storage.get(
        _SCOPE,
        ["old-1", "old-2", "new-1"],
    )
    assert old_obsolete.lifecycle is LifecycleState.ARCHIVED
    assert old_target.lifecycle is LifecycleState.SUPERSEDED
    assert replacement.supersedes == "old-2"
    assert replacement.provenance == ["source-new-1", "source-old-2"]


def test_duplicate_update_target_preserves_the_second_new_property() -> None:
    storage = _storage()
    _persist(storage, _property("old", "Alice uses Python"))
    first = _property("new-1", "Alice uses Python 3.13")
    second = _property("new-2", "Alice also uses Rust")
    llm = _QueueLLM(
        json.dumps(
            {
                "existing": [],
                "new": [
                    {"id": "n1", "op": "update", "target": "p1", "value": "Python 3.13"},
                    {"id": "n2", "op": "update", "target": "p1", "value": "Rust"},
                ],
            }
        )
    )

    plan = _planner(storage, llm).plan([first, second])

    assert [operation.target.id for operation in plan.updates] == ["old"]
    assert [unit.id for unit in plan.additions] == ["new-2"]


def test_property_recall_requires_exact_scope_and_entity() -> None:
    storage = _storage()
    other_scope = Scope(org="org", user="alice", agent="agent", session="s2")
    _persist(
        storage,
        _property("other-entity", "Bob is an engineer", entity_key="entity-bob"),
        _property("other-scope", "Alice is an engineer", scope=other_scope),
    )
    candidate = _property("new", "Alice became a manager")
    llm = _QueueLLM('{"existing": [], "new": []}')

    plan = _planner(storage, llm).plan([candidate])

    assert [unit.id for unit in plan.additions] == ["new"]
    assert plan.updates == []
    assert plan.archives == []
    assert llm.calls == []


def test_entity_registry_survives_restart_and_keeps_propertyless_identity() -> None:
    storage = _storage()
    observation = _property("entity-observation", "Alice", entity_name="Alice")
    observation.system_metadata["extraction_mode"] = "schema_entity_observation"
    observation.system_metadata.pop("schema_property_name")
    observation.system_metadata.pop("schema_property_operation")
    first_resolver = SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=_QueueLLM(),
    )
    first_resolver.resolve([observation])
    entity_id = str(observation.system_metadata["schema_entity_id"])
    SchemaEntityRegistry(storage=storage, embedder=_ConstantEmbedder()).sync([observation])

    raw = storage.kv.get(_SCOPE, schema_entity_key(entity_id))
    entity = loads(raw)
    assert entity is not None
    assert entity.system_metadata["schema_entity_name"] == "Alice"

    new_property = _property("after-restart", "Alice became a manager")
    second_llm = _QueueLLM()
    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=second_llm,
    ).resolve([new_property])

    assert new_property.system_metadata["schema_entity_id"] == entity_id
    assert new_property.system_metadata["schema_entity_resolution"] == "exact"
    assert second_llm.calls == []


def test_entity_registry_writes_named_entity_vectors_and_resolver_reads_them() -> None:
    entity_vectors = InMemoryVectorStore()
    storage = CompositeStorage(
        kv=InMemoryKVStore(),
        vector_ports={"schema_entities": entity_vectors},
    )
    observation = _property("entity-observation", "Alice is a software engineer")
    observation.system_metadata["extraction_mode"] = "schema_entity_observation"
    observation.system_metadata["schema_entity_aliases"] = ["Alice Chen"]
    observation.system_metadata.pop("schema_property_name")
    observation.system_metadata.pop("schema_property_operation")
    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=_QueueLLM(),
    ).resolve([observation])
    entity_id = str(observation.system_metadata["schema_entity_id"])

    SchemaEntityRegistry(storage=storage, embedder=_ConstantEmbedder()).sync([observation])

    records = entity_vectors.get(
        _SCOPE,
        [entity_id, f"{entity_id}#sf0", f"{entity_id}#sf1"],
    )
    assert records[0].metadata["entity_vector_owner_id"] == entity_id
    assert any(record.id.startswith(f"{entity_id}#sf") for record in records)

    candidate = _property("new-property", "Alice became a manager")
    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=_QueueLLM(),
    ).resolve([candidate])
    assert candidate.system_metadata["schema_entity_id"] == entity_id


def test_entity_identity_rejects_same_name_with_incompatible_type() -> None:
    storage = _storage()
    person = _property(
        "old",
        "Acme is a person",
        entity_name="Acme",
        entity_type="person",
        entity_id="entity-person-acme",
    )
    _persist(storage, person)
    company = _property(
        "new",
        "Acme is a company",
        entity_name="Acme",
        entity_type="company",
    )
    llm = _QueueLLM(json.dumps({"action": "create"}))

    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=llm,
    ).resolve([company])

    assert company.system_metadata["schema_entity_id"] != "entity-person-acme"
    assert company.system_metadata["schema_entity_resolution"] == "create"


def test_entity_identity_reuses_exact_base_name_and_type_without_llm() -> None:
    storage = _storage()
    _persist(
        storage,
        _property(
            "old",
            "Toby is a German Shepherd",
            entity_name="Toby (German Shepherd)",
            entity_type="pet",
            entity_id="entity-toby",
        ),
    )
    candidate = _property(
        "new",
        "Toby is a golden retriever",
        entity_name="Toby (golden retriever)",
        entity_type="pet",
    )
    llm = _QueueLLM()

    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=llm,
    ).resolve([candidate])

    assert candidate.system_metadata["schema_entity_id"] == "entity-toby"
    assert candidate.system_metadata["schema_entity_name"] == "Toby (German Shepherd)"
    assert candidate.system_metadata["schema_entity_resolution"] == "exact"
    assert llm.calls == []


def test_named_entity_never_merges_into_generic_user() -> None:
    storage = _storage()
    _persist(
        storage,
        _property(
            "old",
            "The user enjoys hiking",
            entity_name="User",
            entity_id="entity-user",
        ),
    )
    alice = _property("new", "Alice enjoys hiking", entity_name="Alice")
    llm = _QueueLLM(json.dumps({"action": "update", "target_entity": "User"}))

    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=llm,
    ).resolve([alice])

    assert alice.system_metadata["schema_entity_id"] != "entity-user"
    assert alice.system_metadata["schema_entity_resolution"] == "create"
    assert llm.calls == []


def test_contaminated_legacy_entity_identity_is_not_reused() -> None:
    storage = _storage()
    _persist(
        storage,
        _property(
            "old-1",
            "Caroline likes hiking",
            entity_name="Caroline",
            entity_id="legacy-shared-user",
        ),
        _property(
            "old-2",
            "Melanie started a job",
            entity_name="Melanie",
            entity_id="legacy-shared-user",
        ),
    )
    fresh = _property("new", "Caroline volunteers", entity_name="Caroline")
    llm = _QueueLLM()

    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=llm,
    ).resolve([fresh])

    assert fresh.system_metadata["schema_entity_id"] != "legacy-shared-user"
    assert fresh.system_metadata["schema_entity_resolution"] == "create"
    assert llm.calls == []


def test_invalid_entity_merge_response_fails_safe_to_create() -> None:
    storage = _storage()
    _persist(
        storage,
        _property(
            "old",
            "Caroline likes hiking",
            entity_name="Caroline",
            entity_id="entity-caroline",
        ),
    )
    melanie = _property("new", "Melanie started a job", entity_name="Melanie")

    SchemaEntityResolver(
        storage=storage,
        embedder=_ConstantEmbedder(),
        llm=_QueueLLM("not json"),
        max_merge_retries=1,
    ).resolve([melanie])

    assert melanie.system_metadata["schema_entity_id"] != "entity-caroline"
    assert melanie.system_metadata["schema_entity_resolution"] == "create"


class _Config:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def test_original_property_merge_config_key_and_default_are_preserved() -> None:
    storage = _storage()
    index = _RecordingIndexBuilder()
    old = _property("old", "Alice is an engineer")
    candidate = _property("candidate", "Alice is a staff engineer")
    _persist(storage, old)
    default_llm = _QueueLLM()
    original_key_llm = _QueueLLM()
    alias_llm = _QueueLLM("not json")

    default_components = _build_schema_components(
        _Config({}),
        storage=storage,
        index_builder=index,
        llm=default_llm,
        embedder=_ConstantEmbedder(),
    )
    original_key_wins = _build_schema_components(
        _Config({"use_property_merge": False, "schema_property_merge_enabled": True}),
        storage=storage,
        index_builder=index,
        llm=original_key_llm,
        embedder=_ConstantEmbedder(),
    )
    alias_components = _build_schema_components(
        _Config({"schema_property_merge_enabled": True}),
        storage=storage,
        index_builder=index,
        llm=alias_llm,
        embedder=_ConstantEmbedder(),
    )

    assert default_components.planner.plan([candidate]).additions == [candidate]
    assert original_key_wins.planner.plan([candidate]).additions == [candidate]
    assert alias_components.planner.plan([candidate]).additions == [candidate]
    assert default_llm.calls == []
    assert original_key_llm.calls == []
    assert len(alias_llm.calls) == 1


def test_schema_procedural_path_resolves_and_merges_without_source_copy() -> None:
    storage = _storage()
    index = _RecordingIndexBuilder()
    candidate = _property("property", "Alice is an engineer")
    evolver = _SchemaOrchestratingHarness(
        storage=storage,
        index_builder=index,
        extractor=_StaticExtractor([candidate]),
        procedural=True,
        planner=_planner(storage, _QueueLLM(), merge_enabled=False),
        executor=SchemaPropertyMergeExecutor(storage=storage, index_builder=index),
    )
    source = MemoryUnit(id="source", scope=_SCOPE, segments=[Segment(content="source")])

    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == ["property"]
    assert storage.get(_SCOPE, ["source"]) == []
    assert storage.get(_SCOPE, ["property"])[0].content == "Alice is an engineer"
