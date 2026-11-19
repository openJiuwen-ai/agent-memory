"""Joint metadata can finish within the configured wire cap without a full rewrite."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_semantics import GraphSemantics
from jiuwen_memory.construction.hierarchy_composer_impl import recursive_summary as module
from tests.unit.construction.test_fact_record_summary import records, response, summarizer

pytestmark = pytest.mark.unit


class BoundedMetadataModel:
    def __init__(self, output=None):
        self.calls = []
        self.output = response() if output is None else output

    def chat(self, messages, **options):
        self.calls.append((deepcopy(messages), deepcopy(options)))
        # This deterministic model represents a metadata-heavy response that
        # needs more wire space than its normalized stored summary.
        if options["max_tokens"] < 2000:
            return '{"details":{"0":{"personal":"The user owns two'
        return json.dumps(self.output)


def test_full_wire_budget_avoids_a_rewrite_without_changing_facts_or_source_request():
    old = summarizer(max_output_tokens=3072)
    new = summarizer(max_output_tokens=3072, structured_output_headroom=True)
    old.llm, new.llm = BoundedMetadataModel(), BoundedMetadataModel()
    source = records()
    saved_source = deepcopy(source)
    assert old.summarize(source) == new.summarize(source)
    assert len(old.llm.calls) == 2 and len(new.llm.calls) == 1
    assert old.llm.calls[0][0] == new.llm.calls[0][0]
    assert old.llm.calls[0][1]["response_format"] == new.llm.calls[0][1]["response_format"]
    assert new.llm.calls[0][1]["max_tokens"] == 3072
    assert old.input_cost(source) == new.input_cost(source)
    assert source == saved_source
    assert len(new.call_input_tokens) == 1, "Every attempted call must remain accounted"


def test_existing_output_max_is_not_raised_and_failure_still_has_one_repair():
    summary = summarizer(max_output_tokens=1500, structured_output_headroom=True)
    summary.llm = BoundedMetadataModel()
    with pytest.raises(module.SummaryOutputIncomplete):
        summary.summarize(records())
    assert len(summary.llm.calls) == len(summary.call_input_tokens) == 2
    assert all(options["max_tokens"] == 1500 for _, options in summary.llm.calls)


def test_more_wire_space_cannot_admit_an_oversized_factual_view():
    output = response()
    output["details"]["0"]["personal"] = "A distinct attributed fact. " * 300
    summary = summarizer(max_output_tokens=3072, structured_output_headroom=True)
    summary.llm = BoundedMetadataModel(output)
    with pytest.raises(module.SummaryViewOverflow):
        summary.summarize(records())
    assert len(summary.llm.calls) == 2


def test_parent_merge_with_inherited_features_keeps_the_existing_wire_request():
    source = [{"id": 0, "kind": "summary", "text": "The user owns two bicycles.",
               "message_time": None, "features": GraphSemantics((), ()).model_view()}]
    output = {"details": {"0": source[0]["text"]}, "l0": "Bicycles", "l1": "Bicycles owned"}
    old, new = summarizer(max_output_tokens=3072), summarizer(
        max_output_tokens=3072, structured_output_headroom=True)

    class ParentModel(BoundedMetadataModel):
        def chat(self, messages, **options):
            self.calls.append((deepcopy(messages), deepcopy(options)))
            return json.dumps(output)

    old.llm, new.llm = ParentModel(), ParentModel()
    assert old.summarize(source) == new.summarize(source)
    assert old.llm.calls == new.llm.calls


def test_new_budget_policy_has_a_distinct_reuse_identity_without_prompt_edits():
    old, new = summarizer(), summarizer(structured_output_headroom=True)
    assert old.policy_id != new.policy_id
    assert old.messages(records()) == new.messages(records())
    assert old.response_format(records()) == new.response_format(records())
    assert summarizer(structured_output_headroom=False).policy_id == old.policy_id


@pytest.mark.parametrize("value", [1, "true", None])
def test_headroom_requires_a_boolean(value):
    with pytest.raises(ValidationError, match="structured_output_headroom must be bool"):
        replace(module.RecursivePolicy(), structured_output_headroom=value)


def test_headroom_requires_joint_structured_features():
    with pytest.raises(ValidationError, match="requires structured_features"):
        module.RecursivePolicy(structured_output_headroom=True)
