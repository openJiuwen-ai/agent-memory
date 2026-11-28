"""Frozen sync plans reject changes between preview and execute."""

from __future__ import annotations

import pytest

from jiuwen_memory_entry.local.sync.plan import PlanChanged, SyncPlan, diff_plan

pytestmark = pytest.mark.unit


def test_plan_diff_detects_content_and_membership_changes():
    plan = SyncPlan(
        install_id="i", grant_id="g", grant_version=1, target_space="s",
        writes={"a": "1", "b": "2"}, deletes=frozenset({"c"}),
    )
    with pytest.raises(PlanChanged) as raised:
        diff_plan(plan, {"a": "1", "b": "3"}, frozenset())
    assert raised.value.changed == ("b",)
    assert "c" in raised.value.removed


def test_plan_diff_accepts_identical_scan():
    plan = SyncPlan(
        install_id="i", grant_id="g", grant_version=1, target_space="s",
        writes={"a": "1"},
    )
    diff_plan(plan, {"a": "1"}, frozenset())
