"""SwarmFlow evidence is submitted only when a completed run has tool results."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwen_memory.construction.experience import compress_trace, tool_call_digest
from jiuwen_memory_adapter.jiuwenswarm.workflow_experience import (
    build_agent_experience,
    build_workflow_experience,
)


def _activity(call_id: str, result: str | None) -> dict:
    return {
        "type": "tool_call",
        "tool_call_id": call_id,
        "tool_name": "build",
        "content": "Run the CI build",
        "tool_result_preview": result,
    }


@pytest.mark.unit
def test_workflow_experience_preserves_evidence_across_phases() -> None:
    initial = _activity("call", "missing config")
    snapshot = {
        "id": "wf_run",
        "status": "completed",
        "summary": "Fix CI",
        "phases": [
            {"agents": [{"id": "diagnose", "activity": [initial, initial]}]},
            {"agents": [{"id": "verify", "activity": [_activity("call", "build passed")]}]},
        ],
    }

    request = build_workflow_experience(snapshot)
    assert request is not None
    content, metadata = request
    assert metadata == {"trajectory_id": "wf_run", "tool_call_count": "2"}
    results = [item for item in json.loads(content)["messages"] if item["role"] == "tool"]
    assert [(item["tool_call_id"], item["content"]) for item in results] == [
        ("diagnose:call", "missing config"),
        ("verify:call", "build passed"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("status", ["running", "paused", "failed", "stopped", "completed"])
def test_workflow_experience_skips_incomplete_evidence(status: str) -> None:
    result = None if status == "completed" else "build passed"
    snapshot = {
        "id": "wf_run",
        "status": status,
        "phases": [{"agents": [{"id": "verify", "activity": [_activity("call", result)]}]}],
    }
    assert build_workflow_experience(snapshot) is None


@pytest.mark.unit
def test_workflow_experience_tracks_retrieval_separately_from_execution() -> None:
    search = _activity("search", json.dumps({
        "results": [{"item_id": "lesson", "usage_ref": "0.n"}],
    }))
    search["tool_name"] = "agent_memory_experience"
    activities = [search]
    snapshot = {
        "id": "wf_run",
        "status": "completed",
        "phases": [{"agents": [{"id": "verify", "activity": activities}]}],
    }
    assert build_workflow_experience(snapshot) is None

    activities.append(_activity("verify", "build passed"))
    request = build_workflow_experience(snapshot)
    assert request is not None
    content, metadata = request
    assert metadata["tool_call_count"] == "1"
    assert json.loads(content)["retrieved"] == [{"id": "lesson", "ref": "0.n"}]


@pytest.mark.unit
def test_agent_experience_projects_completed_tool_calls() -> None:
    assert build_agent_experience("agent_run", "Fix CI", "Which pipeline?", []) is None
    clarification = _activity("ask", "release pipeline")
    clarification["tool_name"] = "ask_user"
    activities = [
        clarification, _activity("diagnose", "config missing"), _activity("verify", "passed"),
    ]
    request = build_agent_experience("agent_run", "Fix CI", "Build succeeded", activities)
    assert request is not None
    raw, metadata = request
    assert metadata == {"trajectory_id": "agent_run", "tool_call_count": "2"}
    messages = json.loads(raw)["messages"]
    assert messages[0] == {"role": "user", "content": "Fix CI"}
    assert messages[-1] == {"role": "assistant", "content": "Build succeeded"}
    assert [item["content"] for item in messages if item["role"] == "tool"] == [
        "config missing", "passed",
    ]


@pytest.mark.unit
def test_agent_experience_keeps_tool_name_and_verification_in_model_trace() -> None:
    activity = _activity("verify", "Command details " * 40 + "\nStdout: name = Alice\nage = 30")
    activity["tool_name"] = "bash"
    activity["content"] = json.dumps({"command": "Import-Csv -Delimiter ';'"})
    request = build_agent_experience("csv", "Import two columns", "Import verified", [activity])

    assert request is not None
    messages = json.loads(request[0])["messages"]
    assert tool_call_digest(messages) == [("bash", "Import-Csv -Delimiter ';'")]
    trace = compress_trace(messages)
    assert "assistant calls tools: bash" in trace
    assert "tool bash:" in trace
    assert "Stdout: name = Alice\nage = 30" in trace


@pytest.mark.unit
def test_sync_experience_submits_core_tool_evidence() -> None:
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import AgentMemoryMemoryProvider

    provider = SimpleNamespace(add_trajectory=AsyncMock())
    asyncio.run(
        AgentMemoryMemoryProvider.sync_experience(
            provider, "agent_task", "Fix CI", "Fixed", [_activity("build", "passed")], "session",
        )
    )

    provider.add_trajectory.assert_awaited_once()
    content, metadata, session = provider.add_trajectory.await_args.args
    assert session == "session"
    assert metadata["trajectory_id"] == "agent_task"
    assert json.loads(content)["messages"][-2]["content"] == "passed"
