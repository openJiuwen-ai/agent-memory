# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI and HTTP use identical MemoryAPI arguments and original JSON results."""

from __future__ import annotations

import argparse
import http.client
import inspect
import io
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import MemoryAPI, Scope, build_dev_authenticator
from jiuwen_memory.common.security.request_context import get_request_id
from jiuwen_memory.common.security.types import get_current
from jiuwen_memory_entry.cli import __main__ as cli
from jiuwen_memory_entry.cli import client as client_module
from jiuwen_memory_entry.cli import commands
from jiuwen_memory_entry.core import api_contract
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from tests.unit.jiuwen_memory_entry.fixtures import (
    DEV_MANAGEMENT_CASES,
    fixed_dev_management_config,
    seed_dev_management_space,
    upstream_api_error,
)

pytestmark = pytest.mark.unit
SCOPE = {"org": "local", "user": "developer"}


@pytest.mark.parametrize("method", sorted(MemoryAPI.__abstractmethods__))
def test_cli_exposes_every_api_parameter(method: str, capsys, monkeypatch) -> None:
    registered = []
    recorder = SimpleNamespace(
        add_argument=lambda *flags, **options: registered.append((flags, options))
    )
    commands.add_api_arguments(recorder, method)
    actual = {
        options["dest"].removeprefix("api_"): (flags, options)
        for flags, options in registered
        if options.get("dest", "").startswith("api_")
    }
    parameters = {
        name: p
        for name, p in inspect.signature(getattr(MemoryAPI, method)).parameters.items()
        if name not in {"self", "security"}
    }

    assert set(actual) == set(parameters)
    for name, parameter in parameters.items():
        flags, options = actual[name]
        assert flags == (f"--{name}",)
        assert options["required"] == (parameter.default is inspect.Parameter.empty)
        assert options["default"] == argparse.SUPPRESS

    exit_mock = Mock(side_effect=RuntimeError("parser exit"))
    monkeypatch.setattr(argparse.ArgumentParser, "exit", exit_mock)
    with pytest.raises(RuntimeError, match="^parser exit$"):
        cli.build_parser().parse_args([method, "--help"])
    exit_mock.assert_called_once_with()
    help_text = capsys.readouterr().out
    assert all(f"--{name} " in help_text for name in parameters)


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("add", {"content": "hello", "scope": SCOPE, "source": "text", "tags": ["a"]}),
        ("search", {"query": "hello", "context": {"scope": SCOPE}, "top_k": 3}),
        (
            "search",
            {"query": "hello", "context": {"scope": SCOPE}, "hierarchy_kind": "time", "top_k": 3},
        ),
        ("get", {"unit_id": "u1", "scope": SCOPE}),
        ("update", {"unit_id": "u1", "scope": SCOPE, "patch": {"content": "new"}}),
        ("delete", {"selector": {"scope": SCOPE, "unit_ids": ["u1"], "mode": "purge"}}),
        (
            "batch_add_async",
            {"items": [{"content": "hello"}], "scope": SCOPE, "continue_on_error": False},
        ),
        ("admin_set", {"key": "test", "value": "false"}),
        ("list", {"scope": SCOPE, "extensions": None}),
    ],
)
def test_cli_builds_same_request_without_defaults(method: str, payload: dict) -> None:
    contract = api_contract.method_contract(method)
    argv = [method]
    for name, value in payload.items():
        encoded = value if contract.type_hints[name] is str else json.dumps(value)
        # Enum parameters use their string values on the command line.
        if name in {"source", "hierarchy_kind"}:
            encoded = value
        argv.extend([f"--{name}", encoded])
    args = cli.build_parser().parse_args(argv)
    assert commands.build_payload(method, args) == payload


@pytest.mark.parametrize("old_option", ["--tenant", "--item-id", "--modality", "--k", "-u"])
def test_cli_rejects_legacy_options(old_option: str, monkeypatch) -> None:
    exit_mock = Mock(side_effect=RuntimeError("parser exit"))
    monkeypatch.setattr(argparse.ArgumentParser, "exit", exit_mock)
    with pytest.raises(RuntimeError, match="^parser exit$"):
        cli.build_parser().parse_args(
            ["add", "--content", "x", "--scope", json.dumps(SCOPE), old_option, "old"]
        )
    exit_mock.assert_called_once()
    status, message = exit_mock.call_args.args
    assert status == 2
    assert f"unrecognized arguments: {old_option} old" in message


