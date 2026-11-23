# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable per-scope successful input snapshots for experience consolidation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.experience import ATTR_HARMFUL, UtilityStore, attribution_event_key
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory import LifecycleState, MemoryTier
from jiuwen_memory.storage.kv import KVStore

PROGRESS_KEY = "/experience/consolidation/progress"


def is_consolidation_entry(unit: MemoryUnit) -> bool:
    """Exclude skills and run summaries from the new-experience trigger."""
    return (
        unit.system_metadata.get("memory_type") == "experience"
        and unit.lifecycle == LifecycleState.ACTIVE
        and unit.tier in (MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL)
    )


@dataclass
class ConsolidationSnapshot:
    entry_ids: set[str] = field(default_factory=set)
    harmful_events: set[str] = field(default_factory=set)

    def has_new_inputs(self, known: ConsolidationSnapshot) -> bool:
        return bool(self.entry_ids - known.entry_ids or self.harmful_events - known.harmful_events)


def harmful_event_ids(utility: UtilityStore, scope: Scope) -> set[str]:
    """Snapshot the latest harmful attribution revisions before any work begins."""
    latest = {}
    for key, payload in utility.scan_events(scope):
        if key.kind != "attr":
            continue
        identity = (key.entry_id, key.author, key.trajectory_id)
        previous = latest.get(identity)
        if previous is None or key.revision > previous[0]:
            latest[identity] = (key.revision, payload.get("verdict"))
    return {
        attribution_event_key(entry_id, author, trajectory_id, revision)
        for (entry_id, author, trajectory_id), (revision, verdict) in latest.items()
        if verdict == ATTR_HARMFUL
    }


class ExperienceConsolidationProgress:
    """Store processed entry and harmful-event IDs under the full experience scope.

    Input IDs are captured before work begins. Saving never scans live storage,
    so an entry arriving during consolidation cannot be acknowledged by accident.
    A stale concurrent writer can cause a retry, but cannot acknowledge unseen IDs.
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def load(self, scope: Scope) -> ConsolidationSnapshot:
        try:
            raw = self._kv.get(scope, PROGRESS_KEY)
        except NotFoundError:
            return ConsolidationSnapshot()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValidationError("Invalid experience consolidation progress") from exc
        if not isinstance(payload, dict) or payload.get("version") not in (1, 2):
            raise ValidationError("Invalid experience consolidation progress")
        entries = payload.get("entry_ids")
        harmful = [] if payload["version"] == 1 else payload.get("harmful_events")
        for ids in (entries, harmful):
            if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
                raise ValidationError("Invalid experience consolidation progress")
        return ConsolidationSnapshot(set(entries), set(harmful))

    def save(self, scope: Scope, snapshot: ConsolidationSnapshot) -> None:
        payload = json.dumps({
            "version": 2,
            "entry_ids": sorted(snapshot.entry_ids),
            "harmful_events": sorted(snapshot.harmful_events),
            "completed_at": datetime.now(UTC).isoformat(),
        }).encode("utf-8")
        try:
            self._kv.insert(scope, PROGRESS_KEY, payload)
        except ConflictError:
            self._kv.update(scope, PROGRESS_KEY, payload)
