"""connect/disconnect: idempotent steps, merge-only host writes, revocation (F17 D5)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from jiuwen_memory.api import LocalTokenStore, token_digest
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local import install as install_module
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home
from jiuwen_memory_entry.local.hosts.codex import CodexHookWriter, CodexMcpWriter
from jiuwen_memory_entry.local.install import connect, disconnect

pytestmark = pytest.mark.unit

MODEL = ModelSettings(llm_base_url="https://api.deepseek.com", llm_model="m")
DISCOVERY = discovery_module.Discovery(
    pid=1,
    port=1,
    protocol=1,
    instance_nonce="nonce",
    home="/home",
    started_at=0.0,
)


class FakeClient:
    """Minimal service double: space registry plus status/reload bookkeeping."""

    spaces: dict[tuple[str, str], list[dict]] = {}
    calls: list[tuple[str, dict]] = []
    status_nonce = "nonce"
    allow_list = True
    reloads = 0

    def __init__(self, discovery, token: str) -> None:
        self.token = token

    def post(self, path: str, payload: dict, **kwargs):
        self.calls.append((path, payload))
        if path == "/v1/get_space":
            key = (payload["org"], payload["space"])
            return (200, {}) if key in self.spaces else (404, {"error": "NotFoundError"})
        if path == "/v1/create_space":
            spec = payload["spec"]
            key = (spec["org"], spec["space"])
            if key in self.spaces:
                return 409, {"error": "ConflictError"}
            self.spaces[key] = [{"scope": spec["owner"]}]
            return 200, {}
        if path == "/v1/list_space_members":
            if not self.allow_list:
                return 403, {"error": "PermissionDeniedError"}
            key = (payload["org"], payload["space"])
            return 200, {"members": self.spaces.get(key, [])}
        if path == "/v1/add_space_member":
            key = (payload["org"], payload["space"])
            member = payload["member"]
            bucket = self.spaces.setdefault(key, [])
            if member not in bucket:
                bucket.append(member)
            return 200, {}
        if path == "/local/status":
            return 200, {"instance_nonce": self.status_nonce, "reloading": False}
        return 200, {}

    @staticmethod
    def egress():
        return [
            {"component": "embedder.default", "destination": "https://e.example",
             "sends": ["query"]},
            {"component": "llm.default", "destination": "local", "sends": []},
        ]

    def status(self):
        return {"instance_nonce": self.status_nonce, "reloading": False}

    @staticmethod
    def reload():
        FakeClient.reloads += 1


@pytest.fixture
def home(tmp_path, monkeypatch) -> MemoryHome:
    memory_home = MemoryHome(tmp_path / "home")
    init_home(memory_home, document_root=tmp_path / "docs", model=MODEL)
    monkeypatch.setattr(install_module, "ServiceClient", FakeClient)
    monkeypatch.setattr(
        install_module.discovery_module, "ensure_service", lambda *a, **k: DISCOVERY
    )
    monkeypatch.setattr(
        install_module.discovery_module, "live_discovery", lambda *a, **k: DISCOVERY
    )
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(CodexMcpWriter, "verify", lambda self, install: True)
    monkeypatch.setattr(CodexHookWriter, "verify", lambda self, install: True)
    FakeClient.spaces = {}
    FakeClient.calls = []
    FakeClient.status_nonce = "nonce"
    FakeClient.allow_list = True
    FakeClient.reloads = 0
    yield memory_home


def _hooks(tmp_path) -> dict:
    return json.loads((tmp_path / "codex" / "hooks.json").read_text(encoding="utf-8"))


def test_connect_merges_host_entries_and_registers_connector(home, tmp_path):
    result = connect(home, "codex", confirm=lambda _text: True)
    assert result.state == "complete", result.message
    hooks = _hooks(tmp_path)
    commands = {
        item["command"] for item in hooks["hooks"]["UserPromptSubmit"]
    }
    assert any(result.install_id in command for command in commands)
    settings = install_module._settings(home)
    connectors = settings["memory_api"]["globals"]["continuity_connectors"]
    assert result.install_id in connectors
    actor = connectors[result.install_id]["actor"]
    # 主体名取 install id 的摘要而不是前缀：同宿主重装不能复用上一次的成员关系
    digest = hashlib.sha256(result.install_id.encode("utf-8")).hexdigest()[:16]
    assert actor["agent"] == f"codex-{digest}"
    assert connectors[result.install_id]["projects"] == ["default"]


def test_connect_is_resumable_and_idempotent(home, tmp_path):
    first = connect(home, "codex", confirm=lambda _text: True)
    second = connect(home, "codex", confirm=lambda _text: True)
    assert first.install_id == second.install_id
    hooks = _hooks(tmp_path)
    assert len(hooks["hooks"]["UserPromptSubmit"]) == 1, "duplicate hook entries"
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    token = (home.installs_dir / first.install_id / "token").read_text(encoding="utf-8").strip()
    assert store.digest_for(first.install_id) == token_digest(token)
    store.close()
    connectors = install_module._settings(home)["memory_api"]["globals"][
        "continuity_connectors"
    ]
    assert list(connectors) == [first.install_id]


def test_first_connect_creates_space_as_admin_then_demotes(home):
    result = connect(home, "codex", confirm=lambda _text: True)
    assert result.state == "complete", result.message
    assert ("local", "default") in FakeClient.spaces
    create_calls = [payload for path, payload in FakeClient.calls if path == "/v1/create_space"]
    assert create_calls, "space was not created"
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    principal = store.principal_for_install(result.install_id)
    store.close()
    assert principal is not None and principal.role.value == "user"
    conn = sqlite3.connect(str(home.data_dir / "tokens.db"))
    try:
        admins = conn.execute(
            "SELECT install_id FROM install_tokens WHERE revoked = 0 AND role = 'admin'"
        ).fetchall()
    finally:
        conn.close()
    assert admins == [], "an active ADMIN token was left behind"
    conn = sqlite3.connect(str(home.data_dir / "tokens.db"))
    try:
        one_off = conn.execute(
            "SELECT install_id FROM install_tokens WHERE install_id LIKE 'space-admin-%'"
        ).fetchall()
    finally:
        conn.close()
    assert one_off == [], "the one-off space admin credential was not purged"


def test_host_config_not_written_before_status_confirmation(home, tmp_path):
    FakeClient.status_nonce = "different"
    result = connect(home, "codex", confirm=lambda _text: True)
    assert result.state == "incomplete"
    assert not (tmp_path / "codex" / "hooks.json").exists()


def test_disconnect_revokes_token_and_rolls_back_entries(home, tmp_path):
    result = connect(home, "codex", confirm=lambda _text: True)
    assert result.state == "complete", result.message
    kept = disconnect(home, result.install_id)
    assert kept == []
    hooks = _hooks(tmp_path)
    assert hooks["hooks"]["UserPromptSubmit"] == [], "hook entry was not rolled back"
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    assert store.principal_for_install(result.install_id).revoked is True
    store.close()
    connectors = install_module._settings(home)["memory_api"]["globals"].get(
        "continuity_connectors", {}
    )
    assert result.install_id not in connectors


def test_concurrent_connects_keep_both_connections(home, tmp_path, monkeypatch):
    from jiuwen_memory_entry.local.hosts.dsh import DshHookWriter, DshMcpWriter

    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path / "dsh"))
    monkeypatch.setattr(DshHookWriter, "merge", lambda self, install: [])
    monkeypatch.setattr(DshHookWriter, "verify", lambda self, install: True)
    monkeypatch.setattr(DshMcpWriter, "merge", lambda self, install: [])
    monkeypatch.setattr(DshMcpWriter, "verify", lambda self, install: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda host: connect(home, host, confirm=lambda _text: True),
                ("codex", "dsh"),
            )
        )
    assert [result.state for result in results] == ["complete", "complete"], results
    connectors = install_module._settings(home)["memory_api"]["globals"][
        "continuity_connectors"
    ]
    assert {result.install_id for result in results} <= set(connectors)
    assert len(connectors) == 2


def test_connector_update_is_serialized_by_a_file_lock(home, monkeypatch):
    from jiuwen_memory.api import ConflictError

    monkeypatch.setattr(install_module, "SETTINGS_LOCK_TIMEOUT", 0.2)
    install = install_module.step_install_id(
        home, "codex", capture="task", project=None, org="local"
    )
    handle = install_module._settings_lock(home)
    lock = handle.__enter__()
    try:
        with pytest.raises(ConflictError):
            install_module.step_connector(home, install)
    finally:
        handle.__exit__(None, None, None)
        del lock


def test_concurrent_connects_for_the_same_host_create_one_install(home):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: connect(home, "codex", confirm=lambda _text: True), range(2)
            )
        )
    assert {result.install_id for result in results} == {results[0].install_id}
    records = list(home.installs_dir.glob("*/install.json"))
    assert len(records) == 1


def test_egress_is_shown_before_host_configuration(home, tmp_path):
    seen = {"egress": False, "hooks_existed": None}

    def confirm(text):
        if "outbound data destinations" in text:
            seen["egress"] = True
            seen["hooks_existed"] = (tmp_path / "codex" / "hooks.json").exists()
            seen["reloads_before"] = FakeClient.reloads
        return True

    result = connect(home, "codex", confirm=confirm)
    assert result.state == "complete", result.message
    assert seen["egress"] is True
    assert seen["hooks_existed"] is False, "host config was written before the egress review"
    assert seen["reloads_before"] == 0, "the service reloaded before the egress review"
    assert FakeClient.reloads >= 1


def test_declining_the_final_confirmation_aborts_and_cleans_up(home, tmp_path):
    def confirm(text):
        return "continue and write host configuration" not in text

    result = connect(home, "codex", confirm=confirm)
    assert result.state == "aborted"
    assert FakeClient.spaces == {}, "space provisioning happened before confirmation"
    assert not (tmp_path / "codex" / "hooks.json").exists()
    assert not (tmp_path / "codex" / "config.toml").exists()
    connectors = install_module._settings(home)["memory_api"]["globals"].get(
        "continuity_connectors", {}
    )
    assert result.install_id not in connectors
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    assert store.principal_for_install(result.install_id).revoked is True
    store.close()
    record = json.loads(
        (home.installs_dir / result.install_id / "install.json").read_text(encoding="utf-8")
    )
    assert record["state"] == "disconnected"


def test_codex_mcp_config_uses_the_recorded_interpreter(home, tmp_path):
    import tomllib

    result = connect(home, "codex", confirm=lambda _text: True)
    assert result.state == "complete", result.message
    data = tomllib.loads((tmp_path / "codex" / "config.toml").read_text(encoding="utf-8"))
    recorded = home.interpreter_file.read_text(encoding="utf-8").strip()
    assert data["mcp_servers"]["jiuwen-memory"]["command"] == recorded


def test_actor_is_stable_per_install_and_distinct_between_installs():
    from jiuwen_memory_entry.local.install import _actor

    first = _actor("jiuwenswarm", "jiuwenswarm-aaaaaaaaaaaaaaaa", "local")
    same = _actor("jiuwenswarm", "jiuwenswarm-aaaaaaaaaaaaaaaa", "local")
    other = _actor("jiuwenswarm", "jiuwenswarm-bbbbbbbbbbbbbbbb", "local")
    assert first == same
    assert first.agent != other.agent
    assert first.agent.startswith("jiuwenswarm-") and len(first.agent) > len("jiuwenswarm-")


def test_host_verify_failure_rolls_back_the_written_entries(home, monkeypatch):
    from pathlib import Path

    from jiuwen_memory_entry.local.hosts import EntryRef

    install = install_module.step_install_id(
        home, "codex", capture="task", project="proj", org="local"
    )
    install_module.step_token(home, install)
    entry = EntryRef(path=Path("/tmp/fake"), section="hooks", value={"install_id": "x"})
    calls = []
    monkeypatch.setattr(CodexMcpWriter, "merge", lambda self, host: [entry])
    monkeypatch.setattr(CodexMcpWriter, "verify", lambda self, host: True)
    monkeypatch.setattr(CodexHookWriter, "merge", lambda self, host: [entry])
    monkeypatch.setattr(CodexHookWriter, "verify", lambda self, host: False)
    monkeypatch.setattr(
        CodexHookWriter, "rollback", lambda self, host, entries: calls.append(entries) or []
    )
    with pytest.raises(Exception, match="did not verify"):
        install_module.step_hosts(home, install)
    assert calls == [[entry]]


def test_reconnect_applies_an_explicit_capture_off(home, tmp_path):
    assert connect(home, "codex", capture="task", confirm=lambda _text: True).state == "complete"
    result = connect(
        home, "codex", capture="off", project="other", confirm=lambda _text: True
    )
    assert result.state == "complete", result.message
    install = install_module._find_install(home, "codex")
    assert install is not None
    assert install.capture == "off"
    assert install.project == "other"
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    assert settings["memory_api"]["globals"]["continuity_connectors"][
        install.connector_id
    ]["projects"] == ["other"]
