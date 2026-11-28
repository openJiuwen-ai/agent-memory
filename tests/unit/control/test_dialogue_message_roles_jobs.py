# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F16 控制层任务：EvolveJob 离线抽取保序与 MiddleToLongJob 角色过滤/归档单测。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.chunker.chunker_impl.fixed_window_chunker import FixedWindowChunker
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import (
    MESSAGE_ROLE_KEY,
    CandidateGroup,
    CandidateOutcome,
    ChatMessage,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction.dedup_impl.keyword_dedup import KeywordDedup
from jiuwen_memory.construction.evolver import EvolveMode, EvolveResult
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.extractor_impl.keyword_extractor import KeywordExtractor
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeOptions
from jiuwen_memory.construction.index_builder_impl.forward_index_builder import ForwardIndexBuilder
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import (
    HierarchyJobLimits,
    collect_candidates,
)
from jiuwen_memory.control.jobs_impl.hierarchy_incremental import collect_incremental
from jiuwen_memory.control.jobs_impl.middle_to_long_job import MiddleToLongJob
from jiuwen_memory.control.lifecycle_impl.kv_lifecycle_manager import KVLifecycleManager
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv import list_units
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager_impl.composite_store_manager import CompositeStoreManager

pytestmark = pytest.mark.unit


class _StaticResolver:
    def __init__(self, groups: list[CandidateGroup]) -> None:
        self._groups = groups

    async def resolve(self) -> CandidateOutcome:
        return CandidateOutcome(groups=self._groups)


class _RecordingEvolver:
    def __init__(self) -> None:
        self.batches: list[list[MemoryUnit]] = []

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.batches.append(list(units))
        return EvolveResult()


class _RecordingContinuityLLM(LLM):
    def __init__(self, reply: str = '{"results":["true"]}') -> None:
        self.prompts: list[str] = []
        self._reply = reply

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        self.prompts.append(messages[-1].content)
        return self._reply


def _unit(
    uid: str,
    content: str,
    *,
    role: str | None = None,
    t_ingest: datetime,
    middle: bool = False,
) -> MemoryUnit:
    meta: dict[str, str] = {}
    if role is not None:
        meta[MESSAGE_ROLE_KEY] = role
    if middle:
        meta["middle"] = "true"
    return MemoryUnit(
        id=uid,
        scope=Scope(org="o", space="s", user="u", session="s1"),
        tier=MemoryTier.WORKING,
        segments=[Segment(content=content)],
        temporal=Temporal(t_ingest=t_ingest, t_valid=t_ingest),
        system_metadata=meta,
        lifecycle=LifecycleState.ACTIVE,
    )


def test_evolve_job_sorts_role_buckets_ascending_and_preserves_plain_order():
    scope = Scope(org="o", space="s", user="u", session="s1")
    t0 = datetime(2026, 11, 26, 10, 0, 0, tzinfo=UTC)
    # 模拟 list_memory_entries 默认按 t_ingest 降序返回（含 naive/aware 混排）
    u_new = _unit("u2", "我再想想", role="user", t_ingest=t0 + timedelta(seconds=5))
    u_old = _unit(
        "u1",
        "周五提交可以吗？",
        role="assistant",
        t_ingest=datetime(2026, 11, 26, 10, 0, 0),
    )
    evolver = _RecordingEvolver()
    job = EvolveJob(
        scope=scope,
        kv=InMemoryKVStore(),
        evolver=evolver,
        mode=EvolveMode.EXTRACT,
        resolver=_StaticResolver([CandidateGroup(scope=scope, units=[u_new, u_old])]),
    )
    asyncio.run(job.run())
    assert [u.id for u in evolver.batches[0]] == ["u1", "u2"], "带 role 桶必须按 t_ingest 升序送模"

    # 无 role 桶保持原序
    p_new = _unit("p2", "second", t_ingest=t0 + timedelta(seconds=5))
    p_old = _unit("p1", "first", t_ingest=t0)
    evolver_plain = _RecordingEvolver()
    job_plain = EvolveJob(
        scope=scope,
        kv=InMemoryKVStore(),
        evolver=evolver_plain,
        mode=EvolveMode.EXTRACT,
        resolver=_StaticResolver([CandidateGroup(scope=scope, units=[p_new, p_old])]),
    )
    asyncio.run(job_plain.run())
    assert [u.id for u in evolver_plain.batches[0]] == ["p2", "p1"], "无 role 桶保持原序"


def test_middle_to_long_job_archives_pure_instructions_with_zero_llm():
    scope = Scope(org="o", space="s", user="u", session="s1")
    kv = InMemoryKVStore()
    index = ForwardIndexBuilder(CompositeStoreManager(kv=kv))
    lifecycle = KVLifecycleManager(kv)
    llm = _RecordingContinuityLLM()
    evolver = _RecordingEvolver()
    t0 = datetime(2026, 11, 26, 10, 0, 0, tzinfo=UTC)
    index.build(
        [
            _unit("s1", "system prompt", role="SYSTEM", t_ingest=t0, middle=True),
            _unit(
                "d1",
                "dev prompt",
                role="developer",
                t_ingest=t0 + timedelta(seconds=1),
                middle=True,
            ),
        ]
    )
    job = MiddleToLongJob(
        scope=scope,
        kv=kv,
        evolver=evolver,
        lifecycle=lifecycle,
        index=index,
        llm=llm,
    )
    asyncio.run(job.run())
    assert not llm.prompts, "纯指令中期消息不应触发连续性检测 LLM"
    assert not evolver.batches, "纯指令中期消息不应触发抽取"
    stored, _ = list_units(kv, scope, limit=10)
    assert all(u.lifecycle is LifecycleState.ARCHIVED for u in stored), (
        "纯指令须直接归档避免下轮重拉"
    )
    assert {u.system_metadata.get(MESSAGE_ROLE_KEY) for u in stored} == {"SYSTEM", "developer"}


def test_middle_to_long_job_mixed_roles_continuity_extract_and_archive():
    scope = Scope(org="o", space="s", user="u", session="s1")
    kv = InMemoryKVStore()
    msg_kv = InMemoryKVStore()
    manager = CompositeStoreManager(kv=kv, fulltext=InMemoryFulltextStore(WhitespaceTokenizer()))
    index = ForwardIndexBuilder(manager)
    lifecycle = KVLifecycleManager(kv)
    llm = _RecordingContinuityLLM('{"results":["true"]}')
    evolver = OrchestratingEvolver(
        extractor=KeywordExtractor(chunker=FixedWindowChunker(size=200)),
        abstractor=None,
        associator=None,
        index_builder=index,
        storage=manager,
        message_store=msg_kv,
        dedup=KeywordDedup(manager),
        llm=llm,
        dedup_extract=False,
    )
    t0 = datetime(2026, 11, 26, 10, 0, 0, tzinfo=UTC)
    index.build(
        [
            _unit("s1", "sys instruction", role="system", t_ingest=t0, middle=True),
            _unit(
                "a1",
                "周五提交可以吗？",
                role="assistant",
                t_ingest=t0 + timedelta(seconds=1),
                middle=True,
            ),
            _unit(
                "u1",
                "可以，就定周五",
                role="user",
                t_ingest=t0 + timedelta(seconds=2),
                middle=True,
            ),
        ]
    )
    job = MiddleToLongJob(
        scope=scope,
        kv=kv,
        evolver=evolver,
        lifecycle=lifecycle,
        index=index,
        llm=llm,
    )
    asyncio.run(job.run())
    assert len(llm.prompts) == 1
    assert "[ROLE: assistant] 周五提交可以吗？" in llm.prompts[0]
    assert "[ROLE: user] 可以，就定周五" in llm.prompts[0]
    assert "sys instruction" not in llm.prompts[0]

    stored, _ = list_units(kv, scope, limit=20)
    originals = {u.id: u for u in stored if u.id in {"s1", "a1", "u1"}}
    derived = [u for u in stored if u.id not in {"s1", "a1", "u1"}]
    assert all(u.lifecycle is LifecycleState.ARCHIVED for u in originals.values())
    assert originals["a1"].system_metadata.get(MESSAGE_ROLE_KEY) == "assistant"
    assert originals["u1"].system_metadata.get(MESSAGE_ROLE_KEY) == "user"
    assert len(derived) == 2
    assert all(MESSAGE_ROLE_KEY not in u.system_metadata for u in derived), (
        "转长期后的派生事实必须剥离 message_role"
    )


def test_hierarchy_candidates_and_incremental_skip_unattached_instruction_leaves():
    scope = Scope(org="o", space="s", user="u", session="s1")
    kv = InMemoryKVStore()
    index = ForwardIndexBuilder(CompositeStoreManager(kv=kv))
    t0 = datetime(2026, 11, 26, 10, 0, 0, tzinfo=UTC)

    def _snap(uid: str, text: str, offset_min: int, role: str | None = None) -> MemoryUnit:
        ts = t0 + timedelta(minutes=offset_min)
        meta = {MESSAGE_ROLE_KEY: role} if role else {}
        return MemoryUnit(
            id=uid,
            scope=scope,
            tier=MemoryTier.EPISODIC,
            segments=[Segment(content=text)],
            temporal=Temporal(t_ingest=ts, t_valid=ts, t_event=ts),
            system_metadata=meta,
            lifecycle=LifecycleState.ACTIVE,
            hierarchy=HierarchyRef(
                kind=HierarchyKind.TIME,
                role=HierarchyRole.SNAPSHOT,
                span_start=ts,
                span_end=ts,
            ),
        )

    sys_leaf = _snap("s-sys", "system policy", 0, role="system")
    dev_leaf = _snap("s-dev", "dev instruction", 1, role="developer")
    plain_sys = MemoryUnit(
        id="s-plain-sys",
        scope=scope,
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content="plain system prompt")],
        temporal=Temporal(t_ingest=t0, t_valid=t0, t_message=t0),
        system_metadata={MESSAGE_ROLE_KEY: "system"},
        lifecycle=LifecycleState.ACTIVE,
    )
    usr_leaf = _snap("s-usr", "user message", 2, role="user")
    asst_leaf = _snap("s-asst", "assistant reply", 3, role="assistant")
    index.build([sys_leaf, dev_leaf, plain_sys, usr_leaf, asst_leaf])

    opts = HierarchyComposeOptions(
        kind=HierarchyKind.TIME,
        leaf_role=HierarchyRole.SNAPSHOT,
        parent_roles=[HierarchyRole.TIME_SPAN],
        tree_home_scope=scope,
        span_start=t0,
        span_end=t0 + timedelta(hours=1),
    )
    limits = HierarchyJobLimits()

    collected = collect_candidates(
        kv, scope, opts, limits, require_snapshots=True
    )
    assert {u.id for u in collected.leaves.values()} == {"s-usr", "s-asst"}, (
        "未挂父的 system/developer snapshot 不得进入建树候选，防止 reuse_existing 复检失败"
    )
    assert collected.skipped_incomplete_time_count == 0

    inc = collect_incremental(kv, opts, (HierarchyRole.TIME_SPAN,), limits)
    assert {u.id for u in inc.inputs} == {"s-usr", "s-asst"}, (
        "增量建树取数须跳过未挂父的 system/developer snapshot"
    )
    assert inc.skipped_late_count == 0
    assert inc.skipped_incomplete_time_count == 0
    assert inc.needs_rebuild_count == 0
