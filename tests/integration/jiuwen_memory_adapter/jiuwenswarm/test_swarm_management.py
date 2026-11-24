"""Verify the Swarm application loader, browser RPC, and owner-only configuration."""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from time import time
from uuid import uuid4

import pytest

pytest.importorskip("agent_gateway")

from agent_gateway.auth import service
from agent_gateway.auth.account_kit import Credential, LoginOutcome
from agent_gateway.auth.session_store import AuthSessionStore
from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from agent_gateway.common import paths
from agent_gateway.extensions.loader import ExtensionLoader
from agent_gateway.extensions.registry import ExtensionRegistry
from fastapi.testclient import TestClient

from jiuwen_memory_adapter.agent_security.runtime import SecurityDenied
from jiuwen_memory_adapter.jiuwenswarm import security_control as swarm_control
from jiuwen_memory_adapter.jiuwenswarm.security_policy import SwarmPolicySettings

pytestmark = pytest.mark.integration
ASSETS = (
    Path(__file__).resolve().parents[4]
    / "jiuwen_memory_adapter/jiuwenswarm"
)
ORIGIN = "http://testserver"


def test_swarm_installed_management_uses_owner_login(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    monkeypatch.setenv("MINDVAULT_SWARM_OWNER_ID", "swarm-native-owner")
    monkeypatch.setenv("MINDVAULT_SWARM_WEB_ORIGIN", ORIGIN)
    monkeypatch.setenv("LOGIN_AUTH_SIMULATE", "true")
    memory_config = tmp_path / "memory.yml"
    memory_config.write_text(
        "globals: {privacy_enabled: false, llm_api_key: synthetic-config-secret}\n",
        encoding="utf-8",
    )
    installation = tmp_path / "infra/mindvault/installation.json"
    installation.parent.mkdir(parents=True)
    installation.write_text(json.dumps({"memory_config": str(memory_config)}), encoding="utf-8")
    auth = AuthSessionStore(persist=False)
    monkeypatch.setattr(service, "_service", service.AuthService(flow=object(), store=auth))
    credential = Credential("synthetic-token-never-echo", "", time() + 900)
    owner = auth.create(LoginOutcome(credential, "swarm-native-owner", "Owner"))
    other = auth.create(LoginOutcome(credential, "different-user", "Other"))

    async def build():
        registry = ExtensionRegistry(None, {}, logging.getLogger(__name__))
        await ExtensionLoader(registry).load_extension(ASSETS)
        channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
        channel.on_message(lambda _: False)
        registry.bind_application_plugins(channel)
        return build_web_channel_app(channel)

    app = asyncio.run(build())
    store = swarm_control.SwarmProtectionStore(tmp_path / "infra/mindvault/control.sqlite3")

    def rpc(client, method, params, *, cookie=None, origin=ORIGIN):
        headers = {"origin": origin}
        if cookie is not None:
            headers["cookie"] = f"jiuwenswarm_auth={cookie}"
        key = uuid4().hex
        with client.websocket_connect("/ws", headers=headers) as ws:
            ws.send_json({"type": "req", "id": key, "method": f"plugin.mindvault.{method}",
                          "params": params})
            for _ in range(10):
                frame = ws.receive_json()
                if frame.get("id") == key:
                    break
            else:
                pytest.fail("Swarm Gateway did not answer the management RPC")
        return frame

    with TestClient(app) as client:
        items = client.get("/api/application-plugins").json()["plugins"]
        entry, = items
        assert entry["enabled"] and entry["nav_key"] == "app:mindvault"
        response = client.get(entry["entry_url"])
        assert response.status_code == 200 and 'role="switch"' in response.text
        assert response.text.count('role="switch"') == 1
        assert "敏感记忆使用保护" in response.text and "记忆写入脱敏" in response.text
        assert "记忆使用与扫描策略" in response.text and "敏感记忆扫描" in response.text
        assert rpc(client, "get", {})["code"] == "FORBIDDEN"
        assert rpc(client, "set", {"enabled": True, "revision": 0},
                   cookie=other.session_id)["code"] == "FORBIDDEN"
        assert rpc(client, "set", {"enabled": True, "revision": 0},
                   cookie=owner.session_id, origin="http://invalid.example")["code"] == "FORBIDDEN"
        initial = rpc(client, "get", {}, cookie=owner.session_id)["payload"]
        assert initial["status"] == "unavailable"
        assert initial["memory_write_redaction"] == {"configured": False, "effective": None}
        assert "synthetic-config-secret" not in repr(initial)
        assert memory_config.name not in repr(initial)

        memory_config.write_text("globals: {privacy_enabled: true}\n", encoding="utf-8")
        configured = rpc(client, "get", {}, cookie=owner.session_id)["payload"]
        assert configured["memory_write_redaction"] == {"configured": True, "effective": None}
        assert rpc(client, "set", {
            "enabled": True, "revision": 0, "memory_write_redaction": False,
        }, cookie=owner.session_id)["code"] == "BAD_REQUEST"

        store.publish("worker", (False, 0))
        assert rpc(client, "set", {"enabled": 1, "revision": 0},
                   cookie=owner.session_id)["code"] == "BAD_REQUEST"
        requested = rpc(client, "set", {"enabled": True, "revision": 0}, cookie=owner.session_id)
        assert requested["payload"]["status"] == "pending"
        assert requested["payload"]["effective"] is False
        assert rpc(client, "set", {"enabled": False, "revision": 0},
                   cookie=owner.session_id)["code"] == "STATE_CONFLICT"

        store.publish("worker", store.desired())
        active = rpc(client, "get", {}, cookie=owner.session_id)["payload"]
        assert active["status"] == "enabled" and active["effective"] is True
        assert credential.id_token not in repr(active) and owner.session_id not in repr(active)
        assert active["configuration"]["memory_backend"] == "jiuwen"
        assert active["memory_write_redaction"] == {"configured": True, "effective": True}

        rpc(client, "set", {"enabled": False, "revision": 1}, cookie=owner.session_id)
        store.publish("worker", store.desired())
        unchanged = rpc(client, "get", {}, cookie=owner.session_id)["payload"]
        assert unchanged["status"] == "disabled"
        assert unchanged["memory_write_redaction"] == active["memory_write_redaction"]
        assert memory_config.read_text(encoding="utf-8") == "globals: {privacy_enabled: true}\n"

        memory_config.write_text("globals: {privacy_enabled: false}\n", encoding="utf-8")
        changed = rpc(client, "get", {}, cookie=owner.session_id)["payload"]
        assert changed["memory_write_redaction"] == {"configured": False, "effective": True}
        store.release("worker")
        memory_config.write_text("globals: [", encoding="utf-8")
        invalid = rpc(client, "get", {}, cookie=owner.session_id)["payload"]
        assert invalid["memory_write_redaction"] == {"configured": None, "effective": None}
        policy = invalid["policy"]
        assert policy["model"] and policy["tools"] and policy["agents"]
        assert {entry["id"] for entry in invalid["categories"]} == set(policy["categories"])
        policy["tools"] = False
        policy["agents"] = False
        policy["categories"]["pii.phone_cn"] = {"enabled": False, "level": "S3"}
        params = {"policy": policy, "revision": invalid["revision"]}
        assert rpc(client, "policy.set", params, cookie=other.session_id)["code"] == "FORBIDDEN"
        assert rpc(client, "policy.set", {**params, "enabled": True},
                   cookie=owner.session_id)["code"] == "BAD_REQUEST"
        saved = rpc(client, "policy.set", params, cookie=owner.session_id)["payload"]
        assert saved["policy"] == policy and saved["desired"] is False
        assert saved["revision"] == invalid["revision"] + 1
        stale = rpc(client, "policy.set", params, cookie=owner.session_id)
        assert stale["code"] == "STATE_CONFLICT"
        assert store.snapshot()[2].mapping() == policy
        assert client.get(entry["entry_url"]).status_code == 200
        auth.remove(owner.session_id)
        assert rpc(client, "set", {"enabled": True, "revision": saved["revision"]},
                   cookie=owner.session_id)["code"] == "FORBIDDEN"


def test_swarm_worker_recovers_from_contention_and_sqlite_error(tmp_path, monkeypatch):
    store = swarm_control.SwarmProtectionStore(tmp_path / "control.sqlite3")
    store.set_enabled(True, 0)
    store.publish("previous-worker", (True, 1))
    runtime = swarm_control.SwarmProtectionRuntime(store)
    monkeypatch.setattr(swarm_control, "_HEARTBEAT_SECONDS", 0.01)
    publish = store.publish
    fail = False

    def publish_snapshot(worker, snapshot):
        if fail:
            raise sqlite3.OperationalError("test storage unavailable")
        publish(worker, snapshot)

    monkeypatch.setattr(store, "publish", publish_snapshot)

    def enabled():
        try:
            return runtime.enabled
        except SecurityDenied:
            return None

    async def wait_for_state(expected):
        async with asyncio.timeout(3):
            while enabled() is not expected:
                await asyncio.sleep(0.01)

    async def check():
        nonlocal fail
        await runtime.start(lambda: True)
        try:
            with pytest.raises(SecurityDenied, match="保护状态不可用"):
                runtime.enabled
            store.release("previous-worker")
            await wait_for_state(True)
            fail = True
            store.set_enabled(False, 1)
            await wait_for_state(None)
            assert store.public()["status"] == "pending"
            fail = False
            await wait_for_state(False)
            assert store.public()["status"] == "disabled"
        finally:
            await runtime.aclose()
        assert store.public()["status"] == "unavailable"

    asyncio.run(check())


def test_swarm_detector_skips_disabled_categories_and_uses_configured_level():
    settings = SwarmPolicySettings.defaults().mapping()
    settings["categories"]["pii.phone_cn"]["enabled"] = False
    settings["categories"]["pii.email"]["level"] = "S3"
    inspector = SwarmPolicySettings.parse(settings).inspector()
    inspected = inspector.inspect("电话 13800138000，邮箱 test@example.com")
    assert inspected.categories == ("pii.email",) and inspected.level_id == "S3"
    assert "13800138000" in inspected.redacted and "test@example.com" not in inspected.redacted


@pytest.mark.parametrize(("base", "endpoint"), [
    ("https://MODEL.example/v1/", "https://model.example/v1/chat/completions"),
    ("https://model.example/v1/chat/completions", "https://model.example/v1/chat/completions"),
    ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1/chat/completions"),
    ("http://[::1]:8080/v1", "http://[::1]:8080/v1/chat/completions"),
    ("http://model.example/v1", None),
    ("", None),
])
def test_swarm_worker_validates_authenticated_model_address(monkeypatch, base, endpoint):
    from jiuwenswarm.common import config

    from jiuwen_memory_adapter.jiuwenswarm import security_worker

    monkeypatch.setattr(config, "get_default_models", lambda: [{
        "model_client_config": {"model_name": "fixture", "api_base": base, "api_key": "fixture"},
    }])
    resolve = getattr(security_worker, "_swarm_model")
    if endpoint is None:
        with pytest.raises(SecurityDenied):
            resolve()
    else:
        target, key = resolve()
        assert target.endpoint == endpoint and key == "fixture"
