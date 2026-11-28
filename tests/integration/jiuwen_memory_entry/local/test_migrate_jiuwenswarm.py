"""migrate jiuwenswarm: plan from old config, apply, roll back on failure (F17 D12)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import sqlite3

import pytest
import yaml

from jiuwen_memory.api import LocalTokenStore, Scope
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local import migrate as migrate_module
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home
from jiuwen_memory_entry.local.locks import acquire_file_lock, release_file_lock
from jiuwen_memory_entry.local.migrate import (
    MigrationError,
    apply_migration,
    plan_migration,
    recover_admin,
)

pytestmark = pytest.mark.integration

MODEL = ModelSettings(llm_base_url="https://api.deepseek.com", llm_model="m")
DISCOVERY = discovery_module.Discovery(
    pid=1, port=1, protocol=1, instance_nonce="n", home="/h", started_at=0.0
)


class FakeClient:
    responses: dict[str, tuple[int, dict]] = {}

    def __init__(self, discovery, token):
        self.token = token

    def post(self, path, payload, **kwargs):
        if path in self.responses:
            return self.responses[path]
        if path == "/v1/search":
            return 200, {"items": [{"unit_id": "u1"}]}
        if path == "/v1/continuity_task":
            request = payload.get("request") or {}
            operation = request.get("operation")
            if operation == "create":
                return 200, {"task_id": "probe-task", "revision": 1}
            if operation == "bind":
                return 200, {
                    "revision": 2,
                    "task": {
                        "revision": 2,
                        "bindings": [{"binding_id": "probe-binding",
                                      "connector_id": request.get("connector_id")}],
                    },
                }
        return 200, {}

    @staticmethod
    def reload():
        pass


@pytest.fixture
def home(tmp_path, monkeypatch) -> MemoryHome:
    memory_home = MemoryHome(tmp_path / "home")
    init_home(memory_home, document_root=tmp_path / "docs", model=MODEL)
    monkeypatch.setattr(migrate_module.discovery_module, "ensure_service",
                        lambda *a, **k: DISCOVERY)
    monkeypatch.setattr(migrate_module.discovery_module, "live_discovery",
                        lambda *a, **k: DISCOVERY)
    monkeypatch.setattr(migrate_module, "ServiceClient", FakeClient)
    FakeClient.responses = {}
    yield memory_home


def _config(tmp_path, *, with_actor=True, permission_db=None) -> None:
    block = {"space": "proj", "data_dir": "/old/data"}
    if with_actor:
        block.update({"user_id": "u1", "agent_id": "agent-1"})
    if permission_db:
        block["permission_db"] = str(permission_db)
    (tmp_path / "swarm.yaml").write_text(
        yaml.safe_dump({"memory": block}), encoding="utf-8"
    )


def _permission_db(path, scope: Scope) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE space_members (org TEXT, space TEXT, user TEXT, agent TEXT)"
    )
    conn.execute(
        "INSERT INTO space_members VALUES (?, ?, ?, ?)",
        (scope.org, scope.space, scope.user, scope.agent),
    )
    conn.commit()
    conn.close()


def test_plan_reads_actor_and_record_scopes_from_the_old_config(tmp_path):
    db = tmp_path / "perm.db"
    _permission_db(db, Scope(org="local", space="proj", user="u1", agent="agent-1"))
    _config(tmp_path, permission_db=db)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    assert plan.actor_source == "config"
    assert plan.actor.agent == "agent-1"
    assert plan.actor.space == "proj"
    assert [scope.agent for scope in plan.record_scopes] == ["agent-1"]
    assert plan.spaces_without_grants == []
    assert plan.storages["data_dir"] == "/old/data"


def test_plan_without_actor_is_unverified_and_lists_losses(tmp_path):
    _config(tmp_path, with_actor=False)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    assert plan.actor is None
    assert plan.actor_source == "unverified"
    assert any("unreachable" in loss for loss in plan.losses)


def test_host_export_wins_over_config(tmp_path):
    _config(tmp_path)
    export = tmp_path / "actor.json"
    export.write_text(
        '{"org": "local", "space": "proj", "user": "u2", "agent": "agent-2"}',
        encoding="utf-8",
    )
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=export)
    assert plan.actor_source == "host"
    assert plan.actor.agent == "agent-2"


def test_apply_writes_connector_and_install(home, tmp_path):
    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    report = apply_migration(home, plan, confirm=lambda _text: True)
    assert report.state == "complete", report.message
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    connectors = settings["memory_api"]["globals"]["continuity_connectors"]
    assert report.install_id
    assert any(name.startswith("jiuwenswarm-agent-1") for name in connectors)
    record = home.installs_dir / report.install_id / "install.json"
    assert record.exists()
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    assert store.principal_for_install(report.install_id).revoked is False
    store.close()


def test_apply_rolls_back_when_verification_fails(home, tmp_path):
    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    FakeClient.responses = {"/v1/search": (500, {})}
    with pytest.raises(MigrationError):
        apply_migration(home, plan, confirm=lambda _text: True)
    failed = home.service_config.with_name("service.yaml.failed")
    assert failed.exists(), "failed configuration must be kept for inspection"
    restored = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    connectors = (
        restored.get("memory_api", {}).get("globals", {}).get("continuity_connectors", {})
    )
    assert not any(name.startswith("jiuwenswarm-") for name in connectors)


def test_apply_waits_for_the_settings_lock(home, tmp_path, monkeypatch):
    """A concurrent connect owns service.yaml; migration must not read-modify-write it."""
    from jiuwen_memory_entry.local import install as install_module

    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    monkeypatch.setattr(install_module, "SETTINGS_LOCK_TIMEOUT", 0.1)
    handle = install_module._settings_lock(home)
    handle.__enter__()
    try:
        before = home.service_config.read_text(encoding="utf-8")
        with pytest.raises(Exception) as error:
            apply_migration(home, plan, confirm=lambda _text: True)
        assert "service.yaml" in str(error.value)
        assert home.service_config.read_text(encoding="utf-8") == before
        assert not home.service_config.with_name("service.yaml.migrating").exists()
    finally:
        handle.__exit__(None, None, None)


def test_apply_refuses_when_the_data_directory_is_locked(home, tmp_path):
    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    handle = acquire_file_lock(home.start_lock, timeout=1.0, conflict_message="locked")
    try:
        with pytest.raises(MigrationError):
            apply_migration(home, plan, confirm=lambda _text: True)
    finally:
        release_file_lock(handle)


def test_recover_admin_grants_manager_and_writes_audit(home, tmp_path, monkeypatch):
    monkeypatch.setattr(migrate_module.discovery_module, "live_discovery",
                        lambda *a, **k: None)
    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    report = apply_migration(home, plan, confirm=lambda _text: True)
    assert report.state == "complete", report.message
    result = recover_admin(home, "proj", report.install_id, confirm=lambda _text: True)
    assert result["state"] == "complete"
    audit = home.data_dir / "audit.log"
    assert audit.exists() and "recover_admin" in audit.read_text(encoding="utf-8")
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    assert store.principal_for_install(report.install_id).role.value == "user"
    store.close()


def test_recover_admin_can_be_aborted(home, tmp_path):
    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    report = apply_migration(home, plan, confirm=lambda _text: True)
    result = recover_admin(home, "proj", report.install_id, confirm=lambda _text: False)
    assert result["state"] == "aborted"
    assert not (home.data_dir / "audit.log").exists()


def test_apply_maps_legacy_storage_and_keeps_connector_and_full_actor(
    home, tmp_path, monkeypatch
):
    import json as jsonlib

    registry = tmp_path / "registry.db"
    conn = sqlite3.connect(str(registry))
    conn.execute(
        'CREATE TABLE kv (org TEXT, space TEXT, "user" TEXT, agent TEXT, session TEXT, '
        "key TEXT, value BLOB)"
    )
    task = {
        "task_id": "legacy-task",
        "owner": migrate_module._fingerprint(
            {"org": "local", "space": "proj", "user": "", "agent": "agent-1",
             "session": ""}
        ),
        "state": "open",
        "revision": 1,
        "bindings": [
            {"binding_id": "legacy-binding", "connector_id": "legacy-conn", "active": True}
        ],
        "background": [{"ref": {"unit_id": "legacy-unit"}}],
    }
    conn.execute(
        'INSERT INTO kv VALUES (?, ?, ?, ?, ?, ?, ?)',
        ("local", "proj", "", "agent-1", "", "/continuity/tasks/legacy-task",
         jsonlib.dumps(task)),
    )
    conn.commit()
    conn.close()
    docs = tmp_path / "legacy-docs"
    docs.mkdir()
    (tmp_path / "swarm.yaml").write_text(
        yaml.safe_dump(
            {
                "memory": {
                    "user_id": "",
                    "agent_id": "agent-1",
                    "space": "proj",
                    "registry_store": str(registry),
                    "document_root": str(docs),
                    "connectors": {
                        "legacy-conn": {
                            "actor": {"org": "local", "space": "proj", "user": "",
                                      "agent": "agent-1", "session": ""},
                            "projects": ["proj"], "enabled": True,
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    assert plan.connectors == ["legacy-conn"]
    assert plan.samples and plan.samples[0]["task_id"] == "legacy-task"
    assert plan.samples[0]["bindings"][0]["binding_id"] == "legacy-binding"

    owner = migrate_module._fingerprint(
        {"org": plan.actor.org, "space": plan.actor.space, "user": plan.actor.user,
         "agent": plan.actor.agent, "session": plan.actor.session}
    )

    def post(path, payload, **kwargs):
        if path == "/v1/continuity_task":
            request = payload["request"]
            if request["operation"] == "create":
                return 200, {"task_id": "probe-task", "revision": 1}
            if request["operation"] == "bind":
                return 200, {"revision": 2, "task": {"revision": 2, "bindings": [
                    {"binding_id": "probe-binding",
                     "connector_id": request["connector_id"]}]}}
            return 200, {"owner": owner, "task_id": request["task_id"]}
        if path == "/v1/get" or path == "/v1/add":
            return 200, [{"id": "u"}] if path == "/v1/add" else {"unit_id": "u"}
        return 200, {}

    def fake_post(_self, path, payload, **kwargs):
        return post(path, payload, **kwargs)

    monkeypatch.setattr(FakeClient, "post", fake_post)
    report = apply_migration(home, plan, confirm=lambda _text: True)
    assert report.state == "complete", report.message
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    api = settings["memory_api"]
    assert api["markdown_store"]["default"]["params"]["root"] == str(docs)
    assert api["kv_store"]["registry"]["params"]["db_path"] == str(registry)
    connector = api["globals"]["continuity_connectors"]["legacy-conn"]
    assert connector["actor"]["space"] == "proj"
    record = jsonlib.loads(
        (home.installs_dir / report.install_id / "install.json").read_text(encoding="utf-8")
    )
    assert record["connector_id"] == "legacy-conn"
    assert record["actor"]["space"] == "proj"


def test_verify_rejects_an_empty_read_when_no_samples_exist():
    class Empty:
        @staticmethod
        def post(path, payload, **kwargs):
            return 200, {"items": []}

    plan = plan_migration.__globals__["MigrationPlan"](
        actor=Scope(org="local", space="proj", agent="a"),
        actor_source="config",
        record_scopes=[Scope(org="local", space="proj", agent="a")],
        connectors=[],
        owned_tasks={},
        spaces_without_grants=[],
        storages={},
        losses=[],
    )
    with pytest.raises(MigrationError):
        migrate_module._verify(
            Empty(), plan, "install-1", plan.actor, "legacy-conn", "proj"
        )


def test_recover_admin_leaves_no_admin_rows(home, tmp_path, monkeypatch):
    monkeypatch.setattr(migrate_module.discovery_module, "live_discovery",
                        lambda *a, **k: None)
    _config(tmp_path)
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    report = apply_migration(home, plan, confirm=lambda _text: True)
    recover_admin(home, "proj", report.install_id, confirm=lambda _text: True)
    conn = sqlite3.connect(str(home.data_dir / "tokens.db"))
    try:
        admins = conn.execute(
            "SELECT install_id FROM install_tokens WHERE role = 'admin' AND revoked = 0"
        ).fetchall()
        one_off = conn.execute(
            "SELECT install_id FROM install_tokens WHERE install_id LIKE 'recover-admin-%'"
        ).fetchall()
        principal = conn.execute(
            "SELECT role, revoked FROM install_tokens WHERE install_id = ?",
            (report.install_id,),
        ).fetchone()
    finally:
        conn.close()
    assert admins == []
    assert one_off == []
    assert principal == ("user", 0)


def test_apply_preserves_each_legacy_connector_mapping(home, tmp_path):
    (tmp_path / "swarm.yaml").write_text(
        yaml.safe_dump(
            {"memory": {
                "user_id": "", "agent_id": "codex-agent", "space": "proj",
                "connectors": {
                    "codex-conn": {
                        "actor": {"org": "local", "space": "proj",
                                  "agent": "codex-agent"},
                        "projects": ["proj"], "enabled": True,
                    },
                    "swarm-conn": {
                        "actor": {"org": "local", "space": "other",
                                  "agent": "swarm-agent"},
                        "projects": ["other"], "enabled": False,
                    },
                },
            }}
        ),
        encoding="utf-8",
    )
    plan = plan_migration(tmp_path / "swarm.yaml", actor_export=None)
    assert plan.connector_specs["swarm-conn"]["projects"] == ["other"]
    report = apply_migration(home, plan, confirm=lambda _text: True)
    assert report.state == "complete", report.message
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    connectors = settings["memory_api"]["globals"]["continuity_connectors"]
    assert connectors["codex-conn"]["actor"]["agent"] == "codex-agent"
    assert connectors["codex-conn"]["projects"] == ["proj"]
    assert connectors["swarm-conn"]["actor"]["agent"] == "swarm-agent"
    assert connectors["swarm-conn"]["projects"] == ["other"]
    assert connectors["swarm-conn"]["enabled"] is False
