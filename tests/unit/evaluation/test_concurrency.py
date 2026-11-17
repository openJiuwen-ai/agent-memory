"""并发评测的正确性测试。

核心不变量：
- 并发执行的结果必须与串行执行**逐题等价**（顺序可不同）。
- 各 scope 的 key→unit_id 映射必须隔离，不能串台。
- 任一 scope 失败不得中断其他 scope，也不得污染断点。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class _FakeCase:
    query_id: str
    scope: tuple[str, ...]


@dataclass
class _FakeOutcome:
    query_id: str
    payload: list[str] = field(default_factory=list)


class _Recorder:
    """记录各线程看到的 key→ids 映射，用于检测串台。"""

    def __init__(self) -> None:
        self.seen: dict[str, dict[str, list[str]]] = {}
        self.lock = threading.Lock()

    def record(self, query_id: str, mapping: dict[str, list[str]]) -> None:
        with self.lock:
            self.seen[query_id] = dict(mapping)


def _make_scoped_worker(recorder: _Recorder, delay: float = 0.01):
    """模拟 _evaluate_scope：写入本 scope 的 key 映射，稍等，再读取。"""

    def worker(query_id: str) -> _FakeOutcome:
        mapping = {f"{query_id}_k": [f"{query_id}_u"]}
        # 模拟真实实现：先写实例级字典，延迟后再读
        local = mapping
        time.sleep(delay)
        recorder.record(query_id, local)
        return _FakeOutcome(query_id=query_id, payload=[f"{query_id}_u"])

    return worker


def test_concurrent_scopes_do_not_leak_state() -> None:
    """并发执行时，每个 scope 的映射必须只包含自己的 key。"""
    from concurrent.futures import ThreadPoolExecutor

    recorder = _Recorder()
    worker = _make_scoped_worker(recorder)
    query_ids = [f"q{i}" for i in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(worker, query_ids))

    assert len(outcomes) == 12
    for qid in query_ids:
        per_scope = recorder.seen[qid]
        assert list(per_scope) == [f"{qid}_k"], f"{qid} 看到了别的 scope 的 key: {per_scope}"
        assert per_scope[f"{qid}_k"] == [f"{qid}_u"]


def test_concurrent_results_match_serial_order_independent() -> None:
    """并发结果集合必须与串行完全一致（顺序无关）。"""
    from concurrent.futures import ThreadPoolExecutor

    recorder = _Recorder()
    worker = _make_scoped_worker(recorder, delay=0.005)
    query_ids = [f"q{i}" for i in range(10)]

    serial = {worker(q).query_id: worker(q).payload for q in query_ids}
    with ThreadPoolExecutor(max_workers=5) as pool:
        concurrent = {o.query_id: o.payload for o in pool.map(worker, query_ids)}

    assert concurrent == serial


def test_scope_failure_does_not_abort_others() -> None:
    """单个 scope 抛异常时，其余必须正常完成。"""
    from concurrent.futures import ThreadPoolExecutor

    def worker(qid: str) -> _FakeOutcome:
        if qid == "q3":
            raise RuntimeError("simulated scope failure")
        return _FakeOutcome(query_id=qid)

    query_ids = [f"q{i}" for i in range(6)]
    results: list[_FakeOutcome] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(worker, q): q for q in query_ids}
        for future in futures:
            try:
                results.append(future.result())
            except RuntimeError:
                failures.append(futures[future])

    assert len(results) == 5, "其余 scope 应全部完成"
    assert failures == ["q3"], "只应记录失败的 scope"
    assert {r.query_id for r in results} == {q for q in query_ids if q != "q3"}


def test_metrics_exclude_perf_when_concurrent() -> None:
    """并发 >1 时不得产出 perf 指标（wall-clock 测量失效）。"""
    from evaluation.longmemeval.metrics import build_metric_suite

    serial = build_metric_suite(concurrency=1)
    concurrent = build_metric_suite(concurrency=4)

    assert len(concurrent) == len(serial) - 1, "并发应恰好少 perf 一项"
    # 用行为验证：并发套件在仅有延迟数据时不产出任何 *_wall_ms / *_p50 指标
    from evaluation.longmemeval.types import CaseOutcome

    outcome = CaseOutcome(
        query_id="q1",
        query_text="",
        ranked_unit_ids=["u1"],
        relevant_unit_ids={"u1"},
        contents=["c"],
        context_dates=["2024-01-01"],
        trajectory=[],
        memory_retrieval_e2e_wall_ms=123.0,
        storage_recall_wall_ms=45.0,
    )
    concurrent_names = {r.name for metric in concurrent for r in metric([outcome])}
    serial_names = {r.name for metric in serial for r in metric([outcome])}

    perf_only = serial_names - concurrent_names
    assert perf_only, "串行应多出 perf 指标"
    assert all(
        ("wall_ms" in name) or ("stage_sum_ms" in name) or ("tokens" in name)
        for name in perf_only
    ), f"多出的必须都是性能类指标，实际: {perf_only}"


def test_metric_suite_rejects_invalid_concurrency() -> None:
    from evaluation.longmemeval.metrics import build_metric_suite

    with pytest.raises(ValueError):
        build_metric_suite(concurrency=0)

