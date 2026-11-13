# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 D10：图端口的 shared / split 分片包装层。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import RelocationInProgressError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.graph_impl.shard_routed_graph import ShardRoutedGraphStore
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver, ShardLocation
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
SHARDS = ("working", "profile")


class DictLocator:
    def __init__(self, mapping: dict[str, str], moving: set[str] | None = None) -> None:
        self.mapping = mapping
        self.moving = frozenset(moving or ())

    def locate(self, scope: Scope, unit_ids: list[str]) -> ShardLocation:
        found = {uid: self.mapping[uid] for uid in unit_ids if uid in self.mapping}
        return ShardLocation(
            found=found,
            unlocated=frozenset(set(unit_ids) - set(found)),
            moving=frozenset(uid for uid in unit_ids if uid in self.moving),
        )


def resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(shards=list(SHARDS), rules=[], default_shard="working")


def node(uid: str, content: str = "alpha") -> Node:
    return Node(uid, "memory", {"content": content})


def build_shared(mapping: dict[str, str]) -> ShardRoutedGraphStore:
    return ShardRoutedGraphStore(
        resolver=resolver(),
        locator=DictLocator(mapping),
        mode="shared",
        inner=InMemoryGraphStore(),
    )


def build_split(mapping: dict[str, str]) -> ShardRoutedGraphStore:
    return ShardRoutedGraphStore(
        resolver=resolver(),
        locator=DictLocator(mapping),
        mode="split",
        stores={name: InMemoryGraphStore() for name in SHARDS},
    )


# ---------------------------------------------------------------------------
# shared：标签 + 截断前过滤
# ---------------------------------------------------------------------------


def test_shared_insert_tags_nodes_and_rejects_missing_placement() -> None:
    graph = build_shared({"u1": "working", "u2": "profile"})
    graph.insert(SCOPE, nodes=[node("u1"), node("u2")])

    tagged = {}
    for item in graph.get(SCOPE, ["u1", "u2"]):
        tagged[item.id] = item.properties.get("storage_shard")
    assert tagged == {"u1": "working", "u2": "profile"}

    with pytest.raises(ValidationError, match="active shard placement"):
        graph.insert(SCOPE, nodes=[node("deleted")])


def test_shared_seed_and_search_filter_before_limit() -> None:
    # p1/p2 在 profile，w 在 working：从 p1 出发必须经过 w 才能到 p2。
    graph = build_shared({"p1": "profile", "p2": "profile", "w": "working"})
    graph.insert(
        SCOPE,
        nodes=[node("p1"), node("p2"), node("w")],
        edges=[
            Edge("e1", "p1", "w", "related", {}),
            Edge("e2", "w", "p2", "related", {}),
        ],
    )

    seeds = graph.seed_ids(SCOPE, {"alpha"}, extensions={"shards": ["profile"]})
    assert seeds == ["p1", "p2"]

    result = graph.search(
        SCOPE,
        GraphQuery(start_id="p1", depth=2, limit=10, extensions={"shards": ["profile"]}),
    )
    # 经过 working 的 w，但只返回 profile 的 p2（且 p1 是起点不重复返回）。
    assert [item.id for item in result] == ["p2"]

    assert graph.search(
        SCOPE,
        GraphQuery(start_id="w", limit=10, extensions={"shards": ["profile"]}),
    ) == []


def test_shared_relocation_only_changes_labels() -> None:
    graph = build_shared({"u1": "working"})
    graph.insert(SCOPE, nodes=[node("u1")])

    graph.relocate_unit(SCOPE, "u1", "working", "profile")
    moved = graph.get(SCOPE, ["u1"])[0]
    assert moved.properties["storage_shard"] == "working"
    assert moved.properties["storage_shard_next"] == "profile"

    graph.cleanup_unit(SCOPE, "u1", "working")
    cleaned = graph.get(SCOPE, ["u1"])[0]
    assert cleaned.properties["storage_shard"] == "profile"
    assert "storage_shard_next" not in cleaned.properties


def test_shared_cleanup_target_rolls_back_transition_label() -> None:
    graph = build_shared({"u1": "working"})
    graph.insert(SCOPE, nodes=[node("u1")])
    graph.relocate_unit(SCOPE, "u1", "working", "profile")

    graph.cleanup_unit(SCOPE, "u1", "profile")
    rolled_back = graph.get(SCOPE, ["u1"])[0]
    assert rolled_back.properties["storage_shard"] == "working"
    assert "storage_shard_next" not in rolled_back.properties


# ---------------------------------------------------------------------------
# split：按归属拆图，跨分片边丢弃
# ---------------------------------------------------------------------------


