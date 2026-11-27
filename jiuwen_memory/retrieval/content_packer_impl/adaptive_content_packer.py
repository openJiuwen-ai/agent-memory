# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""默认 ContentPacker：Mixed-Score 价值重排 + 按规模三级无模型自适应压缩（retrieval F15）。

对 ``cut_to_top_k`` 产出的 K 条候选（逐条 ``ScoredMemoryUnit``）：

1. **价值排序（五项 Mixed-Score）**：
   ``w_rel × minmax(score) + w_question × 问题词重合度 + w_recency × 时间新近度``
   ``+ w_entity × 实体重合度 − w_cost × 单条长度占比``
   （默认 ``0.45 / 0.50 / 0.0 / 0.0 / 0.05``，``recency_tau_days=30.0``；
   得分全为 0 时回退输入名次线性代理）；
2. **按实际候选数路由策略**（``strategy="auto"`` 时）：
   - ``≤ role_compact_max_items``（默认 10）：``role_compact``，将角色称呼缩写为 ``U / A``
     并剥离与 ``t_event`` 同日的 ``On YYYY-MM-DD, `` 前缀，仍超预算时尾部低分项转电报式；
   - ``≤ telegraph_max_items``（默认 20）：``telegraph``，前 ``h`` 条高分项保留原文，其余剔除
     实词覆盖率 ≥ ``dup_coverage`` 的重复项后去英文虚词改电报式（保留否定词、专名、数字）；
   - ``> telegraph_max_items``：``drop``，按 Mixed-Score 从高到低贪心保留预算内条目，其余整条剔除；
3. **逐条浅拷贝输出**：保持保留候选原本的相对相关性顺序；未改写条目直接复用原对象，改写条目以
   ``dataclasses.replace`` 浅拷贝替换 ``segments`` 并清空旧 ``layers``（``vectors``/``metadata``
   等重字段零复制共享），保证主链路异常回退（``return final``）时原候选与摘要层完好。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import (
    ContentLayers,
    MemoryUnit,
    Modality,
    ScoredCandidate,
    ScoredMemoryUnit,
    Segment,
)
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.content_packer import (
    ContentPacker,
    ContentPackerProducer,
    PackStrategy,
    to_pack_strategy,
)
from jiuwen_memory.retrieval.types import ParsedQuery

DEFAULT_BUDGET_RATIO = 0.8
DEFAULT_ROLE_COMPACT_MAX_ITEMS = 10
DEFAULT_TELEGRAPH_MAX_ITEMS = 20
DEFAULT_W_REL = 0.45
DEFAULT_W_QUESTION = 0.50
DEFAULT_W_RECENCY = 0.0
DEFAULT_W_ENTITY = 0.0
DEFAULT_W_COST = 0.05
DEFAULT_RECENCY_TAU_DAYS = 30.0
DEFAULT_DUP_COVERAGE = 0.8
_SECONDS_PER_DAY = 86400.0

_BPE_APPROX = re.compile(r"[a-zA-Z0-9]{1,8}|[\u4e00-\u9fff]|[^\s\w]")
_LEXICAL = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")
_TELE_TOKEN = re.compile(r"[\w'’\-]+|[.,;:!?]")
_ROLE = re.compile(r"(?<!\b[Aa] )(?<!\b[Aa]n )\b(?:[Tt]he )?([Uu]ser|[Aa]ssistant)\b")

_TELE_DROP = frozenset(
    "a an the of to in on at for with is was were be been being has have had that this these those "
    "it its and or as by from about into than then so very really also just which who whom whose "
    "what when where would could should will can may might do does did their there they them he "
    "she his her him".split()
)
_TELE_KEEP = frozenset("not no never without".split())
_STOP = frozenset(
    "the a an and or of to in on at for with is was were be been has have had that this it its "
    "user assistant their they them he she his her about from as by who what which".split()
)


@dataclass(frozen=True)
class _Entry:
    index: int
    event_date: str
    text: str
    rewritten: bool = False
    telegraphed: bool = False


def estimate_bpe_tokens(text: str) -> int:
    """零外部依赖的 BPE 近似分词计数（英文词块 / 汉字 / 标点）。"""
    if not text:
        return 0
    return max(1, len(_BPE_APPROX.findall(text)))


