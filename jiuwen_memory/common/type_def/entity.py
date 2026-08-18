"""实体反向索引的跨层数据结构层。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from jiuwen_memory.common.type_def.scope import Scope


def hash_entity_text(normalized_name: str) -> str:
    """归一化实体名的 sha256 哈希。

    索引里持久化的是哈希而非明文，ES store 泄露也不暴露实体文本。哈希同时是
    倒排索引的精确匹配 key（hash 精确匹配的第一级）。
    """
    return hashlib.sha256(normalized_name.encode()).hexdigest()


class EntityType(str, Enum):
    """技术型实体分类（检索锚定用，不表达完整业务 ontology）。

    LLM 抽取的实体统一标 PROPER（专名级实体）；保留 QUOTED/TOPIC/IDENTIFIER
    供后续按需细化分类，当前生产路径只产 PROPER。
    """

    PROPER = "PROPER"
    QUOTED = "QUOTED"
    TOPIC = "TOPIC"
    IDENTIFIER = "IDENTIFIER"


@dataclass(frozen=True)
class EntityMention:
    """从 memory 或 query 中抽出的一个实体提及，已带归一化名称。"""

    entity_type: str
    display_name: str
    normalized_name: str


@dataclass(frozen=True)
class EntityStoreFilters:
    """实体检索的硬隔离字段（term 过滤用），不包含 space_id。

    space_id 走 ES routing + 文档字段（同 shard 聚簇），不是 term 过滤的隔离
    维度——与原设计一致，``ElasticsearchEntityStore._build_filters`` 按
    actor_id 单段生成 term 子句。agent/session 不作隔离维度：同 user 下跨
    agent、跨 session 共享实体索引（实体是 user 级知识，不随会话/agent 切换）。
    """

    actor_id: str | None = None

    @classmethod
    def from_scope(cls, scope: Scope) -> "EntityStoreFilters":
        """从 Scope 构造隔离字段。

        actor_id ← scope.user（用户隔离，唯一 term 过滤维度）
        全空时返回 None（不按 user 过滤，仅靠 space_id routing 隔离）。
        """
        return cls(
            actor_id=scope.user or None,
        )

    def key(self) -> tuple[str | None, ...]:
        """分组 key：同 (space_id, actor_id) 的 unit 共享一次 bulk 查询/写入。"""
        return (self.actor_id,)


@dataclass(frozen=True)
class EntityRecord:
    """独立存放的实体文档：该实体关联到哪些 memory。

    linked_memory_ids 存 unit.id（str），与召回侧 ``ScoredUnit.unit_id`` 对齐——
    召回命中的实体记录取 linked_memory_ids 直接就是候选 unit_id 列表。
    """

    id: str
    space_id: str
    entity_text: str
    entity_type: str
    linked_memory_ids: tuple[str, ...]
    filters: EntityStoreFilters
    entity_text_hash: str = ""


@dataclass(frozen=True)
class EntityLinkResult:
    """memory 写入后维护实体反向索引的结果统计。"""

    extracted_count: int = 0
    inserted_count: int = 0
    updated_count: int = 0
    deleted_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0  # 因 tier 不在准入白名单而跳过的记录数


class EntityOpType(str, Enum):
    """entity store bulk API 支持的变更操作。"""

    INSERT = "INSERT"  # 新建实体，index 全文档
    LINK = "LINK"  # 脚本追加 memory_id 到 linked_memory_ids
    UNLINK_UPDATE = "UNLINK_UPDATE"  # doc 局部更新 linked_memory_ids（unlink 后非空）
    DELETE = "DELETE"  # 删除实体（unlink 后为空）


@dataclass(frozen=True)
class EntityOperation:
    """单条 entity-store 变更命令，用于批量提交。

    INSERT/UNLINK_UPDATE 携带 ``record``；DELETE 携带 ``record_id``；
    LINK 携带 ``record_id`` + ``link_memory_ids``。
    """

    type: EntityOpType
    record: EntityRecord | None = None
    record_id: str | None = None
    link_memory_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityBatchResult:
    """批量 entity 写入的 per-item 结果——partial failure 不抛异常。

    与主链路 BulkWriteError（all-or-nothing）不同，entity 链路需要 per-item
    粒度：一条失败不影响其他，失败 id 回传给 linker 计 failed_count。
    """

    successful_ids: list[str]
    failed_ids: list[str]
