# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MiddleToLongJob——中期转长期任务。

Job 内完成：list 候选 → 连续性检测切批 → 串行/并发调
``evolver.evolve(batch, EXTRACT)`` → 归档原文。evolver 不做任何修改。

- ``interval>0`` 作为定时任务声明，submit 时注册到 per scope TimerWheel，
  Timer 协程周期生成实例入队，每个实例跑一次 ``run()`` 即返回；
- 退出：``run()`` 扫到无候选时返回 ``is_done="true"``，Scheduler 标记
  parent entry ``is_done``，entries 全 ``is_done`` 时 Timer 协程退出；
- 非破坏式归档：原文走 ``lifecycle.transition(ARCHIVED)`` + 派生索引退出检索。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.lock import LockProducer, LockProvider, LockTimeoutError
from jiuwen_memory.common.log import get_logger, scope_for_log
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.construction import EvolveMode, Evolver
from jiuwen_memory.construction.evolver import EvolverProducer
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.control.jobs import Job, JobFactory, JobFactoryProducer, JobType
from jiuwen_memory.control.lifecycle import LifecycleManager, LifecycleProducer
from jiuwen_memory.control.types import JobInfo, JobStatus
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode

logger = get_logger(__name__)


# ---- 连续性检测 prompt ---------------------------------------------------- #

_CONTINUITY_SYSTEM_PROMPT = """Role
你是对话边界检测专家，严格判定历史对话与新对话的语义连续性，仅按规则输出指定格式纯 JSON 字符串。

Definitions
判断规则：
- 话题高度相关、上下文承接、语义有关联或没有历史对话 → 判定连续，返回 true
- 完全切换全新话题、无语义关联、场景彻底割裂、无上下文承接 → 判定不连续，返回 false
- 弱关联延伸、同主题拓展追问、同领域衍生提问 → 统一判定连续，返回 true
- 无关闲聊插入、跨领域无衔接跳转、无任何逻辑语义关联 → 强制判定不连续，返回 false

Input Data
历史对话
{old_conversation}
新对话
{new_conversation}

Output Data
仅输出无空格、无换行、无解释、无 Markdown 的纯紧凑 JSON 字符串，
固定格式：{{"results":["true"]}} 或 {{"results":["false"]}}。
/no_think
"""

_MAX_RETRIES = 3

#: scope 级锁键末段——同 scope 任意时刻只有一个实例跑 MiddleToLongJob.run。
#: 锁键最终形如 ``am:lock:v1:{scope 五段}:middle_to_long``。
_LOCK_NAME = "middle_to_long"


