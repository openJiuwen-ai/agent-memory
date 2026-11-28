"""Discovery file and service environment (F17 D2 / D6)."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jiuwen_memory_entry.local.discovery import (
    PROTOCOL_VERSION,
    Discovery,
    clear_discovery,
    live_discovery,
    probe,
    read_discovery,
    service_env,
    write_discovery,
)
from jiuwen_memory_entry.local.home import MemoryHome

pytestmark = pytest.mark.unit


def _home(tmp_path) -> MemoryHome:
    home = MemoryHome(tmp_path / "home")
    home.run_dir.mkdir(parents=True)
    return home


def _discovery(**overrides) -> Discovery:
    values = {
        "pid": 42,
        "port": 5000,
        "protocol": PROTOCOL_VERSION,
        "instance_nonce": "nonce-a",
        "home": "/tmp/home",
        "started_at": 1.0,
    }
    values.update(overrides)
    return Discovery(**values)


def test_discovery_roundtrip_and_clear(tmp_path):
    home = _home(tmp_path)
    assert read_discovery(home) is None
    write_discovery(home, _discovery(home=str(home.root)))
    loaded = read_discovery(home)
    assert loaded is not None and loaded.instance_nonce == "nonce-a"
    if os.name != "nt":
        assert home.discovery_file.stat().st_mode & 0o777 == 0o600
    clear_discovery(home, nonce="other")
    assert read_discovery(home) is not None, "nonce mismatch must not delete the file"
    clear_discovery(home, nonce="nonce-a")
    assert read_discovery(home) is None


def test_malformed_discovery_is_ignored(tmp_path):
    home = _home(tmp_path)
    home.discovery_file.write_text("{not json", encoding="utf-8")
    assert read_discovery(home) is None
    home.discovery_file.write_text(json.dumps({"pid": "x"}), encoding="utf-8")
    assert read_discovery(home) is None


class _HealthStub:
    def __init__(self, payload: dict):
        stub = self
        self.payload = payload

        class Handler(BaseHTTPRequestHandler):
            def do_get(self):
                body = json.dumps(stub.payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: D401
                pass

        setattr(Handler, "do_GET", Handler.do_get)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_probe_requires_matching_nonce_and_home(tmp_path):
    home = _home(tmp_path)
    with _HealthStub(
        {"home": str(home.root.resolve()), "instance_nonce": "nonce-a", "protocol": 1}
    ) as stub:
        matching = _discovery(port=stub.port, home=str(home.root.resolve()))
        assert probe(matching) is True
        assert probe(
            _discovery(port=stub.port, home=str(home.root.resolve()), instance_nonce="other")
        ) is False
    with _HealthStub({"home": "/somewhere/else", "instance_nonce": "nonce-a"}) as stub:
        assert probe(
            _discovery(port=stub.port, home=str(home.root.resolve()), instance_nonce="nonce-a")
        ) is False


def test_service_env_inherits_only_declared_variables(tmp_path, monkeypatch):
    home = _home(tmp_path)
    home.service_config.write_text(
        "memory_api:\n  globals:\n    llm_api_key: '${JIUWEN_TEST_KEY}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWEN_TEST_KEY", "secret")
    monkeypatch.setenv("UNRELATED_VARIABLE", "must-not-leak")
    env = service_env(home)
    assert env["JIUWEN_TEST_KEY"] == "secret"
    assert "UNRELATED_VARIABLE" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_live_discovery_uses_probe(tmp_path):
    home = _home(tmp_path)
    with _HealthStub(
        {"home": str(home.root.resolve()), "instance_nonce": "nonce-a", "protocol": 1}
    ) as stub:
        write_discovery(
            home, _discovery(port=stub.port, home=str(home.root.resolve()))
        )
        assert live_discovery(home) is not None
    assert live_discovery(home) is None, "stale discovery must not count as live"


def test_failed_start_leaves_a_diagnosable_service_log(tmp_path, monkeypatch):
    """A start failure must keep the service's stderr instead of discarding it."""
    from jiuwen_memory_entry.local import discovery as discovery_module

    home = MemoryHome(tmp_path / "home")
    home.run_dir.mkdir(parents=True)
    home.service_config.write_text("memory_api: {}\n", encoding="utf-8")

    class _Dead:
        returncode = 1

        @staticmethod
        def poll():
            return 1

    def fake_spawn(home_arg, command_arg, kwargs, detached, allow_systemd_arg):
        os.write(kwargs["stderr"], b"Traceback: service failed to assemble\n")
        return _Dead(), detached

    monkeypatch.setattr(discovery_module, "_spawn", fake_spawn)
    with pytest.raises(discovery_module.ServiceUnavailable) as error:
        discovery_module.ensure_service(home, timeout=30)
    log = discovery_module.service_log_path(home)
    assert log.is_file(), "no service log was written"
    assert "Traceback" in log.read_text(encoding="utf-8")
    if os.name != "nt":
        assert log.stat().st_mode & 0o777 == 0o600
    assert str(log) in str(error.value)
