"""Real-service migration: legacy stores are mapped and sampled IDs stay usable."""

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
from jiuwen_memory_entry.local.migrate import (
    apply_migration,
    plan_migration,
    recover_admin,
)

pytestmark = pytest.mark.integration

MODEL = ModelSettings(llm_base_url="https://api.deepseek.com", llm_model="m")
ORG = "local"
PROJECT = "legacy-project"


def _local_only(path) -> None:
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    api = settings["memory_api"]
    api["tokenizer"] = {"default": {"target": "whitespace"}}
    api["llm"] = {"default": "echo"}
    api["embedder"] = {"default": {"target": "hashing",
                                   "params": {"tokenizer": "default", "embedder_dim": 64}}}
    api["extractor"] = {"default": {"target": "keyword",
                                    "params": {"tokenizer": "default"}}}
    api["reranker"] = {"default": {"target": "overlap", "params": {"tokenizer": "default"}}}
    path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")


def _register(home: MemoryHome, install_id: str, token: str, actor: Scope,
              role: Role = Role.USER) -> None:
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    try:
        store.register(install_id, token_digest(token), actor, role, allow_scope=True)
    finally:
        store.close()


def _install(home: MemoryHome, install_id: str, actor: Scope, connector: str) -> str:
    token = issue_token()
    directory = home.installs_dir / install_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    os.chmod(directory / "token", 0o600)
    (directory / "install.json").write_text(
        json.dumps(
            {"install_id": install_id, "host": "jiuwenswarm", "connector_id": connector,
             "actor": {"org": actor.org, "space": actor.space, "user": actor.user,
                       "agent": actor.agent, "session": actor.session},
             "project": PROJECT, "capture": "task", "state": "connected"}
        ),
        encoding="utf-8",
    )
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    connectors = settings["memory_api"]["globals"].setdefault("continuity_connectors", {})
    connectors[connector] = {"actor": {"org": actor.org, "space": actor.space,
                                       "user": actor.user, "agent": actor.agent,
                                       "session": actor.session},
                             "projects": [PROJECT], "enabled": True}
    home.service_config.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    _register(home, install_id, token, actor)
    return token


