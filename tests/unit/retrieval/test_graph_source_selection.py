"""Raw vector refinement keeps source authorization, filters and shared budgets."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.type_def import (
    HierarchyRole,
    RecallBatch,
    RecallChannel,
    RecallResult,
    ScoredMemoryUnit,
)
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graph_source_selection import personal_source_order
from jiuwen_memory.retrieval.graphmem_request_reads import _RequestReads
from tests.unit.construction.graphmem_fixtures import HOME, query, tree, world

pytestmark = pytest.mark.unit












def role_sources():
    speakers = {"a0": "assistant", "unknown": None, "a1": "ASSISTANT", "u0": " user ",
                "u1": "user", "u2": "user", "a2": "assistant", "u3": "user", "u4": "user"}
    sources = {}
    for key, speaker in speakers.items():
        _, unit = tree("p", key)
        unit.user_metadata["speaker"] = speaker
        sources[key] = ScoredMemoryUnit(unit, 1)
    return list(speakers), sources


def test_personal_role_balance_preserves_every_source_and_unknown_position():
    keys, sources = role_sources()
    ordered = personal_source_order(keys, sources, "Where did I go?")
    assert ordered == ["u0", "unknown", "u1", "u2", "a0", "u3", "u4", "a1", "a2"]
    assert set(ordered) == set(keys) and len(ordered) == len(keys)
    assert keys[0] == "a0", "source order input was modified in place"


@pytest.mark.parametrize("question", [
    "Where did Alice go?", "What did you recommend for my trip?",
    "What did you say about my plan?", "What was the assistant's advice for me?",
])
def test_role_balance_does_not_reorder_nonpersonal_or_assistant_history(question):
    keys, sources = role_sources()
    assert personal_source_order(keys, sources, question) == keys


def test_role_balance_does_not_infer_roles_for_named_or_invalid_speakers():
    keys, sources = role_sources()
    for key in keys:
        sources[key].unit.user_metadata["speaker"] = "Alice" if key.startswith("u") else ["user"]
    assert personal_source_order(keys, sources, "Where did I go?") == keys


def test_progressive_stages_share_vector_scope_budget_and_query_cache():
    domain, _, _, _ = world([])
    calls = []

    def recall(scope, q, **kwargs):
        calls.append(scope)
        return RecallResult([RecallBatch(RecallChannel.VECTOR, "vector", [])])

    domain.recall = recall
    cache = _RequestReads(domain, GraphNavigationPolicy(source_vector_scopes=1), [])
    parsed = replace(query(), vector=[1.0], hierarchy_role=HierarchyRole.SNAPSHOT)
    first_scope = replace(HOME, session="s1")
    options = {"channels": [RecallChannel.VECTOR], "recall_limit": 128}
    first = cache.recall(first_scope, parsed, **options)
    assert cache.recall(first_scope, replace(parsed), **options) is first
    assert not cache.recall(replace(HOME, session="s2"), parsed, **options).batches
    assert calls == [first_scope] and cache.limited and cache.reads == 0
