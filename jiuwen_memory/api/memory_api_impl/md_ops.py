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
from dataclasses import replace

from jiuwen_memory.api.collective import dimensions_of
from jiuwen_memory.common.errors import NotFoundError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security import principal
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import COORDS_KEY, Context, MemoryUnit, Scope
from jiuwen_memory.storage.markdown import MarkdownStore
from jiuwen_memory.storage.markdown_impl._md_path import collective_document_path, is_diary_filename

from .local_support import _strip_transient_metadata, space_scope_for_md_read


def _unique_scopes(scopes):
    return list({(s.org, s.space, s.user, s.agent, s.session): s for s in scopes}.values())


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

        鉴权 ``READ`` 单段（与 ``get`` / ``list`` 同口径，D5），target 是折算后的反查
        scope：路由启用时数据面反查 scope 经 :meth:`_md_read_scope` 折算为空间级
        （org+space），鉴权与审计（``target_scope``）对同一折算 scope 执行——与写入侧
        「先定落点、对落盘空间判权 + 审计」（``add`` 对 ``_write_target`` 落点判权）
        对称。折算不出时退回 raw ``context.scope``，判权/审计/反查三者同轴。project
        解析在 Engine 层，本层不拆 Context。返回前剥瞬态键（coords 等是判定输入 /
        路径解析依据，不是条目属性），与 ``add`` 出口同处置。
        """
        if "collective_target" in context.extensions:
            paths, scopes = self._collective_document_scope(context, md_filename, security, Action.READ)
            units = []
            for path in paths:
                units.extend(self._collective_document_units(path, scopes, md_filename, line_range))
            return [_strip_transient_metadata(unit) for unit in units]
        self._reject_internal_document_options(context)
        identity = security.auth.actor
        read_scope = self._md_read_scope(context, md_filename, security)
        auth = self._authorize(security, read_scope, Action.READ, "read_md_file")
        if self._shared_profile(read_scope, md_filename) and read_scope == context.scope:
            units = self._collective_document_units(
                "USER.md", self._profile_scopes(read_scope), md_filename, line_range,
            )
        else:
            units = asyncio.run(
                self._queries.read_md_file(
                    replace(context, scope=read_scope), md_filename, line_range=line_range
                )
            )
        self._log(identity, "read_md_file", target_scope=read_scope, detail=auth)
        return [_strip_transient_metadata(unit) for unit in units]

    def _md_read_scope(
        self, context: Context, md_filename: str, security: RequestSecurityContext
    ) -> Scope:
        """路由启用时，把 ``read_md_file`` 的反查/鉴权/审计 scope 折算为空间级（org+space）。

        与写入侧同一坐标口径：``principal.kernel_coords`` 以身份覆盖内核三项
        （user/agent/session），使 ``u_{user}`` 这类内核坐标模板也能渲染出空间名——只拿
        调用方业务 coords 时该模板必缺 user 键、渲染空串退回 raw scope，user_profile 的
        读回仍落空。折算出的空间级 scope 同时是鉴权（READ）与审计的 target：调用方须对
        该空间有读权，否则在 PEP 拒绝。只把数据面反查 axis 折到落盘形态、鉴权仍留在
        raw scope 的话，空 space 绕过空间轴判权（越权读取）且审计看不出实际访问的空间
        （审计盲区）。路由未启用或坐标缺失/渲染不出时退回 raw scope——判权/审计/反查
        同轴，行为与未折算部署一致。
        """
        if not self._routing_enabled():
            return context.scope
        coords = context.extensions.get(COORDS_KEY)
        if not isinstance(coords, dict):
            return context.scope
        identity = self._resource_principal(security, Action.READ)
        return space_scope_for_md_read(
            self._route_table.naming,
            context.scope,
            md_filename,
            principal.kernel_coords(coords, identity),
        )

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
        if "collective_target" in context.extensions:
            if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
                raise ValidationError("Collective edit requires a non-empty old_text and a string new_text")
            paths, scopes = self._collective_document_scope(context, md_filename, security, Action.WRITE)
            self._ensure_space_writable(context.scope)
            self._ensure_audit_integrity_healthy()
            return self._edit_shared_document(
                paths, scopes, md_filename, old_text, new_text, security,
            )
        self._reject_internal_document_options(context)
        identity = security.auth.actor
        auth = self._authorize(security, context.scope, Action.WRITE, "edit_md_file")
        self._ensure_space_writable(context.scope)
        self._ensure_audit_integrity_healthy()
        if self._shared_profile(context.scope, md_filename):
            return self._edit_shared_document(
                ("USER.md",), self._profile_scopes(context.scope), md_filename, old_text, new_text, security,
            )
        units = asyncio.run(
            self._commands.edit_md_file(context, md_filename, old_text, new_text)
        )
        self._log(identity, "edit_md_file", target_scope=context.scope, detail=auth)
        return [_strip_transient_metadata(unit) for unit in units]

    def _shared_profile(self, scope: Scope, filename: str) -> bool:
        return self._document_mode and not self._routing_enabled() and filename == "USER.md" and bool(scope.user)

    def _profile_scopes(self, scope: Scope) -> list[Scope]:
        home = (scope.org, scope.space, scope.user, scope.session)
        scopes = [
            item for item in self._queries.candidate_scopes()
            if (item.org, item.space, item.user, item.session) == home
        ]
        return _unique_scopes([scope, replace(scope, agent=""), *scopes])

    def _edit_shared_document(self, paths, scopes, filename, old_text, new_text, security):
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ValidationError("Shared document edit requires a non-empty old_text and a string new_text")
        matches = []
        for path in paths:
            units = self._collective_document_units(path, scopes, filename, None)
            matches.extend((path, unit) for unit in units if old_text in unit.content)
        if not matches:
            raise NotFoundError(message="No matching visible document memory")
        if len(matches) != 1:
            raise ValidationError("Shared document edit requires one visible original memory")
        path, original = matches[0]
        target = Context(scope=original.scope, extensions={"_authorized_collective_document": path})
        units = asyncio.run(self._commands.edit_md_file(target, filename, old_text, new_text))
        self._log(security.auth.actor, "edit_md_file", target_scope=target.scope,
                  detail={"previous_unit": original.id, "document": path})
        return [_strip_transient_metadata(unit) for unit in units]

    @staticmethod
    def _reject_internal_document_options(context):
        if "_authorized_collective_document" in context.extensions:
            raise ValidationError("Invalid document options")

    def _collective_document_scope(self, context, filename, security, action):
        self._reject_internal_document_options(context)
        scope = context.scope
        operation = "read_md_file" if action == Action.READ else "edit_md_file"
        coordinates = self._require_collective_subject(scope, security, action, operation)
        auth = self._authorize(security, scope, action, operation)
        target = context.extensions["collective_target"]
        ordinary_diary = ""
        personal_agent = scope.agent not in ("", "main_agent")
        if target == "profile" and filename == "USER.md" and scope.user:
            scopes = self._profile_scopes(scope)
            path = "USER.md"
        elif target == "personal" and filename == "MEMORY.md" and personal_agent:
            scopes = [scope]
            path = collective_document_path(target, scope.agent)
        elif target in ("project", "team", "group"):
            if filename != "MEMORY.md" and (target != "project" or not is_diary_filename(filename)):
                raise ValidationError("Invalid collective document target or filename")
            dimension = next((d for d in dimensions_of(scope, coordinates) if d.kind == target), None)
            if dimension is None or dimension not in self._member_store().active_dimensions(scope):
                raise PermissionDeniedError("Collective document requires active membership")
            scopes = [
                member.scope for member in self._member_store().members(dimension)
                if member.scope.user == scope.user
            ]
            path = collective_document_path(target, dimension.id, filename)
            if filename != "MEMORY.md":
                ordinary_diary = MarkdownStore.resolve_md_path(filename, dimension.id)
        else:
            raise ValidationError("Invalid collective document target or filename")
        self._log(security.auth.actor, operation, target_scope=scope, detail=auth)
        paths = (path, ordinary_diary) if ordinary_diary else (path,)
        return paths, _unique_scopes(scopes)

    def _collective_document_units(self, path, scopes, filename, line_range):
        async def read():
            results = await asyncio.gather(*(
                self._queries.read_md_file(
                    Context(scope=scope, extensions={"_authorized_collective_document": path}),
                    filename, line_range=line_range,
                )
                for scope in scopes
            ))
            return list({unit.id: unit for units in results for unit in units}.values())

        return asyncio.run(read())
