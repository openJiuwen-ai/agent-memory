"""evolve 立即执行链 + forget 全链（F03 dreaming，控制层纯执行件）。

PEP 边界（S03）改造后本文件只测控制层：

- 立即跑（dreaming=None）：一次性 EvolveJob，candidate 决定候选（含 t_ingest 窗口）；
  ``buckets`` / ``denied_scopes`` 为 API 层裁决产物透传，fan-out 型候选源才收。
- forget 链：mode=FORGET + 谓词源时间窗 → 窗口内 units 送进 evolver。
- build_resolver 边界：candidate → resolver 翻译。

注册 / 注销 / 恢复 / 持续授权的编排测试在 ``tests/unit/api/test_dreaming_coordinator.py``
（API 层 DreamingCoordinator）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    IdsCandidate,
    MemoryUnit,
    PredicateCandidate,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.control.engine_impl.evolve_dispatch import build_resolver, submit_evolve
from jiuwen_memory.control.jobs import JobFactory, JobType
from jiuwen_memory.control.jobs_impl.candidate_resolver import (
    FanOutResolver,
    IdsResolver,
    PredicateResolver,
)
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="coding", user="alice")


class RecordingEvolver(Evolver):
    """记录调用入参的 Evolver 替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[MemoryUnit], EvolveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append((units, mode))
        return EvolveResult(forgotten_ids=[u.id for u in units])


def _make_unit(
    uid: str, scope: Scope, *, t_ingest: datetime | None = None
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=scope,
        segments=[Segment(content=f"content of {uid}")],
        temporal=Temporal(t_ingest=t_ingest),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert(kv: InMemoryKVStore, unit: MemoryUnit) -> None:
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))


def _factory(kv: InMemoryKVStore) -> JobFactory:
    from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob

    factory = JobFactory()
    factory.register(
        JobType.EVOLVE,
        lambda scope, **kw: EvolveJob(scope=scope, kv=kv, **kw),
    )
    return factory


async def _submit(
    kv: InMemoryKVStore,
    scheduler: InProcessScheduler,
    evolver: RecordingEvolver,
    *,
    mode: EvolveMode = EvolveMode.FORGET,
    candidate=None,
    buckets: list[Scope] | None = None,
    denied_scopes: list[str] | None = None,
):
    return await submit_evolve(
        scope=_SCOPE,
        mode=mode,
        channel=Channel.BACKGROUND,
        candidate=candidate,
        kv=kv,
        scheduler=scheduler,
        job_factory=_factory(kv),
        evolver=evolver,
        retriever=None,
        buckets=buckets,
        denied_scopes=denied_scopes,
    )


# ---------------------------------------------------------------------------
# 立即跑（dreaming=None，纯执行链）
# ---------------------------------------------------------------------------


class TestImmediateRun:
    def test_none_dreaming_runs_once_with_default_predicate_source(self) -> None:
        kv = InMemoryKVStore()
        scheduler = InProcessScheduler()
        evolver = RecordingEvolver()
        _insert(kv, _make_unit("u1", _SCOPE))
        _insert(kv, _make_unit("u2", _SCOPE))

        job_id = asyncio.run(_submit(kv, scheduler, evolver))

        assert job_id is not None
        assert scheduler.status(job_id).status.value == "succeeded"
        units, mode = evolver.calls[0]
        assert {u.id for u in units} == {"u1", "u2"}
        assert mode == EvolveMode.FORGET

    def test_candidate_ids_source_point_read(self) -> None:
        """立即跑 + 点名源：只把点名的 units 送进 evolver。"""
        kv = InMemoryKVStore()
        scheduler = InProcessScheduler()
        evolver = RecordingEvolver()
        _insert(kv, _make_unit("u1", _SCOPE))
        _insert(kv, _make_unit("u2", _SCOPE))

        asyncio.run(
            _submit(kv, scheduler, evolver, candidate=IdsCandidate(unit_ids=["u1"]))
        )

        units, _ = evolver.calls[0]
        assert [u.id for u in units] == ["u1"]

    def test_candidate_dict_dsl_accepted_at_boundary(self) -> None:
        """candidate 支持 dict DSL（HTTP/SDK 边界形态）。"""
        kv = InMemoryKVStore()
        scheduler = InProcessScheduler()
        evolver = RecordingEvolver()
        _insert(kv, _make_unit("u1", _SCOPE))

        asyncio.run(
            _submit(
                kv, scheduler, evolver,
                candidate={"type": "ids", "unit_ids": ["u1"]},
            )
        )

        assert [u.id for u in evolver.calls[0][0]] == ["u1"]


# ---------------------------------------------------------------------------
# forget 全链（mode=FORGET × 谓词源时间窗）
# ---------------------------------------------------------------------------


class TestForgetChain:
    def test_forget_with_t_ingest_window_selects_fresh_only(self) -> None:
        """mode=FORGET + window：只有窗口内新写入的记忆进入遗忘裁决。"""
        kv = InMemoryKVStore()
        scheduler = InProcessScheduler()
        evolver = RecordingEvolver()
        fresh = _make_unit("fresh", _SCOPE, t_ingest=_now())
        stale = _make_unit("stale", _SCOPE, t_ingest=_now() - timedelta(hours=2))
        _insert(kv, fresh)
        _insert(kv, stale)

        asyncio.run(
            _submit(
                kv, scheduler, evolver,
                candidate=PredicateCandidate(window=3600),
            )
        )

        units, mode = evolver.calls[0]
        assert mode == EvolveMode.FORGET
        assert [u.id for u in units] == ["fresh"], "t_ingest 窗口下推排除 stale"


# ---------------------------------------------------------------------------
# build_resolver 边界
# ---------------------------------------------------------------------------


class TestBuildResolver:
    def test_none_maps_to_default_predicate(self) -> None:
        resolver = build_resolver(_SCOPE, None, kv=InMemoryKVStore(), retriever=None)
        assert isinstance(resolver, PredicateResolver)

    def test_fanout_from_dict_dsl(self) -> None:
        resolver = build_resolver(
            _SCOPE,
            {"type": "fan_out", "child": {"filters": None, "window": 60}},
            kv=InMemoryKVStore(),
            retriever=None,
        )
        assert isinstance(resolver, FanOutResolver)

    def test_ids_from_dict_dsl(self) -> None:
        resolver = build_resolver(
            _SCOPE,
            {"type": "ids", "unit_ids": ["a"]},
            kv=InMemoryKVStore(),
            retriever=None,
        )
        assert isinstance(resolver, IdsResolver)

    def test_recall_without_retriever_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Retriever"):
            build_resolver(
                _SCOPE,
                {"type": "recall", "query": {"text": "x"}},
                kv=InMemoryKVStore(),
                retriever=None,
            )

    def test_unknown_candidate_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_resolver(
                _SCOPE, object(), kv=InMemoryKVStore(), retriever=None  # type: ignore[arg-type]
            )
