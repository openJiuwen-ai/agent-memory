"""Host sync gate: Hermes and WorkBuddy sample directories import idempotently."""

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
PROJECT = "host-sync-project"


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


def _make_home(tmp_path, host: str):
    home = MemoryHome(tmp_path / "home")
    init_home(home, document_root=tmp_path / "docs", model=MODEL)
    _local_only(home.service_config)
    source = tmp_path / host
    if host == "hermes":
        memories = source / "memories"
        memories.mkdir(parents=True)
        (memories / "MEMORY.md").write_text(
            "# Facts\n\n- user prefers dark mode\n", encoding="utf-8"
        )
        types = {"fact"}
    else:
        source.mkdir(parents=True)
        (source / "USER.md").write_text("user prefers dark mode\n", encoding="utf-8")
        types = {"profile"}
    install_id = f"{host}-sync"
    token = issue_token()
    directory = home.installs_dir / install_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    grant = new_grant([str(source)], types=types, target_space=PROJECT)
    (directory / "install.json").write_text(
        json.dumps(
            {
                "install_id": install_id,
                "host": host,
                "connector_id": install_id,
                "actor": {
                    "org": ORG, "space": "", "user": "",
                    "agent": f"{host}-sync", "session": "",
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
            Scope(org=ORG, agent=f"{host}-sync"), Role.ADMIN,
        )
    finally:
        store.close()
    return home, token


@pytest.fixture(params=["hermes", "workbuddy"])
def host_home(request, tmp_path):
    home, token = _make_home(tmp_path, request.param)
    yield home, token, request.param
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


def test_host_sample_sync_is_idempotent(host_home):
    home, token, host = host_home
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, token)
    status, space = client.post(
        "/v1/create_space",
        {"spec": {"org": ORG, "space": PROJECT,
                  "owner": {"org": ORG, "agent": f"{host}-sync"}}},
    )
    assert status == 200, space
    status, first = _apply(client)
    assert status == 200, first
    assert first["added"] >= 1, first
    status, second = _apply(client)
    assert status == 200, second
    assert second["added"] == 0, second
    assert second["skipped"] >= 1, second
    status, state = client.post("/local/sync/status", {})
    assert status == 200, state
    assert state["missing_from_source"] == [], state
    assert state["new_in_source"] == [], state

    observer_token = issue_token()
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    try:
        store.register(
            f"observer-{host}", token_digest(observer_token),
            Scope(org=ORG, agent=f"observer-{host}"), Role.USER,
        )
    finally:
        store.close()
    status, member = client.post(
        "/v1/add_space_member",
        {
            "org": ORG,
            "space": PROJECT,
            "member": {
                "scope": {"org": ORG, "agent": f"observer-{host}"},
                "content_role": "contributor",
                "governance_role": "none",
            },
        },
    )
    assert status == 200, member
    observer = ServiceClient(discovery, observer_token)
    status, found = observer.post(
        "/v1/search",
        {
            "query": "dark mode",
            "context": {"scope": {"org": ORG, "space": PROJECT}},
            "top_k": 5,
        },
    )
    assert status == 200, found
    assert found["items"], found
