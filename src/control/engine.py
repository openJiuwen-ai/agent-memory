"""MemoryEngine — 记忆引擎（接口层 §9 各语义的编排中枢）。

记忆接口层 ``src/api`` 是本引擎的薄封装：只做参数装配后逐方法委托到
这里，跨层编排全部在引擎内完成。**引擎方法一律为异步协程**（数据面与
管理面均触及 IO——存储、向量库、LLM/embedding），事件循环形态（HTTP/
MCP）可直接 await 非阻塞调用，CLI/脚本等同步形态由接口层 ``src/api``
自行桥接（如 ``asyncio.run``）：
- write：权限校验 → Ingestor 规约（RawPayload → MemoryUnit，不落盘）
  → 构建层落盘 + hot 轻量索引 → Scheduler 提交 background 重演进，
  返回插入的记忆单元；
- recall：scope/权限校验 → Retriever 完整检索链路；
- list：按 scope 枚举已建索引的 `/memory/` 真源记忆，支持分页与记忆类型过滤；
- get / update / delete：存储层点读 + 非破坏式修正/遗忘（记血缘）；
- evolve：委托 Scheduler 双通道调度；admin_*：委托 PolicyManager。

Ingestor / 构建层算子 / Retriever / 其余控制算子 / Store 由装配注入。
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import Any

from common.factory.factory import Factory
from common.type_def import FilterExpr, MemoryUnit, Modality, Scope
from construction import EvolveMode
from retrieval import RetrievalQuery, RetrievalResult

from .base import ControlOperator
from .types import (
    BatchWriteItem,
    BatchWriteResult,
    Channel,
    DeleteSelector,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
)


class EngineProducer(Factory):
    """MemoryEngine 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``engine_impl`` 下以 ``@EngineProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`control.bootstrap.register_controllers` 统一触发。
    """

    TOP_NAME = "engine"


class MemoryEngine(ControlOperator):
    """编排接口层各语义；本身不实现具体能力，驱动各层算子完成。"""

    @abstractmethod
    async def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        """异步（协程）写入：hot path 完成（真源落盘 + 轻量索引）后返回
        本次插入的全部记忆单元（规约/切分可产生多条）；重的抽取/升华
        提交 background 通道异步执行，不在本调用内等待。接口层的同步
        write 由其自行桥接本协程。"""

    @abstractmethod
    async def recall(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        """混合检索召回：scope/权限校验后委托 ``Retriever.retrieve(scope, query)``
        执行完整链路。scope 作为显式参数传入（不随 query 携带）。"""

    @abstractmethod
    async def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, str] | None = None,
        filters: FilterExpr | None = None,
    ) -> MemoryListResult:
        """列出 ``scope`` 下已建索引的记忆单元。

        只返回真源中 ``/memory/`` 前缀的 MemoryUnit，不包含 ``/messages/`` 下的
        infer 原文缓存。``offset``/``limit`` 语义参考 mem1.0 的 list_memories；
        ``memory_types`` 为空或 ``None`` 时不过滤。
        """

    @abstractmethod
    async def batch_write(
        self,
        items: list[BatchWriteItem],
        *,
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        """按输入顺序执行已通过 API 前置校验的批量写入。"""

    @abstractmethod
    async def permission_context_for_unit(
        self, unit_id: str, scope: Scope
    ) -> PermissionContext:
        """读取已有记忆的权限判断上下文。

        只返回鉴权需要的元数据（memory_type/tags/metadata 等），不返回 content/assets。
        API 层用于 get/update/delete 等只有 unit_id 的入口在正式数据操作前做类型化鉴权。
        """

    @abstractmethod
    async def list_with_permission_contexts(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, str] | None = None,
        filters: FilterExpr | None = None,
    ) -> tuple[MemoryListResult, list[PermissionContext]]:
        """一次读取 list 当前分页及其逐项权限上下文，供 API 鉴权后返回。"""

    @abstractmethod
    async def permission_contexts_for_delete(
        self, selector: DeleteSelector
    ) -> list[PermissionContext]:
        """解析 delete selector 会命中的候选 unit 权限上下文。

        Engine 负责按 selector 扫描候选，但只暴露鉴权元数据；API 层据返回结果逐条
        调用 PermissionManager.check，全部通过后才执行 delete。
        """

    @abstractmethod
    async def get(
        self, unit_id: str, scope: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        """按 id 点读记忆单元；``scope`` 为已鉴权的目标范围（鉴权在 API 层
        完成，本层信任）。``as_of`` 为空返回该 id 那一条，非空则沿
        ``supersedes`` 版本链返回 valid 区间含 ``as_of`` 的那一版（双时间
        模型）。不存在时抛 :class:`~common.errors.NotFoundError`。"""

    @abstractmethod
    async def update(self, unit_id: str, scope: Scope, patch: MemoryPatch) -> MemoryUnit:
        """按 ``patch.mode`` 修正：``SUPERSEDE``（默认、非破坏式）生成新 id
        版本、旧版标记 superseded、新版 ``supersedes`` 记版本链；``OVERWRITE``
        原地覆写沿用同 id（旧内容仅留审计）。``scope`` 为已鉴权的目标范围；
        返回结果记忆单元。"""

    @abstractmethod
    async def delete(self, selector: DeleteSelector) -> list[str]:
        """按选择器遗忘/归档/降权（非破坏式、可审计），返回命中的记忆
        单元 id。"""

    @abstractmethod
    async def purge_space(self, org: str, space: str) -> list[str]:
        """物理删除指定 space 及其全部子 scope 的记忆真源和派生索引。"""

    @abstractmethod
    async def evolve(
        self, scope: Scope, mode: EvolveMode, channel: Channel = Channel.BACKGROUND
    ) -> str:
        """触发一次演进：委托 Scheduler 提交指定阶段与通道，返回任务 id。"""

    @abstractmethod
    async def admin_get(self, key: str) -> str:
        """读取一项运行时策略（委托 PolicyManager）。"""

    @abstractmethod
    async def admin_set(self, key: str, value: str) -> None:
        """调整一项运行时策略（委托 PolicyManager；不可变配置报错拒绝）。"""

    @abstractmethod
    async def admin_all(self) -> dict[str, str]:
        """列出全部运行时策略及当前值。"""
