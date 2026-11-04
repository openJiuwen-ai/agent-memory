"""检索结果上下文打包的纯单测（`retrieval/context_pack.py`）。

模块不访问存储、不调模型，因此脱离装配单测。与 LoCoMo 实验实现的逐字等价性
（1540 题 × Top-10/20/50 记忆块完全一致）在 F09「验证」里记录，这里只钉规则与契约。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.retrieval import context_pack
from jiuwen_memory.retrieval.context_pack import (
    PackMemory,
    PackStrategy,
    mixed_score_order,
    pack_context,
    render_memories,
    resolve_strategy,
)

pytestmark = pytest.mark.unit

DAY1 = "2023-05-07T10:00:00"
DAY2 = "2023-05-08T09:30:00"


def _memories(n: int) -> list[PackMemory]:
    topics = ["painting", "hiking", "adoption", "guitar", "pottery", "camping", "violin"]
    return [
        PackMemory(
            content=(
                f"On 2023-05-{7 + i % 3:02d}, The user said that the {topics[i % len(topics)]} "
                f"class number {i} was really fun and that the assistant should remember it."
            ),
            created_at=f"2023-05-{7 + i % 3:02d}T10:00:00",
            score=1.0 - i / (n + 1),
        )
        for i in range(n)
    ]


def _assert_partition(packed, n: int) -> None:
    groups = [set(packed.kept), set(packed.telegraphed), set(packed.dropped)]
    assert sum(len(g) for g in groups) == n, "kept/telegraphed/dropped must not overlap"
    assert set().union(*groups) == set(range(n)), "every input index must be accounted for"


# -- 策略选择与输入校验 ------------------------------------------------------- #


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, PackStrategy.ROLE_COMPACT),
        (10, PackStrategy.ROLE_COMPACT),
        (11, PackStrategy.TELEGRAPH),
        (20, PackStrategy.TELEGRAPH),
        (21, PackStrategy.DROP),
        (200, PackStrategy.DROP),
    ],
)
def test_auto_strategy_follows_item_count(n: int, expected: PackStrategy) -> None:
    assert resolve_strategy(n) is expected, f"{n} items should resolve to {expected}"
    assert pack_context("q", _memories(n)).strategy is expected, "auto must use resolve_strategy"


def test_empty_input_packs_to_empty_text() -> None:
    packed = pack_context("what did the user paint?", [])
    assert packed.text == "", "no memories means no memory block"
    assert (packed.kept, packed.telegraphed, packed.dropped) == ((), (), ()), "no indices"
    assert packed.tokens == 0 and packed.full_tokens == 0, "no tokens are counted"


@pytest.mark.parametrize("ratio", [0.0, -0.1, 1.5])
def test_budget_ratio_outside_unit_interval_is_rejected(ratio: float) -> None:
    with pytest.raises(ValueError):
        pack_context("q", _memories(3), budget_ratio=ratio)


def test_strategy_accepts_its_string_value() -> None:
    packed = pack_context("q", _memories(5), strategy="drop")
    assert packed.strategy is PackStrategy.DROP, "string values map onto PackStrategy"


# -- 渲染 ------------------------------------------------------------------- #


def test_official_rendering_is_chronological_with_human_dates() -> None:
    text = render_memories(
        [
            PackMemory("Later fact.", DAY2),
            PackMemory("Undated fact."),
            PackMemory("Earlier fact.", DAY1),
        ]
    )
    assert text.splitlines() == [
        context_pack.OFFICIAL_INTRO,
        "",
        "(unknown date) Undated fact.",
        "(Sunday, May 07, 2023) Earlier fact.",
        "(Monday, May 08, 2023) Later fact.",
    ], "official block sorts by created_at and renders one dated line per memory"


def test_role_compact_groups_by_date_and_abbreviates_roles() -> None:
    memories = [
        PackMemory("On 2023-05-07, The user adopted a dog.", DAY1),
        PackMemory("The assistant's advice was to visit a shelter.", DAY1),
        PackMemory("The user's sister is an assistant manager.", DAY2),
        PackMemory("Other users like cats.", DAY2),
    ]
    packed = pack_context("q", memories, budget_ratio=1.0, strategy=PackStrategy.ROLE_COMPACT)
    assert packed.text.splitlines() == [
        context_pack.ROLE_INTRO,
        "",
        "[Sunday, May 07, 2023]",
        "U adopted a dog.",
        "A's advice was to visit a shelter.",
        "",
        "[Monday, May 08, 2023]",
        "U's sister is an assistant manager.",
        "Other users like cats.",
    ], "same-date prefix is stripped; 'an assistant manager' and 'users' stay as written"
    assert packed.kept == (0, 1, 2, 3) and packed.telegraphed == (), "lossless when it fits"


# -- 电报式 ----------------------------------------------------------------- #


def test_telegraph_drops_function_words_but_keeps_names_numbers_and_negation() -> None:
    memory = PackMemory(
        "On 2023-05-08, The user went to the LGBTQ support group on 7 May 2023 and "
        "did not find it very empowering.",
        DAY2,
    )
    packed = pack_context("q", [memory], strategy=PackStrategy.TELEGRAPH, budget_ratio=0.9)
    assert packed.telegraphed == (0,), "a 0.9 budget cannot hold the verbatim line"
    assert packed.text.splitlines()[-1] == (
        "(Monday, May 08, 2023) The user went LGBTQ support group 7 May 2023 not find empowering"
    ), "telegraph keeps capitalised words, digits and negation; drops function words"


def test_telegraph_strategy_compresses_the_low_ranked_tail() -> None:
    facts = [
        "The user started painting sunsets with watercolors after a trip to Maui.",
        "The assistant recommended a beginner pottery workshop near the harbor.",
        "The user's brother moved to Denver for a nursing job in March.",
        "The user is training for a half marathon scheduled for October 2023.",
        "The user adopted two kittens from the shelter and named them Pixel and Byte.",
        "The user is not a fan of spicy food but enjoys mild Thai curries.",
        "The user plays violin in a community orchestra every Thursday evening.",
        "The user's daughter won first prize at the regional science fair.",
    ]
    memories = [PackMemory(f, DAY1 if i % 2 else DAY2, 1.0 - i / 10) for i, f in enumerate(facts)]
    packed = pack_context("q", memories, strategy=PackStrategy.TELEGRAPH, budget_ratio=0.95)
    assert packed.telegraphed, "a 0.95 budget must telegraph part of the tail"
    assert packed.kept, "the head of the Mixed-Score order stays verbatim"
    order = mixed_score_order("q", memories)
    assert set(packed.kept) == set(order[: len(packed.kept)]), "verbatim items are the top h"
    _assert_partition(packed, len(memories))


def test_tail_item_covered_by_the_head_is_dropped_not_telegraphed() -> None:
    memories = [
        PackMemory("The user adopted a golden retriever named Max in Boston.", DAY1, 0.9),
        PackMemory("The user adopted a retriever named Max in Boston.", DAY1, 0.1),
    ]
    packed = pack_context("dog", memories, strategy=PackStrategy.TELEGRAPH, budget_ratio=0.75)
    assert packed.kept == (0,), "the higher-ranked memory stays verbatim"
    assert packed.dropped == (1,), "a tail item whose content words are covered is dropped"


# -- 直接删条与预算 ----------------------------------------------------------- #


def test_drop_keeps_selected_memories_verbatim() -> None:
    memories = _memories(30)
    packed = pack_context("q", memories, strategy=PackStrategy.DROP)
    lines = packed.text.splitlines()[2:]
    assert len(lines) == len(packed.kept), "one official line per kept memory"
    contents = {m.content for m in memories}
    assert all(line.split(") ", 1)[1] in contents for line in lines), "no text is rewritten"
    assert packed.telegraphed == (), "drop never telegraphs"
    _assert_partition(packed, len(memories))


@pytest.mark.parametrize("strategy", list(PackStrategy))
@pytest.mark.parametrize("ratio", [0.5, 0.8, 1.0])
def test_packed_block_fits_the_budget(strategy: PackStrategy, ratio: float) -> None:
    memories = _memories(25)
    packed = pack_context("which class was fun?", memories, budget_ratio=ratio, strategy=strategy)
    assert packed.budget == pytest.approx(ratio * packed.full_tokens), "budget = ratio × full"
    assert packed.tokens <= packed.budget, f"{strategy} at {ratio} exceeded its budget"
    assert packed.tokens == context_pack.estimate_tokens(packed.text), "tokens describe text"
    _assert_partition(packed, len(memories))


def test_one_memory_is_kept_when_nothing_fits() -> None:
    memories = [PackMemory("x" * 400, DAY1, 0.2), PackMemory("y" * 400, DAY1, 0.9)]
    packed = pack_context("q", memories, budget_ratio=0.1, strategy=PackStrategy.DROP)
    assert packed.kept == (1,), "the top Mixed-Score memory survives even over budget"


def test_custom_token_counter_sets_both_budget_and_usage() -> None:
    def words(text: str) -> int:
        return len(text.split())

    memories = _memories(15)
    packed = pack_context("q", memories, count_tokens=words)
    assert packed.full_tokens == words(render_memories(memories)), "budget base uses counter"
    assert packed.tokens == words(packed.text), "usage uses the same counter"


def test_packing_is_deterministic() -> None:
    memories = _memories(20)
    assert pack_context("which class?", memories) == pack_context("which class?", memories), (
        "same input must give the same block"
    )


# -- Mixed-Score 排序 ------------------------------------------------------- #


def test_question_overlap_can_outrank_retrieval_score() -> None:
    memories = [
        PackMemory("Likes jazz.", DAY1, 1.0),
        PackMemory("Went on a hiking trip to Yosemite.", DAY1, 0.5),
        PackMemory("Owns a bike.", DAY1, 0.0),
    ]
    assert mixed_score_order("hiking Yosemite trip", memories) == [1, 0, 2], (
        "full question overlap (0.50) beats half of the min-max score range (0.45 × 0.5)"
    )


def test_zero_scores_fall_back_to_input_rank() -> None:
    memories = [PackMemory(f"fact {i}") for i in range(4)]
    assert mixed_score_order("unrelated", memories) == [0, 1, 2, 3], "rank proxy keeps order"