@pytest.mark.parametrize("body", [None, [], "job-1", {"items": [], "count": 0}])
def test_http_client_preserves_payload_and_original_response(monkeypatch, body) -> None:
    seen = []

    class Response(io.BytesIO):
        status = 200

    def urlopen(request, *, timeout):
        seen.append((request, timeout))
        return Response(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    payload = {"scope": SCOPE, "limit": 2}
    client = client_module.HttpClient("http://example.test", api_key="test-key")
    status, result = client.call("list", payload)

    assert status == 200
    assert result == body
    assert seen[0][0].full_url == "http://example.test/v1/list"
    assert json.loads(seen[0][0].data) == payload
    assert seen[0][0].get_header("Authorization") == "Bearer test-key"
    assert seen[0][1] == 300.0


def test_http_client_uses_configured_timeout(monkeypatch) -> None:
    seen = []

    class Response(io.BytesIO):
        status = 200

    def urlopen(request, *, timeout):
        seen.append(timeout)
        return Response(b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = client_module.make_client("http://example.test", timeout=451.5)

    assert client.call("list", {"scope": SCOPE}) == (200, [])
    assert seen == [451.5]


def test_cli_timeout_precedence_and_client_forwarding(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_TIMEOUT", "420")

    seen = {}

    class Client:
        @staticmethod
        def healthz():
            return 200, {"status": "ok"}

        @staticmethod
        def close() -> None:
            return None

    def make_client(*args, **kwargs):
        seen.update(kwargs)
        return Client()

    monkeypatch.setattr(cli, "make_client", make_client)

    assert cli.main(["--server", "http://example.test", "healthz"]) == 0
    assert seen["timeout"] == 420.0
    assert cli.main(["--server", "http://example.test", "--timeout", "510.5", "healthz"]) == 0
    assert seen["timeout"] == 510.5


def test_cli_flag_overrides_invalid_env_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_TIMEOUT", "bad")
    seen = {}

    class Client:
        @staticmethod
        def healthz():
            return 200, {"status": "ok"}

        @staticmethod
        def close() -> None:
            return None

    def make_client(*args, **kwargs):
        seen.update(kwargs)
        return Client()

    monkeypatch.setattr(cli, "make_client", make_client)

    assert cli.main(["--server", "http://example.test", "--timeout", "12", "healthz"]) == 0
    assert seen["timeout"] == 12.0


def test_cli_rejects_invalid_env_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_TIMEOUT", "abc")
    monkeypatch.setattr(cli, "make_client", Mock(side_effect=AssertionError("client built")))
    exit_mock = Mock(side_effect=RuntimeError("parser exit"))
    monkeypatch.setattr(argparse.ArgumentParser, "exit", exit_mock)

    with pytest.raises(RuntimeError, match="^parser exit$"):
        cli.main(["--server", "http://example.test", "healthz"])

    exit_mock.assert_called_once()
    assert exit_mock.call_args.args[0] == 2
    expected = "AGENT_MEMORY_TIMEOUT must be a finite positive number: 'abc'"
    assert expected in exit_mock.call_args.args[1]


def test_cli_local_mode_ignores_env_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_TIMEOUT", "abc")

    assert cli.main(["--auth-mode", "dev", "healthz"]) == 0


def test_cli_rejects_timeout_without_remote_server(monkeypatch) -> None:
    exit_mock = Mock(side_effect=RuntimeError("parser exit"))
    monkeypatch.setattr(argparse.ArgumentParser, "exit", exit_mock)
    with pytest.raises(RuntimeError, match="^parser exit$"):
        cli.main(["--timeout", "5", "--auth-mode", "dev", "healthz"])

    exit_mock.assert_called_once()
    assert exit_mock.call_args.args[0] == 2
    assert "--timeout applies only to remote execution" in exit_mock.call_args.args[1]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
def test_cli_rejects_invalid_timeout(value, monkeypatch) -> None:
    exit_mock = Mock(side_effect=RuntimeError("parser exit"))
    monkeypatch.setattr(argparse.ArgumentParser, "exit", exit_mock)
    with pytest.raises(RuntimeError, match="^parser exit$"):
        cli.build_parser().parse_args(["--timeout", value, "healthz"])

    exit_mock.assert_called_once()
    assert exit_mock.call_args.args[0] == 2


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "bad"])
def test_client_rejects_invalid_programmatic_timeout(value) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        client_module.HttpClient("http://example.test", timeout=value)
    with pytest.raises(ValueError, match="finite positive"):
        client_module.make_client("http://example.test", timeout=value)


@pytest.fixture
def slow_http_server(monkeypatch):
    """Start a real HTTP server whose response arrives after one second."""
    delay = 1.0

    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            time.sleep(delay)
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    # BaseHTTPRequestHandler dispatches GET requests through this exact attribute.
    setattr(Handler, "do_GET", Handler.handle_get)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_http_client_reports_timeout_as_connection_failure(slow_http_server) -> None:
    status, body = client_module.HttpClient(slow_http_server, timeout=0.2).healthz()

    assert status == 0
    assert body["error"] == "ConnectionError"
    assert body["message"] == ("HTTP socket operation timed out (error=TimeoutError, timeout=0.2s)")


@pytest.mark.parametrize(
    "failure",
    [
        http.client.RemoteDisconnected("peer closed connection"),
        ConnectionResetError("connection reset"),
        http.client.IncompleteRead(b"partial", 20),
    ],
    ids=["remote-disconnected", "connection-reset", "incomplete-response"],
)
def test_http_client_maps_unwrapped_transport_failures(monkeypatch, failure) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=failure))

    status, body = client_module.HttpClient("http://example.test").healthz()

    assert status == 0
    assert body == {
        "error": "ConnectionError",
        "message": f"HTTP transport failed (error={type(failure).__name__})",
    }


