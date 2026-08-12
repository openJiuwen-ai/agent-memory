"""最小实现：:class:`~construction.extractor.Extractor`。

从原始记忆单元提取**低抽象粒度**的派生单元：用注入的 Chunker 把每条「原始」单元
（``provenance`` 为空且 active）的内容切成 chunk，每个 chunk 提升为一条 SEMANTIC
「事实」派生单元，``provenance`` 回指来源、打 ``extracted`` 标签。真实实现会用 LLM
把内容拆成离散事实，这里用切分作可复现占位（保持派生 + 血缘 + 低抽象的结构）。

只处理原始 active 单元，避免对派生单元 / 被取代单元反复再抽取。
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import List

from jiuwen_memory.common.chunker.base import Chunker, ChunkerProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryTier, MemoryUnit, Segment, Temporal
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer

logger = get_logger(__name__)


class KeywordExtractor(Extractor):
    """按 chunk 把原始单元提升为带血缘的 SEMANTIC 事实派生单元。"""

    def __init__(self, chunker: Chunker) -> None:
        self._chunker = chunker

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(
        self,
        units: List[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> List[MemoryUnit]:
        # context 对本实现 noop：无 LLM prompt 可拼，指代消解/去重靠下游 Evolver
        # （_dedup_batch 兜底）。接受参数仅为统一 Extractor 签名。
        # procedural 模式：本实现无 LLM 做结构化汇总，降级为把本轮原文原样合成 1 条
        # PROCEDURAL（provenance 回指全部本轮 unit），仍保证「1 条过程记忆」契约。
        if any(str(u.metadata.get("procedural", "")).strip().lower() == "true" for u in units):
            return self._build_procedural(units)
        logger.info("KeywordExtractor: received %d units", len(units))
        derived: List[MemoryUnit] = []
        for unit in units:
            if unit.provenance or unit.lifecycle != LifecycleState.ACTIVE:
                logger.debug(
                    "KeywordExtractor: skipping unit id=%s (provenance=%s, lifecycle=%s)",
                    unit.id[:8],
                    unit.provenance,
                    unit.lifecycle.value,
                )
                continue  # 跳过派生 / 非 active 单元
            logger.info(
                "KeywordExtractor: extracting from unit id=%s tier=%s content=%s",
                unit.id[:8],
                unit.tier.value,
                unit.content[:200],
            )
            for chunk in self._chunker.chunk(unit.content, unit.id):
                d = deepcopy(unit)
                d.id = str(uuid.uuid4())
                d.tier = MemoryTier.SEMANTIC
                d.segments = [
                    Segment(content=chunk.text, assets=list(unit.assets), source=unit.source)
                ]
                d.provenance = [unit.id]
                d.supersedes = ""
                if "extracted" not in d.tags:
                    d.tags.append("extracted")
                derived.append(d)
                logger.info(
                    "KeywordExtractor: produced derived unit id=%s tier=%s "
                    "provenance=%s content=%s",
                    d.id[:8],
                    d.tier.value,
                    d.provenance,
                    d.content[:200],
                )
        logger.info(
            "KeywordExtractor: produced %d derived units from %d originals",
            len(derived),
            len(units),
        )
        return derived

    def _build_procedural(self, units: List[MemoryUnit]) -> List[MemoryUnit]:
        """procedural 降级：无 LLM，把本轮原文原样合成 1 条 PROCEDURAL。"""
        from datetime import datetime, timezone

        source = units[0]
        content = "\n".join(u.content for u in units if u.content).strip()
        if not content:
            return []
        now = datetime.now(timezone.utc)
        unit = MemoryUnit(
            id=str(uuid.uuid4()),
            scope=source.scope,
            tier=MemoryTier.PROCEDURAL,
            segments=[Segment(content=content, source=source.source)],
            # 不设 source_ref：procedural 原文不落 KV，source.id 指向不存在的记录，
            # 设了反而误导溯源。provenance 仍记本轮 unit id（血缘列表，可指向未落盘源）。
            temporal=Temporal(
                t_event=None,
                t_ingest=now,
                t_valid=now,
                t_message=source.temporal.t_message,
            ),
            provenance=[u.id for u in units],
            # 合并 write tags（engine 已写到源 unit）+ 系统标记 procedural
            tags=merge_unit_tags(source.tags, ["procedural"]),
            metadata={"procedural": "true"},
            lifecycle=LifecycleState.ACTIVE,
        )
        logger.info(
            "KeywordExtractor._build_procedural: built 1 PROCEDURAL unit id=%s content=%s",
            unit.id[:8], unit.content[:200],
        )
        return [unit]


# -- 注册到 ExtractorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@ExtractorProducer.register("keyword")
def _build(config):
    return KeywordExtractor(ChunkerProducer.dep(config, default="fixed_window"))
