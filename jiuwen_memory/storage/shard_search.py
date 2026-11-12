# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""分片检索的共用件（F09 D8 / D11）：分片选择、共享索引过滤、跨索引合并。

向量与全文包装层共用同一套语义，避免两处实现漂移：

- 未指定 ``extensions["shards"]`` → 查全部分片；
- ``[]`` / ``None`` / 非列表 / 未配置分片名 → ``ValidationError``；
- 共享索引把 ``system_metadata.storage_shard IN (...)`` 与调用方 filters 取 AND，在截断前下推；
- 拆分索引各分片取 top-k 后按分数合并、按 id 去重（保留较优分数）再截断。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError, ValidationError
from jiuwen_memory.common.type_def import FilterExpr, Scope
from jiuwen_memory.common.type_def.filter import FilterClause, FilterGroup, FilterLogic, FilterOp
from jiuwen_memory.storage.capability import StoreCapability, require_capability
from jiuwen_memory.storage.shard_port import ShardSetProvider
from jiuwen_memory.storage.shard_routing import ShardLocator

#: 共享索引中由 storage 维护的分片标签字段（覆盖调用方同名输入）。
#:
#: 放在既有的 ``system_metadata.`` 命名空间下（而不是另造一个裸字段），这样
#: ``normalize`` 会原样保留它、各后端已有的 metadata 索引/过滤路径直接可用，
#: 也不需要让 common 层认识一个只属于 storage 的字段名。
STORAGE_SHARD_FIELD = "system_metadata.storage_shard"
#: 共享索引搬迁过渡期的目标分片标签（F09 D16）。``relocate_unit`` 只写这个字段、不改
#: ``STORAGE_SHARD_FIELD``；清理阶段才把它转正。前置过滤对两者取 OR，所以过渡期内按
#: 源分片、目标分片查询都能在截断前命中，最终归属仍由返回前的归属校验裁决。
STORAGE_SHARD_NEXT_FIELD = "system_metadata.storage_shard_next"
_UNIT_ID_FIELD = "unit_id"
_BINDING_SEPARATORS = ("-", ":")

_T = TypeVar("_T")


def unit_id_from_metadata(record_id: str, metadata: dict[str, Any]) -> str:
    """校验「记录 id 由 metadata.unit_id 派生」的绑定并返回 unit_id（F09 D8）。"""
    unit_id = metadata.get(_UNIT_ID_FIELD)
    if not isinstance(unit_id, str) or not unit_id:
        raise ValidationError(
            f"record {record_id!r} metadata must carry a non-empty 'unit_id' for sharding"
        )
    if record_id != unit_id and not any(
        record_id.startswith(f"{unit_id}{separator}") for separator in _BINDING_SEPARATORS
    ):
        raise ValidationError(
            f"record id {record_id!r} is not derived from its unit_id {unit_id!r}"
        )
    return unit_id


def parse_shards(extensions: dict[str, Any] | None) -> tuple[str, ...] | None:
    """解析 ``extensions['shards']`` 的形状：缺键 → ``None``，非法 / 空 → 报错。

    这是 ``[]`` / ``None`` / 非集合 / 空字符串成员判定的**唯一实现**——KV、数据面、
    检索层与展开器都调它，避免一处把空集合当成「未指定」而静默退回全分片。
    """
    if not extensions or "shards" not in extensions:
        return None
    value = extensions["shards"]
    if value is None or isinstance(value, (str, bytes)) or not isinstance(
        value, (list, tuple, set, frozenset)
    ):
        raise ValidationError("extensions['shards'] must be a non-empty list of shard names")
    selected = list(value)
    if not selected:
        raise ValidationError("extensions['shards'] must not be empty")
    if any(not isinstance(name, str) or not name for name in selected):
        raise ValidationError("extensions['shards'] must contain non-empty shard names")
    return tuple(dict.fromkeys(selected))


def selected_shards(
    extensions: dict[str, Any] | None, all_shards: tuple[str, ...]
) -> tuple[str, ...]:
    """解析 ``extensions['shards']``；未指定返回全部分片，未知分片报错。"""
    selected = parse_shards(extensions)
    if selected is None:
        return all_shards
    unknown = [name for name in selected if name not in all_shards]
    if unknown:
        raise ValidationError(
            f"unknown shard(s) {unknown}; configured shards: {list(all_shards)}"
        )
    return selected


def configured_shards(locator_port: Any) -> tuple[str, ...]:
    """读取定位端口声明的已配置分片集合（唯一入口，按能力而非鸭子类型判定）。

    ``SHARD_LOCATOR`` 是「能定位」的能力；分片集合是它的只读事实，端口名与
    字段形状由该契约固定。调用方不要再 ``getattr(port, "shards")`` 猜。
    """
    require_capability(locator_port, StoreCapability.SHARD_LOCATOR, role="kv locator")
    # 声明 SHARD_LOCATOR 即承诺实现 ShardSetProvider（`shards` 只读属性）。
    provider = cast(ShardSetProvider, locator_port)
    return tuple(provider.shards)


