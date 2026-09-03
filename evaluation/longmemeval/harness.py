"""EvalHarness——用真实装配把数据集灌入并跑查询，产出可打分的原始观测。

走 ``MemoryAPI`` 公共面（``write`` / ``recall``），因此评测的是「整体功能」而非
检索层孤件：``write`` 经接入/构建/索引落库，``recall`` 经查询理解/多路召回/融合/
披露返回。``key→unit_id`` 映射在写入时捕获（``write`` 返回本次创建的 ``MemoryUnit``），
使数据集的逻辑相关集能映射到真实 ``unit_id`` 再与召回结果比对。

每个 harness 持有一套独立的内核（``build_kernel``），天然隔离——不同 Config 的对比
跑分各起一套，互不污染。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from inspect import signature
from pathlib import Path
from time import perf_counter
from typing import Dict, List, Optional

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.type_def import Context
from jiuwen_memory.config.config import Config

try:
    from jiuwen_memory.common.security.legacy import legacy_request_context
except ImportError:  # Older Mem2.0 commits still accept identity= directly.
    legacy_request_context = None

from .artifacts import sha256_file, to_jsonable, write_json
from .no_source import assert_no_source, no_source_extraction
from .types import CaseOutcome, Dataset, MemorySeed, QueryCase


class EvalHarness:
    """装配内核 → 灌语料 → 跑查询 → 采集观测。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        artifact_dir: str | Path | None = None,
    ) -> None:
        self._kernel = build_kernel(config=config)
        self._api = self._kernel.api
        self._key2ids: Dict[str, List[str]] = {}
        self._artifact_dir = Path(artifact_dir) if artifact_dir else None
        self._pre_dedup_calls: list[dict] = []
        self._retrieval_audits: list[dict] = []
        if self._artifact_dir is not None:
            self._install_pre_dedup_capture()

    @staticmethod
    def _security_kwargs(method, identity) -> dict:
        """Bridge the legacy identity= API and the current security= API."""
        if "security" not in signature(method).parameters:
            return {"identity": identity}
        if legacy_request_context is None:
            raise RuntimeError(
                "Mem2.0 requires security= but legacy_request_context is unavailable"
            )
        return {"security": legacy_request_context(identity)}

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

        extractor._build_units = captured_build_units

    def ingest(self, seeds: List[MemorySeed]) -> None:
        """逐条写入语料，捕获每个数据集 key 对应的真实 unit_id（可为多条：规约/切分）。"""
        add = getattr(self._api, "add", None) or getattr(self._api, "write")
        metadata_arg = (
            "system_metadata"
            if "system_metadata" in signature(add).parameters
            else "metadata"
        )
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
            self._key2ids[seed.key] = [u.id for u in units]

    def run_query(self, case: QueryCase) -> CaseOutcome:
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
        retriever = self._api._engine._retriever
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
                memory_retrieval_e2e_wall_ms = (
                    perf_counter() - retrieval_started
                ) * 1000.0
        finally:
            storage.recall = original_recall
        if len(recall_durations_ms) != 1:
            raise RuntimeError(
                "expected exactly one storage recall call, got "
                f"{len(recall_durations_ms)}"
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
        unit_message_dates: Dict[str, str] = {}
        unit_event_dates: Dict[str, str] = {}
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
                unit_message_dates[unit.id] = (
                    message_at.isoformat() if message_at else ""
                )
                unit_event_dates[unit.id] = event_at.isoformat() if event_at else ""
        relevant_ids = set()
        relevant_key_unit_ids: Dict[str, List[str]] = {}
        for key in case.relevant_keys:
            unit_ids = list(self._key2ids.get(key, []))
            relevant_ids.update(unit_ids)
        source_keys = case.relevant_source_keys or {
            key: {key} for key in case.relevant_keys
        }
        for source_key, keys in source_keys.items():
            relevant_key_unit_ids[source_key] = [
                unit_id
                for key in keys
                for unit_id in self._key2ids.get(key, [])
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
                unit_message_dates.get(item.unit_id, "")
                or unit_event_dates.get(item.unit_id, "")
                for item in result.items
            ],
            trajectory=list(result.trajectory),
            context_message_dates=[
                unit_message_dates.get(item.unit_id, "") for item in result.items
            ],
            context_event_dates=[
                unit_event_dates.get(item.unit_id, "") for item in result.items
            ],
            memory_retrieval_e2e_wall_ms=memory_retrieval_e2e_wall_ms,
            storage_recall_wall_ms=storage_recall_wall_ms,
            expected_answer=case.expected_answer,
            metadata=dict(case.metadata),
            relevant_key_unit_ids=relevant_key_unit_ids,
        )

    def evaluate(self, dataset: Dataset, concurrency: int = 1) -> List[CaseOutcome]:
        """按 scope 隔离执行 write→recall；可并行不同 sample 的 scope。"""
        if concurrency <= 0:
            raise ValueError(f"evaluation concurrency must be positive, got {concurrency}")
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
        with no_source_extraction():
            if concurrency == 1:
                indexed_outcomes = [
                    result
                    for seeds, indexed_cases in jobs
                    for result in self._evaluate_scope(seeds, indexed_cases)
                ]
            else:
                indexed_outcomes = []
                with ThreadPoolExecutor(
                    max_workers=concurrency,
                    thread_name_prefix="memory-eval",
                ) as executor:
                    futures = [
                        executor.submit(self._evaluate_scope, seeds, indexed_cases)
                        for seeds, indexed_cases in jobs
                    ]
                    for future in as_completed(futures):
                        indexed_outcomes.extend(future.result())
        return [outcome for _, outcome in sorted(indexed_outcomes, key=lambda item: item[0])]

    def _evaluate_scope(
        self,
        seeds: list[MemorySeed],
        indexed_cases: list[tuple[int, QueryCase]],
    ) -> list[tuple[int, CaseOutcome]]:
        self.ingest(seeds)
        persisted_units = self._list_persisted_units(indexed_cases[0][1].scope)
        assert_no_source(
            self._kernel,
            indexed_cases[0][1].scope,
            persisted_units,
        )
        results = [(index, self.run_query(case)) for index, case in indexed_cases]
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
        assert self._artifact_dir is not None
        candidate_count = sum(len(call["candidates"]) for call in self._pre_dedup_calls)
        memory_unit_count = sum(
            len(call["memory_units"]) for call in self._pre_dedup_calls
        )
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
                    audit["storage_recall_call_count"]
                    for audit in self._retrieval_audits
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

    @staticmethod
    def _scope_key(scope) -> tuple[str, ...]:
        # ``space`` is introduced by the post-9ed Scope API. Keep the
        # evaluation overlay compatible with the current official 9ed commit,
        # where Scope does not expose that field yet.
        return (
            scope.org,
            getattr(scope, "space", ""),
            scope.user,
            scope.agent,
            scope.session,
        )
