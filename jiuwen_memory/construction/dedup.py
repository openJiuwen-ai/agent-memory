# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dedup — 去重召回（架构 §8 演进去重的召回侧）。

Evolver 的 EXTRACT/CONSOLIDATE 模式需判定候选是否与已有记忆重复/可合并/可替代。
召回链（向量化/分词 → Store.search → 加载 unit → 过滤聚合）本属「用哪个索引」的选择，
故抽象成本接口，由装配按 ``vector_enabled`` 选实现：

- 向量开 → :class:`~construction.dedup_impl.vector_dedup.VectorDedup`
  （Embedder → VectorStore.search，cosine 计分）
- 只倒排 → :class:`~construction.dedup_impl.keyword_dedup.KeywordDedup`
  （FulltextStore.search，词重叠率计分，与 cosine 同为 0~1 量纲）

判定（中/高阈值 + LLM 语义判定）仍留在 Evolver——本接口只产出
``list[(MemoryUnit, score)]``（已加载、已滤自身、已按 ``min_similarity`` 过滤、
已按 unit 聚合取 max）。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    ISOLATED_MEMORY_TYPES,
    MEMORY_TYPE_FILTER_FIELD,
    FilterClause,
    FilterOp,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.storage.kv import KVStore, load_units

from .base import ConstructionOperator

logger = get_logger(__name__)


class DedupProducer(Factory):
    """Dedup 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名（如 vector / keyword）。各实现在 ``dedup_impl`` 下以
    ``@DedupProducer.register("<名>")`` 自注册——由
    :func:`construction.bootstrap.register_constructors` 统一触发。
    """

    TOP_NAME = "dedup"


class Dedup(ConstructionOperator):
    """去重召回路：对一条候选召回已有相似记忆。

    实现负责：组装底层 Store 的检索查询 → 召回 top-k → 加载 unit → 过滤自身与
    非 ACTIVE → 按 unit 聚合取 max score → 按 ``min_similarity`` 过滤低分项。
    返回结果按 score 降序，score 量纲 0~1（向量=cosine，倒排=词重叠率），供 Evolver
    做统一的阈值 + LLM 判定。
    """

    def __init__(
        self,
        kv: KVStore,
        *,
        min_similarity: float = 0.5,
        top_k: int = 5,
        tier_filter: bool = False,
        scope_filter: bool = True,
    ) -> None:
        self._kv = kv
        self._min_similarity = min_similarity
        self._top_k = top_k
        self._tier_filter = tier_filter
        self._scope_filter = scope_filter

    @abstractmethod
    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        """对候选召回已有相似记忆，返回 (unit, score) 列表（按 score 降序）。

        已完成：加载 unit、过滤候选自身、过滤非 ACTIVE、按 unit 聚合取 max、
        按 ``min_similarity`` 过滤低分。空列表表示无相似记忆（Evolver 判 ADD）。
        实现内部任何异常都应吞掉并返回空列表——去重是尽力而为，不可阻断演进。
        """

    # -- 共享：从真源按 unit_id 加载 MemoryUnit（跨 scope 回退） ---------------- #
    def _load_unit(self, unit_id: str, scope: Scope) -> MemoryUnit | None:
        """从 KV 真源点读 MemoryUnit；缺失时跨作用域回退。"""
        try:
            units = load_units(self._kv, scope, [unit_id])
            if units:
                return units[0]
            for candidate_scope in self._kv.scopes():
                units = load_units(self._kv, candidate_scope, [unit_id])
                if units:
                    return units[0]
            return None
        except Exception:
            logger.warning("Dedup._load_unit: failed to load unit %s", unit_id)
            return None


def same_scope(a, b) -> bool:
    """比较两个 Scope 的 org + space + user 是否相同（跨 session/agent 的去重粒度）。

    VectorDedup / KeywordDedup 共用的 scope 过滤辅助——去重召回时按 org+space+user
    聚合（同租户同用户跨 session/agent 视为同 scope），与检索层 scope 隔离粒度对齐。
    """
    return (
        getattr(a, "org", "") == getattr(b, "org", "")
        and getattr(a, "space", "") == getattr(b, "space", "")
        and getattr(a, "user", "") == getattr(b, "user", "")
    )


def memory_type_filter_clauses(candidate: MemoryUnit) -> list[FilterClause]:
    """类型隔离的去重召回谓词（mem2.0 §2.3「去重按类型隔离」）。

    按候选的 ``memory_type`` 加过滤：隔离值取等值、其余取 ``NOT_IN``——对话记忆
    与经验不互判为重复，不会把一条对话记忆记成经验的一次复现（复现作者记录、
    复现要求都建立在去重能把同类认作同一条之上，跨类型的误判会污染计数）。
    ``NOT_IN`` 的字段缺失命中（不带 ``memory_type`` 的对话条目不被排除）由
    存储层的 metadata 形态语义保证。谓词供消费 ``filters`` 的后端下推（防经验
    条目占满 top_k 名额）；不消费的后端由 :func:`memory_type_matches` 在加载
    unit 后兜底。
    """
    value = str(candidate.system_metadata.get("memory_type", "")).strip()
    if value in ISOLATED_MEMORY_TYPES:
        return [FilterClause(MEMORY_TYPE_FILTER_FIELD, FilterOp.EQ, value)]
    return [
        FilterClause(
            MEMORY_TYPE_FILTER_FIELD, FilterOp.NOT_IN, sorted(ISOLATED_MEMORY_TYPES)
        )
    ]


def memory_type_matches(candidate: MemoryUnit, unit: MemoryUnit) -> bool:
    """:func:`memory_type_filter_clauses` 的加载后判定形态（两个 Dedup 实现都在
    加载 unit 后调用——倒排路的 ``TextQuery`` filters 与向量路的 ``VectorQuery``
    filters 均不被内存后端消费，谓词下推只对 milvus/pgvector 等后端生效，内存
    后端靠这一层兜底）。候选是隔离类型时只认同类；否则排除两类隔离条目。
    """
    candidate_type = str(candidate.system_metadata.get("memory_type", "")).strip()
    unit_type = str(unit.system_metadata.get("memory_type", "")).strip()
    if candidate_type in ISOLATED_MEMORY_TYPES:
        return unit_type == candidate_type
    return unit_type not in ISOLATED_MEMORY_TYPES
