"""存储层接口涉及的数据类型（仅定义结构，与具体后端无关）。

**scope 不在这些结构体里**：它是各 Store 方法的显式第一入参（``scope: Scope``，
架构 §7「scope 是独立轴、显式串参，不随查询对象携带、也不混进过滤条件」），
由存储层据此做原生隔离——写入按 scope 落库、检索/点查/删除物理约束在该 scope
内。``filters``/``scalar_filters`` 只承载 scope 之外的额外元数据谓词，由上层
（Recaller/IndexBuilder）组装查询时显式填入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jiuwen_memory.common.type_def import FilterExpr, MemoryUnit, normalize


@dataclass
class ScoredID:
    """检索结果项：记录 id 及其相关性得分。"""

    id: str  # 记录 id
    score: float  # 相关性得分（越大越相关）
    metadata: dict[str, Any] | None = None  # 命中记录的元数据；None 表示未携带


@dataclass
class KVMemoryListResult:
    """KVStore.list 的结果：当前页原始条目与分页前匹配总数。"""

    entries: list[tuple[str, bytes]] = field(default_factory=list)
    count: int = 0


@dataclass
class MemoryListResult:
    """Storage.list 的领域结果。"""

    items: list[MemoryUnit] = field(default_factory=list)
    count: int = 0


@dataclass
class ScoredHit:
    """检索命中项：id + 相关性得分 + 按需回带的元数据 payload。"""

    id: str  # 记录 id（与 ScoredID.id 同义：scope 内逻辑主键）
    score: float  # 相关性得分（越大越相关，与 ScoredID.score 同口径）
    metadata: dict[str, Any] = field(default_factory=dict)  # 命中行按需回带的元数据


# --------------------------------------------------------------------------- #
# vector
# --------------------------------------------------------------------------- #


@dataclass
class VectorRecord:
    """向量行：id + embedding + 可选元数据（scope 由写入方法入参提供）。"""

    id: str  # 向量行逻辑 id（与记忆单元/chunk 对应，scope 内唯一）
    vector: list[float]  # 稠密向量（维度须与索引一致）
    metadata: dict[str, Any] = field(default_factory=dict)  # scope 之外的额外过滤元数据


@dataclass
class VectorQuery:
    """ANN 查询：查询向量 + top_k + 额外元数据过滤（scope 由检索方法入参提供）。"""

    vector: list[float]  # 查询向量
    top_k: int = 10  # 返回条数
    filters: FilterExpr | None = None  # scope 之外的元数据谓词（AND/OR/NOT 树；None 表示无过滤）
    # True 时支持的后端在 ANN 命中同时回带记录 metadata（填入 ScoredID.metadata）。
    return_metadata: bool = False

    def __post_init__(self) -> None:
        self.filters = normalize(self.filters)  # 边界规范化：兼容旧 list，内部统一 FilterExpr


# --------------------------------------------------------------------------- #
# fulltext
# --------------------------------------------------------------------------- #


@dataclass
class Document:
    """全文文档：id + 待索引文本 + 可选元数据（scope 由写入方法入参提供）。"""

    id: str  # 文档逻辑 id（scope 内唯一）
    text: str  # 待索引正文
    metadata: dict[str, Any] = field(default_factory=dict)  # scope 之外的额外过滤元数据


@dataclass
class TextQuery:
    """关键词查询：查询文本 + top_k + 额外元数据过滤（scope 由检索方法入参提供）。"""

    text: str  # 查询文本（关键词/短语）
    top_k: int = 10  # 返回条数
    filters: FilterExpr | None = None  # scope 之外的元数据谓词（AND/OR/NOT 树；None 表示无过滤）

    def __post_init__(self) -> None:
        self.filters = normalize(self.filters)  # 边界规范化：兼容旧 list，内部统一 FilterExpr


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    """图节点：id + 标签 + 属性（scope 由写入方法入参提供）。"""

    id: str  # 节点逻辑 id（scope 内唯一）
    label: str = ""  # 节点标签/类型（如 PERSON / EVENT / TOPIC）
    properties: dict[str, Any] = field(default_factory=dict)  # 节点属性


@dataclass
class Edge:
    """图边：id + 起止节点 + 关系类型 + 属性（scope 由写入方法入参提供）。"""

    id: str  # 边逻辑 id（scope 内唯一）
    source: str  # 起点节点 id
    target: str  # 终点节点 id
    relation: str = ""  # 关系类型（如 caused_by / refers_to）
    properties: dict[str, Any] = field(default_factory=dict)  # 边属性


@dataclass
class GraphQuery:
    """图遍历查询：从 start_id 出发按关系扩展邻域/子图（scope 由检索方法入参提供）。"""

    start_id: str  # 遍历起点节点 id
    relation: str | None = None  # 限定关系类型；None 表示不限定
    depth: int = 1  # 遍历深度（跳数）
    limit: int = 100  # 返回节点数上限


# --------------------------------------------------------------------------- #
# fusion（向量 + 倒排 + 正排）
# --------------------------------------------------------------------------- #


@dataclass
class FusionRecord:
    """融合行：同一 id 上同时携带向量（ANN）、文本（倒排）、标量字段（过滤）
        与原始值（正排 by-id 读取）；scope 由写入方法入参提供。
    """

    id: str  # 行逻辑 id（scope 内唯一，同时服务向量/倒排/正排三条路）
    vector: list[float] | None = None  # 稠密向量（ANN 召回用）；None 表示无
    text: str | None = None  # 文本（倒排/关键词相关性用）；None 表示无
    scalars: dict[str, Any] = field(default_factory=dict)  # scope 之外的标量字段
    value: bytes | None = None  # 原始值（正排按 id 点读用）；None 表示无


@dataclass
class FusionQuery:
    """融合查询：向量近邻 + 文本相关性 + 标量谓词在一次调用内组合（scope 由检索
        方法入参提供）；vector_weight 控制向量得分与文本得分的混合比例。
    """

    vector: list[float] | None = None  # 查询向量；None 则不走向量召回
    text: str | None = None  # 查询文本；None 则不计文本相关性
    scalar_filters: FilterExpr | None = None  # scope 之外的标量谓词（AND/OR/NOT 树）
    top_k: int = 10  # 返回条数
    vector_weight: float = 0.5  # 向量得分权重（1.0 纯向量，0.0 纯文本）

    def __post_init__(self) -> None:
        self.scalar_filters = normalize(self.scalar_filters)  # 边界规范化：兼容旧 list，内部统一


# --------------------------------------------------------------------------- #
# fs
# --------------------------------------------------------------------------- #


@dataclass
class FileStat:
    """文件元信息：规范引用 + 大小 + 内容类型 + 时间戳。"""

    ref: str  # 规范引用（insert 返回的 ref）
    size: int  # 文件大小（字节）
    content_type: str = ""  # MIME 类型
    created_at: float = 0.0  # 创建时间（Unix 秒）
    updated_at: float = 0.0  # 最后修改时间（Unix 秒）
