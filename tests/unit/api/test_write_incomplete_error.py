# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""未知提交结果在单条与批量入口必须给出同一个领域异常（F10 决策 4）。"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.errors import WriteIncompleteError
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.construction.index_builder import UnitCommitState, UnitCommitStatus
from jiuwen_memory.control.types import BatchWriteItem
from tests.support.scoped_authenticator import ScopedAuthenticator

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="alice")


def _security():
    return internal_context(ScopedAuthenticator(Scope(org="acme", space="", user="alice")))


class _UncertainBuilder:
    """所有写入都返回 UNKNOWN：落盘状态无法确认。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    @staticmethod
    def supports_grouped_build() -> bool:
        return True

    @staticmethod
    def build_grouped(units):
        return [
            UnitCommitStatus(
                unit_id=unit.id,
                state=UnitCommitState.UNKNOWN,
                error=RuntimeError("commit outcome unknown"),
            )
            for unit in units
        ]

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_add_async_raises_write_incomplete_error() -> None:
    api = assemble()
    api._engine._index = _UncertainBuilder(api._engine._index)  # type: ignore[assignment]
    try:
        with pytest.raises(WriteIncompleteError) as caught:
            asyncio.run(api.add_async("未知提交", _SCOPE, security=_security()))
    finally:
        api.close()

    assert caught.value.status == "unknown"
    assert caught.value.retry_action == "recover_then_retry"
    assert caught.value.refs == (), "无法确认的写入不能声称已保存"


def test_batch_add_reports_same_error_type() -> None:
    api = assemble()
    api._engine._index = _UncertainBuilder(api._engine._index)  # type: ignore[assignment]
    try:
        result = asyncio.run(
            api.batch_add_async(
                [BatchWriteItem(content="未知提交", scope=_SCOPE)],
                security=_security(),
            )
        )
    finally:
        api.close()

    outcome = result.outcomes[0]
    assert outcome.error_type == "WriteIncompleteError", "批量入口必须与单条一致"
    assert outcome.units == [], "无法确认的写入不得回显为已保存"
    assert outcome.error, "必须带可诊断的错误"
