"""Event-tail recovery keeps source coverage and the one-repair contract."""

import json

import pytest

from jiuwen_memory.common.graphmem_semantics import GraphSemantics
from jiuwen_memory.construction.hierarchy_composer_impl import recursive_summary
from tests.unit.construction.test_fact_record_summary import records, response, summarizer

pytestmark = pytest.mark.unit


class Scripted:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, **options):
        self.calls.append((messages, options))
        return next(self.responses)


def prefix(fields, events):
    return json.dumps(fields)[:-1] + ', "events": [' + ", ".join(
        json.dumps(event) for event in events) + ', {"scene": "incomplete'


def test_truncated_events_keep_complete_facts_and_repair_only_events():
    fields, source = response(), records()
    kept = fields.pop("events")[0]
    second = {"scene": "bicycle ownership", "entities": [], "sources": [0],
              "status": "ongoing", "time": None}
    summary = summarizer(repair_event_prefix=True, compact_semantic_wire=True)
    summary.llm = Scripted(prefix(fields, [kept]), json.dumps({"events": [second]}))

    out = summary.summarize(source)
    assert out[recursive_summary.EVENT_PREFIX_REPAIRED] is True
    assert out["child_details"] == [fields["details"]["0"]["personal"],
                                    fields["details"]["1"]["content"]]
    assert [e.scene for e in out["semantics"].events] == [kept["scene"], second["scene"]]
    initial, repair = summary.llm.calls
    assert initial[0] == summarizer().messages(source)
    assert repair[1]["response_format"]["json_schema"]["schema"]["required"] == ["events"]
    payload = json.loads(repair[0][1].content)
    assert payload["preserved_events"] == [kept] and payload["source_records"] == source
    assert len(summary.call_input_tokens) == 2


def test_incomplete_facts_still_use_the_existing_full_repair():
    fields, source = response(), records()
    fields.pop("events")
    del fields["details"]["1"]
    summary = summarizer(repair_event_prefix=True)
    summary.llm = Scripted(prefix(fields, [response()["events"][0]]), json.dumps(response()))
    assert len(summary.summarize(source)["child_details"]) == 2
    schema = summary.llm.calls[-1][1]["response_format"]["json_schema"]["schema"]
    assert "details" in schema["required"]


def test_compact_parent_transport_preserves_values_and_source_requests():
    parent = [{"id": 0, "kind": "summary", "text": "Two bicycles.",
               "message_time": None, "features": GraphSemantics().model_view()}]
    baseline = summarizer()
    compact = summarizer(compact_semantic_wire=True)
    old_payload = baseline.messages(parent)[1].content
    new_payload = compact.messages(parent)[1].content
    assert json.loads(old_payload) == json.loads(new_payload)
    assert len(new_payload) < len(old_payload)
    assert compact.messages(records()) == baseline.messages(records())


def test_local_repair_changes_parent_wire_only_for_a_repaired_child():
    parent = [{"id": 0, "kind": "summary", "text": "Two bicycles.",
               "message_time": None, "features": GraphSemantics().model_view()}]
    baseline = summarizer()
    local = summarizer(repair_event_prefix=True)
    assert local.messages(parent) == baseline.messages(parent)
    repaired = [{**parent[0], "_event_prefix_repaired": True}]
    text = local.messages(repaired)[1].content
    baseline_text = baseline.messages(parent)[1].content
    assert json.loads(text) == json.loads(baseline_text)
    assert len(text) < len(baseline_text)
    assert baseline.messages(repaired) == baseline.messages(parent)
