"""Sync HTTP endpoints: preview, frozen plan execute, status, revoke cleanup."""

from __future__ import annotations

import json
import os
import signal
import time

import pytest
import yaml

from jiuwen_memory.api import LocalTokenStore, Role, Scope, issue_token, token_digest
from jiuwen_memory_entry.local.client import ServiceClient
from jiuwen_memory_entry.local.discovery import ensure_service, live_discovery
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home
from jiuwen_memory_entry.local.sync.grant import new_grant

pytestmark = pytest.mark.integration

MODEL = ModelSettings(
    llm_base_url="https://api.deepseek.com", llm_model="m",
    llm_api_key_ref="env:KEY", embedder_base_url="http://x/v1",
    embedder_model="m", embedder_api_key_ref="env:KEY",
)
ORG = "local"
PROJECT = "sync-project"


def _local_only(service_config) -> None:
    settings = yaml.safe_load(service_config.read_text(encoding="utf-8"))
    api = settings["memory_api"]
    api["tokenizer"] = {"default": {"target": "whitespace"}}
    api["llm"] = {"default": "echo"}
    api["embedder"] = {
        "default": {"target": "hashing", "params": {"tokenizer": "default", "embedder_dim": 64}}
    }
    api["extractor"] = {
        "default": {"target": "keyword", "params": {"tokenizer": "default"}}
    }
    api["reranker"] = {
        "default": {"target": "overlap", "params": {"tokenizer": "default"}}
    }
    service_config.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")


@pytest.fixture
def sync_home(tmp_path):
    home = MemoryHome(tmp_path / "home")
    init_home(home, document_root=tmp_path / "docs", model=MODEL)
    _local_only(home.service_config)
    source = tmp_path / "codex"
    (source / "memories").mkdir(parents=True)
    (source / "memories" / "MEMORY.md").write_text(
        "# Facts\n\n- user prefers dark mode\n", encoding="utf-8"
    )
    install_id = "codex-sync"
    token = issue_token()
    directory = home.installs_dir / install_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    grant = new_grant([str(source)], types={"fact"}, target_space=PROJECT)
    (directory / "install.json").write_text(
        json.dumps(
            {
                "install_id": install_id,
                "host": "codex",
                "connector_id": install_id,
                "actor": {
                    "org": ORG, "space": "", "user": "",
                    "agent": "codex-sync", "session": "",
                },
                "project": PROJECT,
                "capture": "off",
                "state": "connected",
                "hooks": False,
                "sync": grant.as_dict(),
            }
        ),
        encoding="utf-8",
    )
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    try:
        store.register(
            install_id, token_digest(token),
            Scope(org=ORG, agent="codex-sync"), Role.ADMIN,
        )
    finally:
        store.close()
    yield home, token, source
    discovery = live_discovery(home)
    if discovery is not None:
        try:
            os.kill(discovery.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and live_discovery(home) is not None:
            time.sleep(0.1)


def _apply(client):
    status, preview = client.post("/local/sync", {"dry_run": True})
    assert status == 200, preview
    status, report = client.post("/local/sync", {"plan_id": preview["plan_id"]})
    return status, report


def test_sync_preview_execute_status_and_revoke(sync_home):
    home, token, source = sync_home
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {
            "spec": {
                "org": ORG,
                "space": PROJECT,
                "owner": {"org": ORG, "agent": "codex-sync"},
            }
        },
    )
    assert status == 200, space
    status, preview = client.post("/local/sync", {"dry_run": True})
    assert status == 200, preview
    assert preview["added"] == 2, preview
    assert preview["plan_id"]
    status, report = client.post("/local/sync", {"plan_id": preview["plan_id"]})
    assert status == 200, report
    assert report["added"] == 2, report
    status, recovered = client.post("/local/sync/recover", {})
    assert status == 200, recovered
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert state["missing_from_source"] == []
    assert state["new_in_source"] == []
    (source / "memories" / "MEMORY.md").write_text(
        "# Facts\n\n- user prefers dark mode\n- user likes tea\n", encoding="utf-8"
    )
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert len(state["new_in_source"]) == 1, state
    status, preview = client.post("/local/sync", {"dry_run": True})
    assert status == 200, preview
    assert preview["added"] == 1, preview
    status, report = client.post("/local/sync", {"plan_id": preview["plan_id"]})
    assert status == 200, report
    assert report["added"] == 1, report
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert state["new_in_source"] == []
    status, revoked = client.post("/local/sync/revoke", {"delete": "current"})
    assert status == 200, revoked
    assert revoked["deleted"] == [], revoked
    assert len(revoked["retained"]) == 3, revoked


