# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""数据面查询端口：封装 ``MemoryEngine`` 的 search/list/get 与鉴权元数据读取。"""

from __future__ import annotations

from datetime import datetime

from jiuwen_memory.common.type_def import Context, FilterExpr, MemoryUnit, Scope
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.types import DeleteSelector, MemoryListResult, PermissionContext
from jiuwen_memory.retrieval import RetrievalQuery, RetrievalResult


class MemoryQueryService:
    """已鉴权 scope 上的查询协调入口。

    路由谓词回注、两族系统谓词和逐单元 READ 鉴权仍在 API（PEP）。本端口接收已经
    收窄后的查询对象，委托 Engine 取数并返回鉴权所需的真源权限上下文。
    """

    def __init__(self, engine: MemoryEngine) -> None:
        self._engine = engine

    def validate_retrieval_pipeline(self, name: str | None) -> None:
        self._engine.validate_retrieval_pipeline(name)

    async def recall(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        return await self._engine.recall(scope, query)

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
        return await self._engine.list_with_permission_contexts(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            extensions=extensions,
            filters=filters,
        )

    async def get(
        self, unit_id: str, scope: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        return await self._engine.get(unit_id, scope, as_of)

    async def permission_context_for_unit(
        self, unit_id: str, scope: Scope
    ) -> PermissionContext:
        return await self._engine.permission_context_for_unit(unit_id, scope)

    async def permission_contexts_for_delete(
        self, selector: DeleteSelector
    ) -> list[PermissionContext]:
        return await self._engine.permission_contexts_for_delete(selector)

    async def read_md_file(
        self,
        context: Context,
        md_filename: str,
        *,
        line_range: tuple[int, int] | None = None,
    ) -> list[MemoryUnit]:
        """薄包装：委托 ``MemoryEngine.read_md_file``（F08 md 文件级读接口）。

        与 :meth:`recall` 同构——已鉴权 scope 上的查询转发，project 解析在 Engine 层
        （:func:`project_from_context`），本端口不拆 Context。鉴权（READ）在 API 层
        PEP 完成后才进入本端口，与 ``get`` / ``list`` 的鉴权后委托模式一致。
        """
        return await self._engine.read_md_file(context, md_filename, line_range=line_range)
