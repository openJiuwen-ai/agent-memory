# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP 工具面：契约锁、安全注入与功能闭环（与 test_cli.py 同一验证口径）。

MCP 工具是手写的（不像 HTTP/CLI 从 MemoryAPI 反射生成），契约锁测试是防
「工具签名与 API 签名漂移」的唯一防线：参数名集合与契约**全量相等**（as_of 漂移
的教训——子集锁会放行静默缺失）、代表性 payload 形状全部与 ``api_contract``
对齐断言。功能闭环重点钉住本特性的核心语义：evolve→job_status 任务闭环（旧 7
工具时代的断链）与 consolidate→trace 血缘链。

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
from datetime import datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("mcp.server.fastmcp")

# __main__ 在模块级把 sys.argv[1:] 当配置路径读取、按环境变量装配认证器；
# pytest 的 argv 与宿主环境不得影响导入结果，先钉住再导入。认证模式环境变量
# 同样只在导入期被读取一次（模块级 _build_authenticator），导入完成（或失败）
# 后立即还原，不向同进程后续测试泄漏全局状态；测试依赖的 dev 认证器已随导入
# 固化为 mcp_main._AUTHENTICATOR，还原不影响本文件行为。
_AUTH_MODE_ENV = "JIUWEN_MEMORY_MCP_AUTH_MODE"
_ORIG_AUTH_MODE = os.environ.get(_AUTH_MODE_ENV)
os.environ[_AUTH_MODE_ENV] = "dev"
_ARGV = sys.argv
sys.argv = ["mcp"]
try:
    from jiuwen_memory_entry.mcp_server import __main__ as mcp_main
