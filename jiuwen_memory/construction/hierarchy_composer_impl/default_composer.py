# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME 两层树：只在副本上生成候选，完整校验后按可恢复顺序写入。

持久化依次写新父本体、切换叶边、归档并断开旧父边、软删除旧父索引、刷新新父与叶索引。
本体阶段失败立即停止，索引阶段逐项报告失败；不承诺事务回滚，也不自动修复。
所有输入由调用方提供，本实现不读存储，因此只能验证已提供的旧父及其全部子节点。
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    MemoryUnit,
    Scope,
    validate_ref,
    validate_tree,
)
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeProfile,
    HierarchyComposer,
    HierarchyComposeRequest,
    HierarchyComposeResult,
    HierarchyComposerProducer,
    HierarchyRepair,
)
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

from .profile_config import build_profiles
from .time_pipeline import TimeSpanMergerOptions, run_time_pipeline

logger = get_logger(__name__)
ScopeKey = tuple[str, str, str, str, str]
NodeKey = tuple[ScopeKey, str]


@dataclass
class DefaultHierarchyComposer(HierarchyComposer):
    """不依赖模型的 snapshot → time_span 构建器，不扫描或补齐数据库节点。"""

    index_builder: IndexBuilder
    profiles: dict[HierarchyKind, HierarchyComposeProfile] = field(default_factory=dict)
    allow_cross_user: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allow_cross_user, bool):
            raise ValidationError("allow_cross_user 必须是 bool")
        if not isinstance(self.profiles, dict):
            raise ValidationError("profiles 必须是 kind → HierarchyComposeProfile 的映射")
        raw_profiles = {}
        for profile_kind, profile in self.profiles.items():
            if not isinstance(profile, HierarchyComposeProfile) or profile.kind != profile_kind:
                raise ValidationError("profile 类型或 kind 与注册键不一致")
            raw_profiles[profile_kind] = {
                "leaf_role": profile.leaf_role,
                "parent_roles": profile.parent_roles,
                "stage_options": profile.stage_options,
            }
        self.profiles = build_profiles(raw_profiles)

    @staticmethod
    def operator_type() -> OperatorType:
        """声明树结构构建算子类型。"""
        return OperatorType.HIERARCHY_COMPOSER

    @staticmethod
    def health() -> None:
        """本实现无外部连接需要探测。"""
        return None

    def build(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        """首次建树，已有父引用或携带旧父时拒绝整个请求。"""
        self._validate_request(request)
        if request.existing_parents or any(leaf.hierarchy.parent_id for leaf in request.leaves):
            raise ValidationError("build 不得覆盖已有父关系，请使用 replace_in_span")
        _validate_leaf_spans(request)
        return self._compose(request)

    def replace_in_span(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        """替换有界区间内的父层，要求所有相交旧父及其直接子节点由调用方备齐。"""
        self._validate_request(request)
        if request.options.span_start is None or request.options.span_end is None:
            raise ValidationError("replace_in_span 要求成对且有界的 span")
        _validate_replacement(request)
        return self._compose(request)

    def _validate_request(self, request: HierarchyComposeRequest) -> None:
        if not isinstance(request, HierarchyComposeRequest):
            raise ValidationError("request 必须是 HierarchyComposeRequest")
        _validate_options(request.options)
        if not isinstance(request.leaves, list) or not request.leaves:
            raise ValidationError("leaves 必须是非空 MemoryUnit 列表")
        if not isinstance(request.existing_parents, list):
            raise ValidationError("existing_parents 必须是 MemoryUnit 列表")
        for leaf in request.leaves:
            _validate_unit(leaf, HierarchyRole.SNAPSHOT)
            if leaf.hierarchy.child_ids:
                raise ValidationError(f"snapshot 不能包含子节点：{leaf.id}")
        for old_parent in request.existing_parents:
            _validate_unit(old_parent, HierarchyRole.TIME_SPAN)
            if old_parent.scope != request.options.tree_home_scope:
                raise ValidationError(f"旧父不在 tree_home_scope：{old_parent.id}")
            if old_parent.hierarchy.parent_id or not old_parent.hierarchy.child_ids:
                raise ValidationError(f"旧 time_span 必须是有直接子的根：{old_parent.id}")
        validate_tree(
            [*request.leaves, *request.existing_parents],
            allow_cross_user=self.allow_cross_user,
        )

    def _compose(self, request: HierarchyComposeRequest) -> HierarchyComposeResult:
        candidate = deepcopy(request)
        profile = self.profiles.get(HierarchyKind.TIME)
        stage_values = profile.stage_options.get("TimeSpanMerger", {}) if profile else {}
        parents, children = run_time_pipeline(
            candidate.leaves,
            tree_home_scope=candidate.options.tree_home_scope,
            options=TimeSpanMergerOptions.from_stage_options(stage_values),
            extra_metadata=candidate.options.metadata,
        )
        validate_tree([*parents, *children], allow_cross_user=self.allow_cross_user)
        previous_keys = {_node_key(unit) for unit in candidate.existing_parents}
        if previous_keys.intersection(_node_key(unit) for unit in parents):
            raise ValidationError("新父 id 不得复用旧父 id")
        return self._persist(parents, children, candidate.existing_parents)

    def _persist(
        self,
        parents: list[MemoryUnit],
        children: list[MemoryUnit],
        old_parents: list[MemoryUnit],
    ) -> HierarchyComposeResult:
        result = HierarchyComposeResult()
        for parent_group in _by_scope(parents):
            if not _attempt(
                lambda: self.index_builder.build(parent_group, mode=IndexWriteMode.FORWARD_ONLY),
                result, parent_group, "parent_write_failed",
            ):
                return result
            result.created_parent_ids.extend(unit.id for unit in parent_group)
        for child_group in _by_scope(children):
            if not _attempt(
                lambda: self.index_builder.update(child_group, mode=IndexWriteMode.FORWARD_ONLY),
                result, child_group, "child_edge_write_failed",
            ):
                return result
            result.updated_child_ids.extend(unit.id for unit in child_group)
        for previous_parent in old_parents:
            previous_parent.lifecycle = LifecycleState.ARCHIVED
            previous_parent.hierarchy.parent_id = ""
            previous_parent.hierarchy.parent_scope = None
            previous_parent.hierarchy.child_ids = []
            previous_parent.hierarchy.child_scopes = []
        for old_group in _by_scope(old_parents):
            if not _attempt(
                lambda: self.index_builder.update(old_group, mode=IndexWriteMode.FORWARD_ONLY),
                result, old_group, "old_parent_retire_failed",
            ):
                return result
            result.replaced_parent_ids.extend(unit.id for unit in old_group)
        if old_parents:
            _attempt(
                lambda: self.index_builder.remove(old_parents, mode=IndexRemoveMode.SOFT),
                result, old_parents, "index_remove_failed",
            )
        _attempt(
            lambda: self.index_builder.build(parents, mode=IndexWriteMode.RETRIEVAL_ONLY),
            result, parents, "index_build_failed",
        )
        _attempt(
            lambda: self.index_builder.update(children, mode=IndexWriteMode.RETRIEVAL_ONLY),
            result, children, "index_update_failed",
        )
        return result


def _validate_options(options: HierarchyComposeOptions) -> None:
    if not isinstance(options, HierarchyComposeOptions):
        raise ValidationError("options 必须是 HierarchyComposeOptions")
    if options.kind is not HierarchyKind.TIME or options.leaf_role is not HierarchyRole.SNAPSHOT:
        raise ValidationError("当前只支持 TIME 的 snapshot → time_span")
    if options.parent_roles != [HierarchyRole.TIME_SPAN]:
        raise ValidationError("parent_roles 当前只支持 [TIME_SPAN]")
    _validate_scope(options.tree_home_scope)
    _validate_span(options.span_start, options.span_end)
    if not isinstance(options.metadata, dict):
        raise ValidationError("metadata 必须是 dict[str, str]")
    for metadata_key, metadata_value in options.metadata.items():
        if not isinstance(metadata_key, str) or not isinstance(metadata_value, str):
            raise ValidationError("metadata 必须是 dict[str, str]")


def _validate_unit(unit: MemoryUnit, expected_role: HierarchyRole) -> None:
    if not isinstance(unit, MemoryUnit) or not isinstance(unit.id, str) or not unit.id:
        raise ValidationError("节点必须是具有非空字符串 id 的 MemoryUnit")
    _validate_scope(unit.scope)
    ref = unit.hierarchy
    if not isinstance(ref, HierarchyRef):
        raise ValidationError(f"节点缺少 HierarchyRef：{unit.id}")
    if ref.kind is not HierarchyKind.TIME or ref.role is not expected_role:
        raise ValidationError(f"节点必须是 TIME/{expected_role.value}：{unit.id}")
    if unit.lifecycle is not LifecycleState.ACTIVE or ref.status is not HierarchyStatus.ACTIVE:
        raise ValidationError(f"节点必须处于生命周期及结构 ACTIVE 状态：{unit.id}")
    if not isinstance(ref.parent_id, str) or not isinstance(ref.child_ids, list):
        raise ValidationError(f"父子引用类型非法：{unit.id}")
    if any(not isinstance(child_id, str) or not child_id for child_id in ref.child_ids):
        raise ValidationError(f"child_ids 必须是非空字符串列表：{unit.id}")
    if not isinstance(ref.child_scopes, list):
        raise ValidationError(f"child_scopes 必须是 Scope 列表：{unit.id}")
    for referenced_scope in ref.child_scopes:
        _validate_scope(referenced_scope)
    if ref.parent_scope is not None:
        _validate_scope(ref.parent_scope)
        if not ref.parent_id:
            raise ValidationError(f"parent_scope 不得脱离 parent_id：{unit.id}")
    _validate_span(ref.span_start, ref.span_end)
    validate_ref(ref, unit_id=unit.id)


def _validate_scope(scope: Scope) -> None:
    if not isinstance(scope, Scope):
        raise ValidationError("scope 必须是 Scope")
    if any(not isinstance(value, str) for value in _scope_key(scope)):
        raise ValidationError("Scope 的五维字段必须是字符串")


def _validate_span(start: datetime | None, end: datetime | None) -> None:
    if (start is None) != (end is None):
        raise ValidationError("span 必须成对出现")
    if start is None:
        return
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise ValidationError("span 必须使用 datetime")
    if _as_utc(start) > _as_utc(end):
        raise ValidationError("span_start 不得晚于 span_end")


def _validate_leaf_spans(request: HierarchyComposeRequest) -> None:
    if request.options.span_start is None:
        return
    for leaf in request.leaves:
        if not leaf.hierarchy.parent_id and not _intersects(leaf, request.options):
            raise ValidationError(f"未挂接叶必须与请求区间相交：{leaf.id}")


def _validate_replacement(request: HierarchyComposeRequest) -> None:
    leaf_keys = {_node_key(unit) for unit in request.leaves}
    parent_keys = {_node_key(unit) for unit in request.existing_parents}
    for old_parent in request.existing_parents:
        if not _intersects(old_parent, request.options):
            raise ValidationError(f"旧父必须与请求区间相交：{old_parent.id}")
        for child_position, child_identifier in enumerate(old_parent.hierarchy.child_ids):
            child_scope = old_parent.hierarchy.child_scope_at(child_position, old_parent.scope)
            if (_scope_key(child_scope), child_identifier) not in leaf_keys:
                raise ValidationError(f"替换必须提供旧父的全部直接子叶：{old_parent.id}")
    for leaf in request.leaves:
        if not leaf.hierarchy.parent_id:
            continue
        parent_scope = leaf.hierarchy.resolved_parent_scope(leaf.scope)
        if (_scope_key(parent_scope), leaf.hierarchy.parent_id) not in parent_keys:
            raise ValidationError(f"叶指向未提供的旧父：{leaf.id}")
    _validate_leaf_spans(request)


def _intersects(unit: MemoryUnit, options: HierarchyComposeOptions) -> bool:
    return (
        _as_utc(unit.hierarchy.span_start) <= _as_utc(options.span_end)
        and _as_utc(unit.hierarchy.span_end) >= _as_utc(options.span_start)
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _scope_key(scope: Scope) -> ScopeKey:
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def _node_key(unit: MemoryUnit) -> NodeKey:
    return _scope_key(unit.scope), unit.id


def _by_scope(units: list[MemoryUnit]) -> list[list[MemoryUnit]]:
    grouped: dict[ScopeKey, list[MemoryUnit]] = {}
    for unit in units:
        grouped.setdefault(_scope_key(unit.scope), []).append(unit)
    return [grouped[group_key] for group_key in sorted(grouped)]


def _attempt(
    action: Callable[[], None],
    result: HierarchyComposeResult,
    units: list[MemoryUnit],
    issue: str,
) -> bool:
    try:
        action()
        return True
    except Exception as exc:
        logger.warning("HierarchyComposer %s: %s", issue, exc)
        result.complete = False
        result.repair_required.extend(
            HierarchyRepair(
                unit_id=unit.id,
                issue=issue,
                expected_parent_id=unit.hierarchy.parent_id,
            )
            for unit in units
        )
        return False


@HierarchyComposerProducer.register("default")
def _build(config):
    return DefaultHierarchyComposer(
        index_builder=IndexBuilderProducer.dep(config, "index_builder"),
        profiles=build_profiles(config.get("hierarchy_profiles")),
        allow_cross_user=config.get("allow_cross_user", False),
    )