def _validate_ratio(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a float in (0, 1], got {value!r}")
    ratio = float(value)
    if math.isnan(ratio) or math.isinf(ratio) or not 0.0 < ratio <= 1.0:
        raise ValidationError(f"{name} must be in (0, 1], got {value!r}")
    return ratio


def _validate_non_negative(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a non-negative number, got {value!r}")
    num = float(value)
    if math.isnan(num) or math.isinf(num) or num < 0.0:
        raise ValidationError(f"{name} must be >= 0, got {value!r}")
    return num


def _validate_positive(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a positive number, got {value!r}")
    num = float(value)
    if math.isnan(num) or math.isinf(num) or num <= 0.0:
        raise ValidationError(f"{name} must be > 0, got {value!r}")
    return num


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _lexical_tokens(text: str) -> list[str]:
    return [tok.lower() for tok in _LEXICAL.findall(text or "")]


def _unit_timestamp(unit: MemoryUnit) -> datetime | None:
    temporal = getattr(unit, "temporal", None)
    if temporal is None:
        return None
    for attr in ("t_ingest", "t_valid", "t_message", "t_event"):
        ts = getattr(temporal, attr, None)
        if ts is not None:
            return ts
    return None


def _recency_score(unit: MemoryUnit, now: datetime, tau_days: float) -> float:
    """软衰减时效性 ``1 / (1 + age_days / tau_days)``；缺失时间戳取中性值 0.5。"""
    ts = _unit_timestamp(unit)
    if ts is None:
        return 0.5
    age_days = max(0.0, (as_utc(now) - as_utc(ts)).total_seconds() / _SECONDS_PER_DAY)
    return 1.0 / (1.0 + age_days / tau_days)


def _entity_overlap(query_tokens: set[str], entities: Sequence[object]) -> float:
    """记忆实体词集命中查询词的比例；无实体或无查询词时为 0.0。"""
    if not entities or not query_tokens:
        return 0.0
    hits = 0
    for ent in entities:
        raw = ent.text if hasattr(ent, "text") else str(ent)
        ent_tokens = set(_lexical_tokens(raw))
        if ent_tokens and (ent_tokens & query_tokens):
            hits += 1
    return hits / len(entities)


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _role_letter(match: re.Match[str]) -> str:
    return "U" if match.group(1).lower() == "user" else "A"


def _event_date_str(unit: MemoryUnit) -> str:
    temporal = getattr(unit, "temporal", None)
    t_event = getattr(temporal, "t_event", None) if temporal is not None else None
    if t_event is None:
        return ""
    return t_event.strftime("%Y-%m-%d")


def _strip_date_prefix(text: str, event_date: str) -> str:
    """剥离与 ``t_event`` 同日的 ``On YYYY-MM-DD, `` 冗余前缀。"""
    if not event_date:
        return text
    prefix = f"On {event_date}, "
    size = len(prefix)
    return text[size:] if text.startswith(prefix) else text


def _telegraph_entry(entry: _Entry) -> _Entry:
    """去同日日期前缀与英文虚词，保留否定词、首字母大写词与含数字词。"""
    stripped = _strip_date_prefix(entry.text, entry.event_date)
    out: list[str] = []
    for tok in _TELE_TOKEN.findall(stripped):
        low = tok.lower()
        if low in _TELE_KEEP or tok[0].isupper() or any(ch.isdigit() for ch in tok):
            out.append(tok)
        elif tok in ".,;:!?":
            if out and out[-1] not in ",;":
                out.append(",")
        elif low not in _TELE_DROP:
            out.append(tok)
    compressed = " ".join(out).replace(" ,", ",").strip(" ,")
    final_text = compressed if compressed else stripped
    return _Entry(
        index=entry.index,
        event_date=entry.event_date,
        text=final_text,
        rewritten=(final_text != entry.text or entry.rewritten),
        telegraphed=True,
    )


def _replace_unit_content(unit: MemoryUnit, text: str) -> MemoryUnit:
    """用 ``dataclasses.replace`` 浅拷贝替换正文并清空 ``layers``，保证降级回退时原对象完好。"""
    if unit.segments:
        first = replace(unit.segments[0], content=text, assets=list(unit.assets))
        segments = [first]
    else:
        segments = [Segment(content=text, source=Modality.TEXT)]
    return replace(unit, segments=segments, layers=ContentLayers())


def _entries_for_strategy(
    strategy: PackStrategy,
    raw_texts: list[str],
    event_dates: list[str],
) -> list[_Entry]:
    """按策略构造待拟合条目；DROP 与 TELEGRAPH 用原文，ROLE_COMPACT 先做轻量缩写。"""
    if strategy is PackStrategy.ROLE_COMPACT:
        entries: list[_Entry] = []
        for i, text in enumerate(raw_texts):
            shortened = _strip_date_prefix(
                _ROLE.sub(_role_letter, text), event_dates[i]
            )
            entries.append(
                _Entry(
                    index=i,
                    event_date=event_dates[i],
                    text=shortened,
                    rewritten=(shortened != text),
                )
            )
        return entries
    return [_Entry(i, event_dates[i], raw_texts[i]) for i in range(len(raw_texts))]


class AdaptiveContentPacker(ContentPacker):
    """Top-K 三档无模型自适应内容打包实现。"""

    def __init__(
        self,
        *,
        budget_ratio: float = DEFAULT_BUDGET_RATIO,
        strategy: PackStrategy | str = PackStrategy.AUTO,
        role_compact_max_items: int = DEFAULT_ROLE_COMPACT_MAX_ITEMS,
        telegraph_max_items: int = DEFAULT_TELEGRAPH_MAX_ITEMS,
        w_rel: float = DEFAULT_W_REL,
        w_question: float = DEFAULT_W_QUESTION,
        w_recency: float = DEFAULT_W_RECENCY,
        w_entity: float = DEFAULT_W_ENTITY,
        w_cost: float = DEFAULT_W_COST,
        recency_tau_days: float = DEFAULT_RECENCY_TAU_DAYS,
        dup_coverage: float = DEFAULT_DUP_COVERAGE,
        count_tokens: Callable[[str], int] | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._budget_ratio = _validate_ratio("budget_ratio", budget_ratio)
        self._strategy = to_pack_strategy(strategy)
        if isinstance(role_compact_max_items, bool) or int(role_compact_max_items) < 0:
            raise ValidationError(
                f"role_compact_max_items must be >= 0, got {role_compact_max_items!r}"
            )
        if isinstance(telegraph_max_items, bool) or int(telegraph_max_items) < int(
            role_compact_max_items
        ):
            raise ValidationError(
                "telegraph_max_items must be >= role_compact_max_items, "
                f"got {telegraph_max_items!r} < {role_compact_max_items!r}"
            )
        self._role_max = int(role_compact_max_items)
        self._tele_max = int(telegraph_max_items)
        self._w_rel = _validate_non_negative("w_rel", w_rel)
        self._w_question = _validate_non_negative("w_question", w_question)
        self._w_recency = _validate_non_negative("w_recency", w_recency)
        self._w_entity = _validate_non_negative("w_entity", w_entity)
        self._w_cost = _validate_non_negative("w_cost", w_cost)
        self._recency_tau_days = _validate_positive("recency_tau_days", recency_tau_days)
        self._dup_coverage = _validate_ratio("dup_coverage", dup_coverage)
        self._count_tokens = count_tokens or estimate_bpe_tokens
        self._clock = clock

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.CONTENT_PACKER

    def health(self) -> None:
        return None

    def explain(self) -> dict[str, str]:
        return {
            "configured_strategy": self._strategy.value,
            "budget_ratio": f"{self._budget_ratio:g}",
        }

    def resolve_strategy(self, count: int) -> PackStrategy:
        """返回实际生效的压缩策略。"""
        if self._strategy is not PackStrategy.AUTO:
            return self._strategy
        if count <= self._role_max:
            return PackStrategy.ROLE_COMPACT
        if count <= self._tele_max:
            return PackStrategy.TELEGRAPH
        return PackStrategy.DROP

    def mixed_score_order(
        self,
        query_text: str,
        scores: Sequence[float],
        texts: Sequence[str],
        *,
        recencies: Sequence[float] | None = None,
        entity_overlaps: Sequence[float] | None = None,
    ) -> list[int]:
        """计算五项 Mixed-Score 价值降序下标；同分保持输入顺序。"""
        n = len(texts)
        if n == 0:
            return []
        if any(s != 0.0 for s in scores):
            lo, hi = min(scores), max(scores)
            rel = [0.0] * n if hi - lo < 1e-9 else [(s - lo) / (hi - lo) for s in scores]
        else:
            rel = [1.0 - i / max(n - 1, 1) for i in range(n)]
        rec_vals = list(recencies) if recencies is not None else [0.0] * n
        ent_vals = list(entity_overlaps) if entity_overlaps is not None else [0.0] * n
        if len(rec_vals) != n or len(ent_vals) != n:
            raise ValidationError("recencies and entity_overlaps length must match texts")
        query_tokens = set(_lexical_tokens(query_text))
        costs = [max(1, self._count_tokens(text)) for text in texts]
        denom = float(max(sum(costs), 1))
        mixed: list[float] = []
        for idx, (text, r_val, cost) in enumerate(zip(texts, rel, costs)):
            overlap = 0.0
            if query_tokens:
                overlap = len(query_tokens & set(_lexical_tokens(text))) / len(query_tokens)
            mixed.append(
                self._w_rel * r_val
                + self._w_question * overlap
                + self._w_recency * rec_vals[idx]
                + self._w_entity * ent_vals[idx]
                - self._w_cost * cost / denom
            )
        return sorted(range(n), key=lambda i: (-mixed[i], i))

    def pack(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
    ) -> list[ScoredCandidate]:
        packed, _ = self.pack_with_detail(query, candidates)
        return packed

    def pack_with_detail(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        *,
        keep_count: bool = False,
        **_kwargs: object,
    ) -> tuple[list[ScoredCandidate], dict[str, str]]:
        if not candidates:
            chosen = self.resolve_strategy(0)
            return [], {
                "strategy": chosen.value,
                "in": "0",
                "out": "0",
                "full_tokens": "0",
                "tokens": "0",
                "kept": "0",
                "telegraphed": "0",
                "dropped": "0",
                "keep_count": "true" if keep_count else "false",
                "suppressed_drops": "0",
            }

        materialized = [c for c in candidates if isinstance(c, ScoredMemoryUnit)]
        if len(materialized) != len(candidates):
            # 兼容仅含 unit_id 的裸 ScoredUnit 测试桩：无正文可压，原样透传。
            chosen = self.resolve_strategy(len(candidates))
            return list(candidates), {
                "strategy": chosen.value,
                "skipped": "unmaterialized",
                "in": str(len(candidates)),
                "out": str(len(candidates)),
                "keep_count": "true" if keep_count else "false",
                "suppressed_drops": "0",
            }

        n = len(materialized)
        natural = self.resolve_strategy(n)
        raw_texts = [cand.unit.content for cand in materialized]
        scores = [float(cand.score) for cand in materialized]
        event_dates = [_event_date_str(cand.unit) for cand in materialized]

        full_tokens = sum(self._count_tokens(t) for t in raw_texts)
        budget = self._budget_ratio * full_tokens
        query_text = (query.rewritten or query.raw) if query is not None else ""
        recencies: list[float] | None = None
        if self._w_recency > 0.0:
            ref_now = (
                query.as_of
                if (query is not None and query.as_of is not None)
                else self._clock()
            )
            recencies = [
                _recency_score(cand.unit, ref_now, self._recency_tau_days)
                for cand in materialized
            ]
        entity_overlaps: list[float] | None = None
        if self._w_entity > 0.0:
            q_tokens = set(_lexical_tokens(query_text))
            entity_overlaps = [
                _entity_overlap(q_tokens, getattr(cand.unit, "entities", None) or ())
                for cand in materialized
            ]
        order = self.mixed_score_order(
            query_text,
            scores,
            raw_texts,
            recencies=recencies,
            entity_overlaps=entity_overlaps,
        )

        suppressed_drops = 0
        base_entries = _entries_for_strategy(natural, raw_texts, event_dates)
        if keep_count:
            # 业务要求条数不变：沿用自然档位的压缩强度，但禁止去重/预算删条；
            # suppressed_drops = 若按原策略会丢掉的条数（含 telegraph 去重与 drop）。
            natural_selected = self._select_for_strategy(
                natural, base_entries, order, budget
            )
            suppressed_drops = n - len(natural_selected)
            chosen = (
                PackStrategy.ROLE_COMPACT
                if natural is PackStrategy.ROLE_COMPACT
                else PackStrategy.TELEGRAPH
            )
            # DROP 在 keep_count 下改为 telegraph 保条数；ROLE_COMPACT / TELEGRAPH 保头原文。
            selected = self._telegraph_fit(
                base_entries, order, budget, preserve_count=True
            )
        else:
            chosen = natural
            selected = self._select_for_strategy(
                natural, base_entries, order, budget
            )

        by_index = {entry.index: entry for entry in selected}
        output: list[ScoredCandidate] = []
        for idx in range(n):
            entry = by_index.get(idx)
            if entry is None:
                continue
            cand = materialized[idx]
            if entry.rewritten or entry.text != raw_texts[idx]:
                packed_unit = _replace_unit_content(cand.unit, entry.text)
                output.append(replace(cand, unit=packed_unit))
            else:
                output.append(cand)

        packed_tokens = sum(self._count_tokens(entry.text) for entry in selected)
        tele_count = sum(1 for entry in selected if entry.telegraphed)
        kept_count = len(selected) - tele_count
        dropped_count = n - len(selected)
        detail = {
            "strategy": chosen.value,
            "in": str(n),
            "out": str(len(output)),
            "full_tokens": str(full_tokens),
            "tokens": str(packed_tokens),
            "kept": str(kept_count),
            "telegraphed": str(tele_count),
            "dropped": str(dropped_count),
            "keep_count": "true" if keep_count else "false",
            "suppressed_drops": str(suppressed_drops),
        }
        return output, detail

    def _select_for_strategy(
        self,
        strategy: PackStrategy,
        entries: list[_Entry],
        order: list[int],
        budget: float,
    ) -> list[_Entry]:
        """按自然策略选出保留条目（允许删条）；供普通路径与 keep_count 对比基线共用。"""
        if strategy is PackStrategy.DROP:
            return self._greedy_fit(entries, order, budget)
        return self._telegraph_fit(entries, order, budget)

    def _greedy_fit(
        self, entries: list[_Entry], order: list[int], budget: float
    ) -> list[_Entry]:
        picked: list[_Entry] = []
        used_tokens = 0
        for idx in order:
            cost = self._count_tokens(entries[idx].text)
            if used_tokens + cost <= budget:
                picked.append(entries[idx])
                used_tokens += cost
        return picked if picked else [entries[order[0]]]

    def _telegraph_fit(
        self,
        entries: list[_Entry],
        order: list[int],
        budget: float,
        *,
        preserve_count: bool = False,
    ) -> list[_Entry]:
        """取最大的头部条数 ``h`` 保留原样，其余去重并转电报式以满足预算。

        ``preserve_count=True``（请求级 keep_count）时：不做实词去重、不因预算弹尾，
        保证返回条数 = 输入条数；预算仍尽量让高价值头部保留原文。
        """
        costs = [self._count_tokens(e.text) for e in entries]
        word_sets = [_content_words(e.text) for e in entries]
        tail: list[_Entry] = []
        for h in range(len(order), -1, -1):
            head = [entries[i] for i in order[:h]]
            head_tokens = sum(costs[i] for i in order[:h])
            if head_tokens > budget and h > 0:
                continue
            covered: set[str] = set().union(*(word_sets[i] for i in order[:h]))
            rest: list[_Entry] = []
            for i in order[h:]:
                words = word_sets[i]
                if (
                    not preserve_count
                    and words
                    and len(words & covered) / len(words) >= self._dup_coverage
                ):
                    continue
                rest.append(entries[i])
                covered |= words
            tail = [_telegraph_entry(e) for e in rest]
            tail_tokens = sum(self._count_tokens(e.text) for e in tail)
            if head_tokens + tail_tokens <= budget:
                return head + tail

        if preserve_count:
            # 紧预算下连全电报式也装不进 budget 时：仍全部转电报式，保条数优先于预算硬顶。
            return [_telegraph_entry(entries[i]) for i in order]

        total = sum(self._count_tokens(e.text) for e in tail)
        while len(tail) > 1 and total > budget:
            removed = tail.pop()
            total -= self._count_tokens(removed.text)
        return tail if tail else [entries[order[0]]]


# -- 注册到 ContentPackerProducer（实现自注册） ----------------------------------- #


@ContentPackerProducer.register("default")
@ContentPackerProducer.register("adaptive")
def _build(config):
    return AdaptiveContentPacker(
        budget_ratio=float(Factory.cfg_get(config, "budget_ratio", DEFAULT_BUDGET_RATIO)),
        strategy=Factory.cfg_get(config, "strategy", PackStrategy.AUTO.value),
        role_compact_max_items=int(
            Factory.cfg_get(config, "role_compact_max_items", DEFAULT_ROLE_COMPACT_MAX_ITEMS)
        ),
        telegraph_max_items=int(
            Factory.cfg_get(config, "telegraph_max_items", DEFAULT_TELEGRAPH_MAX_ITEMS)
        ),
        w_rel=float(Factory.cfg_get(config, "w_rel", DEFAULT_W_REL)),
        w_question=float(Factory.cfg_get(config, "w_question", DEFAULT_W_QUESTION)),
        w_recency=float(Factory.cfg_get(config, "w_recency", DEFAULT_W_RECENCY)),
        w_entity=float(Factory.cfg_get(config, "w_entity", DEFAULT_W_ENTITY)),
        w_cost=float(Factory.cfg_get(config, "w_cost", DEFAULT_W_COST)),
        recency_tau_days=float(
            Factory.cfg_get(config, "recency_tau_days", DEFAULT_RECENCY_TAU_DAYS)
        ),
        dup_coverage=float(Factory.cfg_get(config, "dup_coverage", DEFAULT_DUP_COVERAGE)),
    )
