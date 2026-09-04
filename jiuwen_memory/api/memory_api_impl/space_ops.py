# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Space CRUD/policy/members after PEP; delete_space uses SpaceLifecycleService."""

from __future__ import annotations

import asyncio
import json

from jiuwen_memory.common.errors import (
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import (
    Scope,
)
from jiuwen_memory.control.types import (
    DeleteMode,
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
)

from .local_support import (
    _SPACE_SCAN_CAP,
    _resolve_space_owner,
    _space_permission_context,
    _space_scope,
    _space_target_id,
    _trim_space_policy,
)

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")


class SpaceOpsMixin:
    """Space CRUD/policy/members after PEP; delete_space uses SpaceLifecycleService."""

    def create_space(self, spec: SpaceSpec, *, security: RequestSecurityContext) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(spec.org, spec.space)
        target_id = _space_target_id(spec.org, spec.space)
        auth = self._authorize(
            identity,
            Scope(org=spec.org),
            Action.WRITE,
            "create_space",
            target_id,
            context=_space_permission_context("space", target),
            require_space=False,
        )
        info = self._space.create(_resolve_space_owner(spec, identity))
        # 建空间同样要下发失效：事实缓存对「空间不存在」也装填一份（元数据与成员皆空），
        # 建之前任何一次读取（含 get_space 的鉴权路径）都会装填它，不清则新空间在一个
        # TTL 内判定无归属、无成员，归属主体本人也写不进去。
        self._invalidate_space_facts(spec.org, spec.space)
        self._log(identity, "create_space", target_id, target_scope=target, detail=auth)
        return info

    def get_space(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "get_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = _trim_space_policy(self._space.get(org, space), auth.get("permission_axis", ""))
        self._log(identity, "get_space", target_id, target_scope=target, detail=auth)
        return info

    def list_spaces(
        self,
        org: str,
        *,
        security: RequestSecurityContext,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        identity = security.auth.actor
        target = Scope(org=org)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "list_spaces",
            org,
            context=_space_permission_context("space_list", target),
            require_space=False,
            check_permission=not self._needs_space_facts(),
        )
        detail = dict(auth)
        if self._needs_space_facts():
            if limit <= 0:
                raise ValidationError("limit must be > 0")
            candidates = self._listable_candidates(org, status)
            spaces = self._readable_spaces(identity, org, candidates)[:limit]
            detail["candidate_spaces"] = str(len(candidates))
            if cursor is not None:
                # F07「``cursor`` 标记废弃」：``limit`` 在鉴权之后生效，偏移量按候选序解释
                # 会与返回序错位。忽略但记一笔——静默忽略会让期望翻页的调用方拿到重复页
                # 而无从察觉。
                detail["cursor_ignored"] = str(cursor)
        else:
            spaces = self._space.list(org, status=status, limit=limit, cursor=cursor)
        self._log(
            identity,
            "list_spaces",
            org,
            target_scope=target,
            detail={**detail, "count": str(len(spaces))},
        )
        return spaces

    def _listable_candidates(self, org: str, status: SpaceStatus | None) -> list[SpaceInfo]:
        """``list_spaces`` 的候选：org 下的全部空间，交由逐空间判权裁决。

        **不取主体反查索引。** 索引的超集契约针对成员关系——写入方只有归属登记与成员记录
        两类，靠显式授权（``grant``）取得读权的主体不在索引里。以索引作候选，这类调用方
        直接 ``search`` 读得到、``list_spaces`` 却列不出来，且不报错。与 F07 决策 23
        「不以反查索引粗筛写入候选」是同一条理由，读侧同样成立。

        授权记录接入索引不在本批处置：索引项不带来源标记，``remove_member`` 已需靠
        ``normalized not in info.owners`` 这一显式来源判断才能避免误删归属主体的索引项；
        再加第三类写入方，``revoke`` 与 ``remove_member`` 两个方向都要跨组件判断「该主体
        是否仍凭另一来源持有该空间」，而授权记录在 :class:`PermissionManager` 里，
        :class:`SpaceManager` 查不到。判断不全的失效方向是索引遗漏，恰是契约唯一禁止的
        方向。前置条件与遗留事项见 F07「``list_spaces`` 的候选来源」。

        代价是判权次数等于 org 下的空间数。这是既有形态而非本次引入；取值上界由
        :data:`_SPACE_SCAN_CAP` 封住，达到上界记 WARNING，不静默截断。
        """
        infos = self._space.list(org, status=status, limit=_SPACE_SCAN_CAP)
        if len(infos) >= _SPACE_SCAN_CAP:
            logger.warning(
                "list_spaces: org %s has at least %d spaces, candidates truncated at the scan cap",
                org,
                _SPACE_SCAN_CAP,
            )
        return infos

    def _readable_spaces(
        self, identity: Scope, org: str, spaces: list[SpaceInfo]
    ) -> list[SpaceInfo]:
        """逐空间求值，无权的直接剔除（F07「跨空间检索」末段）。

        整段在返回之前一次性完成，走与单空间入口同一个鉴权方法——分叉即出现「列得出
        但打不开」或其反向。

        逐空间的拒绝不落审计：一次调用对无权空间产生 M 条拒绝记录，审计价值低于噪声
        成本；整次调用仍记一条。

        策略裁剪与 ``get_space`` 同判据同实现：两者同处归属主体档第二级，分叉即出现
        「列表里读得到、单查读不到」或其反向。
        """
        readable: list[SpaceInfo] = []
        for info in spaces:
            target = _space_scope(org, info.space)
            context, _ = self._apply_space_policy_context(
                target,
                _space_permission_context("space", target),
                entry="list_spaces",
            )
            outcome = self._perm.decide(identity, target, Action.READ, context=context)
            if outcome.allowed:
                axis = outcome.axis.value if outcome.axis is not None else ""
                readable.append(_trim_space_policy(info, axis))
        return readable

    def update_space(
        self,
        org: str,
        space: str,
        patch: SpacePatch,
        *,
        security: RequestSecurityContext,
    ) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "update_space",
            target_id,
            context=_space_permission_context("space", target),
            space_patch=patch,
        )
        info = self._space.update(org, space, patch)
        self._invalidate_space_facts(org, space)
        self._log(identity, "update_space", target_id, target_scope=target, detail=auth)
        return info

    def archive_space(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpaceInfo:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "archive_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.archive(org, space)
        self._invalidate_space_facts(org, space)
        self._log(identity, "archive_space", target_id, target_scope=target, detail=auth)
        return info

    def delete_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        mode: DeleteMode = DeleteMode.PURGE,
    ) -> SpaceDeleteResult:
        identity = security.auth.actor
        if mode != DeleteMode.PURGE:
            raise ValidationError("delete_space currently supports DeleteMode.PURGE only")
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.DELETE,
            "delete_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        result, purged = asyncio.run(self._space_lifecycle.delete_space(org, space))
        self._invalidate_space_facts(org, space)
        self._log(
            identity,
            "delete_space",
            target_id,
            target_scope=target,
            detail={
                **auth,
                "deleted_memory_ids": json.dumps(purged, ensure_ascii=False),
                "deleted_counts": json.dumps(result.deleted_counts, ensure_ascii=False),
            },
        )
        return result

    def export_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        include_audit: bool = True,
    ) -> str:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "export_space",
            target_id,
            context=_space_permission_context("space_export", target),
        )
        export_id = self._space.export(org, space, include_audit=include_audit)
        self._log(
            identity,
            "export_space",
            target_id,
            target_scope=target,
            detail={**auth, "export_id": export_id, "include_audit": str(include_audit)},
        )
        return export_id

    def space_usage(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpaceUsage:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "space_usage",
            target_id,
            context=_space_permission_context("space_usage", target),
        )
        usage = self._space.usage(org, space)
        self._log(
            identity,
            "space_usage",
            target_id,
            target_scope=target,
            detail={
                **auth,
                "memory_count": str(usage.memory_count),
                "message_count": str(usage.message_count),
                "storage_bytes": str(usage.storage_bytes),
            },
        )
        return usage

    def get_space_policy(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpacePolicy:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "get_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        policy = self._space.get_policy(org, space)
        self._log(identity, "get_space_policy", target_id, target_scope=target, detail=auth)
        return policy

    def set_space_policy(
        self,
        org: str,
        space: str,
        policy: SpacePolicy,
        *,
        security: RequestSecurityContext,
    ) -> SpacePolicy:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "set_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        updated = self._space.set_policy(org, space, policy)
        self._invalidate_space_facts(org, space)
        self._log(
            identity,
            "set_space_policy",
            target_id,
            target_scope=target,
            detail={**auth, "principal_path": updated.principal_path.value},
        )
        return updated

    def list_space_members(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> list[SpaceMember]:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "list_space_members",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        members = self._space.list_members(org, space)
        self._log(
            identity,
            "list_space_members",
            target_id,
            target_scope=target,
            detail={**auth, "count": str(len(members))},
        )
        return members

    def add_space_member(
        self,
        org: str,
        space: str,
        member: SpaceMember,
        *,
        security: RequestSecurityContext,
    ) -> None:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.SHARE,
            "add_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._enforce_member_write_ceilings(identity, target, member)
        self._space.add_member(org, space, member)
        self._invalidate_space_facts(org, space)
        self._log(
            identity,
            "add_space_member",
            target_id,
            target_scope=target,
            detail={**auth, "member_role": member.role},
        )

    def remove_space_member(
        self,
        org: str,
        space: str,
        member: Scope,
        *,
        security: RequestSecurityContext,
    ) -> None:
        identity = security.auth.actor
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.SHARE,
            "remove_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._enforce_member_removal_ceiling(identity, target, member)
        self._space.remove_member(org, space, member)
        self._invalidate_space_facts(org, space)
        self._log(identity, "remove_space_member", target_id, target_scope=target, detail=auth)
