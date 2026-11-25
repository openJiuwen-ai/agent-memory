# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ContentPacker 算子与 AdaptiveContentPacker 默认实现的契约单元测试（retrieval F15）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import (
    ContentLayers,
    MemoryUnit,
    Modality,
    ScoredMemoryUnit,
    Segment,
    Temporal,
)
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.retrieval.content_packer import (
    ContentPacker,
    ContentPackerProducer,
    PackStrategy,
    to_pack_strategy,
)
from jiuwen_memory.retrieval.content_packer_impl.adaptive_content_packer import (
    AdaptiveContentPacker,
    estimate_bpe_tokens,
)
from jiuwen_memory.retrieval.retriever import RetrieverProducer
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.bootstrap import register_backends

pytestmark = pytest.mark.unit

DAY1 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
DAY2 = datetime(2023, 5, 8, 9, 30, tzinfo=UTC)


def _cand(
    uid: str,
    content: str,
    score: float = 0.5,
    *,
    t_event: datetime | None = DAY1,
    l0: str = "",
) -> ScoredMemoryUnit:
    unit = MemoryUnit(
        id=uid,
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(t_event=t_event),
        layers=ContentLayers(l0=l0),
    )
    return ScoredMemoryUnit(unit, score, RecallChannel.KEYWORD)


def _candidates(count: int) -> list[ScoredMemoryUnit]:
    topics = ["painting", "hiking", "adoption", "guitar", "pottery", "camping", "violin"]
    items: list[ScoredMemoryUnit] = []
    for idx in range(count):
        day = datetime(2023, 5, 7 + idx % 3, 10, 0, tzinfo=UTC)
        date_str = day.strftime("%Y-%m-%d")
        text = (
            f"On {date_str}, The user said that the {topics[idx % len(topics)]} "
            f"class number {idx} was really fun and that the assistant should remember it."
        )
        items.append(_cand(f"u{idx}", text, score=1.0 - idx / (count + 1), t_event=day))
    return items


# -- 算子契约与装配注册 -------------------------------------------------------- #


def test_operator_metadata_and_health() -> None:
    packer = AdaptiveContentPacker()
    assert isinstance(packer, ContentPacker)
    assert packer.operator_type() is RetrievalOperatorType.CONTENT_PACKER
    assert packer.health() is None
    assert packer.explain() == {"configured_strategy": "auto", "budget_ratio": "0.8"}


def test_producer_registers_default_and_adaptive_targets() -> None:
    register_operators()
    known = ContentPackerProducer.known()
    assert "default" in known and "adaptive" in known
    ctx = AssemblyContext.from_dict(default_config_dict())
    built = ContentPackerProducer.build_named("default", ctx)
    assert isinstance(built, AdaptiveContentPacker)


@pytest.mark.parametrize(
    ("in_globals", "in_params", "expected"),
    [
        (None, None, False),
        (True, None, True),
        (None, True, True),
        (True, False, False),
    ],
)
def test_switch_precedence_between_retriever_params_and_globals(
    in_globals, in_params, expected
) -> None:
    register_plugins()
    register_backends()
    register_operators()
    raw = default_config_dict()
    if in_globals is not None:
        raw.get("globals", {})["content_pack_enabled"] = in_globals
    if in_params is not None:
        raw.get("retriever", {}).get("default", {}).get("params", {})[
            "content_pack_enabled"
        ] = in_params
    try:
        retriever = RetrieverProducer.build_named("default", AssemblyContext.from_dict(raw))
        assert retriever.content_pack_enabled is expected
        assert isinstance(retriever.content_packer, AdaptiveContentPacker)
    finally:
        Factory.reset_all()


# -- 策略路由与参数校验 -------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, PackStrategy.ROLE_COMPACT),
        (10, PackStrategy.ROLE_COMPACT),
        (11, PackStrategy.TELEGRAPH),
        (20, PackStrategy.TELEGRAPH),
        (21, PackStrategy.DROP),
        (50, PackStrategy.DROP),
    ],
)
def test_auto_strategy_routes_by_candidate_count(count: int, expected: PackStrategy) -> None:
    packer = AdaptiveContentPacker()
    assert packer.resolve_strategy(count) is expected
    _, detail = packer.pack_with_detail(ParsedQuery(raw="q"), _candidates(count))
    assert detail.get("strategy") == expected.value


