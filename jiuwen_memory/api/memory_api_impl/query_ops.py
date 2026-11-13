# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data-plane read/update/delete/evolve after PEP."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any

from jiuwen_memory.api.personal_memory import (
    PersonalScopeBinding,
    personal_scope_binding,
    search_personal_records,
)
from jiuwen_memory.api.search_options import SearchOptions
from jiuwen_memory.common.errors import (
    AgentMemoryError,
    BackendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger, metadata_for_log, redact_for_log
from jiuwen_memory.common.plaintext import PlaintextDataError
from jiuwen_memory.common.security import principal, space_predicates
from jiuwen_memory.common.security.privacy.base import privacy_operation
from jiuwen_memory.common.security.types import Action, RequestSecurityContext
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    EXT_MAX_TOKENS,
    CandidateSource,
    ChannelError,
    Context,
    FilterClause,
    FilterExpr,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    MemoryUnit,
    Scope,
    and_merge,
    normalize,
)
from jiuwen_memory.common.type_def.continuity import MemoryRef, ViewBudget
from jiuwen_memory.common.type_def.filter import evaluate
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery
from jiuwen_memory.common.type_def.memory_filter import matches_filter_value, matches_memory_unit
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeOptions
from jiuwen_memory.construction.router import (
    narrow_dims_of,
    reject_kernel_coords,
)
from jiuwen_memory.control import collective
from jiuwen_memory.control.application.personal_memory import (
    PersonalPage,
    PersonalSearchResult,
    list_personal,
    scope_key,
)
from jiuwen_memory.control.evolution.validation import (
    scope_contains,
    validate_evolve_options,
    validate_evolve_request,
)
from jiuwen_memory.control.pipeline import route_value_of
from jiuwen_memory.control.types import (
    BackgroundJobStartResult,
    BatchUpdateOutcome,
    BatchUpdateResult,
    Channel,
    DeleteSelector,
    EvolveTaskOptions,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
    UpdateSelector,
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
    _pop_query_context,
    _pop_spaces,
    _recall_permission_context,
    _reject_kernel_system_metadata,
    _reject_memory_type_transition,
    _reject_non_scalar_metadata,
    _reject_route_tag_keys,
    _routing_clauses_of,
    _selector_permission_context,
    _space_denied,
    _TrustedRequestTarget,
    _type_isolation_clause,
    _unit_lookup_permission_context,
    _unit_snapshot_permission_context,
)

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")


@dataclass(frozen=True)
class _SearchAccess:
    spaces: list[str]
    coords: dict[str, str] | None
    narrow: dict[str, str]


def _copy_search_extensions(extensions: dict[str, Any]) -> dict[str, Any]:
    """复制标准容器，但保留不透明运行时插件对象的身份与不可复制资源。"""
    memo: dict[int, Any] = {}
    visited: set[int] = set()
    pending: list[Any] = [extensions]
    while pending:
        current = pending.pop()
        object_id = id(current)
        if object_id in visited:
            continue
        visited.add(object_id)
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset)):
            pending.extend(current)
        else:
            memo[object_id] = current
    return deepcopy(extensions, memo)


