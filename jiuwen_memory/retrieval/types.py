# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""检索层接口涉及的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from jiuwen_memory.common.type_def import (
    ChannelError,
    ChannelEvidence,
    FilterExpr,
    HierarchyKind,
    HierarchyRole,
    MetadataValueType,
    ParsedQuery,
    RecallChannel,
    ScoredMemoryUnit,
    ScoredUnit,
    normalize,
)
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery, validate_expand_depth

__all__ = [
    "ChannelError",
    "ChannelEvidence",
    "DisclosureLevel",
    "ParsedQuery",
    "RecallChannel",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievedItem",
    "ScoredMemoryUnit",
    "ScoredUnit",
    "TrajectoryStep",
]


class DisclosureLevel(str, Enum):
    """渐进式披露层级：按需加载、控制 token（架构 §7 ④）。"""

    L0 = "l0"  # 摘要
    L1 = "l1"  # 片段
    L2 = "l2"  # 全文
    ADAPTIVE = "adaptive"  # 自适应：按预算自动选择实际 L0/L1/L2


@dataclass
class RetrievalQuery:
    """检索请求：原始 query + 过滤条件 + 检索选项。

    不含 scope：检索范围作为显式参数贯穿 ``Retriever.retrieve`` →
    ``Recaller.recall``（query 是「找什么」，scope 是「在谁的范围内找」，
    两条轴分开传），不再随查询对象携带。
    """

    text: str = ""  # 自然语言查询
    # 标签/元数据前置过滤：树形谓词；旧 list/dict 仅在本查询对象边界兼容。
    filters: FilterExpr | None = None
    as_of: datetime | None = None  # 时间点回溯（双时间模型 valid-time）；None 表示当前
    top_k: int = 10  # 直接命中根的上限；展开子不占根名额
    disclosure: DisclosureLevel = DisclosureLevel.L0  # 结果披露层级
    max_tokens: int | None = None  # 披露预算；展开时根/后代共享，None 不设共享上限
    with_trajectory: bool = False  # 是否返回检索轨迹
    # -- 调用级 options（就近覆盖 profile/scope 配置，§13.2）；None = 用装配默认 -- #
    channels: list[RecallChannel] | None = None  # 覆盖启用的召回通道；None 用 parser 建议
    rerank: bool | None = None  # 覆盖重排开关；None 用装配默认（是否注入了 reranker）
    include_archived: bool = False  # 当前态查询是否纳入 archived 记忆
    # 调用方自定义透传配置（源自 Context.extensions）；内核核心不解释，
    # 顺 parser 进 ParsedQuery 供自定义检索模块按约定 key 读取。
    extensions: dict[str, Any] = field(default_factory=dict)
    hierarchy_kind: HierarchyKind | None = None
    hierarchy_role: HierarchyRole | None = None
    span_start: datetime | None = None
    span_end: datetime | None = None
    expand_depth: int = 0
    # 内部两阶段合并开关：公开 SearchOptions 不暴露；跨空间先选根，再统一展开。
    defer_expansion: bool = False

    def __post_init__(self) -> None:
        # 查询对象边界规范化：外部兼容旧输入，内部只保留 FilterExpr | None。
        self.filters = normalize(self.filters)
        hierarchy = HierarchyQuery.from_query(self)
        validate_expand_depth(self.expand_depth, hierarchy.hierarchy_kind)
        self.span_start = hierarchy.span_start
        self.span_end = hierarchy.span_end


@dataclass
class RetrievedItem:
    """最终结果项：三层级内容 + 融合后得分。

    ``abstract`` / ``overview`` / ``content`` 分别对应 L0 摘要 / L1 片段 / L2 全文，
    一次性加载（来自 ``unit.layers.l0`` / ``unit.layers.l1`` / ``unit.content``）。
    调用方按需取用：紧预算用 abstract，中等用 overview，全文用 content。
    ``level`` 标记本次披露的主层级（ADAPTIVE 按 max_tokens 选定）。
    """

    unit_id: str = ""  # 记忆单元 id
    score: float = 0.0  # 直接命中取融合/重排分，展开后代继承根分
    abstract: str = ""  # L0 摘要（unit.layers.l0，50-100 字）
    overview: str = ""  # L1 片段（unit.layers.l1，200-500 字）
    content: str = ""  # L2 全文（unit.content）
    user_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    level: DisclosureLevel = DisclosureLevel.L0  # 本次披露主层级
    system_metadata: dict[str, MetadataValueType] = field(default_factory=dict)
    parent_id: str = ""  # 真源结构父节点 id；未挂父或未启用结构时为空


@dataclass
class TrajectoryStep:
    """检索轨迹中的一步：可观测、可调试，非黑盒（§7 ⑤）。"""

    stage: str = ""  # 阶段：parse / recall / fuse / rerank / disclose
    channel: RecallChannel | None = None  # 涉及的召回通道（非召回步为 None）
    candidate_count: int = 0  # 本步产出的候选数
    cost_ms: float = 0.0  # 本步耗时（毫秒）
    detail: dict[str, str] = field(default_factory=dict)  # 附加信息（参数/截断原因等）


@dataclass
class RetrievalResult:
    """一次检索的返回：结果项 + 可选的完整检索轨迹。"""

    items: list[RetrievedItem] = field(default_factory=list)  # 排序后的结果项
    trajectory: list[TrajectoryStep] = field(
        default_factory=list
    )  # 检索轨迹（with_trajectory 时返回）
    errors: list[ChannelError] = field(default_factory=list)  # 部分通道失败，不依赖轨迹开关
