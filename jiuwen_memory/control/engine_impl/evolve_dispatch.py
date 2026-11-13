# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""evolve 执行分发 + 候选源装配（Engine 层共用件，纯执行）。

F10 D3 层级分工：**EvolveJob 拥有链条**（候选→演进→回显），**Engine 拥有
装配与分发**（candidate→resolver 翻译、提交），**Scheduler 拥有调度**（通道/
并发）。本模块是 Engine 分发逻辑的共用实现——InMemoryEngine / CloudEngine
的 ``evolve`` 都委托到这里，不各自复制。

**PEP 边界（S03）**：本层不做鉴权。入口鉴权与持续授权（dreaming 的注册/
注销/恢复编排、每 tick 复验、fan-out 逐桶裁决）都在 API 层
（:mod:`jiuwen_memory.api.memory_api_impl.dreaming`）；本层只接收 API 层
裁决后的产物——``buckets``（获准桶列表）/ ``denied_scopes``（拒绝桶标签，
随 JobInfo 原样回显给调用方）。``buckets=None`` 的内核直调路径下，fan-out
resolver 自行枚举全部命中桶（无授权、无拒绝）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    CandidateSource,
    FanOutCandidate,
    FilterClause,
    FilterOp,
    IdsCandidate,
    MemoryUnit,
    PredicateCandidate,
    RecallCandidate,
    Scope,
    and_merge,
    candidate_from_dict,
)
from jiuwen_memory.construction import EvolveMode, Evolver
from jiuwen_memory.construction.evolver import EvolverProducer
from jiuwen_memory.control.jobs import JobFactory, JobType
from jiuwen_memory.control.jobs_impl.candidate_resolver import (
    CandidateResolver,
    CandidateStore,
    FanOutResolver,
    IdsResolver,
    PredicateResolver,
    RecallResolver,
)
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.types import Channel
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.storage.store_manager import StoreManagerProducer

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModeEvolverBinding:
    """专用 mode 的处理实例及可选候选真源；不改变其他 mode 的数据面。"""

    evolver: Evolver
    candidate_store: CandidateStore | None = None
    retriever: Retriever | None = None


def build_mode_evolvers(config) -> dict[EvolveMode, ModeEvolverBinding]:
    """装配 mode 的处理实例/候选真源，不经条目 profile 路由。"""
    raw = config.params.get("mode_evolvers", {})
    if not isinstance(raw, dict):
        raise ValidationError("engine.params.mode_evolvers 必须是 mode 到 Evolver 引用的映射")
    result: dict[EvolveMode, ModeEvolverBinding] = {}
    for value, spec in raw.items():
        try:
            mode = EvolveMode(value)
        except (ValueError, TypeError):
            raise ValidationError(f"mode_evolvers 的演进模式无效：{value!r}") from None
        domain_name = None
        retriever_name = None
        if isinstance(spec, dict):
            if set(spec) - {"evolver", "domain_store", "retriever"}:
                raise ValidationError(f"mode_evolvers[{mode.value!r}] 包含未知配置字段")
            name = spec.get("evolver")
            domain_name = spec.get("domain_store")
            retriever_name = spec.get("retriever")
            if domain_name is not None and (
                not isinstance(domain_name, str) or not domain_name.strip()
            ):
                raise ValidationError(f"mode_evolvers[{mode.value!r}].domain_store 必须是具名引用")
            if retriever_name is not None and (
                not isinstance(retriever_name, str) or not retriever_name.strip()
            ):
                raise ValidationError(f"mode_evolvers[{mode.value!r}].retriever 必须是具名引用")
        else:
            name = spec
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"mode_evolvers[{mode.value!r}] 必须引用具名 Evolver")
        store = (
            StoreManagerProducer.resolve(config).domain_store(domain_name.strip())
            if domain_name is not None else None
        )
        result[mode] = ModeEvolverBinding(
            evolver=EvolverProducer.build_named(name.strip(), config.ctx), candidate_store=store,
            retriever=(
                RetrieverProducer.build_named(retriever_name.strip(), config.ctx)
                if retriever_name is not None else None
            ),
        )
    return result


def mode_candidate_scopes(
    mode: EvolveMode,
    mode_evolvers: dict[EvolveMode, Evolver | ModeEvolverBinding],
    fallback: Callable[[], list[Scope]],
) -> list[Scope]:
    """fan-out 授权枚举与执行阶段使用同一个 mode 候选数据面。"""
    binding = mode_evolvers.get(mode)
    if isinstance(binding, ModeEvolverBinding) and binding.candidate_store is not None:
        return binding.candidate_store.scopes()
    return fallback()


