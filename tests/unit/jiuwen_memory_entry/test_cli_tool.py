# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CLI 面验证：契约锁、会话内闭环与辅助命令（重建 test_cli.py 消失后的验证证据）。

CLI 命令集由 ``MemoryAPI.__abstractmethods__`` 反射生成（``cli/__main__.py:42``），
本文件锁四件事：
1. 契约锁——36 个方法全部有同名子命令、选项名与 ``api_contract`` 零漂移；
2. 会话内闭环——同一 client 的 add→search→list→get→update→delete→evolve 全链路；
   注意 OFFLINE 内存后端不跨 CLI 进程保留数据，跨调用共享状态的唯一方式是
   同一 client（或 batch 单会话 / 远程常驻服务）；
3. 失闭——未注入认证器时业务调用拒绝，绝不从 payload 生成身份；
4. 进程级拒绝——argparse 参数错误以子进程真实退出码 2 断言。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from jiuwen_memory.api import build_dev_authenticator
from jiuwen_memory_entry.cli import __main__ as cli_main
from jiuwen_memory_entry.cli import client as client_module
from jiuwen_memory_entry.core.api_contract import api_method_names, method_contract
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

pytestmark = pytest.mark.unit

SCOPE = {"org": "local", "user": "developer"}
# 本文件位于 tests/unit/jiuwen_memory_entry/ 下——向上 4 层才是仓库根。
# 算错会把 PYTHONPATH 指到 tests/ 下不存在的路径，仅在仓库根运行或已 editable
# 安装时侥幸通过。
_REPO = Path(__file__).resolve().parents[3]


