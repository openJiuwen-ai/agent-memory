"""Complementary entry views stay inside the original materialization quota."""

from itertools import permutations

import pytest

from jiuwen_memory.common.graphmem_semantics import (
    EntityLabel,
    EventTime,
    GraphSemantics,
    SceneEvent,
)
from jiuwen_memory.common.type_def import (
    RecallBatch,
    RecallChannel,
    RecallResult,
    Scope,
    ScoredUnit,
)
from jiuwen_memory.retrieval.graphmem_query import QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import (
    EntryProbePool,
    limit_recalled,
    ranked_entry_ids,
    retain_attributed_entry,
)

pytestmark = pytest.mark.unit


def batches(overview="overview"):
    return [
        RecallBatch(RecallChannel.VECTOR, "vector_l2", [ScoredUnit("detail", 0.8)]),
        RecallBatch(RecallChannel.VECTOR, "vector_l1", [ScoredUnit(overview, 0.7)]),
        RecallBatch(RecallChannel.VECTOR, "vector_l0", [ScoredUnit("broad", 0.9)]),
        RecallBatch(RecallChannel.KEYWORD, "keyword_l2", [ScoredUnit("broad", 2)]),
    ]


@pytest.mark.parametrize(
    "limit,expected",
    [(0, set()), (1, {"detail"}), (2, {"detail", "broad"}), (3, {"detail", "overview", "broad"})],
)
def test_original_two_entries_survive_before_complementary_overview(limit, expected):
    for order in permutations(batches()):
        result = limit_recalled(RecallResult(batches=list(order)), limit)
        assert {c.unit_id for b in result.batches for c in b.candidates} == expected
        assert len(result.batches) == len(order)
        for before, after in zip(order, result.batches, strict=True):
            assert after.source == before.source and after.channel is before.channel
            assert all(c in before.candidates for c in after.candidates)


def test_identical_view_heads_do_not_force_an_extra_overview_candidate():
    source = batches(overview="detail")
    source[1].candidates.append(ScoredUnit("runner-up", 0.6))
    assert ranked_entry_ids(RecallResult(batches=source))[:2] == ["detail", "broad"]


def test_tight_entry_pool_reserves_a_distinct_native_two_view_match():
    source = batches(overview="alternate")
    source[0].candidates.append(ScoredUnit("alternate", 0.6))
    recall = RecallResult(batches=source)
    pool = EntryProbePool.from_recalled(
        Scope(), recall, 2, 5, detail_keyword=True, corroborate=True
    )
    assert pool.initial_ids == ("detail", "broad")
    assert pool.corroborated_alternative == "alternate"
    assert pool.corroborated_alternative in pool.alternative_ids
    roomy = EntryProbePool.from_recalled(
        Scope(), recall, 2, 8, detail_keyword=True, corroborate=True
    )
    assert roomy.corroborated_alternative is None


@pytest.mark.parametrize(
    "kind",
    ["absent_overview", "custom_source", "wrong_channel", "nonfinite_overview", "absent_detail"],
)
def test_missing_or_ineligible_native_view_preserves_original_fusion(kind):
    source = batches()
    if kind == "absent_overview":
        source.pop(1)
    elif kind == "custom_source":
        source[1].source = "custom_vector_overview"
    elif kind == "wrong_channel":
        source[1].channel = RecallChannel.KEYWORD
    elif kind == "nonfinite_overview":
        source[1].candidates[0].score = float("nan")
    else:
        source.pop(0)
    order = ranked_entry_ids(RecallResult(batches=source))
    assert order[0] == ("broad" if kind == "absent_detail" else "detail")
    if kind != "absent_detail":
        assert order[1] == "broad"


