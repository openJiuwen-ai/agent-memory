# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI and HTTP use identical MemoryAPI arguments and original JSON results."""

from __future__ import annotations

import argparse
import inspect
import io
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
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
from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

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
        if name == "source":
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


@pytest.fixture(params=["local", "http"])
def api_client(request, monkeypatch):
    local = client_module.InProcessClient(authenticator=build_dev_authenticator())
    if request.param == "local":
        try:
            yield local
        finally:
            local.close()
        return
    server = HttpServer(
        local.server.config, local.server, security_runtime=build_dev_security_runtime()
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


def test_dev_authentication_still_checks_business_permissions(api_client) -> None:
    status, body = api_client.call("add", {"content": "x", "scope": {"org": "another-org"}})
    assert status == 403
    assert body["error"] == "PermissionDeniedError"


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
