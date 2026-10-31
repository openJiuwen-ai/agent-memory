# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~retrieval.retriever.Retriever`——检索链路编排者。

单次 :meth:`retrieve` 驱动完整链路：
查询理解 → 前置谓词构造 → 多路召回与真源复核 → 融合 → 截断候选预算 →
（可选）重排 → 阈值过滤 → 截断 top_k → 渐进式披露 → 返回结果与轨迹。
scope 作显式首参贯穿下推；召回/取数/排序全部委托统一 Storage 的三条首选路径，
本类不含召回/打分逻辑，也不持有召回路（CompositeStorage 的兼容 Recaller 由
storage 层工厂按配置装配）。
"""

from __future__ import annotations

from dataclasses import replace
from time import perf_counter

from jiuwen_memory.common.errors import (
    BackendError,
    UnsupportedCapabilityError,
    ValidationError,
    safe_error_message,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger, redact_for_log
from jiuwen_memory.common.reranker.base import Reranker, RerankerProducer
from jiuwen_memory.common.type_def import (
    ChannelError,
    ParsedQuery,
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
from jiuwen_memory.common.type_def.hierarchy_query import (
    HierarchyQuery,
    validate_expand_depth,
    validate_rollup,
)
from jiuwen_memory.config.document_flag import WRITE_DOCUMENT_KEY, should_write_document
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.discloser import Discloser, DiscloserProducer
from jiuwen_memory.retrieval.expander import Expander, ExpanderProducer
from jiuwen_memory.retrieval.expansion import (
    ExpansionSource,
    complete_expansion,
    prepare_expansion,
    uses_recalled_children,
)
from jiuwen_memory.retrieval.fuser import Fuser, FuserProducer
from jiuwen_memory.retrieval.query_parser import QueryParser, QueryParserProducer
from jiuwen_memory.retrieval.retriever import RetrieverProducer
from jiuwen_memory.retrieval.schema_temporal.pipeline import (
    OrdinaryRetrieval,
    SchemaTemporalPipelineExtension,
    build_schema_temporal_extension,
)
from jiuwen_memory.retrieval.staged_retriever import RetrieveState, StagedRetriever
from jiuwen_memory.retrieval.types import (
    DisclosureLevel,
    RecallChannel,
    RetrievalQuery,
    RetrievalResult,
)
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

from .hierarchy_rollup import RollupRequest, rollup_candidates
from .predicate_builder import (
    build_hierarchy_filters,
    build_rollup_role_filters,
    build_system_filters,
)
from .unit_reader import UnitReader

logger = get_logger(__name__)

# 精排输入限长出厂默认（retriever params 可经 rerank_max_query_chars /
# rerank_max_doc_chars 覆盖；上调须自行承担精排超时风险）：交叉编码器负载 =
# query 长度 × 候选数，超长 query（如 10KB）会把远程 rerank 服务顶到
# reranker_timeout（默认 120s）才返回。召回向量化仍用全量 query，这里只
# 截断送精排的文本；相关性以头部为主，故头部截断。
_MAX_RERANK_QUERY_CHARS = 2000
_MAX_RERANK_DOC_CHARS = 4000


def _truncate_rerank_inputs(
    query: str, docs: list[str], query_limit: int, doc_limit: int
) -> tuple[str, list[str]]:
    """精排输入限长：query/doc 超长会把远程 rerank 顶满 reranker_timeout。"""
    return (
        query[:query_limit],
        [doc[:doc_limit] for doc in docs],
    )


class PipelineRetriever(StagedRetriever):
    """编排 parse → 谓词 → recall/真源复核 → fuse → rerank → disclose。"""

    def __init__(
        self,
        parser: QueryParser,
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
        *,
        domain_store: DomainStore,
        schema_temporal_extension: SchemaTemporalPipelineExtension | None = None,
        doc_mode: bool = False,
        # 精排输入限长：与 reranker_timeout 同走 retriever params 通道。
        rerank_max_query_chars: int = _MAX_RERANK_QUERY_CHARS,
        rerank_max_doc_chars: int = _MAX_RERANK_DOC_CHARS,
    ) -> None:
        self._parser = parser
        self._fuser = fuser
        self._discloser = discloser
        self._reader = unit_reader
        self._domain = domain_store
        self._schema_temporal_extension = schema_temporal_extension
        self._expander: Expander | None = None
        # 文档模式标志：ShadowRecaller.channel()=DOCUMENT，但 parser 产出的
        # parsed.channels 默认含 [KEYWORD, GRAPH]（+VECTOR）不含 DOCUMENT。
        # _domain.recall 用 `r.channel() in channels` 过滤 recaller——DOCUMENT 不在
        # enabled 里则 ShadowRecaller 被过滤 → 召回落空（F08 §5.6 S14 根因之二）。
        # retrieve() 据此标志把 DOCUMENT 补进 enabled（不改 ShadowRecaller.channel()，
        # 保持其 DOCUMENT 语义）。
        self._doc_mode = doc_mode
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
        # 精排输入限长上限（护栏旋钮，上调须自行承担精排超时风险）。
        self._rerank_max_query_chars = max(1, int(rerank_max_query_chars))
        self._rerank_max_doc_chars = max(1, int(rerank_max_doc_chars))

    @property
    def storage(self) -> DomainStore:
        """当前 Retriever 使用的数据面 DomainStore 实例。

        F07：需要管理面能力时由装配层经 ``StoreManagerProducer`` 取 manager，
        Retriever 运行期只持数据面。
        """
        return self._domain

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def bind_expander(self, expander: Expander) -> None:
        """装配阶段绑定同源的展开算子，不引入第二份存储配置。"""
        self._expander = expander

    def health(self) -> None:
        return None

    def query_parsers(self) -> tuple[QueryParser, ...]:
        return (self._parser,)

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        state = RetrieveState.start(scope, query)
        self.validate(state)
        state.log_start()
        if not self.parse(state):
            return state.empty_result()
        self.build_filters(state)
        self.select_channels(state)
        self.plan_budget(state)
        self.recall_and_fuse(state)
        state.cut_to_budget()
        self.rerank(state)
        if query.rollup:
            self.rollup(state)
        self.threshold(state)
        state.cut_to_top_k()
        self.disclose(state)
        state.build_result()
        if query.expand_depth:
            self.expand(state)
            if query.defer_expansion:
                assert state.result is not None
                return state.result
        return self.finalize(state)

    def validate(self, state: RetrieveState) -> None:
        query = state.query
        hierarchy = HierarchyQuery.from_query(query)
        validate_expand_depth(query.expand_depth, hierarchy.hierarchy_kind)
        validate_rollup(query.rollup, hierarchy.hierarchy_kind)
        if query.expand_depth and not isinstance(query.disclosure, DisclosureLevel):
            raise ValidationError("展开 disclosure 必须是 DisclosureLevel")
        if query.expand_depth and self._expander is None:
            raise UnsupportedCapabilityError(
                "expand_depth", str(query.expand_depth), "PipelineRetriever",
                "当前 Retriever 未装配 Expander",
            )
        # 入参校验：top_k 非法直接拒绝（可预期的调用错误）。
        if query.top_k <= 0:
            raise ValidationError(f"top_k must be positive, got {query.top_k}")
        if query.max_tokens is not None and query.max_tokens <= 0:
            raise ValidationError(f"max_tokens must be positive, got {query.max_tokens}")

        state.hierarchy = hierarchy

    def parse(self, state: RetrieveState) -> bool:
        query = state.query
        hierarchy = state.hierarchy
        assert hierarchy is not None, "validate must complete first"
        step = state.run.step
        trace_id, scope_dims = state.run.trace_id, state.run.scope_dims
        # 空 query 短路：无可检索信号，返回空结果而非把空串喂给各后端。
        if not query.text.strip():
            step("parse", perf_counter(), detail={"skipped": "empty_query"})
            logger.debug(
                "Retriever.retrieve empty query: trace_id=%s scope_dims=%s",
                trace_id,
                scope_dims,
            )
            state.run.log_done(0, 0, 0)
            return False

        # [2] 查询理解
        t0 = perf_counter()
        parsed = self._parser.parse(replace(query))
        parsed.hierarchy_kind = hierarchy.hierarchy_kind
        candidate_hierarchy = replace(hierarchy, hierarchy_role=None) if query.rollup else hierarchy
        parsed.hierarchy_role = candidate_hierarchy.hierarchy_role
        parsed.span_start = hierarchy.span_start
        parsed.span_end = hierarchy.span_end
        if not parsed.raw.strip():
            step("parse", t0, detail={"skipped": "empty_after_parse"})
            return False
        # 调用方自定义透传配置随 parsed 下达各召回路（自定义 Recaller 按约定读取）；
        # 在此统一接力，无需各 parser 实现感知。
        parsed.extensions = dict(query.extensions)
        if query.rollup and query.as_of is not None:
            parsed.as_of = query.as_of
        parse_detail = {"rewrite": parsed.rewrite_status}
        if parsed.rewrite_fallback_reason:
            parse_detail["fallback_reason"] = parsed.rewrite_fallback_reason
        if parsed.rewrite_status == "no_intent":
            parse_detail["skipped"] = "no_retrieval_intent"
        step("parse", t0, n=len(parsed.tokens), detail=parse_detail)

        if parsed.rewrite_status == "no_intent":
            logger.debug(
                "Retriever.retrieve no retrieval intent: trace_id=%s scope_dims=%s",
                trace_id,
                scope_dims,
            )
            return False

        state.parsed = parsed
        return True

    def build_filters(self, state: RetrieveState) -> None:
        query = state.query
        hierarchy = state.hierarchy
        assert hierarchy is not None, "validate must complete first"
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        candidate_hierarchy = replace(hierarchy, hierarchy_role=None) if query.rollup else hierarchy
        # [3a] 前置谓词：系统谓词（lifecycle×as_of / 时间窗）与用户表达式 AND 外包一同下推。
        #      用户表达式作整体 child——绝不摊平，防其 OR 稀释 lifecycle 等安全谓词。
        sys_filters = build_hierarchy_filters(candidate_hierarchy) + build_system_filters(
            parsed.as_of, parsed.time_from, parsed.time_to, query.include_archived
        )
        user_filters = parsed.scalar_filters  # parser 已 normalize 的用户表达式（供 §6 复核）
        if query.rollup:
            parser_filters = [] if user_filters is None else [user_filters]
            user_filters = and_merge(
                query.filters, [*parser_filters, *build_rollup_role_filters(hierarchy)],
            )
        parsed.scalar_filters = and_merge(user_filters, sys_filters)

        parsed.include_archived = query.include_archived
        parsed.recheck_filters = user_filters
        state.user_filters = user_filters

    def select_channels(self, state: RetrieveState) -> None:
        query = state.query
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        # 通道选择：调用级 query.channels 覆盖 parser 建议；显式空列表不是“全部”。
        if query.channels == []:
            raise ValidationError("channels must be omitted or contain at least one channel")
        enabled = query.channels if query.channels is not None else (parsed.channels or None)
        # 文档模式：ShadowRecaller.channel()=DOCUMENT 不在 parser 默认 channels 内
        # （[KEYWORD, GRAPH]+可选 VECTOR），这里补进去——否则 ShadowRecaller 被
        # `r.channel() in channels` 过滤掉 → 召回落空（F08 §5.6 S14 根因之二）。
        # 调用方若显式传 query.channels 仍尊重其选择（仅补、不替；DOCUMENT 缺失时才加）。
        if self._doc_mode and enabled is not None:
            if RecallChannel.DOCUMENT not in enabled:
                enabled = [*enabled, RecallChannel.DOCUMENT]
                # 文档模式召回能否命中的关键：DOCUMENT 缺失即 ShadowRecaller 被
                # channels 过滤掉 → 召回恒空（F08 §5.6 S14 根因之二）。
                logger.info(
                    "PipelineRetriever: DOCUMENT injected into channels "
                    "(doc_mode=%s)", self._doc_mode,
                )
        state.channels = enabled

    def plan_budget(self, state: RetrieveState) -> None:
        query = state.query
        # 召回超采样（撒宽网）与精排预算（控成本）解耦：
        #   recall_k = max(top_k*factor, floor)  —— 每路召回宽度
        #   budget_n = max(rerank_max, top_k)    —— recheck+rerank 封顶，永不欠 top_k
        recall_k = max(query.top_k * self._over_fetch_factor, self._over_fetch_floor)
        if self._recall_max > 0:
            recall_k = min(recall_k, self._recall_max)  # 硬上限：封顶后端召回压力
        budget_n = max(self._rerank_max, query.top_k)

        state.recall_k, state.budget_n = recall_k, budget_n

    def recall_and_fuse(self, state: RetrieveState) -> None:
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        record_step = state.run.record
        trace_id, scope_dims = state.run.trace_id, state.run.scope_dims
        scope, enabled = state.scope, state.channels
        recall_k, budget_n = state.recall_k, state.budget_n
        # [3b-6] DomainStore 的全局首选值选择三条 recall/get/rank 路径。
        pipeline = self._domain.preferred_retrieval_pipeline()
        t0 = perf_counter()
        errors: list[ChannelError]
        if pipeline == RetrievalPipeline.RECALL_GET_RANK:
            recalled = self._domain.recall(
                scope, parsed, channels=enabled, recall_limit=recall_k
            )
            materialized = _materialize_recalled(self._domain, scope, recalled, parsed)
            fused = self._fuser.fuse(
                parsed, [batch.candidates for batch in materialized.batches]
            )
            errors = materialized.errors
        elif pipeline == RetrievalPipeline.RECALL_AND_GET_RANK:
            materialized = self._domain.recall_and_get(
                scope, parsed, channels=enabled, recall_limit=recall_k
            )
            materialized = _filter_materialized(materialized, parsed)
            fused = self._fuser.fuse(
                parsed, [batch.candidates for batch in materialized.batches]
            )
            errors = materialized.errors
        else:
            ranked = self._domain.retrieve(
                scope,
                parsed,
                self._fuser,
                channels=enabled,
                recall_limit=recall_k,
                rank_limit=budget_n,
            )
            fused = [
                candidate for candidate in ranked.candidates if _passes_recheck(candidate, parsed)
            ]
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
                redact_for_log(error.message),
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

        state.candidates, state.errors = fused, list(errors)

    def rerank(self, state: RetrieveState) -> None:
        query = state.query
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        survivors = state.candidates
        step, record_step = state.run.step, state.run.record
        trace_id = state.run.trace_id
        # [7] 可选重排：内容已物化，按 ParsedQuery.rewritten 的相关性精排
        do_rerank = query.rerank if query.rerank is not None else (self._reranker is not None)
        reranked = False
        if do_rerank and self._reranker is not None and survivors:
            t0 = perf_counter()
            try:
                # rerank 输入截断护栏：query/doc 超长会把精排请求卡满 120s 超时
                # （issue #231 实测 124.9s）；rewrite 后的 query 可能更长，同样截断。
                query_text, doc_texts = _truncate_rerank_inputs(
                    parsed.rewritten,
                    [candidate.unit.content for candidate in survivors],
                    self._rerank_max_query_chars,
                    self._rerank_max_doc_chars,
                )
                scores = self._reranker.rerank(query_text, doc_texts)
            except BackendError as exc:
                detail = {
                    "skipped": "reranker_unavailable",
                    "error": safe_error_message(exc),
                }
                logger.warning(
                    "Retriever.rerank unavailable: trace_id=%s error=%s",
                    trace_id,
                    redact_for_log(detail["error"]),
                )
                record_step("rerank", (perf_counter() - t0) * 1000.0, len(survivors), detail=detail)
            else:
                order = sorted(range(len(survivors)), key=lambda i: scores[i], reverse=True)
                survivors = [replace(survivors[i], score=scores[i]) for i in order]
                step(
                    "rerank",
                    t0,
                    n=len(survivors),
                    detail={"query_source": "parsed.rewritten", "rewrite": parsed.rewrite_status},
                )
                reranked = True
        elif do_rerank and self._reranker is None:
            # 显式要求精排但装配未注入 reranker：记轨迹让降级可见（阈值走未校准路径）。
            record_step(
                "rerank", 0.0, n=len(survivors), detail={"skipped": "no_reranker_configured"}
            )

        state.candidates, state.calibrated = survivors, reranked

    def rollup(self, state: RetrieveState) -> None:
        query = state.query
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        hierarchy = state.hierarchy
        assert hierarchy is not None, "validate must complete first"
        scope, survivors, errors = state.scope, state.candidates, state.errors
        reuse_children = uses_recalled_children(query)
        step = state.run.step
        t0 = perf_counter()
        rolled = rollup_candidates(self._domain, RollupRequest(
            scope=scope, query=parsed, target_role=hierarchy.hierarchy_role,
            candidates=survivors,
            child_depth=query.expand_depth if reuse_children else 0,
        ))
        survivors = rolled.candidates
        errors = list(errors)
        errors.extend(ChannelError(
            RecallChannel.HIERARCHY, "rollup", issue, f"rollup: {issue}",
        ) for issue in rolled.issues)
        step("rollup", t0, n=len(survivors), detail={
            "algorithm": "maxp", "admitted": str(rolled.admitted_count),
            "boosted": str(rolled.boosted_count), "visited": str(rolled.visited_count),
            "complete": str(not rolled.issues).lower(), "issues": ",".join(rolled.issues),
            "target_role": hierarchy.hierarchy_role.value if hierarchy.hierarchy_role else "",
        })
        parsed.hierarchy_role = hierarchy.hierarchy_role

        state.candidates, state.rolled, state.errors = survivors, rolled, errors

    def threshold(self, state: RetrieveState) -> None:
        query = state.query
        survivors, reranked = state.candidates, state.calibrated
        step = state.run.step
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

        state.candidates = survivors

    def disclose(self, state: RetrieveState) -> None:
        query = state.query
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        final = state.final
        units = {candidate.unit_id: candidate.unit for candidate in final}
        step = state.run.step
        # [10] 渐进式披露（纯内容塑形，复用已点读的 units）
        t0 = perf_counter()
        items = self._discloser.disclose(
            parsed, final, units,
            DisclosureLevel.L0 if query.expand_depth else query.disclosure,
            max_tokens=None if query.expand_depth else query.max_tokens,
        )
        disclose_detail = {}
        if query.expand_depth:
            disclose_detail = {"mode": "prepare_expansion", "budget": "after_root_selection"}
        elif query.disclosure == DisclosureLevel.ADAPTIVE:
            disclose_detail = {
                "mode": "adaptive",
                "max_tokens": str(query.max_tokens or ""),
                "estimated_tokens": str(sum(_estimate_tokens(item.content) for item in items)),
                "levels": ",".join(item.level.value for item in items),
            }
        step("disclose", t0, n=len(items), detail=disclose_detail)

        state.items = items

    def expand(self, state: RetrieveState) -> None:
        query = state.query
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        scope, final, result = state.scope, state.final, state.result
        assert result is not None, "build_result must complete first"
        rolled, recalled_candidates = state.rolled, state.recalled_children
        reuse_children = uses_recalled_children(query)
        parsed_filters = [] if parsed.recheck_filters is None else [parsed.recheck_filters]
        child_filters = and_merge(query.filters, parsed_filters)
        source = ExpansionSource(
            scope=scope,
            query=replace(
                parsed, hierarchy_role=None,
                scalar_filters=child_filters,
                recheck_filters=child_filters,
            ),
            expander=self._expander,
            discloser=self._discloser,
        )
        prepared = prepare_expansion(result, source, final)
        if reuse_children and rolled is not None:
            prepared.keep_recalled(recalled_candidates, rolled.recalled_parents)
        if query.defer_expansion:
            state.result = prepared
            return
        state.result = complete_expansion(result, [prepared], query)

    def finalize(self, state: RetrieveState) -> RetrievalResult:
        query = state.query
        parsed = state.parsed
        assert parsed is not None, "parse must complete first"
        scope, final, result = state.scope, state.final, state.result
        assert result is not None, "build_result must complete first"
        user_filters, step = state.user_filters, state.run.step
        if self._schema_temporal_extension is None:
            return result
        return self._schema_temporal_extension.apply(
            scope,
            query,
            parsed,
            OrdinaryRetrieval(list(final), result),
            user_filters=user_filters,
            record_step=step,
        )


# -- 注册到 RetrieverProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@RetrieverProducer.register("pipeline")
def _build(config):
    # 召回路装配归 CompositeStoreManager 工厂（按 vector_enabled 等开关构建期同步
    # 组装）；这里只取 manager 与其 domain_store——非 Composite 实现自带检索路径。
    manager = StoreManagerProducer.resolve(config)
    kv_name = resolve_name(config, "kv_store")
    domain = manager.domain_store(resolve_name(config, "domain_store"))
    # 精排器与 UnitReader 的真源 kv 与索引/构建侧共享同一实例。
    reranker = (
        RerankerProducer.dep(config, default="overlap")
        if config.get("rerank_enabled", True)
        else None
    )
    discloser = DiscloserProducer.dep(config, default="truncating")
    doc_mode = should_write_document(config.get(WRITE_DOCUMENT_KEY, False))
    unit_reader = None if doc_mode else (
        UnitReader(manager.kv(kv_name)) if manager.has_kv(kv_name) else None
    )
    retriever = PipelineRetriever(
        QueryParserProducer.dep(config, default="simple"),
        FuserProducer.dep(config, default="rrf"),
        discloser,
        unit_reader,
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
        domain_store=domain,
        schema_temporal_extension=build_schema_temporal_extension(
            config,
            manager,
            domain,
            reranker,
            discloser,
        ),
        doc_mode=doc_mode,
        rerank_max_query_chars=int(
            Factory.cfg_get(config, "rerank_max_query_chars", _MAX_RERANK_QUERY_CHARS)
        ),
        rerank_max_doc_chars=int(
            Factory.cfg_get(config, "rerank_max_doc_chars", _MAX_RERANK_DOC_CHARS)
        ),
    )
    retriever.bind_expander(ExpanderProducer.build("default", {"domain_store": domain}, config.ctx))
    return retriever


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


def _materialize_recalled(
    domain: DomainStore,
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
    units = {unit.id: unit for unit in domain.get(scope, unit_ids)}
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


def _passes_recheck(candidate: ScoredMemoryUnit, query: ParsedQuery) -> bool:
    return is_retrieval_candidate(
        candidate.unit,
        query,
        filters=query.recheck_filters,
    )