@pytest.mark.parametrize("raw", ["drop", "DROP", PackStrategy.DROP])
def test_to_pack_strategy_accepts_enum_and_case_variants(raw) -> None:
    assert to_pack_strategy(raw) is PackStrategy.DROP


@pytest.mark.parametrize("bad_strategy", ["invalid", 123])
def test_invalid_strategy_raises_validation_error(bad_strategy) -> None:
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(strategy=bad_strategy)


@pytest.mark.parametrize("ratio", [0.0, -0.1, 1.5, float("nan"), True])
def test_invalid_budget_ratio_raises_validation_error(ratio) -> None:
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(budget_ratio=ratio)


def test_invalid_item_thresholds_and_weights_raise_validation_error() -> None:
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(role_compact_max_items=-1)
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(role_compact_max_items=20, telegraph_max_items=10)
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(w_rel=-0.1)
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(w_recency=-0.1)
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(w_entity=-0.1)
    with pytest.raises(ValidationError):
        AdaptiveContentPacker(recency_tau_days=0.0)


def test_empty_and_unmaterialized_candidates_pass_through_safely() -> None:
    packer = AdaptiveContentPacker()
    packed_empty, empty_detail = packer.pack_with_detail(ParsedQuery(raw="q"), [])
    assert packed_empty == []
    assert empty_detail.get("out") == "0"

    bare = [ScoredUnit("u1", 0.9, RecallChannel.KEYWORD)]
    packed_bare, bare_detail = packer.pack_with_detail(ParsedQuery(raw="q"), bare)
    assert packed_bare == bare
    assert bare_detail.get("skipped") == "unmaterialized"


# -- 三种压缩策略与非破坏性 MemoryUnit 输出 ------------------------------------ #


def test_role_compact_abbreviates_roles_strips_same_day_prefix_and_preserves_nouns() -> None:
    cands = [
        _cand("u0", "On 2023-05-07, The user adopted a dog.", 0.9, t_event=DAY1, l0="old l0"),
        _cand("u1", "The assistant's advice was to visit a shelter.", 0.8, t_event=DAY1),
        _cand("u2", "The user's sister is an assistant manager.", 0.7, t_event=DAY2),
        _cand("u3", "Other users like cats.", 0.6, t_event=DAY2),
        _cand("u4", "A user and An assistant walked by.", 0.5, t_event=DAY2),
    ]
    packer = AdaptiveContentPacker(budget_ratio=1.0, strategy=PackStrategy.ROLE_COMPACT)
    out, detail = packer.pack_with_detail(ParsedQuery(raw="dog"), cands)

    assert [c.unit.content for c in out] == [
        "U adopted a dog.",
        "A's advice was to visit a shelter.",
        "U's sister is an assistant manager.",
        "Other users like cats.",
        "A user and An assistant walked by.",
    ]
    assert detail.get("telegraphed") == "0" and detail.get("dropped") == "0"
    # 浅拷贝替换正文与 layers（不原地修改输入对象，保证主链路异常回退 return final 干净）。
    assert cands[0].unit.content == "On 2023-05-07, The user adopted a dog."
    assert cands[0].unit.layers.l0 == "old l0"
    assert out[0].unit.layers.l0 == ""
    # 未发生改写的条目（u3）直接复用原对象及其缓存层。
    assert out[3] is cands[3]


def test_telegraph_keeps_names_digits_and_negation_while_dropping_function_words() -> None:
    cand = _cand(
        "u0",
        "On 2023-05-08, The user went to the LGBTQ support group on 7 May 2023 and "
        "did not find it very empowering.",
        0.9,
        t_event=DAY2,
    )
    packer = AdaptiveContentPacker(budget_ratio=0.85, strategy=PackStrategy.TELEGRAPH)
    out, detail = packer.pack_with_detail(ParsedQuery(raw="q"), [cand])
    assert len(out) == 1
    assert detail.get("telegraphed") == "1"
    assert out[0].unit.content == (
        "The user went LGBTQ support group 7 May 2023 not find empowering"
    )


def test_telegraph_keeps_high_value_head_verbatim_and_drops_duplicate_tail() -> None:
    cands = [
        _cand("u0", "The user adopted a golden retriever named Max in Boston.", 0.9, t_event=DAY1),
        _cand("u1", "The user adopted a retriever named Max in Boston.", 0.2, t_event=DAY1),
    ]
    packer = AdaptiveContentPacker(budget_ratio=0.75, strategy=PackStrategy.TELEGRAPH)
    out, detail = packer.pack_with_detail(ParsedQuery(raw="Max"), cands)
    assert [c.unit_id for c in out] == ["u0"]
    assert out[0].unit.content == cands[0].unit.content
    assert detail.get("dropped") == "1"


