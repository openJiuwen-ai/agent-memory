# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""nano-graphrag 后端的图分片过滤：可经过其它分片，返回前过滤。"""

from __future__ import annotations

import json
import tempfile

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.graph_impl.nano_graphrag_graph import NanoGraphRAGGraphStore
from jiuwen_memory.storage.types import GraphQuery

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


class _NodeView:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __call__(self, data: bool = False):
        return list(self._data.items()) if data else list(self._data)

    def __getitem__(self, node_id: str):
        return self._data[node_id]


class _FakeGraph:
    def __init__(self) -> None:
        def payload(shard: str) -> dict:
            return {
                "label": "memory",
                "properties": json.dumps({"content": "x", "storage_shard": shard}),
            }

        self.data = {
            "p1": payload("profile"),
            "w": payload("working"),
            "p2": payload("profile"),
        }
        self.edges = {
            ("p1", "w"): {"id": "e1", "relation": "r"},
            ("w", "p2"): {"id": "e2", "relation": "r"},
        }
        self.nodes = _NodeView(self.data)

    def has_node(self, node_id: str) -> bool:
        return node_id in self.data

    def neighbors(self, node_id: str):
        return [
            b if a == node_id else a
            for a, b in self.edges
            if a == node_id or b == node_id
        ]

    def get_edge_data(self, source: str, target: str):
        return self.edges.get((source, target)) or self.edges.get((target, source))

    def __getitem__(self, node_id: str):
        return self.data[node_id]


class _Storage:
    def __init__(self, graph: _FakeGraph) -> None:
        self._graph = graph


class _TestNanoStore(NanoGraphRAGGraphStore):
    def __init__(self, graph: _FakeGraph) -> None:
        super().__init__(working_dir=tempfile.mkdtemp(prefix="nano-graph-test-"))
        self._storage = _Storage(graph)

    def _store(self, scope):
        return self._storage


def _store(graph: _FakeGraph):
    return _TestNanoStore(graph)


def test_nano_search_traverses_working_node_to_profile_result() -> None:
    result = _store(_FakeGraph()).search(
        SCOPE,
        GraphQuery(
            start_id="p1",
            depth=2,
            limit=1,
            extensions={"shards": ["profile"]},
        ),
    )
    assert [node.id for node in result] == ["p2"]