def test_split_drops_cross_shard_edge_and_routes_nodes() -> None:
    graph = build_split({"u1": "working", "u2": "profile", "u3": "working"})
    graph.insert(
        SCOPE,
        nodes=[node("u1"), node("u2"), node("u3")],
        edges=[
            Edge("e1", "u1", "u3", "related", {}),
            Edge("e2", "u1", "u2", "related", {}),
        ],
    )

    assert [item.id for item in graph.get(SCOPE, ["u1", "u2", "u3"])] == [
        "u1",
        "u2",
        "u3",
    ]
    # 同分片边保留：从 u1 出发到 u3
    found = graph.search(
        SCOPE,
        GraphQuery(start_id="u1", depth=1, limit=10, extensions={"shards": ["working"]}),
    )
    assert [item.id for item in found] == ["u3"]
    # 跨分片边 e2 被丢弃：不会到 u2
    assert all(
        item.id != "u2"
        for item in graph.search(SCOPE, GraphQuery(start_id="u1", depth=2, limit=10))
    )
    # 选中 profile 时 u1 不在该分片，起点不可见
    assert graph.search(
        SCOPE,
        GraphQuery(start_id="u1", extensions={"shards": ["profile"]}),
    ) == []


class _RecordingGraphStore(InMemoryGraphStore):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def delete(self, scope, node_ids=None, edge_ids=None) -> None:
        self.deleted.extend(node_ids or [])
        super().delete(scope, node_ids=node_ids, edge_ids=edge_ids)


def test_split_relocation_copies_node_then_cleanup_source() -> None:
    stores = {name: _RecordingGraphStore() for name in SHARDS}
    graph = ShardRoutedGraphStore(
        resolver=resolver(),
        locator=DictLocator({"u1": "working"}),
        mode="split",
        stores=stores,
    )
    graph.insert(SCOPE, nodes=[node("u1")])

    graph.relocate_unit(SCOPE, "u1", "working", "profile")
    assert graph.get(SCOPE, ["u1"])[0].id == "u1"

    graph.cleanup_unit(SCOPE, "u1", "working")
    assert graph.get(SCOPE, ["u1"])[0].id == "u1"
    assert stores["working"].deleted == ["u1"]
    assert stores["profile"].get(SCOPE, ["u1"])[0].id == "u1"


def test_missing_placement_rejected_for_split() -> None:
    graph = build_split({})
    with pytest.raises(ValidationError, match="active shard placement"):
        graph.insert(SCOPE, nodes=[node("ghost")])


class _GraphManager:
    def __init__(self, graph) -> None:
        self._graph = graph

    @staticmethod
    def has_graph(name: str = "default") -> bool:
        return True

    def graph(self, name: str = "default"):
        return self._graph


def test_graph_recaller_supports_shards_and_filters_seeds() -> None:
    from jiuwen_memory.common.type_def import ParsedQuery
    from jiuwen_memory.storage.domain_store_impl.graph_recaller import GraphRecaller

    graph = build_shared({"seed": "profile", "u1": "working", "u2": "profile"})
    graph.insert(
        SCOPE,
        nodes=[node("seed", "alpha"), node("u1"), node("u2")],
        edges=[
            Edge("e1", "seed", "u1", "related", {}),
            Edge("e2", "seed", "u2", "related", {}),
        ],
    )
    recaller = GraphRecaller(_GraphManager(graph))
    assert recaller.supports_shards() is True

    result = recaller.recall(
        SCOPE,
        ParsedQuery(raw="alpha", keywords=["alpha"], extensions={"shards": ["profile"]}),
        top_k=10,
    )
    ids = [item.unit_id for item in result]
    assert "u2" in ids
    assert "u1" not in ids  # 非选中分片的私有节点被过滤


def test_graph_recaller_rejects_shards_for_plain_backend() -> None:
    from jiuwen_memory.storage.domain_store_impl.graph_recaller import GraphRecaller

    recaller = GraphRecaller(_GraphManager(InMemoryGraphStore()))
    assert recaller.supports_shards() is False


def test_shared_write_during_moving_is_rejected() -> None:
    graph = ShardRoutedGraphStore(
        resolver=resolver(),
        locator=DictLocator({"u1": "working"}, moving={"u1"}),
        mode="shared",
        inner=InMemoryGraphStore(),
    )
    with pytest.raises(RelocationInProgressError):
        graph.insert(SCOPE, nodes=[node("u1")])


def test_split_write_during_moving_is_rejected() -> None:
    graph = ShardRoutedGraphStore(
        resolver=resolver(),
        locator=DictLocator({"u1": "working"}, moving={"u1"}),
        mode="split",
        stores={name: InMemoryGraphStore() for name in SHARDS},
    )
    with pytest.raises(RelocationInProgressError):
        graph.update(SCOPE, nodes=[node("u1")])


def test_split_delete_during_moving_is_rejected() -> None:
    graph = ShardRoutedGraphStore(
        resolver=resolver(),
        locator=DictLocator({"u1": "working"}, moving={"u1"}),
        mode="split",
        stores={name: InMemoryGraphStore() for name in SHARDS},
    )
    with pytest.raises(RelocationInProgressError):
        graph.delete(SCOPE, node_ids=["u1"])


class _SameResourceGraph(InMemoryGraphStore):
    def resource_descriptor(self) -> str | None:
        return "nano-graphrag://shared"


def test_split_rejects_duplicate_physical_graph_resource() -> None:
    with pytest.raises(ValidationError, match="same physical resource"):
        ShardRoutedGraphStore(
            resolver=resolver(),
            locator=DictLocator({}),
            mode="split",
            stores={
                "working": _SameResourceGraph(),
                "profile": _SameResourceGraph(),
            },
        )
