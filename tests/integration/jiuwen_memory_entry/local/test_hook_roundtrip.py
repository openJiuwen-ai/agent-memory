"""Real-service hook round trip: recall → capture → replay → restart (F17 D8/D9)."""

from __future__ import annotations

import json
import os
import signal
import time

import pytest
import yaml

from jiuwen_memory.api import LocalTokenStore, Role, Scope, issue_token, token_digest
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local.client import ServiceClient
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home

pytestmark = pytest.mark.integration

MODEL = ModelSettings(llm_base_url="https://api.deepseek.com", llm_model="m")
ORG = "local"
PROJECT = "hook-project"


def _local_only(service_config) -> None:
    settings = yaml.safe_load(service_config.read_text(encoding="utf-8"))
    api = settings["memory_api"]
    api["tokenizer"] = {"default": {"target": "whitespace"}}
    api["llm"] = {"default": "echo"}
    api["embedder"] = {"default": {"target": "hashing",
                                   "params": {"tokenizer": "default", "embedder_dim": 64}}}
    api["extractor"] = {"default": {"target": "keyword",
                                    "params": {"tokenizer": "default"}}}
    api["reranker"] = {"default": {"target": "overlap", "params": {"tokenizer": "default"}}}
    service_config.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")


def _install(home: MemoryHome, host: str, agent: str, connector: str) -> tuple[str, str]:
    install_id = f"{host}-hook"
    token = issue_token()
    directory = home.installs_dir / install_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    os.chmod(directory / "token", 0o600)
    (directory / "install.json").write_text(
        json.dumps(
            {
                "install_id": install_id,
                "host": host,
                "connector_id": connector,
                "actor": {"org": ORG, "space": "", "user": "", "agent": agent, "session": ""},
                "project": PROJECT,
                "capture": "task",
                "state": "connected",
            }
        ),
        encoding="utf-8",
    )
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    connectors = settings["memory_api"]["globals"].setdefault("continuity_connectors", {})
    connectors[connector] = {
        "actor": {"org": ORG, "space": "", "user": "", "agent": agent, "session": ""},
        "projects": [PROJECT],
        "enabled": True,
    }
    home.service_config.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    try:
        store.register(install_id, token_digest(token), Scope(org=ORG, agent=agent), Role.USER)
    finally:
        store.close()
    return install_id, token


def _scope() -> dict:
    return {"org": ORG, "space": PROJECT}


def _post(client: ServiceClient, path: str, payload: dict) -> tuple[int, dict]:
    status, body = client.post(path, payload)
    return status, body if isinstance(body, dict) else {}