def validate_mode_evolver_binding(
    mode: EvolveMode,
    mode_evolvers: dict[EvolveMode, Evolver | ModeEvolverBinding],
    candidate: CandidateSource | dict[str, Any] | None = None,
) -> None:
    """注册和执行前纯配置校验，避免长周期任务首次 tick 才暴露错配。"""
    selected = mode_evolvers.get(mode)
    if selected is None and mode is EvolveMode.EXPERIENCE_CONSOLIDATE:
        raise ValidationError(
            "experience_consolidate 要求 engine.params.mode_evolvers 显式绑定处理实例"
        )
    if isinstance(selected, ModeEvolverBinding) and selected.candidate_store is not None:
        parsed = candidate_from_dict(candidate) if isinstance(candidate, dict) else candidate
        if isinstance(parsed, RecallCandidate) and selected.retriever is None:
            raise ValidationError(
                "mode_evolvers 显式绑定 domain_store 的 recall 候选必须同时绑定同源 retriever"
            )


def _experience_candidate(
    candidate: CandidateSource | dict[str, Any] | None,
) -> CandidateSource:
    """专用 mode 在取数前下推经验类型过滤；点名源由执行边界再次核对类型。"""
    if isinstance(candidate, dict):
        candidate = candidate_from_dict(candidate)
    constraint = [FilterClause("system_metadata.memory_type", FilterOp.EQ, "experience")]
    if candidate is None:
        return PredicateCandidate(filters=constraint)
    if isinstance(candidate, (PredicateCandidate, RecallCandidate)):
        return replace(candidate, filters=and_merge(candidate.filters, constraint))
    if isinstance(candidate, FanOutCandidate):
        return replace(
            candidate,
            child=replace(candidate.child, filters=and_merge(candidate.child.filters, constraint)),
        )
    return candidate

# candidate kind → resolver 工厂（注册表，与 candidate.py 的 _CANDIDATE_CODECS、
# construction 层 EvolverProducer 同款风格）：新增候选源类型 = candidate.py 加
# dataclass（含 kind）+ codec 条目 + 此处一条工厂条目——不改 build_resolver 本体，
# 无 isinstance 分支表（S09 §1/§9 基线与 Evolver 插件拉平）。
ResolverFactory = Callable[..., CandidateResolver]


def _predicate_factory(
    scope: Scope, candidate: PredicateCandidate, *, kv: CandidateStore, retriever: Retriever | None
) -> CandidateResolver:
    return PredicateResolver(
        scope=scope, kv=kv, filters=candidate.filters, window=candidate.window
    )


def _ids_factory(
    scope: Scope, candidate: IdsCandidate, *, kv: CandidateStore, retriever: Retriever | None
) -> CandidateResolver:
    return IdsResolver(scope=scope, kv=kv, unit_ids=candidate.unit_ids)


def _recall_factory(
    scope: Scope, candidate: RecallCandidate, *, kv: CandidateStore, retriever: Retriever | None
) -> CandidateResolver:
    if retriever is None:
        raise ValidationError("recall 候选源要求 Engine 装配 Retriever")
    return RecallResolver(
        scope=scope,
        kv=kv,
        retriever=retriever,
        query=candidate.query,
        channels=candidate.channels,
        top_k=candidate.top_k,
        filters=candidate.filters,
    )


def _fan_out_factory(
    scope: Scope,
    candidate: FanOutCandidate,
    *,
    kv: CandidateStore,
    retriever: Retriever | None,
    buckets: list[Scope] | None = None,
    denied_scopes: list[str] | None = None,
) -> CandidateResolver:
    return FanOutResolver(
        scope=scope,
        kv=kv,
        child=candidate.child,
        require_empty=candidate.require_empty,
        require_nonempty=candidate.require_nonempty,
        buckets=buckets,
        denied_scopes=denied_scopes,
    )


_RESOLVER_FACTORIES: dict[str, ResolverFactory] = {
    PredicateCandidate.kind: _predicate_factory,
    IdsCandidate.kind: _ids_factory,
    RecallCandidate.kind: _recall_factory,
    FanOutCandidate.kind: _fan_out_factory,
}


