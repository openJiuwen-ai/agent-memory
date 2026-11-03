# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""检索结果的上下文打包：在 token 预算内把 Top-K 记忆渲染成答题 prompt 的记忆块（F09）。

输入是调用方已拿到的 Top-K 记忆（正文、事件日期、检索得分），输出预算内的记忆块文本与
逐条去向。预算 = ``budget_ratio`` × 同一批记忆按官方格式（每行 ``(日期) 正文``）渲染的
token 数。三种策略都不调用模型：

| 策略 | 做法 |
|---|---|
| ``role_compact`` | 角色称呼缩写为 U / A + 同日记忆归入日期标题；仍超预算时尾部改电报式 |
| ``telegraph`` | Mixed-Score 序前 h 条原样，其余去虚词改电报式，h 取装得下的最大值 |
| ``drop`` | Mixed-Score 序贪心装满预算，其余整条丢弃 |

``auto`` 按条数选：≤ 10 条 ``role_compact``，≤ 20 条 ``telegraph``，其余 ``drop``。

**不是检索算子**：与 :mod:`.cross_space` 同属不访问存储、不调模型的纯函数，无 Producer
注册、不进工厂，也不改 ``Retriever`` 的结果——排序、删条与改写只作用于返回的文本。
Discloser 只做内容塑形（AGENTS.md 铁律 10），而本模块会重排与删条，因此不放进 Discloser。
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "PackMemory",
    "PackStrategy",
    "PackedContext",
    "estimate_tokens",
    "mixed_score_order",
    "pack_context",
    "render_memories",
    "resolve_strategy",
]

OFFICIAL_INTRO = "The following memories are presented in chronological order (oldest to newest)."
COMPACT_INTRO = (
    "The following memories are grouped by date, in chronological order (oldest to newest)."
)
ROLE_INTRO = COMPACT_INTRO + " U = the user, A = the assistant."
UNKNOWN_DATE = "unknown date"

# auto 策略的条数分界；验证只覆盖 10 / 20 / 50 三档（F09「验证」）。
ROLE_COMPACT_MAX_ITEMS = 10
TELEGRAPH_MAX_ITEMS = 20

# Mixed-Score 权重：检索得分 / 问题词重合 / 长度惩罚（新近度与实体权重为 0）。
_W_REL = 0.45
_W_QUESTION = 0.50
_W_COST = 0.05

_LEXICAL = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")
_TELE_TOKEN = re.compile(r"[\w'’\-]+|[.,;:!?]")
_ROLE = re.compile(r"(?<!\ba )(?<!\ban )\b(?:[Tt]he )?([Uu]ser|[Aa]ssistant)\b")
_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d")

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
# 尾部条目的内容词有这么大比例已出现在前面保留的条目里时直接丢弃。
_DUP_COVERAGE = 0.8


class PackStrategy(str, Enum):
    """打包策略。"""

    AUTO = "auto"
    ROLE_COMPACT = "role_compact"
    TELEGRAPH = "telegraph"
    DROP = "drop"


@dataclass(frozen=True)
class PackMemory:
    """一条待打包的记忆。``created_at`` 为 ISO 8601 事件时间，空串表示未知日期。"""

    content: str
    created_at: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class PackedContext:
    """打包结果。下标均指向输入序列；``kept`` / ``telegraphed`` / ``dropped`` 互斥且覆盖全部输入。

    ``tokens`` 为 ``text`` 的 token 数，``full_tokens`` 为同一批记忆官方格式渲染的 token 数，
    ``budget`` = ``budget_ratio`` × ``full_tokens``。所有记忆都放不下时仍保留排第一的一条，
    此时 ``tokens`` 可能超过 ``budget``。
    """

    text: str
    strategy: PackStrategy
    kept: tuple[int, ...]
    telegraphed: tuple[int, ...]
    dropped: tuple[int, ...]
    tokens: int
    budget: float
    full_tokens: int


@dataclass(frozen=True)
class _Entry:
    index: int
    created_at: str
    text: str
    telegraph: bool = False


