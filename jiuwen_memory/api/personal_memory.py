# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""个人记忆的范围绑定、单条读取和检索候选限制。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass

from jiuwen_memory.common.errors import (
    ConflictError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.security import principal
from jiuwen_memory.common.security.types import RequestSecurityContext
from jiuwen_memory.common.type_def import Context, MemoryUnit, Scope
from jiuwen_memory.common.type_def.filter import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    and_merge,
    normalize,
)
from jiuwen_memory.control.application.personal_memory import (
    LocatedHit,
    memory_source,
    scope_key,
    search_personal,
)
from jiuwen_memory.control.types import MemoryListResult
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalResult

from .memory_api import MemoryAPI


@dataclass(frozen=True)
class PersonalScopeBinding:
    scope: Scope
    author_principal: str = ""

    def author_filter(self) -> dict[str, str] | None:
        if self.author_principal:
            return {"system_metadata.author_principal": self.author_principal}
        return None


def personal_scope_binding(identity: Scope, scope: Scope) -> PersonalScopeBinding | None:
    """按当前身份判定个人归属，实际读取仍需由 MemoryAPI 鉴权。"""
    if scope.org != identity.org or (scope.user and scope.user != identity.user):
        return None
    if scope.user:
        return PersonalScopeBinding(scope)
    author, _ = principal.derive_author(identity)
    return PersonalScopeBinding(scope, author)


def _read_bound_record(api, binding, unit_id, security) -> MemoryUnit:
    unit = api.get(unit_id, binding.scope, security=security)
    if binding.author_principal and (
        unit.system_metadata.get("author_principal") != binding.author_principal
    ):
        raise PermissionDeniedError(message="memory is outside the personal binding")
    return unit


def read_personal_record(
    api: MemoryAPI, scope: Scope, unit_id: str, *, security: RequestSecurityContext,
) -> MemoryUnit:
    """检查指定记录的个人归属及当前权限，无需枚举其他范围。"""
    principal.require_principal(security.auth.actor)
    binding = personal_scope_binding(security.auth.actor, scope)
    if binding is None:
        raise PermissionDeniedError(message="scope is outside the personal binding")
    return _read_bound_record(api, binding, unit_id, security)


def search_personal_records(
    api: MemoryAPI,
    bindings: Sequence[PersonalScopeBinding],
    query: str,
    *,
    read_scope: Callable[..., MemoryListResult],
    skip_candidate: Callable[[MemoryUnit], bool],
    security: RequestSecurityContext,
    top_k: int,
    source: str | None,
) -> list[LocatedHit]:
    """复用原检索，并将候选限定在逐范围授权取得的记录 ID。"""
    if source is not None and not isinstance(source, str):
        raise ValidationError("source must be a string")
    by_scope = {scope_key(binding.scope): binding for binding in bindings}

    def author_filter(scope):
        return normalize(by_scope[scope_key(scope)].author_filter())

    source_filter = None
    if source:
        source_filter = FilterClause("system_metadata.source_app", FilterOp.EQ, source)

    def filters(scope, unit_ids):
        author = author_filter(scope)
        # Explicitly select all projects, including missing metadata (NE matches
        # missing values). Document recall otherwise defaults to project="".
        # This is a presentation boundary, not authorization: scope, author and
        # source restrictions remain AND predicates and API PEP still runs.
        all_projects = FilterGroup(FilterLogic.OR, [
            FilterClause("system_metadata.project", FilterOp.EQ, ""),
            FilterClause("system_metadata.project", FilterOp.NE, ""),
        ])
        predicates = (
            author, source_filter, all_projects, FilterClause("unit_id", FilterOp.IN, unit_ids),
        )
        clauses = [predicate for predicate in predicates if predicate is not None]
        return FilterGroup(FilterLogic.AND, clauses)

    # Ordinary document recall deliberately spans sessions. Restrict candidates
    # before top_k to the exact, authorized binding so strict get uses the same scope.
    remaining = 10000

    def search_scope(scope, count):
        nonlocal remaining
        page = read_scope(
            scope, security=security, limit=remaining + 1,
            filters=and_merge(
                author_filter(scope),
                [source_filter] if source_filter is not None else [],
            ),
        )
        if page.count < 0 or page.count > remaining:
            raise UnsupportedCapabilityError("list_size", "10000", "personal")
        if len(page.items) != page.count:
            raise ConflictError(message="personal search could not obtain a complete scope")
        if any(unit.scope != scope for unit in page.items):
            raise ValidationError("personal search candidate does not match its scope")
        remaining -= page.count
        # Exclude records that ordinary recall would drop after top_k.
        unit_ids = [
            unit.id for unit in page.items
            if (source != "" or not memory_source(unit)) and not skip_candidate(unit)
        ]
        if not unit_ids:
            return RetrievalResult()
        return api.search(
            query,
            Context(scope=scope),
            security=security,
            top_k=count,
            filters=filters(scope, unit_ids),
            disclosure=DisclosureLevel.L2,
            hierarchy_kind=None,
            hierarchy_role=None,
            expand_depth=0,
            rollup=False,
        )

    return search_personal(
        deepcopy(tuple(binding.scope for binding in bindings)),
        search_scope,
        lambda scope, unit_id: _read_bound_record(
            api, by_scope[scope_key(scope)], unit_id, security,
        ),
        top_k=top_k,
    )
