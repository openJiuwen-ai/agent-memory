"""Source-grounded dates and bounded semantic features, independent of model fluency."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import jsonschema
import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graph_memory import memory_fingerprint
from jiuwen_memory.common.graphmem_semantics import (
    FEATURES_KEY,
    EntityLabel,
    EventTime,
    GraphSemantics,
    interval_gap,
    read_semantics,
)
from jiuwen_memory.common.type_def import HierarchyRole, Segment
from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import (
    RecursivePolicy,
    RecursiveSummarizer,
    records_for,
)
from jiuwen_memory.construction.hierarchy_composer_impl.semantic_summary import (
    grounded_semantics,
    inherited_semantics,
    parse_semantics,
    semantic_schema,
)
from tests.unit.construction.hierarchy_fixtures import make_leaf

pytestmark = pytest.mark.unit


def record(text="Alice bought a bike yesterday.", date="2023-05-09T10:00:00+00:00"):
    return {"id": 0, "kind": "source", "text": text, "message_time": date}


def extraction(*, start="2023-05-08", end="2023-05-09", expression="yesterday"):
    return {
        "entities": [{"name": "Alice", "kind": "person", "aliases": [], "qualifier": ""}],
        "events": [
            {
                "scene": "buy a bike",
                "entities": ["Alice"],
                "status": "completed",
                "sources": [0],
                "time": {
                    "source": 0,
                    "expression": expression,
                    "start": start,
                    "end": end,
                    "precision": "day",
                    "kind": "occurrence",
                },
            }
        ],
    }


@pytest.mark.parametrize("structured", [False, True])
def test_native_speaker_metadata_survives_summary_attribution(structured):
    import json

    leaf = make_leaf("speaker")
    leaf.segments = [Segment(content="I bought a bike yesterday.")]
    leaf.user_metadata["speaker"] = "Alice"
    before = deepcopy(leaf)
    records = records_for([leaf])
    policy = RecursivePolicy(structured_features=structured, max_view_tokens=1400)
    summarizer = RecursiveSummarizer(None, lambda text: len(text) // 4, policy, "fixture")
    sent = json.loads(summarizer.messages(records)[1].content)
    assert sent[0]["speaker"] == "Alice" and sent[0]["text"] == leaf.content
    response = {
        "details": {"0": "Alice bought a bike yesterday."},
        "entities": ["Alice"],
        "l0": "Bike",
        "l1": "Alice's bike",
    }
    if structured:
        response.update(extraction(start="2026-09-09", end="2026-09-10"))
    parsed = summarizer._parse(json.dumps(response), records)
    assert parsed["entities"] == ["Alice"]
    assert leaf == before, "summary projection changed the source"


def test_existing_attributed_text_keeps_the_same_prompt_without_duplicate_speaker():
    leaf = make_leaf("speaker")
    leaf.segments = [Segment(content="[Session s1 | 2023-05-09] Alice: I bought a bike.")]
    without = records_for([leaf])
    leaf.user_metadata["speaker"] = "Alice"
    assert records_for([leaf]) == without


@pytest.mark.parametrize("speaker", [None, "", "  ", 3, ["Alice"], "a" * 101, "Alice\nBob"])
def test_unusable_speaker_metadata_does_not_enter_model_input(speaker):
    leaf = make_leaf("speaker")
    leaf.user_metadata["speaker"] = speaker
    assert "speaker" not in records_for([leaf])[0]


def test_parent_metadata_is_not_a_source_speaker():
    parent = make_leaf("summary")
    parent.hierarchy.role = HierarchyRole.TIME_SPAN
    parent.user_metadata["speaker"] = "Alice"
    assert "speaker" not in records_for([parent])[0]


def test_speaker_date_is_not_grounding_for_an_event_date():
    source = {**record("Alice bought a bike."), "speaker": "May 8"}
    with pytest.raises(ValueError, match="not grounded"):
        parse_semantics(extraction(expression="May 8"), [source])


def test_relative_date_uses_source_anchor_and_roundtrips():
    features = parse_semantics(extraction(), [record()])
    event = features.events[0]
    assert event.time.start == "2023-05-08T00:00:00+00:00"
    assert event.time.anchor == "2023-05-09T10:00:00+00:00"
    unit = make_leaf("x")
    before = memory_fingerprint(unit)
    unit.system_metadata[FEATURES_KEY] = features.encode()
    assert read_semantics(unit) == features
    assert memory_fingerprint(unit) != before


def test_source_timezone_determines_calendar_day():
    features = parse_semantics(extraction(), [record(date="2023-05-09T01:00:00+08:00")])
    assert features.events[0].time.start == "2023-05-07T16:00:00+00:00"
    assert features.events[0].time.end == "2023-05-08T16:00:00+00:00"


@pytest.mark.parametrize(
    "expression,start,end",
    [
        ("yesterday", "2023-05-07", "2023-05-08"),
        ("yesterday", "2023-02-30", "2023-03-01"),
        ("recently", "2023-05-08", "2023-05-09"),
    ],
)
def test_invalid_or_overprecise_time_requires_correction(expression, start, end):
    with pytest.raises(ValueError):
        parse_semantics(
            extraction(start=start, end=end, expression=expression),
            [record(f"Alice bought a bike {expression}.")],
        )


def test_missing_anchor_cannot_resolve_relative_dates():
    with pytest.raises(ValueError, match="anchor"):
        parse_semantics(extraction(), [record(date=None)])


@pytest.mark.parametrize("expression", ["Monday", "May 8", "星期一", "5月8日"])
def test_weekday_and_yearless_calendar_dates_require_source_anchor(expression):
    data = extraction(expression=expression)
    with pytest.raises(ValueError, match="anchor"):
        parse_semantics(data, [record(f"Alice bought a bike {expression}.", date=None)])
    features = parse_semantics(data, [record(f"Alice bought a bike {expression}.")])
    assert features.events[0].time.resolution == "source_anchored"


def test_parent_inherits_time_without_resolving_it_again():
    child = parse_semantics(extraction(), [record()])
    parent_input = {
        "id": 0,
        "kind": "summary",
        "text": "Alice bought a bike.",
        "message_time": None,
        "features": child.model_view(),
    }
    data = extraction()
    data["events"][0]["time"] = {"source": 0, "ref": child.model_view()["events"][0]["id"]}
    parent = parse_semantics(data, [parent_input])
    assert parent.events[0].time == child.events[0].time
    data["events"][0]["time"] = extraction()["events"][0]["time"]
    with pytest.raises(ValueError, match="inherited"):
        parse_semantics(data, [parent_input])


@pytest.mark.parametrize("expression", ["3 bikes", "5 books", "42 items"])
def test_plain_quantities_are_not_calendar_expressions(expression):
    with pytest.raises(ValueError, match="calendar date"):
        parse_semantics(extraction(expression=expression), [record(f"Alice bought {expression}.")])


@pytest.mark.parametrize("expression", ["two days later", "two weeks earlier", "5/8"])
def test_later_and_yearless_numeric_dates_require_source_clock(expression):
    with pytest.raises(ValueError, match="anchor"):
        parse_semantics(
            extraction(expression=expression),
            [record(f"Alice bought a bike {expression}.", date=None)],
        )


def test_earlier_day_resolution_is_checked_against_source_clock():
    data = extraction(expression="two days earlier", start="2023-05-07", end="2023-05-08")
    features = parse_semantics(data, [record("Alice bought a bike two days earlier.")])
    assert features.events[0].time.start == "2023-05-07T00:00:00+00:00"
    data["events"][0]["time"].update(start="2023-05-06", end="2023-05-07")
    with pytest.raises(ValueError, match="source anchor"):
        parse_semantics(data, [record("Alice bought a bike two days earlier.")])


def test_source_schema_rejects_timestamp_as_inherited_event_reference():
    schema = {"type": "object", "properties": semantic_schema([record()])}
    data = extraction()
    jsonschema.validate(data, schema)
    data["events"][0]["time"] = {"source": 0, "ref": "2023-05-08T00:00:00Z"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)


def test_mixed_schema_binds_inherited_time_to_actual_child_and_event():
    child = parse_semantics(extraction(), [record()])
    parent = {"id": 1, "kind": "summary", "features": child.model_view()}
    schema = {"type": "object", "properties": semantic_schema([record(), parent])}
    data = extraction()
    data["events"][0]["sources"] = [1]
    data["events"][0]["time"] = {"source": 1, "ref": "E0"}
    jsonschema.validate(data, schema)
    for invalid in (
        {"source": 0, "ref": "E0"},
        {"source": 1, "ref": "E9"},
        {**extraction()["events"][0]["time"], "source": 1},
    ):
        data["events"][0]["time"] = invalid
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data, schema)


@pytest.mark.parametrize("instruction", ["SEMANTIC_INSTRUCTION", "PARENT_BUDGET_REPAIR"])
def test_prompt_change_invalidates_recursive_reuse_fingerprint(monkeypatch, instruction):
    from jiuwen_memory.construction.hierarchy_composer_impl import recursive_summary as module

    policy = module.RecursivePolicy(structured_features=True)
    first = module.RecursiveSummarizer(None, lambda text: len(text) // 4, policy, "fixture")
    monkeypatch.setattr(module, instruction, getattr(module, instruction) + "\nTest.")
    second = module.RecursiveSummarizer(None, lambda text: len(text) // 4, policy, "fixture")
    assert first.policy_id != second.policy_id


@pytest.mark.parametrize("failure_kind", ["incomplete", "overflow"])
@pytest.mark.parametrize("repair_attempts", [0, 1])
def test_parent_repair_shares_budget_without_extra_attempts(failure_kind, repair_attempts):
    import json

    class ParentModel:
        def __init__(self):
            self.instructions = []

        def chat(self, messages, **options):
            self.instructions.append(messages[0].content)
            if len(self.instructions) == 1:
                if failure_kind == "incomplete":
                    return '{"details":{"0":"unfinished'
                return json.dumps(
                    {
                        "details": {"0": "x" * 20000, "1": "Other topic"},
                        "l0": "Topics",
                        "l1": "Two topics",
                    }
                )
            assert "TOTAL across all children, not per child" in messages[0].content
            return json.dumps(
                {
                    "details": {"0": "Alice bought a bike.", "1": "Bob sold a car."},
                    "l0": "Transport",
                    "l1": "Distinct purchases and sales",
                }
            )

    records = [
        {
            "id": i,
            "kind": "summary",
            "text": text,
            "message_time": None,
            "features": GraphSemantics((), ()).model_view(),
        }
        for i, text in enumerate(["Alice bought a bike.", "Bob sold a car."])
    ]
    original = deepcopy(records)
    model = ParentModel()
    policy = RecursivePolicy(structured_features=True, repair_attempts=repair_attempts)
    summary = RecursiveSummarizer(model, lambda text: len(text) // 4, policy, "fixture")
    assert summary.fits(records)
    if repair_attempts:
        result = summary.summarize(records)
        assert "Alice" in result["text"] and "Bob" in result["text"]
    else:
        with pytest.raises(ValidationError):
            summary.summarize(records)
    assert len(model.instructions) == repair_attempts + 1
    assert "TOTAL across all children" not in model.instructions[0]
    assert records == original
    assert max(summary.call_input_tokens) <= policy.max_input_tokens


def test_parent_model_view_omits_only_program_inherited_features():
    import json

    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import (
        RecursivePolicy,
        RecursiveSummarizer,
    )

    features = parse_semantics(extraction(), [record()])
    records = [
        {
            "id": 0,
            "kind": "summary",
            "text": "Alice bought a bike.",
            "message_time": None,
            "features": features.model_view(),
        }
    ]
    original = deepcopy(records)
    policy = RecursivePolicy(structured_features=True, max_view_tokens=1400)
    plain = RecursiveSummarizer(None, lambda text: len(text) // 4, policy, "fixture")
    compact = RecursiveSummarizer(
        None, lambda text: len(text) // 4, replace(policy, compact_parent_records=True), "fixture"
    )
    sent = json.loads(compact.messages(records)[1].content)
    assert sent == [{k: v for k, v in records[0].items() if k != "features"}]
    assert compact.input_cost(records) < plain.input_cost(records)
    assert compact.messages([record()]) == plain.messages([record()])
    response = json.dumps({"details": {"0": records[0]["text"]}, "l0": "Bike", "l1": "Use"})
    parsed = compact._parse(response, records)
    assert parsed["semantics"].events[0].time == features.events[0].time
    assert parsed["semantics"].entities == features.entities
    assert records == original and compact.policy_id != plain.policy_id


def test_shared_clock_prompt_is_lossless_and_uses_its_actual_size():
    import json

    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import (
        RecursivePolicy,
        RecursiveSummarizer,
    )

    base = datetime.fromisoformat("2023-05-09T10:00:00+08:00")
    records = [
        {
            **record(f"Alice owns item {i}."),
            "id": i,
            "message_time": (base + timedelta(microseconds=i)).isoformat(),
        }
        for i in range(20)
    ]
    original = deepcopy(records)
    policy = RecursivePolicy(structured_features=True)
    plain = RecursiveSummarizer(None, lambda text: len(text) // 4, policy, "fixture")
    compact = RecursiveSummarizer(
        None, lambda text: len(text) // 4, replace(policy, compact_source_records=True), "fixture"
    )
    payload = json.loads(compact.messages(records)[1].content)
    assert isinstance(payload, dict) and len(payload["records"]) == len(records)
    restored = [
        {
            **{k: v for k, v in r.items() if k != "offset_us"},
            "kind": payload["kind"],
            "message_time": (base + timedelta(microseconds=r["offset_us"])).isoformat(),
        }
        for r in payload["records"]
    ]
    assert restored == records == original
    assert compact.input_cost(records) < plain.input_cost(records)
    assert compact.policy_id != plain.policy_id
    assert isinstance(json.loads(plain.messages(records)[1].content), list)


@pytest.mark.parametrize(
    "changed",
    [
        {"message_time": None},
        {"message_time": "2023-05-09T10:00:02+00:00"},
        {"message_time": "2023-05-10T10:00:00+00:00"},
        {"kind": "summary"},
        {"offset_us": 123},
    ],
)
def test_general_source_clocks_and_mixed_inputs_keep_explicit_records(changed):
    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import _source_batch

    records = [record(), {**record(), "id": 1, **changed}]
    original = deepcopy(records)
    assert _source_batch(records) is records
    assert records == original


def test_shared_clock_does_not_cross_a_calendar_second_boundary():
    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import _source_batch

    records = [
        {**record(), "message_time": "2023-05-09T10:00:00.900000+00:00"},
        {**record(), "id": 1, "message_time": "2023-05-09T10:00:01.100000+00:00"},
    ]
    assert _source_batch(records) is records


def test_dense_structured_json_repair_can_use_full_configured_output_budget():
    import json

    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import (
        RecursivePolicy,
        RecursiveSummarizer,
    )

    class Model:
        limits = []

        def chat(self, messages, **options):
            self.limits.append(options["max_tokens"])
            if len(self.limits) == 1:
                return '{"details":{"0":"A long record", "1":"An unfinished record'
            return json.dumps(
                {
                    "details": {"0": "Alice bought a bike.", "1": "Alice rides."},
                    "entities": [],
                    "events": [],
                    "l0": "Bike",
                    "l1": "Bike use",
                }
            )

    model = Model()
    policy = RecursivePolicy(structured_features=True, max_view_tokens=1400, max_output_tokens=3072)
    summarizer = RecursiveSummarizer(model, lambda text: len(text) // 4, policy, "fixture")
    result = summarizer.summarize([record(), {**record("Alice rides."), "id": 1}])
    assert "Alice rides." in result["text"]
    assert len(model.limits) == 2 and model.limits[0] < model.limits[1] == 3072


def test_no_event_time_inferred_from_session_header():
    data = extraction(expression="2023-05-08")
    source = record("[Session s1 | 2023-05-08] Alice bought a bike.")
    with pytest.raises(ValueError, match="grounded"):
        parse_semantics(data, [source])
    data["events"][0]["time"] = None
    result = parse_semantics(data, [source])
    assert not result.events[0].time.known


def test_entity_labels_are_grounded_deduplicated_and_homonyms_not_merged():
    data = extraction()
    data["entities"].append(deepcopy(data["entities"][0]))
    result = parse_semantics(data, [record()])
    assert len(result.entities) == 1
    assert (
        EntityLabel("John", "person", qualifier="neighbor").key
        != EntityLabel("John", "person", qualifier="brother").key
    )
    data["entities"][0]["aliases"] = ["unmentioned person"]
    with pytest.raises(ValueError, match="ungrounded"):
        parse_semantics(data, [record()])


def test_states_remain_distinct_and_no_parent_min_max_interval_is_invented():
    data = extraction()
    changed = deepcopy(data["events"][0])
    changed["status"] = "cancelled"
    changed["time"] = {
        **changed["time"],
        "expression": "2023-06-10",
        "start": "2023-06-10",
        "end": "2023-06-11",
    }
    data["events"].append(changed)
    features = parse_semantics(
        data, [record("Alice planned a bike yesterday, cancelled 2023-06-10.")]
    )
    assert len(features.events) == 2
    assert interval_gap(features.events[0].time, features.events[1].time) == 32 * 86400
    assert {e.status for e in features.events} == {"completed", "cancelled"}


def test_unknown_or_recurring_time_is_not_a_nearest_occurrence():
    known = EventTime("2023-05-08T00:00:00Z", "2023-05-09T00:00:00Z", "day", "occurrence")
    assert interval_gap(known, EventTime()) == float("inf")
    assert interval_gap(known, replace(known, kind="recurrence")) == float("inf")


def test_corrupt_persisted_semantics_is_not_silently_accepted():
    unit = make_leaf("x")
    unit.system_metadata[FEATURES_KEY] = '{"version":1,"entities":[],"events":[{}]}'
    with pytest.raises(ValidationError):
        read_semantics(unit)
    unit.system_metadata[FEATURES_KEY] = GraphSemantics().encode()
    assert read_semantics(unit) == GraphSemantics()


def test_optional_bad_annotations_are_dropped_and_audited_without_inventing_replacements():
    data = extraction(start="2023-05-01", end="2023-05-02")
    data["entities"].append({"name": "Unknown", "kind": "person", "aliases": [], "qualifier": ""})
    data["events"][0]["entities"].append("Unknown")
    features, counters = grounded_semantics(data, [record()])
    assert [e.name for e in features.entities] == ["Alice"]
    assert len(features.events) == 1 and features.events[0].time == EventTime()
    assert counters == dict(
        dropped_entities=1, dropped_event_entities=1, unknown_times=1, dropped_events=0
    )


def test_upper_features_are_bounded_and_do_not_lose_resolved_dates_on_rephrasing():
    child = parse_semantics(extraction(), [record()])
    parents = [
        {
            "id": i,
            "kind": "summary",
            "text": "Bike purchase.",
            "message_time": None,
            "features": child.model_view(),
        }
        for i in range(10)
    ]
    inherited = inherited_semantics(parents)
    assert len(inherited.events) == 1 and len(inherited.entities) == 1
    assert inherited.events[0].time == child.events[0].time
    assert inherited.events[0].sources == tuple(range(10))


def test_annotation_overflow_prunes_whole_optional_entries_and_retains_core_text():
    from jiuwen_memory.common.graphmem_semantics import SceneEvent
    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import (
        RecursivePolicy,
        RecursiveSummarizer,
    )

    child = parse_semantics(extraction(), [record()])
    events = [child.events[0]] + [
        SceneEvent(
            "undated topic " * 12 + str(i),
            child.events[0].entity_keys,
            "reported",
            EventTime(),
            (0,),
        )
        for i in range(7)
    ]
    features = replace(child, events=tuple(events))
    records = [
        {
            "id": 0,
            "kind": "summary",
            "text": "Alice bought a bike yesterday.",
            "message_time": None,
            "features": features.model_view(),
        }
    ]
    summarizer = RecursiveSummarizer(
        None,
        lambda s: len(s) // 4,
        RecursivePolicy(structured_features=True, max_view_tokens=400),
        tokenizer_id="fixture",
    )
    import json

    parsed = summarizer._parse(
        json.dumps({"details": {"0": "Alice bought a bike."}, "l0": "Bike", "l1": "Bike purchase"}),
        records,
    )
    assert parsed["text"] == "Alice bought a bike."
    assert parsed["semantic_diagnostics"]["budget_dropped_events"] > 0
    assert parsed["semantics"].events[0].time == child.events[0].time
    schema = summarizer.response_format(records)["json_schema"]["schema"]
    assert set(schema["required"]) == {"details", "l0", "l1"}


@pytest.mark.parametrize(
    "start,end,expression,source,expected",
    [
        (
            "2023-05-09",
            "2023-05-10",
            "last Friday",
            "Alice bought a bike last Friday.",
            "2023-05-05T00:00:00+00:00",
        ),
        (
            "2023-05-09",
            "2023-05-09T24:00:00Z",
            "today",
            "Alice bought a bike today.",
            "2023-05-09T00:00:00+00:00",
        ),
        ("2023-05-09", "2023-05-10", "yesterday", "Alice bought a bike.", None),
        (
            "2023-05-09",
            "2023-05-10",
            "two days earlier",
            "Alice bought a bike two days earlier.",
            None,
        ),
    ],
)
def test_grounded_relative_day_repair_is_bounded_to_an_explicit_source(
    start, end, expression, source, expected
):
    payload = extraction(start=start, end=end, expression=expression)
    baseline, diagnostics = grounded_semantics(payload, [record(source)])
    assert not baseline.events[0].time.known
    assert diagnostics["unknown_times"] == 1

    candidate, diagnostics = grounded_semantics(payload, [record(source)], repair_source_day=True)
    event = candidate.events[0]
    assert event.time.start == (expected or "")
    assert event.sources == baseline.events[0].sources
    assert diagnostics.get("repaired_source_days", 0) == int(expected is not None)


def test_source_day_repair_changes_the_policy_identity_without_changing_the_first_prompt():
    base = RecursivePolicy(structured_features=True)
    def count(text):
        return len(text) // 4
    ordinary = RecursiveSummarizer(None, count, base, "fixture")
    repaired = RecursiveSummarizer(None, count, replace(base, repair_source_day=True), "fixture")

    assert ordinary.messages([record()]) == repaired.messages([record()])
    assert ordinary.response_format([record()]) == repaired.response_format([record()])
    assert ordinary.policy_id != repaired.policy_id


def test_compact_source_day_preserves_events_and_reaches_the_next_merge():
    import json

    from jiuwen_memory.construction.hierarchy_composer_impl.recursive_summary import (
        SOURCE_DAY_COMPACT_WIRE,
    )

    source = record("Alice bought a bike yesterday.")
    obj = {
        "details": {"0": "Alice bought a bike yesterday."},
        "l0": "Bike",
        "l1": "Purchase",
        **extraction(start="2023-05-09", end="2023-05-10"),
    }
    policy = RecursivePolicy(structured_features=True, max_input_tokens=24000, max_view_tokens=1400)
    reference = RecursiveSummarizer(None, len, policy, "fixture")
    repaired = RecursiveSummarizer(None, len, replace(policy, repair_source_day=True), "fixture")
    raw_before = reference._parse(json.dumps(obj), [source])
    raw_after = repaired._parse(json.dumps(obj), [source])
    counted = {k: raw_after[k] for k in ("text", "entities", "l0", "l1", "features")}
    compact_limit = len(json.dumps(counted, ensure_ascii=False, separators=(",", ":")))
    tight = RecursiveSummarizer(
        None, len, replace(policy, repair_source_day=True, max_view_tokens=compact_limit), "fixture"
    )
    retained = tight._parse(json.dumps(obj), [source])

    assert retained[SOURCE_DAY_COMPACT_WIRE]
    assert retained["semantics"] == raw_after["semantics"]
    assert retained["semantics"].entities == raw_before["semantics"].entities
    assert (
        replace(retained["semantics"].events[0], time=EventTime())
        == raw_before["semantics"].events[0]
    )
    tagged = [tight._view_record(0, retained)]
    ordinary = [tight._view_record(0, raw_after)]
    assert json.loads(tight.messages(tagged)[1].content) == ordinary
    assert len(tight.messages(tagged)[1].content) < len(tight.messages(ordinary)[1].content)
    assert reference.messages(tagged) == reference.messages(ordinary)


@pytest.mark.parametrize(
    ("phrase", "start", "end", "reason"),
    [
        ("the other day", "2023-05-08", "2023-05-09", "overprecise_vague_date"),
        ("last weekend", "2023-05-07", "2023-05-08", "overprecise_weekend"),
        ("May 12", "2023-05-12", "2023-05-13", "completed_event_in_future"),
        ("last month", "2023-04-14", "2023-04-15", "overprecise_month"),
        ("yesterday", "2023-05-08", "2023-05-09", None),
    ],
)
def test_declared_time_precision_is_checked_without_losing_the_source_event(
    phrase, start, end, reason
):
    payload = extraction(expression=phrase, start=start, end=end)
    sources = [record(f"Alice bought a bike {phrase}.")]
    baseline, _ = grounded_semantics(payload, sources)
    protected, diagnostic = grounded_semantics(
        payload,
        sources,
        validate_explicit_time_precision=True,
    )
    assert baseline.events[0].time.known
    event = protected.events[0]
    assert [event.scene, event.status, event.sources, event.entity_keys] == [
        baseline.events[0].scene,
        baseline.events[0].status,
        (0,),
        baseline.events[0].entity_keys,
    ]
    assert event.time.known is (reason is None)
    if reason is not None:
        assert diagnostic[reason] == diagnostic["unknown_times"] == 1


def test_future_planned_event_keeps_its_fully_grounded_date():
    payload = extraction(expression="tomorrow", start="2023-05-10", end="2023-05-11")
    payload["events"][0]["status"] = "planned"
    features, _ = grounded_semantics(
        payload,
        [record("Alice plans to buy a bike tomorrow.")],
        validate_explicit_time_precision=True,
    )
    assert features.events[0].time.known
