"""特征与关联结构：实体 / 关系 / 特征集。

构建层用它们富化记忆并产出图索引的节点与边；检索层用同样的结构
抽取 query 特征做精确/图召回，因此放在 ``common.type_def``。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Entity:
    """命名实体。"""

    text: str = ""  # 实体文本
    type: str = ""  # 实体类型：PERSON / ORG / LOC / ...
    score: float = 0.0  # 识别置信度


@dataclass
class Relation:
    """实体/记忆间的关联（共指、因果、引用、跨会话等）。"""

    source_id: str = ""  # 关联起点（记忆单元/实体 id）
    target_id: str = ""  # 关联终点（记忆单元/实体 id）
    relation: str = ""  # 关系类型（caused_by / refers_to / corefers ...）
    score: float = 0.0  # 关联置信度
    metadata: dict[str, str] = field(default_factory=dict)  # 附加信息


@dataclass
class FeatureSet:
    """从文本抽取的结构化特征（不含稠密向量，向量由 Embedder 单独产出）。"""

    keywords: list[str] = field(default_factory=list)  # 关键词
    entities: list[Entity] = field(default_factory=list)  # 命名实体
    labels: dict[str, str] = field(default_factory=dict)  # 分类标签（维度 → 取值）
