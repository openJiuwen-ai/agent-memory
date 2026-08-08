"""最小实现：:class:`~retrieval.retriever.Retriever`——检索链路编排者。

单次 :meth:`retrieve` 驱动完整链路（Option B：点读/有效性/重排为独立阶段）：
查询理解 → 前置谓词构造 → 并行多路召回 → 融合 → 截断候选预算 → 点读真源 +
有效性过滤 → （可选）重排 → 阈值过滤 → 截断 top_k → 渐进式披露 → 返回结果与轨迹。
scope 作显式首参贯穿下推到各召回路；各子算子由装配注入，本类不含召回/打分逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from time import perf_counter
from uuid import uuid4

from common.errors import ValidationError, safe_error_message
from common.factory.factory import Factory
from common.log import get_logger
from common.reranker.base import Reranker, RerankerProducer
from common.type_def import (
    ChannelError,
    RecallBatch,
    RecallResult,
    RetrievalPipeline,
    Scope,
    ScoredCandidate,
    ScoredMemoryUnit,
    ScoredUnit,
    and_merge,
    is_retrieval_candidate,
)
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
    TrajectoryStep,
)
from storage.storage import Storage, StorageProducer
from storage.storage_impl import CompositeStorage

from .predicate_builder import build_system_filters
from .unit_reader import UnitReader

logger = get_logger(__name__)


class PipelineRetriever(Retriever):
    """编排 parse → 谓词 → recall(多路) → fuse → 点读+复核 → rerank → disclose。"""

    def __init__(
        self,
        parser: QueryParser,
        recallers: list[Recaller],
        fuser: Fuser,
        discloser: Discloser,
        unit_reader: UnitReader | None,
        reranker: Reranker | None = None,
        over_fetch_factor: int = 4,
        over_fetch_floor: int = 60,
        recall_max: int = 100,
        rerank_max: int = 60,
        min_score: float = 0.0,
        # 相对阈值默认关闭，与 defaults.py 的 retriever params 保持一致。
        min_score_ratio: float = 0.0,
        min_score_ratio_uncalibrated: float = 0.0,
        min_results: int = 0,
        storage: Storage | None = None,
    ) -> None:
        self._parser = parser
        self._recallers = recallers
        self._fuser = fuser
        self._discloser = discloser
        self._reader = unit_reader
        if storage is None:
            if unit_reader is None:
                raise ValidationError("PipelineRetriever requires storage or unit_reader")
            storage = CompositeStorage(kv=unit_reader.kv, recallers=recallers)
        elif isinstance(storage, CompositeStorage):
            storage.bind_recallers(recallers)
        self._storage = storage
        self._reranker = reranker
        # 召回超采样：每路取 max(top_k*factor, floor)，撒宽网喂融合。
        self._over_fetch_factor = max(1, int(over_fetch_factor))
        self._over_fetch_floor = max(1, int(over_fetch_floor))
        # 召回硬上限：封顶每路 recall_k，防止调用方超大 top_k 经 factor 放大压垮后端
        # （0=不限）。融合池 ≤ recall_max×通道数 → 间接封顶下游点读/复核/重排。
        self._recall_max = max(0, int(recall_max))
        # 两旋钮矛盾时上限赢（保护优先），但要让运维可见，避免 floor 静默失效。
        if 0 < self._recall_max < self._over_fetch_floor:
            logger.warning(
                "recall_max(%d) < over_fetch_floor(%d)：召回硬上限压过下限，"
                "每路生效召回宽度为 recall_max",
                self._recall_max,
                self._over_fetch_floor,
            )
        # 精排预算：recheck+rerank 封顶（控 cross-encoder 成本），但不低于 top_k。
        self._rerank_max = max(1, int(rerank_max))
        self._min_score = float(min_score)
        self._min_score_ratio = float(min_score_ratio)
        self._min_score_ratio_uncalibrated = float(min_score_ratio_uncalibrated)
        self._min_results = max(0, int(min_results))

    @property
    def recallers(self) -> list[Recaller]:
        """已接入的 recaller 列表（只读视图；外部不应原地修改）。"""
        return self._recallers

    @property
    def storage(self) -> Storage:
        """当前 Retriever 使用的统一 Storage 实例。"""
        return self._storage

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

        started_at = perf_counter()
        trace_id = uuid4().hex[:16]
        scope_dims = _scope_log_dims(scope)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Retriever.retrieve start: trace_id=%s scope_dims=%s top_k=%d channels=%s "
                "rerank=%s disclosure=%s query_len=%d",
                trace_id,
                scope_dims,
                query.top_k,
                _format_channels(query.channels, auto_label="auto"),
                query.rerank,
                query.disclosure.value,
                len(query.text),
            )

        traj: list[TrajectoryStep] = []
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
            logger.debug(
                "Retriever.retrieve empty query: trace_id=%s scope_dims=%s",
                trace_id,
                scope_dims,
            )
            logger.debug(
                "Retriever.retrieve done: trace_id=%s scope_dims=%s fused=%d survivors=%d "
                "returned=%d cost_ms=%.2f",
                trace_id,
                scope_dims,
                0,
                0,
                0,
                (perf_counter() - started_at) * 1000.0,
            )
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

        # [3a] 前置谓词：系统谓词（lifecycle×as_of / 时间窗）与用户表达式 AND 外包一同下推。
        #      用户表达式作整体 child——绝不摊平，防其 OR 稀释 lifecycle 等安全谓词。
        sys_filters = build_system_filters(
            parsed.as_of, parsed.time_from, parsed.time_to, query.include_archived
        )
        user_filters = parsed.scalar_filters  # parser 已 normalize 的用户表达式（供 §6 复核）
        parsed.scalar_filters = and_merge(user_filters, sys_filters)

        # 通道选择：调用级 query.channels 覆盖 parser 建议；显式空列表不是“全部”。
        if query.channels == []:
            raise ValidationError("channels must be omitted or contain at least one channel")
        enabled = query.channels if query.channels is not None else (parsed.channels or None)
        parsed.include_archived = query.include_archived
        parsed.recheck_filters = user_filters
        # 召回超采样（撒宽网）与精排预算（控成本）解耦：
        #   recall_k = max(top_k*factor, floor)  —— 每路召回宽度
        #   budget_n = max(rerank_max, top_k)    —— recheck+rerank 封顶，永不欠 top_k
        recall_k = max(query.top_k * self._over_fetch_factor, self._over_fetch_floor)
        if self._recall_max > 0:
            recall_k = min(recall_k, self._recall_max)  # 硬上限：封顶后端召回压力
        budget_n = max(self._rerank_max, query.top_k)

        # [3b-6] Storage 的全局首选值选择三条 recall/get/rank 路径。
        pipeline = self._storage.preferred_retrieval_pipeline()
        t0 = perf_counter()
        errors: list[ChannelError]
        if pipeline == RetrievalPipeline.RECALL_GET_RANK:
            recalled = self._storage.recall(
                scope, parsed, channels=enabled, recall_limit=recall_k
            )
            materialized = _materialize_recalled(self._storage, scope, recalled, parsed)
            fused = self._fuser.fuse(
                parsed, [batch.candidates for batch in materialized.batches]
            )
            errors = materialized.errors
        elif pipeline == RetrievalPipeline.RECALL_AND_GET_RANK:
            materialized = self._storage.recall_and_get(
                scope, parsed, channels=enabled, recall_limit=recall_k
            )
            materialized = _filter_materialized(materialized, parsed)
            fused = self._fuser.fuse(
                parsed, [batch.candidates for batch in materialized.batches]
            )
            errors = materialized.errors
        else:
            ranked = self._storage.retrieve(
                scope,
                parsed,
                self._fuser,
                channels=enabled,
                recall_limit=recall_k,
                rank_limit=budget_n,
            )
            fused = ranked.candidates
            errors = ranked.errors

        for error in errors:
            logger.warning(
                "Retriever.recall degraded: trace_id=%s scope_dims=%s channel=%s "
                "source=%s error_type=%s error=%s",
                trace_id,
                scope_dims,
                error.channel.value,
                error.source,
                error.error_type,
                error.message,
            )
        recall_cost_ms = (perf_counter() - t0) * 1000.0
        if pipeline == RetrievalPipeline.RETRIEVE:
            record_step(
                "recall",
                recall_cost_ms,
                len(fused),
                detail={"pipeline": pipeline.value, "errors": str(len(errors))},
            )
        else:
            for batch in materialized.batches:
                record_step(
                    "recall",
                    recall_cost_ms,
                    len(batch.candidates),
                    batch.channel,
                    {"source": batch.source, "pipeline": pipeline.value},
                )
            for error in errors:
                record_step(
                    "recall",
                    recall_cost_ms,
                    channel=error.channel,
                    detail={
                        "source": error.source,
                        "degraded": error.error_type,
                        "error": error.message,
                    },
                )

        explain_fusion = getattr(self._fuser, "explain", None)
        fusion_detail = explain_fusion() if callable(explain_fusion) else {}
        record_step("fuse", 0.0, n=len(fused), detail=fusion_detail)

        # Fuser 后再限制精排预算；Storage.retrieve 已在入口内应用同一个上限。
        survivors = list(fused[:budget_n])
        units = {candidate.unit_id: candidate.unit for candidate in survivors}
        recheck_dropped = 0
        record_step("recheck", 0.0, n=len(survivors), detail={"dropped": "0"})
        if recheck_dropped:
            logger.debug(
                "Retriever.recheck dropped: trace_id=%s scope_dims=%s dropped=%d budget=%d",
                trace_id,
                scope_dims,
                recheck_dropped,
                len(survivors),
            )

        # [7] 可选重排：内容已物化，按与 query 的相关性精排
        do_rerank = query.rerank if query.rerank is not None else (self._reranker is not None)
        reranked = False
        if do_rerank and self._reranker is not None and survivors:
            t0 = perf_counter()
            scores = self._reranker.rerank(
                parsed.raw, [units[su.unit_id].content for su in survivors]
            )
            order = sorted(range(len(survivors)), key=lambda i: scores[i], reverse=True)
            survivors = [replace(survivors[i], score=scores[i]) for i in order]
            step("rerank", t0, n=len(survivors))
            reranked = True
        elif do_rerank and self._reranker is None:
            # 显式要求精排但装配未注入 reranker：记轨迹让降级可见（阈值走未校准路径）。
            record_step(
                "rerank", 0.0, n=len(survivors), detail={"skipped": "no_reranker_configured"}
            )

        # [8] 统一阈值过滤：精排路径用校准分；未精排路径仅使用相对阈值。
        t0 = perf_counter()
        survivors, threshold_detail = apply_threshold(
            survivors,
            query.top_k,
            calibrated=reranked,
            min_score=self._min_score,
            min_score_ratio=self._min_score_ratio,
            min_score_ratio_uncalibrated=self._min_score_ratio_uncalibrated,
            min_results=self._min_results,
        )
        step("threshold", t0, n=len(survivors), detail=threshold_detail)

        # [9] 截断 top_k
        final = survivors[: query.top_k]

        # [10] 渐进式披露（纯内容塑形，复用已点读的 units）
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

        logger.debug(
            "Retriever.retrieve done: trace_id=%s scope_dims=%s fused=%d survivors=%d "
            "returned=%d cost_ms=%.2f",
            trace_id,
            scope_dims,
            len(fused),
            len(survivors),
            len(items),
            (perf_counter() - started_at) * 1000.0,
        )
        return RetrievalResult(items=items, trajectory=traj, errors=errors)

# -- 注册到 RetrieverProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@RetrieverProducer.register("pipeline")
def _build(config):
    storage = StorageProducer.resolve(config)
    # 召回路按能力开关启用；每路 recaller 自取其 Store，可被 config 各自覆盖。
    recallers = [RecallerProducer.dep(config, "keyword_recaller", default="keyword")]
    if config.get("vector_enabled", True):
        recallers.append(RecallerProducer.dep(config, "vector_recaller", default="vector"))
    if config.get("graph_enabled", True):
        recallers.append(RecallerProducer.dep(config, "graph_recaller", default="graph"))
    # L0/L1 分层召回：layers_index_enabled 默认 true（与构建侧对齐：默认建默认查）。
    # recaller 内部 store 为 None 时 recall 返空，不破坏其他路（向后兼容）。
    if config.get("layers_index_enabled", True):
        recallers.append(RecallerProducer.dep(config, "keyword_l0_recaller", default="keyword_l0"))
        recallers.append(RecallerProducer.dep(config, "keyword_l1_recaller", default="keyword_l1"))
        if config.get("vector_enabled", True):
            recallers.append(
                RecallerProducer.dep(config, "vector_l0_recaller", default="vector_l0")
            )
            recallers.append(
                RecallerProducer.dep(config, "vector_l1_recaller", default="vector_l1")
            )
    # 精排器与 UnitReader 的真源 kv 与索引/构建侧共享同一实例。
    reranker = (
        RerankerProducer.dep(config, default="overlap")
        if config.get("rerank_enabled", True)
        else None
    )
    if isinstance(storage, CompositeStorage):
        storage.bind_recallers(recallers)
    return PipelineRetriever(
        QueryParserProducer.dep(config, default="simple"),
        recallers,
        FuserProducer.dep(config, default="rrf"),
        DiscloserProducer.dep(config, default="truncating"),
        UnitReader(storage.kv) if storage.has_kv() else None,
        reranker,
        over_fetch_factor=int(Factory.cfg_get(config, "over_fetch_factor", 4)),
        over_fetch_floor=int(Factory.cfg_get(config, "over_fetch_floor", 60)),
        # 回退值 = 出厂默认（与 defaults.py 的 retriever params 保持一致）：实例级覆盖会
        # 整体替换 params，漏写键时落到这里——回退到出厂行为而非静默关闭防护。
        recall_max=int(Factory.cfg_get(config, "recall_max", 100)),
        rerank_max=int(Factory.cfg_get(config, "rerank_max", 60)),
        min_score=float(Factory.cfg_get(config, "min_score", 0.0)),
        min_score_ratio=float(Factory.cfg_get(config, "min_score_ratio", 0.0)),
        min_score_ratio_uncalibrated=float(
            Factory.cfg_get(config, "min_score_ratio_uncalibrated", 0.0)
        ),
        min_results=int(Factory.cfg_get(config, "min_results", 0)),
        storage=storage,
    )


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def apply_threshold(
    survivors: list[ScoredCandidate],
    top_k: int,
    *,
    calibrated: bool,
    min_score: float = 0.0,
    min_score_ratio: float = 0.0,
    min_score_ratio_uncalibrated: float = 0.0,
    min_results: int = 0,
) -> tuple[list[ScoredCandidate], dict[str, str]]:
    """应用统一相关性阈值，返回保留候选与轨迹明细。"""
    n_in = len(survivors)
    positive = sorted(
        (su for su in survivors if su.score > 0.0),
        key=lambda su: su.score,
        reverse=True,
    )
    # 绝对阈值仅在校准（已精排）路径生效；相对阈值分路取不同默认：
    # 校准分较可信取严，未校准 RRF 分聚集取松，避免过度偏好多通道一致。
    abs_min = min_score if calibrated else 0.0
    ratio = min_score_ratio if calibrated else min_score_ratio_uncalibrated
    base_detail = {
        "in": str(n_in),
        "positive": str(len(positive)),
        "calibrated": str(calibrated),
        "min_score": f"{abs_min:g}",
        "min_score_ratio": f"{ratio:g}",
    }
    if not positive:
        return [], {
            **base_detail,
            "passed": "0",
            "backfilled": "0",
            "out": "0",
            "dropped": str(n_in),
        }

    max_score = positive[0].score

    def _pass(score: float) -> bool:
        if abs_min > 0.0 and score < abs_min:
            return False
        if ratio > 0.0 and max_score > 0.0 and score < ratio * max_score:
            return False
        return True

    n_pass = 0
    for su in positive:
        if _pass(su.score):
            n_pass += 1
        else:
            break

    floor = min(max(0, int(min_results)), top_k) if min_results > 0 else 0
    keep_n = max(n_pass, floor)
    kept = positive[:keep_n]
    backfilled = max(0, len(kept) - n_pass)
    return kept, {
        **base_detail,
        "passed": str(n_pass),
        "backfilled": str(backfilled),
        "out": str(len(kept)),
        "dropped": str(n_in - len(kept)),
    }


def _format_channels(channels: list[RecallChannel] | None, *, auto_label: str) -> str:
    if channels is None:
        return auto_label
    if not channels:
        return "all"
    return ",".join(channel.value for channel in channels)


def _scope_log_dims(scope: Scope) -> str:
    dims: list[str] = []
    if scope.org:
        dims.append("org")
    if scope.space:
        dims.append("space")
    if scope.user:
        dims.append("user")
    if scope.agent:
        dims.append("agent")
    if scope.session:
        dims.append("session")
    return ",".join(dims) if dims else "none"


def _safe_error(exc: Exception) -> str:
    return safe_error_message(exc)


def _materialize_recalled(
    storage: Storage,
    scope: Scope,
    recalled: RecallResult[ScoredUnit],
    query,
) -> RecallResult[ScoredMemoryUnit]:
    """读取 id 仅去重 IO，随后恢复每个物理入口的全部候选证据。"""
    unit_ids: list[str] = []
    seen: set[str] = set()
    for batch in recalled.batches:
        for candidate in batch.candidates:
            if candidate.unit_id not in seen:
                seen.add(candidate.unit_id)
                unit_ids.append(candidate.unit_id)
    units = {unit.id: unit for unit in storage.get(scope, unit_ids)}
    batches: list[RecallBatch[ScoredMemoryUnit]] = []
    errors = list(recalled.errors)
    for batch in recalled.batches:
        materialized: list[ScoredMemoryUnit] = []
        for candidate in batch.candidates:
            unit = units.get(candidate.unit_id)
            if unit is None:
                errors.append(
                    ChannelError(
                        batch.channel,
                        batch.source,
                        "MissingMemoryUnit",
                        f"MemoryUnit not found: {candidate.unit_id}",
                    )
                )
                continue
            scored = ScoredMemoryUnit(
                unit, candidate.score, candidate.channel, candidate.evidence
            )
            if _passes_recheck(scored, query):
                materialized.append(scored)
        batches.append(RecallBatch(batch.channel, batch.source, materialized))
    return RecallResult(batches=batches, errors=errors)


def _filter_materialized(
    result: RecallResult[ScoredMemoryUnit], query
) -> RecallResult[ScoredMemoryUnit]:
    batches: list[RecallBatch[ScoredMemoryUnit]] = []
    for batch in result.batches:
        candidates = [
            candidate
            for candidate in batch.candidates
            if _passes_recheck(candidate, query)
        ]
        batches.append(RecallBatch(batch.channel, batch.source, candidates))
    return RecallResult(batches=batches, errors=result.errors)


def _passes_recheck(candidate: ScoredMemoryUnit, query) -> bool:
    return is_retrieval_candidate(
        candidate.unit,
        as_of=query.as_of,
        time_from=query.time_from,
        time_to=query.time_to,
        filters=query.recheck_filters,
        include_archived=query.include_archived,
    )