def _stop(home, discovery) -> None:
    try:
        os.kill(discovery.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and discovery_module.read_discovery(home):
        time.sleep(0.1)


@pytest.fixture
def runtime(tmp_path, request):
    home = MemoryHome(tmp_path / "home")
    init_home(home, document_root=tmp_path / "docs", model=MODEL)
    _local_only(home.service_config)
    old_id, old_token = _install(home, "jiuwenswarm", "swarm-old", "swarm-conn")
    codex_id, codex_token = _install(home, "codex", "codex-agent", "codex-conn")
    discovery = discovery_module.ensure_service(home, timeout=180)

    def stop():
        current = discovery_module.read_discovery(home)
        if current is not None:
            _stop(home, current)

    request.addfinalizer(stop)
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    try:
        store.register(old_id, token_digest(old_token), Scope(org=ORG, agent="swarm-old"),
                       Role.ADMIN)
        old = ServiceClient(discovery, old_token)
        status, body = old.post(
            "/v1/create_space",
            {"spec": {"org": ORG, "space": PROJECT,
                      "owner": {"org": ORG, "agent": "swarm-old"}}},
        )
        assert status == 200, body
    finally:
        store.register(old_id, token_digest(old_token), Scope(org=ORG, agent="swarm-old"),
                       Role.USER)
        store.close()
    old = ServiceClient(discovery, old_token)
    codex = ServiceClient(discovery, codex_token)
    status, body = old.post(
        "/v1/add_space_member",
        {"org": ORG, "space": PROJECT,
         "member": {"scope": {"org": ORG, "agent": "codex-agent"},
                    "content_role": "contributor", "governance_role": "none"}},
    )
    assert status == 200, body
    status, created = _post(old, "/v1/continuity_task",
                            {"scope": _scope(),
                             "request": {"operation": "create", "request_id": "hook-create",
                                         "title": "hook task"}})
    assert status == 200, created
    task_id = created["task_id"]
    status, view = _post(codex, "/v1/continuity_task",
                         {"scope": _scope(), "request": {"operation": "get",
                                                         "task_id": task_id}})
    assert status == 200, view
    status, bound = _post(codex, "/v1/continuity_task",
                          {"scope": _scope(),
                           "request": {"operation": "bind", "request_id": "hook-bind",
                                       "task_id": task_id, "connector_id": "codex-conn",
                                       "context_id": "s1",
                                       "expected_revision": view["revision"]}})
    assert status == 200, bound
    yield home, discovery, codex
    stop()


def test_recall_capture_replay_and_restart(runtime):
    home, discovery, codex = runtime
    # Unbound session: ordinary recall, no pending turn, a clear notice.
    status, body = _post(codex, "/local/hook/recall",
                         {"host_session_id": "s1", "turn_id": "t1", "prompt": "hello"})
    assert status == 200, body
    assert body["status"] == "ok", body
    # Capture after a bound recall is committed with refs.
    status, body = _post(codex, "/local/hook/recall",
                         {"host_session_id": "s1", "turn_id": "t2", "prompt": "how"})
    assert status == 200 and body["status"] == "ok", body
    status, captured = _post(
        codex,
        "/local/hook/capture",
        {"host_session_id": "s1", "turn_id": "t2", "prompt": "how", "answer": "like this"},
    )
    assert status == 200, captured
    assert captured["status"] == "committed", captured
    assert captured["refs"], captured
    first_refs = captured["refs"]

    status, shown = _post(codex, "/local/op",
                          {"local_op_id": captured["local_op_id"]})
    assert status == 200, shown
    assert shown["event_id"] and shown["kernel_operation_id"]
    assert shown["kernel_operation_id"] != shown["event_id"]
    assert shown["refs"] == [ref["unit_id"] for ref in first_refs]

    from jiuwen_memory_entry.local.turns import Operation, TurnKey, TurnStore

    hidden_store = TurnStore(home.data_dir / "local_turns.db")
    hidden_store.create_operation(
        Operation(
            local_op_id="op-hidden", key=TurnKey("codex-conn", PROJECT, "s1", "hidden"),
            install_id="codex-hook", actor={}, task_id="task-hidden",
            binding_id="binding-hidden", event_id="event-hidden", digest="d",
            status="committed",
            receipt={"status": "committed", "operation_id": "kernel-hidden",
                     "refs": [{"unit_id": "missing-unit",
                               "scope": {"org": ORG, "space": PROJECT}}]},
        )
    )
    hidden_store.close()
    status, limited = _post(codex, "/local/op", {"local_op_id": "op-hidden"})
    assert status == 200 and limited == {
        "local_op_id": "op-hidden", "status": "committed"
    }, limited

    # Replaying the same turn is idempotent.
    status, again = _post(
        codex,
        "/local/hook/capture",
        {"host_session_id": "s1", "turn_id": "t2", "prompt": "how", "answer": "like this"},
    )
    assert status == 200 and again["status"] == "committed"
    assert [ref["unit_id"] for ref in again["refs"]] == [
        ref["unit_id"] for ref in first_refs
    ]

    # Different content for a frozen turn is a conflict, not a second write.
    status, conflict = _post(
        codex,
        "/local/hook/capture",
        {"host_session_id": "s1", "turn_id": "t2", "prompt": "how", "answer": "changed"},
    )
    assert status == 200 and conflict["status"] == "conflict", conflict

    # Restart: the operation record and pending turn still answer the replay.
    _stop(home, discovery)
    discovery = discovery_module.ensure_service(home, timeout=180)
    codex = ServiceClient(discovery, (home.installs_dir / "codex-hook" / "token")
                          .read_text(encoding="utf-8").strip())
    status, replay = _post(
        codex,
        "/local/hook/capture",
        {"host_session_id": "s1", "turn_id": "t2", "prompt": "how", "answer": "like this"},
    )
    assert status == 200 and replay["status"] == "committed", replay
    assert [ref["unit_id"] for ref in replay["refs"]] == [
        ref["unit_id"] for ref in first_refs
    ]


def test_mcp_bridge_takeover_and_io(runtime):
    """The MCP bridge (add/search + self-bind) can take over a task end to end."""
    import asyncio
    from types import SimpleNamespace

    from jiuwen_memory_adapter.continuity.client import ContinuityClient
    from jiuwen_memory_adapter.continuity.mcp import _check_task_operation

    home, discovery, _codex = runtime
    token = (home.installs_dir / "codex-hook" / "token").read_text(encoding="utf-8").strip()
    client = ContinuityClient(f"http://127.0.0.1:{discovery.port}", token)
    local = SimpleNamespace(connector_id="codex-conn")
    scope = _scope()
    created = client.call(
        "continuity_task",
        {"scope": scope,
         "request": {"operation": "create", "request_id": "mcp-create",
                     "title": "mcp takeover"}},
    )
    task_id = created["task_id"]
    view = client.call(
        "continuity_task",
        {"scope": scope, "request": {"operation": "get", "task_id": task_id}},
    )
    arguments = {
        "scope": scope,
        "request": {"operation": "bind", "request_id": "mcp-bind", "task_id": task_id,
                    "connector_id": "codex-conn", "context_id": "mcp-session",
                    "expected_revision": view["revision"]},
    }
    asyncio.run(_check_task_operation(client, local, arguments))
    bound = client.call("continuity_task", arguments)
    bindings = bound["task"]["bindings"]
    binding_id = next(
        item["binding_id"] for item in bindings if item["connector_id"] == "codex-conn"
    )
    units = client.call(
        "add",
        {"content": "user: mcp\nassistant: takeover", "scope": scope,
         "continuity": {"task_id": task_id, "binding_id": binding_id,
                        "event_id": "mcp-event"}},
    )
    assert isinstance(units, list) and units, units
    found = client.call(
        "search",
        {"query": "takeover", "context": {"scope": scope}, "task_id": task_id, "top_k": 5},
    )
    assert found["items"], found
    # The bridge still refuses another connection's binding.
    with pytest.raises(ValueError):
        asyncio.run(
            _check_task_operation(
                client, SimpleNamespace(connector_id="other"), arguments
            )
        )
    client.close()