finally:
    sys.argv = _ARGV
    if _ORIG_AUTH_MODE is None:
        os.environ.pop(_AUTH_MODE_ENV, None)
    else:
        os.environ[_AUTH_MODE_ENV] = _ORIG_AUTH_MODE

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
    "memory_add": (
        "add",
        {"content": "hello", "scope": SCOPE, "source": "text",
         "assets": ["file:///a.png"], "occurred_at": "2026-06-17T10:00:00+00:00",
         "system_metadata": {"infer": "true"}, "user_metadata": {"project": "x"}},
    ),
    "memory_add_async": (
        "add_async",
        {"content": "hello", "scope": SCOPE, "source": "text",
         "occurred_at": "2026-06-17T10:00:00+00:00",
         "system_metadata": {"infer": "true"}, "user_metadata": {"project": "x"}},
    ),
    "memory_batch_add": (
        "batch_add",
        {"items": [{"content": "a"}], "scope": SCOPE, "source": "text",
         "stream_id": "s1", "occurred_at": "2026-06-17T10:00:00+00:00",
         "system_metadata": {"batch": "true"}, "user_metadata": {"project": "x"}},
    ),
    "memory_batch_add_async": (
        "batch_add_async",
        {"items": [{"content": "a"}], "scope": SCOPE, "source": "text",
         "stream_id": "s1", "occurred_at": "2026-06-17T10:00:00+00:00",
         "system_metadata": {"batch": "true"}, "user_metadata": {"project": "x"}},
    ),
    "memory_search": (
        "search",
        {"query": "hello", "context": {"scope": SCOPE},
         "as_of": "2026-06-17T10:30:00+00:00",
         "filters": {"field": "tags", "op": "contains", "value": "a"},
         "disclosure": "l2"},
    ),
    "memory_list": (
        "list",
        {"scope": SCOPE, "memory_types": ["episodic"],
         "extensions": {"k": "v"},
         "filters": {"field": "tier", "op": "eq", "value": "episodic"}},
    ),
    "memory_get": (
        "get", {"unit_id": "u1", "scope": SCOPE, "as_of": "2026-06-17T10:30:00+00:00"}
    ),
    "memory_update": (
        "update",
        {"unit_id": "u1", "scope": SCOPE, "patch": {"content": "x"}},
    ),
    "memory_delete": ("delete", {"selector": {"unit_ids": ["u1"], "scope": SCOPE}}),
    "memory_evolve": ("evolve", {"scope": SCOPE, "mode": "extract",
                                 "channel": "background"}),
    "memory_check_write": (
        "check_write",
        {"scope": SCOPE, "tags": ["t"], "system_metadata": {"k": "v"},
         "user_metadata": {"k": "v"}},
    ),
    "memory_submit_ingest": (
        "submit_ingest",
        {"content": "doc", "scope": SCOPE, "source": "text",
         "payload_id": "p1", "source_ref": "file:///tmp/a.pdf",
         "assets": ["file:///tmp/a.pdf"], "tags": ["doc"],
         "system_metadata": {"k": "v"}, "user_metadata": {"k": "v"}},
    ),
    "memory_job_status": ("job_status", {"job_id": "j1"}),
    "memory_job_cancel": ("job_cancel", {"job_id": "j1"}),
    "memory_admin_get": ("admin_get", {"key": "k"}),
    "memory_admin_set": ("admin_set", {"key": "k", "value": "v"}),
    "memory_admin_all": ("admin_all", {}),
    "memory_inspect": ("inspect", {"unit_ids": ["u1"], "scope": SCOPE}),
    "memory_trace": ("trace", {"unit_id": "u1", "scope": SCOPE}),
    "memory_audit": ("audit", {"filters": {}}),
    "memory_verify_audit": (
        "verify_audit",
        {"after_sequence": 0, "page_size": 100, "max_samples": 5,
         "anchor_policy": "if_configured"},
    ),
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
    "memory_list_spaces": (
        "list_spaces", {"org": "local", "status": "active", "limit": 10,
                        "cursor": None},
    ),
    "memory_update_space": (
        "update_space",
        {"org": "local", "space": "team-a", "patch": {"display_name": "Alpha"}},
    ),
    "memory_archive_space": ("archive_space", {"org": "local", "space": "team-a"}),
    "memory_delete_space": (
        "delete_space", {"org": "local", "space": "team-a", "mode": "purge"}
    ),
    "memory_export_space": ("export_space", {"org": "local", "space": "team-a"}),
    "memory_space_usage": ("space_usage", {"org": "local", "space": "team-a"}),
    "memory_get_space_policy": (
        "get_space_policy", {"org": "local", "space": "team-a"}),
    "memory_set_space_policy": (
        "set_space_policy", {"org": "local", "space": "team-a", "policy": {}},
    ),
    "memory_list_space_members": (
        "list_space_members", {"org": "local", "space": "team-a"}),
    "memory_add_space_member": (
        "add_space_member",
        {"org": "local", "space": "team-a", "member": {"scope": SCOPE}},
    ),
    "memory_remove_space_member": (
        "remove_space_member", {"org": "local", "space": "team-a", "member": SCOPE},
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
    # 全量相等锁：as_of 漂移的教训——此前的子集锁会放行可选参数静默缺失，
    # MCP 客户端经工具 schema 感知不到该参数。工具面与契约的任何参数差异
    # 都必须显式决策（改工具或改契约），不允许静默漂移。
    assert tool_params == api_params, (
        f"{tool_name} 参数与契约漂移——缺失: {sorted(api_params - tool_params)},"
        f" 多余: {sorted(tool_params - api_params)}"
    )


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
    assert [outcome["index"] for outcome in outcomes] == [0, 1, 2]
    assert all(outcome["units"] for outcome in outcomes)


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


def test_get_as_of_returns_version_valid_at_that_time(kernel) -> None:
    # 镜像 tests/unit/control/test_engine_get_as_of.py 的版本链回溯语义；
    # as_of 取返回单元自身的 temporal.t_valid——与内核时间戳同时钟域，免时区换算。
    old = asyncio.run(mcp_main.memory_add(content="v1", scope=SCOPE))[0]
    time.sleep(0.05)
    new_id = asyncio.run(
        mcp_main.memory_update(unit_id=old["id"], scope=SCOPE, patch={"content": "v2"})
    )["id"]
    as_of = old["temporal"]["t_valid"]

    current = asyncio.run(mcp_main.memory_get(unit_id=new_id, scope=SCOPE))
    assert current["segments"][0]["content"] == "v2"
    historical = asyncio.run(
        mcp_main.memory_get(unit_id=old["id"], scope=SCOPE, as_of=as_of)
    )
    assert historical["id"] == old["id"], "as_of 回溯应命中当时有效的旧版本"
    assert historical["segments"][0]["content"] == "v1"
    # get 沿 supersedes 链双向回溯——新 id 配旧时刻同样命中旧版本
    via_new_id = asyncio.run(
        mcp_main.memory_get(unit_id=new_id, scope=SCOPE, as_of=as_of)
    )
    assert via_new_id["id"] == old["id"]
    # 早于链起点：整条链无有效版本 → NotFound
    before_chain = (
        datetime.fromisoformat(as_of) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(RuntimeError, match="NotFoundError"):
        asyncio.run(mcp_main.memory_get(unit_id=new_id, scope=SCOPE, as_of=before_chain))


def test_search_as_of_excludes_units_not_yet_valid(kernel) -> None:
    added = asyncio.run(mcp_main.memory_add(content="hello coffee", scope=SCOPE))[0]
    before_write = (
        datetime.fromisoformat(added["temporal"]["t_valid"]) - timedelta(seconds=1)
    ).isoformat()

    past = asyncio.run(
        mcp_main.memory_search(query="coffee", context={"scope": SCOPE}, as_of=before_write)
    )
    assert past["items"] == [], "as_of 早于写入时间（t_valid）不应召回"
    present = asyncio.run(
        mcp_main.memory_search(query="coffee", context={"scope": SCOPE})
    )
    assert present["items"], "缺省 as_of 应召回当前有效记忆"


def test_list_memory_types_filters_results(kernel) -> None:
    asyncio.run(mcp_main.memory_add(content="hello coffee", scope=SCOPE))
    matched = asyncio.run(
        mcp_main.memory_list(scope=SCOPE, memory_types=["episodic"])
    )
    assert matched["items"], "episodic 类型应命中（OFFLINE 缺省 tier）"
    other = asyncio.run(
        mcp_main.memory_list(scope=SCOPE, memory_types=["core"])
    )
    assert other["items"] == [] and other["count"] == 0, "core 类型不应命中"


def test_search_filters_narrow_results(kernel) -> None:
    asyncio.run(
        mcp_main.memory_add(content="hello coffee", scope=SCOPE, tags=["brew"])
    )
    asyncio.run(mcp_main.memory_add(content="hello tea", scope=SCOPE, tags=["leaf"]))
    filtered = asyncio.run(
        mcp_main.memory_search(
            query="hello",
            context={"scope": SCOPE},
            filters={"field": "tags", "op": "contains", "value": "leaf"},
        )
    )
    contents = {item.get("content", "") for item in filtered["items"]}
    assert contents == {"hello tea"}, f"filters 应收敛到带 leaf 标签的记忆: {contents}"


def test_delete_requires_real_criterion_besides_scope(kernel) -> None:
    # scope 只是范围限定符、不是选择条件——单独给它必须被拒绝
    with pytest.raises(RuntimeError, match="unit_ids, tags, before, or filters"):
        asyncio.run(mcp_main.memory_delete(selector={"scope": SCOPE, "mode": "forget"}))


def test_delete_space_rejects_archive_mode(kernel) -> None:
    # 检视回归：delete_space 当前仅支持 purge，mode=archive 在 API 边界被拒
    # （space_ops 在鉴权前校验）；归档的正确入口是 memory_archive_space
    with pytest.raises(RuntimeError, match="DeleteMode.PURGE only"):
        asyncio.run(
            mcp_main.memory_delete_space(org="local", space="no-such", mode="archive")
        )


# --- E. 模型可见 Schema：36 工具、ctx 不进 schema --------------------------------- #


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
    # occurred_at 走 call_tool 锁两件事：pydantic 编组层按 str 放行 ISO 字符串；
    # 且按 F07 语义落 temporal.t_message——t_event 由 Extractor 从内容提取，恒 None。
    occurred_at = "2026-06-17T10:00:00+00:00"
    blocks, structured = asyncio.run(
        mcp_main.mcp.call_tool(
            "memory_add",
            {"content": "marshalled", "scope": SCOPE, "occurred_at": occurred_at},
        )
    )
    units = structured["result"]
    assert units[0]["segments"][0]["content"] == "marshalled"
    assert json.loads(blocks[0].text)["id"] == units[0]["id"]
    assert units[0]["temporal"]["t_message"] == occurred_at, (
        "occurred_at 应落 temporal.t_message（F07 消息时间语义）"
    )
    assert units[0]["temporal"]["t_event"] is None, (
        "t_event 由内容提取，不经 occurred_at 下传"
    )
    got = asyncio.run(
        mcp_main.mcp.call_tool(
            "memory_get",
            {"unit_id": units[0]["id"], "scope": SCOPE,
             "as_of": units[0]["temporal"]["t_valid"]},
        )
    )
    assert json.loads(got[0].text)["id"] == units[0]["id"]
    # search 的 as_of 也走 call_tool：锁 str 注解——若改回 datetime | None，
    # pydantic 会把 ISO 字符串收成 datetime 对象、在共享契约边界被拒，此调用即红
    found = asyncio.run(
        mcp_main.mcp.call_tool(
            "memory_search",
            {"query": "marshalled", "context": {"scope": SCOPE},
             "as_of": units[0]["temporal"]["t_valid"]},
        )
    )
    search_result = json.loads(found[0].text)
    assert any(
        item["unit_id"] == units[0]["id"] for item in search_result["items"]
    ), "call_tool 路径的 as_of 字符串应正常过编组层并召回"