def test_entry_promotion_uses_a_single_sentence_in_existing_keyword_hits():
    alice = EntityLabel("Alice", "person")
    unrelated = GraphSemantics(
        (alice,), (SceneEvent("bought groceries", (alice.key,), "completed", EventTime(), (0,)),)
    ).encode()
    hits = {
        "leader": "Alice joined a club. Bob visited the forest with friends.",
        "tail": "Alice bought groceries.",
        "split": "Alice stayed home. Bob visited the forest with friends.",
        "correct": "Alice visited the forest with friends.",
    }
    ids = list(hits)
    recalled = RecallResult(
        batches=[
            RecallBatch(
                RecallChannel.KEYWORD,
                "keyword_l1",
                [
                    ScoredUnit(uid, 1.0, entry_semantics=unrelated, entry_keyword_text=text)
                    for uid, text in hits.items()
                ],
            )
        ]
    )
    needs = QueryNeeds(mode="fact", entities=("Alice",), facets=("visited forest with friends",))
    order, selected = retain_attributed_entry(recalled, ids, 2, needs)
    assert selected == "correct"
    assert order == ["leader", "correct", "tail", "split"]
    pool = EntryProbePool.from_recalled(
        Scope(), recalled, 2, 4, ordered_ids=order, attributed_entry=selected
    )
    assert pool.initial_ids == ("leader", "correct")
    assert pool.alternative_ids == ("tail", "split")
    limited = limit_recalled(recalled, 2, ordered_ids=order)
    assert {hit.unit_id for hit in limited.batches[0].candidates} == set(pool.initial_ids)
    dated = QueryNeeds(
        mode="temporal",
        entities=needs.entities,
        facets=needs.facets,
        time_from="2026-01-04T00:00:00Z",
    )
    assert retain_attributed_entry(recalled, ids, 2, dated) == (ids, None)


def test_complementary_entry_requires_jointly_attributed_words_and_keeps_the_head():
    recalled = RecallResult(
        batches=[
            RecallBatch(
                RecallChannel.KEYWORD,
                "keyword_l2",
                [
                    ScoredUnit("head", 4, entry_keyword_text="Alice ran a winter drill."),
                    ScoredUnit("tail", 3, entry_keyword_text="Alice attended the winter drill."),
                    ScoredUnit(
                        "split", 2, entry_keyword_text="Alice visited. Bob ran harbor rescue."
                    ),
                    ScoredUnit(
                        "complement", 1, entry_keyword_text="Alice practiced harbor rescue."
                    ),
                ],
            )
        ]
    )
    order = ["head", "tail", "split", "complement"]
    needs = QueryNeeds(mode="fact", entities=("Alice",), facets=("winter drill harbor rescue",))
    assert retain_attributed_entry(recalled, order, 2, needs) == (order, None)
    selected_order, selected = retain_attributed_entry(recalled, order, 2, needs, diversify=True)
    assert selected == "complement"
    assert selected_order == ["head", "complement", "tail", "split"]
    limited = limit_recalled(recalled, 2, ordered_ids=selected_order)
    assert {hit.unit_id for hit in limited.batches[0].candidates} == {"head", "complement"}


def test_dated_entry_requires_a_source_bound_event_in_the_window():
    alice = EntityLabel("Alice", "person", sources=(0,))
    event_day = EventTime(
        "2026-01-06T00:00:00Z", "2026-01-07T00:00:00Z", precision="day", kind="occurrence"
    )
    event = SceneEvent("harbor rescue training", (alice.key,), "completed", event_day, (0,))
    encoded = GraphSemantics((alice,), (event,)).encode()
    recalled = RecallResult(
        batches=[
            RecallBatch(
                RecallChannel.KEYWORD,
                "keyword_l2",
                [
                    ScoredUnit("head", 4),
                    ScoredUnit(
                        "tail", 3, entry_keyword_text="Alice attended harbor rescue training."
                    ),
                    ScoredUnit("within", 2, entry_semantics=encoded),
                ],
            )
        ]
    )
    order = ["head", "tail", "within"]
    options = dict(mode="temporal", entities=("Alice",), facets=("harbor rescue training",))
    match = QueryNeeds(**options, time_from="2026-01-05T00:00:00Z", time_to="2026-01-08T00:00:00Z")
    later = QueryNeeds(**options, time_from="2026-01-07T00:00:00Z")
    assert retain_attributed_entry(recalled, order, 2, match, diversify=True)[1] == "within"
    assert retain_attributed_entry(recalled, order, 2, later, diversify=True) == (order, None)


def test_deferred_elapsed_anchor_requires_joint_names_in_detailed_native_text():
    from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery

    recall = RecallResult(
        batches=[
            RecallBatch(
                RecallChannel.KEYWORD,
                "keyword_l2",
                [
                    ScoredUnit(
                        "related", 7, entry_keyword_text="A Harbor Rescue pilot spoke in Boston."
                    ),
                    ScoredUnit(
                        "split",
                        6,
                        entry_keyword_text="A Harbor Rescue pilot. Boston hosted a tour.",
                    ),
                    ScoredUnit(
                        "candidate",
                        5,
                        entry_keyword_text="I met the Harbor Rescue pilot in Boston.",
                    ),
                    ScoredUnit(
                        "vector", 4, entry_keyword_text="I attended the air show in Austin."
                    ),
                ],
            )
        ]
    )
    query = GraphMemParsedQuery(
        raw="How many weeks after the Harbor Rescue meeting in Boston did I return?",
        navigation_needs=QueryNeeds(
            mode="temporal",
            entities=("Harbor Rescue", "Boston"),
            facets=("meeting with Harbor Rescue in Boston",),
        ),
    )
    order = ["related", "vector", "split", "candidate"]
    pool = EntryProbePool.from_recalled(
        Scope(), recall, 2, 4, ordered_ids=order, anchor_query=query
    )
    assert pool.initial_ids == ("related", "vector")
    assert pool.temporal_anchor_alternative == "candidate"
    query.navigation_needs = QueryNeeds(
        mode="temporal",
        entities=("Harbor Rescue", "Boston"),
        facets=("meeting with Harbor Rescue in Boston",),
        time_from="2026-07-01T00:00:00Z",
    )
    assert (
        EntryProbePool.from_recalled(
            Scope(),
            recall,
            2,
            4,
            ordered_ids=order,
            anchor_query=query,
        ).temporal_anchor_alternative
        is None
    )


