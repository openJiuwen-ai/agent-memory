# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager


@pytest.mark.asyncio
async def test_related_old_memories_concurrent():
    """三条新记忆的检索时间区间应互相重叠，而不是依次排队。"""
    manager = FragmentMemoryManager(memory_index=MagicMock())
    spans = []

    async def fake_search(**kwargs):
        enter = time.monotonic()
        await asyncio.sleep(0.05)
        spans.append((enter, time.monotonic()))
        return [{"id": "m1", "score": 0.9, "mem": "旧记忆"}]

    manager.search = fake_search
    result = await manager._get_related_old_memories(
        {"a": "新记忆1", "b": "新记忆2", "c": "新记忆3"}, "u", "s")

    assert result == {"m1": "旧记忆"}   # 三次命中同一条，去重后只剩一条
    assert len(spans) == 3
    assert spans[1][0] < spans[0][1]    # 第 2 次在第 1 次结束前开始
    assert spans[2][0] < spans[1][1]    # 第 3 次在第 2 次结束前开始


@pytest.mark.asyncio
async def test_related_old_memories_result_filter_and_dedup():
    """结果一致性：低于阈值的命中被过滤，重复 id 只保留一次。"""
    manager = FragmentMemoryManager(memory_index=MagicMock())

    async def fake_search(**kwargs):
        if kwargs["query"] == "新记忆1":
            return [{"id": "m1", "score": 0.9, "mem": "旧记忆1"},
                    {"id": "m2", "score": 0.5, "mem": "低分旧记忆"}]
        return [{"id": "m1", "score": 0.8, "mem": "旧记忆1"},
                {"id": "m3", "score": 0.85, "mem": "旧记忆3"}]

    manager.search = fake_search
    result = await manager._get_related_old_memories(
        {"a": "新记忆1", "b": "新记忆2"}, "u", "s")

    assert result == {"m1": "旧记忆1", "m3": "旧记忆3"}
