# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""影子索引去重召回：shadow.search_vector → shadow.get_units → embedder 精排 cosine。

文档模式（``globals.write_document=true``）下作为 VectorDedup/KeywordDedup 的统一替代：
真源是 ``md`` 文件 + ``shadow`` 影子索引（sqlite3 + sqlite-vec 复合），召回不再走
独立的 vector_store（文档模式写入不碰它，恒空 → VectorDedup recall 恒空），而是查
影子索引自身的 ``search_vector`` / ``search_fulltext``——与 ShadowRecaller 同源。

**分数量纲对齐**：Evolver 阈值用 cosine 量纲（``dedup_medium_similarity=0.7`` /
``dedup_high_similarity=0.9``，越大越相关）。shadow 两路返回的分数与此不可比——
``search_vector`` 返回 ``-distance``（vec0 L2 距离，越大越相关但非 0~1），
``search_fulltext`` 返回 BM25（负值，越小越相关）。本实现用 shadow 召回候选 id 后，
用 ``embedder`` 重新算 ``cosine(candidate, hit)`` 作为分数，与 Evolver 阈值完全对齐。

**两路并跑取并集**（与 :class:`~storage.domain_store_impl.shadow_recaller.ShadowRecaller`
同设计）：fulltext 主路 + vector 补充路并行，按 ``unit_id`` 取并集。去重的失败模式是
**漏召回**（如"我喜欢周杰伦"与"我不喜欢周杰伦"只差一个"不"，向量近邻可能把负例排到
top_k 外，但 fulltext BM25 对近乎逐字重叠会强命中；反之纯语义改写 fulltext 漏时向量
补上）。shadow 两路原始分数量纲不可比（``-distance`` vs BM25 负值），但本实现不消费
它们——只用两路产出候选 id 并集，分数统一由 embedder 重算 cosine，与 Evolver 阈值对齐。