def test_native_clause_keyword_enforces_one_probe_and_does_not_expand_seed_count():
    from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery
    from jiuwen_memory.retrieval.graphmem_relation_plan import with_missing_clause_keyword_entry

    recall = RecallResult(
        batches=[
            RecallBatch(
                RecallChannel.KEYWORD,
                "keyword_l2",
                [
                    ScoredUnit("head", 4, entry_keyword_text="Alice held a winter rescue drill."),
                    ScoredUnit(
                        "duplicate", 3, entry_keyword_text="Alice held a winter rescue drill."
                    ),
                ],
            )
        ]
    )
    order = ["head", "duplicate"]
    needs = QueryNeeds(
        mode="comparison",
        entities=("Alice", "Bob"),
        facets=("Alice winter rescue drill", "Bob harbor bridge repair"),
    )

    class Domain:
        calls = 0

        def recall(self, scope, query, channels, recall_limit):
            self.calls += 1
            assert channels == [RecallChannel.KEYWORD] and recall_limit == 2
            assert query.rewritten == "Bob harbor bridge repair"
            return RecallResult(
                batches=[
                    RecallBatch(
                        RecallChannel.KEYWORD,
                        "keyword_l2",
                        [
                            ScoredUnit(
                                "wrong",
                                7,
                                entry_keyword_text="Alice approved harbor bridge repair.",
                            ),
                            ScoredUnit(
                                "new", 5, entry_keyword_text="Bob approved harbor bridge repair."
                            ),
                        ],
                    )
                ]
            )

    domain = Domain()
    new, ranked, selected = with_missing_clause_keyword_entry(
        domain, Scope(), GraphMemParsedQuery(), recall, order, 2, needs
    )
    assert domain.calls == 1 and selected == "new" and ranked[:2] == ["head", "new"]
    limited = limit_recalled(new, 2, ordered_ids=ranked)
    assert {hit.unit_id for batch in limited.batches for hit in batch.candidates} == {"head", "new"}
    assert [hit.unit_id for hit in recall.batches[0].candidates] == ["head", "duplicate"]


def test_single_slot_consensus_requires_overview_and_lexical_support_for_one_event():
    from jiuwen_memory.retrieval.graphmem_relation_plan import retain_single_slot_consensus

    recall = RecallResult(
        batches=[
            RecallBatch(RecallChannel.VECTOR, "vector_l2", [ScoredUnit("detailed_vector", 0.8)]),
            RecallBatch(RecallChannel.VECTOR, "vector_l1", [ScoredUnit("tail", 0.7)]),
            RecallBatch(RecallChannel.KEYWORD, "keyword_l2", [ScoredUnit("tail", 4)]),
            RecallBatch(
                RecallChannel.KEYWORD,
                "keyword_l1",
                [
                    ScoredUnit("tail", 3, entry_keyword_text="John's 2022 Rocky Mountains trip."),
                ],
            ),
        ]
    )
    order = ["detailed_vector", "tail"]
    needs = QueryNeeds(
        mode="temporal",
        entities=("John", "Rocky Mountains"),
        facets=("John's trip to the Rocky Mountains",),
    )
    assert retain_single_slot_consensus(recall, order, needs) == ["tail", "detailed_vector"]
    needs = QueryNeeds(
        mode="temporal", entities=("John",), facets=("take a trip to the Rocky Mountains",)
    )
    assert retain_single_slot_consensus(recall, order, needs) == ["tail", "detailed_vector"]
    recall.batches[-1].candidates[0].entry_keyword_text = "John went on a trip. Rocky Mountains."
    assert retain_single_slot_consensus(recall, order, needs) == order
