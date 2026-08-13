from __future__ import annotations

from io import BytesIO

import pytest

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.fs_impl.in_memory_fs_store import InMemoryFSStore
from jiuwen_memory.storage._support import scope_dims, scope_segments
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.fusion_impl.in_memory_fusion_store import InMemoryFusionStore
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.types import (
    Document,
    Edge,
    FusionQuery,
    FusionRecord,
    GraphQuery,
    Node,
    TextQuery,
    VectorQuery,
    VectorRecord,
)
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

pytestmark = pytest.mark.unit


_SPACE_A = Scope(org="acme", space="product", user="alice")
_SPACE_B = Scope(org="acme", space="coding", user="alice")


def _scope_tuple(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def test_scope_helpers_treat_space_as_fixed_partition_under_org() -> None:
    assert scope_dims(Scope(org="acme", user="alice")) == [
        ("org", "acme"),
        ("space", ""),
        ("user", "alice"),
    ]
    assert scope_segments(Scope(org="acme", user="alice")) == [
        "acme",
        "_",
        "alice",
        "_",
        "_",
    ]


def test_in_memory_kv_isolates_same_key_by_space() -> None:
    store = InMemoryKVStore()

    store.insert(_SPACE_A, "same", b"a")
    store.insert(_SPACE_B, "same", b"b")

    assert store.get(_SPACE_A, "same") == b"a"
    assert store.get(_SPACE_B, "same") == b"b"
    assert {_scope_tuple(scope) for scope in store.scopes()} == {
        _scope_tuple(_SPACE_A),
        _scope_tuple(_SPACE_B),
    }


def test_sqlite_kv_isolates_same_key_by_space(tmp_path) -> None:
    store = SQLiteKVStore(str(tmp_path / "kv.sqlite3"))

    store.insert(_SPACE_A, "same", b"a")
    store.insert(_SPACE_B, "same", b"b")

    assert store.get(_SPACE_A, "same") == b"a"
    assert store.get(_SPACE_B, "same") == b"b"
    assert {_scope_tuple(scope) for scope in store.scopes()} == {
        _scope_tuple(_SPACE_A),
        _scope_tuple(_SPACE_B),
    }


def test_vector_store_isolates_same_id_by_space() -> None:
    store = InMemoryVectorStore()

    store.insert(_SPACE_A, [VectorRecord(id="same", vector=[1.0, 0.0])])
    store.insert(_SPACE_B, [VectorRecord(id="same", vector=[0.0, 1.0])])

    assert store.get(_SPACE_A, ["same"])[0].vector == [1.0, 0.0]
    assert store.get(_SPACE_B, ["same"])[0].vector == [0.0, 1.0]
    assert [hit.id for hit in store.search(_SPACE_A, VectorQuery(vector=[1.0, 0.0]))] == [
        "same"
    ]


def test_fulltext_store_isolates_same_id_by_space() -> None:
    store = InMemoryFulltextStore(WhitespaceTokenizer())

    store.insert(_SPACE_A, [Document(id="same", text="alpha")])
    store.insert(_SPACE_B, [Document(id="same", text="beta")])

    assert store.get(_SPACE_A, ["same"])[0].text == "alpha"
    assert store.get(_SPACE_B, ["same"])[0].text == "beta"
    assert store.search(_SPACE_A, TextQuery(text="beta")) == []


def test_graph_store_isolates_same_node_by_space() -> None:
    store = InMemoryGraphStore()

    store.insert(
        _SPACE_A,
        nodes=[Node(id="root"), Node(id="neighbor-a")],
        edges=[Edge(id="edge", source="root", target="neighbor-a")],
    )
    store.insert(
        _SPACE_B,
        nodes=[Node(id="root"), Node(id="neighbor-b")],
        edges=[Edge(id="edge", source="root", target="neighbor-b")],
    )

    assert [node.id for node in store.search(_SPACE_A, GraphQuery(start_id="root"))] == [
        "neighbor-a"
    ]
    assert [node.id for node in store.search(_SPACE_B, GraphQuery(start_id="root"))] == [
        "neighbor-b"
    ]


def test_fusion_store_isolates_same_id_by_space() -> None:
    store = InMemoryFusionStore(WhitespaceTokenizer())

    store.insert(_SPACE_A, [FusionRecord(id="same", vector=[1.0, 0.0], text="alpha")])
    store.insert(_SPACE_B, [FusionRecord(id="same", vector=[0.0, 1.0], text="beta")])

    assert store.get(_SPACE_A, ["same"])[0].text == "alpha"
    assert store.get(_SPACE_B, ["same"])[0].text == "beta"
    assert store.search(_SPACE_A, FusionQuery(vector=[0.0, 1.0], text="beta")) == []


def test_fs_store_isolates_refs_by_space() -> None:
    store = InMemoryFSStore()

    ref_a = store.insert(_SPACE_A, "same.bin", BytesIO(b"a"))
    ref_b = store.insert(_SPACE_B, "same.bin", BytesIO(b"b"))

    assert ref_a != ref_b
    assert store.get(_SPACE_A, ref_a).read() == b"a"
    assert store.get(_SPACE_B, ref_b).read() == b"b"
    with pytest.raises(NotFoundError):
        store.get(_SPACE_A, ref_b)
