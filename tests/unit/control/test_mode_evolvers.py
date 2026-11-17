"""专用经验 mode 复用通用候选和 Job 链，绑定不经条目 profile。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import IdsCandidate, MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction import EvolveMode, EvolveResult
from jiuwen_memory.control.engine_impl.evolve_dispatch import (
    ModeEvolverBinding,
    build_mode_evolvers,
    mode_candidate_scopes,
    submit_evolve,
)
from jiuwen_memory.control.jobs import JobFactory, JobType
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


class _Evolver:
    def __init__(self) -> None:
        self.calls: list[tuple[list[MemoryUnit], EvolveMode]] = []

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append((units, mode))
        return EvolveResult(created_ids=[unit.id for unit in units])


def _run(candidate=None):
    scope = Scope(org="acme", space="shared")
    kv = InMemoryKVStore()
    for uid, kind in (("ordinary", "chat"), ("experience", "experience"), ("skill", "skill")):
        unit = MemoryUnit(
            id=uid, scope=scope, segments=[Segment(content=uid)],
            system_metadata={"memory_type": kind},
        )
        kv.insert(scope, memory_key(uid), dumps(unit))
    default = _Evolver()
    experience = _Evolver()
    factory = JobFactory()
    factory.register(JobType.EVOLVE, lambda scope, **kw: EvolveJob(scope, kv, **kw))

    def forbidden_profile_lookup(_unit):
        raise AssertionError("专用 mode 不应按条目选择 profile")

    scheduler = InProcessScheduler()
    job_id = asyncio.run(submit_evolve(
        scope=scope, mode=EvolveMode.EXPERIENCE_CONSOLIDATE,
        channel=Channel.BACKGROUND, candidate=candidate, kv=kv,
        scheduler=scheduler, job_factory=factory, evolver=default, retriever=None,
        evolver_for_unit=forbidden_profile_lookup,
        mode_evolvers={EvolveMode.EXPERIENCE_CONSOLIDATE: experience},
    ))
    return default, experience, scheduler.status(job_id)


@pytest.mark.parametrize("candidate", [
    None,
    {"type": "predicate", "filters": {"OR": [
        {"system_metadata.memory_type": "experience"},
        {"system_metadata.memory_type": "chat"},
    ]}},
    IdsCandidate(unit_ids=["ordinary", "experience", "skill"]),
])
def test_experience_mode_only_processes_experience_without_profile_lookup(candidate) -> None:
    default, experience, info = _run(candidate)

    assert default.calls == [], "专用 mode 不应落到普通 Evolver"
    assert len(experience.calls) == 1, "每个候选桶仅调用一次处理实例"
    units, mode = experience.calls[0]
    assert [unit.id for unit in units] == ["experience"], "普通与技能条目均不参与经验整理"
    assert mode is EvolveMode.EXPERIENCE_CONSOLIDATE
    assert info.detail["created_ids"] == "experience", "结果沿用通用 Job 回显"


def test_experience_mode_requires_explicit_mode_binding_before_job_submission() -> None:
    with pytest.raises(ValidationError, match="mode_evolvers"):
        asyncio.run(submit_evolve(
            scope=Scope(org="acme"), mode=EvolveMode.EXPERIENCE_CONSOLIDATE,
            channel=Channel.BACKGROUND, candidate=None, kv=InMemoryKVStore(),
            scheduler=InProcessScheduler(), job_factory=JobFactory(),
            evolver=_Evolver(), retriever=None,
        ))


def test_mode_evolvers_builds_named_shared_instance() -> None:
    instance = _Evolver()
    config = SimpleNamespace(
        params={"mode_evolvers": {"experience_consolidate": "curation"}}, ctx=object(),
    )
    with patch(
        "jiuwen_memory.control.engine_impl.evolve_dispatch.EvolverProducer.build_named",
        return_value=instance,
    ) as build:
        result = build_mode_evolvers(config)

    assert result == {EvolveMode.EXPERIENCE_CONSOLIDATE: ModeEvolverBinding(instance)}
    build.assert_called_once_with("curation", config.ctx)


@pytest.mark.parametrize("raw", [None, [], {"wrong-mode": "experience"}, {"forget": ""}])
def test_mode_evolvers_rejects_invalid_binding_at_assembly(raw) -> None:
    config = SimpleNamespace(params={"mode_evolvers": raw}, ctx=object())
    with pytest.raises(ValidationError):
        build_mode_evolvers(config)


def test_mode_bound_domain_requires_explicit_retriever_for_recall() -> None:
    with pytest.raises(ValidationError, match="同源 retriever"):
        asyncio.run(submit_evolve(
            scope=Scope(org="acme"), mode=EvolveMode.EXPERIENCE_CONSOLIDATE,
            channel=Channel.BACKGROUND, candidate={"type": "recall", "query": {"text": "经验"}},
            kv=InMemoryKVStore(), scheduler=InProcessScheduler(), job_factory=JobFactory(),
            evolver=_Evolver(), retriever=None,
            mode_evolvers={EvolveMode.EXPERIENCE_CONSOLIDATE: ModeEvolverBinding(
                _Evolver(), candidate_store=InMemoryKVStore(),
            )},
        ))


def test_mode_bound_recall_uses_explicit_retriever_and_candidate_store() -> None:
    scope = Scope(org="acme", space="experience")
    ordinary = InMemoryKVStore()
    experience = InMemoryKVStore()
    unit = MemoryUnit(
        id="experience", scope=scope, segments=[Segment(content="经验")],
        system_metadata={"memory_type": "experience"},
    )
    experience.insert(scope, memory_key(unit.id), dumps(unit))
    calls = []

    class Retriever:
        @staticmethod
        def retrieve(target, query):
            calls.append((target, query))
            return SimpleNamespace(items=[SimpleNamespace(unit_id=unit.id)])

    selected = _Evolver()
    factory = JobFactory()
    factory.register(JobType.EVOLVE, lambda scope, **kw: EvolveJob(scope, ordinary, **kw))
    asyncio.run(submit_evolve(
        scope=scope, mode=EvolveMode.EXPERIENCE_CONSOLIDATE,
        channel=Channel.BACKGROUND, candidate={"type": "recall", "query": {"text": "经验"}},
        kv=ordinary, scheduler=InProcessScheduler(), job_factory=factory,
        evolver=_Evolver(), retriever=None,
        mode_evolvers={EvolveMode.EXPERIENCE_CONSOLIDATE: ModeEvolverBinding(
            selected, candidate_store=experience, retriever=Retriever(),
        )},
    ))

    assert len(calls) == 1, "只调用显式绑定的检索器"
    assert [unit.id for unit in selected.calls[0][0]] == ["experience"], "ID回读使用经验真源"
    assert mode_candidate_scopes(
        EvolveMode.EXPERIENCE_CONSOLIDATE,
        {EvolveMode.EXPERIENCE_CONSOLIDATE: ModeEvolverBinding(selected, experience)},
        ordinary.scopes,
    ) == [scope], "fan-out枚举使用同一候选真源"
