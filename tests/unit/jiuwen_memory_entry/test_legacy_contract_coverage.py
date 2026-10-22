# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""legacy 入口契约覆盖锁：契约里每个入参在 legacy 扁平面都必须有明确归属。

HTTP / CLI / MCP 三个入口的字段集合由 ``core/api_contract`` 从 ``MemoryAPI`` 签名
自动派生，不会漂移；legacy / in-process 的 ``handler.dispatch`` 是**平行手写**实现，
契约新增可选字段时没有任何机制强制两边同步——``search.as_of``、``update.patch.tier``、
``evolve.channel``、``SpaceSpec.owner``、成员两轴角色、``Grant.actions`` 等，
都曾因此在 legacy 入口被静默吃掉（返回 200，语义不符）。

本文件把「legacy 面到底支持哪些契约参数」从隐性知识变成**显式表格**
（``_LEGACY_SURFACE``），并对每一项做机械校验：

1. **覆盖性**：每个 legacy 路由 verb 的契约参数（dataclass 参数细化到字段）都必须
   出现在表里，否则测试失败——契约加字段时作者必须显式决定它在 legacy 面的归属；
2. **可达性**：声明为 legacy 键的项，该键必须真的被这个 verb 的处理函数读取
   （AST 取值路径，含其调用图）；
3. **空白显式化**：``gap(...)`` 项必须写明原因，且当前确实不可达——一旦有人补上
   透传（例如 PR #358 合入），测试会失败并迫使删掉该行，避免表格随代码腐化。

局限：嵌套路径（``defaults.source``、``policy.require_space``）只校验首段
（容器键）来自 ``payload``、末段被某个变量读取，不做完整数据流分析。
"""

from __future__ import annotations

import ast
import dataclasses
from functools import lru_cache
from pathlib import Path

import pytest

from jiuwen_memory_entry.core.api_contract import is_known_verb, method_contract

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_HANDLER_PATH = _REPO / "jiuwen_memory_entry" / "core" / "handler.py"


class _Derived:
    """由适配器或请求目标组装，legacy 扁平面不出现对应键。"""

    # 哨兵本身无实例状态，故按静态方法声明（G.CLS.07 类的方法不需要访问实例
    # 时建议定义为 staticmethod）；repr()/f-string 均按特殊方法查找正常调用它。
    @staticmethod
    def __repr__() -> str:  # pragma: no cover - 仅供断言消息可读
        return "DERIVED"


DERIVED = _Derived()


class _Gap:
    """显式声明 legacy 扁平面不支持的契约参数（必须带原因）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - 仅供断言消息可读
        return f"gap({self.reason})"


def gap(reason: str) -> _Gap:
    return _Gap(reason)


# 整个值都由请求上下文组装、legacy 面既不展开也不读取的 dataclass 参数。
# 未列在此处的 dataclass 参数必须细化到字段，否则校验失败。
_WHOLE_REQUEST_DERIVED = {
    ("search", "context"),  # 由请求目标 + extensions 组装（Context(scope, extensions)）
    ("batch_add", "items"),  # 每个 item 由适配器解析成 BatchWriteItem
    ("remove_space_member", "member"),  # 嵌套/扁平 Scope 由适配器解析
}

# 所有 verb 通用：``scope`` 由适配器从 tenant_id / scope / space / ... 定位键组装，
# 从不来自业务 payload，因此无需逐 verb 登记。
_CONTEXT_PARAMETERS = {"scope"}

# legacy 路由里不对应 MemoryAPI 方法的 verb（job/admin 是聚合入口，不映射单方法）。
_NON_CONTRACT_VERBS = {
    "job": "聚合入口：video 任务查询/取消，无单一 MemoryAPI 方法对应",
    "admin": "聚合入口：admin_get/admin_set/admin_all，无单一 MemoryAPI 方法对应",
}