class MiddleToLongJob(Job):
    """中期转长期任务。"""

    def __init__(
        self,
        scope: Scope,
        storage: Storage,
        evolver: Evolver,
        lifecycle: LifecycleManager,
        index: IndexBuilder,
        llm: LLM,
        max_fetch: int = 100,
        batch_size: int = 10,
        concurrency: int = 4,
        interval: int = 50,
        *,
        lock: LockProvider | None = None,
    ) -> None:
        super().__init__(scope=scope, interval=interval)
        self._storage = storage
        self._evolver = evolver
        self._lifecycle = lifecycle
        self._index = index
        self._llm = llm
        self._max_fetch = max_fetch
        self._batch_size = batch_size
        self._concurrency = max(1, concurrency)
        self._lock = lock

    # ---- 连续性检测 ----

    @staticmethod
    def _format_for_continuity(unit: MemoryUnit) -> str:
        """取 unit 第一段 content 作为连续性检测输入。"""
        return unit.segments[0].content if unit.segments else ""

    # ---- 入口 ----

    async def run(self) -> JobInfo:
        """执行任务。

        若装配期注入了 :class:`LockProvider`，以 scope 级锁围栏临界区——多实例同
        scope 同时触发中期记忆时，只有一个实例进入 ``_run_inner``，其余实例在
        ``LockTimeoutError`` 路径上跳过本次 tick（返回 ``SUCCEEDED`` + ``skipped_due_to_lock``，
        不标 ``is_done``，下个 tick 继续重试）。

        ``wait_timeout_ms=0``：只试一次不等待，避免 drain 协程空等挤占 scheduler。
        Timer 节拍本身有抖动，下一 tick 自然串行。

        ``LockProvider`` 未注入（单实例 / 本地开发）时直接走原路径，无锁开销。
        """
        if self._lock is None:
            return await self._run_inner()
        try:
            async with self._lock.guard(
                self.scope, _LOCK_NAME,
                wait_timeout_ms=0,
            ) as handle:
                if handle.lost.is_set():
                    logger.warning(
                        "MiddleToLongJob: lock lost at start, skip tick, scope=%s",
                        scope_for_log(self.scope),
                    )
                    return JobInfo(
                        scope=self.scope,
                        status=JobStatus.SUCCEEDED,
                        detail={"skipped_due_to_lock": "true"},
                    )
                return await self._run_inner()
        except LockTimeoutError:
            logger.info(
                "MiddleToLongJob: lock held elsewhere, skip tick, scope=%s",
                scope_for_log(self.scope),
            )
            return JobInfo(
                scope=self.scope,
                status=JobStatus.SUCCEEDED,
                detail={"skipped_due_to_lock": "true"},
            )

    async def _run_inner(self) -> JobInfo:
        """临界区主体：list 候选 → 连续性检测切批 → evolve 抽取 → 归档原文。"""
        candidates = await self._list_working_units()
        if not candidates:
            # 候选转完——通知 Scheduler 停止下一轮触发
            logger.info(
                "MiddleToLongJob: no middle candidates, scope=%s, exit",
                scope_for_log(self.scope),
            )
            return JobInfo(
                scope=self.scope,
                status=JobStatus.SUCCEEDED,
                detail={"is_done": "true", "reason": "no candidates"},
            )

        logger.info(
            "MiddleToLongJob: processing %d candidates (max_fetch=%d), scope=%s",
            len(candidates),
            self._max_fetch,
            scope_for_log(self.scope),
        )

        batches = await self._split_by_continuity(candidates)

        created_ids: list[str] = []
        processed_units: list[MemoryUnit] = []

        if self._concurrency <= 1:
            # 串行：失败批次隔离（不收集 unit），原文保留 ACTIVE+WORKING 下轮重试
            for batch in batches:
                try:
                    r = await asyncio.to_thread(
                        self._evolver.evolve, batch, EvolveMode.EXTRACT
                    )
                    created_ids.extend(r.created_ids)
                    processed_units.extend(batch)
                except Exception as e:
                    logger.warning("middle batch failed, originals preserved: %s", e)
        else:
            results = await self._gather_batches(batches)
            for batch, r in zip(batches, results):
                if isinstance(r, Exception):
                    logger.warning("middle batch failed, originals preserved: %s", r)
                    continue
                created_ids.extend(r.created_ids)
                processed_units.extend(batch)

        # 归档转换成功的原文（非破坏式：真源留 ARCHIVED，仅退出检索）
        if processed_units:
            await self._archive_originals(processed_units)

        return JobInfo(
            scope=self.scope,
            status=JobStatus.SUCCEEDED,
            detail={
                "created_ids": ",".join(created_ids),
                "candidates": str(len(candidates)),
                "batches": str(len(batches)),
                "processed": str(len(processed_units)),
                "mode": EvolveMode.EXTRACT.value,
            },
        )

    async def _check_continuity(self, prev: MemoryUnit, cur: MemoryUnit) -> str:
        """单次连续性检测：返回 'true' / 'false'。3 次重试，失败默认 'true'（合并）。

        ``LLM.chat`` 是同步方法，网络 IO 可能阻塞事件循环——用 ``to_thread``
        推到独立线程跑（连续性检测是串行依赖，无法 gather 并发）。

        小模型可能返回单引号、无引号 key 或带 Markdown 包装的伪 JSON，
        ``json.loads`` 失败时用正则 fallback 提取首个 true/false。
        """
        prompt = _CONTINUITY_SYSTEM_PROMPT.replace(
            "{old_conversation}", self._format_for_continuity(prev)
        ).replace(
            "{new_conversation}", self._format_for_continuity(cur)
        )

        for attempt in range(_MAX_RETRIES):
            resp = None
            try:
                resp = await asyncio.to_thread(
                    self._llm.chat,
                    [ChatMessage(role="user", content=prompt)],
                    temperature=0,
                    max_tokens=32,
                )
                try:
                    result = json.loads(resp)
                    # LLM 偶尔返回 {"results":[true]}（bool）而非字符串——强制 str
                    # 归一化，否则后续 `cont == "true"` 比较会错误切批。
                    return str(result["results"][0]).lower()
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    m = re.search(r"\b(true|false)\b", resp.lower())
                    if m:
                        return m.group(1)
                    raise
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
                if attempt < _MAX_RETRIES - 1:
                    continue
                logger.warning(
                    "continuity check failed after %d retries, default true: %s "
                    "(resp=%r)",
                    _MAX_RETRIES,
                    e,
                    resp,
                )
                return "true"
        return "true"

    async def _split_by_continuity(
        self, candidates: list[MemoryUnit]
    ) -> list[list[MemoryUnit]]:
        """串行连续性检测 + 批次切分。

        ``_list_working_units`` 已按 ``t_ingest`` 升序取最老 max_fetch 条；
        遍历，连续（true）且当前批未达 ``batch_size`` → 留在当前批，否则切批。
        """
        if not candidates:
            return []
        batches: list[list[MemoryUnit]] = [[candidates[0]]]
        prev = candidates[0]
        for cur in candidates[1:]:
            cont = await self._check_continuity(prev, cur)
            if cont == "true" and len(batches[-1]) < self._batch_size:
                batches[-1].append(cur)
            else:
                batches.append([cur])
            prev = cur
        return batches

    # ---- 候选拉取 ----

    async def _list_working_units(self) -> list[MemoryUnit]:
        """filter tier=WORKING + lifecycle=ACTIVE + metadata["middle"]="true"
        → 按 t_ingest 升序取最老 max_fetch 条。

        ``kv.scan`` 是同步阻塞 IO，推到独立线程跑避免阻塞事件循环。
        """
        page = await asyncio.to_thread(self._storage.list, self.scope, limit=1_000_000)
        units = page.items
        candidates = []
        for u in units:
            if u.tier != MemoryTier.WORKING:
                continue
            if u.lifecycle != LifecycleState.ACTIVE:
                continue
            if u.system_metadata.get("middle") != "true":
                continue
            candidates.append(u)
        candidates.sort(
            key=lambda unit: (
                unit.temporal.t_ingest or datetime.min.replace(tzinfo=timezone.utc),
                unit.id,
            )
        )
        return candidates[: self._max_fetch]

    # ---- 并发执行批 ----

    async def _gather_batches(self, batches):
        """并发跑所有批，``Semaphore`` 限流，``return_exceptions`` 收集失败。"""
        sem = asyncio.Semaphore(self._concurrency)

        async def _run_one(batch):
            async with sem:
                return await asyncio.to_thread(
                    self._evolver.evolve, batch, EvolveMode.EXTRACT
                )

        return await asyncio.gather(
            *(_run_one(b) for b in batches), return_exceptions=True
        )

    # ---- 原文归档 ----

    async def _archive_originals(self, units: list[MemoryUnit]) -> None:
        """转换成功的原文走 ARCHIVED + 退出检索——可审计可恢复。

        ``lifecycle.transition`` + 索引移除都是同步阻塞 IO，推到独立
        线程跑避免阻塞事件循环。两者顺序依赖，不能并行。
        """
        unit_ids = [u.id for u in units]
        await asyncio.to_thread(
            self._lifecycle.transition, self.scope, unit_ids, LifecycleState.ARCHIVED
        )
        # 非破坏式：真源留 ARCHIVED 供审计，仅退出检索。
        await asyncio.to_thread(self._remove_from_search, units)
        logger.info("MiddleToLongJob: %d originals archived", len(units))

    def _remove_from_search(self, units: list[MemoryUnit]) -> None:
        """``asyncio.to_thread`` 只接 callable + args，关键字参数抽成同步方法包装。"""
        self._index.remove(units, mode=IndexRemoveMode.SOFT)


