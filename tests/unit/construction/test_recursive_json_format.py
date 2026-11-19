"""Formatting is opt-in, preserves payload semantics, and has its own reuse identity."""

import json
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.construction.hierarchy_composer_impl import recursive_summary as module
from tests.unit.construction.test_fact_record_summary import records, response, summarizer

pytestmark = pytest.mark.unit


def test_disabled_formatting_keeps_existing_request_and_policy():
    original = summarizer()
    explicit = summarizer(compact_json_output=False)
    assert explicit.policy_id == original.policy_id
    assert explicit.messages(records()) == original.messages(records())
    assert explicit.response_format(records()) == original.response_format(records())


@pytest.mark.parametrize("value", [1, "true", None])
def test_format_option_requires_a_boolean(value):
    with pytest.raises(ValidationError, match="compact_json_output must be bool"):
        replace(module.RecursivePolicy(), compact_json_output=value)


@pytest.mark.parametrize("feedback", ["", "invalid JSON", "invalid attribution|empty_records=0"])
def test_formatting_keeps_source_text_schema_and_repair_limit(feedback):
    original, compact = summarizer(), summarizer(compact_json_output=True)
    source = records()
    source[0]["text"] = "First line.\nSecond line with  two spaces."
    before, after = original.messages(source, feedback), compact.messages(source, feedback)
    assert after[1] == before[1], "formatting must not rewrite source evidence"
    assert after[0].content == before[0].content + module.COMPACT_JSON_INSTRUCTION
    assert compact.response_format(source, feedback) == original.response_format(source, feedback)
    assert compact.policy.repair_attempts == original.policy.repair_attempts == 1
    assert compact.input_cost(source, feedback) > original.input_cost(source, feedback)


def test_formatting_does_not_reject_valid_pretty_json_or_change_parsed_facts():
    original, compact = summarizer(), summarizer(compact_json_output=True)
    obj = response()
    obj["details"]["0"]["personal"] += " Note: line one.\nLine two with  two spaces."
    pretty = json.dumps(obj, ensure_ascii=False, indent=2)
    packed = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    assert compact._parse(pretty, records()) == original._parse(pretty, records())
    assert compact._parse(packed, records()) == compact._parse(pretty, records())


def test_actual_format_instruction_participates_in_reuse_identity(monkeypatch):
    original = summarizer()
    compact = summarizer(compact_json_output=True)
    assert compact.policy_id != original.policy_id
    monkeypatch.setattr(module, "COMPACT_JSON_INSTRUCTION", module.COMPACT_JSON_INSTRUCTION + "!")
    assert summarizer(compact_json_output=True).policy_id != compact.policy_id
    assert summarizer().policy_id == original.policy_id


def test_concise_fact_prompt_keeps_payload_and_schema_but_has_separate_reuse_identity():
    source = records()
    original = summarizer()
    candidate = summarizer(concise_fact_prompt=True)
    assert original.policy_id == summarizer(concise_fact_prompt=False).policy_id
    assert candidate.policy_id != original.policy_id
    assert candidate.messages(source)[1] == original.messages(source)[1]
    assert candidate.response_format(source) == original.response_format(source)
    assert candidate.input_cost(source) < original.input_cost(source)
