# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""批量写入 [成功, 失败, 成功] 的回执与真实存储必须一致（F10 决策 3、4）。"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import BatchWriteItem
from tests.support.scoped_authenticator import ScopedAuthenticator

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="alice")


def _security():
    return internal_context(ScopedAuthenticator(Scope(org="acme", space="", user="alice")))


class _FlakyBuilder:
    """第二条正文写入失败；其余委托真实构建器。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    @staticmethod
    def supports_grouped_build() -> bool:
        return False

    def build(self, units):
        content = units[0].segments[0].content
        self.calls.append(content)
        if content == "第二条":
            raise RuntimeError("build failed")
        return self._inner.build(units)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _items() -> list[BatchWriteItem]:
    return [
        BatchWriteItem(content=text, scope=_SCOPE) for text in ("第一条", "第二条", "第三条")
    ]


def test_fail_fast_stops_after_first_failure_and_persists_nothing_after() -> None:
    api = assemble()
    api._engine._index = _FlakyBuilder(api._engine._index)  # type: ignore[assignment]
    try:
        result = asyncio.run(
            api.batch_add_async(_items(), continue_on_error=False, security=_security())
        )
        stored = [
            api.get(outcome.units[0].id, _SCOPE, security=_security()) if outcome.units else None
            for outcome in result.outcomes
        ]
    finally:
        api.close()

    assert result.outcomes[0].units, "第一条必须写入"
    assert result.outcomes[1].error, "第二条必须报错"
    assert result.outcomes[2].error_type == "Skipped", "fail-fast 后第三条不再执行"
    assert stored[0] is not None and stored[0].content == "第一条"
    assert stored[2] is None, "被跳过的条目不能出现在真源里"


def test_continue_on_error_keeps_later_items() -> None:
    api = assemble()
    api._engine._index = _FlakyBuilder(api._engine._index)  # type: ignore[assignment]
    try:
        result = asyncio.run(
            api.batch_add_async(_items(), continue_on_error=True, security=_security())
        )
        ids = [outcome.units[0].id if outcome.units else None for outcome in result.outcomes]
        third = api.get(ids[2], _SCOPE, security=_security())
        with pytest.raises(NotFoundError):
            api.get("missing-after-failure", _SCOPE, security=_security())
    finally:
        api.close()

    assert result.outcomes[0].units and result.outcomes[2].units, "失败条目之外的必须落盘"
    assert result.outcomes[1].error, "失败条目标错误"
    assert not result.outcomes[2].error, "后续条目不得被连带标错"
    assert third.content == "第三条"


def test_real_engine_per_item_failure_logs_and_chains(monkeypatch) -> None:
    """真实引擎的逐条失败必须留下 ERROR 现场，且单条入口带异常链。"""
    from jiuwen_memory.common.errors import AgentMemoryError
    from jiuwen_memory.control.engine_impl import grouped_write

    api = assemble()
    api._engine._index = _FlakyBuilder(api._engine._index)  # type: ignore[assignment]
    logged: list[tuple] = []
    monkeypatch.setattr(
        grouped_write.logger, "exception", lambda *args, **kwargs: logged.append((args, kwargs))
    )
    try:
        with pytest.raises(AgentMemoryError) as caught:
            asyncio.run(api.add_async("第二条", _SCOPE, security=_security()))
    finally:
        api.close()

    assert isinstance(caught.value.__cause__, RuntimeError), "异常链必须保留原始异常"
    assert str(caught.value) == "unexpected write failure", "回执不回显内部原文"
    assert logged, "逐条失败必须写 ERROR 日志，否则回执屏蔽原文后就再无现场"
    for args, kwargs in logged:
        rendered = " ".join(str(value) for value in args) + " ".join(
            str(value) for value in kwargs.values()
        )
        assert "第二条" not in rendered, "日志参数不得包含记忆正文"
