"""检索层接口涉及的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from common.type_def import Entity, FilterClause


class DisclosureLevel(str, Enum):
    """渐进式披露层级：按需加载、控制 token（架构 §7 ④）。"""

    L0 = "l0"  # 摘要
    L1 = "l1"  # 片段
    L2 = "l2"  # 全文
    ADAPTIVE = "adaptive"  # 自适应：按预算自动选择实际 L0/L1/L2


class RecallChannel(str, Enum):
    """召回通道——**逻辑**召回路（§6.2）+ 时序过滤。

    通道到物理 Store 的映射由检索层装配内部决定，非 1:1：一路可对应一个
    Store，多路也可合到一个 Store（如同时请求 KEYWORD+VECTOR 时走
    FusionStore 一次召回），TEMPORAL 通常是叠加在其他通道上的时间过滤而非
    独立 Store。因此「某通道没有专属 Store」不是缺口。
    """

    DOCUMENT = "document"  # 文档定位：路径/章节式
    KEYWORD = "keyword"  # 关键词/全文 BM25
    VECTOR = "vector"  # 向量语义相似
    GRAPH = "graph"  # 图：实体-关系多跳遍历
    TEMPORAL = "temporal"  # 时序过滤：有效期/时间点（双时间模型，多叠加在其他通道上）


@dataclass
class ChannelEvidence:
    """单条结果在某召回通道内的融合证据。"""

    channel: RecallChannel = RecallChannel.VECTOR  # 来源通道
    rank: int = 0  # 通道内名次（0-based）
    score: float = 0.0  # 通道原始分
    weight: float = 1.0  # 融合时使用的通道权重
    contribution: float = 0.0  # 对最终融合分的贡献


@dataclass
class RetrievalQuery:
    """检索请求：原始 query + 过滤条件 + 检索选项。

    不含 scope：检索范围作为显式参数贯穿 ``Retriever.retrieve`` →
    ``Recaller.recall``（query 是「找什么」，scope 是「在谁的范围内找」，
    两条轴分开传），不再随查询对象携带。
    """

    text: str = ""  # 自然语言查询
    # 标签/元数据前置过滤（结构化谓词，AND 组合）。
    filters: list[FilterClause] = field(default_factory=list)
    as_of: datetime | None = None  # 时间点回溯（双时间模型 valid-time）；None 表示当前
    top_k: int = 10  # 返回条数
    disclosure: DisclosureLevel = DisclosureLevel.L0  # 结果披露层级
    max_tokens: int | None = None  # 自适应披露预算；None 表示使用 discloser 默认策略
    with_trajectory: bool = False  # 是否返回检索轨迹
    # -- 调用级 options（就近覆盖 profile/scope 配置，§13.2）；None = 用装配默认 -- #
    channels: list[RecallChannel] | None = None  # 覆盖启用的召回通道；None 用 parser 建议
    rerank: bool | None = None  # 覆盖重排开关；None 用装配默认（是否注入了 reranker）
    include_archived: bool = False  # 当前态查询是否纳入 archived 记忆
    # 调用方自定义透传配置（源自 Context.extensions）；内核核心不解释，
    # 顺 parser 进 ParsedQuery 供自定义检索模块按约定 key 读取。
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedQuery:
    """查询理解的产出：结构化的查询表示，供各召回通道直接消费。

    软召回信号（tokens/keywords/entities/vector，模糊打分）与硬前置过滤
    （scalar_filters，索引级门槛）分开承载：前者决定「召回什么」，后者
    决定「先排除什么」，不能互相折叠。as_of（valid-time）与 time_from/
    time_to（event-time）也是两条独立时间轴，见各字段说明。
    """

    raw: str = ""  # 原始 query
    rewritten: str = ""  # LLM 改写/补全后的 query
    intent: str = ""  # 意图标签
    tokens: list[str] = field(default_factory=list)  # 分词结果（关键词通道用）
    keywords: list[str] = field(default_factory=list)  # 抽取的关键词
    entities: list[Entity] = field(default_factory=list)  # 实体（图通道用）
    vector: list[float] = field(default_factory=list)  # query 向量（向量通道用）
    # 硬前置过滤谓词（源自 RetrievalQuery.filters），组装进各 Store 查询 filters。
    scalar_filters: list[FilterClause] = field(default_factory=list)
    # valid-time 回溯点：过滤 [t_valid, t_invalid]，问「T 时刻哪个版本有效」。
    as_of: datetime | None = None
    time_from: datetime | None = None  # 事件时间下界（event-time）：从 query 文本解析，过滤 t_event
    time_to: datetime | None = None  # 事件时间上界（event-time）
    channels: list[RecallChannel] = field(default_factory=list)  # 建议启用的通道
    # 透传自 RetrievalQuery.extensions，供自定义 Recaller 按约定 key 读取（内核核心不解释）。
    extensions: dict[str, str] = field(default_factory=dict)


@dataclass
class ScoredUnit:
    """单路召回的候选：记忆单元 id + 得分 + 来源通道。"""

    unit_id: str = ""  # 记忆单元 id
    score: float = 0.0  # 本通道内的召回得分
    channel: RecallChannel = RecallChannel.VECTOR  # 来源通道
    evidence: list[ChannelEvidence] = field(default_factory=list)  # 融合证据明细


@dataclass
class RetrievedItem:
    """最终结果项：三层级内容 + 融合后得分。

    ``abstract`` / ``overview`` / ``content`` 分别对应 L0 摘要 / L1 片段 / L2 全文，
    一次性加载（来自 ``unit.layers.l0`` / ``unit.layers.l1`` / ``unit.content``）。
    调用方按需取用：紧预算用 abstract，中等用 overview，全文用 content。
    ``level`` 标记本次披露的主层级（ADAPTIVE 按 max_tokens 选定）。
    """

    unit_id: str = ""  # 记忆单元 id
    score: float = 0.0  # 融合/重排后的最终得分
    abstract: str = ""  # L0 摘要（unit.layers.l0，50-100 字）
    overview: str = ""  # L1 片段（unit.layers.l1，200-500 字）
    content: str = ""  # L2 全文（unit.content）
    level: DisclosureLevel = DisclosureLevel.L0  # 本次披露主层级


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
