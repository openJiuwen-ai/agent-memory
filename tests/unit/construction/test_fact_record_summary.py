"""Source fact/discussion separation, attribution and opt-in compatibility."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import jsonschema
import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_semantics import GraphSemantics
from jiuwen_memory.construction.hierarchy_composer_impl import recursive_summary as module

pytestmark = pytest.mark.unit


def summarizer(*, enabled=True, **kwargs):
    policy = module.RecursivePolicy(structured_features=True, fact_first_records=enabled,
                                   max_view_tokens=1400, **kwargs)
    return module.RecursiveSummarizer(None, lambda text: len(text) // 4, policy, "fixture")


def records():
    return [{"id": i, "kind": "source", "message_time": None, "speaker": speaker,
             "text": text} for i, (speaker, text) in enumerate([
                 ("user", "I own two bicycles; I have not bought a third."),
                 ("assistant", "Consider a cargo bicycle for carrying groceries."),
             ])]


def response():
    return {"details": {
        "0": {"personal": "The user owns two bicycles and has not bought a third.",
              "content": ""},
        "1": {"personal": "", "content": "The assistant suggests a cargo bicycle."},
    }, "entities": [], "events": [
        {"scene": "bicycle advice", "entities": [], "sources": [1],
         "status": "suggested", "time": None},
    ], "l0": "Bicycles", "l1": "The user owns two bicycles, not three."}


def test_default_policy_and_wire_request_remain_compatible():
    # Frozen pre-feature request; avoids invalidating all existing summary caches.
    source = [{"id": 0, "kind": "source", "text": "Alice owns two bicycles.",
               "message_time": None}]
    summary = summarizer(enabled=False)
    wire = {"messages": [(m.role, m.content) for m in summary.messages(source)],
            "schema": summary.response_format(source)}
    assert summary.policy_id == "d81deae2f1b29e4107ecd10c345411926c5ba93c17dd08ff173e46231f9ac7d5"
    assert hashlib.sha256(json.dumps(wire, sort_keys=True).encode()).hexdigest() == \
        "19cb2724221ca853f96f012a01e4d3b9b5254dc162daeaa567748ea6542b3f1f"


def test_fact_fields_preserve_attribution_and_direct_child_mapping():
    summary, source, output = summarizer(), records(), response()
    original = deepcopy(source)
    jsonschema.validate(output, summary.response_format(source)["json_schema"]["schema"])
    parsed = summary._parse(json.dumps(output), source)
    assert parsed["child_details"] == [output["details"]["0"]["personal"],
                                       output["details"]["1"]["content"]]
    assert parsed["text"] == "\n".join(parsed["child_details"])
    assert parsed["l1"] == output["l1"], "generic event projection overwrote personal facts"
    assert parsed["semantic_diagnostics"]["personal_records"] == 1
    assert source == original


def test_first_fact_request_unchanged_by_empty_record_repair():
    summary = summarizer()
    wire = {"messages": [(m.role, m.content) for m in summary.messages(records())],
            "schema": summary.response_format(records())}
    assert hashlib.sha256(json.dumps(wire, sort_keys=True).encode()).hexdigest() == \
        "f47e18076f8154970b465b2e770a29ca48e2f301a932f48b90b6c92c99cc8ffa"


def test_empty_record_repair_requires_a_description_without_inventing_facts():
    summary, source, output = summarizer(), records(), response()
    source[0]["text"] = ""
    output["details"]["0"] = {"personal": "", "content": ""}
    feedback = "invalid attribution|empty_records=0"
    schema = summary.response_format(source, feedback)["json_schema"]["schema"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(output, schema)
    output["details"]["0"]["content"] = "The user sent an empty message."
    jsonschema.validate(output, schema)
    parsed = summary._parse(json.dumps(output), source)
    assert parsed["child_details"][0] == output["details"]["0"]["content"]
    assert parsed["semantic_diagnostics"]["personal_records"] == 0
    instruction = summary.messages(source, feedback)[0].content
    assert "do not invent a personal fact" in instruction
    assert "anyOf" not in summary.response_format(source)["json_schema"]["schema"]["properties"][
        "details"]["properties"]["0"]
    assert "minLength" not in schema["properties"]["details"]["properties"]["1"][
        "properties"]["content"]
    assert source[0]["text"] == ""


def test_empty_field_feedback_lists_only_failed_records():
    output = response()
    output["details"]["0"] = {"personal": " ", "content": ""}
    with pytest.raises(ValueError, match="^invalid attribution\\|empty_records=0$"):
        summarizer()._parse(json.dumps(output), records())


@pytest.mark.parametrize("invalid", ["old unstructured value", {"personal": "fact"},
                                    {"personal": 1, "content": "topic"},
                                    {"personal": " ", "content": ""}])
def test_malformed_or_empty_record_is_not_accepted_as_coverage(invalid):
    output = response()
    output["details"]["0"] = invalid
    with pytest.raises(ValueError, match="invalid attrib"):
        summarizer()._parse(json.dumps(output), records())


def test_missing_source_record_still_requires_repair():
    output = response()
    del output["details"]["1"]
    with pytest.raises(ValueError, match="missing input records"):
        summarizer()._parse(json.dumps(output), records())


def test_core_facts_are_not_sliced_to_fit_budget():
    output = response()
    output["details"]["0"]["personal"] = "A distinct attributed fact. " * 300
    with pytest.raises(ValueError, match="output view budget"):
        summarizer()._parse(json.dumps(output), records())


def test_upper_summary_request_and_semantics_are_unchanged():
    source = [{"id": 0, "kind": "summary", "text": "The user owns two bicycles.",
               "message_time": None, "features": GraphSemantics((), ()).model_view()}]
    old, new = summarizer(enabled=False), summarizer()
    assert old.messages(source) == new.messages(source)
    assert old.response_format(source) == new.response_format(source)
    output = json.dumps({"details": {"0": source[0]["text"]}, "l0": "Bicycles",
                         "l1": "Bicycle ownership"})
    assert old._parse(output, source) == new._parse(output, source)


def test_fact_policy_requires_features_and_invalidates_reuse(monkeypatch):
    with pytest.raises(ValidationError, match="requires structured_features"):
        module.RecursivePolicy(fact_first_records=True)
    with pytest.raises(ValidationError, match="must be bool"):
        replace(summarizer().policy, fact_first_records=1)
    old, first = summarizer(enabled=False), summarizer()
    monkeypatch.setattr(module, "FACT_RECORD_INSTRUCTION", module.FACT_RECORD_INSTRUCTION + "!")
    assert first.policy_id != summarizer().policy_id
    assert first.policy_id != old.policy_id
    assert old.policy_id == summarizer(enabled=False).policy_id


def test_single_bounded_repair_then_parses_same_output_contract():
    class Model:
        calls = 0

        def chat(self, messages, **options):
            self.calls += 1
            output = response()
            if self.calls == 1:
                output["details"]["0"] = {"personal": "", "content": ""}
            return json.dumps(output)

    summary, model = summarizer(), Model()
    summary.llm = model
    parsed = summary.summarize(records())
    assert "two bicycles" in parsed["text"]
    assert model.calls == len(summary.call_input_tokens) == 2
    assert max(summary.call_input_tokens) <= summary.policy.max_input_tokens