def test_drop_keeps_selected_units_verbatim_and_preserves_relative_order() -> None:
    cands = _candidates(30)
    packer = AdaptiveContentPacker(budget_ratio=0.8, strategy=PackStrategy.DROP)
    out, detail = packer.pack_with_detail(ParsedQuery(raw="guitar class"), cands)

    orig_by_id = {c.unit_id: c.unit.content for c in cands}
    indices = [int(c.unit_id[1:]) for c in out]
    assert indices == sorted(indices), "保留条目须维持原相关性相对顺序"
    assert all(c.unit.content == orig_by_id.get(c.unit_id) for c in out)
    assert detail.get("telegraphed") == "0"
    assert int(detail.get("dropped", "0")) > 0


def test_keep_count_preserves_all_items_and_records_suppressed_drops() -> None:
    """请求级 keep_count：即使会走 drop / telegraph 去重，也不得减少条数。"""
    cands = _candidates(30)
    # 0.8 默认预算下：保条数的同时头部保留部分原文，且总 token 严格落入 80% 预算内。
    default_packer = AdaptiveContentPacker(budget_ratio=0.8, strategy=PackStrategy.AUTO)
    def_dropped, _ = default_packer.pack_with_detail(
        ParsedQuery(raw="guitar class"), cands
    )
    def_kept, def_detail = default_packer.pack_with_detail(
        ParsedQuery(raw="guitar class"), cands, keep_count=True
    )
    assert len(def_kept) == len(cands)
    assert int(def_detail.get("kept", "0")) > 0
    assert int(def_detail.get("telegraphed", "0")) > 0
    assert int(def_detail.get("tokens", "0")) <= 0.8 * int(def_detail.get("full_tokens", "0"))
    assert int(def_detail.get("suppressed_drops", "0")) == len(cands) - len(def_dropped)

    # 50% 预算下：连同电报式尾部一并计入预算，仍能装进 50% 预算且条数不减。
    packer = AdaptiveContentPacker(budget_ratio=0.5, strategy=PackStrategy.AUTO)
    dropped_out, dropped_detail = packer.pack_with_detail(
        ParsedQuery(raw="guitar class"), cands
    )
    assert int(dropped_detail.get("dropped", "0")) > 0
    assert len(dropped_out) < len(cands)

    kept_out, kept_detail = packer.pack_with_detail(
        ParsedQuery(raw="guitar class"), cands, keep_count=True
    )
    assert [c.unit_id for c in kept_out] == [c.unit_id for c in cands]
    assert kept_detail.get("keep_count") == "true"
    assert int(kept_detail.get("tokens", "0")) <= 0.5 * int(kept_detail.get("full_tokens", "0"))
    assert kept_detail.get("dropped") == "0"
    assert int(kept_detail.get("suppressed_drops", "0")) == len(cands) - len(dropped_out)
    assert kept_detail.get("strategy") == PackStrategy.TELEGRAPH.value

    # 极端紧预算（20%）连全电报式也装不进预算：走全电报式兜底，保条数优先于预算硬顶。
    tight_packer = AdaptiveContentPacker(budget_ratio=0.2, strategy=PackStrategy.AUTO)
    tight_out, tight_detail = tight_packer.pack_with_detail(
        ParsedQuery(raw="guitar class"), cands, keep_count=True
    )
    assert len(tight_out) == len(cands)
    assert tight_detail.get("kept") == "0"
    assert tight_detail.get("telegraphed") == str(len(cands))

    # telegraph 档：高度重复的 15 条在无 keep_count 时会去重删条，有 keep_count 时须记 suppressed。
    dup_cands = [
        _cand(
            f"d{i}",
            "The user adopted a golden retriever named Max in Boston. " * 3,
            1.0 - i * 0.01,
        )
        for i in range(15)
    ]
    tele_packer = AdaptiveContentPacker(budget_ratio=0.8, strategy=PackStrategy.AUTO)
    tele_out, tele_detail = tele_packer.pack_with_detail(
        ParsedQuery(raw="golden Max Boston"), dup_cands
    )
    assert tele_detail.get("strategy") == PackStrategy.TELEGRAPH.value
    assert int(tele_detail.get("dropped", "0")) > 0
    tele_kept, tele_kept_detail = tele_packer.pack_with_detail(
        ParsedQuery(raw="golden Max Boston"), dup_cands, keep_count=True
    )
    assert len(tele_kept) == len(dup_cands)
    assert int(tele_kept_detail.get("suppressed_drops", "0")) == len(dup_cands) - len(
        tele_out
    )


