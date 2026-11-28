"""F17 delivery gate: old install data, Codex take-over, restart, permission matrix."""

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
PROJECT = "gate-project"


def _local_only(service_config) -> None:
    """Replace remote components so the gate runs without network or API keys."""
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


def _actor(agent: str) -> Scope:
    return Scope(org=ORG, agent=agent)


def _install(home: MemoryHome, host: str, agent: str, connector: str) -> tuple[str, str]:
    install_id = f"{host}-gate"
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
        store.register(install_id, token_digest(token), _actor(agent), Role.USER)
    finally:
        store.close()
    return install_id, token


def _scope() -> dict:
    return {"org": ORG, "space": PROJECT}


def _post(client: ServiceClient, path: str, payload: dict) -> tuple[int, dict]:
    status, body = client.post(path, payload)
    return status, body if isinstance(body, dict) else {}


def _create_task(client: ServiceClient) -> str:
    status, body = _post(
        client,
        "/v1/continuity_task",
        {"scope": _scope(), "request": {"operation": "create", "request_id": "gate-create",
                                        "title": "gate", "description": "delivery gate task"}},
    )
    assert status == 200, body
    return body["task_id"]


def _bind(client: ServiceClient, task_id: str, connector: str) -> str:
    status, view = _post(client, "/v1/continuity_task",
                         {"scope": _scope(), "request": {"operation": "get",
                                                         "task_id": task_id}})
    assert status == 200, view
    status, body = _post(
        client,
        "/v1/continuity_task",
        {
            "scope": _scope(),
            "request": {"operation": "bind", "request_id": f"bind-{connector}",
                        "task_id": task_id, "connector_id": connector,
                        "context_id": "session-1",
                        "expected_revision": view.get("revision", 0)},
        },
    )
    assert status == 200, body
    bindings = body.get("task", body).get("bindings") or []
    match = [
        item for item in bindings
        if item.get("connector_id") == connector and item.get("active")
    ]
    assert match, bindings
    return match[-1]["binding_id"]


def _write(client: ServiceClient, task_id: str, binding_id: str, event_id: str, text: str):
    status, body = client.post(
        "/v1/add",
        {
            "content": text,
            "scope": _scope(),
            "continuity": {"task_id": task_id, "binding_id": binding_id,
                           "event_id": event_id},
        },
    )
    assert status == 200, body
    assert isinstance(body, list), body
    return body


def _reload(client: ServiceClient) -> None:
    client.reload()
    deadline = time.monotonic() + 120
    last_unavailable: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if not client.status().get("reloading"):
                return
        except discovery_module.ServiceUnavailable as exc:
            # The service may be briefly unreachable while it restarts; keep polling.
            last_unavailable = exc
        time.sleep(0.1)
    raise AssertionError(f"service did not finish reloading: {last_unavailable}")


def _unit_ids(units: list[dict]) -> list[str]:
    return [unit["id"] for unit in units]


@pytest.fixture
def home(tmp_path):
    memory_home = MemoryHome(tmp_path / "home")
    init_home(memory_home, document_root=tmp_path / "docs", model=MODEL)
    _local_only(memory_home.service_config)
    yield memory_home


def _stop_if_live(home) -> None:
    discovery = discovery_module.read_discovery(home)
    if discovery is not None:
        _stop(home, discovery)