def register_resolver_factory(kind: str, factory: ResolverFactory) -> None:
    """第三方候选源 resolver 工厂注册入口（装配期，fail-fast 不覆盖）。

    装配协议（F10 D4，与 candidate.py 的 ``register_candidate_codec`` 成对）：

    - 注册发生在**装配期**（server 启动 / kernel 组装），运行期注册视为装配
      错误——工厂在 ``build_resolver`` 的查表路径上，运行期改动等于热改判别逻辑；
    - 重复 kind = :class:`ValidationError` 当场拒绝，不静默覆盖（先注册的实现
      被悄悄顶掉会延迟暴露到运行期）；
    - 工厂签名与内置四型一致：``factory(scope, candidate, *, kv, retriever)``
      （fan-out 型可多声明 ``buckets`` / ``denied_scopes`` 收 API 层逐桶
      鉴权后的产物，语义见
      :class:`~control.jobs_impl.candidate_resolver.FanOutResolver`）；
    - 错误统一 :class:`ValidationError`（未知 kind / 非法形态与内置同口径）。
    """
    if not isinstance(kind, str) or not kind:
        raise ValidationError(f"resolver 工厂 kind 必须是非空字符串：{kind!r}")
    if kind in _RESOLVER_FACTORIES:
        raise ValidationError(
            f"resolver 工厂 kind 已注册：{kind!r}（不允许覆盖；合法取值 "
            f"{'/'.join(_RESOLVER_FACTORIES)}）"
        )
    _RESOLVER_FACTORIES[kind] = factory


def build_resolver(
    scope: Scope,
    candidate: CandidateSource | dict[str, Any] | None,
    *,
    kv: CandidateStore,
    retriever: Retriever | None,
    buckets: list[Scope] | None = None,
    denied_scopes: list[str] | None = None,
) -> CandidateResolver:
    """candidate（dict DSL 或数据类）→ resolver（装配期一次性翻译）。

    ``None`` → 默认谓词源（list scope 全量）——现状行为的等价物，不带
    candidate 的 evolve 调用路径零变化。dict 走
    :func:`~common.type_def.candidate_from_dict` 边界解析（非法形态
    ValidationError fail fast，鉴权前拦截）。分派按 ``type(candidate).kind``
    查 ``_RESOLVER_FACTORIES`` 注册表（判别键与 dict DSL 的 ``"type"`` 同源，
    序列化与装配不可能漂移）。``buckets`` / ``denied_scopes`` 非空时随
    kwargs 传入声明了这些参数的工厂（内置仅 fan-out）；其余工厂不收。
    """
    if candidate is None:
        return PredicateResolver(scope=scope, kv=kv)
    if isinstance(candidate, dict):
        candidate = candidate_from_dict(candidate)
    factory = _RESOLVER_FACTORIES.get(getattr(type(candidate), "kind", None))
    if factory is None:
        raise ValidationError(f"未知的候选源类型：{type(candidate).__name__}")
    kwargs: dict[str, Any] = {"kv": kv, "retriever": retriever}
    if buckets is not None:
        kwargs["buckets"] = buckets
    if denied_scopes is not None:
        kwargs["denied_scopes"] = denied_scopes
    return factory(scope, candidate, **kwargs)


async def submit_evolve(
    *,
    scope: Scope,
    mode: EvolveMode,
    channel: Channel,
    candidate: CandidateSource | dict[str, Any] | None,
    kv: CandidateStore,
    scheduler: Scheduler,
    job_factory: JobFactory,
    evolver: Evolver,
    retriever: Retriever | None,
    buckets: list[Scope] | None = None,
    denied_scopes: list[str] | None = None,
    evolver_for_unit: Callable[[MemoryUnit], Evolver | None] | None = None,
    mode_evolvers: dict[EvolveMode, Evolver | ModeEvolverBinding] | None = None,
) -> str | None:
    """立即执行一次演进（唯一执行链条）：candidate → resolver → EvolveJob → submit。

    纯执行件——鉴权在 API 层（入口 + fan-out 逐桶）已完成；``buckets`` /
    ``denied_scopes`` 为 API 层裁决产物，fan-out 型候选源才收（与
    ``build_resolver`` 的 kwargs 透传协议一致）。红线：必须经 EvolveJob
    提交，不得另写执行链条。``evolver_for_unit`` 透传给 EvolveJob 按条目
    所属 profile 分发演进（mem2.0 §2.5.1 改动 1）；None 时整批交 ``evolver``。
    """
    validate_mode_evolver_binding(mode, mode_evolvers or {}, candidate)
    selected = (mode_evolvers or {}).get(mode)
    if selected is not None:
        if isinstance(selected, ModeEvolverBinding):
            evolver = selected.evolver
            if selected.candidate_store is not None:
                kv = selected.candidate_store
            if selected.retriever is not None:
                retriever = selected.retriever
        else:
            evolver = selected
        evolver_for_unit = None
    if mode is EvolveMode.EXPERIENCE_CONSOLIDATE:
        candidate = _experience_candidate(candidate)
    resolver = build_resolver(
        scope,
        candidate,
        kv=kv,
        retriever=retriever,
        buckets=buckets,
        denied_scopes=denied_scopes,
    )
    job = job_factory.get_job(
        JobType.EVOLVE,
        scope=scope,
        mode=mode,
        evolver=evolver,
        resolver=resolver,
        evolver_for_unit=evolver_for_unit,
    )
    return await scheduler.submit(job, channel)
