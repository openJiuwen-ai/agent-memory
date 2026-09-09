# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP 工具面：契约锁、安全注入与功能闭环（与 test_cli.py 同一验证口径）。

MCP 工具是手写的（不像 HTTP/CLI 从 MemoryAPI 反射生成），契约锁测试是防
「工具签名与 API 签名漂移」的唯一防线：参数名集合、必填覆盖、代表性 payload
形状全部与 ``api_contract`` 对齐断言。功能闭环重点钉住本特性的两个核心语义：
evolve→job_status 任务闭环（旧 7 工具时代的断链）与 consolidate→trace 血缘链。

工具为 async（FastMCP 在事件循环线程裸调工具函数，而同步 MemoryAPI 方法内部
经 asyncio.run 桥接协程，故执行体经 asyncio.to_thread 隔离）；测试用
asyncio.run 驱动，与真实 MCP 调用路径一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
import time
from typing import Any

import pytest

pytest.importorskip("mcp.server.fastmcp")

# __main__ 在模块级把 sys.argv[1:] 当配置路径读取、按环境变量装配认证器；
# pytest 的 argv 与宿主环境不得影响导入结果，先钉住再导入。
os.environ["JIUWEN_MEMORY_MCP_AUTH_MODE"] = "dev"
_ARGV = sys.argv
sys.argv = ["mcp"]
try:
    from jiuwen_memory_entry.mcp_server import __main__ as mcp_main
finally:
    sys.argv = _ARGV

from jiuwen_memory.api import Surface, ValidationError  # noqa: E402
from jiuwen_memory_entry.core.api_contract import (  # noqa: E402
    is_known_verb,
    method_contract,
    parse_request,
)

pytestmark = pytest.mark.unit

SCOPE = {"org": "local", "user": "developer"}

# 工具名 → (MemoryAPI 方法名, 代表性最小合法 payload)。payload 同时锁「形状可过
# parse_request」——工具转发给 _invoke 的键名集合必须与这里给出的契约一致。
TOOL_CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "memory_add": ("add", {"content": "hello", "scope": SCOPE}),
    "memory_add_async": ("add_async", {"content": "hello", "scope": SCOPE}),
    "memory_batch_add": ("batch_add", {"items": [{"content": "a"}], "scope": SCOPE}),
    "memory_batch_add_async": (
        "batch_add_async", {"items": [{"content": "a"}], "scope": SCOPE}),
    "memory_search": ("search", {"query": "hello", "context": {"scope": SCOPE}}),
    "memory_list": ("list", {"scope": SCOPE}),
    "memory_get": ("get", {"unit_id": "u1", "scope": SCOPE}),
    "memory_update": (
        "update",
        {"unit_id": "u1", "scope": SCOPE, "patch": {"content": "x"}},
    ),
    "memory_delete": ("delete", {"selector": {"unit_ids": ["u1"], "scope": SCOPE}}),
    "memory_evolve": ("evolve", {"scope": SCOPE, "mode": "extract"}),
    "memory_check_write": ("check_write", {"scope": SCOPE}),
    "memory_submit_ingest": (
        "submit_ingest",
        {"content": "doc", "scope": SCOPE, "source": "text",
         "payload_id": "p1", "source_ref": "file:///tmp/a.pdf"},
    ),
    "memory_job_status": ("job_status", {"job_id": "j1"}),
    "memory_job_cancel": ("job_cancel", {"job_id": "j1"}),
    "memory_admin_get": ("admin_get", {"key": "k"}),
    "memory_admin_set": ("admin_set", {"key": "k", "value": "v"}),
    "memory_admin_all": ("admin_all", {}),
    "memory_inspect": ("inspect", {"unit_ids": ["u1"], "scope": SCOPE}),
    "memory_trace": ("trace", {"unit_id": "u1", "scope": SCOPE}),
    "memory_audit": ("audit", {"filters": {}}),
    "memory_verify_audit": ("verify_audit", {}),
    "memory_grant": (
        "grant",
        {"grant": {"grantor": SCOPE, "grantee": {"org": "local", "agent": "helper"},
                   "actions": ["read"]}},
    ),
    "memory_revoke": (
        "revoke",
        {"grant": {"grantor": SCOPE, "grantee": {"org": "local", "agent": "helper"},
                   "actions": ["read"]}},
    ),
    "memory_create_space": (
        "create_space",
        {"spec": {"org": "local", "space": "team-a", "display_name": "Team A"}},
    ),
    "memory_get_space": ("get_space", {"org": "local", "space": "team-a"}),
    "memory_list_spaces": ("list_spaces", {"org": "local"}),
    "memory_update_space": (
        "update_space",
        {"org": "local", "space": "team-a", "patch": {"display_name": "Alpha"}},
    ),
    "memory_archive_space": ("archive_space", {"org": "local", "space": "team-a"}),
    "memory_delete_space": ("delete_space", {"org": "local", "space": "team-a"}),
    "memory_export_space": ("export_space", {"org": "local", "space": "team-a"}),
    "memory_space_usage": ("space_usage", {"org": "local", "space": "team-a"}),
    "memory_get_space_policy": (
        "get_space_policy", {"org": "local", "space": "team-a"}),
    "memory_set_space_policy": (
        "set_space_policy",
        {"org": "local", "space": "team-a", "policy": {}},
    ),
    "memory_list_space_members": (
        "list_space_members", {"org": "local", "space": "team-a"}),
    "memory_add_space_member": (
        "add_space_member",
        {"org": "local", "space": "team-a", "member": {"scope": SCOPE}},
    ),
    "memory_remove_space_member": (
        "remove_space_member",
        {"org": "local", "space": "team-a", "member": SCOPE},
    ),
}


