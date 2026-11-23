"""Convert completed Agent execution records to experience ingestion requests."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def _experience_references(result: str) -> list[dict[str, str]]:
    """Read attribution tokens from a recorded experience tool response."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    references = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        item_id, usage_ref = item.get("item_id"), item.get("usage_ref")
        if not isinstance(item_id, str) or not isinstance(usage_ref, str):
            continue
        if item_id and usage_ref:
            references.append({"id": item_id, "ref": usage_ref})
    return references


def _trajectory_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            if isinstance(json.loads(arguments), dict):
                return arguments
        except ValueError:
            pass
        arguments = {"preview": arguments}
    return json.dumps(arguments or {}, ensure_ascii=False, default=str)


def build_workflow_experience(
    snapshot: dict[str, Any],
) -> tuple[str, dict[str, str]] | None:
    """Use paired tool evidence, without treating workflow status as external verification."""
    run_id = snapshot.get("id")
    if not run_id or snapshot.get("status") != "completed":
        return None
    agents = (agent for phase in snapshot.get("phases", []) for agent in phase.get("agents", []))
    return _build_experience(
        str(run_id), snapshot.get("summary") or snapshot.get("name") or "SwarmFlow",
        agents, snapshot.get("result"),
    )


def build_agent_experience(
    trajectory_id: str, query: str, output: str, activities: list[dict[str, Any]],
) -> tuple[str, dict[str, str]] | None:
    """Build a trajectory from the completed tool calls of one logical Agent run."""
    return _build_experience(
        trajectory_id, query, [{"id": "agent", "activity": activities}], output,
    )


def _build_experience(
    trajectory_id: str, query: str, agents: Iterable[dict[str, Any]], output: str | None,
) -> tuple[str, dict[str, str]] | None:
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": query,
        }
    ]
    seen: set[tuple[str, str]] = set()
    retrieved: dict[str, dict[str, str]] = {}
    for agent in agents:
        for activity in agent.get("activity", []):
            if activity.get("type") != "tool_call":
                continue
            call_id = activity.get("tool_call_id")
            name = activity.get("tool_name")
            result = activity.get("tool_result_preview")
            if not call_id or not name or result is None:
                continue
            if name == "agent_memory_experience":
                for reference in _experience_references(result):
                    retrieved.setdefault(reference["id"], reference)
                continue
            if name in {"ask_user", "structured_output"} or name.startswith("agent_memory_"):
                continue
            key = (agent["id"], call_id)
            if key in seen:
                continue
            seen.add(key)
            scoped_id = f"{key[0]}:{call_id}"
            messages.extend((
                {
                    "role": "assistant",
                    "content": agent.get("prompt") or "",
                    "tool_calls": [{
                        "id": scoped_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": _trajectory_arguments(activity.get("content")),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": scoped_id,
                    "name": name,
                    "content": result,
                },
            ))
        if agent.get("outcome"):
            messages.append({"role": "assistant", "content": agent["outcome"]})

    if not seen:
        return None
    if output:
        messages.append({"role": "assistant", "content": output})
    return json.dumps(
        {"messages": messages, "retrieved": list(retrieved.values())}, ensure_ascii=False,
    ), {
        "trajectory_id": trajectory_id,
        "tool_call_count": str(len(seen)),
    }