@pytest.mark.parametrize("strategy", list(PackStrategy))
@pytest.mark.parametrize("ratio", [0.5, 0.8, 1.0])
def test_all_strategies_respect_token_budget(strategy: PackStrategy, ratio: float) -> None:
    cands = _candidates(25)
    packer = AdaptiveContentPacker(budget_ratio=ratio, strategy=strategy)
    out, detail = packer.pack_with_detail(ParsedQuery(raw="which class was fun?"), cands)

    full_tokens = sum(estimate_bpe_tokens(c.unit.content) for c in cands)
    packed_tokens = sum(estimate_bpe_tokens(c.unit.content) for c in out)
    assert packed_tokens <= ratio * full_tokens
    assert int(detail.get("kept", "0")) + int(detail.get("telegraphed", "0")) + int(
        detail.get("dropped", "0")
    ) == len(cands)


def test_at_least_top_mixed_score_unit_survives_under_tiny_budget() -> None:
    cands = [
        _cand("u0", "irrelevant " * 40, 0.2),
        _cand("u1", "important answer " * 40, 0.9),
    ]
    packer = AdaptiveContentPacker(budget_ratio=0.05, strategy=PackStrategy.DROP)
    out = packer.pack(ParsedQuery(raw="important answer"), cands)
    assert [c.unit_id for c in out] == ["u1"]


def test_mixed_score_question_overlap_and_zero_score_fallback() -> None:
    packer = AdaptiveContentPacker()
    order = packer.mixed_score_order(
        "hiking Yosemite trip",
        [1.0, 0.5, 0.0],
        ["Likes jazz.", "Went on a hiking trip to Yosemite.", "Owns a bike."],
    )
    assert order == [1, 0, 2]

    zero_order = packer.mixed_score_order("unrelated", [0.0, 0.0, 0.0], ["a", "b", "c"])
    assert zero_order == [0, 1, 2]


def test_mixed_score_recency_and_entity_weights_take_effect_when_configured() -> None:
    now = datetime(2023, 6, 7, 10, 0, tzinfo=UTC)
    old_day = datetime(2023, 1, 1, 10, 0, tzinfo=UTC)
    recent_day = datetime(2023, 6, 6, 10, 0, tzinfo=UTC)

    # 开启 w_recency 时，同等得分与长度下更新的记忆优先保留。
    old_cand = _cand("old", "status note " * 20, 0.8, t_event=old_day)
    recent_cand = _cand("recent", "status note " * 20, 0.8, t_event=recent_day)
    recency_packer = AdaptiveContentPacker(
        budget_ratio=0.55,
        strategy=PackStrategy.DROP,
        w_rel=0.20,
        w_question=0.25,
        w_recency=0.50,
        w_entity=0.0,
        w_cost=0.05,
        recency_tau_days=30.0,
        clock=lambda: now,
    )
    rec_out = recency_packer.pack(ParsedQuery(raw="status"), [old_cand, recent_cand])
    assert [c.unit_id for c in rec_out] == ["recent"]

    # 开启 w_entity（通过 YAML 配置装配）时，实体命中查询词的记忆优先保留。
    no_ent = _cand("u_plain", "visited city museum " * 15, 0.8, t_event=DAY1)
    with_ent = _cand("u_ent", "visited city museum " * 15, 0.8, t_event=DAY1)
    with_ent.unit.entities = ["Boston", "Caroline"]

    register_operators()
    raw = default_config_dict()
    params = raw.get("content_packer", {}).get("default", {}).get("params", {})
    params["budget_ratio"] = 0.55
    params["strategy"] = "drop"
    params["w_entity"] = 0.60
    try:
        ent_packer = ContentPackerProducer.build_named("default", AssemblyContext.from_dict(raw))
        ent_out = ent_packer.pack(
            ParsedQuery(raw="trip with Caroline"), [no_ent, with_ent]
        )
        assert [c.unit_id for c in ent_out] == ["u_ent"]
    finally:
        Factory.reset_all()
