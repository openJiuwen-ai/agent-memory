"""Focused contracts for the opt-in Schema extraction extension."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import build_kernel
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import (
    ChatMessage,
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.config import Config
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.construction.entity_schema import EntitySchemaCatalog
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver import (
    SchemaOrchestratingEvolver,
)
from jiuwen_memory.construction.extractor_impl.entity_schema_extractor import (
    EntitySchemaExtractor,
    InvalidSchemaExtractionError,
    SchemaExtractionNormalizer,
    _parse_json_object,
)
from jiuwen_memory.construction.index_builder_impl.entity_index_builder import EntityLinkService
from jiuwen_memory.storage.entity_store import EntityStore
from jiuwen_memory.storage.types import IndexWriteMode

pytestmark = pytest.mark.unit


def _catalog() -> EntitySchemaCatalog:
    return EntitySchemaCatalog.from_data(
        [
            {
                "entity_type": "person",
                "dynamic_property": {
                    "default_property": {"desc": "fallback"},
                    "occupation": {"desc": "job"},
                    "birthday": {"desc": "birth date"},
                },
            }
        ]
    )


def _source(unit_id: str = "source-1") -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=Scope(org="org", user="user"),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content="speaker=Alice: I became an engineer in 2023-08")],
        temporal=Temporal(),
        user_metadata={"dataset": "test"},
    )


class _ConstantResponseLLM(LLM):
    def __init__(self, response: str) -> None:
        self.response = response

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del messages, options
        return self.response


class _SequenceResponseLLM(LLM):
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.call_count = 0

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del messages, options
        response = self._responses[self.call_count]
        self.call_count += 1
        return response


class _CapturingResponseLLM(_ConstantResponseLLM):
    def __init__(self, response: str) -> None:
        super().__init__(response)
        self.messages: list[ChatMessage] = []

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del options
        self.messages.extend(messages)
        return self.response


def _property_response(
    source_id: str,
    *,
    entity_type: str = "person",
    property_name: str = "occupation",
) -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": entity_type,
                    "properties": [
                        {
                            "property_name": property_name,
                            "value": "Alice is an engineer",
                            "time": "",
                            "source_unit_ids": [source_id],
                        }
                    ],
                }
            ]
        }
    )


def test_selected_schema_is_the_validation_allowlist() -> None:
    normalizer = SchemaExtractionNormalizer(_catalog())
    selected = [
        {
            "entity_type": "person",
            "dynamic_property": {
                "default_property": {"desc": "fallback"},
                "occupation": {"desc": "job"},
            },
        }
    ]
    normalized, errors = normalizer.normalize_and_validate(
        {
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "birthday",
                            "value": "Alice was born on 1990-01-01",
                            "time": "1990-01-01",
                        }
                    ],
                }
            ]
        },
        "2026-08-19",
        entity_schema=selected,
    )

    assert normalized["entities"][0]["properties"] == []
    assert any("was not selected" in error for error in errors)


def test_entity_name_uniqueness_uses_persisted_stripped_names() -> None:
    normalizer = SchemaExtractionNormalizer(_catalog())

    normalized, errors = normalizer.normalize_and_validate(
        {
            "entities": [
                {"name": "Alice", "entity_type": "person", "properties": []},
                {"name": " Alice ", "entity_type": "person", "properties": []},
            ]
        },
        "2026-08-19",
    )

    assert [entity["name"] for entity in normalized["entities"]] == ["Alice", "Alice"]
    assert "entity names must be unique within one response" in errors


def test_episode_entity_is_rejected_instead_of_silently_dropped() -> None:
    catalog = EntitySchemaCatalog.from_data(
        [
            {
                "entity_type": "person",
                "dynamic_property": {"default_property": {"desc": "fallback"}},
            },
            {
                "entity_type": "episodes",
                "dynamic_property": {"default_property": {"desc": "raw episode"}},
            },
        ]
    )
    normalizer = SchemaExtractionNormalizer(catalog)

    normalized, errors = normalizer.normalize_and_validate(
        {
            "entities": [
                {
                    "name": "Conversation episode",
                    "entity_type": "episodes",
                    "properties": [],
                }
            ]
        },
        "2026-08-19",
        entity_schema=catalog.schema_for_generation(),
    )

    assert normalized["entities"] == []
    assert any("type 'episodes' was not selected" in error for error in errors)


def test_schema_selection_filters_static_and_dynamic_properties() -> None:
    catalog = EntitySchemaCatalog.from_data(
        [
            {
                "entity_type": "person",
                "static_property": {"name": {}, "birthday": {}},
                "dynamic_property": {
                    "default_property": {},
                    "occupation": {},
                    "employer": {},
                },
            }
        ]
    )

    selected = catalog.filter_selected(
        catalog.schema_for_generation(),
        [{"entity_type": "person", "relevant_properties": ["occupation"]}],
    )

    assert selected[0]["static_property"] == {}
    assert set(selected[0]["dynamic_property"]) == {"default_property", "occupation"}


@pytest.mark.parametrize(
    "selection",
    [
        {"entity_type": "person"},
        {"entity_type": "person", "relevant_properties": []},
        {"entity_type": "person", "relevant_properties": "all"},
    ],
)
def test_schema_selection_does_not_expand_missing_or_empty_properties(selection) -> None:
    catalog = _catalog()

    selected = catalog.filter_selected(catalog.schema_for_generation(), [selection])

    assert selected == []


def test_empty_schema_selection_skips_property_generation() -> None:
    llm = _SequenceResponseLLM([json.dumps({"selected_entities": []})])
    extractor = EntitySchemaExtractor(
        llm=llm,
        schema=_catalog(),
        enable_schema_selection=True,
    )

    units = extractor.extract([_source()])

    assert units == []
    assert llm.call_count == 1


def test_schema_selection_prompt_render_failure_falls_back_to_full_schema(monkeypatch) -> None:
    source = _source()
    llm = _SequenceResponseLLM([_property_response(source.id)])
    extractor = EntitySchemaExtractor(
        llm=llm,
        schema=_catalog(),
        enable_schema_selection=True,
    )
    monkeypatch.setattr(
        "jiuwen_memory.construction.extractor_impl.entity_schema_extractor."
        "_SCHEMA_SELECTION_SYSTEM_PROMPT",
        "{missing_template_field}",
    )

    units = extractor.extract([source])

    assert len(units) == 1
    assert units[0].system_metadata["schema_property_name"] == "occupation"
    assert llm.call_count == 1


def test_schema_json_parser_requires_one_root_object() -> None:
    assert _parse_json_object('```json\n{"entities": []}\n```')["entities"] == []
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_json_object('example: {"entities": []}')


def test_rendered_entity_generation_prompt_has_no_escaped_json_braces() -> None:
    source = _source()
    llm = _CapturingResponseLLM(_property_response(source.id))
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    units = extractor.extract([source])

    assert len(units) == 1
    assert llm.messages
    rendered_prompt = llm.messages[-1].content
    assert "{{" not in rendered_prompt


def test_partial_event_time_does_not_create_private_temporal_metadata() -> None:
    source = _source()
    response = json.dumps(
        {
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "Alice became an engineer in 2023-08",
                            "time": "2023-08",
                            "source_unit_ids": [source.id],
                        }
                    ],
                }
            ]
        }
    )
    extractor = EntitySchemaExtractor(
        llm=_ConstantResponseLLM(response),
        schema=_catalog(),
    )

    units = extractor.extract([source])

    assert len(units) == 1
    assert units[0].temporal.t_event is None
    assert not any(key.startswith("schema_event_time") for key in units[0].system_metadata)
    assert units[0].user_metadata == {"dataset": "test"}


def test_schema_property_does_not_inherit_previous_schema_state() -> None:
    source = _source()
    source.system_metadata = {
        "pipeline": "schema-regression",
        "extraction_mode": "schema",
        "schema_entity_id": "old-entity",
        "schema_property_name": "old-property",
        "property_merge_action": "update",
    }
    response = json.dumps(
        {
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "Alice is an engineer",
                            "time": "",
                            "source_unit_ids": [source.id],
                        }
                    ],
                }
            ]
        }
    )
    extractor = EntitySchemaExtractor(
        llm=_ConstantResponseLLM(response),
        schema=_catalog(),
    )

    unit = extractor.extract([source])[0]

    assert unit.system_metadata["pipeline"] == "schema-regression"
    assert unit.system_metadata["schema_property_name"] == "occupation"
    assert "schema_entity_id" not in unit.system_metadata
    assert "property_merge_action" not in unit.system_metadata


def test_each_property_becomes_one_unit_and_named_speaker_owns_it() -> None:
    response = json.dumps(
        {
            "entities": [
                {
                    "name": "User",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "User became an engineer in 2023-08",
                            "time": "2023-08",
                            "source_unit_ids": ["source-1"],
                        },
                        {
                            "property_name": "default_property",
                            "value": "User enjoys solving backend problems",
                            "time": "",
                            "source_unit_ids": ["source-1"],
                        },
                    ],
                }
            ]
        }
    )
    extractor = EntitySchemaExtractor(
        llm=_ConstantResponseLLM(response),
        schema=_catalog(),
    )

    units = extractor.extract([_source()])

    assert len(units) == 2
    assert {unit.system_metadata["schema_property_name"] for unit in units} == {
        "occupation",
        "default_property",
    }
    for unit in units:
        assert unit.entities == []
        assert unit.system_metadata["schema_entity_name"] == "Alice"
        assert unit.system_metadata["schema_entity_type"] == "person"
        assert unit.system_metadata["schema_name"] == "inline-schema"
        assert unit.provenance == ["source-1"]
        assert unit.source_ref == "source-1"


def test_invalid_source_binding_is_corrected_inside_validation_retries() -> None:
    source = _source()
    llm = _SequenceResponseLLM(
        [
            _property_response("unknown-source"),
            _property_response(source.id),
        ]
    )
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    units = extractor.extract([source])

    assert len(units) == 1
    assert units[0].provenance == [source.id]
    assert llm.call_count == 2


def test_valid_properties_survive_invalid_siblings_after_retries() -> None:
    source = _source()
    response = json.dumps(
        {
            "entities": [
                {
                    "name": "Alice",
                    "entity_type": "person",
                    "properties": [
                        {
                            "property_name": "occupation",
                            "value": "Alice is an engineer",
                            "time": "",
                            "source_unit_ids": [source.id],
                        },
                        {
                            "property_name": "birthday",
                            "value": "Alice was born on 1990-01-01",
                            "time": "1990-01-01",
                            "source_unit_ids": ["unknown-source"],
                        },
                    ],
                }
            ]
        }
    )
    llm = _SequenceResponseLLM([response, response, response])
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    units = extractor.extract([source])

    assert len(units) == 1
    assert units[0].system_metadata["schema_property_name"] == "occupation"
    assert llm.call_count == 3


def test_unsupported_entity_type_is_corrected_instead_of_retyped() -> None:
    source = _source()
    llm = _SequenceResponseLLM(
        [
            _property_response(source.id, entity_type="unsupported-person"),
            _property_response(source.id),
        ]
    )
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    units = extractor.extract([source])

    assert len(units) == 1
    assert units[0].system_metadata["schema_entity_type"] == "person"
    assert llm.call_count == 2


class _Storage:
    def __init__(self) -> None:
        self.units: dict[str, MemoryUnit] = {}
        self.kv = SimpleNamespace()

    @staticmethod
    def has_graph() -> bool:
        return False

    def get(self, _scope, unit_ids):
        return [self.units[unit_id] for unit_id in unit_ids if unit_id in self.units]

    def add(self, _scope, units):
        for unit in units:
            self.units[unit.id] = unit

    def update(self, _scope, units):
        for unit in units:
            self.units[unit.id] = unit


class _Index:
    def __init__(self, storage: _Storage) -> None:
        self._storage = storage
        self.ids: list[str] = []
        self.updated_ids: list[str] = []
        self.update_modes: list[IndexWriteMode] = []

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        self.ids.extend(unit.id for unit in units)
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        for unit in units:
            self._storage.add(unit.scope, [unit])

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL):
        self.updated_ids.extend(unit.id for unit in units)
        self.update_modes.append(mode)
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        for unit in units:
            self._storage.update(unit.scope, [unit])


class _SchemaEvolverHarness(SchemaOrchestratingEvolver):
    def _persist_and_maintain_messages(self, _units):
        return []

    def _maybe_collect_extract_context(self, _units, _recent):
        return None

    @staticmethod
    def _is_procedural(_units):
        return False

    @staticmethod
    def _annotate_layers(_units):
        return None


def _evolver(extractor) -> tuple[SchemaOrchestratingEvolver, _Storage, _Index]:
    storage = _Storage()
    index = _Index(storage)
    evolver = _SchemaEvolverHarness(
        extractor=extractor,
        abstractor=SimpleNamespace(),
        associator=SimpleNamespace(),
        index_builder=index,
        storage=storage,
        message_store=SimpleNamespace(),
        dedup=SimpleNamespace(),
        llm=SimpleNamespace(),
    )
    return evolver, storage, index


def test_schema_failure_keeps_searchable_source_memory() -> None:
    class FailingExtractor:
        @staticmethod
        def extract(_units, *, context=None):
            del context
            raise RuntimeError("three validation attempts exhausted")

    evolver, storage, index = _evolver(FailingExtractor())
    source = _source()
    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id]
    assert index.ids == [source.id]
    assert storage.units[source.id].system_metadata["schema_source_evidence"] is True
    assert storage.units[source.id].user_metadata == {"dataset": "test"}
    assert source.system_metadata == {}
    assert "RuntimeError" in evolver.last_schema_error


def test_invalid_sources_after_all_retries_keep_only_source_memory() -> None:
    source = _source()
    response = _property_response("unknown-source")
    llm = _SequenceResponseLLM([response, response, response])
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())
    evolver, storage, index = _evolver(extractor)

    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id]
    assert set(storage.units) == {source.id}
    assert index.ids == [source.id]
    assert llm.call_count == 3
    assert "invalid source_unit_ids" in evolver.last_schema_error


def test_unsupported_entity_type_after_all_retries_is_not_persisted() -> None:
    source = _source()
    response = _property_response(source.id, entity_type="unsupported-person")
    llm = _SequenceResponseLLM([response, response, response])
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())
    evolver, storage, index = _evolver(extractor)

    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id]
    assert set(storage.units) == {source.id}
    assert index.ids == [source.id]
    assert llm.call_count == 3
    assert "unsupported type" in evolver.last_schema_error


def test_unsupported_entity_type_without_source_first_raises() -> None:
    source = _source()
    response = _property_response(source.id, entity_type="unsupported-person")
    llm = _SequenceResponseLLM([response, response, response])
    extractor = EntitySchemaExtractor(llm=llm, schema=_catalog())

    with pytest.raises(InvalidSchemaExtractionError, match="unsupported type"):
        extractor.extract([source])


def test_schema_properties_are_added_without_ordinary_dedup() -> None:
    source = _source()
    source.entities = ["ExistingEntity"]
    property_unit = MemoryUnit(
        id="property-1",
        scope=source.scope,
        tier=MemoryTier.CORE,
        segments=[Segment(content="Alice is an engineer")],
        provenance=[source.id],
        entities=[],
        system_metadata={
            "extraction_mode": "schema",
            "schema_entity_name": "Alice",
            "schema_property_name": "occupation",
        },
    )

    class SuccessfulExtractor:
        @staticmethod
        def extract(_units, *, context=None):
            del context
            return [property_unit]

    evolver, storage, index = _evolver(SuccessfulExtractor())
    result = evolver.evolve([source], EvolveMode.EXTRACT)

    assert result.created_ids == [source.id, property_unit.id]
    assert set(storage.units) == {source.id, property_unit.id}
    assert index.ids == [source.id, property_unit.id]
    assert index.updated_ids == [source.id]
    assert index.update_modes == [IndexWriteMode.ALL]
    assert storage.units[source.id].entities == ["ExistingEntity", "Alice", "occupation"]
    assert storage.units[property_unit.id].entities == []


class _MemoryEntityStore(EntityStore):
    def __init__(self) -> None:
        self.records: dict[str, EntityRecord] = {}

    def store_type(self):
        return None

    def health(self) -> None:
        return None

    def ensure_index(self) -> None:
        return None

    def find_by_entity_text_hash(
        self,
        _space_id: str,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        limit: int = 500,
    ) -> list[EntityRecord]:
        del filters
        hashes = set(entity_text_hashes)
        return [record for record in self.records.values() if record.entity_text_hash in hashes][
            :limit
        ]

    def find_by_linked_memory_id(
        self,
        _space_id: str,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
    ) -> list[EntityRecord]:
        del filters
        return [record for record in self.records.values() if memory_id in record.linked_memory_ids]

    def execute_operations(
        self,
        _space_id: str,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        successful: list[str] = []
        for operation in operations:
            if operation.type is EntityOpType.INSERT and operation.record is not None:
                self.records[operation.record.id] = operation.record
                successful.append(operation.record.id)
            elif operation.type is EntityOpType.LINK and operation.record_id is not None:
                existing = self.records[operation.record_id]
                linked = tuple(
                    dict.fromkeys([*existing.linked_memory_ids, *operation.link_memory_ids])
                )
                self.records[existing.id] = EntityRecord(
                    id=existing.id,
                    space_id=existing.space_id,
                    entity_text=existing.entity_text,
                    entity_type=existing.entity_type,
                    linked_memory_ids=linked,
                    filters=existing.filters,
                    entity_text_hash=existing.entity_text_hash,
                )
                successful.append(existing.id)
        return EntityBatchResult(successful_ids=successful, failed_ids=[])


def test_source_entities_feed_the_official_entity_reverse_index() -> None:
    store = _MemoryEntityStore()
    linker = EntityLinkService(entity_store=store)
    unit = MemoryUnit(
        id="source-1",
        scope=Scope(org="org", user="alice"),
        tier=MemoryTier.CORE,
        segments=[Segment(content="Alice is an engineer")],
        entities=["Alice", "occupation"],
    )

    result = linker.link_memories([unit])

    assert result.inserted_count == 2
    records_by_text = {record.entity_text: record for record in store.records.values()}
    assert set(records_by_text) == {"Alice", "occupation"}
    assert records_by_text["Alice"].linked_memory_ids == (unit.id,)
    assert records_by_text["occupation"].linked_memory_ids == (unit.id,)


def test_schema_is_disabled_in_default_assembly_config() -> None:
    defaults = default_config_dict()
    assert defaults["globals"]["schema_enabled"] is False
    assert defaults["extractor"]["default"]["target"] == "dynamic_llm"
    assert defaults["evolver"]["default"]["target"] == "orchestrating"
    assert defaults["storage"]["default"]["target"] == "composite"


def test_schema_target_requires_enabled_assembly_switch() -> None:
    config = Config.from_dict(
        {
            "extractor": {
                "default": {
                    "target": "entity_schema",
                    "params": {"schema_path": "examples/persona.json"},
                }
            }
        }
    )

    with pytest.raises(ValidationError, match="schema_enabled=true"):
        build_kernel(config=config)


class _StaticSchemaLLM(LLM):
    def __init__(self, response: str) -> None:
        self.response = response

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        del options
        match = re.search(r"unit_id=([^\]\r\n]+)", messages[-1].content)
        source_id = match.group(1).strip() if match else ""
        return self.response.replace("__SOURCE_ID__", source_id)


def _build_static_schema_llm(config) -> _StaticSchemaLLM:
    return _StaticSchemaLLM(config.get("response"))


def _noop_setup_logging(config) -> None:
    del config


def _raise_unexpected_schema_registration() -> None:
    raise AssertionError("default assembly must not register Schema constructors")


def test_default_assembly_does_not_register_schema(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwen_memory.api.memory_api_impl.assembly.setup_logging",
        _noop_setup_logging,
    )
    monkeypatch.setattr(
        "jiuwen_memory.construction.schema_bootstrap.register_schema_constructors",
        _raise_unexpected_schema_registration,
    )

    kernel = build_kernel()

    assert kernel.api is not None


def test_schema_enabled_assembly_runs_source_first_property_extraction(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwen_memory.api.memory_api_impl.assembly.setup_logging",
        _noop_setup_logging,
    )
    target = "schema_extension_test_static"
    if target not in LlmProducer.known():
        LlmProducer.register(target)(_build_static_schema_llm)
    response = json.dumps(
        {
            "entities": [
                {
                    "name": "User",
                    "entity_type": "user",
                    "properties": [
                        {
                            "property_name": "position_event",
                            "value": "On 2023-08-03, Alice became a software engineer",
                            "time": "2023-08-03",
                            "source_unit_ids": ["__SOURCE_ID__"],
                        }
                    ],
                }
            ]
        }
    )
    schema_path = Path(__file__).resolve().parents[3] / "examples" / "persona.json"
    kernel = build_kernel(
        config=Config.from_dict(
            {
                "globals": {
                    "schema_enabled": True,
                    "vector_enabled": True,
                    "graph_enabled": False,
                    "rerank_enabled": False,
                },
                "llm": {
                    "default": {
                        "target": target,
                        "params": {"response": response},
                    }
                },
                "extractor": {
                    "default": {
                        "target": "entity_schema",
                        "params": {
                            "schema_path": str(schema_path),
                            "llm": "default",
                            "schema_validation_attempts": 1,
                        },
                    }
                },
                "evolver": {
                    "default": {
                        "target": "schema_orchestrating",
                        "params": {
                            "extractor": "default",
                            "llm": "default",
                        },
                    }
                },
            }
        )
    )
    scope = Scope(org="schema-e2e", user="alice")

    created = kernel.api.add(
        "speaker=Alice: On 2023-08-03, I became a software engineer.",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"infer": True},
    )

    assert len(created) == 2
    source = next(unit for unit in created if unit.system_metadata.get("memory_role"))
    property_unit = next(
        unit for unit in created if unit.system_metadata.get("extraction_mode") == "schema"
    )
    assert source.system_metadata["schema_source_evidence"] is True
    assert property_unit.content == "On 2023-08-03, Alice became a software engineer"
    assert source.entities == ["Alice", "position_event"]
    assert property_unit.entities == []
    assert property_unit.system_metadata["schema_entity_name"] == "Alice"
    assert property_unit.system_metadata["schema_entity_type"] == "user"
    assert property_unit.system_metadata["schema_property_name"] == "position_event"
    assert property_unit.temporal.t_event is not None
    assert "schema_entity_id" not in property_unit.system_metadata
    assert "schema_entity_key" not in property_unit.system_metadata
