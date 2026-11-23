# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``sequential`` 编排：兼容开关（F10 决策 3 真值表第 4 行）。

不分组、不并发、不合并，结果与执行顺序和引入编排前一致；新增的只有 per-user 锁的
等待与超时。逐条执行逻辑与 ``grouped`` 的兼容路径完全共用，避免出现第二套实现。
"""

from __future__ import annotations

from jiuwen_memory.control.types import BatchWriteOutcome, BatchWriteResult
from jiuwen_memory.control.write_orchestrator import WriteOrchestratorProducer, WriteTask
from jiuwen_memory.control.write_orchestrator_impl.grouped import (
    GroupedWriteOrchestrator,
    _optional_int,
)
from jiuwen_memory.control.write_orchestrator_impl.segments import (
    Entry,
    executor_from_config,
    validate_lock_scope,
)


class SequentialWriteOrchestrator(GroupedWriteOrchestrator):
    """第 4 行：不论配置如何，一律逐条、按输入顺序执行并加锁。"""

    async def write(self, engine, task: WriteTask) -> BatchWriteResult:
        if not task.items:
            return BatchWriteResult(outcomes=[])
        results: list[BatchWriteOutcome | None] = [None] * len(task.items)
        entries = [Entry(index=index, item=item) for index, item in enumerate(task.items)]
        await self._run_sequential(engine, entries, task, results)
        return BatchWriteResult(
            outcomes=[
                outcome
                if outcome is not None
                else self._failed(task.items[index], RuntimeError("write outcome missing"))
                for index, outcome in enumerate(results)
            ]
        )

    async def _run_grouped(self, engine, entries, task, results) -> None:
        # 兼容模式下即使误调也退回逐条路径，不做分组并发。
        await self._run_sequential(engine, entries, task, results)


@WriteOrchestratorProducer.register("sequential")
def _build(config):
    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.common.lock import LockProducer

    lock = LockProducer.dep(config, "lock")
    validate_lock_scope(config.ctx, lock)
    return SequentialWriteOrchestrator(
        lock=lock,
        executor=executor_from_config(config),
        lock_name=str(Factory.cfg_get(config, "lock_name", "infer_write")),
        lock_wait_ms=_optional_int(Factory.cfg_get(config, "lock_wait_ms", None)),
    )
