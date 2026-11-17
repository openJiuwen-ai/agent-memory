# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``shard_routed`` 图存储（F09 D10）。

两种模式：

- ``shared``（默认）：单图，节点 properties 上写 ``storage_shard`` 标签；检索在
  后端截断 limit **之前**按标签过滤（内层须声明 ``SHARD_PREFILTER``）。关联图
  节点就是记忆，没有公共节点：写入查不到归属或命中 ``moving`` 均拒绝。搬迁只改
  标签（``storage_shard_next`` 过渡，与共享向量索引一致）。
- ``split``：每个分片一个图存储；节点按 KV 归属路由，跨分片的边直接丢弃
  （接受跨分片多跳丢失）。搬迁复制节点到目标分片，源分片删除节点及其关联边。

证据图（角色关系投影）引用其他 Scope 的记忆、按关系头读取，不走本包装层：
其构建器要求 ``supports_incident_edges`` / ``supports_conditional_projection``，
本包装层不声明这两项，因此配成 ``shard_routed`` 时会在装配期被拒绝。
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import (
    ConflictError,
    MissingShardPlacementError,
    RelocationInProgressError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import BaseStore, StoreType
from jiuwen_memory.storage.capability import StoreCapability, capabilities_of, require_capability
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.shard_routing import ShardLocator, ShardResolver
from jiuwen_memory.storage.shard_search import (
    GRAPH_SHARD_FIELD,
    GRAPH_SHARD_NEXT_FIELD,
    LazyShardLocator,
    require_distinct_shard_resources,
    selected_shards,
)
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

logger = get_logger(__name__)


class ShardRoutedGraphStore(GraphStore):
    """图端口的 ``shard_routed`` 包装层（shared / split）。"""

    def __init__(
        self,
        *,
        resolver: ShardResolver,
        locator: ShardLocator,
        mode: str = "shared",
        inner: GraphStore | None = None,
        stores: dict[str, GraphStore] | None = None,
        owned: tuple[BaseStore, ...] = (),
    ) -> None:
        if mode not in {"shared", "split"}:
            raise ValidationError(f"graph shard mode must be 'shared' or 'split', got {mode!r}")
        if mode == "shared":
            if inner is None:
                raise ValidationError("shared graph sharding requires an inner GraphStore")
            require_capability(
                inner, StoreCapability.SHARD_PREFILTER, role="shared graph inner"
            )
        else:
            missing = [name for name in resolver.shards if name not in (stores or {})]
            if missing:
                raise ValidationError(f"shard_routed graph is missing stores for shards: {missing}")
            unknown = [name for name in (stores or {}) if name not in resolver.shards]
            if unknown:
                raise ValidationError(
                    f"shard_routed graph has stores for unconfigured shards: {unknown}"
                )
            require_distinct_shard_resources(stores or {}, kind="graph")
        self._resolver = resolver
        self._locator = locator
        self._mode = mode
        self._inner = inner
        self._stores = dict(stores or {})
        self._owned = tuple(owned)

    # -- 自描述 --------------------------------------------------------------- #

    def store_type(self) -> StoreType:
        return StoreType.GRAPH

    def capabilities(self) -> frozenset[StoreCapability]:
        caps = {StoreCapability.SHARD_ROUTED, StoreCapability.SHARD_RELOCATABLE}
        if self._inner is not None and StoreCapability.SHARD_PREFILTER in capabilities_of(
            self._inner
        ):
            caps.add(StoreCapability.SHARD_PREFILTER)
        return frozenset(caps)

    @property
    def shards(self) -> tuple[str, ...]:
        return tuple(self._stores) if self._mode == "split" else self._resolver.shards

    def shard_descriptors(self, kind: str = "graph") -> dict[str, dict[str, str]]:
        if self._inner is not None:
            descriptor = self._inner.resource_descriptor()
            if descriptor is None:
                return {}
            return {shard: {kind: descriptor} for shard in self.shards}
        out: dict[str, dict[str, str]] = {}
        for shard, store in self._stores.items():
            descriptor = store.resource_descriptor()
            if descriptor is not None:
                out[shard] = {kind: descriptor}
        return out

    def locator_name(self) -> str | None:
        provider = getattr(self._locator, "port_name", None)
        return provider() if callable(provider) else None

    def bind_shard_locator(self, locator: Any) -> None:
        bind = getattr(self._locator, "bind", None)
        if not callable(bind):
            raise ValidationError(
                f"{type(self).__name__} locator does not support assembly-time binding"
            )
        bind(locator)

    def health(self) -> None:
        if self._inner is not None:
            self._inner.health()
            return
        for store in self._stores.values():
            store.health()

    def connection_dependencies(self) -> tuple[BaseStore, ...]:
        targets = [self._inner] if self._inner is not None else list(self._stores.values())
        return tuple(store for store in targets if isinstance(store, BaseStore))

    def close(self) -> None:
        for store in self._owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        self._owned = ()

    # -- 读取 ---------------------------------------------------------------- #

    def _shared(self) -> GraphStore:
        if self._inner is None:
            raise ValidationError("shared graph sharding requires an inner GraphStore")
        return self._inner

    def _selected(self, extensions: dict[str, Any] | None) -> tuple[str, ...]:
        return selected_shards(extensions, self.shards)

    def seed_ids(
        self,
        scope: Scope,
        tokens: set[str],
        *,
        extensions: dict[str, Any] | None = None,
    ) -> list[str]:
        selected = self._selected(extensions)
        if self._mode == "shared":
            return self._shared().seed_ids(scope, tokens, extensions=extensions)
        out: list[str] = []
        seen: set[str] = set()
        for shard in selected:
            for node_id in self._stores[shard].seed_ids(scope, tokens):
                if node_id not in seen:
                    seen.add(node_id)
                    out.append(node_id)
        return out

    def search(self, scope: Scope, query: GraphQuery) -> list[Node]:
        selected = self._selected(query.extensions)
        if self._mode == "shared":
            return self._shared().search(scope, query)
        # split 已经按物理图选片；内层不应再按共享标签过滤（split 节点没有标签）。
        extensions = dict(query.extensions)
        extensions.pop("shards", None)
        inner_query = GraphQuery(
            start_id=query.start_id,
            relation=query.relation,
            depth=query.depth,
            limit=query.limit,
            extensions=extensions,
        )
        merged: dict[str, Node] = {}
        order: list[str] = []
        for shard in selected:
            for node in self._stores[shard].search(scope, inner_query):
                if node.id not in merged:
                    merged[node.id] = node
                    order.append(node.id)
        return [merged[node_id] for node_id in order[: query.limit]]

    def get(self, scope: Scope, node_ids: list[str]) -> list[Node]:
        if self._mode == "shared":
            return self._shared().get(scope, node_ids)
        found: dict[str, Node] = {}
        for store in self._stores.values():
            for node in store.get(scope, node_ids):
                found.setdefault(node.id, node)
        return [found[node_id] for node_id in node_ids if node_id in found]

    # -- 写入 ---------------------------------------------------------------- #

    def _located(self, scope: Scope, node_ids: list[str]):
        """定位写入涉及的节点；搬迁中拒绝，缺归属也拒绝（两种模式一致）。"""
        unique = list(dict.fromkeys(node_ids))
        location = self._locator.locate(scope, unique)
        moving = [node_id for node_id in unique if node_id in location.moving]
        if moving:
            raise RelocationInProgressError(moving[0])
        missing = [node_id for node_id in unique if node_id not in location.found]
        if missing:
            raise MissingShardPlacementError(missing)
        return location

    @staticmethod
    def _tag_node(node: Node, shard: str) -> Node:
        properties = dict(node.properties)
        properties[GRAPH_SHARD_FIELD] = shard
        return Node(id=node.id, label=node.label, properties=properties)

    def insert(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        nodes = nodes or []
        edges = edges or []
        if self._mode == "shared":
            inner = self._shared()
            endpoint_ids = [value for edge in edges for value in (edge.source, edge.target)]
            location = self._located(scope, [node.id for node in nodes] + endpoint_ids)
            inner.insert(
                scope,
                nodes=[self._tag_node(node, location.found[node.id]) for node in nodes],
                edges=edges,
            )
            return
        grouped_nodes: dict[str, list[Node]] = {}
        if nodes:
            location = self._located(scope, [node.id for node in nodes])
            for node in nodes:
                grouped_nodes.setdefault(location.found[node.id], []).append(node)
        for shard, group in grouped_nodes.items():
            self._stores[shard].insert(scope, nodes=group)
        if edges:
            endpoints = [value for edge in edges for value in (edge.source, edge.target)]
            location = self._located(scope, endpoints)
            by_shard: dict[str, list[Edge]] = {}
            for edge in edges:
                source, target = location.found[edge.source], location.found[edge.target]
                if source != target:
                    logger.debug(
                        "drop cross-shard graph edge %s (%s->%s)",
                        edge.id,
                        source,
                        target,
                    )
                    continue
                by_shard.setdefault(source, []).append(edge)
            for shard, group in by_shard.items():
                self._stores[shard].insert(scope, edges=group)

    def update(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        nodes = nodes or []
        edges = edges or []
        if self._mode == "shared":
            inner = self._shared()
            endpoint_ids = [value for edge in edges for value in (edge.source, edge.target)]
            location = self._located(scope, [node.id for node in nodes] + endpoint_ids)
            inner.update(
                scope,
                nodes=[self._tag_node(node, location.found[node.id]) for node in nodes],
                edges=edges,
            )
            return
        grouped: dict[str, list[Node]] = {}
        if nodes:
            location = self._located(scope, [node.id for node in nodes])
            for node in nodes:
                grouped.setdefault(location.found[node.id], []).append(node)
        for shard, group in grouped.items():
            self._stores[shard].update(scope, nodes=group)
        if edges:
            endpoints = [value for edge in edges for value in (edge.source, edge.target)]
            location = self._located(scope, endpoints)
            by_shard: dict[str, list[Edge]] = {}
            for edge in edges:
                source, target = location.found[edge.source], location.found[edge.target]
                if source == target:
                    by_shard.setdefault(source, []).append(edge)
            for shard, group in by_shard.items():
                self._stores[shard].update(scope, edges=group)

    def delete(
        self,
        scope: Scope,
        node_ids: list[str] | None = None,
        edge_ids: list[str] | None = None,
    ) -> None:
        unique = list(dict.fromkeys(node_ids or []))
        if unique:
            location = self._locator.locate(scope, unique)
            moving = [node_id for node_id in unique if node_id in location.moving]
            if moving:
                raise RelocationInProgressError(moving[0])
        if self._mode == "shared":
            self._shared().delete(scope, node_ids=node_ids, edge_ids=edge_ids)
            return
        if unique:
            for node_id in unique:
                shard = location.found.get(node_id)
                if shard is not None:
                    self._stores[shard].delete(scope, node_ids=[node_id])
                else:
                    for store in self._stores.values():
                        store.delete(scope, node_ids=[node_id])
        if edge_ids:
            for store in self._stores.values():
                store.delete(scope, edge_ids=edge_ids)

    # -- 搬迁（F09 D16） ------------------------------------------------------ #

    def relocate_unit(self, scope: Scope, unit_id: str, source: str, target: str) -> None:
        if self._mode == "shared":
            inner = self._shared()
            nodes = inner.get(scope, [unit_id])
            if not nodes:
                return
            properties = dict(nodes[0].properties)
            properties[GRAPH_SHARD_NEXT_FIELD] = target
            inner.update(
                scope,
                nodes=[Node(id=unit_id, label=nodes[0].label, properties=properties)],
            )
            return
        nodes = self._stores[source].get(scope, [unit_id])
        if not nodes:
            return
        try:
            self._stores[target].insert(scope, nodes=[nodes[0]])
        except ConflictError:
            existing = self._stores[target].get(scope, [unit_id])
            if not existing or existing[0] != nodes[0]:
                raise

    def cleanup_unit(self, scope: Scope, unit_id: str, shard: str) -> None:
        if self._mode == "shared":
            inner = self._shared()
            nodes = inner.get(scope, [unit_id])
            if not nodes:
                return
            properties = dict(nodes[0].properties)
            current = properties.get(GRAPH_SHARD_FIELD)
            pending = properties.get(GRAPH_SHARD_NEXT_FIELD)
            if current == shard and pending is not None:
                properties[GRAPH_SHARD_FIELD] = pending
                properties.pop(GRAPH_SHARD_NEXT_FIELD, None)
            elif pending == shard:
                properties.pop(GRAPH_SHARD_NEXT_FIELD, None)
            else:
                return
            inner.update(
                scope,
                nodes=[Node(id=unit_id, label=nodes[0].label, properties=properties)],
            )
            return
        self._stores[shard].delete(scope, node_ids=[unit_id])


@GraphProducer.register("shard_routed")
def _build(config):
    """装配分片图：resolver + shared inner 或每分片图存储。"""
    from jiuwen_memory.storage.shard_routing import ShardResolverProducer

    resolver = ShardResolverProducer.dep(config, "resolver")
    locator = LazyShardLocator(str(config.get("locator", "default")))
    mode = str(config.get("mode", "shared"))
    if mode == "shared":
        inner = GraphProducer.dep(config, "inner")
        return ShardRoutedGraphStore(
            resolver=resolver, locator=locator, mode="shared", inner=inner
        )
    specs = config.params.get("stores") or {}
    if not isinstance(specs, dict) or not specs:
        raise ValidationError("split graph sharding requires params.stores")
    stores: dict[str, GraphStore] = {}
    owned: list[GraphStore] = []
    for shard, spec in specs.items():
        if isinstance(spec, str):
            # 具名实例由装配缓存共享，包装层不能关闭借用资源。
            stores[shard] = GraphProducer.build_named(spec, config.ctx)
        elif isinstance(spec, dict) and spec.get("target"):
            store = GraphProducer.build(
                str(spec["target"]),
                dict(spec.get("params") or {}),
                config.ctx,
                name=f"graph_{shard}",
            )
            stores[shard] = store
            owned.append(store)
        else:
            raise ValidationError(
                f"params.stores[{shard!r}] must be a name or inline config"
            )
    return ShardRoutedGraphStore(
        resolver=resolver,
        locator=locator,
        mode="split",
        stores=stores,
        owned=tuple(owned),
    )
