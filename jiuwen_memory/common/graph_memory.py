"""Shared, deterministic identities for optional GraphMem relation projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY
from jiuwen_memory.common.graphmem_structure import STRUCTURE_KEYS
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.common.type_def.memory_codec import dumps

RELATION_KIND = "graphmem"
PROJECTION_VERSION = "graphmem-relations-v1"


def memory_fingerprint(unit: MemoryUnit) -> str:
    """Bind an edge to the source version, including scope, structure and validity.

    Exclude derived index metadata: indexing can populate it after construction.
    Truth fields affecting source attribution are retained. External indexed
    vectors have a separate associator input fingerprint for cache invalidation.
    """
    # Relation decisions use summaries/features, never inherited raw fact text.
    # Keep this true for strict summary-only retrieval as well.
    data = json.loads(dumps(replace(unit, tree_memory=None)))
    if unit.tree_memory is not None:
        data["source_ids"] = [
            [fact.source_scope.org, fact.source_scope.space, fact.source_scope.user,
             fact.source_scope.agent, fact.source_scope.session, fact.source_id]
            for fact in unit.tree_memory.facts
        ]
    metadata = data.pop("system_metadata", {})
    # Recursive height/session affect valid navigation and association decisions.
    if any(key in metadata for key in STRUCTURE_KEYS):
        data["recursive_structure"] = {key: metadata.get(key) for key in STRUCTURE_KEYS}
    if FEATURES_KEY in metadata:
        data["graph_semantics"] = metadata[FEATURES_KEY]
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
