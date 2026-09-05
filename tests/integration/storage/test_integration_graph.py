"""集成测试：nano-graphrag(NetworkXStorage) 背书的 GraphStore。

图存储落本地 GraphML 文件，无需外部服务，但需要 nano-graphrag 及其图存储依赖
（networkx/numpy/tiktoken）可加载；不可用时整组自动 skip。每个用例用独立
working_dir + scope 隔离，互不污染。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ConflictError, HealthCheckError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage import StoreType
from jiuwen_memory.storage.graph import GraphProducer
from jiuwen_memory.storage.graph_impl.nano_graphrag_graph import NanoGraphRAGGraphStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

SCOPE = Scope(org="itest", user="u1")


@pytest.fixture
def graph(tmp_path):
    store = NanoGraphRAGGraphStore(working_dir=str(tmp_path / "graph"))
    try:
        store.health()
    except HealthCheckError as exc:
        pytest.skip(f"nano-graphrag graph store unavailable: {exc}")
    return store


def _ids(nodes) -> set[str]:
    return {n.id for n in nodes}


# --------------------------------------------------------------- 工厂
def test_graph_registered_and_routes(tmp_path):
    assert "nano_graphrag" in GraphProducer.known()
    store = NanoGraphRAGGraphStore(working_dir=str(tmp_path))
    assert isinstance(store, NanoGraphRAGGraphStore)
    assert store.store_type() is StoreType.GRAPH


# --------------------------------------------------------------- CRUD
def test_graph_full_crud_lifecycle(graph):
    graph.insert(
        SCOPE,
        nodes=[
            Node(id="alice", label="PERSON", properties={"age": 30}),
            Node(id="bob", label="PERSON", properties={"city": "NYC"}),
        ],
        edges=[
            Edge(
                id="e1", source="alice", target="bob", relation="knows", properties={"since": 2020}
            )
        ],
    )
    got = {n.id: n for n in graph.get(SCOPE, ["alice", "bob"])}
    assert got["alice"].label == "PERSON" and got["alice"].properties == {"age": 30}
    assert got["bob"].properties == {"city": "NYC"}
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="alice", depth=1))) == {"bob"}

    graph.update(SCOPE, nodes=[Node(id="bob", label="PERSON", properties={"city": "LA"})])
    assert graph.get(SCOPE, ["bob"])[0].properties == {"city": "LA"}

    graph.delete(SCOPE, node_ids=["bob"])
    assert graph.get(SCOPE, ["bob"]) == []
    assert graph.search(SCOPE, GraphQuery(start_id="alice", depth=1)) == []  # 边随节点删除


def test_graph_node_conflict(graph):
    graph.insert(SCOPE, nodes=[Node(id="a", label="X")])
    with pytest.raises(ConflictError):
        graph.insert(SCOPE, nodes=[Node(id="a", label="Y")])
    assert graph.get(SCOPE, ["a"])[0].label == "X"  # 原值保留


def test_graph_edge_id_and_pair_conflict(graph):
    graph.insert(
        SCOPE,
        nodes=[Node(id="a"), Node(id="b"), Node(id="c")],
        edges=[Edge(id="e1", source="a", target="b", relation="r")],
    )
    # 同一对端点已有边 -> 冲突（nx.Graph 非多重图）
    with pytest.raises(ConflictError):
        graph.insert(SCOPE, edges=[Edge(id="e2", source="a", target="b", relation="r2")])
    # 边 id 重复 -> 冲突
    with pytest.raises(ConflictError):
        graph.insert(SCOPE, edges=[Edge(id="e1", source="a", target="c", relation="r3")])


def test_graph_update_missing_raises(graph):
    graph.insert(SCOPE, nodes=[Node(id="a")])
    with pytest.raises(NotFoundError):
        graph.update(SCOPE, nodes=[Node(id="ghost")])
    with pytest.raises(NotFoundError):
        graph.update(SCOPE, edges=[Edge(id="no-edge", source="a", target="a")])


def test_graph_update_edge_relation_reflected_in_search(graph):
    graph.insert(
        SCOPE,
        nodes=[Node(id="a"), Node(id="b")],
        edges=[Edge(id="e1", source="a", target="b", relation="knows")],
    )
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="a", relation="knows"))) == {"b"}
    graph.update(SCOPE, edges=[Edge(id="e1", source="a", target="b", relation="best_friend")])
    assert graph.search(SCOPE, GraphQuery(start_id="a", relation="knows")) == []
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="a", relation="best_friend"))) == {"b"}


def test_graph_delete_idempotent(graph):
    graph.insert(
        SCOPE,
        nodes=[Node(id="a"), Node(id="b")],
        edges=[Edge(id="e1", source="a", target="b", relation="r")],
    )
    graph.delete(SCOPE, node_ids=["a"], edge_ids=["e1"])
    graph.delete(SCOPE, node_ids=["a"], edge_ids=["e1"])  # 再删不报错
    graph.delete(SCOPE, node_ids=["never"], edge_ids=["never"])
    assert graph.get(SCOPE, ["a"]) == []


def test_graph_get_subset_and_empty(graph):
    graph.insert(SCOPE, nodes=[Node(id="a"), Node(id="b")])
    assert _ids(graph.get(SCOPE, ["a", "missing", "b"])) == {"a", "b"}
    assert graph.get(SCOPE, []) == []


def test_graph_empty_inputs_are_noops(graph):
    graph.insert(SCOPE)  # 无 nodes/edges
    graph.update(SCOPE)
    graph.delete(SCOPE)
    assert graph.get(SCOPE, []) == []


# --------------------------------------------------------------- 遍历
def test_graph_search_depth_relation_limit(graph):
    # a - b - c - d 链；a 另有 a-x（关系 other）
    graph.insert(
        SCOPE,
        nodes=[Node(id=i) for i in ("a", "b", "c", "d", "x")],
        edges=[
            Edge(id="ab", source="a", target="b", relation="link"),
            Edge(id="bc", source="b", target="c", relation="link"),
            Edge(id="cd", source="c", target="d", relation="link"),
            Edge(id="ax", source="a", target="x", relation="other"),
        ],
    )
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="a", depth=1))) == {"b", "x"}
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="a", depth=2))) == {"b", "x", "c"}
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="a", depth=3))) == {"b", "x", "c", "d"}
    # 关系过滤：只走 link
    assert _ids(graph.search(SCOPE, GraphQuery(start_id="a", relation="link", depth=3))) == {
        "b",
        "c",
        "d",
    }
    # limit 截断
    assert len(graph.search(SCOPE, GraphQuery(start_id="a", depth=3, limit=2))) == 2
    # 起点不存在 -> 空
    assert graph.search(SCOPE, GraphQuery(start_id="nope")) == []


# --------------------------------------------------------------- scope 隔离
def test_graph_scope_isolation(graph):
    other = Scope(org="itest", user="u2")
    graph.insert(SCOPE, nodes=[Node(id="shared", label="MINE")])
    graph.insert(other, nodes=[Node(id="shared", label="THEIRS")])  # 同 id 不冲突
    assert graph.get(SCOPE, ["shared"])[0].label == "MINE"
    assert graph.get(other, ["shared"])[0].label == "THEIRS"
    graph.delete(SCOPE, node_ids=["shared"])
    assert graph.get(SCOPE, ["shared"]) == []
    assert graph.get(other, ["shared"])[0].label == "THEIRS"  # 互不影响


# --------------------------------------------------------------- 持久化
def test_graph_persists_across_instances(graph, tmp_path):
    graph.insert(
        SCOPE,
        nodes=[Node(id="a", label="P", properties={"k": "v"}), Node(id="b")],
        edges=[Edge(id="e1", source="a", target="b", relation="r")],
    )
    # 新实例从同一 working_dir 重新加载
    reopened = NanoGraphRAGGraphStore(working_dir=getattr(graph, "_fallback_working_dir"))
    assert reopened.get(SCOPE, ["a"])[0].properties == {"k": "v"}
    assert _ids(reopened.search(SCOPE, GraphQuery(start_id="a", relation="r"))) == {"b"}


# --------------------------------------------------------------- 多轮一致性
def test_graph_many_ops_consistency(graph):
    n = 30
    graph.insert(SCOPE, nodes=[Node(id=f"n{i}", properties={"i": i}) for i in range(n)])
    # 串成一条链 n0-n1-...-n29
    graph.insert(
        SCOPE,
        edges=[
            Edge(id=f"e{i}", source=f"n{i}", target=f"n{i + 1}", relation="next")
            for i in range(n - 1)
        ],
    )
    assert _ids(graph.get(SCOPE, [f"n{i}" for i in range(n)])) == {f"n{i}" for i in range(n)}

    # 删偶数 id 的边，改奇数节点属性
    graph.delete(SCOPE, edge_ids=[f"e{i}" for i in range(0, n - 1, 2)])
    updated = [i for i in range(n) if i % 2 == 1]
    graph.update(SCOPE, nodes=[Node(id=f"n{i}", properties={"i": i, "upd": True}) for i in updated])

    got = {nd.id: nd for nd in graph.get(SCOPE, [f"n{i}" for i in range(n)])}
    assert len(got) == n  # 节点都在
    for i in range(n):
        assert got[f"n{i}"].properties.get("upd", False) == (i % 2 == 1)
    # n0 的边 e0 被删 -> 从 n0 深度1 搜不到 n1
    assert graph.search(SCOPE, GraphQuery(start_id="n0", depth=1)) == []
    # e1 (n1-n2) 保留 -> 从 n1 能搜到 n2
    assert "n2" in _ids(graph.search(SCOPE, GraphQuery(start_id="n1", depth=1)))
