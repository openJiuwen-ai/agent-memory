"""Watchdog — 文档看门狗算子契约：监听 md 文件变更，同步影子索引。

文档记忆（F07/F08）下，md 是人类可读视图、影子索引是机器真源。用户手改 md 后，
看门狗把改动同步进影子索引，保持 md 与影子索引一致（F07 §12）。

**与 Store 算子的差异**：Store 是被动数据后端（被 ``CompositeStorage`` 持有、按
``scope`` 串参调 CRUD）；本算子是**反向驱动组件**（主动监听文件事件 → 反向调
影子索引的 ``insert_units``/``delete_units``）。故：

- 不继承 ``BaseStore``——它没有 insert/get/search 等 CRUD 动词，硬塞会污染
  ``BaseStore`` 的存储语义；
- 不进 ``StorageCapability`` 枚举——它不提供任何数据存储能力，硬加一项会让
  ``CompositeStorage.capabilities()`` 语义混乱；
- 不被 ``CompositeStorage._stores`` 持有——生命周期独立挂在 ``Kernel``，随事件
  循环 start/stop（与 Store 算子「构造即就绪」不同，看门狗需事件循环引用才能
  起 Observer 线程桥接）。

**装配范式**（与 markdown/shadow 同构）：契约 + 注册式工厂 ``WatchdogProducer``
同处本接口模块；实现在 ``watchdog_impl`` 下用 ``@WatchdogProducer.register("<后端>")``
自注册；由 :func:`storage.bootstrap.register_backends` 统一 import 实现模块触发。

**异步任务机制**（F07 §12.8）：看门狗不复用 ``Job``/``Scheduler``（它是事件驱动
非周期触发）。Observer 线程的 ``on_modified`` 回调经 ``loop.call_soon_threadsafe``
投递到主事件循环，``asyncio.create_task`` 起异步同步任务，sqlite 操作经
``asyncio.to_thread`` 推到独立线程避免阻塞事件循环（桥接模式抄
``agent-core/lite/manager.py``，见步骤 2 设计说明）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jiuwen_memory.common.factory.factory import Factory


class WatchdogProducer(Factory):
    """Watchdog 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 watchdog）。各实现在 ``watchdog_impl`` 下以
    ``@WatchdogProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "watchdog"


class Watchdog(ABC):
    """文档看门狗算子契约。

    监听 memory 目录的 md 文件修改事件，把用户对 md 的改动同步进影子索引，
    保持 md（人类视图）与影子索引（机器真源）一致（F07 §12）。

    生命周期由 ``Kernel`` 绑定：``start`` 在事件循环就绪后调（拿 ``get_running_loop``
    引用做 Observer 线程→主循环桥接），``stop`` 在 Kernel 关闭时调（stop+join Observer）。
    """

    @abstractmethod
    def start(self) -> None:
        """启动文件监听。

        须在事件循环线程调（实现内取 ``asyncio.get_running_loop`` 拿 loop 引用，
        供 Observer 线程经 ``call_soon_threadsafe`` 投递事件回主循环）。启动后
        延迟置 ``watcher_initialized`` 避开初始扫描风暴（抄 lite）。
        """

    @abstractmethod
    def stop(self) -> None:
        """停止文件监听 + join Observer 线程；幂等。同时取消在途 debounce 定时器。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛 :class:`~common.errors.HealthCheckError`。"""
