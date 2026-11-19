"""Versioned recursive structure; physical height is independent of TIME roles."""

import hashlib
import json
from dataclasses import asdict

from jiuwen_memory.common.type_def import HierarchyRole, MemoryUnit

RECURSIVE_VERSION = "1"
STRUCTURE_KEYS = ("graphmem_recursive_version", "graphmem_height", "graphmem_session_scope",
                  "graphmem_child_roles")
DETAIL_SPANS_KEY = "graphmem_child_detail_spans"


def _detail_binding(unit):
    text = "\n".join(segment.content for segment in unit.segments[1:])
    material = [text, asdict(unit.scope), unit.hierarchy.child_ids,
                [asdict(unit.hierarchy.child_scope_at(i, unit.scope))
                 for i in range(len(unit.hierarchy.child_ids))],
                unit.system_metadata.get("graphmem_child_roles")]
    return text, hashlib.sha256(json.dumps(material, ensure_ascii=False,
                                          sort_keys=True).encode()).hexdigest()


def encode_child_details(unit: MemoryUnit, details: list[str]) -> str:
    """Bind generated per-child details to spans in the existing summary body."""
    text, binding = _detail_binding(unit)
    if (not is_recursive(unit) or len(details) != len(unit.hierarchy.child_ids)
            or not 1 <= len(details) <= 64
            or any(not isinstance(s, str) or not s.strip() for s in details)
            or "\n".join(dict.fromkeys(details)) != text):
        raise ValueError("child details must reconstruct the complete summary")
    offsets, cursor = {}, 0
    for detail in dict.fromkeys(details):
        offsets[detail] = [cursor, cursor + len(detail)]
        cursor += len(detail) + 1
    return json.dumps({"version": 1, "binding": binding,
                       "spans": [offsets[detail] for detail in details]}, separators=(",", ":"))


def read_child_details(unit: MemoryUnit) -> dict[int, str]:
    """Old/stale/malformed optional maps are unavailable, never new read authority."""
    encoded = unit.system_metadata.get(DETAIL_SPANS_KEY)
    if not is_recursive(unit) or not isinstance(encoded, str) or len(encoded) > 16384:
        return {}
    try:
        data = json.loads(encoded)
        text, binding = _detail_binding(unit)
        spans = data["spans"]
        if (type(data["version"]) is not int or data["version"] != 1
                or data["binding"] != binding or not isinstance(spans, list)
                or not 1 <= len(spans) <= 64 or len(spans) != len(unit.hierarchy.child_ids)):
            return {}
        if any(not isinstance(span, list) or len(span) != 2
               or any(type(n) is not int for n in span)
               or not 0 <= span[0] < span[1] <= len(text) for span in spans):
            return {}
        return {i: text[start:end] for i, (start, end) in enumerate(spans)}
    except (ValueError, TypeError, KeyError):
        return {}


def is_recursive(unit: MemoryUnit) -> bool:
    return unit.system_metadata.get("graphmem_recursive_version") == RECURSIVE_VERSION


def node_height(unit: MemoryUnit) -> int:
    if is_recursive(unit):
        value = unit.system_metadata.get("graphmem_height")
        try:
            height = int(value)
        except (TypeError, ValueError):
            return -1
        return height if str(height) == str(value) and height > 0 else -1
    return {HierarchyRole.SNAPSHOT: 0, HierarchyRole.TIME_SPAN: 1,
            HierarchyRole.SCENE: 2, HierarchyRole.EVENT: 3}.get(unit.hierarchy.role, -1)


def valid_child_role(parent: MemoryUnit, child: MemoryUnit) -> bool:
    """Structural role/height check only; callers must still verify scope and backlinks."""
    if is_recursive(parent):
        if parent.hierarchy.role is not HierarchyRole.TIME_SPAN:
            return False
        if child.hierarchy.role is HierarchyRole.SNAPSHOT and not is_recursive(child):
            s = child.scope
            session = json.dumps((s.org, s.space, s.user, s.agent, s.session))
            return (node_height(parent) > 0
                    and session == parent.system_metadata.get("graphmem_session_scope"))
        return (is_recursive(child) and child.hierarchy.role is HierarchyRole.TIME_SPAN
                and parent.system_metadata.get("graphmem_session_scope")
                == child.system_metadata.get("graphmem_session_scope")
                and 0 < node_height(child) < node_height(parent))
    if is_recursive(child):
        return False
    expected = {HierarchyRole.TIME_SPAN: HierarchyRole.SNAPSHOT,
                HierarchyRole.SCENE: HierarchyRole.TIME_SPAN,
                HierarchyRole.EVENT: HierarchyRole.SCENE}.get(parent.hierarchy.role)
    return expected is not None and expected is child.hierarchy.role