# -- Spec + builder + Producer 注册 ---------------------------------------- #


@dataclass
class MiddleToLongJobSpec:
    """MiddleToLongJob 装配期固化的部分——不含 scope（write 时补）。

    依赖、业务参数与定时周期 interval 经 :class:`JobFactoryProducer` 装配期
    固化。运行时 :meth:`with_scope` 补 scope 生成完整 Job 实例。
    """

    storage: Storage
    evolver: Evolver
    lifecycle: LifecycleManager
    index: IndexBuilder
    llm: LLM
    max_fetch: int = 100
    batch_size: int = 10
    concurrency: int = 4
    interval: int = 50
    #: 多实例部署时注入，scope 级锁围栏临界区；None（未配 ``lock`` 段）走原路径不加锁。
    lock: LockProvider | None = None

    def with_scope(self, scope: Scope, **kwargs) -> MiddleToLongJob:
        """生成完整 Job 实例——``kwargs`` 透传运行时参数（``evolver`` / ``index`` / ``interval``）。

        ``evolver`` / ``index`` 用于多 profile 适配——``CloudEngine._write_middle_path``
        注入 binding 的，保证 Job 归档原文时 ``index.remove`` 调对正确的 index。
        ``interval`` 经 write metadata 透传，覆盖 Spec 装配期默认。
        """
        evolver = kwargs.pop("evolver", None) or self.evolver
        index = kwargs.pop("index", None) or self.index
        interval = int(kwargs.pop("interval", None) or self.interval)
        return MiddleToLongJob(
            scope=scope,
            storage=self.storage,
            evolver=evolver,
            lifecycle=self.lifecycle,
            index=index,
            llm=self.llm,
            max_fetch=self.max_fetch,
            batch_size=self.batch_size,
            concurrency=self.concurrency,
            interval=interval,
            lock=self.lock,
            **kwargs,
        )