# verb → {契约参数: legacy 归属}。归属取值：
#   "key"            legacy 扁平键（"." 分隔表示嵌套容器，如 defaults.source）
#   DERIVED          由适配器/请求目标组装，legacy 扁平面无该键
#   gap("原因")       显式声明不支持
#   {字段: 归属}       dataclass 参数，细化到每个字段
_LEGACY_SURFACE: dict[str, dict[str, object]] = {
    "add": {
        "content": "content",
        "scope": DERIVED,
        "source": "source",
        "assets": "assets",
        "tags": "tags",
        "system_metadata": "system_metadata",
        "user_metadata": "user_metadata",
        "occurred_at": "occurred_at",
    },
    "batch_add": {
        "items": DERIVED,
        "scope": DERIVED,
        "source": "defaults.source",
        "tags": "defaults.tags",
        "system_metadata": "defaults.system_metadata",
        "user_metadata": "defaults.user_metadata",
        "occurred_at": "defaults.occurred_at",
        "stream_id": "defaults.stream_id",
        "continue_on_error": "continue_on_error",
    },
    "search": {
        "query": "query",
        "context": DERIVED,
        "filters": "filters",
        "as_of": "as_of",
        "top_k": "k",
        "disclosure": "disclosure",
        "with_trajectory": "trace",
        "hierarchy_kind": "hierarchy_kind", "hierarchy_role": "hierarchy_role",
        "span_start": "span_start", "span_end": "span_end",
        "expand_depth": "expand_depth", "rollup": "rollup",
    },
    "list": {
        "scope": DERIVED,
        "offset": "offset",
        "limit": "limit",
        "memory_types": "memory_types",
        "extensions": "extensions",
        "filters": "filters",
    },
    "get": {
        "unit_id": "item_id",
        "scope": DERIVED,
        "as_of": "as_of",
    },
    "update": {
        "unit_id": "item_id",
        "scope": DERIVED,
        "patch": {
            "content": "content",
            "mode": "mode",
            "system_metadata": "system_metadata",
            "t_invalid": "t_invalid",
            "t_valid": "t_valid",
            "tags": "tags",
            "tier": "tier",
            "user_metadata": "user_metadata",
        },
    },
    "delete": {
        "selector": {
            "unit_ids": "item_id",
            "scope": DERIVED,
            "tags": "tags",
            "before": "before",
            "filters": "filters",
            "mode": "mode",
        },
    },
    "evolve": {
        "scope": DERIVED,
        "mode": "mode",
        "channel": "channel",
        "candidate": "candidate",
        "dreaming": "dreaming",
        "interval": "interval",
        "hierarchy_options": "hierarchy_options", "reuse_existing": "reuse_existing",
    },
    "inspect": {
        "unit_ids": "item_ids",
        "scope": DERIVED,
    },
    "trace": {
        "unit_id": "item_id",
        "scope": DERIVED,
    },
    "audit": {
        "filters": "filters",
        "limit": "limit",
    },
    "grant": {
        "grant": {
            "actions": "actions",
            "expires_at": "expires_at",
            "grantor": DERIVED,
            "grantee": DERIVED,
            "grant_id": gap("授权 ID 由服务端生成并返回，不接受请求侧指定"),
            "revoked": gap("撤销由 revoke 动作表达，不由 legacy 扁平面设置"),
        },
    },
    "revoke": {
        "grant": {
            "actions": "actions",
            "expires_at": "expires_at",
            "grantor": DERIVED,
            "grantee": DERIVED,
            "grant_id": "grant_id",
            "revoked": gap("撤销由 revoke 动作表达，不由 legacy 扁平面设置"),
        },
    },
    "create_space": {
        "spec": {
            "org": DERIVED,
            "space": DERIVED,
            "display_name": "display_name",
            "owner": "owner",
            "policy": "policy",
            "metadata": "metadata",
            "principal_path": "principal_path",
        },
    },
    "get_space": {
        "org": DERIVED,
        "space": DERIVED,
    },
    "list_spaces": {
        "org": DERIVED,
        "status": "status",
        "limit": "limit",
        "cursor": "cursor",
    },
    "update_space": {
        "org": DERIVED,
        "space": DERIVED,
        "patch": {
            "display_name": "display_name",
            "status": "status",
            "principal_path": "principal_path",
            "policy": "policy",
            "metadata": "metadata",
        },
    },
    "archive_space": {
        "org": DERIVED,
        "space": DERIVED,
    },
    "delete_space": {
        "org": DERIVED,
        "space": DERIVED,
        "mode": "mode",
    },
    "export_space": {
        "org": DERIVED,
        "space": DERIVED,
        "include_audit": "include_audit",
    },
    "space_usage": {
        "org": DERIVED,
        "space": DERIVED,
    },
    "get_space_policy": {
        "org": DERIVED,
        "space": DERIVED,
    },
    "set_space_policy": {
        "org": DERIVED,
        "space": DERIVED,
        "policy": {
            "require_space": "policy.require_space",
            "principal_path": "principal_path",
            "storage_isolation_strategy": "policy.storage_isolation_strategy",
            "retention": "policy.retention",
            "quotas": "policy.quotas",
            "index_profiles": "policy.index_profiles",
            "pipeline_profiles": "policy.pipeline_profiles",
        },
    },
    "list_space_members": {
        "org": DERIVED,
        "space": DERIVED,
    },
    "add_space_member": {
        "org": DERIVED,
        "space": DERIVED,
        "member": {
            "scope": DERIVED,
            "role": "role",
            "content_role": "content_role",
            "governance_role": "governance_role",
            "created_at": "created_at",
            "expires_at": "expires_at",
        },
    },
    "remove_space_member": {
        "org": DERIVED,
        "space": DERIVED,
        "member": DERIVED,
    },
}


