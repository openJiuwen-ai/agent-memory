# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""WriteOrchestrator — 写入编排端口（F10 决策 2）。

位于 ``MemoryCommandService`` 与 ``MemoryEngine`` 之间：把已鉴权、已归一化的批量条目
切成「锁段」与「抽取批」，按配置决定是否合并抽取、是否跨组并发、是否加锁，再调用
Engine 的批量写入原语。**只有编排，不鉴权、不审计、不调用 LLM。**

两种实现由配置 ``write.orchestrator`` 选择：

- ``grouped``（缺省）：按 ``(载体 scope, stream_id)`` 分组 → 组内切批 → 组间有界并发；
- ``sequential``：兼容开关，不分组、不并发、不合并（但 infer 仍加 per-user 锁）。

锁的粒度与去重粒度一致（``(org, space, user)``），以「同一锁 key 的连续 infer 段」
为单位获取与释放；任何时刻只持有一把锁，因此混合多个用户的批次不会死锁。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.control.types import BatchWriteItem, BatchWriteOutcome, BatchWriteResult

from .base import ControlOperator


class WriteOrchestratorProducer(Factory):
    """写入编排的注册式工厂（配置项 ``write``）。"""

    TOP_NAME = "write"


@dataclass
class WriteTask:
    """一次批量写入编排的输入。

    ``merge_extract`` 由调用级开关（``merge_extract``）与部署级开关合并后的最终值决定；
    ``continue_on_error`` 为 False 时编排器退回逐条执行（真值表第 3 行）。
    """

    items: list[BatchWriteItem] = field(default_factory=list)
    merge_extract: bool = True
    continue_on_error: bool = True


class WriteOrchestrator(ControlOperator):
    """批量写入的编排契约。"""

    @abstractmethod
    async def write(self, engine, task: WriteTask) -> BatchWriteResult:
        """执行一次批量写入，返回与 ``task.items`` 逐条对齐的结果。"""

    @abstractmethod
    async def batch_write_aligned(
        self,
        engine,
        items: list[BatchWriteItem],
        origins: list[tuple[int, BatchWriteItem]],
        *,
        merge_extract: bool,
        continue_on_error: bool,
    ) -> list[BatchWriteOutcome]:
        """写已鉴权条目，并把调用方下标与原始条目回填到结果上。"""