def estimate_tokens(text: str) -> int:
    """与 ``pipeline_retriever`` 同一口径：约 4 字符 1 token。"""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def resolve_strategy(n_memories: int) -> PackStrategy:
    """``auto`` 对应的实际策略。"""
    if n_memories <= ROLE_COMPACT_MAX_ITEMS:
        return PackStrategy.ROLE_COMPACT
    if n_memories <= TELEGRAPH_MAX_ITEMS:
        return PackStrategy.TELEGRAPH
    return PackStrategy.DROP


def render_memories(memories: Sequence[PackMemory]) -> str:
    """官方格式：按事件时间升序，每行 ``(日期) 正文``。即不打包时的记忆块。"""
    return _render_official([_Entry(i, m.created_at, m.content) for i, m in enumerate(memories)])


def mixed_score_order(query: str, memories: Sequence[PackMemory]) -> list[int]:
    """Mixed-Score 降序的下标；同分按输入顺序。

    ``0.45 × 检索得分（min-max）+ 0.50 × 问题词重合度 − 0.05 × 长度占比``。得分全为 0 时
    检索得分改用名次代理（第一名 1.0、末名 0.0）。
    """
    n = len(memories)
    if n == 0:
        return []
    scores = [float(m.score) for m in memories]
    if any(s != 0.0 for s in scores):
        lo, hi = min(scores), max(scores)
        rel = [0.0] * n if hi - lo < 1e-9 else [(s - lo) / (hi - lo) for s in scores]
    else:
        rel = [1.0 - i / max(n - 1, 1) for i in range(n)]
    query_tokens = set(_lexical_tokens(query))
    costs = [estimate_tokens(m.content) for m in memories]
    denom = float(max(sum(costs), 1))
    mixed = []
    for m, r, cost in zip(memories, rel, costs):
        overlap = 0.0
        if query_tokens:
            overlap = len(query_tokens & set(_lexical_tokens(m.content))) / len(query_tokens)
        mixed.append(_W_REL * r + _W_QUESTION * overlap - _W_COST * cost / denom)
    return sorted(range(n), key=lambda i: (-mixed[i], i))


def pack_context(
    query: str,
    memories: Sequence[PackMemory],
    *,
    budget_ratio: float = 0.8,
    strategy: PackStrategy | str = PackStrategy.AUTO,
    count_tokens: Callable[[str], int] | None = None,
) -> PackedContext:
    """在 ``budget_ratio`` × 官方格式 token 的预算内打包 ``memories``。

    ``count_tokens`` 为空时用 :func:`estimate_tokens`（字符估算）；预算与实际用量用同一个
    计数器，评测与生产应传入答题模型的分词器以保证按 token 计的降幅。
    """
    if not 0.0 < budget_ratio <= 1.0:
        raise ValueError(f"budget_ratio must be in (0, 1], got {budget_ratio}")
    count = count_tokens or estimate_tokens
    chosen = PackStrategy(strategy)
    if chosen is PackStrategy.AUTO:
        chosen = resolve_strategy(len(memories))
    if not memories:
        return PackedContext("", chosen, (), (), (), 0, 0.0, 0)

    full_tokens = count(render_memories(memories))
    budget = budget_ratio * full_tokens
    order = mixed_score_order(query, memories)
    if chosen is PackStrategy.DROP:
        entries = [_Entry(i, m.created_at, m.content) for i, m in enumerate(memories)]
        picked = _greedy_fit(entries, order, budget, _render_official, count)
        packed = [entries[i] for i in sorted(picked)]
        render = _render_official
    else:
        if chosen is PackStrategy.ROLE_COMPACT:
            texts = [_ROLE.sub(_role_letter, m.content) for m in memories]
            render = _render_role
        else:
            texts = [m.content for m in memories]
            render = _render_official
        entries = [_Entry(i, m.created_at, t) for i, (m, t) in enumerate(zip(memories, texts))]
        packed = _telegraph_fit(entries, order, budget, render, count)

    text = render(packed)
    kept = tuple(sorted(e.index for e in packed if not e.telegraph))
    telegraphed = tuple(sorted(e.index for e in packed if e.telegraph))
    used = set(kept) | set(telegraphed)
    dropped = tuple(i for i in range(len(memories)) if i not in used)
    return PackedContext(text, chosen, kept, telegraphed, dropped, count(text), budget, full_tokens)


