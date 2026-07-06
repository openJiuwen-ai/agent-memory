"""最小实现：:class:`~retrieval.retriever.Retriever`——检索链路编排者。

单次 :meth:`retrieve` 驱动完整链路（Option B：点读/有效性/重排为独立阶段）：
查询理解 → 前置谓词构造 → 并行多路召回 → 融合 → 截断候选预算 → 点读真源 +
有效性过滤 → （可选）重排 → 阈值过滤 → 截断 top_k → 渐进式披露 → 返回结果与轨迹。
scope 作显式首参贯穿下推到各召回路；各子算子由装配注入，本类不含召回/打分逻辑。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from uuid import uuid4

from common.errors import ValidationError
from common.factory.factory import Factory
from common.log import get_logger
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

logger = get_logger(__name__)

_ERROR_CREDENTIAL_RE = re.compile(r"//[^:/@\s]*:[^@\s]+@")
_ERROR_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bauthorization\b['\"]?\s*[:=]\s*['\"]?(?:bearer|basic)\s+)[^'\",\s;&]+"
)
_ERROR_AUTH_VALUE_RE = re.compile(
    r"(?i)(\bauthorization\b['\"]?\s*[:=]\s*['\"]?)(?!(?:bearer|basic)\s+)[^'\",\s;&]+"
)
_ERROR_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\b\s*[:=]\s*[^,;&]+"
)


class PipelineRetriever(Retriever):
    """编排 parse → 谓词 → recall(多路) → fuse → 点读+复核 → rerank → disclose。"""

    def __init__(
        self,
        parser: QueryParser,
        recallers: list[Recaller],
        fuser: Fuser,
        discloser: Discloser,
        unit_reader: UnitReader,
        reranker: Reranker | None = None,
        over_fetch_factor: int = 4,
        over_fetch_floor: int = 60,
        recall_max: int = 100,
        rerank_max: int = 50,
        min_score: float = 0.0,
        min_score_ratio: float = 0.6,
        min_score_ratio_uncalibrated: float = 0.3,
        min_results: int = 0,
    ) -> None:
        self._parser = parser
        self._recallers = recallers
        self._fuser = fuser
        self._discloser = discloser
        self._reader = unit_reader
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

        # [3a] 前置谓词：系统谓词（lifecycle×as_of / 时间窗）并入用户 filters 一同下推
        sys_filters = build_system_filters(
            parsed.as_of, parsed.time_from, parsed.time_to, query.include_archived
        )
        parsed.scalar_filters = list(parsed.scalar_filters) + sys_filters

        # 通道选择：调用级 query.channels 覆盖 parser 建议
        enabled = query.channels if query.channels is not None else parsed.channels
        # 召回超采样（撒宽网）与精排预算（控成本）解耦：
        #   recall_k = max(top_k*factor, floor)  —— 每路召回宽度
        #   budget_n = max(rerank_max, top_k)    —— recheck+rerank 封顶，永不欠 top_k
        recall_k = max(query.top_k * self._over_fetch_factor, self._over_fetch_floor)
        if self._recall_max > 0:
            recall_k = min(recall_k, self._recall_max)  # 硬上限：封顶后端召回压力
        budget_n = max(self._rerank_max, query.top_k)

        # [3b] 并行多路召回：每通道失败隔离——单路异常降级为空集并记轨迹。
        selected_recallers = [
            recaller for recaller in self._recallers if not enabled or recaller.channel() in enabled
        ]
        if not selected_recallers:
            logger.warning(
                "Retriever.retrieve no recallers: trace_id=%s scope_dims=%s "
                "enabled_channels=%s",
                trace_id,
                scope_dims,
                _format_channels(enabled, auto_label="all"),
            )
        per_channel: list[list[ScoredUnit]] = []
        recall_results: dict[int, list[ScoredUnit]] = {}
        recall_steps: dict[int, tuple[float, int, RecallChannel, dict[str, str]]] = {}
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
                        error = _safe_error(exc)
                        detail = {
                            "degraded": type(exc).__name__,
                            "error": error,
                        }
                        logger.warning(
                            "Retriever.recall degraded: trace_id=%s scope_dims=%s channel=%s "
                            "error_type=%s error=%s",
                            trace_id,
                            scope_dims,
                            recaller.channel().value,
                            type(exc).__name__,
                            error,
                        )
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

        # [5] 截断到精排预算；top_k 大于 rerank_max 时自动扩展，避免静默欠召。
        budget = fused[:budget_n]

        # [6] 点读真源（查询 scope 内）+ 后置过滤（纵深防御）：
        #     lifecycle×as_of 有效性 + event-time 窗 + 调用方显式 filters。
        t0 = perf_counter()
        units: dict[str, MemoryUnit] = self._reader.load(scope, [su.unit_id for su in budget])

        def _keep(u: MemoryUnit) -> bool:
            return (
                passes(u, parsed.as_of, query.include_archived)
                and in_event_window(u, parsed.time_from, parsed.time_to)
                and matches_filters(u, query.filters)
            )

        survivors = [su for su in budget if su.unit_id in units and _keep(units[su.unit_id])]
        recheck_dropped = len(budget) - len(survivors)
        step("recheck", t0, n=len(survivors), detail={"dropped": str(recheck_dropped)})
        if recheck_dropped:
            logger.debug(
                "Retriever.recheck dropped: trace_id=%s scope_dims=%s dropped=%d budget=%d",
                trace_id,
                scope_dims,
                recheck_dropped,
                len(budget),
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
        over_fetch_factor=int(Factory.cfg_get(config, "over_fetch_factor", 4)),
        over_fetch_floor=int(Factory.cfg_get(config, "over_fetch_floor", 60)),
        # 回退值 = 出厂默认（与 defaults.py 的 retriever params 保持一致）：实例级覆盖会
        # 整体替换 params，漏写键时落到这里——回退到出厂行为而非静默关闭防护。
        recall_max=int(Factory.cfg_get(config, "recall_max", 100)),
        rerank_max=int(Factory.cfg_get(config, "rerank_max", 50)),
        min_score=float(Factory.cfg_get(config, "min_score", 0.0)),
        min_score_ratio=float(Factory.cfg_get(config, "min_score_ratio", 0.6)),
        min_score_ratio_uncalibrated=float(
            Factory.cfg_get(config, "min_score_ratio_uncalibrated", 0.3)
        ),
        min_results=int(Factory.cfg_get(config, "min_results", 0)),
    )


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def apply_threshold(
    survivors: list[ScoredUnit],
    top_k: int,
    *,
    calibrated: bool,
    min_score: float = 0.0,
    min_score_ratio: float = 0.0,
    min_score_ratio_uncalibrated: float = 0.0,
    min_results: int = 0,
) -> tuple[list[ScoredUnit], dict[str, str]]:
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
    if scope.user:
        dims.append("user")
    if scope.agent:
        dims.append("agent")
    if scope.session:
        dims.append("session")
    return ",".join(dims) if dims else "none"


def _safe_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    text = _ERROR_CREDENTIAL_RE.sub("//<redacted>:<redacted>@", text)
    text = _ERROR_AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _ERROR_AUTH_VALUE_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _ERROR_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[:200]
