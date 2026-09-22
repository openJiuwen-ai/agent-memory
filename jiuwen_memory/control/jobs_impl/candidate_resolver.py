# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""候选源 resolver——四源一个接口、一个产出、一个调用点（F04 D3）。

``EvolveJob.run`` 只调 ``resolver.resolve()``，不感知源类型（换源零改动）；
筛选过程发生在同一阶段：③的召回、④的子桶枚举都包在各自 resolver 内部。
四实现全部由**已有存储原语**组合而成（``list_units`` / ``load_units`` /
``Retriever.retrieve`` / ``scopes()``），零新查询能力。

依赖注入：kv / retriever 由 Engine 装配期固化（与 Engine 同一实例，E-06 同
模式）；recall 源走 Engine 装配的同一 ``Retriever``（含融合 + 真源复核），
而非裸 VectorStore.search。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    CandidateGroup,
    CandidateOutcome,
    FilterClause,
    FilterExpr,
    FilterOp,
    MemoryUnit,
    PredicateCandidate,
    RecallChannel,
    Scope,
    and_merge,
)
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import RetrievalQuery
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.kv import KVStore, list_units, load_units

MAX_CANDIDATE_UNITS = 10_000
MAX_FAN_OUT_BUCKETS = 1_000

CandidateStore = KVStore | DomainStore


def _is_kv_store(store: CandidateStore) -> bool:
    """识别 KV 端口及 StoreManager 的授权代理（代理不保留 isinstance）。"""
    return isinstance(store, KVStore) or (
        hasattr(store, "scan") and hasattr(store, "mget")
    )


def _list_units(
    store: CandidateStore,
    scope: Scope,
    *,
    limit: int,
    filters: FilterExpr | None = None,
) -> tuple[list[MemoryUnit], int]:
    if _is_kv_store(store):
        return list_units(store, scope, limit=limit, filters=filters)
    result = store.list(scope, limit=limit, filters=filters)
    return result.items, result.count


def _load_units(
    store: CandidateStore, scope: Scope, unit_ids: list[str]
) -> list[MemoryUnit]:
    if _is_kv_store(store):
        return load_units(store, scope, unit_ids)
    return store.get(scope, unit_ids)


def _scopes(store: CandidateStore) -> list[Scope]:
    return store.scopes()


def _require_within_unit_limit(count: int, scope: Scope) -> None:
    if count > MAX_CANDIDATE_UNITS:
        raise ValidationError(
            "dreaming 候选量超过单次上限 "
            f"{MAX_CANDIDATE_UNITS}：scope={scope!r}, count={count}；"
            "请增加 filters/window 或拆分 scope"
        )


class CandidateResolver(Protocol):
    """统一候选解析接口：产出 :class:`~common.type_def.CandidateOutcome`。"""

    async def resolve(self) -> CandidateOutcome:
        ...


def _effective_filters(filters: FilterExpr | None, window: int | None) -> FilterExpr | None:
    """谓词源静态子句 + 本次 resolve 现算的 ``t_ingest`` 时间窗子句 → AND 合并。

    时间窗走 filters 下推（F04 D5）：t_ingest 恒非空（内核接入
    路径强制盖章，防御投影落 T_EVENT_UNKNOWN 哨兵=「史前」不入任何新近窗口），
    ``t_ingest GTE cutoff``（毫秒）进 ``list_units`` 由各后端 FilterExpr 求值器
    统一裁决。cutoff 不落盘、每次现算——重启后窗口随 tick 自然滑动。
    """
    if not window:
        return filters
    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(seconds=window)).timestamp() * 1000
    )
    return and_merge(filters, [FilterClause("t_ingest", FilterOp.GTE, cutoff_ms)])


