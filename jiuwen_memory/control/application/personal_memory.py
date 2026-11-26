# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""个人记忆聚合的取数编排；身份与授权由上游提供的回调承担。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import (
    FilterClause,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    matches_memory_unit,
)
from jiuwen_memory.common.type_def.memory_filter import collective_destination_of
from jiuwen_memory.control.types import MemoryListResult
from jiuwen_memory.retrieval.types import RetrievalResult, RetrievedItem


@dataclass(frozen=True)
class LocatedHit:
    """检索项及其确切目标范围；读取详情仍须逐条鉴权。"""

    scope: Scope
    item: RetrievedItem


@dataclass(frozen=True)
class PersonalOverview:
    """同一批可见记录的逐日写入量，以及有效记忆的来源分布。"""

    start_date: str
    daily_counts: list[int]
    source_counts: dict[str, int]


@dataclass(frozen=True)
class PersonalPage:
    items: list[MemoryUnit]
    count: int
    snapshot: str
    sources: tuple[str, ...]
    agents: tuple[tuple[str, str], ...] = ()
    projects: tuple[str, ...] = ()
    memory_classes: tuple[str, ...] = ()
    lifecycle_counts: dict[str, int] | None = None
    overview: PersonalOverview | None = None


@dataclass(frozen=True)
class PersonalSearchResult:
    items: list[LocatedHit]
    scores_comparable: bool


def _project(unit: MemoryUnit) -> str:
    """取 project：``system_metadata.project`` 标量优先，``coords.project`` 兜底。

    agent / 脚本路径把 project 作标量直传（API 会剥除瞬态的 ``coords``），
    router 路径回写 ``coords.project``；两路与存储侧 ``_project_of`` 同口径。
    """
    metadata = unit.system_metadata or {}
    project = metadata.get("project")
    if isinstance(project, str) and project.strip():
        return project.strip()
    coords = metadata.get("coords")
    value = coords.get("project") if isinstance(coords, dict) else None
    return value if isinstance(value, str) else ""


def memory_source(unit: MemoryUnit) -> str:
    source = unit.system_metadata.get("source_app")
    return source if isinstance(source, str) and source else ""


def _memory_class(unit: MemoryUnit) -> str:
    value = unit.system_metadata.get("memory_class")
    return value if isinstance(value, str) else ""


def scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def _summarize_overview(units: Sequence[MemoryUnit], zone: ZoneInfo) -> PersonalOverview:
    daily_counts = [0] * 365
    start = datetime.now(zone).date() - timedelta(days=len(daily_counts) - 1)
    sources: Counter[str] = Counter()
    for unit in units:
        if unit.temporal.t_ingest is not None:
            day = as_utc(unit.temporal.t_ingest).astimezone(zone).date()
            index = (day - start).days
            if 0 <= index < len(daily_counts):
                daily_counts[index] += 1
        if unit.lifecycle == LifecycleState.ACTIVE:
            sources[memory_source(unit)] += 1
    return PersonalOverview(start.isoformat(), daily_counts, dict(sources))


