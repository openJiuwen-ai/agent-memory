"""EvalHarness——用真实装配把数据集灌入并跑查询，产出可打分的原始观测。

走 ``MemoryAPI`` 公共面（``write`` / ``recall``），因此评测的是「整体功能」而非
检索层孤件：``write`` 经接入/构建/索引落库，``recall`` 经查询理解/多路召回/融合/
披露返回。``key→unit_id`` 映射在写入时捕获（``write`` 返回本次创建的 ``MemoryUnit``），
使数据集的逻辑相关集能映射到真实 ``unit_id`` 再与召回结果比对。

每个 harness 持有一个正式的 ``MemoryRuntime``，评测结束后显式关闭。持久化后端的
运行间隔离由调用入口生成的唯一 scope 保证。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from inspect import signature
from pathlib import Path
from time import perf_counter
from typing import Any

from jiuwen_memory.api import (
    Credentials,
    DeleteMode,
    DeleteSelector,
    MemoryRuntime,
    Surface,
    assemble_runtime,
    build_dev_authenticator,
    new_request_context,
)
from jiuwen_memory.common.type_def import Context
from jiuwen_memory.config.config import Config

from .artifacts import sha256_file, to_jsonable, write_json
from .checkpoint import CheckpointError, load_checkpoint, save_checkpoint
from .no_source import assert_no_source, no_source_extraction
from .types import CaseOutcome, Dataset, MemorySeed, QueryCase

logger = logging.getLogger(__name__)


def _shared_evaluation_kv(api: Any) -> Any:
    """Return the KV already assembled into the API for the raw-source assertion.

    ``MemoryAPI.list`` intentionally excludes ``/messages/`` records, so this
    evaluation-only assertion needs the engine's existing KV port. It does not
    construct a second store: the returned object is the exact stateful dependency
    already shared by the official assembly topology.
    """
    engine = getattr(api, "_engine", None)
    kv = getattr(engine, "_kv", None)
    if not callable(getattr(kv, "scan", None)):
        raise RuntimeError("Mem2.0 evaluation raw KV observation is unavailable")
    return kv


class EvalHarness:
    """装配内核 → 灌语料 → 跑查询 → 采集观测。"""

    def __init__(
        self,
        config: Config | None = None,
        artifact_dir: str | Path | None = None,
        checkpoint_dir: str | Path | None = None,
        checkpoint_fingerprint: str = "",
    ) -> None:
        runtime = assemble_runtime(config=config)
        self._runtime: MemoryRuntime | None = runtime
        self._checkpoint_dir: Path | None = Path(checkpoint_dir) if checkpoint_dir else None
        self._checkpoint_fingerprint = checkpoint_fingerprint
        self._enable_timing = True
        if not hasattr(self, "_failed_scopes"):
            self._failed_scopes = []
        # 本地跨主体评测显式选择 ROOT，不依赖 DEV 缺省提权；数据集 scope 只作目标。
        self._authenticator = build_dev_authenticator(
            identities={
                "evaluation-root": {
                    "actor": {"org": "local", "user": "developer"},
                    "role": "root",
                },
            }
        )
        try:
            self._api = runtime.api
            self._kv = _shared_evaluation_kv(self._api)
            self._key2ids: dict[str, list[str]] = {}
            self._artifact_dir = Path(artifact_dir) if artifact_dir else None
            self._pre_dedup_calls: list[dict] = []
            self._retrieval_audits: list[dict] = []
            if self._artifact_dir is not None:
                self._install_pre_dedup_capture()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release resources owned by the official runtime exactly once."""
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        runtime.close()

    def purge_run_data(self, dataset: Dataset) -> dict[str, int]:
        """Physically delete this run's memory units and derived indexes."""
        scopes: dict[tuple[str, ...], Any] = {}
        for seed in dataset.seeds():
            scopes.setdefault(self._scope_key(seed.scope), seed.scope)
        for case in dataset.queries():
            scopes.setdefault(self._scope_key(case.scope), case.scope)

        deleted_count = 0
        for scope in scopes.values():
            units = self._list_scope_units(scope)
            if units:
                delete = self._api.delete
                deleted = delete(
                    DeleteSelector(
                        unit_ids=[unit.id for unit in units],
                        scope=scope,
                        mode=DeleteMode.PURGE,
                    ),
                    **self._security_kwargs(delete, scope),
                )
                deleted_count += len(deleted)
            remaining = self._list_scope_units(scope)
            if remaining:
                raise RuntimeError(
                    f"evaluation cleanup left {len(remaining)} memory units in scope {scope}"
                )
        return {"scopes": len(scopes), "memory_units": deleted_count}

    def _security_kwargs(self, _method, _target) -> dict:
        """每次调用生成新上下文，不从数据集目标推导 actor。"""
        auth = self._authenticator.authenticate(Credentials(api_key="evaluation-root"))
        return {"security": new_request_context(auth, surface=Surface.INTERNAL)}

    def _install_pre_dedup_capture(self) -> None:
        """Observe extractor output before Evolver dedup without changing it."""
        engine = getattr(self._api, "_engine", None)
        evolver = getattr(engine, "_evolver", None)
        extractor = getattr(evolver, "_extractor", None)
        original_build_units = getattr(extractor, "_build_units", None)
        if not callable(original_build_units):
            raise RuntimeError("cannot install pre-dedup MemoryUnit capture")

        def captured_build_units(candidates, source_units):
            memory_units = original_build_units(candidates, source_units)
            self._pre_dedup_calls.append(
                {
                    "call_index": len(self._pre_dedup_calls),
                    "candidates": to_jsonable(candidates),
                    "source_units": to_jsonable(source_units),
                    "memory_units": to_jsonable(memory_units),
                }
            )
            return memory_units

        setattr(extractor, "_build_units", captured_build_units)

    def ingest(self, seeds: list[MemorySeed]) -> dict[str, list[str]]:
        """逐条写入语料，返回本 scope 的 key→unit_id 映射（可为多条：规约/切分）。

        返回而非写入实例状态，使并发执行时各 scope 天然隔离：
        两个 scope 的 key 可能重名，共享一个字典会互相覆盖。
        """
        add = getattr(self._api, "add", None) or getattr(self._api, "write")
        metadata_arg = (
            "system_metadata" if "system_metadata" in signature(add).parameters else "metadata"
        )
        key2ids: dict[str, list[str]] = {}
        for seed in seeds:
            kwargs = {
                "tags": list(seed.tags),
                "occurred_at": seed.occurred_at,
                metadata_arg: dict(seed.metadata),
            }
            kwargs.update(self._security_kwargs(add, seed.scope))
            units = add(
                seed.content,
                seed.scope,
                **kwargs,
            )
            key2ids[seed.key] = [u.id for u in units]
        return key2ids

    def run_query(
        self,
        case: QueryCase,
        key2ids: dict[str, list[str]] | None = None,
        *,
        enable_timing: bool = True,
    ) -> CaseOutcome:
        if key2ids is None:
            key2ids = {}

        """执行一次 recall，把相关性标注 key 映射为物理 id，连同轨迹打包为观测。"""
        search = getattr(self._api, "search", None) or getattr(self._api, "recall")
        search_security = self._security_kwargs(search, case.scope)
        # Keep both boundaries explicit:
        # - memory_retrieval_e2e_wall_ms wraps the public MemoryAPI search/recall
        #   call that the benchmark client actually waits for;
        # - storage_recall_wall_ms is an internal diagnostic for the single
        #   storage-level multi-channel recall call.
        # The public timer stops before the later inspect() calls used only to
        # expose t_message/t_event to the evaluator.
        engine = getattr(self._api, "_engine", None)
        retriever = getattr(engine, "_retriever", None)
        if retriever is None:
            raise RuntimeError("cannot access retrieval internals for timing capture")
        storage = retriever.storage
        original_recall = storage.recall
        recall_durations_ms: list[float] = []
        storage_recall_outputs: list[object] = []

        def timed_recall(*args, **kwargs):
            started = perf_counter()
            try:
                output = original_recall(*args, **kwargs)
                if self._artifact_dir is not None:
                    storage_recall_outputs.append(to_jsonable(output))
                return output
            finally:
                recall_durations_ms.append((perf_counter() - started) * 1000.0)

        storage.recall = timed_recall
        try:
            retrieval_started = perf_counter()
            try:
                result = search(
                    case.text,
                    Context(case.scope),
                    **search_security,
                    filters=list(case.filters) or None,
                    as_of=case.as_of,
                    top_k=case.top_k,
                    disclosure=case.disclosure,
                    with_trajectory=True,
                )
            finally:
                memory_retrieval_e2e_wall_ms = (perf_counter() - retrieval_started) * 1000.0
        finally:
            storage.recall = original_recall
        if len(recall_durations_ms) != 1:
            raise RuntimeError(
                f"expected exactly one storage recall call, got {len(recall_durations_ms)}"
            )
        storage_recall_wall_ms = recall_durations_ms[0]
        if self._artifact_dir is not None:
            self._retrieval_audits.append(
                {
                    "query_id": case.query_id,
                    "query": case.text,
                    "memory_retrieval_e2e_wall_ms": memory_retrieval_e2e_wall_ms,
                    "storage_recall_call_count": len(storage_recall_outputs),
                    "storage_recall_wall_ms": storage_recall_wall_ms,
                    "storage_recall_output": storage_recall_outputs[0],
                    "public_result_items": to_jsonable(result.items),
                    "trajectory": to_jsonable(result.trajectory),
                }
            )
        unit_message_dates: dict[str, str] = {}
        unit_event_dates: dict[str, str] = {}
        unit_ids = [item.unit_id for item in result.items if item.unit_id]
        if unit_ids:
            units = self._api.inspect(
                unit_ids,
                case.scope,
                **self._security_kwargs(self._api.inspect, case.scope),
            )
            for unit in units:
                temporal = getattr(unit, "temporal", None)
                message_at = getattr(temporal, "t_message", None)
                event_at = getattr(temporal, "t_event", None)
                unit_message_dates[unit.id] = message_at.isoformat() if message_at else ""
                unit_event_dates[unit.id] = event_at.isoformat() if event_at else ""
        relevant_ids = set()
        relevant_key_unit_ids: dict[str, list[str]] = {}
        for key in case.relevant_keys:
            unit_ids = list(key2ids.get(key, []))
            relevant_ids.update(unit_ids)
        source_keys = case.relevant_source_keys or {key: {key} for key in case.relevant_keys}
        for source_key, keys in source_keys.items():
            relevant_key_unit_ids[source_key] = [
                unit_id for key in keys for unit_id in key2ids.get(key, [])
            ]
        return CaseOutcome(
            query_id=case.query_id,
            query_text=case.text,
            ranked_unit_ids=[item.unit_id for item in result.items],
            relevant_unit_ids=relevant_ids,
            contents=[item.content for item in result.items],
            # ``context_dates`` remains the effective conversation date for
            # older evaluators: latest t_message first, legacy t_event fallback.
            context_dates=[
                unit_message_dates.get(item.unit_id, "") or unit_event_dates.get(item.unit_id, "")
                for item in result.items
            ],
            trajectory=list(result.trajectory),
            context_message_dates=[
                unit_message_dates.get(item.unit_id, "") for item in result.items
            ],
            context_event_dates=[unit_event_dates.get(item.unit_id, "") for item in result.items],
            memory_retrieval_e2e_wall_ms=memory_retrieval_e2e_wall_ms,
            storage_recall_wall_ms=storage_recall_wall_ms,
            expected_answer=case.expected_answer,
            metadata=dict(case.metadata),
            relevant_key_unit_ids=relevant_key_unit_ids,
        )

    def evaluate(self, dataset: Dataset, concurrency: int = 1) -> list[CaseOutcome]:
        """按 scope 隔离执行 write→recall；可并行不同 sample 的 scope。"""
        if concurrency <= 0:
            raise ValueError(f"evaluation concurrency must be positive, got {concurrency}")
        # 计时依赖替换 storage.recall（进程级共享对象），并发线程会互相覆盖补丁；
        # 性能指标在并发下也不成立，故仅在串行时启用。
        self._enable_timing = concurrency == 1
        seeds_by_scope: dict[tuple[str, ...], list[MemorySeed]] = {}
        cases_by_scope: dict[tuple[str, ...], list[tuple[int, QueryCase]]] = {}
        for seed in dataset.seeds():
            seeds_by_scope.setdefault(self._scope_key(seed.scope), []).append(seed)
        for index, case in enumerate(dataset.queries()):
            cases_by_scope.setdefault(self._scope_key(case.scope), []).append((index, case))

        jobs = [
            (seeds_by_scope.get(scope_key, []), indexed_cases)
            for scope_key, indexed_cases in cases_by_scope.items()
        ]
        if self._artifact_dir is not None and len(jobs) != 1:
            raise ValueError("--artifact-dir requires exactly one isolated sample scope")
        if self._artifact_dir is not None and concurrency > 1:
            raise ValueError(
                "--artifact-dir 记录跨 scope 共享的采集状态，不支持 --concurrency > 1"
            )
        with no_source_extraction():
            if concurrency == 1:
                indexed_outcomes = []
                for seeds, indexed_cases in jobs:
                    query_ids = [case.query_id for _, case in indexed_cases]
                    cached = self._load_cached(query_ids)
                    if cached is not None:
                        logger.info(
                            "EvalHarness: checkpoint hit, reusing %d result(s): %s",
                            len(cached),
                            query_ids,
                        )
                        by_id = {outcome.query_id: outcome for outcome in cached}
                        indexed_outcomes.extend(
                            (index, by_id[case.query_id])
                            for index, case in indexed_cases
                            if case.query_id in by_id
                        )
                        continue
                    results = self._evaluate_scope_or_fail(seeds, indexed_cases)
                    if results is None:
                        continue
                    self._save_cached(results)
                    indexed_outcomes.extend(results)
            else:
                indexed_outcomes = []
                with ThreadPoolExecutor(
                    max_workers=concurrency,
                    thread_name_prefix="memory-eval",
                ) as executor:
                    futures = [
                        executor.submit(self._evaluate_scope_or_fail, seeds, indexed_cases)
                        for seeds, indexed_cases in jobs
                    ]
                    for future in as_completed(futures):
                        results = future.result()
                        if results is not None:
                            indexed_outcomes.extend(results)
        return [outcome for _, outcome in sorted(indexed_outcomes, key=lambda item: item[0])]

    def _evaluate_scope(
        self,
        seeds: list[MemorySeed],
        indexed_cases: list[tuple[int, QueryCase]],
    ) -> list[tuple[int, CaseOutcome]]:
        key2ids = self.ingest(seeds)
        persisted_units = self._list_persisted_units(indexed_cases[0][1].scope)
        assert_no_source(
            self._kv,
            indexed_cases[0][1].scope,
            persisted_units,
        )
        results = [
            (index, self.run_query(case, key2ids, enable_timing=self._enable_timing))
            for index, case in indexed_cases
        ]
        if self._artifact_dir is not None:
            self._write_scope_artifacts(
                indexed_cases[0][1].scope,
                [case.query_id for _, case in indexed_cases],
                persisted_units,
            )
        return results

    def _list_persisted_units(self, scope) -> list[object]:
        if self._artifact_dir is None:
            return []
        return self._list_scope_units(scope)

    def _list_scope_units(self, scope) -> list[object]:
        offset = 0
        page_size = 100
        units: list[object] = []
        expected_count: int | None = None
        while expected_count is None or offset < expected_count:
            page = self._api.list(
                scope,
                **self._security_kwargs(self._api.list, scope),
                offset=offset,
                limit=page_size,
            )
            items = list(page.items)
            expected_count = int(page.count)
            units.extend(items)
            offset += len(items)
            if not items:
                break
        if expected_count is None or len(units) != expected_count:
            raise RuntimeError(
                f"persisted MemoryUnit pagination mismatch: got {len(units)}, "
                f"expected {expected_count}"
            )
        return units

    def _write_scope_artifacts(
        self,
        scope,
        query_ids: list[str],
        persisted_units: list[object],
    ) -> None:
        if self._artifact_dir is None:
            raise RuntimeError("artifact directory is required for artifact output")
        candidate_count = sum(len(call["candidates"]) for call in self._pre_dedup_calls)
        memory_unit_count = sum(len(call["memory_units"]) for call in self._pre_dedup_calls)
        pre_dedup_path = self._artifact_dir / "pre_dedup_candidates.json"
        persisted_path = self._artifact_dir / "persisted_memory_units.json"
        retrieval_path = self._artifact_dir / "retrieval_audit.json"
        write_json(
            pre_dedup_path,
            {
                "schema_version": 1,
                "capture_boundary": (
                    "ExtractorImpl._build_units input candidates and output MemoryUnits "
                    "before OrchestratingEvolver dedup"
                ),
                "candidate_count": candidate_count,
                "memory_unit_count": memory_unit_count,
                "calls": self._pre_dedup_calls,
            },
        )
        write_json(
            persisted_path,
            {
                "schema_version": 1,
                "capture_boundary": "MemoryAPI.list after ingest and before query recall",
                "scope": scope,
                "count": len(persisted_units),
                "memory_units": persisted_units,
            },
        )
        write_json(
            retrieval_path,
            {
                "schema_version": 1,
                "capture_boundary": (
                    "single PipelineRetriever storage.recall output plus final public result"
                ),
                "queries": self._retrieval_audits,
            },
        )
        files = (pre_dedup_path, persisted_path, retrieval_path)
        write_json(
            self._artifact_dir / "artifact_manifest.json",
            {
                "schema_version": 1,
                "query_ids": query_ids,
                "pre_dedup_candidate_count": candidate_count,
                "pre_dedup_memory_unit_count": memory_unit_count,
                "persisted_memory_unit_count": len(persisted_units),
                "storage_recall_call_count": sum(
                    audit["storage_recall_call_count"] for audit in self._retrieval_audits
                ),
                "files": {
                    path.name: {
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                    for path in files
                },
            },
        )

    # ---------------------------------------------------------- 断点与容错
    def _load_cached(self, query_ids: list[str]):
        """命中返回 outcomes；未启用或不满足前提返回 None（调用方重跑）。"""
        if self._checkpoint_dir is None:
            return None
        return load_checkpoint(
            self._checkpoint_dir, self._checkpoint_fingerprint, query_ids
        )

    def _save_cached(self, results) -> None:
        """落盘失败只记日志，绝不丢弃已算出的结果。"""
        if self._checkpoint_dir is None:
            return
        try:
            save_checkpoint(
                self._checkpoint_dir,
                self._checkpoint_fingerprint,
                [outcome for _, outcome in results],
            )
        except CheckpointError as exc:
            logger.error("EvalHarness: checkpoint save failed, result kept: %s", exc)

    def _evaluate_scope_or_fail(self, seeds, indexed_cases):
        """单 scope 失败不中断整轮评测；返回 None 表示该 scope 被跳过。"""
        try:
            return self._evaluate_scope(seeds, indexed_cases)
        except Exception as exc:
            query_ids = [case.query_id for _, case in indexed_cases]
            logger.error(
                "EvalHarness: scope failed and was skipped; queries=%s error=%s: %s",
                query_ids,
                type(exc).__name__,
                exc,
            )
            self._failed_scopes.append(
                {
                    "query_ids": query_ids,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:2000],
                }
            )
            return None

    @staticmethod
    def _scope_key(scope) -> tuple[str, ...]:
        # Current Mem2.0 exposes ``space``; getattr keeps the evaluation overlay
        # readable against older commits that did not yet have that field.
        return (
            scope.org,
            getattr(scope, "space", ""),
            scope.user,
            scope.agent,
            scope.session,
        )


def purge_run_data(dataset: Dataset, config: Config | None = None) -> dict[str, int]:
    """Reassemble the official runtime and purge one completed evaluation run."""
    harness = EvalHarness(config=config)
    try:
        return harness.purge_run_data(dataset)
    finally:
        harness.close()
