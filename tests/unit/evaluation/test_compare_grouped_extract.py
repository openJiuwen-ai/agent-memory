# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""合并抽取 A/B 脚本的离线口径。"""

from __future__ import annotations

import pytest

from evaluation.scripts.compare_grouped_extract import compare
from jiuwen_memory.control.types import BatchWriteOutcome

pytestmark = pytest.mark.unit


class _Engine:
    """合并开启时按批执行、关闭时逐条执行（与真实引擎的抽取粒度一致）。"""

    def __init__(self) -> None:
        self.write_group_calls = 0
        self.evolve_calls = 0
        self.scopes: list[str] = []

    async def write_group(self, items, *, merge_extract: bool):
        self.write_group_calls += 1
        self.scopes.extend(item.scope.org for item in items)
        self.evolve_calls += 1 if merge_extract else len(items)
        return [
            BatchWriteOutcome(index=index, item=item, units=[])
            for index, item in enumerate(items)
        ]


def test_compare_isolates_rounds_and_counts_real_calls() -> None:
    engines: list[_Engine] = []

    def factory() -> _Engine:
        engine = _Engine()
        engines.append(engine)
        return engine

    report = compare(factory, [["a", "b"], ["c"]])

    assert len(engines) == 2, "两轮必须各自新建引擎，不能共享已写入的状态"
    assert engines[0].scopes == ["eval-off"] * 3, "关闭轮使用自己的 scope"
    assert engines[1].scopes == ["eval-on"] * 3, "开启轮使用自己的 scope"
    assert report["off"]["write_group_calls"] == 2, "每个 batch 只调一次 write_group"
    assert report["on"]["write_group_calls"] == 2
    assert engines[0].evolve_calls == 3, "关闭合并：逐条抽取"
    assert engines[1].evolve_calls == 2, "开启合并：每批一次抽取"
    assert report["off"]["items"] == report["on"]["items"] == 3
    assert report["derived_delta"] == 0 and report["failure_delta"] == 0
    # 没有 Evolver 的引擎不产生抽取指标；真实装配下这两项来自 Evolver 探针。
    assert report["off"]["extract_calls"] == report["on"]["extract_calls"] == 0
