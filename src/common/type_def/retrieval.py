"""Storage 与 Retrieval 共用的检索数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Generic, Protocol, TypeVar

from .feature import Entity
from .filter import FilterExpr
from .memory import MemoryUnit


class RecallChannel(str, Enum):
    """逻辑召回通道；物理分层索引不新增通道。"""

    DOCUMENT = "document"
    KEYWORD = "keyword"
    VECTOR = "vector"
    GRAPH = "graph"
    TEMPORAL = "temporal"


@dataclass
class ChannelEvidence:
    """单条结果在某召回通道内的融合证据。"""

    channel: RecallChannel = RecallChannel.VECTOR
    rank: int = 0
    score: float = 0.0
    weight: float = 1.0
    contribution: float = 0.0


@dataclass
class ParsedQuery:
    """QueryParser 产出的跨层结构化查询。"""

    raw: str = ""
    rewritten: str = ""
    intent: str = ""
    tokens: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    vector: list[float] = field(default_factory=list)
    scalar_filters: FilterExpr | None = None
    recheck_filters: FilterExpr | None = None
    as_of: datetime | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None
    channels: list[RecallChannel] = field(default_factory=list)
    include_archived: bool = False
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass
class ScoredUnit:
    """尚未物化的召回候选。"""

    unit_id: str = ""
    score: float = 0.0
    channel: RecallChannel = RecallChannel.VECTOR
    evidence: list[ChannelEvidence] = field(default_factory=list)


@dataclass
class ScoredMemoryUnit:
    """已取得 MemoryUnit、仍保留召回上下文的候选。"""

    unit: MemoryUnit = field(default_factory=MemoryUnit)
    score: float = 0.0
    channel: RecallChannel = RecallChannel.VECTOR
    evidence: list[ChannelEvidence] = field(default_factory=list)

    @property
    def unit_id(self) -> str:
        return self.unit.id


ScoredCandidate = ScoredUnit | ScoredMemoryUnit


CandidateT = TypeVar("CandidateT", ScoredUnit, ScoredMemoryUnit)


@dataclass
class RecallBatch(Generic[CandidateT]):
    """一个物理召回入口的候选，source 可区分同通道的分层索引。"""

    channel: RecallChannel
    source: str
    candidates: list[CandidateT] = field(default_factory=list)


@dataclass
class ChannelError:
    """单个物理召回入口的结构化错误。"""

    channel: RecallChannel
    source: str
    error_type: str
    message: str


@dataclass
class RecallResult(Generic[CandidateT]):
    """允许部分成功的分入口召回结果。"""

    batches: list[RecallBatch[CandidateT]] = field(default_factory=list)
    errors: list[ChannelError] = field(default_factory=list)


@dataclass
class RankedStorageResult:
    """Storage 内完成 Fuser 后的物化候选。"""

    candidates: list[ScoredMemoryUnit] = field(default_factory=list)
    errors: list[ChannelError] = field(default_factory=list)


class RetrievalPipeline(str, Enum):
    """Storage 全局稳定的首选检索路径。"""

    RECALL_GET_RANK = "recall_get_rank"
    RECALL_AND_GET_RANK = "recall_and_get_rank"
    RETRIEVE = "retrieve"


class CandidateFuser(Protocol):
    """Storage.retrieve 依赖的最小 Fuser 协议。"""

    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredMemoryUnit]]
    ) -> list[ScoredMemoryUnit]:
        ...
