"""Conservative resolution contracts independent of models and external services."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.construction.conflict_resolver import ConflictResolution as D
from jiuwen_memory.construction.conflict_resolver_impl.strategies import (
    LLMResolver,
    RecencyResolver,
    ResolverChain,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind,context,expected", [
    ("advisory", {}, D.DEFER),
    ("unknown", {}, D.ESCALATE),
    ("factual", {"evidence_complete": False}, D.ESCALATE),
    ("factual", {"multiple_conflicts": True}, D.ESCALATE),
])
def test_chain_rejects_unsafe_arbitration_before_calling_plugins(kind, context, expected):
    plugin = Mock()
    result = ResolverChain([plugin]).resolve(
        None, None, SimpleNamespace(conflict_kind=kind), context
    )
    assert result.decision == expected
    plugin.resolve.assert_not_called()


@pytest.mark.parametrize("changes,expected", [
    ({}, D.PREFER_B),
    ({"source_b": "other"}, D.ESCALATE),
    ({"source_a": ""}, D.ESCALATE),
    ({"explicit_correction": False}, D.DEFER),
    ({"time_b": "2026-01-01"}, D.DEFER),
])
def test_recency_requires_same_source_and_explicit_ordered_correction(changes, expected):
    context = dict(source_a="source", source_b="source", time_a="2026-01-01",
                   time_b="2026-01-02", explicit_correction=True)
    context.update(changes)
    result = RecencyResolver().resolve(
        None, None, SimpleNamespace(conflict_kind="temporal"), context
    )
    assert result.decision == expected


@pytest.mark.parametrize("response,expected", [
    ('{"decision":"prefer_a","reason":"trust me"}', D.ESCALATE),
    ('{"decision":"prefer_b","reason":"trust me"}', D.ESCALATE),
    ('not json', D.ESCALATE),
    ('{"decision":"both_valid","reason":"different conditions"}', D.BOTH_VALID),
])
def test_model_cannot_select_a_winner(response, expected):
    model = Mock()
    model.generate.return_value = response
    result = LLMResolver(model).resolve(
        SimpleNamespace(content="a"), SimpleNamespace(content="b"),
        SimpleNamespace(conditions=["v1"]), {},
    )
    assert result.decision == expected
    assert result.candidate is True
