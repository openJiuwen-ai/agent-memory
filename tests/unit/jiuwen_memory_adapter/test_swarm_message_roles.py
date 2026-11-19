# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F16 Swarm 适配：_HttpClient / _InProcessClient batch_add 与 sync_turn 单测。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwen_memory.api import Scope as ApiScope
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import (
    AuthContext,
    Role,
    Surface,
)
from jiuwen_memory.common.type_def import MESSAGE_ROLE_KEY, MemoryUnit, Segment
from jiuwen_memory.control.types import BatchWriteItem, BatchWriteOutcome, BatchWriteResult
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
    AgentMemoryMemoryProvider,
    _HttpClient,
    _InProcessClient,
    _Scope,
)

pytestmark = pytest.mark.unit


def _security_provider(scope: ApiScope):
    ctx = new_request_context(
        AuthContext(
            actor=scope,
            role=Role.ROOT,
            credential_type="internal",
            auth_method="internal",
            authenticated_at=datetime.now(UTC),
        ),
        surface=Surface.INTERNAL,
        request_id="req-swarm-f16",
    )
    return lambda: ctx


def test_http_client_batch_add_parses_batch_write_result_contract():
    client = _HttpClient("http://127.0.0.1:8137")
    scope = _Scope(org="o", space="s", user="u", agent="a", session="s1")
    items = [
        {"content": "hello", "system_metadata": {MESSAGE_ROLE_KEY: "user"}},
        {"content": "hi", "system_metadata": {MESSAGE_ROLE_KEY: "assistant"}},
    ]

    async def run():
        # 1) 全成功且有派生 unit（真实 HTTP BatchWriteResult 响应格式）
        client._post_verb = AsyncMock(  # pylint: disable=protected-access
            return_value={
                "outcomes": [
                    {"index": 0, "units": [], "error": "", "error_type": ""},
                    {
                        "index": 1,
                        "units": [
                            {
                                "id": "derived-1",
                                "tier": "semantic",
                                "segments": [{"content": "extracted fact"}],
                            }
                        ],
                        "error": "",
                        "error_type": "",
                    },
                ]
            }
        )
        res = await client.batch_add(
            items,
            scope,
            tags=["conversation"],
            system_metadata={"infer": "true"},
        )
        assert res is not None
        assert res.item_id == "derived-1"
        assert res.content == "extracted fact"
        assert res.tier == "semantic"

        # 2) 全成功但派生为空（dedup-noop）→ _AddResult(item_id=None)
        client._post_verb = AsyncMock(  # pylint: disable=protected-access
            return_value={
                "outcomes": [
                    {"index": 0, "units": [], "error": "", "error_type": ""},
                    {"index": 1, "units": [], "error": "", "error_type": ""},
                ]
            }
        )
        noop = await client.batch_add(items, scope)
        assert noop is not None and noop.item_id is None

        # 3) 部分失败：记 warning、不触发传输熔断、返回成功项结果
        breaker_spy = Mock()
        client._record_failure = breaker_spy  # pylint: disable=protected-access
        client._post_verb = AsyncMock(  # pylint: disable=protected-access
            return_value={
                "outcomes": [
                    {"index": 0, "units": [], "error": "bad", "error_type": "ValidationError"},
                    {
                        "index": 1,
                        "units": [
                            {
                                "id": "u-ok",
                                "tier": "semantic",
                                "segments": [{"content": "ok"}],
                            }
                        ],
                        "error": "",
                        "error_type": "",
                    },
                ]
            }
        )
        partial = await client.batch_add(items, scope)
        assert partial is not None and partial.item_id == "u-ok"
        assert breaker_spy.call_count == 0, "单项业务失败不应触发传输熔断"

        # 4) 全失败 → None
        client._post_verb = AsyncMock(  # pylint: disable=protected-access
            return_value={
                "outcomes": [
                    {"index": 0, "units": [], "error": "bad", "error_type": "ValidationError"},
                    {"index": 1, "units": [], "error": "bad", "error_type": "ValidationError"},
                ]
            }
        )
        assert await client.batch_add(items, scope) is None

        # 5) 传输降级（_post_verb 返 None）→ None
        client._post_verb = AsyncMock(return_value=None)  # pylint: disable=protected-access
        assert await client.batch_add(items, scope) is None

    asyncio.run(run())


def test_in_process_client_batch_add_and_sync_turn_whitespace_guard():
    scope = ApiScope(org="o", space="s", user="u", agent="a", session="s1")
    derived_unit = MemoryUnit(id="u-derived", scope=scope, segments=[Segment(content="fact")])

    async def run():
        fake_api = SimpleNamespace(
            batch_add_async=AsyncMock(
                return_value=BatchWriteResult(
                    outcomes=[
                        BatchWriteOutcome(
                            index=0,
                            item=BatchWriteItem("hi"),
                            error="bad",
                            error_type="ValidationError",
                        ),
                        BatchWriteOutcome(index=1, item=BatchWriteItem("ok"), units=[derived_unit]),
                    ]
                )
            )
        )
        client = object.__new__(_InProcessClient)
        client._api = fake_api  # pylint: disable=protected-access
        client._security_provider = _security_provider(ApiScope(org="o", user="u"))  # pylint: disable=protected-access
        client._bound_collective_coordinates = None  # pylint: disable=protected-access
        client.request_identity = None

        res = await client.batch_add(
            [
                {"content": "hi", "system_metadata": {MESSAGE_ROLE_KEY: "user"}},
                {"content": "ok", "system_metadata": {MESSAGE_ROLE_KEY: "assistant"}},
            ],
            _Scope(org="o", space="s", user="u", agent="a", session="s1"),
            tags=["conversation"],
            system_metadata={"infer": "true"},
        )
        assert res is not None and res.item_id == "u-derived"
        passed_items = fake_api.batch_add_async.await_args.args[0]
        assert [i.system_metadata for i in passed_items] == [
            {MESSAGE_ROLE_KEY: "user"},
            {MESSAGE_ROLE_KEY: "assistant"},
        ]

        # 全失败返 None
        fake_api.batch_add_async = AsyncMock(
            return_value=BatchWriteResult(
                outcomes=[
                    BatchWriteOutcome(
                        index=0,
                        item=BatchWriteItem("hi"),
                        error="bad",
                        error_type="ValidationError",
                    ),
                ]
            )
        )
        all_failed = await client.batch_add(
            [{"content": "hi"}], _Scope(org="o", space="s", user="u")
        )
        assert all_failed is None

        # sync_turn 空白正文直接跳过不发请求
        provider = AgentMemoryMemoryProvider(agent_id="a", portal={"enabled": False})
        provider._initialized = True  # pylint: disable=protected-access
        mock_client = SimpleNamespace(batch_add=AsyncMock())
        provider._client = mock_client  # pylint: disable=protected-access
        await provider.sync_turn("   ", "valid reply")
        await provider.sync_turn("valid prompt", "   \n ")
        assert mock_client.batch_add.await_count == 0, "空白消息不应调用 batch_add"

    asyncio.run(run())
