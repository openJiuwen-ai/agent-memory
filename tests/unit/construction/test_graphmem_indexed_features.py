"""Graph inputs come from real scoped index records, without another embedding call."""

import hashlib
import json
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Segment
from jiuwen_memory.construction.associator_impl.graphmem_features import IndexedGraphVectors
from jiuwen_memory.construction.index_builder_impl._index_ops import layer_record_id
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from tests.unit.construction.graphmem_fixtures import HOME, unit

pytestmark = pytest.mark.unit


def indexed(units):
    kv, vector = InMemoryKVStore(), InMemoryVectorStore()
    for u in units:
        ids = [u.id + "-a", u.id + "-b"]
        kv.insert(u.scope, "/index/chunks/" + u.id, json.dumps(ids).encode())
        vector.insert(u.scope, [VectorRecord(uid, [1., 0.], {
            "unit_id": u.id, "indexed_text_sha256": hashlib.sha256(u.content.encode()).hexdigest(),
        }) for uid in ids])
    return IndexedGraphVectors(vector, None, kv), vector




def test_stale_projection_and_other_scope_are_not_used():
    u = unit("a", "original summary", entities=[])
    reader, _ = indexed([u])
    assert reader(u)
    assert reader(replace(u, scope=replace(HOME, user="another"))) == []
    u.segments = [Segment(content="updated summary")]
    assert reader(u) == []


def test_scene_l1_preference_still_checks_scope_and_exact_text_hash():
    u = unit("a", "body", entities=[])
    u.layers.l1 = "scene projection"
    body, _ = indexed([u])
    layer = InMemoryVectorStore()
    layer.insert(HOME, [VectorRecord(layer_record_id(u.id, "l1"), [0., 1.], {
        "unit_id": u.id, "indexed_text_sha256": hashlib.sha256(u.layers.l1.encode()).hexdigest(),
    })])
    reader = IndexedGraphVectors(body._vector, layer, body._kv, prefer_l1=True)
    assert reader(u) == [0., 1.]
    u.layers.l1 = "changed projection"
    assert reader(u) == [1., 0.]
    assert reader(replace(u, scope=replace(HOME, user="another"))) == []


def test_detailed_projection_falls_back_only_to_valid_same_scope_scene():
    u = unit("a", "detailed evidence", entities=[])
    u.layers.l1 = "scene overview"
    body, _ = indexed([u])
    layer = InMemoryVectorStore()
    layer.insert(HOME, [VectorRecord(layer_record_id(u.id, "l1"), [0., 1.], {
        "unit_id": u.id, "indexed_text_sha256": hashlib.sha256(u.layers.l1.encode()).hexdigest(),
    })])
    reader = IndexedGraphVectors(body._vector, layer, body._kv, prefer_l1=False)
    assert reader(u) == [1., 0.]
    u.segments = [Segment(content="changed body")]
    assert reader(u) == [0., 1.]
    assert reader(replace(u, scope=replace(HOME, user="another"))) == []
    u.layers.l1 = "changed scene"
    assert reader(u) == []


def test_detail_metadata_batch_preserves_vectors_and_falls_back_for_legacy_keys(monkeypatch):
    nodes = [unit(name, text, entities=[]) for name, text in (
        ("a", "cycle commute"), ("b", "bicycle repairs"), ("c", "alternate route"))]
    reader, _ = indexed(nodes)
    calls = []
    original = reader._kv.mget

    def record(scope, keys):
        calls.append((scope, list(keys)))
        return original(scope, keys)

    monkeypatch.setattr(reader._kv, "mget", record)
    assert reader.read_many(nodes) == {node.id: reader(node) for node in nodes}
    assert calls == [(HOME, ["/index/chunks/a", "/index/chunks/b", "/index/chunks/c"])]
    reader._kv.delete(HOME, "/index/chunks/b")
    assert reader.read_many(nodes) == {"a": [1., 0.], "b": [], "c": [1., 0.]}
    with pytest.raises(ValidationError, match="one exact Scope"):
        reader.read_many([nodes[0], replace(nodes[1], scope=replace(HOME, user="another"))])
