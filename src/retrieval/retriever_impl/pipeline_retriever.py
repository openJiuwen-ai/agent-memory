"""最小实现：:class:`~retrieval.retriever.Retriever`——检索链路编排者。

单次 :meth:`retrieve` 驱动完整链路（Option B：点读/有效性/重排为独立阶段）：
查询理解 → 前置谓词构造 → 并行多路召回 → 融合 → 截断重排预算 → 点读真源 +
有效性过滤 → （可选）重排 → 截断 top_k → 渐进式披露 → 返回结果与轨迹。
scope 作显式首参贯穿下推到各召回路；各子算子由装配注入，本类不含召回/打分逻辑。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from typing import Dict, List, Optional

from common.errors import ValidationError
from common.reranker.base import Reranker, RerankerProducer
from common.type_def import MemoryUnit, Scope
from retrieval.base import RetrievalOperatorType
from retrieval.discloser import Discloser, DiscloserProducer
from retrieval.fuser import Fuser, FuserProducer
from retrieval.query_parser import QueryParser, QueryParserProducer
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.retriever import Retriever, RetrieverProducer
from retrieval.types import (
    DisclosureLevel,
    RecallChannel,
    RetrievalQuery,
    RetrievalResult,
    ScoredUnit,
    TrajectoryStep,
)
from storage.kv import KvProducer

from .predicate_builder import build_system_filters
from .unit_reader import UnitReader, in_event_window, matches_filters, passes


class PipelineRetriever(Retriever):
    """编排 parse → 谓词 → recall(多路) → fuse → 点读+复核 → rerank → disclose。"""

    def __init__(
        self,
        parser: QueryParser,
        recallers: List[Recaller],
        fuser: Fuser,
        discloser: Discloser,
        unit_reader: UnitReader,
        reranker: Optional[Reranker] = None,
        rerank_top_m: int = 50,
    ) -> None:
        self._parser = parser
        self._recallers = recallers
        self._fuser = fuser
        self._discloser = discloser
        self._reader = unit_reader
        self._reranker = reranker
        self._top_m = rerank_top_m

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        # 入参校验：top_k 非法直接拒绝（可预期的调用错误）。
        if query.top_k <= 0:
            raise ValidationError(f"top_k must be positive, got {query.top_k}")
        if query.max_tokens is not None and query.max_tokens <= 0:
            raise ValidationError(f"max_tokens must be positive, got {query.max_tokens}")

        traj: List[TrajectoryStep] = []
        trace = query.with_trajectory

        def record_step(
            stage: str,
            cost_ms: float,
            n: int = 0,
            channel: RecallChannel | None = None,
            detail: dict[str, str] | None = None,
        ) -> None:
            if trace:
                traj.append(
                    TrajectoryStep(
                        stage=stage,
                        channel=channel,
                        candidate_count=n,
                        cost_ms=cost_ms,
                        detail=detail or {},
                    )
                )

        def step(stage: str, t0: float, n: int = 0, channel=None, detail=None) -> None:
            record_step(stage, (perf_counter() - t0) * 1000.0, n, channel, detail)

        # 空 query 短路：无可检索信号，返回空结果而非把空串喂给各后端。
        if not query.text.strip():
            step("parse", perf_counter(), detail={"skipped": "empty_query"})
            return RetrievalResult(items=[], trajectory=traj)

        # [2] 查询理解
        t0 = perf_counter()
        parsed = self._parser.parse(query)
        if not parsed.raw.strip():
            step("parse", t0, detail={"skipped": "empty_after_parse"})
            return RetrievalResult(items=[], trajectory=traj)
        # 调用方自定义透传配置随 parsed 下达各召回路（自定义 Recaller 按约定读取）；
        # 在此统一接力，无需各 parser 实现感知。
        parsed.extensions = dict(query.extensions)
        step("parse", t0, n=len(parsed.tokens))

        # [3a] 前置谓词：系统谓词（lifecycle×as_of / 时间窗）并入用户 filters 一同下推
        sys_filters = build_system_filters(
            parsed.as_of, parsed.time_from, parsed.time_to, query.include_archived
        )
        parsed.scalar_filters = list(parsed.scalar_filters) + sys_filters

        # 通道选择：调用级 query.channels 覆盖 parser 建议
        enabled = query.channels if query.channels is not None else parsed.channels
        recall_k = max(query.top_k, self._top_m)  # 每路超采样到重排预算

        # [3b] 并行多路召回：每通道失败隔离——单路异常降级为空集并记轨迹。
        selected_recallers = [
            recaller for recaller in self._recallers if not enabled or recaller.channel() in enabled
        ]
        per_channel: List[List[ScoredUnit]] = []
        recall_results: Dict[int, List[ScoredUnit]] = {}
        recall_steps: Dict[int, tuple[float, int, RecallChannel, dict[str, str]]] = {}
        if selected_recallers:
            with ThreadPoolExecutor(max_workers=len(selected_recallers)) as executor:
                futures = {}
                for idx, recaller in enumerate(selected_recallers):
                    t0 = perf_counter()
                    future = executor.submit(recaller.recall, scope, parsed, recall_k)
                    futures[future] = (idx, recaller, t0)

                for future in as_completed(futures):
                    idx, recaller, t0 = futures[future]
                    try:
                        cands = future.result()
                        detail: dict[str, str] = {}
                    except Exception as exc:  # 通道隔离：任何后端故障不中断其他通道
                        cands = []
                        detail = {
                            "degraded": type(exc).__name__,
                            "error": str(exc)[:200],
                        }
                    recall_results[idx] = cands
                    recall_steps[idx] = (
                        (perf_counter() - t0) * 1000.0,
                        len(cands),
                        recaller.channel(),
                        detail,
                    )

        for idx in range(len(selected_recallers)):
            cands = recall_results.get(idx, [])
            if cands:
                per_channel.append(cands)
            if idx in recall_steps:
                cost_ms, count, channel, detail = recall_steps[idx]
                record_step("recall", cost_ms, count, channel, detail)

        # [4] 融合
        t0 = perf_counter()
        fused = self._fuser.fuse(parsed, per_channel)
        explain_fusion = getattr(self._fuser, "explain", None)
        fusion_detail = explain_fusion() if callable(explain_fusion) else {}
        step("fuse", t0, n=len(fused), detail=fusion_detail)

        # [5] 截断到重排预算 top-M
        budget = fused[: self._top_m]

        # [6] 点读真源（查询 scope 内）+ 后置过滤（纵深防御）：
        #     lifecycle×as_of 有效性 + event-time 窗 + 调用方显式 filters。
        t0 = perf_counter()
        units: Dict[str, MemoryUnit] = self._reader.load(scope, [su.unit_id for su in budget])

        def _keep(u: MemoryUnit) -> bool:
            return (
                passes(u, parsed.as_of, query.include_archived)
                and in_event_window(u, parsed.time_from, parsed.time_to)
                and matches_filters(u, query.filters)
            )

        survivors = [su for su in budget if su.unit_id in units and _keep(units[su.unit_id])]
        step("recheck", t0, n=len(survivors), detail={"dropped": str(len(budget) - len(survivors))})

        # [7] 可选重排：内容已物化，按与 query 的相关性精排
        do_rerank = query.rerank if query.rerank is not None else (self._reranker is not None)
        if do_rerank and self._reranker is not None and survivors:
            t0 = perf_counter()
            scores = self._reranker.rerank(
                parsed.raw, [units[su.unit_id].content for su in survivors]
            )
            order = sorted(range(len(survivors)), key=lambda i: scores[i], reverse=True)
            survivors = [
                ScoredUnit(
                    survivors[i].unit_id,
                    scores[i],
                    survivors[i].channel,
                    evidence=survivors[i].evidence,
                )
                for i in order
            ]
            step("rerank", t0, n=len(survivors))
            t0 = perf_counter()
            before_filter = len(survivors)
            survivors = [su for su in survivors if su.score > 0.0]
            step(
                "score_filter",
                t0,
                n=len(survivors),
                detail={"dropped": str(before_filter - len(survivors)), "min_score": ">0"},
            )

        # [8] 截断 top_k
        final = survivors[: query.top_k]

        # [9] 渐进式披露（纯内容塑形，复用已点读的 units）
        t0 = perf_counter()
        items = self._discloser.disclose(
            parsed, final, units, query.disclosure, max_tokens=query.max_tokens
        )
        disclose_detail = {}
        if query.disclosure == DisclosureLevel.ADAPTIVE:
            disclose_detail = {
                "mode": "adaptive",
                "max_tokens": str(query.max_tokens or ""),
                "estimated_tokens": str(sum(_estimate_tokens(item.content) for item in items)),
                "levels": ",".join(item.level.value for item in items),
            }
        step("disclose", t0, n=len(items), detail=disclose_detail)

        return RetrievalResult(items=items, trajectory=traj)


# -- 注册到 RetrieverProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@RetrieverProducer.register("pipeline")
def _build(config):
    # 召回路按能力开关启用；每路 recaller 自取其 Store，可被 config 各自覆盖。
    recallers = [RecallerProducer.dep(config, "keyword_recaller", default="keyword")]
    if config.get("vector_enabled", True):
        recallers.append(RecallerProducer.dep(config, "vector_recaller", default="vector"))
    if config.get("graph_enabled", True):
        recallers.append(RecallerProducer.dep(config, "graph_recaller", default="graph"))
    # 精排器与 UnitReader 的真源 kv 与索引/构建侧共享同一实例。
    reranker = (
        RerankerProducer.dep(config, default="overlap")
        if config.get("rerank_enabled", True)
        else None
    )
    return PipelineRetriever(
        QueryParserProducer.dep(config, default="simple"),
        recallers,
        FuserProducer.dep(config, default="rrf"),
        DiscloserProducer.dep(config, default="truncating"),
        UnitReader(KvProducer.dep(config, default="memory")),
        reranker,
    )


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
