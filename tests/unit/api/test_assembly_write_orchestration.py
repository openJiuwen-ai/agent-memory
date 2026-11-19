# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入编排的装配边界与调用级开关（F10 决策 3、7）。"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import BatchWriteItem
from tests.support.scoped_authenticator import ScopedAuthenticator

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="alice")


def _security(actor: str = "alice"):
    return internal_context(ScopedAuthenticator(Scope(org="acme", space="", user=actor)))


def test_default_assembly_uses_memory_lock() -> None:
    api = assemble()
    try:
        assert api is not None, "默认装配必须开箱可用"
    finally:
        api.close()


def test_multi_instance_memory_lock_fails_assembly() -> None:
    with pytest.raises(ValidationError, match="redis"):
        assemble(config={"deployment": {"multi_instance": True}})


def test_multi_instance_redis_lock_assembles() -> None:
    api = assemble(
        {
            "deployment": {"multi_instance": True},
            "lock": {
                "default": {
                    "target": "redis",
                    "params": {"url": "redis://localhost:6379/0"},
                }
            },
        }
    )
    try:
        assert api is not None, "声明多实例且配了 redis 锁时应装配成功"
    finally:
        api.close()


def test_lock_deleted_fails_assembly() -> None:
    with pytest.raises(ValidationError):
        assemble(config={"lock": {"default": None}})


def test_lock_deleted_sequential_also_fails() -> None:
    """sequential 也加锁：删掉锁配置不能靠切换编排器绕过。"""
    with pytest.raises(ValidationError):
        assemble(
            config={
                "lock": {"default": None},
                "write": {"default": {"target": "sequential", "params": {}}},
            }
        )


def test_unknown_orchestrator_target_fails_assembly() -> None:
    with pytest.raises(ValidationError, match="未注册"):
        assemble(config={"write": {"default": {"target": "bogus", "params": {"lock": "default"}}}})


def test_multi_instance_must_be_bool() -> None:
    with pytest.raises(ValidationError, match="multi_instance"):
        assemble(config={"deployment": {"multi_instance": "yes"}})


def test_merge_extract_from_call_metadata(monkeypatch) -> None:
    api = assemble()
    captured = {}
    original = api._commands._orchestrator.write

    async def spy(engine, task):
        captured["merge_extract"] = task.merge_extract
        return await original(engine, task)

    monkeypatch.setattr(api._commands._orchestrator, "write", spy)
    try:
        api.add(
            "调用级关闭合并抽取",
            _SCOPE,
            security=_security(),
            system_metadata={"merge_extract": "false"},
        )
    finally:
        api.close()

    assert captured["merge_extract"] is False, "调用级 system_metadata.merge_extract 必须生效"


def test_merge_extract_is_logical_and_with_deployment_default(monkeypatch) -> None:
    api = assemble(config={"deployment": {"merge_extract": False}})
    captured = {}
    original = api._commands._orchestrator.write

    async def spy(engine, task):
        captured["merge_extract"] = task.merge_extract
        return await original(engine, task)

    monkeypatch.setattr(api._commands._orchestrator, "write", spy)
    try:
        api.add(
            "部署级关闭后调用级不能打开",
            _SCOPE,
            security=_security(),
            system_metadata={"merge_extract": True},
        )
    finally:
        api.close()

    assert captured["merge_extract"] is False, (
        "部署级 false 必须压过调用级 true（逻辑与），否则关闭开关可被单次请求绕过"
    )


def test_batch_item_merge_extract_stripped_from_unit() -> None:
    from jiuwen_memory.control.types import BatchWriteItem

    api = assemble()
    items = [
        BatchWriteItem(
            content="单项上的开关键",
            scope=_SCOPE,
            system_metadata={"merge_extract": "true"},
        )
    ]
    try:
        result = api.batch_add(items, security=_security())
        stored = api.get(result.outcomes[0].units[0].id, _SCOPE, security=_security())
    finally:
        api.close()

    assert "merge_extract" not in (stored.system_metadata or {}), (
        "单项 system_metadata 上的开关键没有语义，必须剥离且不得落盘"
    )


