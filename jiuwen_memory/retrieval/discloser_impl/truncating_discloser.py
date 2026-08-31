# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~retrieval.discloser.Discloser`——渐进式披露（纯内容塑形）。

三层级内容：L0 摘要 / L1 片段 / L2 全文。**优先用预生成的** ``unit.layers.l0``/``.l1``
（由 LayerAnnotator 生成），为空则回退到截断/取窗兜底（向后兼容未跑 LayerAnnotator 的场景）。
``disclose`` 一次性填充 RetrievedItem 的 abstract/overview/content 三字段，调用方按需取用；
``level`` 标记本次披露的主层级（ADAPTIVE 按 max_tokens 选定）。
"""

from __future__ import annotations

from jiuwen_memory.common.type_def import MemoryUnit, ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.discloser import Discloser, DiscloserProducer
from jiuwen_memory.retrieval.types import DisclosureLevel, ParsedQuery, RetrievedItem

_LIMIT = {DisclosureLevel.L0: 80, DisclosureLevel.L1: 240}


class TruncatingDiscloser(Discloser):
    """L0 摘要 / L1 片段 / L2 全文，优先预生成、兜底截断。无状态，无后端依赖。"""

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.DISCLOSER

    def health(self) -> None:
        return None

    def disclose(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        units: dict[str, MemoryUnit],
        level: DisclosureLevel,
        max_tokens: int | None = None,
    ) -> list[RetrievedItem]:
        items: list[RetrievedItem] = []
        effective_level = DisclosureLevel.L0 if level == DisclosureLevel.ADAPTIVE else level
        for su in candidates:
            unit = units.get(su.unit_id)
            if unit is None:
                continue  # 编排者已过滤，缺失视为不一致，跳过
            items.append(
                RetrievedItem(
                    unit_id=unit.id,
                    score=su.score,
                    abstract=self._l0(unit),
                    overview=self._l1(unit, query.keywords),
                    content=unit.content,  # L2 全文
                    user_metadata=dict(unit.user_metadata),
                    level=effective_level,
                    system_metadata=dict(unit.system_metadata),
                )
            )
        return items

    def _l0(self, unit: MemoryUnit) -> str:
        # 优先预生成 l0；空则截断 content 兜底
        if unit.layers.l0:
            return unit.layers.l0
        content = unit.content
        limit = _LIMIT[DisclosureLevel.L0]
        return content if len(content) <= limit else content[:limit].rstrip() + "…"

    def _l1(self, unit: MemoryUnit, keywords: list[str]) -> str:
        # 优先预生成 l1；空则围绕关键词取窗兜底
        if unit.layers.l1:
            return unit.layers.l1
        content = unit.content
        for kw in keywords:  # 围绕首个命中关键词取窗口
            idx = content.find(kw)
            if idx >= 0:
                start = max(0, idx - 30)
                end = start + _LIMIT[DisclosureLevel.L1]
                window = content[start:end]
                return ("…" if start else "") + window
        limit = _LIMIT[DisclosureLevel.L1]
        return content if len(content) <= limit else content[:limit].rstrip() + "…"


# -- 注册到 DiscloserProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@DiscloserProducer.register("truncating")
def _build(config):
    return TruncatingDiscloser()  # 无状态：点读/过滤/重排已上移到 Retriever 阶段