@lru_cache(maxsize=1)
def _handler_tree() -> ast.Module:
    return ast.parse(_HANDLER_PATH.read_text(encoding="utf-8"), filename=str(_HANDLER_PATH))


def _route_verbs() -> dict[str, str]:
    """从 ``_ROUTES`` 取 verb → 处理函数名（静态解析，不导入私有成员）。"""
    routes: dict[str, str] = {}
    for node in _handler_tree().body:
        if not isinstance(node, ast.AnnAssign) or getattr(node.target, "id", "") != "_ROUTES":
            continue
        value = node.value
        assert isinstance(value, ast.Dict)
        for key, func in zip(value.keys, value.values):
            assert isinstance(key, ast.Constant) and isinstance(func, ast.Name)
            routes[str(key.value)] = func.id
    return routes


def _module_functions() -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in _handler_tree().body if isinstance(node, ast.FunctionDef)}


def _payload_reads(func_name: str) -> tuple[set[str], set[str]]:
    """返回 (payload 取值键, 任意变量取值键)，沿调用图传递闭包。"""
    functions = _module_functions()
    payload_keys: set[str] = set()
    any_keys: set[str] = set()
    pending = [func_name]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited or current not in functions:
            continue
        visited.add(current)
        for node in ast.walk(functions[current]):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr == "get":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        _record_read(target.value, node.args[0].value, payload_keys, any_keys)
                elif getattr(target, "id", "") == "_require":
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        payload_keys.add(str(node.args[1].value))
                elif isinstance(target, ast.Name) and target.id in functions:
                    pending.append(target.id)
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                _record_read(node.value, node.slice.value, payload_keys, any_keys)
            elif isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops):
                for comparator in node.comparators:
                    if isinstance(node.left, ast.Constant) and isinstance(comparator, ast.Name):
                        _record_read(comparator, node.left.value, payload_keys, any_keys)
    return payload_keys, any_keys


def _record_read(
    container: ast.expr, key: object, payload_keys: set[str], any_keys: set[str]
) -> None:
    if not isinstance(key, str) or not isinstance(container, ast.Name):
        return
    if container.id == "payload":
        payload_keys.add(key)
    else:
        any_keys.add(key)


def _dataclass_fields(annotation: object) -> set[str] | None:
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return {field.name for field in dataclasses.fields(annotation)}
    return None


def _iter_surface_entries(verb: str):
    """展开 (参数, 归属) —— dataclass 参数展开成 (参数.字段, 归属)。"""
    contract = method_contract(verb)
    hints = contract.type_hints
    for param, entry in _LEGACY_SURFACE[verb].items():
        if isinstance(entry, dict):
            for field_name, field_entry in entry.items():
                yield f"{param}.{field_name}", field_entry, hints.get(param)
        else:
            yield param, entry, hints.get(param)