**向量降级**：``shadow.vec_enabled=False``（无 embedder 凭证 / sqlite_vec 不可导入）
时，``search_vector`` 返空，退化为 fulltext 单路。embedder 仍可用（hashing 兜底）→
cosine 精排照常，只是召回 recall@k 下降，不致全空。
"""

from __future__ import annotations

import math

from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    LifecycleState,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.filter import and_merge
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.dedup import Dedup, DedupProducer, same_scope
from jiuwen_memory.storage.shadow import DocumentShadowIndex
from jiuwen_memory.storage.store_manager import StoreManager, StoreManagerProducer
from jiuwen_memory.storage.types import FilterExpr, TextQuery, VectorQuery

logger = get_logger(__name__)


def _active_lifecycle_filter() -> FilterExpr | None:
    """lifecycle='active' 谓词，下推进 shadow 召回 SQL WHERE。

    与检索侧 ``ShadowRecaller`` 经 ``build_system_filters(as_of=None)`` 产出的
    lifecycle 谓词同口径（``lifecycle IN ['active']``）。dedup 只需当前态 ACTIVE
    记忆参与对照——SUPERSEDED/FORGOTTEN 旧版本语义上已失效，让它进 top_k 会挤占
    ACTIVE 名额导致漏召回（Step C 的 Python 层 ``lifecycle != ACTIVE`` 过滤是兜底，
    但名额已被占）。谓词下推到 SQL 层让 SUPERSEDED 在倒排/向量召回阶段即被排除。

    不复用 ``build_system_filters``：它在 ``retrieval`` 包，``construction`` 导入
    ``retrieval`` 是反向跨层（无先例）；这里只需单条 lifecycle 谓词，直接构造等价
    FilterExpr，零跨层依赖。
    """
    return and_merge(
        None,
        [FilterClause("lifecycle", FilterOp.IN, [LifecycleState.ACTIVE.value])],
    )


def _cosine(a: list[float], b: list[float]) -> float:
    """两向量的 cosine 相似度（自行归一化，不依赖 embedder 是否归一化）。

    返回 0~1 量纲（越大越相关），与 Evolver 阈值对齐。维度不一致或零向量返回 0.0。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class ShadowDedup(Dedup):
    """影子索引去重召回路（包一个 shadow + embedder）。

    文档模式下替代 VectorDedup：shadow.search_vector 召回 top-k 候选 id →
    shadow.get_units 加载完整 unit → embedder 精排 cosine → 聚合过滤。
    """

    def __init__(
        self,
        storage: StoreManager,
        embedder: Embedder,
        *,
        shadow_name: str = "default",
        min_similarity: float = 0.5,
        top_k: int = 5,
        tier_filter: bool = False,
        scope_filter: bool = True,
    ) -> None:
        # 基类需要 kv 做 _load_unit，但文档模式 KV 无数据——传 None 绕过基类 kv 用法，
        # 本类重写 _load_unit 改用 shadow.get_units，不依赖 self._kv。
        super().__init__(
            None,  # type: ignore[arg-type]
            min_similarity=min_similarity,
            top_k=top_k,
            tier_filter=tier_filter,
            scope_filter=scope_filter,
        )
        if not storage.has_shadow_index():
            raise RuntimeError(
                "ShadowDedup 要求 shadow 端口就绪（文档模式 globals.write_document=true），"
                "但注入的 StoreManager 无 shadow 端口"
            )
        self._shadow: DocumentShadowIndex = storage.shadow_index(shadow_name)
        self._embedder = embedder

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def recall(self, candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]:
        # Step A: 向量化候选（精排用）
        try:
            candidate_vector = self._embedder.embed([candidate.content])[0]
        except Exception as exc:
            logger.warning(
                "ShadowDedup: Embedder failed for %s, recall empty: %s",
                candidate.id[:8], exc,
            )
            return []

        scope = candidate.scope

        # lifecycle='active' 谓词下推进 SQL WHERE：SUPERSEDED/FORGOTTEN 旧版本在
        # 倒排/向量召回阶段即排除，不占 top_k 名额（否则 Step C 的 Python 层过滤虽
        # 能剔掉，名额已被占 → 漏召回真正的 ACTIVE 冲突记忆）。
        lifecycle_filter = _active_lifecycle_filter()

        # Step B: 两路并跑取并集（与 ShadowRecaller 同设计）。
        # fulltext 必跑（FTS5 BM25，捕捉近乎逐字重叠如"喜欢/不喜欢"只差一字）；
        # vector 在 vec_enabled 时补跑（捕捉纯语义改写）。两路按 unit_id 取并集，
        # 分数不在召回阶段算——统一交 Step C 的 embedder cosine 精排。
        ft_ids: list = []
        vec_ids: list = []
        try:
            ft_ids = self._shadow.search_fulltext(
                scope,
                TextQuery(
                    text=candidate.content,
                    top_k=self._top_k,
                    filters=lifecycle_filter,
                ),
            )
        except Exception as exc:
            logger.warning(
                "ShadowDedup: search_fulltext failed for %s: %s",
                candidate.id[:8], exc,
            )
        if self._shadow.vec_enabled:
            try:
                vec_ids = self._shadow.search_vector(
                    scope,
                    VectorQuery(
                        vector=candidate_vector,
                        top_k=self._top_k,
                        filters=lifecycle_filter,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "ShadowDedup: search_vector failed for %s: %s",
                    candidate.id[:8], exc,
                )

        logger.info(
            "[trace/dedup] shadow_recall | candidate_id=%s | vec_enabled=%s | ft_hits=%d | vec_hits=%d",
            candidate.id[:8], self._shadow.vec_enabled, len(ft_ids), len(vec_ids),
        )

        # 合并并集，去重（同一 unit 两路都命中只算一次）+ 排除候选自身。
        # shadow 命中 id 直接是 unit_id（非 chunk 复合 id），无需解析。
        seen: set[str] = set()
        unit_ids: list[str] = []
        for sid in [*ft_ids, *vec_ids]:
            if sid.id == candidate.id or sid.id in seen:
                continue
            seen.add(sid.id)
            unit_ids.append(sid.id)
        if not unit_ids:
            return []

        # Step C: 批量加载完整 unit → 精排 cosine → 过滤聚合。
        try:
            units = self._shadow.get_units(scope, unit_ids)
        except Exception as exc:
            logger.warning(
                "ShadowDedup: shadow.get_units failed for %s, recall empty: %s",
                candidate.id[:8], exc,
            )
            return []

        # get_units 缺失 id 省略，返回列表可能短于输入；按 unit_id 建索引。
        unit_map = {u.id: u for u in units if u is not None}

        # 精排：批量 embed 所有 hit content，逐对算 cosine（与 Evolver 阈值同量纲）。
        hit_ids = [uid for uid in unit_ids if uid in unit_map]
        if not hit_ids:
            return []
        try:
            hit_vectors = self._embedder.embed([unit_map[uid].content for uid in hit_ids])
        except Exception as exc:
            logger.warning(
                "ShadowDedup: embedder failed for hits, recall empty: %s", exc,
            )
            return []

        # embedder 返回向量数可能少于输入数（batched API 偶发静默丢条），strict=True
        # 会抛 ValueError 且发生在上面的 try 之外——冒泡到调用方会让整批候选跳过
        # 去重判 ADD。改 zip 截断到较短方：下游 _cosine 已做维度检查（len 不等返 0.0），
        # 少算几条分数只会降一点 recall，不会出错或阻断去重。
        aggregated: dict[str, tuple[MemoryUnit, float]] = {}
        for uid, hvec in zip(hit_ids, hit_vectors):
            unit = unit_map[uid]
            score = _cosine(candidate_vector, hvec)
            if score < self._min_similarity:
                continue
            # lifecycle 复核（纵深防御）：SQL 召回层已下推 lifecycle='active' 谓词，
            # 这里对 get_units 还原的 unit 再判一次——防谓词下推路径回归或真源
            # lifecycle 与索引投影列漂移时 SUPERSEDED 漏网。
            if unit.lifecycle != LifecycleState.ACTIVE:
                continue
            if self._tier_filter and unit.tier != candidate.tier:
                continue
            if self._scope_filter and not same_scope(unit.scope, candidate.scope):
                continue
            if unit.id == candidate.id:
                continue
            # 跳过中期记忆原文——派生必然与源原文语义接近，让原文参与对照会触发
            # LLM dedup 判 NOOP 丢派生（与 VectorDedup 同口径）。
            if unit.system_metadata.get("middle") == "true":
                continue
            if uid not in aggregated or score > aggregated[uid][1]:
                aggregated[uid] = (unit, score)

        hit_units = sorted(aggregated.values(), key=lambda x: x[1], reverse=True)
        return hit_units

    # 文档模式 KV 无 MemoryUnit，重写为 shadow.get_units 加载
    def _load_unit(self, unit_id: str, scope: Scope) -> MemoryUnit | None:
        try:
            units = self._shadow.get_units(scope, [unit_id])
            return units[0] if units else None
        except Exception:
            logger.warning("ShadowDedup._load_unit: failed to load unit %s", unit_id)
            return None


# -- 注册到 DedupProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@DedupProducer.register("shadow")
def _build(config):
    # embedder 解析与影子索引同源：dedup 配置 params.embedder="default"（与
    # shadow_index 配置 params.embedder="default" 指向同一具名实例）时，dep 走
    # build_named 拿到与影子索引写入时完全相同的 embedder 实例——保证 search_vector
    # 的查询向量与 memory_vec 表里的向量同空间、同维度。未配 embedder 时（降级模式）
    # 回退 hashing 兜底：此时 shadow.vec_enabled=False，search_vector 返空不走向量路，
    # cosine 精排退化为 hashing 空间内自洽（仅 fulltext 召回出的候选间比较）。
    if "embedder" in config.params:
        embedder = EmbedderProducer.dep(config)
    else:
        embedder = EmbedderProducer.dep(config, default="hashing")
    return ShadowDedup(
        storage=StoreManagerProducer.resolve(config),
        embedder=embedder,
        shadow_name="default",
        min_similarity=config.get("dedup_min_similarity", 0.5),
        top_k=config.get("dedup_top_k", 5),
        tier_filter=config.get("dedup_tier_filter", False),
        scope_filter=config.get("dedup_scope_filter", True),
    )