def test_merge_extract_invalid_value_rejected() -> None:
    api = assemble()
    try:
        with pytest.raises(ValidationError, match="merge_extract"):
            api.add(
                "非法开关",
                _SCOPE,
                security=_security(),
                system_metadata={"merge_extract": "no"},
            )
    finally:
        api.close()


def test_merge_extract_not_persisted() -> None:
    api = assemble()
    try:
        unit = api.add(
            "开关不落盘",
            _SCOPE,
            security=_security(),
            system_metadata={"merge_extract": "false"},
        )[0]
        stored = api.get(unit.id, _SCOPE, security=_security())
    finally:
        api.close()

    assert "merge_extract" not in (stored.system_metadata or {}), (
        "调用级开关是写入边界参数，不是条目属性"
    )


def test_multi_instance_checks_the_lock_actually_used() -> None:
    """校验必须看编排器实际用的锁，而不是装配根另建的实例。"""

    with pytest.raises(ValidationError, match="redis"):
        assemble(
            config={
                "deployment": {"multi_instance": True},
                # 根上配的是 redis（旧实现会据此放行）……
                "lock": {
                    "default": {
                        "target": "redis",
                        "params": {"url": "redis://localhost:6379/0"},
                    }
                },
                # ……但写入实际使用内联的进程内锁。
                "write": {
                    "default": {
                        "target": "grouped",
                        "params": {"lock": {"target": "memory", "params": {}}},
                    }
                },
            }
        )


def test_multi_instance_passes_when_write_lock_is_cross_instance() -> None:
    api = assemble(
        config={
            "deployment": {"multi_instance": True},
            "lock": {
                "default": {
                    "target": "redis",
                    "params": {"url": "redis://localhost:6379/0"},
                }
            },
        }
    )
    try:
        assert api is not None
    finally:
        api.close()


def test_sequential_orchestrator_preserves_public_write_behaviour() -> None:
    """换成 sequential 后上层行为不变：单条可写、批量逐条提交、fail-fast 一致。"""
    api = assemble(
        config={
            "write": {
                "default": {
                    "target": "sequential",
                    "params": {"lock": "default", "executor": "default"},
                }
            }
        }
    )
    try:
        units = api.add("串行写入", _SCOPE, security=_security())
        result = asyncio.run(
            api.batch_add_async(
                [
                    BatchWriteItem(content=f"条目{i}", scope=_SCOPE)
                    for i in range(3)
                ],
                security=_security(),
            )
        )
    finally:
        api.close()

    assert len(units) == 1, "单条 add 在 sequential 下仍返回写入的记忆"
    assert all(outcome.units and not outcome.error for outcome in result.outcomes)
    assert [outcome.index for outcome in result.outcomes] == [0, 1, 2]


def test_cross_scope_write_denied_in_both_modes() -> None:
    """合并模式不得绕过跨 Scope 鉴权：拒绝与逐条模式一致，且不进入引擎。"""
    api = assemble()
    calls = {"count": 0}
    original = api._engine.write_group

    async def counting(items, *, merge_extract):
        calls["count"] += 1
        return await original(items, merge_extract=merge_extract)

    api._engine.write_group = counting  # type: ignore[method-assign]
    foreign = Scope(org="other-org", space="", user="alice")
    items = [BatchWriteItem(content=f"越界{i}", scope=foreign) for i in range(2)]
    try:
        merged = asyncio.run(api.batch_add_async(items, security=_security()))
        per_item = asyncio.run(
            api.batch_add_async(
                items, security=_security(), system_metadata={"merge_extract": "false"}
            )
        )
    finally:
        api.close()

    assert calls["count"] == 0, "跨 Scope 写入必须在引擎之前被拒绝"
    assert [outcome.error_type for outcome in merged.outcomes] == [
        "PermissionDeniedError"
    ] * 2, "跨 Scope 必须由鉴权拒绝，而不是参数校验"
    assert [outcome.error_type for outcome in per_item.outcomes] == [
        "PermissionDeniedError"
    ] * 2, "合并与逐条模式的拒绝类型一致"
