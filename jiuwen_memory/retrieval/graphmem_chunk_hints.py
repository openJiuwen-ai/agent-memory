"""Carry a winning L2 index location to verified direct-child index selection."""

import hashlib
import json
import math
from dataclasses import replace

from jiuwen_memory.common.graphmem_structure import DETAIL_SPANS_KEY, read_child_details
from jiuwen_memory.common.type_def import RecallChannel
from jiuwen_memory.common.type_def.retrieval import ContentMatch


def valid_match(match):
    return (isinstance(match, ContentMatch)
            and isinstance(match.record_id, str) and 0 < len(match.record_id) <= 256
            and isinstance(match.content_sha256, str) and len(match.content_sha256) == 64
            and all(char in "0123456789abcdef" for char in match.content_sha256)
            and all(type(value) is int for value in (match.seq, match.start, match.end))
            and match.seq >= 0 and 0 <= match.start < match.end
            and type(match.score) in (int, float) and math.isfinite(match.score)
            and match.score > 0)


def match_order(match):
    return -match.score, match.seq, match.start, match.end, match.record_id


def collect_content_matches(batches):
    """Keep one L2 match per already-recalled ID; do not alter fusion scores."""
    matches = {}
    for batch in batches:
        if batch.channel is not RecallChannel.VECTOR or batch.source != "vector_l2":
            continue
        for item in batch.candidates:
            match = item.content_match
            if not valid_match(match):
                continue
            old = matches.get(item.unit_id)
            if old is None or match_order(match) < match_order(old):
                matches[item.unit_id] = match
    return matches


def attach_content_matches(candidates, batches):
    matches = collect_content_matches(batches)
    return [replace(item, content_match=matches.get(item.unit_id)) for item in candidates]


def child_chunk_hints(parent, match, child_scope, ids, role):
    """Suggest children overlapping a validated hit; no fact/time/entity claim."""
    if (not valid_match(match) or match.end > len(parent.content)
            or match.content_sha256 != hashlib.sha256(parent.content.encode()).hexdigest()):
        return {}
    details = read_child_details(parent)
    kinds = parent.system_metadata.get("graphmem_child_roles", [])
    if (not details or not isinstance(kinds, list)
            or len(kinds) != len(parent.hierarchy.child_ids)):
        return {}
    # read_child_details validated this exact map against the current content,
    # complete parent scope and ordered child references. Its offsets exclude
    # the first (span-heading) segment; index offsets include that segment.
    spans = json.loads(parent.system_metadata[DETAIL_SPANS_KEY])["spans"]
    offset = len(parent.segments[0].content) + 1
    allowed, ranked = set(ids), []
    for position, (start, end) in enumerate(spans):
        uid = parent.hierarchy.child_ids[position]
        if (uid not in allowed or kinds[position] != role.value
                or parent.hierarchy.child_scope_at(position, parent.scope) != child_scope):
            continue
        overlap = max(0, min(match.end, offset + end) - max(match.start, offset + start))
        if overlap:
            ranked.append((overlap, position, uid))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return {uid: [{"detail_position": position, "chunk_seq": match.seq,
                   "overlap_chars": overlap}] for overlap, position, uid in ranked}