def with_shard_filter(filters: FilterExpr | None, shards: tuple[str, ...]) -> FilterExpr:
    """把分片条件与调用方 filters 取 AND（共享索引前置过滤）。

    分片条件是「当前标签或过渡期目标标签属于选中分片」：搬迁复制阶段记录同时带两个
    标签，按源分片或目标分片查询都不会在截断前被排除（F09 D16）。
    """
    clause = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause(field=STORAGE_SHARD_FIELD, op=FilterOp.IN, value=list(shards)),
            FilterClause(field=STORAGE_SHARD_NEXT_FIELD, op=FilterOp.IN, value=list(shards)),
        ],
    )
    if filters is None:
        return clause
    return FilterGroup(FilterLogic.AND, [clause, filters])


def merge_scored(
    items: list[_T],
    limit: int,
    *,
    higher_is_better: bool,
    key: Any = lambda item: item.id,
    score: Any = lambda item: item.score,
) -> list[_T]:
    """按分数合并各分片结果，同 id 保留较优者，截断到 ``limit``。"""
    best: dict[Any, _T] = {}
    for item in items:
        identifier = key(item)
        previous = best.get(identifier)
        if previous is None:
            best[identifier] = item
            continue
        if higher_is_better:
            better = score(item) > score(previous)
        else:
            better = score(item) < score(previous)
        if better:
            best[identifier] = item
    merged = sorted(best.values(), key=score, reverse=higher_is_better)
    return merged[:limit]


class LazyShardLocator(ShardLocator):
    """定位端口的装配期延迟绑定桩（S09 #3 / #4）。

    包装层的 ``locator`` 参数指向一个**具名 KV 端口**，而端口在 manager 构造前
    还不存在，因此装配器建好端口后必须调用 :meth:`bind` 完成绑定
    （``CompositeStoreManager.from_config`` → ``_bind_shard_locators``）。

    **不提供运行期解析兜底**：未绑定就 ``locate`` 直接失败（统一「不支持」错误），
    否则等于允许包装层在运行期经服务定位器另建一套平行拓扑——那正是 S09 #3 禁止的，
    也把「缺必需依赖」从装配期拖到运行期。
    """

    def __init__(self, port_name: str = "default") -> None:
        self._port_name = port_name
        self._resolved: Any = None

    def bind(self, port: Any) -> None:
        """装配期绑定已构建的定位端口（只绑一次，重复绑定必须一致）。"""
        if self._resolved is not None and self._resolved is not port:
            raise UnsupportedStorageCapabilityError(
                f"shard locator {self._port_name!r} is already bound to another port"
            )
        self._resolved = port

    def bound(self) -> bool:
        """是否已完成装配期绑定。"""
        return self._resolved is not None

    def _require_bound(self) -> Any:
        if self._resolved is None:
            raise UnsupportedStorageCapabilityError(
                f"shard locator for kv port {self._port_name!r} is not bound: "
                "the shard_routed wrapper must be assembled by the store manager so "
                "the locator port exists at startup (S09 #3 / #4)"
            )
        return self._resolved

    def port_name(self) -> str:
        return self._port_name

    def locate(self, scope: Scope, unit_ids: list[str]) -> Any:
        return self._require_bound().locate(scope, unit_ids)

# ---------------------------------------------------------------------------
# 图分片（F09 D10）
# ---------------------------------------------------------------------------

#: 共享图节点上的分片标签（Node.properties，不是 MemoryUnit metadata）。
GRAPH_SHARD_FIELD = "storage_shard"
#: 共享图节点搬迁过渡期目标分片（F09 D16）；过滤时与正式标签取 OR。
GRAPH_SHARD_NEXT_FIELD = "storage_shard_next"


def graph_shard_filter(extensions: dict[str, Any] | None) -> frozenset[str] | None:
    """解析图检索的 ``extensions['shards']``：未指定返回 ``None``（全部分片）。"""
    selected = parse_shards(extensions)
    return None if selected is None else frozenset(selected)


def graph_node_allowed(
    properties: dict[str, Any], selected: frozenset[str] | None
) -> bool:
    """共享图节点是否属于选中分片。

    关联图节点就是记忆，不存在“公共节点”：没有标签的节点（归属已删除 / 旧数据）
    在带 ``shards`` 的查询里不返回，避免占掉 limit 名额。
    """
    if selected is None:
        return True
    tags = {
        properties.get(GRAPH_SHARD_FIELD),
        properties.get(GRAPH_SHARD_NEXT_FIELD),
    }
    tags.discard(None)
    return bool(tags & selected)


def require_distinct_shard_resources(
    stores: Mapping[str, Any], *, kind: str
) -> None:
    """split 包装层：同一端口的不同分片不能指向同一个物理资源。

    进程内后端 ``resource_descriptor()`` 返回 ``None``，无法证明独立，按现有约定跳过；
    连接型 / 文件型后端必须给出稳定描述符，重复即拒绝启动。
    """
    seen: dict[str, str] = {}
    for shard, store in stores.items():
        descriptor = store.resource_descriptor()
        if descriptor is None:
            continue
        if descriptor in seen:
            raise ValidationError(
                f"{kind} split shards {seen[descriptor]!r} and {shard!r} point to "
                f"the same physical resource {descriptor!r}"
            )
        seen[descriptor] = shard
