"""Ordering keys for equal-score recursive summaries in local memory indexes."""

from collections.abc import Mapping


def summary_position(metadata: Mapping[str, object]) -> tuple | None:
    """Read existing source coordinates without accessing the stored unit."""
    session = metadata.get("system_metadata.graphmem_session_scope")
    start = metadata.get("tree_span_start")
    end = metadata.get("tree_span_end")
    if (metadata.get("hierarchy_role") != "time_span"
            or not isinstance(session, str) or not session
            or type(start) is not int or type(end) is not int):
        return None
    height = metadata.get("system_metadata.graphmem_height")
    seq = metadata.get("seq")
    return start, end, session, height if isinstance(height, str) else "", (
        seq if type(seq) is int else -1
    )