@pytest.fixture
def kernel(monkeypatch):
    """每测试一个全新 OFFLINE 内核（隔离演进任务与调度器状态）。

    工具经 ``_invoke`` 在调用时读模块级 ``_SRV``，monkeypatch 即可换芯。
    """
    srv = mcp_main.Server.build(mcp_main.load_config([mcp_main.OFFLINE]))
    monkeypatch.setattr(mcp_main, "_SRV", srv)
    yield srv
    srv.close(wait=True)


def _wait_job_terminal(job_id: str, scope: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for _ in range(50):
        info = asyncio.run(mcp_main.memory_job_status(job_id=job_id, scope=scope))
        if info["status"] in ("succeeded", "failed", "cancelled"):
            return info
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} not terminal: {info}")


# --- A. 契约锁：工具签名与 MemoryAPI 契约零漂移 -------------------------------- #


def test_mcp_tool_registry_covers_expected_verbs() -> None:
    registered = {name for name in dir(mcp_main) if name.startswith("memory_")}
    assert registered == set(TOOL_CASES), registered ^ set(TOOL_CASES)
    for _tool_name, (verb, _payload) in TOOL_CASES.items():
        assert is_known_verb(verb), verb


@pytest.mark.parametrize("tool_name", list(TOOL_CASES))
def test_tool_signature_matches_api_contract(tool_name: str) -> None:
    verb, _payload = TOOL_CASES[tool_name]
    tool_params = set(inspect.signature(getattr(mcp_main, tool_name)).parameters) - {"ctx"}
    contract = method_contract(verb)
    api_params = set(contract.request_parameters)
    assert tool_params <= api_params, f"{tool_name} 多余参数: {tool_params - api_params}"
    required = {
        name
        for name, parameter in contract.request_parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    assert required <= tool_params, f"{tool_name} 缺必填参数: {required - tool_params}"


@pytest.mark.parametrize("tool_name", list(TOOL_CASES))
def test_tool_representative_payload_parses(tool_name: str) -> None:
    verb, payload = TOOL_CASES[tool_name]
    decoded = parse_request(verb, payload)
    assert isinstance(decoded, dict)  # admin_all/verify_audit 等无参工具合法为空
    for key in payload:
        assert key in decoded


# --- B. 旧协议字段与身份字段在契约边界被拒绝 ------------------------------------ #


@pytest.mark.parametrize(
    "field", ["tenant_id", "item_id", "k", "hard", "identity", "actor", "security"]
)
def test_payload_rejects_legacy_and_identity_fields(field: str) -> None:
    payload = {"content": "x", "scope": SCOPE, field: "spoof"}
    with pytest.raises(ValidationError):
        parse_request("add", payload)


# --- C. 安全：失闭、Surface.MCP 注入、dev 固定身份 ------------------------------- #


def test_invoke_fails_closed_without_authenticator(kernel, monkeypatch) -> None:
    monkeypatch.setattr(mcp_main, "_AUTHENTICATOR", None)
    with pytest.raises(RuntimeError, match="authentication is not configured"):
        asyncio.run(mcp_main.memory_add(content="x", scope=SCOPE))


def test_authenticated_runs_with_mcp_surface_and_dev_identity(
    kernel, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    real_authenticated = mcp_main.authenticated

    @contextlib.contextmanager
    def _capturing(authenticator, credentials, **kwargs):
        with real_authenticated(authenticator, credentials, **kwargs) as security:
            captured["kwargs"] = kwargs
            captured["security"] = security
            yield security

    monkeypatch.setattr(mcp_main, "authenticated", _capturing)
    asyncio.run(mcp_main.memory_add(content="hello", scope=SCOPE))

    assert captured["kwargs"]["surface"] == Surface.MCP
    assert captured["kwargs"]["request_id"]
    actor = captured["security"].auth.actor
    assert (actor.org, actor.user) == ("local", "developer")


# --- D. 功能闭环（任务闭环与血缘链是本特性的核心回归）--------------------------- #


def test_add_returns_original_unit_without_envelope(kernel) -> None:
    units = asyncio.run(mcp_main.memory_add(content="hello coffee", scope=SCOPE))
    assert isinstance(units, list) and units
    unit = units[0]
    assert unit["segments"][0]["content"] == "hello coffee"
    assert "content" not in unit and "item_id" not in unit


def test_update_supersedes_keeps_lineage(kernel) -> None:
    units = asyncio.run(mcp_main.memory_add(content="v1", scope=SCOPE))
    old_id = units[0]["id"]
    updated = asyncio.run(
        mcp_main.memory_update(unit_id=old_id, scope=SCOPE, patch={"content": "v2"})
    )
    assert updated["id"] != old_id
    assert updated["supersedes"] == old_id


def test_batch_add_outcomes_align_with_input(kernel) -> None:
    result = asyncio.run(
        mcp_main.memory_batch_add(
            items=[{"content": "a"}, {"content": "b"}, {"content": "c"}], scope=SCOPE
        )
    )
    outcomes = result["outcomes"]
    assert [o["index"] for o in outcomes] == [0, 1, 2]
    assert all(o["units"] for o in outcomes)


def test_evolve_job_status_and_cancel_loop(kernel) -> None:
    asyncio.run(mcp_main.memory_add(content="hello", scope=SCOPE))
    job_id = asyncio.run(mcp_main.memory_evolve(scope=SCOPE, mode="extract"))
    assert isinstance(job_id, str) and job_id
    info = _wait_job_terminal(job_id, SCOPE)
    assert info["status"] == "succeeded", info
    asyncio.run(mcp_main.memory_job_cancel(job_id=job_id))  # 幂等：已完成任务不报错


def test_inspect_includes_superseded_history(kernel) -> None:
    old_id = asyncio.run(mcp_main.memory_add(content="v1", scope=SCOPE))[0]["id"]
    new_id = asyncio.run(
        mcp_main.memory_update(unit_id=old_id, scope=SCOPE, patch={"content": "v2"})
    )["id"]
    inspected = asyncio.run(
        mcp_main.memory_inspect(unit_ids=[old_id, new_id], scope=SCOPE)
    )
    assert {u["id"] for u in inspected} == {old_id, new_id}


def test_consolidate_produces_derived_unit_with_provenance_chain(kernel) -> None:
    source_ids = [
        asyncio.run(mcp_main.memory_add(content=f"fact {i}", scope=SCOPE))[0]["id"]
        for i in range(2)
    ]
    job_id = asyncio.run(mcp_main.memory_evolve(scope=SCOPE, mode="consolidate"))
    assert _wait_job_terminal(job_id, SCOPE)["status"] == "succeeded"
    listed = asyncio.run(mcp_main.memory_list(scope=SCOPE))
    derived = [u for u in listed["items"] if u.get("provenance")]
    assert derived, "consolidate 未产出带 provenance 的派生单元"
    chain = asyncio.run(mcp_main.memory_trace(unit_id=derived[0]["id"], scope=SCOPE))
    chain_ids = {u["id"] for u in chain}
    assert chain_ids >= {derived[0]["id"], *source_ids}


def test_search_returns_original_result_shape(kernel) -> None:
    asyncio.run(mcp_main.memory_add(content="hello coffee", scope=SCOPE))
    result = asyncio.run(
        mcp_main.memory_search(query="coffee", context={"scope": SCOPE}, top_k=5)
    )
    assert isinstance(result, dict)
    assert set(result) == {"items", "errors", "trajectory"}
    assert "hits" not in result


def test_delete_requires_real_criterion_besides_scope(kernel) -> None:
    # scope 只是范围限定符、不是选择条件——单独给它必须被拒绝
    with pytest.raises(RuntimeError, match="unit_ids, tags, before, or filters"):
        asyncio.run(mcp_main.memory_delete(selector={"scope": SCOPE, "mode": "forget"}))


# --- E. 模型可见 Schema：恰好 12 工具、ctx 不泄漏 -------------------------------- #


def test_list_tools_schema_excludes_ctx() -> None:
    tools = asyncio.run(mcp_main.mcp.list_tools())
    assert {t.name for t in tools} == set(TOOL_CASES)
    for tool in tools:
        props = (tool.inputSchema or {}).get("properties", {})
        assert "ctx" not in props, f"ctx leaked into {tool.name}: {list(props)}"


# --- F. 协议编组层：FastMCP.call_tool 真实调用路径 ------------------------------- #


def test_fastmcp_call_tool_marshals_arguments(kernel) -> None:
    # call_tool(convert_result=True) 的返回形态随工具返回注解分两路：
    # -> list[dict]（如 memory_add）带 structured 输出，返回 (blocks, {"result": ...})；
    # -> dict（如 memory_get）无 structured 输出，直接返回 [TextContent(结果 JSON)]。
    blocks, structured = asyncio.run(
        mcp_main.mcp.call_tool("memory_add", {"content": "marshalled", "scope": SCOPE})
    )
    units = structured["result"]
    assert units[0]["segments"][0]["content"] == "marshalled"
    assert json.loads(blocks[0].text)["id"] == units[0]["id"]
    got = asyncio.run(
        mcp_main.mcp.call_tool(
            "memory_get", {"unit_id": units[0]["id"], "scope": SCOPE}
        )
    )
    assert json.loads(got[0].text)["id"] == units[0]["id"]
