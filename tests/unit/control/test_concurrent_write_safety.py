# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真异步引入的跨线程共享点：信号计数、ContextVar 归属、内存 KV（F10 决策 9）。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope, Segment, Temporal
from jiuwen_memory.construction.evolver import EvolveResult, GroupedExtractResult
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.types import BatchWriteItem
from jiuwen_memory.control.write_orchestrator_impl.signals import (
    DegradeEvent,
    WriteSignals,
)
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="u1")


def _unit(unit_id: str, content: str) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(),
    )


class _Ingestor:
    @staticmethod
    def ingest(payloads):
        return [
            _unit(f"src-{payload.data.decode()}", payload.data.decode()) for payload in payloads
        ]


class _Evolver:
    """合并抽取：每个派生 unit 的来源只记到自己那条原文。"""

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier

    @staticmethod
    def supports_grouped_extract() -> bool:
        return True

    @staticmethod
    def evolve(units, mode=None):  # pragma: no cover - 合并路径不走这里
        raise AssertionError("merged path must use evolve_grouped")

    def evolve_grouped(self, units):
        # 两个线程同时进入，强制归属 ContextVar 交错写入。
        self._barrier.wait(timeout=5)
        derived = _unit(f"derived-{units[0].id}", "派生")
        return GroupedExtractResult(
            result=EvolveResult(created_ids=[derived.id], created_units=[derived]),
            sources={derived.id: [units[0].id]},
        )


class _Builder:
    @staticmethod
    def supports_grouped_build() -> bool:
        return True

    @staticmethod
    def build(units):  # pragma: no cover - 分组路径不走这里
        raise AssertionError("grouped build expected")

    @staticmethod
    def build_grouped(units):
        from jiuwen_memory.construction.index_builder import UnitCommitState, UnitCommitStatus

        return [
            UnitCommitStatus(unit_id=unit.id, state=UnitCommitState.COMMITTED) for unit in units
        ]


def test_signals_counters_thread_safe() -> None:
    signals = WriteSignals()
    workers = 8
    per_worker = 200

    def worker() -> None:
        for _ in range(per_worker):
            signals.record(
                DegradeEvent.MERGE_FALLBACK,
                scope=_SCOPE,
                batch_size=1,
                reason="test",
            )

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert signals.counters().get("merge_fallback") == workers * per_worker, (
        "降级计数在多线程下不能丢计数"
    )


def test_grouped_extract_context_isolated_across_threads() -> None:
    barrier = threading.Barrier(2)
    engine = InMemoryEngine(
        ingestor=_Ingestor(),
        index_builder=_Builder(),
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=None,
        evolver=_Evolver(barrier),
        lifecycle=None,
    )

    results: dict[str, list[str]] = {}
    errors: list[BaseException] = []

    def write(content: str) -> None:
        try:
            outcomes = asyncio.run(
                engine.write_group(
                    [
                        BatchWriteItem(
                            content=content,
                            scope=_SCOPE,
                            system_metadata={"infer": "true"},
                        )
                    ],
                    merge_extract=True,
                )
            )
            results[content] = [unit.id for unit in outcomes[0].units]
        except BaseException as exc:  # noqa: BLE001 —— 线程内异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(content,)) for content in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, f"并发写不得抛错: {errors}"
    assert results["a"] == ["derived-src-a"], "线程 a 的归属不得被线程 b 覆盖"
    assert results["b"] == ["derived-src-b"], "线程 b 的归属不得被线程 a 覆盖"


def test_in_memory_kv_concurrent_put() -> None:
    store = InMemoryKVStore()
    workers = 8
    per_worker = 50

    def worker(number: int) -> None:
        for index in range(per_worker):
            store.insert(_SCOPE, f"/probe/{number}-{index}", b"v")

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    entries = store.scan(_SCOPE, prefix="/probe/")
    assert len(entries) == workers * per_worker, "并发 put 不能丢条目"