def test_cli_exit_code_for_remote_timeout(slow_http_server, capsys) -> None:
    assert cli.main(["--server", slow_http_server, "--timeout", "0.2", "healthz"]) == 1
    stderr = capsys.readouterr().err
    assert "ConnectionError" in stderr
    assert "Traceback" not in stderr

    assert cli.main(["--server", slow_http_server, "--timeout", "5", "healthz"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}


@pytest.fixture(params=["local", "http"])
def api_client(request, monkeypatch):
    local = client_module.InProcessClient(dev_authentication=True)
    if request.param == "local":
        try:
            yield local
        finally:
            local.close()
        return
    server = HttpServer(
        local.server.config,
        local.server,
        security_runtime=local.server.security_runtime,
    )
    # HttpServer expects an object with api and close, both supplied by the existing Server.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)
    thread.start()
    try:
        yield client_module.HttpClient(f"http://127.0.0.1:{httpd.server_port}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        local.close()


def test_local_and_http_complete_same_api_crud(api_client) -> None:
    status, units = api_client.call("add", {"content": "hello coffee", "scope": SCOPE})
    assert status == 200, units
    assert isinstance(units, list)
    unit = units[0]
    assert unit["segments"][0]["content"] == "hello coffee"
    assert "item_id" not in unit
    assert "content" not in unit
    unit_id = unit["id"]

    status, page = api_client.call("list", {"scope": SCOPE})
    assert status == 200, page
    assert page["items"][0]["id"] == unit_id

    status, found = api_client.call(
        "search", {"query": "coffee", "context": {"scope": SCOPE}, "top_k": 1}
    )
    assert status == 200, found
    assert found["items"][0]["unit_id"] == unit_id
    assert "hits" not in found

    status, updated = api_client.call(
        "update",
        {
            "unit_id": unit_id,
            "scope": SCOPE,
            "patch": {"content": "updated coffee", "mode": "overwrite"},
        },
    )
    assert status == 200, updated
    assert updated["segments"][0]["content"] == "updated coffee"
    status, fetched = api_client.call("get", {"unit_id": updated["id"], "scope": SCOPE})
    assert status == 200
    assert fetched == updated

    status, deleted = api_client.call(
        "delete", {"selector": {"scope": SCOPE, "unit_ids": [updated["id"]], "mode": "purge"}}
    )
    assert status == 200, deleted
    assert deleted == [updated["id"]]


def test_local_and_http_await_original_async_result(api_client) -> None:
    status, units = api_client.call("add_async", {"content": "async coffee", "scope": SCOPE})
    assert status == 200, units
    assert isinstance(units, list)
    assert units[0]["segments"][0]["content"] == "async coffee"
    status, batch = api_client.call(
        "batch_add_async", {"items": [{"content": "one"}, {"content": "two"}], "scope": SCOPE}
    )
    assert status == 200, batch
    assert "outcomes" in batch
    assert "job_id" not in batch


def test_inprocess_calls_api_with_independent_security(monkeypatch) -> None:
    client = client_module.InProcessClient(authenticator=build_dev_authenticator())
    seen = []

    def add(content, scope, *, security):
        seen.append((content, scope, security, get_request_id()))
        return []

    monkeypatch.setattr(client.server.api, "add", add)
    try:
        status, body = client.call("add", {"content": "x", "scope": {"org": "other"}})
        assert (status, body) == (200, [])
        assert seen[0][1] == Scope(org="other")
        assert seen[0][2].actor == Scope(org="local", user="developer")
        assert seen[0][2].surface.value == "cli"
        assert seen[0][2].request_id == seen[0][3]
        assert get_current() is None
        assert get_request_id() is None
    finally:
        client.close()


def test_inprocess_fails_closed_without_authentication() -> None:
    client = client_module.InProcessClient()
    try:
        status, body = client.call("add", {"content": "x", "scope": SCOPE})
        assert status == 503
        assert body["error"] == "SecurityUnavailable"
    finally:
        client.close()


@pytest.mark.parametrize("method", ["add", "search"])
@pytest.mark.parametrize("upstream_status", [429, 400, 500])
def test_inprocess_upstream_error_response(method: str, upstream_status: int, monkeypatch) -> None:
    client = client_module.InProcessClient(dev_authentication=True)
    error = upstream_api_error(upstream_status)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(client.server.api, method, fail)
    payload = (
        {"scope": SCOPE, "content": "x"}
        if method == "add"
        else {"context": {"scope": SCOPE}, "query": "x"}
    )
    limited = upstream_status == 429
    try:
        status, body = client.call(method, payload)
        assert status == (429 if limited else 500)
        assert body == {
            "error": "RateLimitError" if limited else "InternalError",
            "message": "too many requests" if limited else "internal server error",
            "retryable": limited,
            "request_id": body["request_id"],
        }
        assert body["request_id"]
        assert get_current() is None
        assert get_request_id() is None
    finally:
        client.close()


def test_default_dev_rejects_cross_org_business_flow(api_client) -> None:
    status, body = api_client.call("add", {"content": "x", "scope": {"org": "another-org"}})
    assert status == 403, body


@pytest.mark.parametrize("role", [None, "admin", "root"], ids=["default-user", "admin", "root"])
def test_cli_management_configuration_migration(role, tmp_path) -> None:
    config = tmp_path / "management-dev.json"
    config.write_text(json.dumps(fixed_dev_management_config(role)), encoding="utf-8")
    client = client_module.InProcessClient([str(config)], dev_authentication=True)
    try:
        seed_dev_management_space(client.server.api)
        for method, payload, allowed_roles in DEV_MANAGEMENT_CASES:
            status, body = client.call(method, payload)
            assert status == (200 if role in allowed_roles else 403), (method, body)
            if method == "verify_audit" and status == 200:
                assert body["status"] == "unsupported", body
    finally:
        client.close()


def test_cli_loads_explicit_root_dev_configuration(tmp_path) -> None:
    config = tmp_path / "root-dev.json"
    config.write_text(
        json.dumps(
            {
                "memory_api": {
                    "security": {
                        "default": {
                            "target": "standard",
                            "params": {
                                "authenticator": {
                                    "target": "dev",
                                    "params": {"role": "root"},
                                }
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    client = client_module.InProcessClient([str(config)], dev_authentication=True)
    try:
        status, body = client.call(
            "add", {"content": "explicit root", "scope": {"org": "another-org"}}
        )
        assert status == 200, body
    finally:
        client.close()


@pytest.mark.parametrize(
    ("method", "payload", "expected"),
    [
        ("admin_set", {"key": "test", "value": "false"}, None),
        ("evolve", {"scope": SCOPE, "mode": "extract"}, "job-1"),
    ],
)
def test_inprocess_preserves_original_returns(monkeypatch, method, payload, expected) -> None:
    client = client_module.InProcessClient(authenticator=build_dev_authenticator())
    monkeypatch.setattr(client.server.api, method, lambda **kwargs: expected)
    try:
        assert client.call(method, payload) == (200, expected)
    finally:
        client.close()


@pytest.mark.parametrize("option", [["--auth-mode", "dev"], ["--config", "local.yaml"]])
def test_remote_cli_rejects_local_runtime_options(monkeypatch, option) -> None:
    monkeypatch.setattr(cli, "make_client", lambda *args, **kwargs: pytest.fail("must not build"))
    exit_mock = Mock(side_effect=RuntimeError("parser exit"))
    monkeypatch.setattr(argparse.ArgumentParser, "exit", exit_mock)
    with pytest.raises(RuntimeError, match="^parser exit$"):
        cli.main(["--server", "http://example.test", *option, "healthz"])
    exit_mock.assert_called_once()
    status, message = exit_mock.call_args.args
    assert status == 2
    assert option[0] in message


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "x", "scope": "alice", "tenant_id": "local"},
        {"content": "x", "scope": SCOPE, "security": {}},
        {"content": "x", "scope": SCOPE, "actor": {}},
    ],
)
def test_both_clients_reject_old_payloads_and_identity(api_client, payload) -> None:
    status, body = api_client.call("add", payload)
    assert status == 400
    assert body["error"] == "ValidationError"


@pytest.mark.parametrize("body", [[], None, "job-1", {"items": [], "count": 0}])
def test_cli_json_output_is_original_result(capsys, body) -> None:
    result = commands.emit(200, body, SimpleNamespace(output="json", pretty=False))
    assert result == 0
    assert json.loads(capsys.readouterr().out) == body


def test_cli_batch_uses_api_parameters_and_keeps_session(monkeypatch, capsys) -> None:
    records = [
        {"op": "add", "content": "coffee", "scope": SCOPE},
        {"op": "list", "scope": SCOPE},
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(map(json.dumps, records))))
    result = cli.main(["--auth-mode", "dev", "batch"])
    lines = capsys.readouterr().out.splitlines()
    assert result == 0
    units, page = map(json.loads, lines)
    assert page["items"][0]["id"] == units[0]["id"]


def test_cli_closes_runtime_on_input_error(monkeypatch) -> None:
    closed = []
    fake = SimpleNamespace(
        call=lambda *args: pytest.fail("Invalid input must not reach the API"),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(cli, "make_client", lambda *args, **kwargs: fake)
    result = cli.main(["list", "--scope", '{"unknown":"field"}'])
    assert result == 2
    assert closed == [True]


def test_text_and_quiet_render_api_fields(capsys) -> None:
    body = [{"id": "u1", "segments": [{"content": "one"}, {"content": "two"}]}]
    args = SimpleNamespace(output="text", pretty=False)
    assert commands.emit(200, body, args) == 0
    assert capsys.readouterr().out == "u1  one\ntwo\n"
    args.output = "quiet"
    assert commands.emit(200, body, args) == 0
    assert capsys.readouterr().out == "u1\n"


# --- 协议与配置错误不得误报成功、不得吞掉错误详情 --------------------------------- #


def test_http_client_non_json_200_body_reports_failure(monkeypatch, capsys) -> None:
    """回归：HTTP 200 + 非 JSON 体曾沿用 200 状态，CLI 退出码 0 却输出 BadResponse。

    现在映射为 502（协议层错误）：退出码 1、错误体走 stderr。
    """

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, *, timeout: Response(b"<html>proxy</html>")
    )
    client = client_module.HttpClient("http://example.test")
    status, body = client.call("list", {"scope": SCOPE})

    assert status == 502
    assert body["error"] == "BadResponse"
    assert commands.emit(status, body, SimpleNamespace(output="json", pretty=False)) == 1
    assert "BadResponse" in capsys.readouterr().err


def test_http_client_non_json_error_body_keeps_original_status(monkeypatch) -> None:
    """HTTP 错误响应体非 JSON：保留原状态码（已是失败，信息更有用）。"""

    def urlopen(request, *, timeout):
        raise urllib.error.HTTPError(
            "http://example.test/v1/list", 503, "Service Unavailable", None, io.BytesIO(b"<html/>")
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = client_module.HttpClient("http://example.test")
    status, body = client.call("list", {"scope": SCOPE})

    assert status == 503
    assert body["error"] == "BadResponse"


def test_quiet_mode_still_shows_error_details(capsys) -> None:
    """回归：quiet 模式曾把错误体渲染成空行（认证失败、记忆不存在时无任何输出）。"""
    body = {"error": "PermissionDeniedError", "message": "authentication failed"}
    result = commands.emit(403, body, SimpleNamespace(output="quiet", pretty=False))

    assert result == 1
    err = capsys.readouterr().err
    assert "PermissionDeniedError" in err
    assert "authentication failed" in err


def test_cli_reports_corrupt_config_layer_without_traceback(tmp_path, capsys) -> None:
    """回归：损坏的 --config 层曾以 JSONDecodeError traceback 崩溃。

    现在 load_layer 转成 ValidationError，main() 按 CliError 同口径输出一行
    错误、退出码 2。
    """
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = cli.main(
        ["--config", str(bad), "--auth-mode", "dev", "list", "--scope", json.dumps(SCOPE)]
    )

    assert result == 2