def test_legacy_routes_are_fully_classified() -> None:
    """路由表里每个 verb 要么在覆盖表内，要么显式登记为聚合入口。"""
    routes = _route_verbs()
    classified = set(_LEGACY_SURFACE) | set(_NON_CONTRACT_VERBS)
    assert classified == set(routes), (
        f"路由未登记: {sorted(set(routes) - classified)}；"
        f"登记了不存在的路由: {sorted(classified - set(routes))}"
    )
    for verb in _NON_CONTRACT_VERBS:
        assert not is_known_verb(verb), f"{verb} 已有同名 MemoryAPI 方法，应移入 _LEGACY_SURFACE"
    for verb in _LEGACY_SURFACE:
        assert is_known_verb(verb), f"{verb} 不是 MemoryAPI 方法"
    for reason in _NON_CONTRACT_VERBS.values():
        assert reason.strip()


@pytest.mark.parametrize("verb", sorted(_LEGACY_SURFACE))
def test_legacy_surface_covers_every_contract_parameter(verb: str) -> None:
    """契约的每个入参都必须有 legacy 归属；dataclass 参数必须细化到字段。"""
    contract = method_contract(verb)
    declared = _LEGACY_SURFACE[verb]
    expected = set(contract.request_parameters)
    assert set(declared) == expected, (
        f"{verb} 契约参数与 legacy 覆盖表不一致——"
        f"未声明: {sorted(expected - set(declared))}，"
        f"多余声明: {sorted(set(declared) - expected)}"
    )
    for param, entry in declared.items():
        fields = _dataclass_fields(contract.type_hints.get(param))
        if fields is None:
            assert not isinstance(entry, dict), f"{verb}.{param} 不是 dataclass，不能细化到字段"
            continue
        if isinstance(entry, dict):
            assert set(entry) == fields, (
                f"{verb}.{param}（{param} 类型）字段未覆盖——"
                f"未声明: {sorted(fields - set(entry))}，"
                f"多余声明: {sorted(set(entry) - fields)}"
            )
        else:
            assert param in _CONTEXT_PARAMETERS or (verb, param) in _WHOLE_REQUEST_DERIVED, (
                f"{verb}.{param} 是 dataclass，必须细化到字段或登记进 _WHOLE_REQUEST_DERIVED"
            )


@pytest.mark.parametrize("verb", sorted(_LEGACY_SURFACE))
def test_declared_legacy_keys_are_read_by_handler(verb: str) -> None:
    """声明为 legacy 键的归属，键必须真的被该 verb 的取值路径读到。"""
    routes = _route_verbs()
    payload_keys, any_keys = _payload_reads(routes[verb])
    for name, entry, _annotation in _iter_surface_entries(verb):
        if not isinstance(entry, str):
            continue
        segments = entry.split(".")
        assert segments[0] in payload_keys, (
            f"{verb} 声明 {name} 走 legacy 键 {entry!r}，但 payload 从未读取 {segments[0]!r}"
            f"（已读取: {sorted(payload_keys)}）"
        )
        for segment in segments[1:]:
            assert segment in any_keys, (
                f"{verb} 声明 {name} 走 legacy 键 {entry!r}，但嵌套键 {segment!r} 从未被读取"
                f"（已读取: {sorted(any_keys)}）"
            )


@pytest.mark.parametrize("verb", sorted(_LEGACY_SURFACE))
def test_declared_gaps_are_still_missing(verb: str) -> None:
    """gap 项必须写明原因且确实不可达——补上透传后本测试会失败，迫使删除该行。"""
    routes = _route_verbs()
    payload_keys, any_keys = _payload_reads(routes[verb])
    for name, entry, _annotation in _iter_surface_entries(verb):
        if not isinstance(entry, _Gap):
            continue
        assert len(entry.reason.strip()) >= 8, f"{verb}.{name} 的 gap 必须写明原因"
        assert name.split(".")[-1] not in (payload_keys | any_keys), (
            f"{verb}.{name} 已能被 legacy 面读到，请从 _LEGACY_SURFACE 的 gap 移除该行"
        )
