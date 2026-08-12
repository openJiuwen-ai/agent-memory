"""Routing*Store：按 ConfigSource ``*.active`` 在已预装实例间切换（方案 A）。"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.routing import (
    ActiveRouter,
    RoutingFSStore,
    RoutingFulltextStore,
    RoutingFusionStore,
    RoutingGraphStore,
    RoutingKVStore,
    RoutingVectorStore,
)
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.types import (
    Document,
    FusionQuery,
    FusionRecord,
    Node,
    TextQuery,
    VectorQuery,
)

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="o", user="u")


def _router(namespace: str, instances: dict, cfg: DictConfigSource, default: str):
    return ActiveRouter(
        namespace=namespace,
        instances=instances,
        config_source=cfg,
        default_name=default,
    )


def test_routing_kv_store_switches_by_active() -> None:
    a = MagicMock(name="kv-a")
    b = MagicMock(name="kv-b")
    a.store_type.return_value = StoreType.KV
    b.store_type.return_value = StoreType.KV
    a.get.return_value = b"from-a"
    b.get.return_value = b"from-b"

    cfg = DictConfigSource({"kv_store.active": "a"})
    store = RoutingKVStore(_router("kv_store", {"a": a, "b": b}, cfg, "a"))

    assert store.get(_SCOPE, "k") == b"from-a"
    a.get.assert_called_once_with(_SCOPE, "k")
    assert store.store_type() is StoreType.KV

    cfg.put("kv_store.active", "b")
    assert store.get(_SCOPE, "k") == b"from-b"
    b.get.assert_called_once_with(_SCOPE, "k")


def test_routing_kv_store_unknown_active_raises() -> None:
    cfg = DictConfigSource({"kv_store.active": "ghost"})
    store = RoutingKVStore(
        _router("kv_store", {"a": MagicMock()}, cfg, "a")
    )
    with pytest.raises(ValidationError, match="ghost"):
        store.health()


def test_routing_vector_store_switches_by_active() -> None:
    a = MagicMock(name="vec-a")
    b = MagicMock(name="vec-b")
    a.store_type.return_value = StoreType.VECTOR
    b.store_type.return_value = StoreType.VECTOR
    q = VectorQuery(vector=[0.1], top_k=1)
    a.search.return_value = []
    b.search.return_value = []

    cfg = DictConfigSource({"vector_store.active": "milvus"})
    store = RoutingVectorStore(
        _router("vector_store", {"milvus": a, "pg": b}, cfg, "milvus")
    )
    store.search(_SCOPE, q)
    a.search.assert_called_once()
    cfg.put("vector_store.active", "pg")
    store.search(_SCOPE, q)
    b.search.assert_called_once()


def test_routing_fulltext_graph_fusion_fs_switch() -> None:
    cfg = DictConfigSource(
        {
            "fulltext_store.active": "es",
            "graph_store.active": "nano",
            "fusion_store.active": "mg",
            "fs_store.active": "local",
        }
    )
    ft_a, ft_b = MagicMock(), MagicMock()
    g_a, g_b = MagicMock(), MagicMock()
    fu_a, fu_b = MagicMock(), MagicMock()
    fs_a, fs_b = MagicMock(), MagicMock()
    for m in (ft_a, ft_b, g_a, g_b, fu_a, fu_b, fs_a, fs_b):
        m.store_type.return_value = StoreType.FULLTEXT

    ft = RoutingFulltextStore(_router("fulltext_store", {"es": ft_a, "mem": ft_b}, cfg, "es"))
    gr = RoutingGraphStore(_router("graph_store", {"nano": g_a, "mem": g_b}, cfg, "nano"))
    fu = RoutingFusionStore(_router("fusion_store", {"mg": fu_a, "mem": fu_b}, cfg, "mg"))
    fs = RoutingFSStore(_router("fs_store", {"local": fs_a, "mem": fs_b}, cfg, "local"))

    ft.search(_SCOPE, TextQuery(text="q"))
    ft_a.search.assert_called_once()
    gr.seed_ids(_SCOPE, {"t"})
    g_a.seed_ids.assert_called_once()
    fu.search(_SCOPE, FusionQuery(vector=[0.1]))
    fu_a.search.assert_called_once()
    fs.stat(_SCOPE, "r")
    fs_a.stat.assert_called_once()

    cfg.put("fulltext_store.active", "mem")
    cfg.put("graph_store.active", "mem")
    cfg.put("fusion_store.active", "mem")
    cfg.put("fs_store.active", "mem")
    ft.insert(_SCOPE, [Document(id="1", text="t")])
    ft_b.insert.assert_called_once()
    gr.insert(_SCOPE, nodes=[Node(id="n", label="l")])
    g_b.insert.assert_called_once()
    fu.insert(_SCOPE, [FusionRecord(id="1", vector=[0.1])])
    fu_b.insert.assert_called_once()
    fs.insert(_SCOPE, "k", BytesIO(b"x"))
    fs_b.insert.assert_called_once()


def test_routing_vector_delegates_recall_and_score_direction() -> None:
    a = MagicMock()
    a.recall.return_value = []
    a.score_higher_is_better.return_value = False
    cfg = DictConfigSource({"vector_store.active": "a"})
    store = RoutingVectorStore(_router("vector_store", {"a": a}, cfg, "a"))
    store.recall(_SCOPE, VectorQuery(vector=[1.0]), output_fields=["metadata"])
    a.recall.assert_called_once()
    assert store.score_higher_is_better() is False
