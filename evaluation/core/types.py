"""评估框架的数据契约：语料、查询样例、单例观测、指标与运行结果。

设计要点：
- **逻辑 key vs 物理 id**：数据集用稳定的 ``key`` 标识每条语料；真实 ``unit_id``
  在写入时由 ``MemoryAPI.write`` 返回后捕获。标准相关集以 ``key`` 表达，跑分前
  经 harness 的 ``key→unit_id`` 映射落到物理 id，再与召回结果比对。
- **IR 与 QA 共用一套 case**：``relevant_keys`` 服务 IR 排序指标；``expected_answer``
  服务端到端 QA 指标。二者可并存，按注入的 metric 各取所需。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Set

from common.type_def import FilterClause, Scope


@dataclass
class MemorySeed:
    """一条待写入语料：数据集内稳定 ``key`` + 写入内容与元信息。"""

    key: str  # 数据集内稳定标识（相关性标注用它表达相关性，不是物理 unit_id）
    content: str
    scope: Scope
    tags: List[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None


@dataclass
class QueryCase:
    """一个查询样例：query + 评测标注（IR 相关 key 集 / QA 参考答案）+ 检索选项。"""

    query_id: str
    text: str
    scope: Scope
    relevant_keys: Set[str] = field(default_factory=set)  # IR 相关性标注：相关语料的 key
    expected_answer: str = ""  # QA 参考答案（端到端用，可空）
    filters: List[FilterClause] = field(default_factory=list)
    as_of: Optional[datetime] = None
    top_k: int = 10
    # 题目标签（如 LoCoMo category），用于分桶统计
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class CaseOutcome:
    """单个 query 跑完后的原始观测，供各 metric 打分（不含打分逻辑）。"""

    query_id: str
    query_text: str
    ranked_unit_ids: List[str]  # recall 返回的有序 unit_id
    relevant_unit_ids: Set[str]  # relevant_keys 经 key→id 映射后的物理 id 集
    contents: List[str]  # 返回项内容（QA 合成 / token 估算用）
    trajectory: List[object]  # list[TrajectoryStep]：阶段耗时/候选数/降级
    expected_answer: str = ""
    metadata: dict[str, str] = field(default_factory=dict)  # 透传自 QueryCase（如 category）


@dataclass
class MetricResult:
    """单个指标的聚合值 + 可选明细（如分阶段、graded 样本数）。"""

    name: str
    value: float
    detail: dict[str, float] = field(default_factory=dict)


@dataclass
class RunResult:
    """一次评测运行的完整产物：指标汇总 + 逐 case 观测 + 装配摘要。"""

    dataset: str
    n_queries: int
    metrics: List[MetricResult] = field(default_factory=list)
    per_case: List[CaseOutcome] = field(default_factory=list)
    config_summary: dict[str, str] = field(default_factory=dict)


class Dataset(ABC):
    """评测数据集契约：产出待写入语料与查询样例。

    Benchmark 适配器（JSONL 评测标注、LoCoMo 等）实现本接口，把各自原生格式归一到
    ``MemorySeed`` / ``QueryCase``，使 harness/runner 与具体数据集解耦。
    """

    name: str = "dataset"

    @abstractmethod
    def seeds(self) -> Sequence[MemorySeed]:
        """返回需写入记忆系统的全部语料。"""

    @abstractmethod
    def queries(self) -> Sequence[QueryCase]:
        """返回全部查询样例（含评测标注）。"""
