# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data-plane read/update/delete/evolve after PEP."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger, metadata_for_log, redact_for_log
from jiuwen_memory.common.security import principal, space_predicates
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.type_def import (
    EXT_MAX_TOKENS,
    ChannelError,
    Context,
    FilterClause,
    FilterExpr,
    MemoryUnit,
    Scope,
    and_merge,
    normalize,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.router import (
    narrow_dims_of,
    reject_kernel_coords,
)
from jiuwen_memory.control import collective
from jiuwen_memory.control.types import (
    Channel,
    DeleteSelector,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
)
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery, RetrievalResult

from .local_support import (
    _ROOT,
    _evolve_space_action,
    _first_family_predicate,
    _list_permission_contexts,
    _list_routing_clauses,
    _normalize_list_extensions,
    _parse_max_tokens,
    _pop_coords,
    _pop_spaces,
    _recall_permission_context,
    _reject_kernel_system_metadata,
    _reject_non_scalar_metadata,
    _reject_route_tag_keys,
    _routing_clauses_of,
    _selector_permission_context,
    _space_denied,
    _unit_lookup_permission_context,
)

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")


class QueryOpsMixin:
    """Data-plane read/update/delete/evolve after PEP."""

    def search(
        self,
        query: str,
        context: Context,
        *,
        security: RequestSecurityContext,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
        as_of: datetime | None = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        """执行检索；``extensions`` 带 ``spaces`` 键时转为跨空间形态（F07「多空间读写」）。

        单空间与跨空间是同一个入口的两条路径，不是两个接口：跨空间不是新的检索算法，是在
        单空间召回之上套的一层编排（定候选空间 → 逐空间判权 → 各自召回 → 轮转合并），
        两族谓词与召回都复用同一份实现。分成两个接口即同一件事有两处契约，接入方须先判断
        部署形态才知道该调哪个。

        ``spaces`` 键不在时本方法与本特性之前逐字一致。判据取键的有无，见 :func:`_pop_spaces`。
        """
        identity = security.auth.actor
        # Context 在边界处拆包：scope 照旧作独立轴下推（鉴权 + 检索），
        # extensions 写入调用级 options 顺 parser 透传给自定义检索模块；
        # Context 对象本身不进内核。三个约定 key 在此解释并从透传 options 中移除，
        # 避免与内核已解释的字段重复：max_tokens（自适应披露预算）解析为 typed int
        # 写入 RetrievalQuery，coords（归属坐标）折算成第二族收窄谓词，spaces（候选空间）
        # 决定走单空间还是跨空间编排。
        options = dict(context.extensions)
        max_tokens = _parse_max_tokens(options.pop(EXT_MAX_TOKENS, None))
        spaces = _pop_spaces(options)
        coords = _pop_coords(options, enabled=self._routing_enabled())
        reject_kernel_coords(coords)
        # 坐标折算成第二族收窄谓词的取值，在此算一次、两条路径共用。放进各自分支即
        # 「两处各折算一次」：漏调哪一处，该路径的 agent 维与 session 维整体不再收窄，
        # 失效方向是放宽且不报错。
        narrow = narrow_dims_of(
            principal.kernel_coords(coords, identity), self._route_table.narrow_dims
        )
        if spaces is not None:
            return self._search_spaces(
                query,
                context,
                identity=identity,
                spaces=spaces,
                filters=filters,
                as_of=as_of,
                top_k=top_k,
                disclosure=disclosure,
                with_trajectory=with_trajectory,
                options=options,
                max_tokens=max_tokens,
                coords=coords,
                narrow=narrow,
            )
        rq = RetrievalQuery(
            text=query,
            # RetrievalQuery 边界统一 normalize 旧 list、clause 与 dict DSL。
            filters=filters,
            as_of=as_of,
            top_k=top_k,
            disclosure=disclosure,
            max_tokens=max_tokens,
            with_trajectory=with_trajectory,
            extensions=options,
        )
        # 权限上下文与 RetrievalQuery 共用同一规范化后的 FilterExpr（不重复转换）。
        permission_context = _recall_permission_context(context, rq.filters)
        auth, auth_context = self._authorize_with_context(
            identity,
            context.scope,
            Action.READ,
            "search",
            context=permission_context,
        )
        # 用户表达式作整体 child 并入外层 AND（与 lifecycle/时间谓词同一机制），不会被
        # 其内部的 OR 稀释。回注的判据见 _routing_clauses_of。
        routing_clauses = _routing_clauses_of(permission_context, self._perm.routing_fields())
        if routing_clauses:
            rq.filters = and_merge(rq.filters, routing_clauses)
        # 两族谓词与调用方表达式合成一个 AND 一次下推，在 top-k 截断之前生效——
        # 召回后二次过滤会让被筛掉的条目白占召回名额，最终返回条数少于 top_k。
        # 第二族由归属坐标折算：坐标缺项不生成对应谓词，表现为该维不收窄，失效方向是放宽。
        system_clauses = space_predicates.system_predicates(
            auth_context.space_facts if auth_context is not None else None,
            identity,
            narrow,
        )
        if system_clauses:
            rq.filters = and_merge(rq.filters, system_clauses)
        result = asyncio.run(self._queries.recall(context.scope, rq))
        self._log(identity, "search", target_scope=context.scope, detail=auth)
        return result

    def _search_spaces(
        self,
        query: str,
        context: Context,
        *,
        identity: Scope,
        spaces: list[str],
        filters: FilterExpr | list[FilterClause] | dict | None,
        as_of: datetime | None,
        top_k: int,
        disclosure: DisclosureLevel,
        with_trajectory: bool,
        options: dict[str, Any],
        max_tokens: int | None,
        coords: dict[str, str] | None,
        narrow: dict[str, str],
    ) -> RetrievalResult:
        """跨空间检索：本层做前两步半，后三步下沉控制层（F07「多空间读写」）。

        由 :meth:`search` 在 ``extensions`` 带 ``spaces`` 键时分流进来，不是独立入口。
        参数袋的拆包与坐标折算都在 :meth:`search` 内完成，结果经形参传入：同一个参数袋
        解释两遍，两处的解释一旦分叉即出现「同一次调用两套取值」。``coords`` 只用于日志，
        实际生效的是已折算好的 ``narrow``。

        | 步 | 内容 | 落点 |
        |---|---|---|
        | 1 定候选空间 | 显式 ``spaces`` 或主体反查索引 | 本层（反查按 ``identity``） |
        | 2 逐空间判权与状态校验 | ``PermissionManager.decide`` | 本层（循环体就是 PEP） |
        | 2.5 逐空间谓词 | 路由值回注 + 两族系统谓词 | 本层（按 ``identity`` 与空间事实） |
        | 3—5 摊配、扇出、合并 | 取数上界、逐空间召回、轮转合并 | 控制层 ``cross_space_recall`` |

        分界取 S02「不做业务编排逻辑」那条的原文判据——「移出本层是否还能按 ``identity``
        裁决」。前两步半读 ``identity``，移出即把 PEP 分裂为两处；后三步在候选集与谓词都
        已定妥之后执行，全程不需要知道调用方是谁，留在本层只是让 PEP 多背一段取数编排。

        第 2 步不放进协程：事实读取与判权都是同步调用，授权记录查询还会在存储层串行。
        判权前置的副产品是取数上界按实际可读空间数计算，无权空间不再凭空占用召回名额。

        ``context.scope`` 只取 ``org`` 维定组织边界，空间维由候选集给出、传了不生效。
        """
        principal.require_principal(identity)
        org = context.scope.org
        normalized_filters = normalize(filters)

        candidates = self._search_candidates(identity, org, spaces)
        # 收窄谓词的实际取值只在此处成形，下游只能看到条数。缺这一行时「召回为空」
        # 无法区分坐标未传到、判定表未声明该维、以及该维确实过滤掉了全部条目。
        logger.info(
            "search cross-space: query=%s coords=%s narrow=%s candidate_spaces=%s",
            redact_for_log(query),
            metadata_for_log(coords),
            metadata_for_log(narrow),
            metadata_for_log(candidates),
        )
        targets: list[collective.SpaceRecallTarget] = []
        denied: list[ChannelError] = []
        for space in candidates:
            target = Scope(org=org, space=space)
            permission_context, facts = self._apply_space_policy_context(
                target,
                _recall_permission_context(
                    Context(scope=target, extensions=dict(context.extensions)),
                    normalized_filters,
                ),
                entry="search",
            )
            try:
                outcome = self._perm.decide(
                    identity, target, Action.READ, context=permission_context
                )
                if outcome.allowed and self._needs_space_facts():
                    # 状态校验与单空间路径同一口径（F07「空间状态校验」）：不补则
                    # 调用方在 extensions["spaces"] 里点名一个正在清理的空间即可照常
                    # 拿到内容，同一个 search 的两条路径对该空间给出两种结果。
                    # 排在判权通过之后、复用同一份事实快照，与 _authorize_with_context
                    # 一致；`_needs_space_facts` 这层门控同样不可省——未装配空间治理的
                    # 部署里无条件加会收紧既有行为。
                    self._ensure_space_state_allows(
                        target,
                        Action.READ,
                        "search",
                        info=facts.info if facts is not None else None,
                    )
            except (PermissionDeniedError, BackendError, NotFoundError, ValidationError) as exc:
                denied.append(_space_denied(space, type(exc).__name__, str(exc)))
                continue
            if outcome.allowed:
                # 第 2.5 步：本空间专属的系统谓词。与单空间入口同一处理——授权所依据的
                # 路由值回注，再叠两族系统谓词。逐空间各算自己的那份：各空间的授权可以
                # 来自不同的策略，共用一份即某个空间按另一个空间的授权取数。
                #
                # 这一步留本层而不随扇出一起下沉：两族谓词由 ``identity`` 与该空间的事实
                # 生成，是 S02「鉴权驱动的编排」明列的一项（生成并回注系统谓词）。
                clauses = _routing_clauses_of(permission_context, self._perm.routing_fields())
                clauses.extend(
                    space_predicates.system_predicates(
                        permission_context.space_facts, identity, narrow
                    )
                )
                targets.append(
                    collective.SpaceRecallTarget(scope=target, clauses=tuple(clauses))
                )
            else:
                denied.append(
                    _space_denied(
                        space,
                        PermissionDeniedError.__name__,
                        f"read denied: rule={outcome.rule} reason={outcome.reason}",
                    )
                )
        # 候选集非空而一个都读不到时抛，与单空间路径同一处置：那条路径上无权即
        # PermissionDeniedError，本路径若静默返回空，同一个方法的两条路径对「完全无权」
        # 给出两种结果，且「无权」与「这些空间里没有内容」在调用方看来不可区分。
        # 候选集为空不抛——那是「主体不在任何空间里」，是合法的空结果。
        if candidates and not targets:
            # 候选来自调用方显式传入时回显空间名（那是他自己的入参，便于排查）；来自主体
            # 反查索引时只给条数。索引按 `context.scope.org` 建桶，而该 org 取自参数袋、
            # 与 `identity.org` 无一致性校验——回显即把另一个组织的空间名交给调用方，而
            # 逐空间判权只挡住了访问，挡不住这行措辞。
            detail = repr(candidates) if spaces else f"{len(candidates)} space(s)"
            raise PermissionDeniedError(f"read denied on every candidate space: {detail}")

        # 查询骨架装配一次，逐空间只差取数上界与本空间谓词，两项都由控制层补齐。
        # 逐空间构造 RetrievalQuery 的循环本身就是取数编排，不留本层。
        # 同步桥接留本层（S02「同步/异步桥接」）：控制层给协程，本层 asyncio.run。
        merged, space_failures = asyncio.run(
            collective.recall_spaces(
                targets,
                RetrievalQuery(
                    text=query,
                    filters=normalized_filters,
                    as_of=as_of,
                    disclosure=disclosure,
                    max_tokens=max_tokens,
                    with_trajectory=with_trajectory,
                    extensions=options,
                ),
                top_k=top_k,
                recall=self._queries.recall,
            )
        )
        for failure in space_failures:
            # 扇出失败逐空间落一条审计。判据取分离返回的那份列表，不从 merged.errors 里
            # 按 channel 过滤——后者会把审计范围绑在控制层的 channel 编码上。
            self._log(
                identity,
                "search",
                failure.source,
                target_scope=Scope(org=org, space=failure.source),
                decision="error",
                detail={"entry": "cross_space", "error": failure.message},
            )
        # 判权剔除与扇出失败一并进 errors，不静默丢弃：调用方拿到少于预期的结果时，
        # 「这个空间我读不到」「这个空间挂了」「这个空间里没有内容」三者的后续动作完全
        # 不同，而只记审计日志时它们在返回值上是同一形态。三类共用 ChannelError，按
        # source 区分是哪个空间、按 error_type 区分是哪一类。各空间自己的分通道错误
        # （VECTOR / KEYWORD 等）已由控制层的合并并入。
        merged.errors.extend(denied + space_failures)
        self._log(
            identity,
            "search",
            org,
            target_scope=Scope(org=org),
            detail={
                "entry": "cross_space",
                "candidate_spaces": str(len(candidates)),
                "readable_spaces": str(len(targets)),
                "denied_spaces": str(len(denied)),
                "failed_spaces": str(len(space_failures)),
                "count": str(len(merged.items)),
            },
        )
        return merged

    def _search_candidates(self, identity: Scope, org: str, spaces: list[str]) -> list[str]:
        """第 1 步：定候选空间。``spaces`` 非空就用它，为空则取主体反查索引结果。

        反查索引是超集契约——不遗漏、允许多给，权限由第 2 步的逐空间判权裁决。截断记
        WARNING，不静默丢弃：静默截断读起来与「这些空间里确实没有内容」不可区分。
        """
        if spaces:
            candidates = [space for space in dict.fromkeys(spaces) if space]
        elif self._membership is not None:
            candidates = list(self._membership.spaces_for(identity, org))
        else:
            candidates = []
        limit = self._space_fanout_limit()
        if len(candidates) > limit:
            self._log(
                identity,
                "search",
                org,
                target_scope=Scope(org=org),
                decision="allow",
                detail={
                    "entry": "cross_space",
                    "truncated_spaces": str(len(candidates) - limit),
                },
            )
            candidates = candidates[:limit]
        return candidates

    def list(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
    ) -> MemoryListResult:
        identity = security.auth.actor
        normalized_extensions = _normalize_list_extensions(extensions)
        normalized_filters = normalize(filters)
        permission_contexts = _list_permission_contexts(
            scope,
            memory_types,
            normalized_filters,
            normalized_extensions,
        )
        auth: dict[str, str] = {}
        auth_context: PermissionContext | None = None
        for permission_context in permission_contexts:
            auth, auth_context = self._authorize_with_context(
                identity,
                scope,
                Action.READ,
                "list",
                context=permission_context,
            )
        routing_clauses = _list_routing_clauses(
            permission_contexts,
            self._perm.routing_fields(),
            memory_types,
        )
        effective_filters = and_merge(normalized_filters, routing_clauses)
        # list 必须注入第一族，否则个体空间的隔离只在 search 上成立：它是同样按空间返回
        # 条目的批量入口，不注入的后果与 search 同因同向。第二段逐条鉴权不能替代谓词——
        # 逐条鉴权的失败形态是抛异常而非过滤，整次调用失败而不是少返回几条。
        system_clauses = _first_family_predicate(identity, auth_context)
        if system_clauses:
            effective_filters = and_merge(effective_filters, system_clauses)
        result, unit_contexts = asyncio.run(
            self._queries.list_with_permission_contexts(
                scope,
                offset=offset,
                limit=limit,
                memory_types=memory_types,
                extensions=normalized_extensions,
                filters=effective_filters,
            )
        )
        for permission_context in unit_contexts:
            # 第二段不携带作者标记：逐条鉴权的失败形态是抛异常而非过滤，携带后个体空间内
            # 只要有一条作者不是调用方的条目，整次调用即失败——代理自主运行写入的条目与
            # 回填期多归属空间中另一归属主体写入的条目都属此列。内容边界由第一族谓词在
            # 取数时承担，本段只判条目真源 scope 的空间归属。
            auth = self._authorize(
                identity,
                permission_context.scope,
                Action.READ,
                "list",
                permission_context.unit_id,
                context=permission_context,
                carry_author_marks=False,
            )
        if len(permission_contexts) > 1:
            auth["permission_memory_types"] = ",".join(
                context.memory_type for context in permission_contexts
            )
        self._log(
            identity,
            "list",
            target_scope=scope,
            detail={
                **auth,
                "count": str(result.count),
                "page_count": str(len(result.items)),
            },
        )
        return result

    def get(
        self,
        unit_id: str,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        as_of: datetime | None = None,
    ) -> MemoryUnit:
        identity = security.auth.actor
        self._authorize(
            identity,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(
            self._queries.permission_context_for_unit(unit_id, scope)
        )
        # 第二段的目标取条目真源 scope，不沿用入参（F07「条目级入口分两段鉴权」）：
        # 它是「判定第 8 步不会命中」的两个前置条件之一，与「回填后条目 scope 只有两维」
        # 各兜一重，两条的失效方向相反。沿用入参即把两重约束落在同一个取值上。
        # 与 list / delete 同一口径。
        auth = self._authorize(
            identity,
            permission_context.scope,
            Action.READ,
            "get",
            unit_id,
            context=permission_context,
        )
        unit = asyncio.run(self._queries.get(unit_id, scope, as_of))
        self._log(
            identity,
            "get",
            unit_id,
            target_scope=scope,
            detail={**auth, "after_unit_id": unit.id},
        )
        return unit

    def update(
        self,
        unit_id: str,
        scope: Scope,
        patch: MemoryPatch,
        *,
        security: RequestSecurityContext,
    ) -> MemoryUnit:
        identity = security.auth.actor
        _reject_kernel_system_metadata(patch.system_metadata)
        # 判定标签键在改写入口同样不可由调用方赋值：只挂写入入口时，改写即绕过通道——
        # 内容 EDITOR 可把他人条目的会话标签改成自己的会话 id，使其出现在他人的上下文里，
        # 或改写项目标签使条目脱离按 `system_metadata.<tag_key>` 谓词执行的批量删除范围。
        # 入参 scope 的主体维不在此校验：它是条目查找键而非落盘键。
        _reject_route_tag_keys(patch.system_metadata, self._route_table.tag_keys)
        _reject_non_scalar_metadata(patch.system_metadata, field_name="system_metadata")
        _reject_non_scalar_metadata(patch.user_metadata, field_name="user_metadata")
        self._authorize(
            identity,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(
            self._queries.permission_context_for_unit(unit_id, scope)
        )
        # 第二段的目标取条目真源 scope，理由同 get。随后的 _ensure_space_writable 仍取
        # 入参 scope——它校验的是本次写入落点所在空间，与鉴权目标是两件事。
        auth = self._authorize(
            identity,
            permission_context.scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=permission_context,
        )
        self._ensure_space_writable(scope)
        before = asyncio.run(self._queries.get(unit_id, scope, None))
        unit = asyncio.run(self._commands.update(unit_id, scope, patch))
        self._log(
            identity,
            "update",
            unit_id,
            target_scope=scope,
            detail={**auth, "before_unit_id": before.id, "after_unit_id": unit.id},
        )
        return unit

    def delete(self, selector: DeleteSelector, *, security: RequestSecurityContext) -> list[str]:
        identity = security.auth.actor
        selector_is_empty = (
            not selector.unit_ids
            and not selector.tags
            and selector.before is None
            and selector.filters is None
        )
        if selector_is_empty:
            raise ValidationError("DeleteSelector requires unit_ids, tags, before, or filters")
        # 按 selector 的目标 scope 鉴权 DELETE；未限定 scope（如纯按 id/标签的
        # 跨范围删除）则退到根 scope 闸门，要求更高权限。
        target = selector.scope or _ROOT
        selector_context = _selector_permission_context(selector, target)
        if selector.scope is not None or not selector.unit_ids:
            self._authorize(
                identity,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        contexts = asyncio.run(self._queries.permission_contexts_for_delete(selector))
        if not contexts:
            auth = self._authorize(
                identity,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        else:
            auth = {"permission_check": "enabled", "permission_reason": "permission check passed"}
            for permission_context in contexts:
                unit_auth = self._authorize(
                    identity,
                    permission_context.scope,
                    Action.DELETE,
                    "delete",
                    permission_context.unit_id,
                    context=permission_context,
                )
                auth.update(unit_auth)
        deleted = asyncio.run(self._commands.delete(selector))
        self._log(
            identity,
            "delete",
            target_scope=target,
            detail={**auth, "before_unit_ids": json.dumps(deleted, ensure_ascii=False)},
        )
        return deleted

    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        security: RequestSecurityContext,
    ) -> str:
        identity = security.auth.actor
        auth = self._authorize(
            identity,
            scope,
            Action.WRITE,
            "evolve",
            space_action=_evolve_space_action(mode),
        )
        self._ensure_space_writable(scope)
        job_id = asyncio.run(self._commands.evolve(scope, mode, channel))
        self._log(identity, "evolve", target_scope=scope, detail={**auth, "job_id": job_id})
        return job_id
