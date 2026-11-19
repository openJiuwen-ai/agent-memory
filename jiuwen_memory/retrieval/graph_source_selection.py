"""Reuse scoped raw vector indexes to select evidence within a verified graph pool."""

from __future__ import annotations

import re
from collections import deque


def hybrid_order(keys, documents, weights, qterms, vector_scores, *, ties=None):
    """RRF combines existing lexical/vector ranks; no trained or model-based scorer."""
    ties = ties if ties is not None else {key: key for key in keys}
    lexical_scores = {key: sum(weights[t] for t in documents.get(key, set()) & qterms)
                      for key in keys}
    lexical = sorted(keys, key=lambda key: (-lexical_scores[key], ties[key]))
    lexical_rank = {key: i + 1 for i, key in enumerate(lexical) if lexical_scores[key] > 0}
    semantic = sorted((key for key in keys if key in vector_scores),
                      key=lambda key: (-vector_scores[key], ties[key]))
    vector_rank = {key: i + 1 for i, key in enumerate(semantic)}
    original_rank = {key: i for i, key in enumerate(keys)}

    def score(key):
        return ((1 / (60 + lexical_rank[key]) if key in lexical_rank else 0)
                + (1 / (60 + vector_rank[key]) if key in vector_rank else 0))

    return sorted(keys, key=lambda key: (-score(key), original_rank[key]))


def is_personal_memory_query(question):
    """First-person memory intent, excluding explicit requests for assistant advice."""
    if not re.search(r"\b(?:I|my|me|mine)\b|我的|我曾|我买|我去", question, re.I):
        return False
    if re.search(
        r"\b(?:did you|you (?:said|told|suggested|recommended)|"
        r"assistant(?:'s)? (?:advice|response|suggestion|recommendation))\b",
        question, re.I,
    ):
        return False
    return True


def personal_source_order(keys, sources, question):
    """Interleave explicit conversation roles for first-person memory questions.

    This only permutes the verified source pool. Each role retains its original
    relative order and unknown/named speakers retain their exact positions. No
    identities are inferred from Scope or text, and no source is dropped.
    """
    if not is_personal_memory_query(question):
        return keys
    roles = {}
    for key in keys:
        speaker = sources[key].unit.user_metadata.get("speaker")
        roles[key] = speaker.strip().casefold() if isinstance(speaker, str) else ""
    users = deque(key for key in keys if roles[key] == "user")
    assistants = deque(key for key in keys if roles[key] == "assistant")
    if not users or not assistants:
        return keys
    ordered = []
    while users or assistants:
        for _ in range(3):
            if users:
                ordered.append(users.popleft())
        if assistants:
            ordered.append(assistants.popleft())
    known = iter(ordered)
    return [next(known) if roles[key] in {"user", "assistant"} else key for key in keys]