def _stop(home, discovery) -> None:
    try:
        os.kill(discovery.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and discovery_module.read_discovery(home):
        time.sleep(0.1)


def test_simulated_old_install_is_taken_over_by_codex_and_restart(home, request):
    request.addfinalizer(lambda: _stop_if_live(home))
    # Connection entries are written before the service starts, so the first assembly
    # already knows both hosts (matching a real `connect` followed by `service start`).
    old_id, old_token = _install(home, "jiuwenswarm", "swarm-old", "swarm-conn")
    codex_id, codex_token = _install(home, "codex", "codex-agent", "codex-conn")
    discovery = discovery_module.ensure_service(home, timeout=180)
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    old_actor = _actor("swarm-old")
    try:
        # First install owns the project space; the codex install is a contributor.
        store.register(old_id, token_digest(old_token), old_actor, Role.ADMIN)
        old = ServiceClient(discovery, old_token)
        status, body = _post(
            old,
            "/v1/create_space",
            {"spec": {"org": ORG, "space": PROJECT, "owner": old_actor.__dict__}},
        )
        assert status == 200, body
    finally:
        store.register(old_id, token_digest(old_token), old_actor, Role.USER)
        store.close()
    old = ServiceClient(discovery, old_token)
    codex = ServiceClient(discovery, codex_token)
    status, body = _post(
        old,
        "/v1/add_space_member",
        {"org": ORG, "space": PROJECT,
         "member": {"scope": _actor("codex-agent").__dict__,
                    "content_role": "contributor", "governance_role": "none"}},
    )
    assert status == 200, body
    task_id = _create_task(old)
    old_binding = _bind(old, task_id, "swarm-conn")
    written_old = _write(old, task_id, old_binding, "old-event", "user: old\nassistant: old answer")

    # Codex self-binds to the same task and writes its own event.
    codex_binding = _bind(codex, task_id, "codex-conn")
    assert codex_binding != old_binding
    written_codex = _write(codex, task_id, codex_binding, "codex-event",
                           "user: q\nassistant: codex answer")

    # Same task, both bindings visible to both installs.
    for client in (old, codex):
        status, view = _post(client, "/v1/continuity_task",
                             {"scope": _scope(), "request": {"operation": "get",
                                                             "task_id": task_id}})
        assert status == 200, view
        connectors = {binding["connector_id"] for binding in view["bindings"]}
        assert connectors == {"swarm-conn", "codex-conn"}

    # Personal memory stays visible through the original five-dimension scope.
    status, found = _post(old, "/v1/search",
                          {"query": "codex", "context": {"scope": {"org": ORG,
                                                                   "space": PROJECT}},
                           "task_id": task_id, "top_k": 5})
    assert status == 200, found
    assert found["items"], "the take-over event is not retrievable by the task"

    # Codex can remove its own binding, but cannot manage another install's task or
    # bind someone else's connector.
    status, view = _post(codex, "/v1/continuity_task",
                         {"scope": _scope(), "request": {"operation": "get",
                                                         "task_id": task_id}})
    revision = view["revision"]
    status, _body = _post(
        codex,
        "/v1/continuity_task",
        {"scope": _scope(), "request": {"operation": "unbind", "task_id": task_id,
                                        "request_id": "codex-unbind", "binding_id":
                                            codex_binding, "expected_revision": revision}},
    )
    assert status == 200, _body
    status, view = _post(codex, "/v1/continuity_task",
                         {"scope": _scope(), "request": {"operation": "get",
                                                         "task_id": task_id}})
    revision = view["revision"]
    for payload in (
        {"scope": _scope(), "request": {"operation": "close", "task_id": task_id,
                                        "request_id": "codex-close",
                                        "expected_revision": revision}},
        {"scope": _scope(), "request": {"operation": "bind", "task_id": task_id,
                                        "request_id": "codex-bind-other",
                                        "connector_id": "swarm-conn", "context_id": "x",
                                        "expected_revision": revision}},
    ):
        status, _body = _post(codex, "/v1/continuity_task", payload)
        assert status == 403, (payload, status, _body)

    # Restart the service; the records survive.
    _stop(home, discovery)
    discovery = discovery_module.ensure_service(home, timeout=180)
    old = ServiceClient(discovery, old_token)
    codex = ServiceClient(discovery, codex_token)
    status, view = _post(codex, "/v1/continuity_task",
                         {"scope": _scope(), "request": {"operation": "get",
                                                         "task_id": task_id}})
    assert status == 200, view
    assert {b["connector_id"] for b in view["bindings"]} == {"swarm-conn", "codex-conn"}
    status, found = _post(old, "/v1/search",
                          {"query": "codex", "context": {"scope": {"org": ORG,
                                                                   "space": PROJECT}},
                           "top_k": 5})
    assert status == 200 and found["items"], found
    assert _unit_ids(written_old) and _unit_ids(written_codex)
    _stop(home, discovery)