# ---------------------------------------------------------------- 选择


def _greedy_fit(
    entries: list[_Entry],
    order: list[int],
    budget: float,
    render: Callable[[list[_Entry]], str],
    count: Callable[[str], int],
) -> list[int]:
    picked: list[int] = []
    for i in order:
        if count(render([entries[j] for j in [*picked, i]])) <= budget:
            picked.append(i)
    return picked or order[:1]


def _telegraph_fit(
    entries: list[_Entry],
    order: list[int],
    budget: float,
    render: Callable[[list[_Entry]], str],
    count: Callable[[str], int],
) -> list[_Entry]:
    """前 h 条原样、其余电报式（与已保留内容重复的先丢弃），取装得下的最大 h。"""
    tail: list[_Entry] = []
    for h in range(len(order), -1, -1):
        head = [entries[i] for i in order[:h]]
        covered: set[str] = set().union(*(_content_words(e.text) for e in head))
        rest = []
        for i in order[h:]:
            words = _content_words(entries[i].text)
            if words and len(words & covered) / len(words) >= _DUP_COVERAGE:
                continue
            rest.append(entries[i])
            covered |= words
        tail = [_telegraph(e) for e in rest]
        if count(render(head + tail)) <= budget:
            return head + tail
    while len(tail) > 1 and count(render(tail)) > budget:
        tail.pop()
    return tail


# ---------------------------------------------------------------- 改写


def _telegraph(entry: _Entry) -> _Entry:
    """去同日日期前缀与虚词，保留否定词、大写开头与含数字的词。"""
    out: list[str] = []
    for tok in _TELE_TOKEN.findall(_strip_date_prefix(entry.text, entry.created_at)):
        low = tok.lower()
        if low in _TELE_KEEP or tok[0].isupper() or any(c.isdigit() for c in tok):
            out.append(tok)
        elif tok in ".,;:!?":
            if out and out[-1] not in ",;":
                out.append(",")
        elif low not in _TELE_DROP:
            out.append(tok)
    text = " ".join(out).replace(" ,", ",").strip(" ,")
    return _Entry(entry.index, entry.created_at, text, telegraph=True)


def _role_letter(match: re.Match[str]) -> str:
    return "U" if match.group(1).lower() == "user" else "A"


def _lexical_tokens(text: str) -> list[str]:
    return [t.lower() for t in _LEXICAL.findall(text or "")]


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


# ---------------------------------------------------------------- 渲染


def _render_official(entries: list[_Entry]) -> str:
    lines = [OFFICIAL_INTRO, ""]
    for e in sorted(entries, key=lambda x: x.created_at):
        lines.append(f"({_human_date(e.created_at)}) {e.text}")
    return "\n".join(lines)


def _render_role(entries: list[_Entry]) -> str:
    lines = [ROLE_INTRO]
    ordered = sorted(entries, key=lambda x: x.created_at)
    for date, group in itertools.groupby(ordered, key=lambda x: _human_date(x.created_at)):
        lines.append(f"\n[{date}]")
        lines.extend(_strip_date_prefix(e.text, e.created_at) for e in group)
    return "\n".join(lines)


def _human_date(created_at: str) -> str:
    if not created_at:
        return UNKNOWN_DATE
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(created_at[:26].rstrip("Z"), fmt).strftime("%A, %B %d, %Y")
        except ValueError:
            continue
    return created_at[:10]


def _strip_date_prefix(text: str, created_at: str) -> str:
    """去掉与事件日期同日的 ``On YYYY-MM-DD, `` 前缀；日期已由行首或日期标题承载。"""
    prefix = f"On {created_at[:10]}, "
    size = len(prefix)
    return text[size:] if created_at and text.startswith(prefix) else text
