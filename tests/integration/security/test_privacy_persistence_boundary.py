"""Trusted processing sees originals; persisted truth and search projections are redacted."""

import copy

import pytest

from jiuwen_memory.api import assemble_server_runtime
from jiuwen_memory.common.type_def import ChunkVector, ContentLayers, MemoryUnit, Segment
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.classifier_impl.llm_classifier import LLMClassifier
from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor
from jiuwen_memory.construction.index_builder_impl.unified_index_builder import UnifiedIndexBuilder
from jiuwen_memory.construction.index_builder_impl.vector_index_builder import VectorIndexBuilder
from jiuwen_memory.construction.layer_annotator_impl.keyword_layer_annotator import (
    KeywordLayerAnnotator,
)
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.integration.security.test_memory_privacy import SCOPE, TEXT, config, security

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("engine", ["in_memory", "cloud"])
@pytest.mark.parametrize("builder", ["hybrid", "unified"])
@pytest.mark.parametrize("infer", [False, True])
def test_processing_uses_originals_and_indexing_uses_redacted_units(
    tmp_path, monkeypatch, engine, builder, infer
):
    processed, indexed, messages = [], [], []
    settings = config(tmp_path)
    settings["globals"]["vector_enabled"] = True
    settings["engine"] = {
        "default": {"target": engine, "params": {"index_builder": "default", "evolver": "default"}}
    }
    settings["constructor"] = {"default": {"target": builder}}
    settings["extractor"] = {"default": "keyword"}
    settings["dedup"] = {"default": "keyword"}
    settings["layer_annotator"] = {"default": "keyword"}
    settings["evolver"] = {
        "default": {
            "target": "orchestrating",
            "params": {"layer_annotator": "default", "index_builder": "default"},
        }
    }

    def classify(self, units):
        for unit in units:
            assert unit.content == TEXT
            unit.vectors = [ChunkVector("old", 0, [999.0])]
            processed.append("classify")
        return units

    def extract(self, units, context=None):
        assert units[0].content == TEXT
        assert messages and "private-demo-value" not in messages[0].content
        processed.append("extract")
        derived = copy.deepcopy(units[0])
        derived.id = "derived-" + units[0].id
        derived.provenance = [units[0].id]
        derived.segments = [Segment("抽取原文：api_key=derived-secret")]
        derived.vectors = [ChunkVector("old", 0, [999.0])]
        return [derived]

    def annotate(self, units):
        assert "derived-secret" in units[0].content
        processed.append("annotate")
        units[0].layers = ContentLayers(l0="password=generated-summary-secret")

    insert = InMemoryKVStore.insert

    def write(self, scope, key, value, ttl=0.0):
        if key.startswith(("/memory/", "/messages/")):
            unit = loads(value)
            assert "private-demo-value" not in unit.content
            assert "derived-secret" not in unit.content
            assert "generated-summary-secret" not in unit.layers.l0
            if key.startswith("/messages/"):
                messages.append(unit)
        return insert(self, scope, key, value, ttl)

    engine_builder = VectorIndexBuilder if builder == "hybrid" else UnifiedIndexBuilder
    build = engine_builder.build

    def index(self, units, **kwargs):
        for unit in units:
            assert "private-demo-value" not in unit.content
            assert "derived-secret" not in unit.content
            assert "generated-summary-secret" not in unit.layers.l0
            assert not unit.vectors, "Original processing vectors must be invalidated"
            indexed.append(unit.id)
        return build(self, units, **kwargs)

    monkeypatch.setattr(LLMClassifier, "classify", classify)
    monkeypatch.setattr(KeywordExtractor, "extract", extract)
    monkeypatch.setattr(KeywordLayerAnnotator, "annotate", annotate)
    monkeypatch.setattr(InMemoryKVStore, "insert", write)
    monkeypatch.setattr(engine_builder, "build", index)
    runtime = assemble_server_runtime(config=settings)
    try:
        units = runtime.api.add(TEXT, SCOPE, system_metadata={"infer": infer}, security=security())
        assert indexed and units
        assert processed == (["extract", "annotate"] if infer else ["classify"])
        for unit in units:
            assert "private-demo-value" not in str(unit) and "derived-secret" not in str(unit)
            assert runtime.api.get(unit.id, SCOPE, security=security()).content == unit.content
    finally:
        runtime.close()


def test_input_splitting_rejects_oversized_sensitive_span_without_staging(privacy, privacy_state):
    from jiuwen_memory.api import ValidationError

    with pytest.raises(ValidationError, match="text limit"):
        privacy.split_input("api_key=" + "a" * 200, 100)
    assert privacy_state.count("fragments") == 0


def test_schema_entity_registry_protects_projection_before_storage_and_embedding(
    privacy, monkeypatch
):
    from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
    from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
        WhitespaceTokenizer,
    )
    from jiuwen_memory.construction.evolver_impl.schema_entity_registry import (
        SchemaEntityRegistry,
        schema_entity_key,
    )
    from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
    from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

    tokenizer = WhitespaceTokenizer()
    embedder = HashingEmbedder(tokenizer)
    embedded = []
    embed = embedder.embed

    def project(texts):
        assert "13800138000" not in str(texts) and "entity-secret" not in str(texts)
        embedded.extend(texts)
        return embed(texts)

    monkeypatch.setattr(embedder, "embed", project)
    kv = InMemoryKVStore()
    registry = SchemaEntityRegistry(
        kv,
        vector_store=InMemoryVectorStore(),
        fulltext_store=InMemoryFulltextStore(tokenizer),
        embedder=embedder,
        privacy=privacy,
    )
    observation = MemoryUnit(
        id="property",
        scope=SCOPE,
        segments=[Segment("property")],
        system_metadata={
            "schema_entity_id": "entity",
            "schema_entity_name": "13800138000",
            "schema_entity_description": "api_key=entity-secret",
        },
    )
    record = registry.sync([observation])[0]
    assert embedded and "entity-secret" not in str(record)
    assert "13800138000" not in str(record)
    stored = kv.get(SCOPE, schema_entity_key(record.id))
    assert b"entity-secret" not in stored and b"13800138000" not in stored
    assert observation.system_metadata["schema_entity_name"] == "13800138000"