def covered_by(
    parent: Scope,
    child: Scope,
    require_empty: frozenset[str] = frozenset(),
    require_nonempty: frozenset[str] = frozenset(),
) -> bool:
    """parent 是否覆盖 child：前缀相等 + 形状约束（F04 D3）。

    前缀：parent 的每个非空字段须与 child 相等——(org=acme) 覆盖 org=acme
    的全部子 scope；五元组全指定则只覆盖自身。形状：``require_empty`` /
    ``require_nonempty`` 对 child 指定 scope 字段施空/非空硬约束（如「所有
    用户桶」= require_empty={"agent", "session"}）——只在前缀命中的覆盖集
    上**剔除**、永不放大（单向收窄，租户边界由前缀钉死，无越权面）。

    公共函数：API 层 dreaming 编排做 fan-out 逐桶鉴权前，用它枚举命中桶。
    """

    for f in ("org", "space", "user", "agent", "session"):
        if getattr(parent, f) and getattr(parent, f) != getattr(child, f):
            return False
    if any(getattr(child, f) for f in require_empty):
        return False
    if not all(getattr(child, f) for f in require_nonempty):
        return False
    return True


class PredicateResolver:
    """① 谓词源：``list_units`` + filters 下推（含时间窗合并），恒单桶。"""

    def __init__(
        self,
        scope: Scope,
        kv: CandidateStore,
        filters: FilterExpr | None = None,
        window: int | None = None,
    ) -> None:
        self._scope = scope
        self._kv = kv
        self._filters = filters
        self._window = window

    async def resolve(self) -> CandidateOutcome:
        units, count = await asyncio.to_thread(
            _list_units,
            self._kv,
            self._scope,
            limit=MAX_CANDIDATE_UNITS + 1,
            filters=_effective_filters(self._filters, self._window),
        )
        _require_within_unit_limit(count, self._scope)
        return CandidateOutcome(
            groups=[CandidateGroup(scope=self._scope, units=units)]
        )


class IdsResolver:
    """② 点名源：``load_units`` 点读 + 差集回显，恒单桶。

    ``load_units`` 契约：不过滤不复核、缺失 id 省略不报错——越权/缺失 id
    自然出批；superseded 项照常演进（裁决权在演进模式，F04 D3）。
    """

    def __init__(self, scope: Scope, kv: CandidateStore, unit_ids: list[str]) -> None:
        self._scope = scope
        self._kv = kv
        self._unit_ids = unit_ids

    async def resolve(self) -> CandidateOutcome:
        # 去重保序（重复 id 点读各自返回，无业务意义且稀释差集回显）。
        requested = list(dict.fromkeys(self._unit_ids))
        units = await asyncio.to_thread(_load_units, self._kv, self._scope, requested)
        loaded = {unit.id for unit in units}
        return CandidateOutcome(
            groups=[CandidateGroup(scope=self._scope, units=units)],
            requested_ids=requested,
            loaded_ids=[unit.id for unit in units],
            skipped=[
                f"{uid}:not_found" for uid in requested if uid not in loaded
            ],
        )


class RecallResolver:
    """③ 召回源：``Retriever`` 完整检索链路选批 → 真源点读，恒单桶。

    query 管「像什么」（相关性，谓词表达不了）、filters 管「还须满足什么」
    （收窄，与 search 接口同构）。检索层产出的 unit_ids 走 ``load_units``
    点读回完整 MemoryUnit——不过滤不复核（forget 等模式需要 SUPERSEDED
    也能进批）。每 tick 重新召回：top-k 是「当前时刻」的答案（F04 D3）。
    """

    def __init__(
        self,
        scope: Scope,
        kv: CandidateStore,
        retriever: Retriever,
        query: dict,
        channels: list[str],
        top_k: int = 50,
        filters: FilterExpr | None = None,
    ) -> None:
        self._scope = scope
        self._kv = kv
        self._retriever = retriever
        self._query = query
        self._channels = channels
        self._top_k = top_k
        self._filters = filters

    async def resolve(self) -> CandidateOutcome:
        retrieval_query = RetrievalQuery(
            text=str(self._query.get("text", "")),
            top_k=self._top_k,
            channels=[RecallChannel(c) for c in self._channels] or None,
            filters=self._filters,
        )
        result = await asyncio.to_thread(
            self._retriever.retrieve, self._scope, retrieval_query
        )
        # 融合后仍可能跨通道重复命中同一 unit——保序去重后真源点读。
        unit_ids = list(dict.fromkeys(item.unit_id for item in result.items))
        units = await asyncio.to_thread(_load_units, self._kv, self._scope, unit_ids)
        return CandidateOutcome(
            groups=[CandidateGroup(scope=self._scope, units=units)]
        )