def _stop(home, discovery) -> None:
    try:
        os.kill(discovery.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and discovery_module.read_discovery(home):
        time.sleep(0.1)


def _post(client, path, payload):
    return client.post(path, payload)


def test_legacy_service_data_is_migrated_in_place(tmp_path, request):
    old = MemoryHome(tmp_path / "old")
    init_home(old, document_root=tmp_path / "old-docs", model=MODEL)
    _local_only(old.service_config)
    actor = Scope(org=ORG, space=PROJECT, agent="legacy-agent")
    old_token = _install(old, "legacy-install", actor, "legacy-conn")
    discovery = discovery_module.ensure_service(old, timeout=180)
    request.addfinalizer(
        lambda: _stop(old, discovery_module.read_discovery(old))
        if discovery_module.read_discovery(old)
        else None
    )
    admin_token = issue_token()
    _register(old, "legacy-admin", admin_token, actor, Role.ADMIN)
    admin = ServiceClient(discovery, admin_token)
    status, body = _post(admin, "/v1/create_space",
                         {"spec": {"org": ORG, "space": PROJECT,
                                   "owner": {"org": ORG, "space": PROJECT,
                                             "agent": "legacy-agent"}}})
    assert status == 200, body
    store = LocalTokenStore(str(old.data_dir / "tokens.db"))
    store.revoke("legacy-admin")
    store.purge("legacy-admin")
    store.close()
    old_client = ServiceClient(discovery, old_token)
    status, task = _post(old_client, "/v1/continuity_task",
                         {"scope": {"org": ORG, "space": PROJECT},
                          "request": {"operation": "create", "request_id": "legacy-create",
                                      "title": "legacy task"}})
    assert status == 200, task
    task_id = task["task_id"]
    status, bound = _post(old_client, "/v1/continuity_task",
                          {"scope": {"org": ORG, "space": PROJECT},
                           "request": {"operation": "bind", "request_id": "legacy-bind",
                                       "task_id": task_id, "connector_id": "legacy-conn",
                                       "context_id": "s1",
                                       "expected_revision": task["revision"]}})
    assert status == 200, bound
    binding_id = bound["task"]["bindings"][0]["binding_id"]
    status, units = old_client.post(
        "/v1/add",
        {"content": "user: legacy\nassistant: old answer",
         "scope": {"org": ORG, "space": PROJECT},
         "continuity": {"task_id": task_id, "binding_id": binding_id,
                        "event_id": "legacy-event"}},
    )
    assert status == 200 and units, units
    unit_id = units[0]["id"]
    status, before_view = _post(
        old_client, "/v1/continuity_task",
        {"scope": {"org": ORG, "space": PROJECT},
         "request": {"operation": "get", "task_id": task_id}},
    )
    assert status == 200, before_view
    before_background = len(before_view.get("background") or [])
    _stop(old, discovery)

    (tmp_path / "legacy.yaml").write_text(
        yaml.safe_dump(
            {"memory": {"identity_org": ORG, "agent_id": "legacy-agent",
                        "space": PROJECT, "data_dir": str(old.data_dir),
                        "document_root": str(tmp_path / "old-docs"),
                        "registry_store": str(old.data_dir / "registry.db"),
                        "permission_db": str(old.data_dir / "permissions.db"),
                        "connectors": {
                            "legacy-conn": {
                                "actor": {"org": ORG, "space": PROJECT, "user": "",
                                          "agent": "legacy-agent", "session": ""},
                                "projects": [PROJECT], "enabled": True,
                            }
                        }}}
        ),
        encoding="utf-8",
    )
    plan = plan_migration(tmp_path / "legacy.yaml", actor_export=None)
    assert plan.samples and plan.samples[0]["task_id"] == task_id

    target = MemoryHome(tmp_path / "target")
    init_home(target, document_root=tmp_path / "target-docs", model=MODEL)
    _local_only(target.service_config)
    report = apply_migration(target, plan, confirm=lambda _text: True)
    request.addfinalizer(
        lambda: _stop(target, discovery_module.read_discovery(target))
        if discovery_module.read_discovery(target)
        else None
    )
    assert report.state == "complete", report.message
    assert any(item == f"unit:{unit_id}" for item in report.verified)
    assert any(item.startswith("probe:") for item in report.verified)

    token = (target.installs_dir / report.install_id / "token").read_text(
        encoding="utf-8"
    ).strip()
    migrated = ServiceClient(discovery_module.ensure_service(target), token)
    status, view = _post(migrated, "/v1/continuity_task",
                         {"scope": {"org": ORG, "space": PROJECT},
                          "request": {"operation": "get", "task_id": task_id}})
    assert status == 200, view
    assert len(view.get("background") or []) == before_background, (
        "migration must not write verification content into a user task"
    )
    status, unit = _post(migrated, "/v1/get",
                         {"unit_id": unit_id,
                          "scope": {"org": ORG, "space": PROJECT}})
    assert status == 200 and unit, unit

    # A regular member (no governance) cannot manage members. recover-admin must
    # change that through the same space component, not merely report success.
    limited_actor = Scope(org=ORG, space=PROJECT, agent="limited-agent")
    limited_token = issue_token()
    _register(target, "limited-install", limited_token, limited_actor)
    limited_dir = target.installs_dir / "limited-install"
    limited_dir.mkdir(parents=True, exist_ok=True)
    (limited_dir / "install.json").write_text(
        json.dumps(
            {"install_id": "limited-install", "host": "jiuwenswarm",
             "connector_id": "limited-conn",
             "actor": {"org": ORG, "space": PROJECT, "user": "",
                       "agent": "limited-agent", "session": ""},
             "project": PROJECT, "capture": "task", "state": "connected"}
        ),
        encoding="utf-8",
    )
    status, added = _post(
        migrated,
        "/v1/add_space_member",
        {"org": ORG, "space": PROJECT,
         "member": {"scope": {"org": ORG, "space": PROJECT,
                              "agent": "limited-agent"}}},
    )
    assert status == 200, added
    limited = ServiceClient(discovery_module.ensure_service(target), limited_token)
    status, _body = _post(
        limited,
        "/v1/add_space_member",
        {"org": ORG, "space": PROJECT,
         "member": {"scope": {"org": ORG, "space": PROJECT,
                              "agent": "recovered-probe"}}},
    )
    assert status == 403, "a non-manager must not manage members before recovery"

    result = recover_admin(target, PROJECT, "limited-install", confirm=lambda _text: True)
    assert result["state"] == "complete", result
    status, added = _post(
        limited,
        "/v1/add_space_member",
        {"org": ORG, "space": PROJECT,
         "member": {"scope": {"org": ORG, "space": PROJECT,
                              "agent": "recovered-probe"}}},
    )
    assert status == 200, added