class QueryOpsMixin:
    """Data-plane read/update/delete/evolve after PEP."""

    # 与公开 MemoryAPI 保持平铺签名，内部以选项对象传递。
    def search(  # pylint: disable=too-many-arguments,unknown-option-value,huawei-too-many-arguments
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
        hierarchy_kind: HierarchyKind | None = HierarchyKind.TIME,
        hierarchy_role: HierarchyRole | None = HierarchyRole.TIME_SPAN,
        span_start: datetime | None = None,
        span_end: datetime | None = None,
        expand_depth: int = 1,
        rollup: bool = True,
        task_id: str = "",
        anchor: MemoryRef | None = None,
        duty: str | list[str] | None = None,
        view_budget: ViewBudget | None = None,
    ) -> RetrievalResult | PersonalSearchResult:
        """将原有平铺参数装成选项，委托统一查询入口。"""
        if "personal" in context.extensions:
            selection = context.extensions["personal"]
            valid_selection = isinstance(selection, dict) and selection.keys() <= {"source"}
            personal_only = (
                context.scope == Scope() and set(context.extensions) == {"personal"}
                and valid_selection
            )
            has_recall_options = filters is not None or as_of is not None or with_trajectory
            options = (
                disclosure, hierarchy_kind, hierarchy_role, span_start, span_end, expand_depth,
                rollup, task_id, anchor, duty, view_budget,
            )
            defaults = (
                DisclosureLevel.L0, HierarchyKind.TIME, HierarchyRole.TIME_SPAN,
                None, None, 1, True, "", None, None, None,
            )
            if not personal_only or has_recall_options or options != defaults:
                raise ValidationError("personal search only accepts a query, source and top_k")
            if not isinstance(query, str) or not query.strip():
                raise ValidationError("query must be a non-empty string")
            hits = search_personal_records(
                self, self._personal_candidates(security), query,
                security=security, top_k=top_k, source=selection.get("source"),
                read_scope=partial(self._list_scope, skip_unavailable_scope=True),
                skip_candidate=lambda unit: self._requires_continuity_point_read(
                    unit.system_metadata
                ),
            )
            return PersonalSearchResult(hits, scores_comparable=False)
        return self._search(
            query, context, SearchOptions(
                filters=filters, as_of=as_of, top_k=top_k, disclosure=disclosure,
                with_trajectory=with_trajectory,
                hierarchy_kind=hierarchy_kind, hierarchy_role=hierarchy_role,
                span_start=span_start, span_end=span_end, expand_depth=expand_depth, rollup=rollup,
                task_id=task_id, anchor=anchor, duty=duty, view_budget=view_budget,
            ), security=security,
        )

    def _search(
        self, query: str, context: Context, options: SearchOptions,
        *, security: RequestSecurityContext,
    ) -> RetrievalResult:
        """按统一选项检索；保留单空间、跨空间与树检索链路。

        ``query_context`` 经 ``Context.extensions`` 进入 API 边界，校验并转换为
        ``RetrievalQuery.query_context``，供支持上下文感知改写的 parser 消费。
        """
        context = Context(
            scope=deepcopy(context.scope),
            extensions=_copy_search_extensions(dict(context.extensions)),
        )
        # 读取默认策略也是业务组件访问，须先验证请求上下文来源。
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.READ, "search", context.scope)
        )
        options = deepcopy(options)
        if not self._hierarchy_enabled():
            options = replace(
                options, hierarchy_kind=None, hierarchy_role=None,
                span_start=None, span_end=None, expand_depth=0, rollup=False,
            )
        hierarchy = HierarchyQuery.from_query(options)
        identity = security.auth.actor
        # Context 在边界处拆包：scope 照旧作独立轴下推（鉴权 + 检索），
        # extensions 写入调用级 options 顺 parser 透传给自定义检索模块；
        # Context 对象本身不进内核。约定 key 在此解释并从透传 options 中移除，
        # 避免与内核已解释的字段重复：max_tokens（自适应披露预算）解析为 typed int
        # 写入 RetrievalQuery，coords（归属坐标）折算成第二族收窄谓词，spaces（候选空间）
        # 决定走单空间还是跨空间编排，query_context 转为 typed tuple 供改写 parser 消费。
        extensions = dict(context.extensions)
        max_tokens = _parse_max_tokens(extensions.pop(EXT_MAX_TOKENS, None))
        query_context = _pop_query_context(extensions)
        pipeline = extensions.pop("retrieval_pipeline", None)
        self._queries.validate_retrieval_pipeline(pipeline)
        if self._privacy is not None:
            query = self._privacy.preview(query)
            if query_context is not None:
                query_context = tuple(self._privacy.preview(turn) for turn in query_context)
        spaces = _pop_spaces(extensions)
        routing_enabled = self._routing_enabled()
        coords = _pop_coords(extensions, enabled=routing_enabled)
        reject_kernel_coords(coords)
        resource_principal = self._resource_principal(security, Action.READ)
        # 坐标折算成第二族收窄谓词的取值，在此算一次、两条路径共用。放进各自分支即
        # 「两处各折算一次」：漏调哪一处，该路径的 agent 维与 session 维整体不再收窄，
        # 失效方向是放宽且不报错。
        narrow = narrow_dims_of(
            principal.kernel_coords(coords, resource_principal), self._route_table.narrow_dims
        )
        if self._document_mode and not routing_enabled:
            # 标量 project 直写的文档同样需要项目过滤，且无需解析其他路由坐标。
            raw_coords = extensions.get(COORDS_KEY)
            project = raw_coords.get("project") if isinstance(raw_coords, dict) else None
            if isinstance(project, str) and project.strip():
                narrow["project"] = project.strip()
        rq = RetrievalQuery(
            text=query, filters=options.filters, as_of=options.as_of,
            top_k=options.top_k, disclosure=options.disclosure,
            max_tokens=max_tokens, with_trajectory=options.with_trajectory,
            extensions=extensions, hierarchy_kind=hierarchy.hierarchy_kind,
            hierarchy_role=hierarchy.hierarchy_role,
            span_start=hierarchy.span_start, span_end=hierarchy.span_end,
            expand_depth=options.expand_depth, rollup=options.rollup,
            query_context=query_context, pipeline=pipeline,
        )
        # 配置中的类型范围在鉴权及视图组装前与调用方过滤一起生效。
        rq = self._queries.prepare_retrieval_query(rq)
        view_requested = bool(options.task_id or options.anchor is not None or options.duty)
        if view_requested:
            if spaces is not None:
                raise ValidationError(
                    "continuity view cannot be combined with cross-space retrieval"
                )
            return self._search_view(
                query, context, replace(options, filters=rq.filters), security=security,
            )
        if spaces is not None:
            self._require_trusted_context(
                security, _TrustedRequestTarget(Action.READ, "search", Scope(org=context.scope.org))
            )
            return self._search_spaces(
                context, rq, _SearchAccess(spaces, coords, narrow), security=security,
            )
        # 权限上下文与 RetrievalQuery 共用同一规范化后的 FilterExpr（不重复转换）。
        permission_context = _recall_permission_context(
            context, rq.filters, self._authorizer.routing_fields()
        )
        auth, auth_context = self._authorize_with_context(
            security,
            context.scope,
            Action.READ,
            "search",
            context=permission_context,
        )
        # 用户表达式作整体 child 并入外层 AND（与 lifecycle/时间谓词同一机制），不会被
        # 其内部的 OR 稀释。回注的判据见 _routing_clauses_of。
        routing_clauses = _routing_clauses_of(
            permission_context, self._routing_fields_for("search")
        )
        if routing_clauses:
            rq.filters = and_merge(rq.filters, routing_clauses)
        # 类型隔离的系统谓词（mem2.0 §2.3）：经验与技能可与对话记忆同空间，检索
        # 互不可见由这条谓词承担——无路由值/非隔离路由值排除两类条目，隔离路由值
        # 只取该类。路由值按 profile 选择同一口径解析（extensions 优先、其次含
        # 授权回注谓词的 filters），追加在授权路由谓词之后：解析看到的 filters 与
        # engine 选 profile 时将看到的一致，两边各解析一套即「选了经验 profile、
        # 却被自己的谓词排除经验」。
        if self._queries.retrieval_pipeline_filter(pipeline) is None:
            route = route_value_of(rq.extensions, rq.filters, self._route_key())
            rq.filters = and_merge(rq.filters, [_type_isolation_clause(route)])
        # 两族谓词与调用方表达式合成一个 AND 一次下推，在 top-k 截断之前生效——
        # 召回后二次过滤会让被筛掉的条目白占召回名额，最终返回条数少于 top_k。
        # 第二族由归属坐标折算：坐标缺项不生成对应谓词，表现为该维不收窄，失效方向是放宽。
        system_clauses = space_predicates.system_predicates(
            auth_context.space_facts if auth_context is not None else None,
            resource_principal,
            narrow,
        )
        if system_clauses:
            rq.filters = and_merge(rq.filters, system_clauses)
        result = asyncio.run(self._queries.recall(context.scope, rq))
        self._log(identity, "search", target_scope=context.scope, detail=auth)
        return self._guard_continuity_search(result, scope=context.scope)

    def _search_view(
        self, query: str, context: Context, options: SearchOptions,
        *, security: RequestSecurityContext,
    ) -> RetrievalResult:
        """Task background / duty view path of ``search`` (S11 5.2, 6.2.6)."""
        from jiuwen_memory.common.type_def.continuity import RoleViewQuery
        from jiuwen_memory.retrieval.types import RetrievedItem

        scope = context.scope
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.READ, "search", scope)
        )
        self._authorize(security, scope, Action.READ, "search")
        service = self._role_service(scope)
        request = RoleViewQuery(
            query=query,
            anchor=options.anchor,
            task_id=options.task_id,
            duty=options.duty if options.duty is not None else "general",
            budget=options.view_budget or ViewBudget(),
        )
        # 调用方的过滤与时间约束在视图路径同样生效：锚点、任务背景与关系端点逐条判过，
        # 语义候选发现也带上同一份条件（否则明确不匹配的条目仍会经 anchor 漏出）。
        normalized_filters = normalize(options.filters)
        raw_read = self._role_reader(security, scope)

        def admits(unit) -> bool:
            if normalized_filters is not None and not matches_memory_unit(
                unit, normalized_filters
            ):
                return False
            if options.as_of is not None:
                moment = as_utc(options.as_of)
                valid_from = unit.temporal.t_valid
                valid_until = unit.temporal.t_invalid
                if valid_from is not None and as_utc(valid_from) > moment:
                    return False
                if valid_until is not None and as_utc(valid_until) <= moment:
                    return False
            return True

        def read(ref):
            unit = raw_read(ref)
            if not admits(unit):
                # 视图构建器把不可读引用当作「跳过这条边/条目」，与权限不足同一形态。
                raise NotFoundError("filtered")
            return unit

        if service.mode == "continuity":
            # 接续模式：普通检索提供语义召回，起点（anchor/任务背景）与合并顺序由插件
            # 归一化，本层只做类型转换。
            base = self._search(
                query,
                context,
                replace(options, task_id="", anchor=None, duty=None, view_budget=None),
                security=security,
            )
            bundled = service.continuity_view_result(
                scope,
                request,
                read,
                extra_items=[
                    {
                        "unit_id": item.unit_id,
                        "content": item.content,
                        "system_metadata": dict(item.system_metadata),
                    }
                    for item in base.items
                ],
                top_k=options.top_k,
            )
            return RetrievalResult(
                items=[
                    RetrievedItem(
                        unit_id=entry["unit_id"],
                        abstract=entry["text"],
                        overview=entry["text"],
                        content=entry["text"],
                        view=entry["annotation"],
                    )
                    for entry in bundled["items"]
                ],
                # 部分通道失败必须对调用方可见：视图是补充，不是把底层错误吞掉。
                errors=list(base.errors),
                paths=bundled["paths"],
                gaps=bundled["gaps"],
                view_meta=bundled["meta"],
            )

        discover_errors: list = []

        def discover(text, limit):
            results = self.search(
                text,
                Context(scope, dict(context.extensions)),
                security=security,
                top_k=limit,
                disclosure=DisclosureLevel.L2,
                filters=options.filters,
                as_of=options.as_of,
            )
            discover_errors.extend(results.errors)
            units = []
            for item in results.items:
                try:
                    unit = self.get(item.unit_id, scope, security=security)
                    read(MemoryRef.of(unit))
                    units.append(unit)
                except (PermissionDeniedError, ConflictError, NotFoundError):
                    continue
            return units

        # 视图组装（条目归一化、排序、截断缺口、元信息）在插件里完成；本层只做类型
        # 转换与审计。
        bundled = service.view_result(scope, request, read, discover, top_k=options.top_k)
        items = [
            RetrievedItem(
                unit_id=entry["unit_id"],
                abstract=entry["text"],
                overview=entry["text"],
                content=entry["text"],
                view=entry["annotation"],
            )
            for entry in bundled["items"]
        ]
        self._log(
            security.auth.actor, "search", target_scope=scope,
            detail={"view": "role", "duty": request.duty},
        )
        return RetrievalResult(
            items=items,
            errors=discover_errors,
            paths=bundled["paths"],
            gaps=bundled["gaps"],
            view_meta=bundled["meta"],
        )

    def _search_spaces(
        self, context: Context, retrieval_query: RetrievalQuery, access: _SearchAccess,
        *, security: RequestSecurityContext,
    ) -> RetrievalResult:
        """跨空间检索：本层做前两步半，后三步下沉控制层（F07「多空间读写」）。

        由 :meth:`search` 在 ``extensions`` 带 ``spaces`` 键时分流进来，不是独立入口。
        参数袋的拆包与坐标折算都在 :meth:`search` 内完成，结果经形参传入：同一个参数袋
        解释两遍，两处的解释一旦分叉即出现「同一次调用两套取值」。``coords`` 只用于日志，
        实际生效的是已折算好的 ``narrow``。

        | 步 | 内容 | 落点 |
        |---|---|---|
        | 1 定候选空间 | 显式 ``spaces`` 或主体反查索引 | 本层（反查按 ``identity``） |
        | 2 逐空间判权与状态校验 | 唯一 PDP（``_decide_space_entry``） | 本层（循环体就是 PEP） |
        | 2.5 逐空间谓词 | 路由值回注 + 两族系统谓词 | 本层（按 ``identity`` 与空间事实） |
        | 3—5 摊配、扇出、合并 | 取数上界、逐空间召回、轮转合并 | 控制层 ``cross_space_recall`` |

        分界取 S02「不做业务编排逻辑」那条的原文判据——「移出本层是否还能按 ``identity``
        裁决」。前两步半读 ``identity``，移出即把 PEP 分裂为两处；后三步在候选集与谓词都
        已定妥之后执行，全程不需要知道调用方是谁，留在本层只是让 PEP 多背一段取数编排。

        第 2 步不放进协程：事实读取与判权都是同步调用，授权记录查询还会在存储层串行。
        判权前置的副产品是取数上界按实际可读空间数计算，无权空间不再凭空占用召回名额。

        ``context.scope`` 只取 ``org`` 维定组织边界，空间维由候选集给出、传了不生效。
        """
        spaces, coords, narrow = access.spaces, access.coords, access.narrow
        identity = security.auth.actor
        principal.require_principal(identity)
        resource_principal = self._resource_principal(security, Action.READ)
        org = context.scope.org
        normalized_filters = retrieval_query.filters

        candidates = self._search_candidates(
            resource_principal, org, spaces, audit_actor=identity
        )
        # 经验检索一次只查一个空间（mem2.0 §2.4）：路由值为 experience 的跨空间
        # 检索返回参数错误——选取算子按一次返回的条数安排探索名额、按能力分组与
        # 补满，跨空间时各空间分别选出的结果还要轮转合并并截断，最终留下哪几条由
        # 合并决定，探索与分组都失去作用。同时检索个人与团队两个空间的经验要求
        # 选取在跨空间合并之后执行，首版不支持，列为已知遗留。
        fixed_type_filter = self._queries.retrieval_pipeline_filter(retrieval_query.pipeline)
        route_value = route_value_of(
            retrieval_query.extensions, normalized_filters, self._route_key(),
        ) if fixed_type_filter is None else ""
        includes_experience = fixed_type_filter is not None and evaluate(
            fixed_type_filter, lambda clause: matches_filter_value("experience", clause),
        )
        if route_value == "experience" or includes_experience:
            raise ValidationError(
                "路由值为 experience 的检索一次只查一个空间，不接受跨空间检索"
            )
        # 类型隔离的系统谓词（mem2.0 §2.3「跨空间检索走同一处」）：不随空间变，
        # 循环外算一次，逐空间随两族系统谓词下推。
        type_clause = _type_isolation_clause(route_value) if fixed_type_filter is None else None
        # 收窄谓词的实际取值只在此处成形，下游只能看到条数。缺这一行时「召回为空」
        # 无法区分坐标未传到、判定表未声明该维、以及该维确实过滤掉了全部条目。
        logger.info(
            "search cross-space: query=%s coords=%s narrow=%s candidate_spaces=%s",
            redact_for_log(retrieval_query.text),
            metadata_for_log(coords),
            metadata_for_log(narrow),
            metadata_for_log(candidates),
        )
        targets: list[collective.SpaceRecallTarget] = []
        denied: list[ChannelError] = []
        backend_failures: list[ChannelError] = []
        for space in candidates:
            target = Scope(org=org, space=space)
            permission_context, facts = self._apply_space_policy_context(
                target,
                _recall_permission_context(
                    Context(scope=target, extensions=dict(context.extensions)),
                    normalized_filters,
                    self._authorizer.routing_fields(),
                ),
                entry="search",
            )
            try:
                outcome = self._decide_space_entry(
                    security,
                    target,
                    Action.READ,
                    "",
                    permission_context,
                    now=datetime.now(UTC),
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
            except PlaintextDataError:
                raise
            except BackendError as exc:
                backend_failures.append(_space_denied(space, type(exc).__name__, str(exc)))
                continue
            except (PermissionDeniedError, NotFoundError, ValidationError) as exc:
                denied.append(_space_denied(space, type(exc).__name__, str(exc)))
                continue
            if outcome.allowed:
                # 第 2.5 步：本空间专属的系统谓词。与单空间入口同一处理——授权所依据的
                # 路由值回注，再叠两族系统谓词。逐空间各算自己的那份：各空间的授权可以
                # 来自不同的策略，共用一份即某个空间按另一个空间的授权取数。
                #
                # 这一步留本层而不随扇出一起下沉：两族谓词由 ``identity`` 与该空间的事实
                # 生成，是 S02「鉴权驱动的编排」明列的一项（生成并回注系统谓词）。
                clauses = _routing_clauses_of(
                    permission_context, self._routing_fields_for("search")
                )
                clauses.extend(
                    space_predicates.system_predicates(
                        permission_context.space_facts, resource_principal, narrow
                    )
                )
                if type_clause is not None:
                    clauses.append(type_clause)
                targets.append(collective.SpaceRecallTarget(scope=target, clauses=tuple(clauses)))
            else:
                denied.append(
                    _space_denied(
                        space,
                        PermissionDeniedError.__name__,
                        f"read denied: rule={outcome.rule} reason={outcome.reason_code}",
                    )
                )
        # 候选集非空而一个都读不到时抛，与单空间路径同一处置：那条路径上无权即
        # PermissionDeniedError，本路径若静默返回空，同一个方法的两条路径对「完全无权」
        # 给出两种结果，且「无权」与「这些空间里没有内容」在调用方看来不可区分。
        # 候选集为空不抛——那是「主体不在任何空间里」，是合法的空结果。
        if candidates and not targets:
            if backend_failures:
                raise BackendError("authorization backend unavailable for candidate spaces")
            # 候选来自调用方显式传入时回显空间名（那是他自己的入参，便于排查）；来自主体
            # 反查索引时只给条数。索引按 `context.scope.org` 建桶，而该 org 取自参数袋、
            # 与 `identity.org` 无一致性校验——回显即把另一个组织的空间名交给调用方，而
            # 逐空间判权只挡住了访问，挡不住这行措辞。
            detail = repr(candidates) if spaces else f"{len(candidates)} space(s)"
            raise PermissionDeniedError(f"read denied on every candidate space: {detail}")

        # 查询骨架装配一次，逐空间只差取数上界与本空间谓词，两项都由控制层补齐。
        # 逐空间构造 RetrievalQuery 的循环本身就是取数编排，不留本层。
        # 同步桥接留本层（S02「同步/异步桥接」）：控制层给协程，本层 asyncio.run。
        item_scopes = {}

        async def guarded_recall(target_scope, query):
            result = await self._queries.recall(target_scope, query)
            # Trusted fan-out origin, never a Scope supplied in memory metadata.
            for item in result.items:
                item_scopes[id(item)] = target_scope
            return self._guard_continuity_search(result, scope=target_scope)

        merged, space_failures = asyncio.run(
            collective.recall_spaces(
                targets,
                retrieval_query,
                top_k=retrieval_query.top_k,
                recall=guarded_recall,
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
        merged.errors.extend(denied + backend_failures + space_failures)
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
                "authorization_backend_failures": str(len(backend_failures)),
                "failed_spaces": str(len(space_failures)),
                "count": str(len(merged.items)),
            },
        )
        return self._guard_continuity_search(merged, item_scopes=item_scopes)

    def _hierarchy_enabled(self) -> bool:
        """仅在可信请求通过校验后读取运行时树总开关。"""
        return str(self._policy.all().get("hierarchy.enabled", "false")).strip().lower() == "true"

    def _route_key(self) -> str:
        """路由键：启用流水线时取其声明（缺省 ``memory_type``），未启用时取缺省。

        类型隔离谓词的路由值解析（mem2.0 §2.3）与 profile 选择共用同一个键：
        两侧键不一致即「按 A 键选了经验 profile、按 B 键解析出无路由值」，
        追加的是排除经验的谓词。未启用流水线的部署没有 profile 可选，但仍按
        同一键解析——请求 filters 点名 ``memory_type`` 时谓词取等值一侧。
        """
        pipeline = self._engine.pipeline
        return getattr(pipeline, "route_key", None) or "memory_type"

    def _search_candidates(
        self, identity: Scope, org: str, spaces: list[str], *, audit_actor: Scope | None = None
    ) -> list[str]:
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
                audit_actor if audit_actor is not None else identity,
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

    def _personal_candidates(self, security: RequestSecurityContext) -> list[PersonalScopeBinding]:
        """按当前身份发现个人候选；读取时仍逐范围鉴权。"""
        identity = security.auth.actor
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.READ, "personal_scope_discovery", identity)
        )
        principal.require_principal(identity)
        eligible: list[PersonalScopeBinding] = []
        for scope in self._queries.candidate_scopes():
            binding = personal_scope_binding(identity, scope)
            if binding is not None:
                eligible.append(deepcopy(binding))
        return eligible

    def list(
        self,
        scope: Scope | None = None,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
    ) -> MemoryListResult | PersonalPage:
        normalized_extensions = _normalize_list_extensions(extensions)
        if "personal" in normalized_extensions:
            selection = normalized_extensions["personal"]
            personal_only = scope is None and set(normalized_extensions) == {"personal"}
            has_scoped_filters = memory_types is not None or filters is not None
            valid_selection = isinstance(selection, dict) and selection.keys() <= {
                "source", "agent", "project", "query", "tier", "memory_class", "lifecycle",
                "snapshot", "include_lifecycle_counts",
            }
            if not personal_only or has_scoped_filters or not valid_selection:
                raise ValidationError("invalid personal list options; omit scope")
            candidates = self._personal_candidates(security)
            bindings = {scope_key(binding.scope): binding for binding in candidates}

            def read_scope(target, start, size):
                return self._list_scope(
                    target, security=security, offset=start, limit=size,
                    filters=bindings[scope_key(target)].author_filter(),
                    skip_unavailable_scope=True,
                )

            return list_personal(
                [binding.scope for binding in candidates], read_scope,
                offset=offset, limit=limit, **selection,
            )
        if scope is None:
            raise ValidationError("missing required field(s) for list: scope")
        return self._list_scope(
            scope, security=security, offset=offset, limit=limit, memory_types=memory_types,
            extensions=normalized_extensions, filters=filters,
        )

    def _list_scope(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
        skip_unavailable_scope: bool = False,
    ) -> MemoryListResult:
        identity = security.auth.actor
        normalized_extensions = extensions if extensions is not None else {}
        normalized_filters = normalize(filters)
        permission_contexts = _list_permission_contexts(
            scope,
            memory_types,
            normalized_filters,
            normalized_extensions,
            self._authorizer.routing_fields(),
        )
        auth: dict[str, str] = {}
        auth_context: PermissionContext | None = None
        try:
            for permission_context in permission_contexts:
                auth, auth_context = self._authorize_with_context(
                    security,
                    scope,
                    Action.READ,
                    "list",
                    context=permission_context,
                )
        except (PermissionDeniedError, NotFoundError):
            if skip_unavailable_scope:
                return MemoryListResult()
            raise
        routing_clauses = _list_routing_clauses(
            permission_contexts,
            self._routing_fields_for("list"),
            memory_types,
        )
        effective_filters = and_merge(normalized_filters, routing_clauses)
        # 类型隔离（mem2.0 §2.3）：显式点名 memory_types 的请求由该参数在存储层
        # 过滤，点名的类型即调用方所要，与 _list_routing_clauses 对 memory_type
        # 字段的跳过同构；未点名时与 search 同一条谓词——默认列举不混入经验与
        # 技能，按路由值点名的只取该类。list 不经 pipeline 选 profile，路由值仍
        # 按同一口径从 extensions 与 filters 解析。
        if not any(str(value).strip() for value in memory_types or ()):
            effective_filters = and_merge(
                effective_filters,
                [
                    _type_isolation_clause(
                        route_value_of(
                            normalized_extensions, normalized_filters, self._route_key()
                        )
                    )
                ],
            )
        # list 必须注入第一族，否则个体空间的隔离只在 search 上成立：它是同样按空间返回
        # 条目的批量入口，不注入的后果与 search 同因同向。第二段逐条鉴权不能替代谓词——
        # 逐条鉴权的失败形态是抛异常而非过滤，整次调用失败而不是少返回几条。
        system_clauses = _first_family_predicate(
            self._resource_principal(security, Action.READ), auth_context
        )
        if system_clauses:
            effective_filters = and_merge(effective_filters, system_clauses)
        # 不可用来源（停用 ∪ 隔离）在分页与 count 之前下推：逐条复核的失败形态是抛异常，
        # 一条被隔离即整页失败；下推后表现为少返回几条，且 count 与页面一致。
        unavailable = self._unavailable_continuity_connectors(scope)
        if unavailable:
            effective_filters = and_merge(
                effective_filters,
                [
                    FilterClause(
                        "system_metadata.continuity_connector",
                        FilterOp.NOT_IN,
                        list(unavailable),
                    )
                ],
            )
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
                security,
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
        # 来源过滤已下推（分页前生效），但**依赖证据复核不可省**：依据被修订/撤回后，
        # get 会拒绝而 list 若只靠谓词仍会返回该结论。逐条复核并**剔除**不合规条目，
        # 不用抛异常（整页失败）也不用 continue（什么也滤不掉）。
        visible = []
        for unit in result.items:
            try:
                self._guard_continuity_snapshot(unit, security)
            except AgentMemoryError:
                # 依赖不可读、摘要已变或来源不可用都表现为「这条不再可披露」；
                # 列表按条剔除，不整页报错（get 仍按点读语义抛错）。
                continue
            visible.append(unit)
        if len(visible) != len(result.items):
            # 逐条剔除发生在分页之后，count 必须同步扣掉，否则调用方按 count 翻页会
            # 反复落到全是空页的区间。
            result.count = max(0, result.count - (len(result.items) - len(visible)))
            result.items = visible
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
            security,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._queries.permission_context_for_unit(unit_id, scope))
        # 第二段的目标取条目真源 scope，不沿用入参（F07「条目级入口分两段鉴权」）：
        # 它是「判定第 8 步不会命中」的两个前置条件之一，与「回填后条目 scope 只有两维」
        # 各兜一重，两条的失效方向相反。沿用入参即把两重约束落在同一个取值上。
        # 与 list / delete 同一口径。
        auth = self._authorize(
            security,
            permission_context.scope,
            Action.READ,
            "get",
            unit_id,
            context=permission_context,
        )
        unit = asyncio.run(self._queries.get(unit_id, scope, as_of))
        # as_of 可选中不同版本；即使 ID 不变，也必须按实际返回快照再次判权。
        # 保留请求 ID 的前置检查，不能借版本链跳过请求条目的访问边界。
        auth = self._authorize(
            security,
            unit.scope,
            Action.READ,
            "get",
            unit.id,
            context=_unit_snapshot_permission_context(unit),
        )
        self._guard_continuity_snapshot(unit, security)
        self._log(
            identity,
            "get",
            unit_id,
            target_scope=scope,
            detail={**auth, "after_unit_id": unit.id},
        )
        return unit

    def _do_single_update(
        self,
        unit_id: str,
        scope: Scope,
        patch: MemoryPatch,
        security: RequestSecurityContext,
    ) -> MemoryUnit:
        """单条更新的 mutation 核心（不含 metadata 拒绝和顶层鉴权，供 batch_update 复用）。

        走完整的 schema prepare→逐项鉴权→commit 流程；普通记忆直接 commands.update。
        """
        self._ensure_space_writable(scope)
        before = asyncio.run(self._queries.get(unit_id, scope, None))
        plan = None
        if self._commands.requires_update_preparation(before, patch):
            plan = asyncio.run(self._commands.prepare_update(unit_id, scope, patch))
        if plan is not None:
            for action, context in self._commands.update_permission_contexts(plan):
                self._authorize(
                    security,
                    context.scope,
                    action,
                    "batch_update",
                    context.unit_id,
                    context=context,
                )
            unit = asyncio.run(self._commands.commit_update(plan))
        else:
            unit = asyncio.run(self._commands.update(unit_id, scope, patch))
        return unit

    @privacy_operation
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
            security,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._queries.permission_context_for_unit(unit_id, scope))
        # 第二段的目标取条目真源 scope，理由同 get。随后的 _ensure_space_writable 仍取
        # 入参 scope——它校验的是本次写入落点所在空间，与鉴权目标是两件事。
        auth = self._authorize(
            security,
            permission_context.scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=permission_context,
        )
        self._ensure_space_writable(scope)
        # mutation 前审计完整性预检与故障隔离（R3，计划 §9.3）。
        self._ensure_audit_integrity_healthy()
        before = asyncio.run(self._queries.get(unit_id, scope, None))
        _reject_memory_type_transition(before, patch.system_metadata)
        if self._privacy is not None:
            from dataclasses import replace as copy_patch

            from jiuwen_memory.common.type_def import RawPayload

            protected = self._privacy.ingress(RawPayload(
                id=unit_id, scope=scope, data=(patch.content or "").encode("utf-8"),
                system_metadata=dict(patch.system_metadata or {}),
                user_metadata=dict(patch.user_metadata or {}),
            ))
            self._privacy.inherit_classification(protected, [before])
            protected_tags = self._privacy.ingress(RawPayload(
                id=unit_id, scope=scope, user_metadata={"tags": patch.tags or []},
            )).user_metadata["tags"] if patch.tags is not None else None
            patch = copy_patch(
                patch, content=protected.data.decode() if patch.content is not None else None,
                system_metadata=protected.system_metadata,
                user_metadata=protected.user_metadata if patch.user_metadata is not None else None,
                tags=protected_tags,
            )
        plan = None
        if self._commands.requires_update_preparation(before, patch):
            plan = asyncio.run(self._commands.prepare_update(unit_id, scope, patch))
        if plan is not None:
            for action, context in self._commands.update_permission_contexts(plan):
                self._authorize(
                    security,
                    context.scope,
                    action,
                    "update",
                    context.unit_id,
                    context=context,
                )
            unit = asyncio.run(self._commands.commit_update(plan))
        else:
            unit = asyncio.run(self._commands.update(unit_id, scope, patch))
        if self._privacy is not None:
            unit = asyncio.run(self._queries.get(unit.id, unit.scope, None))
        update_detail = {}
        if plan is not None:
            affected_unit_ids = []
            for change in plan.changes:
                if change.before is not None:
                    affected_unit_ids.append(change.before.id)
                if change.after is not None:
                    affected_unit_ids.append(change.after.id)
            update_detail = {
                "schema_update_operation_id": plan.operation_id,
                "affected_unit_ids": list(dict.fromkeys(affected_unit_ids)),
            }
        self._log(
            identity,
            "update",
            unit_id,
            target_scope=scope,
            detail={**auth, **update_detail, "before_unit_id": before.id, "after_unit_id": unit.id},
        )
        return unit

    @privacy_operation
    def batch_update(
        self,
        selector: UpdateSelector,
        patches: list[MemoryPatch],
        *,
        security: RequestSecurityContext,
        limit: int | None = None,
        continue_on_error: bool = True,
    ) -> BatchUpdateResult:
        identity = security.auth.actor
        selector_is_empty = (
            not selector.unit_ids
            and not selector.tags
            and selector.before is None
            and selector.after is None
            and selector.filters is None
        )
        if selector_is_empty:
            raise ValidationError(
                "UpdateSelector requires unit_ids, tags, before, after, or filters"
            )
        if not patches:
            raise ValidationError("patches must be non-empty")
        if limit is not None and limit <= 0:
            raise ValidationError("limit must be a positive integer")
        for patch in patches:
            _reject_kernel_system_metadata(patch.system_metadata)
            _reject_route_tag_keys(patch.system_metadata, self._route_table.tag_keys)
            _reject_non_scalar_metadata(patch.system_metadata, field_name="system_metadata")
            _reject_non_scalar_metadata(patch.user_metadata, field_name="user_metadata")
        unified = len(patches) == 1
        if not unified:
            if not selector.unit_ids:
                raise ValidationError(
                    "per-item patches require selector.unit_ids to be non-empty"
                )
            if len(selector.unit_ids) != len(patches):
                raise ValidationError(
                    f"len(unit_ids)={len(selector.unit_ids)} must equal "
                    f"len(patches)={len(patches)} in per-item mode"
                )
        target = selector.scope or _ROOT
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.UPDATE, "batch_update", target)
        )
        # selector 级跨范围鉴权闸门（对齐 delete 的 L761-768）
        selector_context = PermissionContext(
            resource_type="update_selector",
            scope=target,
            tags=tuple(selector.tags),
        )
        if selector.scope is not None or not selector.unit_ids:
            self._authorize(
                security,
                target,
                Action.UPDATE,
                "batch_update",
                context=selector_context,
            )
        contexts = asyncio.run(self._queries.permission_contexts_for_update(selector))
        auth: dict[str, str] = {}
        if not contexts:
            auth = self._authorize(
                security,
                target,
                Action.UPDATE,
                "batch_update",
                context=selector_context,
            )
        else:
            auth = {"permission_check": "enabled", "permission_reason": "permission check passed"}
            for permission_context in contexts:
                unit_auth = self._authorize(
                    security,
                    permission_context.scope,
                    Action.UPDATE,
                    "batch_update",
                    permission_context.unit_id,
                    context=permission_context,
                )
                auth.update(unit_auth)
        # mutation 前审计完整性预检（对齐 update/delete 的 R3 约束）
        self._ensure_audit_integrity_healthy()
        candidates = list(contexts)
        effective_limit = limit if limit is not None else selector.limit
        # selector.limit 与 API 参数 limit 同为显式契约，统一走正整数校验，
        # 不静默忽略 0/-1（否则会宽放行为「更新全部候选」）。
        if effective_limit is not None and effective_limit <= 0:
            raise ValidationError("limit must be a positive integer")
        # 逐条模式 limit 只做下界承诺（不得小于 patches 数量），不截断候选集：
        # 截断会让 unit_ids 后段查不到 candidate_map 被误判 NotFoundError。
        if effective_limit is not None and not unified and effective_limit < len(patches):
            raise ValidationError(
                f"limit={effective_limit} must be >= len(patches)={len(patches)} "
                "in per-item mode"
            )
        if effective_limit is not None and not selector.unit_ids:
            candidates = candidates[:effective_limit]
        outcomes: list[BatchUpdateOutcome] = []
        if not unified:
            # 逐条模式：按 unit_ids 顺序逐项处理，补丁按 unit_ids 下标对应，
            # 缺失 id 生成 error outcome，避免静默丢弃 + 补丁错位（Issue #6）。
            candidate_map = {ctx.unit_id: ctx for ctx in candidates}
            for i, uid in enumerate(selector.unit_ids):
                ctx = candidate_map.get(uid)
                if ctx is None:
                    # 缺失 id：生成 error outcome
                    outcomes.append(
                        BatchUpdateOutcome(
                            index=i,
                            unit_id=uid,
                            error="unit not found",
                            error_type="NotFoundError",
                        )
                    )
                    if not continue_on_error:
                        for j in range(i + 1, len(selector.unit_ids)):
                            outcomes.append(
                                BatchUpdateOutcome(
                                    index=j,
                                    unit_id=selector.unit_ids[j],
                                    error="skipped after previous item failed",
                                    error_type="Skipped",
                                )
                            )
                        break
                    continue
                patch = patches[i]
                try:
                    updated = self._do_single_update(
                        ctx.unit_id, ctx.scope, patch, security,
                    )
                    outcomes.append(
                        BatchUpdateOutcome(index=i, unit_id=ctx.unit_id, updated_unit=updated)
                    )
                except Exception as exc:
                    is_domain_error = isinstance(exc, AgentMemoryError)
                    if not is_domain_error:
                        logger.exception("unexpected batch update failure at index %s", i)
                    error_msg = str(exc) if is_domain_error else "unexpected batch update failure"
                    outcomes.append(
                        BatchUpdateOutcome(
                            index=i,
                            unit_id=ctx.unit_id,
                            error=error_msg,
                            error_type=type(exc).__name__ if is_domain_error else "InternalError",
                        )
                    )
                    if not continue_on_error:
                        for j in range(i + 1, len(selector.unit_ids)):
                            outcomes.append(
                                BatchUpdateOutcome(
                                    index=j,
                                    unit_id=selector.unit_ids[j],
                                    error="skipped after previous item failed",
                                    error_type="Skipped",
                                )
                            )
                        break
        else:
            # 统一模式：按候选顺序处理，所有候选共用 patches[0]
            for index, ctx in enumerate(candidates):
                patch = patches[0]
                try:
                    updated = self._do_single_update(
                        ctx.unit_id, ctx.scope, patch, security,
                    )
                    outcomes.append(
                        BatchUpdateOutcome(index=index, unit_id=ctx.unit_id, updated_unit=updated)
                    )
                except Exception as exc:
                    is_domain_error = isinstance(exc, AgentMemoryError)
                    if not is_domain_error:
                        logger.exception("unexpected batch update failure at index %s", index)
                    error_msg = str(exc) if is_domain_error else "unexpected batch update failure"
                    outcomes.append(
                        BatchUpdateOutcome(
                            index=index,
                            unit_id=ctx.unit_id,
                            error=error_msg,
                            error_type=type(exc).__name__ if is_domain_error else "InternalError",
                        )
                    )
                    if not continue_on_error:
                        for skipped_index in range(index + 1, len(candidates)):
                            outcomes.append(
                                BatchUpdateOutcome(
                                    index=skipped_index,
                                    unit_id=candidates[skipped_index].unit_id,
                                    error="skipped after previous item failed",
                                    error_type="Skipped",
                                )
                            )
                        break
        self._log(
            identity,
            "batch_update",
            target_scope=target,
            detail={
                **auth,
                "candidate_count": str(len(candidates)),
                "outcome_count": str(len(outcomes)),
            },
        )
        return BatchUpdateResult(outcomes=outcomes)

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
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.DELETE, "delete", target)
        )
        selector_context = _selector_permission_context(selector, target)
        if selector.scope is not None or not selector.unit_ids:
            self._authorize(
                security,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        contexts = asyncio.run(self._queries.permission_contexts_for_delete(selector))
        if not contexts:
            auth = self._authorize(
                security,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        else:
            auth = {"permission_check": "enabled", "permission_reason": "permission check passed"}
            for permission_context in contexts:
                unit_auth = self._authorize(
                    security,
                    permission_context.scope,
                    Action.DELETE,
                    "delete",
                    permission_context.unit_id,
                    context=permission_context,
                )
                auth.update(unit_auth)
        # mutation 前审计完整性预检与故障隔离（R3，计划 §9.3）。
        self._ensure_audit_integrity_healthy()
        deleted = asyncio.run(self._commands.delete(selector))
        if self._privacy is not None:
            # IndexBuilder covers normal removals; this also revokes raw-message
            # records removed by a direct purge path.
            for context in contexts:
                if context.unit_id in deleted and selector.mode.value == "purge":
                    self._privacy.discard(context.scope, [context.unit_id])
        self._log(
            identity,
            "delete",
            target_scope=target,
            detail={**auth, "before_unit_ids": json.dumps(deleted, ensure_ascii=False)},
        )
        return deleted

    def _evolve_hierarchy(
        self, scope: Scope, options: EvolveTaskOptions, *, security: RequestSecurityContext,
    ) -> str | None:
        """建树保留 WRITE/UPDATE 授权；关闭后只提交成功的跳过任务。"""
        scope, options = deepcopy(scope), deepcopy(options)
        validate_evolve_request(scope, options)
        auth = self._authorize(
            security, scope, Action.WRITE, "evolve",
            space_action=_evolve_space_action(options.mode),
        )
        self._ensure_space_writable(scope)
        self._authorize(security, scope, Action.UPDATE, "evolve",
                        space_action=_evolve_space_action(options.mode))
        if self._hierarchy_enabled():
            validate_evolve_options(scope, options)
            if self._authorizer.routing_fields():
                raise ValidationError("HIERARCHY 暂不支持权限路由或逐候选鉴权")
            _reject_kernel_system_metadata(options.hierarchy_options.metadata)
            _reject_route_tag_keys(options.hierarchy_options.metadata, self._route_table.tag_keys)
        else:
            options = replace(options, hierarchy_options=None, reuse_existing=False)
        self._ensure_audit_integrity_healthy()
        job_id = asyncio.run(self._commands.evolve_v2(scope, options))
        self._log(security.auth.actor, "evolve", target_scope=scope,
                  detail={**auth, "job_id": job_id})
        return job_id

    async def _start_background_jobs(
        self, scope: Scope, *, security: RequestSecurityContext,
    ) -> BackgroundJobStartResult:
        """Runtime 专用注册入口，复用当前 PEP 和树开关，不扩展 MemoryAPI。"""
        scope = deepcopy(scope)
        scope_contains(scope, scope)
        auth = self._authorize(security, scope, Action.WRITE, "evolve",
                               space_action=_evolve_space_action(EvolveMode.HIERARCHY))
        self._ensure_space_writable(scope)
        self._authorize(security, scope, Action.UPDATE, "evolve",
                        space_action=_evolve_space_action(EvolveMode.HIERARCHY))
        self._ensure_audit_integrity_healthy()
        policies = self._policy.all()
        enabled = all(str(policies.get(key, "false")).strip().lower() == "true"
                      for key in ("hierarchy.enabled", "hierarchy.auto_derive"))
        if not enabled:
            result = BackgroundJobStartResult(skipped=True, reason="hierarchy_disabled")
        else:
            if self._authorizer.routing_fields():
                raise ValidationError("周期 HIERARCHY 暂不支持权限路由或逐候选鉴权")

            def admission(target: Scope) -> None:
                if not scope_contains(scope, target):
                    raise ValidationError("background admission cannot expand registered home")
                self._require_trusted_context(
                    security, _TrustedRequestTarget(Action.WRITE, "evolve", target),
                )
                self._invalidate_space_facts(target.org, target.space)
                if self._authorizer.routing_fields():
                    raise ValidationError("周期 HIERARCHY 暂不支持权限路由或逐候选鉴权")
                for action in (Action.WRITE, Action.UPDATE):
                    self._authorize(
                        security, target, action, "evolve",
                        space_action=_evolve_space_action(EvolveMode.HIERARCHY),
                    )
                self._ensure_space_writable(target)

            result = await self._commands.start_background_jobs(
                scope, self._policy, admission=admission,
            )
        self._log(security.auth.actor, "evolve", target_scope=scope, detail={
            **auth, "trigger": "auto_derive", "job_ids": json.dumps(result.job_ids),
            "skipped": str(result.skipped).lower(), "reason": result.reason,
        })
        return result

    # 新选项追加在旧参数之后，保持公开签名兼容。
    def evolve(  # pylint: disable=too-many-arguments,unknown-option-value,huawei-too-many-arguments
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        candidate: CandidateSource | dict | None = None,
        dreaming: bool | None = None,
        interval: int = 0,
        security: RequestSecurityContext,
        hierarchy_options: HierarchyComposeOptions | None = None,
        reuse_existing: bool = False,
    ) -> str | None:
        if mode is EvolveMode.HIERARCHY:
            return self._evolve_hierarchy(
                scope, EvolveTaskOptions(
                    mode=mode, channel=channel, candidate=candidate, dreaming=dreaming,
                    interval=interval, hierarchy_options=hierarchy_options,
                    reuse_existing=reuse_existing,
                ), security=security,
            )
        identity = security.auth.actor
        # 注册态 interval 语义前置校验（鉴权前 fail fast，避免越权探测报错差异）。
        if dreaming is True and interval <= 0:
            raise ValidationError("dreaming 注册要求 interval > 0（秒）；立即单次演进不传 dreaming")
        auth = self._authorize(
            security,
            scope,
            Action.WRITE,
            "evolve",
            space_action=_evolve_space_action(mode),
        )
        self._ensure_space_writable(scope)
        if (
            (hierarchy_options is not None or reuse_existing is not False)
            and self._hierarchy_enabled()
        ):
            raise ValidationError("仅 HIERARCHY 接受 hierarchy_options/reuse_existing")
        self._ensure_audit_integrity_healthy()
        # 三态分发（F10 D2）：False=幂等注销（不命中不退化）；True=注册；
        # None=立即执行。持续授权（每 tick 复验）与 fan-out 逐桶裁决都在 API 层
        # 的 DreamingCoordinator（PEP 边界，S03）——Engine / EvolveJob 纯执行。
        if dreaming is False:
            entry = self._dreaming.unregister(scope, mode)
            self._log(
                identity,
                "evolve",
                target_scope=scope,
                detail={
                    **auth,
                    "dreaming": "False",
                    "unregistered": "true" if entry is not None else "absent",
                },
            )
            return None
        if dreaming is True:
            job_id = self._dreaming.register(
                scope=scope,
                mode=mode,
                channel=channel,
                candidate=candidate,
                interval=interval,
                security=security,
            )
            self._log(
                identity,
                "evolve",
                target_scope=scope,
                detail={
                    **auth,
                    "job_id": job_id,
                    "dreaming": "True",
                    "interval": str(interval),
                },
            )
            return job_id
        # 立即执行：fan-out 候选源先逐桶裁决（获准桶 / 拒绝桶标签），再走纯执行链。
        buckets, denied = self._dreaming.authorize_fan_out(security, scope, candidate, mode)
        job_id = asyncio.run(
            self._commands.evolve(
                scope,
                mode,
                channel,
                candidate=candidate,
                buckets=buckets,
                denied_scopes=denied,
            )
        )
        detail = {
            **auth,
            **({"job_id": job_id} if job_id is not None else {}),
            "dreaming": "None",
        }
        if denied:
            detail["denied_scopes"] = ";".join(denied)
        self._log(
            identity,
            "evolve",
            target_scope=scope,
            detail=detail,
        )
        return job_id

    def restore_dreaming(self) -> list[str]:
        """重启恢复注册态 dreaming 任务（F10 D8 装配期钩子，非 HTTP verb）。

        启动路径专用（server 启动 / SDK 宿主拉起），不出现在 MemoryAPI 公共
        契约——恢复会持实例锁，任意请求主体可触发会把部署模型的决定权让渡
        给调用方。恢复期复验与每 tick 复验同一套鉴权（DreamingCoordinator
        绑定本 API 的鉴权组件，入口判定与持续判定不漂移）。

        单实例模式下锁竞争抛 :class:`RuntimeError`。主备模式下实例在锁竞争时
        以待命身份启动，并定期重试接管。
        """
        return self._dreaming.restore()

    def stop_dreaming_election(self) -> bool:
        """Stop scheduling new election attempts and report whether to join the election."""
        return self._dreaming.stop_election()

    def wait_for_dreaming_election(self) -> None:
        """Join any pending election before draining the scheduler."""
        self._dreaming.wait_for_election()

    def release_dreaming_lock(self) -> None:
        """释放恢复实例锁（优雅关闭路径钩子，非 HTTP verb）。

        owner 匹配删除——本进程从未持锁（如共享 KV 的运维进程 close）或锁
        已被抢占时不删，避免把运行中实例的锁清掉导致第三实例恢复成功。
        """
        self._dreaming.release_lock()