def _build_middle_to_long_job_spec(config) -> MiddleToLongJobSpec:
    """装配期固化 MiddleToLongJob 的依赖与业务参数——返回 Spec dataclass。"""
    vector_on = config.get("vector_enabled", True)
    ib_default = "hybrid" if vector_on else "fulltext"
    # lock 段未配置时不调 dep、不报错——LockProducer 不设默认实现，缺配置直接 dep 会
    # 抛 ValidationError。这里探测后再装配，让单实例 / 本地开发无需配 lock 段。
    # 多实例部署显式配 lock 段才生效，符合 F06「避免静默退化为单机锁」精神。
    lock = None
    if config.params.get("lock") is not None:
        lock = LockProducer.dep(config)
    return MiddleToLongJobSpec(
        storage=StorageProducer.resolve(config),
        evolver=EvolverProducer.dep(config, default="orchestrating"),
        lifecycle=LifecycleProducer.dep(config, default="kv"),
        index=IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        llm=LlmProducer.dep(config, default="echo"),
        max_fetch=int(config.get("middle_max_fetch", 100)),
        batch_size=int(config.get("middle_batch_size", 10)),
        concurrency=int(config.get("middle_concurrency", 4)),
        interval=int(config.get("middle_interval", 50)),
        lock=lock,
    )


@JobFactoryProducer.register("default")
def _build_job_factory(config):
    """装配 JobFactory 并注册各 Job 类型的 Spec。"""
    from jiuwen_memory.control.jobs_impl.evolve_job import _build_evolve_job_spec

    factory = JobFactory()
    factory.register(
        JobType.MIDDLE_TO_LONG,
        _build_middle_to_long_job_spec(config).with_scope,
    )
    factory.register(
        JobType.EVOLVE,
        _build_evolve_job_spec(config).with_scope,
    )
    return factory
