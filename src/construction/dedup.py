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
from typing import Optional

from common.errors import NotFoundError
from common.factory.factory import Factory
from common.log import get_logger
from common.type_def import MemoryUnit, Scope, memory_key
from storage.kv import KVStore

from .base import ConstructionOperator

logger = get_logger(__name__)


def dedup_text(unit: MemoryUnit) -> str:
    """Return the compact extracted statement used for construction deduplication."""
    statement = str(unit.metadata.get("extracted_statement", "")).strip()
    return statement or unit.content


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
    def _load_unit(self, unit_id: str, scope: Scope) -> Optional[MemoryUnit]:
        """从 KVStore 按 unit_id 读取 MemoryUnit；缺失/损坏返回 None。

        召回命中的是建索引记忆，落 ``/memory/{id}``（见 memory.py），故用 memory_key。
        优先在候选 scope 读；缺失则回退到全 scope 扫描（unit 可能落在别的 scope）。
        """
        try:
            return _loads(self._kv.get(scope, memory_key(unit_id)))
        except NotFoundError:
            for s in self._kv.scopes():
                try:
                    unit = _loads(self._kv.get(s, memory_key(unit_id)))
                    if unit is not None:
                        return unit
                except NotFoundError:
                    continue
            return None
        except Exception:
            logger.warning("Dedup._load_unit: failed to load unit %s", unit_id)
            return None


def _loads(raw: bytes) -> Optional[MemoryUnit]:
    from common.type_def.memory_codec import loads

    return loads(raw)


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
