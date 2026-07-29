"""Structured discloser — 面向 Agent 消费的结构化渐进披露。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from common.type_def import MemoryUnit
from retrieval.base import RetrievalOperatorType
from retrieval.discloser import Discloser, DiscloserProducer
from retrieval.types import DisclosureLevel, ParsedQuery, RetrievedItem, ScoredUnit

_L0_LIMIT = 120
_L1_LIMIT = 260
_L1_CONTEXT = 60
_L2_CONFIDENCE_MARGIN = 1.5


@dataclass
class _DisclosureVariant:
    scored: ScoredUnit
    unit: MemoryUnit
    content_by_level: dict[DisclosureLevel, str]
    actual_level_by_level: dict[DisclosureLevel, DisclosureLevel]


class StructuredDiscloser(Discloser):
    """L0 记忆卡片 / L1 证据片段 / L2 全文。

    三层级一次性填充 ``RetrievedItem`` 的 ``abstract``(L0) / ``overview``(L1) / ``content``(L2)，
    ``level`` 标本次披露主层级。内容采用稳定的行格式，便于 Agent 读取。
    优先用预生成 ``unit.layers.l0``/``.l1``，空则回退到规则渲染（卡片/证据片段）。
    """

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.DISCLOSER

    def health(self) -> None:
        return None

    def disclose(
        self,
        query: ParsedQuery,
        candidates: List[ScoredUnit],
        units: Dict[str, MemoryUnit],
        level: DisclosureLevel,
        max_tokens: int | None = None,
    ) -> List[RetrievedItem]:
        if level == DisclosureLevel.ADAPTIVE:
            return self._adaptive_disclose(query, candidates, units, max_tokens)

        items: List[RetrievedItem] = []
        keywords = self._keywords(query)
        for su in candidates:
            unit = units.get(su.unit_id)
            if unit is None:
                continue
            abstract, _ = self._render(su, unit, DisclosureLevel.L0, keywords)
            overview, _ = self._render(su, unit, DisclosureLevel.L1, keywords)
            full, _ = self._render(su, unit, DisclosureLevel.L2, keywords)
            items.append(
                RetrievedItem(
                    unit_id=unit.id,
                    score=su.score,
                    abstract=abstract,
                    overview=overview,
                    content=full,
                    level=level,
                )
            )
        return items

    def _adaptive_disclose(
        self,
        query: ParsedQuery,
        candidates: List[ScoredUnit],
        units: Dict[str, MemoryUnit],
        max_tokens: int | None,
    ) -> List[RetrievedItem]:
        keywords = self._keywords(query)
        variants = []
        for scored_unit in candidates:
            unit = units.get(scored_unit.unit_id)
            if unit is not None:
                variants.append(self._variants(scored_unit, unit, keywords))
        selected_levels = [DisclosureLevel.L0 for _ in variants]
        if not variants:
            return []

        if max_tokens is None:
            self._try_upgrade(selected_levels, variants, 0, DisclosureLevel.L1, None)
        else:
            budget = max(0, max_tokens)
            self._try_upgrade(selected_levels, variants, 0, DisclosureLevel.L1, budget)
            if self._can_upgrade_top_to_l2(variants):
                self._try_upgrade(selected_levels, variants, 0, DisclosureLevel.L2, budget)
            for idx in range(1, len(variants)):
                self._try_upgrade(selected_levels, variants, idx, DisclosureLevel.L1, budget)

        items: List[RetrievedItem] = []
        for idx, variant in enumerate(variants):
            requested_level = selected_levels[idx]
            actual_level = variant.actual_level_by_level[requested_level]
            # 三层一次性填充：abstract=L0、overview=L1、content=L2（全文）
            abstract = variant.content_by_level[DisclosureLevel.L0]
            overview = variant.content_by_level[DisclosureLevel.L1]
            full = variant.content_by_level[DisclosureLevel.L2]
            items.append(
                RetrievedItem(
                    unit_id=variant.unit.id,
                    score=variant.scored.score,
                    abstract=abstract,
                    overview=overview,
                    content=full,
                    level=actual_level,
                )
            )
        return items

    def _variants(
        self, scored: ScoredUnit, unit: MemoryUnit, keywords: List[str]
    ) -> _DisclosureVariant:
        content_by_level: dict[DisclosureLevel, str] = {}
        actual_level_by_level: dict[DisclosureLevel, DisclosureLevel] = {}
        for level in (DisclosureLevel.L0, DisclosureLevel.L1, DisclosureLevel.L2):
            content, actual_level = self._render(scored, unit, level, keywords)
            content_by_level[level] = content
            actual_level_by_level[level] = actual_level
        return _DisclosureVariant(scored, unit, content_by_level, actual_level_by_level)

    def _try_upgrade(
        self,
        selected_levels: List[DisclosureLevel],
        variants: List[_DisclosureVariant],
        idx: int,
        target_level: DisclosureLevel,
        budget: int | None,
    ) -> None:
        if idx >= len(variants):
            return
        if variants[idx].actual_level_by_level[target_level] != target_level:
            return
        proposed = list(selected_levels)
        proposed[idx] = target_level
        if budget is None or self._total_tokens(variants, proposed) <= budget:
            selected_levels[idx] = target_level

    def _can_upgrade_top_to_l2(self, variants: List[_DisclosureVariant]) -> bool:
        if not variants:
            return False
        if len(variants) == 1:
            return True
        top_score = variants[0].scored.score
        next_score = variants[1].scored.score
        if next_score <= 0:
            return top_score > 0
        return top_score >= next_score * _L2_CONFIDENCE_MARGIN

    def _total_tokens(
        self,
        variants: List[_DisclosureVariant],
        levels: List[DisclosureLevel],
    ) -> int:
        return sum(
            self._estimate_tokens(variant.content_by_level[level])
            for variant, level in zip(variants, levels)
        )

    def _render(
        self,
        scored: ScoredUnit,
        unit: MemoryUnit,
        level: DisclosureLevel,
        keywords: List[str],
    ) -> tuple[str, DisclosureLevel]:
        if level == DisclosureLevel.L2:
            return "[full]\n" + unit.content, DisclosureLevel.L2
        if level == DisclosureLevel.L1:
            # 优先预生成 l1；空则回退证据片段渲染
            if unit.layers.l1:
                return f"[overview]\n{unit.layers.l1}", DisclosureLevel.L1
            snippet, matched = self._best_snippet(unit.content, keywords)
            if snippet:
                return (
                    "\n".join(
                        [
                            f"[summary] {self._summary(unit)}",
                            f"[evidence] {snippet}",
                            f"[matched] {', '.join(matched) if matched else '-'}",
                            f"[why] {self._why(scored)}",
                        ]
                    ),
                    DisclosureLevel.L1,
                )
        # L0：优先预生成 l0；空则回退卡片渲染
        if unit.layers.l0:
            return f"[abstract]\n{unit.layers.l0}", DisclosureLevel.L0
        return self._l0_card(scored, unit), DisclosureLevel.L0

    def _l0_card(self, scored: ScoredUnit, unit: MemoryUnit) -> str:
        return "\n".join(
            [
                f"[summary] {self._summary(unit)}",
                f"[why] {self._why(scored)}",
                f"[scope] {self._scope(unit)}",
                f"[tags] {', '.join(unit.tags) if unit.tags else '-'}",
                f"[lifecycle] {unit.lifecycle.value}",
            ]
        )

    def _summary(self, unit: MemoryUnit) -> str:
        # metadata 值为 JSON 标量原生类型，summary 若被写成非字符串仍需可披露。
        explicit = str(unit.metadata.get("summary") or "").strip()
        if explicit:
            return explicit
        content = " ".join(unit.content.split())
        if not content:
            return ""
        sentence_end = min(
            [idx for idx in (content.find("."), content.find("。")) if idx >= 0] or [len(content)]
        )
        summary = content[: sentence_end + 1] if sentence_end < len(content) else content
        return self._truncate(summary, _L0_LIMIT)

    def _best_snippet(self, content: str, keywords: List[str]) -> tuple[str, List[str]]:
        lowered = content.lower()
        candidates: List[tuple[int, int]] = []
        for keyword in keywords:
            idx = lowered.find(keyword.lower())
            if idx >= 0:
                candidates.append((idx, max(0, idx - _L1_CONTEXT)))
        if not candidates:
            return "", []

        best_idx = len(content)
        best_start = 0
        best_score = -1
        best_matched: List[str] = []
        for idx, start in candidates:
            end = min(len(content), start + _L1_LIMIT)
            window = lowered[start:end]
            matched = [keyword for keyword in keywords if keyword.lower() in window]
            score = len(set(matched))
            if score > best_score or (score == best_score and idx < best_idx):
                best_idx = idx
                best_start = start
                best_score = score
                best_matched = matched

        end = min(len(content), best_start + _L1_LIMIT)
        snippet = content[best_start:end].strip()
        if best_start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet.rstrip() + "..."
        return snippet, list(dict.fromkeys(best_matched))

    def _keywords(self, query: ParsedQuery) -> List[str]:
        keywords: List[str] = []
        seen: set[str] = set()
        for token in list(query.keywords) + list(query.tokens):
            normalized = token.strip()
            key = normalized.lower()
            if normalized and key not in seen:
                keywords.append(normalized)
                seen.add(key)
        return keywords

    def _why(self, scored: ScoredUnit) -> str:
        if not scored.evidence:
            return f"score={scored.score:.4g}"
        parts = []
        for evidence in scored.evidence:
            parts.append(
                f"{evidence.channel.value}"
                f"(rank={evidence.rank + 1},score={evidence.score:.4g},"
                f"weight={evidence.weight:g},contribution={evidence.contribution:.4g})"
            )
        return "; ".join(parts)

    def _scope(self, unit: MemoryUnit) -> str:
        scope = unit.scope
        return (
            f"org={scope.org or '-'} "
            f"user={scope.user or '-'} "
            f"agent={scope.agent or '-'} "
            f"session={scope.session or '-'}"
        )

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit].rstrip() + "..."

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)


# -- 注册到 DiscloserProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@DiscloserProducer.register("structured")
def _build(config):
    return StructuredDiscloser()