class FanOutResolver:
    """④ 枚举源：``scopes()`` × 子谓词，多桶（演进按桶进行，防跨用户混桶）。

    父 scope 只声明覆盖范围（非空字段即前缀约束），``require_empty`` /
    ``require_nonempty`` 在前缀之外按子桶 scope 字段形状收窄（只会剔除、
    不会放大，缺省不约束 = 现行为），子桶选料复用①。

    桶集合来源（F04 D7，PEP 边界下的分工）：

    - API 层注入 ``buckets``（已获准桶列表）+ ``denied_scopes``（拒绝桶
      标签）——API 层 dreaming 编排先枚举命中桶（``covered_by``）、逐桶
      PEP 鉴权，只把获准桶传下来；本 resolver 纯执行：不再鉴权、``denied``
      原样回显给调用方（谁没跑、为什么），不静默吞、也不中断其余桶。
    - ``buckets=None``（内核直调，无 API 层）——本 resolver 自行枚举
      ``scopes()`` 并按 ``covered_by`` 过滤全部命中桶，无授权、无拒绝。
    """

    def __init__(
        self,
        scope: Scope,
        kv: CandidateStore,
        child: PredicateCandidate,
        require_empty: frozenset[str] = frozenset(),
        require_nonempty: frozenset[str] = frozenset(),
        buckets: list[Scope] | None = None,
        denied_scopes: list[str] | None = None,
    ) -> None:
        self._scope = scope
        self._kv = kv
        self._child = child
        self._require_empty = require_empty
        self._require_nonempty = require_nonempty
        self._buckets = buckets
        self._denied = denied_scopes

    async def resolve(self) -> CandidateOutcome:
        groups: list[CandidateGroup] = []
        if self._buckets is not None:
            # API 层已枚举 + 鉴权完毕——纯执行：桶列表原样进料。
            hit_scopes = self._buckets
        else:
            hit_scopes = []
            for candidate_scope in await asyncio.to_thread(_scopes, self._kv):
                if covered_by(
                    self._scope,
                    candidate_scope,
                    require_empty=self._require_empty,
                    require_nonempty=self._require_nonempty,
                ):
                    hit_scopes.append(candidate_scope)
        if len(hit_scopes) > MAX_FAN_OUT_BUCKETS:
            raise ValidationError(
                "fan_out 命中桶数超过单次上限 "
                f"{MAX_FAN_OUT_BUCKETS}：{len(hit_scopes)}；请收窄父 scope 或形状约束"
            )
        total_units = 0
        for child_scope in hit_scopes:
            units, count = await asyncio.to_thread(
                _list_units,
                self._kv,
                child_scope,
                limit=MAX_CANDIDATE_UNITS + 1,
                filters=_effective_filters(self._child.filters, self._child.window),
            )
            _require_within_unit_limit(count, child_scope)
            total_units += count
            if total_units > MAX_CANDIDATE_UNITS:
                raise ValidationError(
                    "fan_out 候选总量超过单次上限 "
                    f"{MAX_CANDIDATE_UNITS}：count={total_units}；"
                    "请收窄父 scope、形状约束或子谓词"
                )
            groups.append(CandidateGroup(scope=child_scope, units=units))
        return CandidateOutcome(groups=groups, denied_scopes=self._denied or None)
