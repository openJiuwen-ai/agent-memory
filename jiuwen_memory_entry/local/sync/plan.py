"""Frozen dry-run plans for sync apply.

The scan engine remains the source of truth for filtering and digesting.  This
module only freezes what a preview observed so execute can reject a changed
host before writing anything.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SyncPlan:
    plan_id: str = ""
    install_id: str = ""
    grant_id: str = ""
    grant_version: int = 0
    target_space: str = ""
    writes: dict[str, str] = field(default_factory=dict)
    deletes: frozenset[str] = frozenset()
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.plan_id:
            object.__setattr__(self, "plan_id", secrets.token_hex(16))
        if not self.created_at:
            object.__setattr__(self, "created_at", time.time())


class PlanChanged(Exception):
    """Raised when an execute-time re-scan no longer matches the frozen plan."""

    def __init__(
        self,
        diff=None,
        *,
        added: list[str] | None = None,
        changed: list[str] | None = None,
        removed: list[str] | None = None,
    ) -> None:
        self.diff = diff
        self.added = tuple(added or ())
        self.changed = tuple(changed or ())
        self.removed = tuple(removed or ())
        detail = (
            f"added={len(self.added)} changed={len(self.changed)} "
            f"removed={len(self.removed)}"
            if not diff
            else str(diff)
        )
        super().__init__(f"sync plan changed: {detail}")


class PlanStore:
    """Process-local plan store; plans are intentionally not durable."""

    def __init__(self) -> None:
        self._plans: dict[str, SyncPlan] = {}
        self._lock = threading.Lock()

    def put(self, plan: SyncPlan) -> SyncPlan:
        with self._lock:
            self._plans[plan.plan_id] = plan
        return plan

    def get(self, plan_id: str) -> SyncPlan | None:
        with self._lock:
            return self._plans.get(plan_id)

    def pop(self, plan_id: str) -> SyncPlan | None:
        with self._lock:
            return self._plans.pop(plan_id, None)


def diff_plan(plan: SyncPlan, writes: dict[str, str], deletes: frozenset[str]) -> None:
    """Raise :class:`PlanChanged` if the current scan differs from ``plan``."""
    added = sorted(set(writes) - set(plan.writes))
    removed = sorted(set(plan.writes) - set(writes))
    changed = sorted(
        key for key in set(plan.writes) & set(writes)
        if plan.writes[key] != writes[key]
    )
    delete_diff = set(deletes) ^ set(plan.deletes)
    has_diff = bool(added or changed or removed or delete_diff)
    if has_diff:
        raise PlanChanged(
            added=added,
            changed=changed,
            removed=sorted(set(removed) | delete_diff),
        )


PLANS = PlanStore()


def execute_plan(
    plan_id: str,
    *,
    writes: dict[str, str],
    deletes: frozenset[str] = frozenset(),
) -> SyncPlan:
    """Validate a re-scan against a frozen plan and consume it.

    The caller performs the actual writes/deletes after this check so the
    frozen plan cannot be applied to a different scan.
    """
    plan = PLANS.pop(plan_id)
    if plan is None:
        raise PlanChanged({"error": f"unknown or already executed plan: {plan_id}"})
    diff_plan(plan, writes, deletes)
    return plan

execute = execute_plan
