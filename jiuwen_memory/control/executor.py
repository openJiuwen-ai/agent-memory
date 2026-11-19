# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BlockingExecutor — 控制层阻塞执行器端口（F10 决策 1）。

写入链路里仍有大量同步阻塞调用：Router / Classifier / Evolver 的 LLM 调用、sqlite 与
审计的 IO、Ingestor 的多模态解析。它们跑在事件循环上时会把整个循环占住，进程内 SDK
宿主的其他请求与定时任务都要等。

本端口只定义三条语义：**选通道**、**交给线程**、**关闭**。通道按耗时特征分成两条：

- ``llm``：一次占用线程数秒的调用（路由、分类、抽取）；
- ``io``：短调用（鉴权、审计、空间状态、ingest、建索引）。

分池的理由是排队：LLM 调用占着线程时，短 IO 不应跟着排在同一队列后面。

**重入矩阵**（F10 决策 1，必须逐条保持）：

| 提交方 | 目标通道 | 行为 |
|---|---|---|
| 事件循环 / 任意线程 | 任意 | 提交到目标池 |
| ``llm`` 工作线程 | ``llm`` / ``io`` | 在当前线程执行 |
| ``io`` 工作线程 | ``io`` | 在当前线程执行 |
| ``io`` 工作线程 | ``llm`` | 提交到 llm 池 |

``llm`` 工作线程一律内联，是为了让「LLM 调用内部再等 IO」不会因为池只有一条线程而
自锁；``io`` → ``llm`` 仍然排队，避免短 IO 线程被 LLM 调用长期占住。

**加锁/释放锁/续租不经过本端口**：``LockProvider`` 是异步契约（Redis 实现用异步客户端，
等锁走 ``asyncio.sleep``），在事件循环上本来就不阻塞；塞进线程池反而要在线程里再造一个
事件循环（F10 决策 1）。
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from jiuwen_memory.common.factory.factory import Factory

from .base import ControlOperator

T = TypeVar("T")


class ExecutorLane(str, Enum):
    """执行器通道。取值同时用作线程名后缀与日志标识。"""

    LLM = "llm"
    IO = "io"


class BlockingExecutorProducer(Factory):
    """阻塞执行器的注册式工厂（配置项 ``executor``）。"""

    TOP_NAME = "executor"


class BlockingExecutor(ControlOperator):
    """把同步阻塞调用交给工作线程，并保证重入不死锁。"""

    @abstractmethod
    async def run(self, lane: ExecutorLane, fn: Callable[..., T], /, *args, **kwargs) -> T:
        """在 ``lane`` 对应的线程上执行 ``fn``，返回其结果。

        提交方已经在该通道（或 llm 通道）的工作线程上时在当前线程执行，见模块文档的
        重入矩阵。``fn`` 必须是同步可调用对象；它抛出的异常原样传播给调用方。

        任务携带提交时刻的 ``contextvars`` 快照，因此 trace、privacy staging、ingest
        取消信号在线程内可见。注意线程内对 ContextVar 的 ``set`` 不会回传到协程——
        需要回传的状态必须放在可变对象里。
        """

    @abstractmethod
    def current_lane(self) -> ExecutorLane | None:
        """当前线程属于本执行器的哪个通道；不是工作线程时返回 ``None``。"""

    @abstractmethod
    def close(self, *, wait: bool = True) -> None:
        """停止接收新任务并关闭线程池。

        ``wait=True`` 等已提交任务跑完再返回；``wait=False`` 立即返回，由调用方在关闭
        屏障里另等。关闭后 ``run`` 抛 ``RuntimeError``。重复调用幂等。
        """
