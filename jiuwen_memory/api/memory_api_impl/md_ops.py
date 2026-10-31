# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data-plane md file ops (F08): read_md_file / edit_md_file after PEP.

md 文件级操作经 Engine 窄端口（非经现有 ``_commands.write`` / ``_queries.get``），
职责独立于 Write/Query/Admin/Space 四个 mixin，单独成 mixin（F08 D7）。鉴权口径对齐
``get``（READ 单段）/ ``add``（WRITE 单段，D5）——文件级操作不按 unit_id，两段鉴权不
自然（R6）。project 解析在 Engine 层 ``project_from_context``，本层不拆 Context（与
``search`` 同：context 整体下推，鉴权轴是 ``context.scope``）。
"""

from __future__ import annotations

import asyncio

from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import Context, MemoryUnit

from .local_support import _strip_transient_metadata


class MdOpsMixin:
    """Data-plane md file ops (F08): ``read_md_file`` / ``edit_md_file`` after PEP."""

    def read_md_file(
        self,
        context: Context,
        md_filename: str,
        *,
        security: RequestSecurityContext,
        line_range: tuple[int, int] | None = None,
    ) -> list[MemoryUnit]:
        """按 ``md_filename`` + 行范围读 md 文件原文，返回命中的 MemoryUnit 列表（F08）。

        鉴权 ``READ`` 对 ``context.scope`` 单段（与 ``get`` / ``list`` 同口径，D5）。project
        解析在 Engine 层，本层不拆 Context。返回前剥瞬态键（coords 等是判定输入 / 路径解析
        依据，不是条目属性），与 ``add`` 出口同处置。
        """
        identity = security.auth.actor
        auth = self._authorize(security, context.scope, Action.READ, "read_md_file")
        units = asyncio.run(
            self._queries.read_md_file(context, md_filename, line_range=line_range)
        )
        self._log(identity, "read_md_file", target_scope=context.scope, detail=auth)
        return [_strip_transient_metadata(unit) for unit in units]

    def edit_md_file(
        self,
        context: Context,
        md_filename: str,
        old_text: str,
        new_text: str,
        *,
        security: RequestSecurityContext,
    ) -> list[MemoryUnit]:
        """按 ``md_filename`` + ``old_text`` 在文件内做唯一替换，返回受影响的新 unit 列表（F08）。

        鉴权 ``WRITE`` 对 ``context.scope`` 单段（与 ``add`` 同口径，D5）——edit 走继承式
        SUPERSEDE，本质是写入新记忆，语义对齐 ``add``（WRITE）而非修正已有（UPDATE，R6）。
        写入前置状态校验经 ``_ensure_space_writable``（与 ``add`` 同，装配空间级判定时由
        ``ENTRY_RULES`` 登记 ``edit_md_file`` 触发 ``_ensure_space_state_allows``）。返回
        新 unit（旧版已标 SUPERSEDED）并剥瞬态键。
        """
        identity = security.auth.actor
        auth = self._authorize(security, context.scope, Action.WRITE, "edit_md_file")
        self._ensure_space_writable(context.scope)
        self._ensure_audit_integrity_healthy()
        units = asyncio.run(
            self._commands.edit_md_file(context, md_filename, old_text, new_text)
        )
        self._log(identity, "edit_md_file", target_scope=context.scope, detail=auth)
        return [_strip_transient_metadata(unit) for unit in units]
