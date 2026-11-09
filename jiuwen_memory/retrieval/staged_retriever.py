# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""分阶段检索契约与每次调用独占的状态；编排由具体 Retriever 提供。"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    ChannelError,
    FilterExpr,
    ParsedQuery,
    Scope,
    ScoredMemoryUnit,
)
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery
from jiuwen_memory.retrieval.expansion import uses_recalled_children
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import (
    RecallChannel,
    RetrievalQuery,
    RetrievalResult,
    RetrievedItem,
    RollupResult,
    TrajectoryStep,
)

logger = get_logger(__name__)


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


@dataclass
class RetrieveRun:
    """请求独占的轨迹与计时，不存入共享 Retriever 实例。"""

    scope_dims: str
    trace: bool
    trace_id: str = field(default_factory=lambda: uuid4().hex[:16])
    started_at: float = field(default_factory=perf_counter)
    trajectory: list[TrajectoryStep] = field(default_factory=list)
    fused_count: int = 0

    def record(
        self, stage: str, cost_ms: float, n: int = 0,
        channel: RecallChannel | None = None, detail: dict[str, str] | None = None,
    ) -> None:
        if self.trace:
            self.trajectory.append(TrajectoryStep(
                stage=stage, channel=channel, candidate_count=n,
                cost_ms=cost_ms, detail=detail or {},
            ))

    def step(
        self, stage: str, t0: float, n: int = 0,
        channel: RecallChannel | None = None, detail: dict[str, str] | None = None,
    ) -> None:
        self.record(stage, (perf_counter() - t0) * 1000.0, n, channel, detail)

    def log_done(self, fused: int, survivors: int, returned: int) -> None:
        logger.debug(
            "Retriever.retrieve done: trace_id=%s scope_dims=%s fused=%d survivors=%d "
            "returned=%d cost_ms=%.2f",
            self.trace_id, self.scope_dims, fused, survivors, returned,
            (perf_counter() - self.started_at) * 1000.0,
        )


@dataclass
class RetrieveState:
    """阶段产物；输入引用只读使用，字段写集与阶段前置条件见 S04。"""

    scope: Scope
    query: RetrievalQuery
    run: RetrieveRun
    hierarchy: HierarchyQuery | None = None
    parsed: ParsedQuery | None = None
    user_filters: FilterExpr | None = None
    channels: list[RecallChannel] | None = None
    recall_k: int = 0
    budget_n: int = 0
    candidates: list[ScoredMemoryUnit] = field(default_factory=list)
    errors: list[ChannelError] = field(default_factory=list)
    calibrated: bool = False
    rolled: RollupResult | None = None
    recalled_children: list[ScoredMemoryUnit] = field(default_factory=list)
    final: list[ScoredMemoryUnit] = field(default_factory=list)
    items: list[RetrievedItem] = field(default_factory=list)
    result: RetrievalResult | None = None

    @classmethod
    def start(cls, scope: Scope, query: RetrievalQuery) -> RetrieveState:
        dims = _scope_log_dims(scope)
        run = RetrieveRun(dims, query.with_trajectory)
        return cls(scope, query, run)

    def log_start(self) -> None:
        """校验成功后记录开始日志；构造状态时不格式化未校验的输入。"""
        query, run = self.query, self.run
        logger.debug(
            "Retriever.retrieve start: trace_id=%s scope_dims=%s top_k=%d channels=%s "
            "rerank=%s disclosure=%s query_len=%d pipeline=%s",
            run.trace_id, run.scope_dims, query.top_k,
            _format_channels(query.channels, auto_label="auto"),
            query.rerank, query.disclosure.value, len(query.text), query.pipeline,
        )

    def empty_result(self) -> RetrievalResult:
        return RetrievalResult(trajectory=self.run.trajectory)

    def cut_to_budget(self) -> None:
        self.run.fused_count = len(self.candidates)
        self.candidates = list(self.candidates[:self.budget_n])
        self.recalled_children = (
            list(self.candidates) if uses_recalled_children(self.query) else []
        )
        self.run.record("recheck", 0.0, len(self.candidates), detail={"dropped": "0"})

    def cut_to_top_k(self) -> None:
        self.final = self.candidates[:self.query.top_k]
        if self.query.expand_depth:
            self.run.record(
                "parent_recall", 0.0, len(self.final), detail={"top_k": str(self.query.top_k)},
            )

    def build_result(self) -> None:
        self.run.log_done(self.run.fused_count, len(self.candidates), len(self.items))
        self.result = RetrievalResult(
            items=self.items, trajectory=self.run.trajectory, errors=self.errors,
        )


class StagedRetriever(Retriever):
    """阶段接口；retrieve 仍是抽象方法，变体可继承具体编排实现。"""

    @abstractmethod
    def validate(self, state: RetrieveState) -> None:
        """校验 query，生成 hierarchy。"""

    @abstractmethod
    def parse(self, state: RetrieveState) -> bool:
        """生成 parsed；无检索意图返回 False。"""

    @abstractmethod
    def build_filters(self, state: RetrieveState) -> None:
        """系统谓词与用户表达式以外层 AND 合并。"""

    @abstractmethod
    def select_channels(self, state: RetrieveState) -> None:
        """解析调用级通道选择。"""

    @abstractmethod
    def plan_budget(self, state: RetrieveState) -> None:
        """生成召回宽度与精排预算。"""

    @abstractmethod
    def recall_and_fuse(self, state: RetrieveState) -> None:
        """召回、真源复核、融合；复核必须先于融合。"""

    @abstractmethod
    def rerank(self, state: RetrieveState) -> None:
        """可选精排；不可修改 recalled_children 快照。"""

    @abstractmethod
    def rollup(self, state: RetrieveState) -> None:
        """上卷并恢复 parsed 的目标角色。"""

    @abstractmethod
    def threshold(self, state: RetrieveState) -> None:
        """在最终 top_k 之前应用阈值。"""

    @abstractmethod
    def disclose(self, state: RetrieveState) -> None:
        """对 final 纯内容塑形，写入 items。"""

    @abstractmethod
    def expand(self, state: RetrieveState) -> None:
        """准备或完成展开；延迟展开结果不进入 finalize。"""

    @abstractmethod
    def finalize(self, state: RetrieveState) -> RetrievalResult:
        """对普通结果执行可选后处理。"""
