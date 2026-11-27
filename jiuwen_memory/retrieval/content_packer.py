# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ContentPacker — Top-K 检索后的记忆筛选与无模型正文压缩算子（retrieval F15）。

``PipelineRetriever`` 在 ``cut_to_top_k`` 之后、``Discloser`` 之前调用本算子：对最终胜出的
K 条已物化候选（:class:`~common.type_def.ScoredMemoryUnit`）在 token 预算内做价值重排筛选与
单条正文压缩，仍以候选列表（逐条 ``MemoryUnit``）向后传递给 ``Discloser``。
阶段方法 ``pack_content`` 已在 :class:`~retrieval.staged_retriever.StagedRetriever` 声明。

职责边界：
- 与 :class:`~retrieval.fuser.Fuser` / :class:`~common.reranker.Reranker` 分离：二者处于精排与
  ``top_k`` 截断之前（面对粗排池），不能提前改写正文污染精排打分，也无法按最终 K 条计算联合预算；
- 与 :class:`~retrieval.discloser.Discloser` 分离：Discloser 受限于「按序一对一单条塑形、禁止删条」
  （铁律 10）且被树展开 ``Expander`` 复用；本算子承担跨条预算下的去重/删条与正文精简，与任意
  Discloser 正交组合。
- 请求级 ``content_pack_keep_count``（经 search ``Context.extensions``，不进 globals）为 true 时
  不得减少候选条数，仅允许压缩正文；轨迹 ``suppressed_drops`` 标识本应删条但被业务抑制的数量。
"""

from __future__ import annotations

from abc import abstractmethod
from enum import Enum

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperator
from jiuwen_memory.retrieval.types import ParsedQuery


class PackStrategy(str, Enum):
    """Top-K 内容打包策略。"""

    AUTO = "auto"  # 按实际候选条数自动路由：≤10 role_compact / 11-20 telegraph / >20 drop
    ROLE_COMPACT = "role_compact"  # 角色称呼缩写为 U/A + 去同日重复日期前缀；仍超预算时尾部转电报式
    TELEGRAPH = "telegraph"  # 高价值头部保留原文，尾部剔除重复并去英文虚词改电报式
    DROP = "drop"  # 按 Mixed-Score 贪心保留预算内高价值条目，其余整条剔除


def to_pack_strategy(raw: PackStrategy | str) -> PackStrategy:
    """将配置中的策略名（枚举值或大小写变体）解析为 :class:`PackStrategy`。"""
    if isinstance(raw, PackStrategy):
        return raw
    if not isinstance(raw, str):
        raise ValidationError(f"pack strategy must be a str, got {type(raw).__name__}")
    normalized = raw.strip().lower()
    try:
        return PackStrategy(normalized)
    except ValueError:
        pass
    try:
        return PackStrategy[raw.strip().upper()]
    except KeyError:
        allowed = ", ".join(item.value for item in PackStrategy)
        raise ValidationError(
            f"invalid pack strategy {raw!r}, must be one of: {allowed}"
        ) from None


class ContentPackerProducer(Factory):
    """ContentPacker 的注册式工厂（顶层命名空间 ``content_packer``）。

    各实现在 ``content_packer_impl`` 下以 ``@ContentPackerProducer.register("<名>")`` 自注册，
    由 :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "content_packer"


class ContentPacker(RetrievalOperator):
    """Top-K 后置筛选与压缩契约：输入 K 条候选，输出预算内保留/压缩后的候选子序列。"""

    def explain(self) -> dict[str, str]:
        """返回算子静态配置摘要，记入 ``content_pack`` 轨迹；业务实现可不覆盖。"""
        return {}

    @abstractmethod
    def pack(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
    ) -> list[ScoredCandidate]:
        """在预算内筛选并压缩 Top-K 候选，返回逐条候选列表。

        契约约束：
        - ``candidates`` 为空时返回空列表；非空时至少保留 1 条候选；
        - 返回列表为输入候选的多重集子集（每个 ``unit_id`` 出现次数不超过输入，不得伪造新 id），
          改写正文时应以 ``dataclasses.replace`` 浅拷贝而非原地修改，确保异常回退时原候选完好。
        """

    def pack_with_detail(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        *,
        keep_count: bool = False,
        **_kwargs: object,
    ) -> tuple[list[ScoredCandidate], dict[str, str]]:
        """执行 :meth:`pack` 并附带单次执行的轨迹统计；业务子类可只实现 :meth:`pack`。

        ``keep_count`` 标识请求级「禁止删条」约束；``**_kwargs`` 吸收后续扩展开关，
        默认实现忽略这些开关（主链路对返回条数另有护栏兜底）。
        """
        _ = keep_count
        return self.pack(query, candidates), {}