def _run_cli_process(*argv: str) -> subprocess.CompletedProcess[str]:
    """以子进程运行 CLI——在真实进程边界下断言 argparse 的退出码。"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(_REPO), str(_REPO / "jiuwen_memory_entry" / "core")]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "jiuwen_memory_entry.cli", *argv],
        capture_output=True, text=True, env=env, cwd=_REPO, timeout=120,
    )


def _subcommand_choices() -> set[str]:
    """从 build_parser() 反射出全部子命令名（含 healthz/batch 两个辅助命令）。"""
    parser = cli_main.build_parser()
    actions = getattr(parser, "_actions", [])
    subparsers = next(
        a for a in actions if a.__class__.__name__ == "_SubParsersAction"
    )
    return set(subparsers.choices)


def _method_option_strings(method: str) -> set[str]:
    """反射某方法子命令的全部选项串（含 --output/--pretty，排除 -h/--help）。"""
    parser = cli_main.build_parser()
    actions = getattr(parser, "_actions", [])
    subparsers = next(
        a for a in actions if a.__class__.__name__ == "_SubParsersAction"
    )
    sub = subparsers.choices[method]
    option_actions = getattr(sub, "_option_string_actions", {})
    return {opt for opt in option_actions if opt not in ("-h", "--help")}


@pytest.fixture
def dev_client():
    """同一会话的进程内 client（dev 认证）——跨调用共享内核状态。"""
    client = client_module.InProcessClient(authenticator=build_dev_authenticator())
    yield client
    client.close()


# --- A. 契约锁：36 方法全覆盖、选项名与 api_contract 零漂移 ---------------------- #


def test_cli_exposes_every_api_method_plus_helpers() -> None:
    choices = _subcommand_choices()
    assert set(api_method_names()) <= choices
    assert {"healthz", "batch"} <= choices  # 仅有的两个辅助命令


@pytest.mark.parametrize("method", sorted(api_method_names()))
def test_command_options_match_api_contract(method: str) -> None:
    contract = method_contract(method)
    expected = {f"--{name}" for name in contract.request_parameters}
    expected |= {"--output", "--pretty"}  # add_output_args 的展示参数
    assert _method_option_strings(method) == expected, (
        f"CLI.{method} 选项与 MemoryAPI 签名漂移"
    )


@pytest.mark.parametrize(
    "old_option", ["--tenant_id", "--item_id", "--k", "--hard"]
)
def test_legacy_options_not_accepted(old_option: str) -> None:
    assert old_option not in _method_option_strings("add")
    assert old_option not in _method_option_strings("get")


# --- B. 会话内全链路（同一 client：写入的数据跨调用可见）-------------------------- #


def test_client_roundtrip_add_list_search_update_get_delete(dev_client) -> None:
    status, units = dev_client.call("add", {"content": "hello coffee", "scope": SCOPE})
    assert status == 200, units
    unit = units[0]
    assert unit["segments"][0]["content"] == "hello coffee"
    assert "content" not in unit and "item_id" not in unit  # 原始结构、无 envelope
    unit_id = unit["id"]

    status, page = dev_client.call("list", {"scope": SCOPE})
    assert status == 200 and page["items"][0]["id"] == unit_id

    status, found = dev_client.call(
        "search", {"query": "coffee", "context": {"scope": SCOPE}, "top_k": 3}
    )
    assert status == 200 and found["items"][0]["unit_id"] == unit_id
    assert "hits" not in found

    status, updated = dev_client.call(
        "update",
        {"unit_id": unit_id, "scope": SCOPE,
         "patch": {"content": "updated coffee", "mode": "overwrite"}},
    )
    assert status == 200 and updated["segments"][0]["content"] == "updated coffee"

    status, fetched = dev_client.call(
        "get", {"unit_id": updated["id"], "scope": SCOPE}
    )
    assert status == 200 and fetched["id"] == updated["id"]

    status, deleted = dev_client.call(
        "delete", {"selector": {"scope": SCOPE, "unit_ids": [updated["id"]]}}
    )
    assert status == 200 and deleted == [updated["id"]]


def test_client_evolve_job_status_and_cancel_loop(dev_client) -> None:
    dev_client.call("add", {"content": "hello", "scope": SCOPE})
    status, job_id = dev_client.call("evolve", {"scope": SCOPE, "mode": "extract"})
    assert status == 200 and isinstance(job_id, str) and job_id

    import time

    info: dict[str, Any] = {}
    for _ in range(50):
        status, info = dev_client.call("job_status", {"job_id": job_id, "scope": SCOPE})
        if info["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert info["status"] == "succeeded", info
    status, _ = dev_client.call("job_cancel", {"job_id": job_id})  # 幂等：已完成不报错
    assert status == 200


# --- C. main() 入口：无状态场景（单条输出 / batch 单会话 / 参数错误）------------- #


def test_main_healthz(capsys) -> None:
    rc = cli_main.main(["healthz"])
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 0 and out["status"] == "ok"


def test_main_add_outputs_original_json(capsys) -> None:
    rc = cli_main.main(
        ["--auth-mode", "dev", "add",
         "--content", "hello coffee", "--scope", json.dumps(SCOPE)]
    )
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert out[0]["segments"][0]["content"] == "hello coffee"
    assert "item_id" not in out[0]  # 不加 envelope、不改字段名


def test_main_batch_two_ops_share_one_session(capsys, monkeypatch) -> None:
    ndjson = "\n".join(
        [
            json.dumps({"op": "add", "content": "batch coffee", "scope": SCOPE}),
            json.dumps({"op": "search", "query": "coffee",
                        "context": {"scope": SCOPE}, "top_k": 3}),
        ]
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(ndjson))
    rc = cli_main.main(["--auth-mode", "dev", "batch"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0 and len(out) == 2
    added, found = json.loads(out[0]), json.loads(out[1])
    assert added[0]["segments"][0]["content"] == "batch coffee"
    assert found["items"][0]["unit_id"] == added[0]["id"]  # batch 单会话内可见


def test_main_rejects_legacy_options() -> None:
    # 旧协议别名（--tenant_id 等）不被接受：argparse 参数错误退出码 2
    proc = _run_cli_process(
        "--auth-mode", "dev", "add",
        "--content", "x", "--scope", json.dumps(SCOPE), "--tenant_id", "demo",
    )
    assert proc.returncode == 2, proc.stderr


def test_main_rejects_identity_fields() -> None:
    # 身份字段是认证边界专属——出现在业务参数里即拒绝
    proc = _run_cli_process(
        "--auth-mode", "dev", "add",
        "--content", "x", "--scope", json.dumps(SCOPE), "--actor", "admin",
    )
    assert proc.returncode == 2, proc.stderr


def test_main_rejects_local_auth_mode_with_server() -> None:
    # 远程认证模式由服务端决定——客户端声明 dev 直接拒绝
    proc = _run_cli_process(
        "--server", "http://127.0.0.1:8137", "--auth-mode", "dev", "healthz",
    )
    assert proc.returncode == 2, proc.stderr


# --- D. 远程等价：本地与 HTTP 完成同样的 CRUD（重建本地=远程证据）----------------- #


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
    unit_id = units[0]["id"]

    status, page = api_client.call("list", {"scope": SCOPE})
    assert status == 200 and page["items"][0]["id"] == unit_id

    status, updated = api_client.call(
        "update",
        {"unit_id": unit_id, "scope": SCOPE,
         "patch": {"content": "updated coffee", "mode": "overwrite"}},
    )
    assert status == 200

    status, deleted = api_client.call(
        "delete", {"selector": {"scope": SCOPE, "unit_ids": [updated["id"]]}}
    )
    assert status == 200 and deleted == [updated["id"]]


# --- E. 远程客户端：payload 与鉴权头保真 ----------------------------------------- #


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    @staticmethod
    def __exit__(*args: object) -> None:
        return None


def test_http_client_preserves_payload_and_original_response(monkeypatch) -> None:
    seen: list[urllib.request.Request] = []
    body: Any = [{"id": "u1", "segments": [{"content": "hi"}]}]

    def fake_urlopen(request: urllib.request.Request, timeout: float = 30.0):
        seen.append(request)
        return _FakeResponse(200, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = {"scope": SCOPE, "limit": 2}
    client = client_module.HttpClient("http://example.test", api_key="test-key")
    status, result = client.call("list", payload)

    assert status == 200 and result == body
    assert seen[0].full_url == "http://example.test/v1/list"
    assert json.loads(seen[0].data) == payload
    assert seen[0].get_header("Authorization") == "Bearer test-key"