def test_sync_mirror_delete_only_removed_item(sync_home):
    home, token, source = sync_home
    source_file = source / "memories" / "MEMORY.md"
    source_file.write_text(
        "# Facts\n\n- user prefers dark mode\n- user likes tea\n", encoding="utf-8"
    )
    record = home.installs_dir / "codex-sync" / "install.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["sync"]["mirror_deletes"] = True
    record.write_text(json.dumps(data), encoding="utf-8")
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {"spec": {"org": ORG, "space": PROJECT,
                  "owner": {"org": ORG, "agent": "codex-sync"}}},
    )
    assert status == 200, space
    status, report = _apply(client)
    assert status == 200, report
    assert report["added"] == 3, report
    source_file.write_text(
        "# Facts\n\n- user prefers dark mode\n", encoding="utf-8"
    )
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert len(state["missing_from_source"]) == 1, state
    status, deleted = _apply(client)
    assert status == 200, deleted
    assert len(deleted["deletes"]) == 1, deleted
    assert deleted["added"] == 0, deleted


def test_sync_resolve_discard_releases_pending_cleanup(sync_home):
    import sqlite3

    home, token, _source = sync_home
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {"spec": {"org": ORG, "space": PROJECT,
                  "owner": {"org": ORG, "agent": "codex-sync"}}},
    )
    assert status == 200, space
    status, report = _apply(client)
    assert status == 200, report
    connection = sqlite3.connect(home.data_dir / "local_sync.db")
    try:
        op_id = connection.execute(
            "SELECT op_id FROM sync_source ORDER BY rowid LIMIT 1"
        ).fetchone()[0]
        connection.execute("UPDATE sync_source SET state='writing'")
        connection.commit()
    finally:
        connection.close()
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert state["unconfirmed"], state
    status, resolved = client.post(
        "/local/sync/resolve", {"op": op_id, "action": "discard"}
    )
    assert status == 200, resolved
    assert resolved["changed"], resolved
    status, recovered = client.post("/local/sync/recover", {})
    assert status == 200, recovered
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert state["unconfirmed"] == [], state


def test_sync_frozen_plan_rejects_changed_source(sync_home):
    home, token, source = sync_home
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {"spec": {"org": ORG, "space": PROJECT,
                  "owner": {"org": ORG, "agent": "codex-sync"}}},
    )
    assert status == 200, space
    status, preview = client.post("/local/sync", {"dry_run": True})
    assert status == 200, preview
    (source / "memories" / "MEMORY.md").write_text(
        "# Facts\n\n- user prefers dark mode\n- user likes tea\n", encoding="utf-8"
    )
    status, changed = client.post("/local/sync", {"plan_id": preview["plan_id"]})
    assert status == 409, changed
    status, fresh = client.post("/local/sync", {"dry_run": True})
    assert status == 200, fresh
    assert fresh["added"] == 3, fresh


def test_sync_revoke_defers_delete_with_unconfirmed_operation(sync_home):
    import sqlite3

    home, token, _source = sync_home
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {"spec": {"org": ORG, "space": PROJECT,
                  "owner": {"org": ORG, "agent": "codex-sync"}}},
    )
    assert status == 200, space
    status, report = _apply(client)
    assert status == 200, report
    connection = sqlite3.connect(home.data_dir / "local_sync.db")
    try:
        connection.execute(
            "UPDATE sync_source SET state='writing', op_id='unknown-operation'"
        )
        connection.commit()
    finally:
        connection.close()
    status, revoked = client.post("/local/sync/revoke", {"delete": "current"})
    assert status == 200, revoked
    assert revoked["deleted"] == [], revoked
    assert revoked["retained"], revoked


def test_sync_double_unconfirmed_discard_waits_for_both(sync_home):
    import sqlite3

    home, token, _source = sync_home
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {"spec": {"org": ORG, "space": PROJECT,
                  "owner": {"org": ORG, "agent": "codex-sync"}}},
    )
    assert status == 200, space
    status, report = _apply(client)
    assert status == 200, report
    connection = sqlite3.connect(home.data_dir / "local_sync.db")
    try:
        rows = connection.execute(
            "SELECT item_id FROM sync_source ORDER BY rowid"
        ).fetchall()
        assert len(rows) >= 2, rows
        for index, (item_id,) in enumerate(rows):
            connection.execute(
                "UPDATE sync_source SET state='writing', op_id=? WHERE item_id=?",
                (f"unknown-{index}", item_id),
            )
        connection.commit()
    finally:
        connection.close()
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    ops = [entry["op"] for entry in state["unconfirmed"]]
    assert len(ops) == 2, state
    status, first = client.post("/local/sync/resolve", {"op": ops[0], "action": "discard"})
    assert status == 200, first
    status, recovered = client.post("/local/sync/recover", {})
    assert status == 200, recovered
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert len(state["unconfirmed"]) == 1, state
    status, second = client.post(
        "/local/sync/resolve", {"op": ops[1], "action": "discard"}
    )
    assert status == 200, second
    status, recovered = client.post("/local/sync/recover", {})
    assert status == 200, recovered
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert state["unconfirmed"] == [], state