def list_personal(
    scopes: Sequence[Scope],
    read_page: Callable[[Scope, int, int], MemoryListResult],
    *,
    offset: int = 0,
    limit: int = 100,
    source: str | None = None,
    snapshot: str | None = None,
    max_units: int = 10000,
    lifecycle: str | None = "active",
    agent: str | None = None,
    project: str | None = None,
    query: str | None = None,
    tier: str | None = None,
    memory_class: str | list[str] | None = None,
    collective_destination: str | None = None,
    team_label: str | None = None,
    include_lifecycle_counts: bool = False,
    overview_timezone: str | None = None,
) -> PersonalPage:
    """聚合授权回调返回的条目；条目数超过上限或分页期间计数改变时拒绝。"""
    valid_offset = type(offset) is int and offset >= 0
    valid_limit = type(limit) is int and 1 <= limit <= 100
    if not valid_offset or not valid_limit:
        raise ValidationError("invalid personal list pagination")
    if source is not None and not isinstance(source, str):
        raise ValidationError("source must be a string")
    for name, value in (
        ("agent", agent), ("project", project), ("query", query),
        ("tier", tier), ("team_label", team_label),
    ):
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"{name} must be a string")
    class_values = [memory_class] if isinstance(memory_class, str) else memory_class
    valid_class_list = (
        isinstance(class_values, list)
        and bool(class_values)
        and all(isinstance(value, str) for value in class_values)
    )
    if class_values is not None and not valid_class_list:
        raise ValidationError("memory_class must be a string or a non-empty list of strings")
    if tier is not None and tier not in {item.value for item in MemoryTier}:
        raise ValidationError("invalid memory tier")
    if collective_destination is not None and collective_destination not in (
        "all", "project", "team", "group", "agent", "user_profile",
    ):
        raise ValidationError("invalid collective destination")
    if snapshot is not None and not isinstance(snapshot, str):
        raise ValidationError("snapshot must be a string")
    if offset and not snapshot:
        raise ValidationError("subsequent personal list pages require a snapshot")
    states = ("active", "superseded", "forgotten", "archived")
    if lifecycle is not None and lifecycle not in states:
        raise ValidationError("invalid lifecycle filter")
    if not isinstance(include_lifecycle_counts, bool):
        raise ValidationError("include_lifecycle_counts must be a boolean")
    zone = None
    if overview_timezone is not None:
        if not isinstance(overview_timezone, str) or len(overview_timezone) > 255:
            raise ValidationError("overview_timezone must be an IANA time zone name")
        try:
            zone = ZoneInfo(overview_timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValidationError("unknown overview_timezone") from exc
    ordered_scopes = {scope_key(scope): scope for scope in scopes}
    units: list[MemoryUnit] = []
    for key in sorted(ordered_scopes):
        scope = ordered_scopes[key]
        # 一次 list 取得同一 Scope 的内容与鉴权快照，避免在翻页期间拼接不同版本。
        page = read_page(scope, 0, max_units - len(units) + 1)
        if page.count < 0 or len(units) + page.count > max_units:
            raise UnsupportedCapabilityError("list_size", str(max_units), "personal")
        if len(page.items) != page.count:
            raise ConflictError(message="personal list could not obtain a complete scope snapshot")
        if any(scope_key(unit.scope) != key for unit in page.items):
            raise ValidationError("personal list item does not match its source scope")
        units.extend(page.items)
    units.sort(key=lambda unit: (scope_key(unit.scope), unit.id))
    identities = [(scope_key(unit.scope), unit.id) for unit in units]
    if len(set(identities)) != len(identities):
        raise ConflictError(message="personal list changed during pagination")
    sources = tuple(sorted({memory_source(unit) for unit in units}))
    agent_labels: dict[str, str] = {}
    for unit in units:
        label = unit.system_metadata.get("agent_label")
        if unit.scope.agent not in agent_labels or isinstance(label, str) and label:
            agent_labels[unit.scope.agent] = label if isinstance(label, str) else ""
    agents = tuple(sorted(agent_labels.items()))
    projects = tuple(sorted({_project(unit) for unit in units}))
    memory_classes = tuple(sorted({_memory_class(unit) for unit in units}))
    if source is not None:
        units = [unit for unit in units if memory_source(unit) == source]
    if agent is not None:
        units = [unit for unit in units if unit.scope.agent == agent]
    if project is not None:
        units = [unit for unit in units if _project(unit) == project]
    if tier is not None:
        tier_filter = FilterClause("tier", value=tier)
        units = [unit for unit in units if matches_memory_unit(unit, tier_filter)]
    if class_values is not None:
        selected_classes = set(class_values)
        units = [unit for unit in units if _memory_class(unit) in selected_classes]
    if collective_destination == "all":
        units = [unit for unit in units if collective_destination_of(unit.system_metadata)]
    elif collective_destination is not None:
        units = [
            unit for unit in units
            if collective_destination_of(unit.system_metadata) == collective_destination
        ]
    if team_label is not None:
        units = [
            unit for unit in units
            if (unit.system_metadata.get("team_label") or "") == team_label
        ]
    if query and query.strip():
        needle = query.strip().casefold()
        units = [
            unit for unit in units
            if needle in (unit.content + " " + " ".join(unit.tags)).casefold()
        ]
    lifecycle_counts = None
    if include_lifecycle_counts:
        lifecycle_counts = dict.fromkeys(states, 0)
        for unit in units:
            lifecycle_counts[unit.lifecycle.value] += 1
    overview = _summarize_overview(units, zone) if zone is not None else None
    if lifecycle is not None:
        state_filter = FilterClause("lifecycle", value=lifecycle)
        units = [unit for unit in units if matches_memory_unit(unit, state_filter)]
    # token 仅对列表的身份与来源作变更检测；具体内容读取仍使用同次授权结果。
    token_data = [(scope_key(unit.scope), unit.id, unit.system_metadata.get("source_app"))
                  for unit in units]
    token = hashlib.sha256(json.dumps(token_data, ensure_ascii=False).encode()).hexdigest()
    if snapshot is not None and snapshot != token:
        raise ConflictError(message="personal list changed; restart pagination")
    return PersonalPage(
        units[offset:offset + limit], len(units), token, sources, agents, projects, memory_classes,
        lifecycle_counts, overview,
    )


def search_personal(
    scopes: Sequence[Scope],
    search_scope: Callable[[Scope, int], RetrievalResult],
    authorize_hit: Callable[[Scope, str], MemoryUnit],
    *,
    top_k: int,
) -> list[LocatedHit]:
    """逐范围检索、复核真源权限，再按范围轮转取最多 ``top_k`` 条。"""
    if type(top_k) is not int or not 1 <= top_k <= 100:
        raise ValidationError("invalid personal search top_k")
    unique = {scope_key(scope): scope for scope in scopes}
    ranked: list[list[LocatedHit]] = []
    for key in sorted(unique):
        scope = unique[key]
        result = search_scope(scope, top_k)
        if result.errors:
            raise BackendError("personal search had a partial retrieval failure")
        bucket: list[LocatedHit] = []
        for item in result.items:
            if (
                isinstance(item.score, bool) or not isinstance(item.score, int | float)
                or not math.isfinite(item.score)
            ):
                raise ValidationError("personal search returned a non-finite score")
            unit = authorize_hit(scope, item.unit_id)
            if scope_key(unit.scope) != key or unit.id != item.unit_id:
                raise ValidationError("personal search result does not match its source")
            # 返回内容和元数据仅取刚完成授权的真源快照；披露器可以重塑文本，
            # 因此不能用检索结果的字符串与真源全文比较来判断权限。
            fresh = replace(
                item,
                abstract=unit.layers.l0,
                overview=unit.layers.l1,
                content=unit.content,
                system_metadata=dict(unit.system_metadata),
                user_metadata=dict(unit.user_metadata),
                parent_id=unit.hierarchy.parent_id,
            )
            bucket.append(LocatedHit(scope, fresh))
        # 分数只用于同一范围内部的排序。
        bucket.sort(key=lambda hit: (-hit.item.score, hit.item.unit_id))
        ranked.append(bucket)
    merged: list[LocatedHit] = []
    rank = 0
    while len(merged) < top_k:
        progressed = False
        for bucket in ranked:
            if rank >= len(bucket):
                continue
            merged.append(bucket[rank])
            progressed = True
            if len(merged) >= top_k:
                break
        if not progressed:
            break
        rank += 1
    return merged
