"""Durable ordered-source cursor. Caller holds the repository single-writer lock."""

from datetime import UTC, datetime

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def.continuity import fingerprint


def source_key(connector, context_id):
    return fingerprint({"connector": connector, "context_id": context_id})


def cursor(repo, scope, connector, context_id):
    try:
        return repo.get(scope, "sync", source_key(connector, context_id))
    except NotFoundError:
        return {"position": 0, "content_digest": None, "last_committed_at": None}


def reserve(repo, scope, connector, context_id, event, operation_id, digest):
    state = cursor(repo, scope, connector, context_id)
    if state.get("cutover_position") is not None:
        # After the dual-write cutover the external product must stop emitting source
        # events; a reservation past the watermark is a misconfigured source, never a
        # second authoritative writer.
        raise ConflictError("sync", message="source has been cut over to agent-memory")
    pending = {"operation_id": operation_id, "digest": digest,
               "position": event.source_position, "previous": event.previous_position}
    if state.get("pending"):
        if state["pending"] != pending:
            raise ConflictError("sync", message="resolve the pending source event before advancing")
        return
    if state["position"] != event.previous_position:
        raise ConflictError("sync", message="source cursor differs from expected previous position")
    state["pending"] = pending
    repo.save(scope, "sync", source_key(connector, context_id), state)


def reject_cutover(repo, scope, connector, context_id):
    """Reject any new event after a dual-write cutover, ordered or not.

    The cutover makes agent-memory the single authoritative writer for the whole source,
    so an event without ``source_position`` cannot slip past the ordered-cursor guard.
    Already-committed replays bypass this via their existing receipt.
    """
    state = cursor(repo, scope, connector, context_id)
    if state.get("cutover_position") is not None:
        raise ConflictError("sync", message="source has been cut over to agent-memory")


def mark_cutover(repo, scope, connector, context_id):
    """Record the dual-write cutover at the current cursor.

    The source is first caught up (no pending reservation), then the watermark marks the
    position after which agent-memory is the single authoritative writer. Idempotent: an
    existing watermark is returned unchanged.
    """
    state = cursor(repo, scope, connector, context_id)
    if state.get("cutover_position") is not None:
        return state
    if state.get("pending"):
        raise ConflictError("sync", message="settle the pending source event before cutover")
    state["cutover_position"] = state["position"]
    state["cutover_at"] = datetime.now(UTC).isoformat()
    repo.save(scope, "sync", source_key(connector, context_id), state)
    return state


def cutover(repo, scope, connector, context_id):
    """Return the cutover watermark position, or ``None`` if the source is still dual-write."""
    state = cursor(repo, scope, connector, context_id)
    return state.get("cutover_position")


def commit(repo, scope, connector, context_id, event, operation_id, digest):
    state = cursor(repo, scope, connector, context_id)
    if state["position"] >= event.source_position:
        return state  # Earlier committed event replay does not move the cursor backward.
    pending = state.get("pending", {})
    if pending.get("operation_id") != operation_id or pending.get("digest") != digest:
        raise ConflictError("sync", message="missing source reservation")
    state = {
        "position": event.source_position, "content_digest": digest,
        "last_committed_at": datetime.now(UTC).isoformat(),
    }
    repo.save(scope, "sync", source_key(connector, context_id), state)
    return state
