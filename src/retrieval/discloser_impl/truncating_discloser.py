"""最小实现：:class:`~retrieval.discloser.Discloser`——渐进式披露（纯内容塑形）。

按层级塑形候选内容：L0 摘要（截断）/ L1 相关片段（围绕 query 关键词的窗口）/
L2 全文。**纯塑形**：候选记忆单元已由 Retriever 经 UnitReader 点读、有效性过滤、
（可选）重排后给定——本算子只按 ``level`` 截/取 ``unit.content``，不再点读 / 过滤
/ 重排（Option B：那些职责上移到 Retriever 阶段）。
"""

from __future__ import annotations

from typing import Dict, List

from common.type_def import MemoryUnit
from retrieval.base import RetrievalOperatorType
from retrieval.discloser import Discloser, DiscloserProducer
from retrieval.types import DisclosureLevel, ParsedQuery, RetrievedItem, ScoredUnit

_LIMIT = {DisclosureLevel.L0: 80, DisclosureLevel.L1: 240}


class TruncatingDiscloser(Discloser):
    """L0 截断摘要 / L1 关键词片段 / L2 全文。无状态，无后端依赖。"""

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.DISCLOSER

    def health(self) -> None:
        return None

    def _content(self, content: str, level: DisclosureLevel, keywords: List[str]) -> str:
        if level == DisclosureLevel.L2:
            return content
        if level == DisclosureLevel.L1:
            for kw in keywords:  # 围绕首个命中关键词取窗口
                idx = content.find(kw)
                if idx >= 0:
                    start = max(0, idx - 30)
                    end = start + _LIMIT[DisclosureLevel.L1]
                    window = content[start:end]
                    return ("…" if start else "") + window
        limit = _LIMIT.get(level, 80)
        return content if len(content) <= limit else content[:limit].rstrip() + "…"

    def disclose(
        self,
        query: ParsedQuery,
        candidates: List[ScoredUnit],
        units: Dict[str, MemoryUnit],
        level: DisclosureLevel,
        max_tokens: int | None = None,
    ) -> List[RetrievedItem]:
        items: List[RetrievedItem] = []
        effective_level = DisclosureLevel.L0 if level == DisclosureLevel.ADAPTIVE else level
        for su in candidates:
            unit = units.get(su.unit_id)
            if unit is None:
                continue  # 编排者已过滤，缺失视为不一致，跳过
            items.append(
                RetrievedItem(
                    unit_id=unit.id,
                    score=su.score,
                    content=self._content(unit.content, effective_level, query.keywords),
                    level=effective_level,
                )
            )
        return items


# -- 注册到 DiscloserProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@DiscloserProducer.register("truncating")
def _build(config):
    return TruncatingDiscloser()  # 无状态：点读/过滤/重排已上移到 Retriever 阶段
