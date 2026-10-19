# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""同步抽取的 TIME 叶信息传递；不创建额外记忆、不复制结构边。"""

from __future__ import annotations

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    DedupDecision,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    MemoryUnit,
)
from jiuwen_memory.common.type_def.hierarchy import validate_ref


def _is_snapshot(unit: MemoryUnit) -> bool:
    return (
        unit.hierarchy.kind is HierarchyKind.TIME
        and unit.hierarchy.role is HierarchyRole.SNAPSHOT
    )


def _has_edges(ref: HierarchyRef) -> bool:
    return bool(ref.parent_id or ref.parent_scope is not None or ref.child_ids or ref.child_scopes)


def _leaf_copy(unit: MemoryUnit) -> HierarchyRef:
    validate_ref(unit.hierarchy, unit_id=unit.id)
    if unit.hierarchy.status is not HierarchyStatus.ACTIVE:
        raise ValidationError("snapshot 传递只接受 ACTIVE 叶提示")
    return HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        span_start=unit.hierarchy.span_start,
        span_end=unit.hierarchy.span_end,
    )


def inherit_infer_snapshots(sources: list[MemoryUnit], candidates: list[MemoryUnit]) -> None:
    """仅将明确 infer=true 来源的叶安全字段传给对应候选。

    source_ref 优先；没有可解析的直接引用时使用唯一 provenance 来源。多源不能猜测
    最新来源或合并区间。匹配在 Router 前完成，用完整 Scope 防止同 id 串线。
    Extractor 本身仍生成空结构，普通/后台 EXTRACT 不因此改变行为。
    """
    opted_in = []
    for source in sources:
        if not _is_snapshot(source):
            continue
        if str(source.system_metadata.get("infer", "")).strip().lower() == "true":
            opted_in.append(source)
    if not opted_in:
        return
    pending: list[tuple[MemoryUnit, HierarchyRef]] = []
    for candidate in candidates:
        scoped = [source for source in sources if source.scope == candidate.scope]
        matched = [source for source in scoped if source.id == candidate.source_ref]
        if not matched:
            matched = [source for source in scoped if source.id in candidate.provenance]
        if len(matched) != 1:
            raise ValidationError("infer snapshot 候选必须有唯一且同 Scope 的来源引用")
        source = matched[0]
        if source in opted_in:
            pending.append((candidate, _leaf_copy(source)))
    for candidate, snapshot in pending:
        candidate.hierarchy = snapshot


def snapshot_for_decision(
    candidate: MemoryUnit,
    decision: DedupDecision,
    existing: MemoryUnit | None,
) -> HierarchyRef | None:
    """在正文合并/持久化前校验，返回 UPDATE 应替换的独立叶引用。

    NOOP 完全不动；缺少新提示时不清除旧叶。已挂树节点的内容/生命周期修改需要
    结构维护，本次不实现，因此 UPDATE/SUPERSEDE 在任何记忆修改前明确拒绝。
    """
    if decision is DedupDecision.NOOP:
        return None
    incoming = _is_snapshot(candidate)
    replacement = None
    if incoming:
        if _has_edges(candidate.hierarchy):
            raise ValidationError("新 snapshot 候选不得携带父子边")
        replacement = _leaf_copy(candidate)
    if existing is None or decision not in (DedupDecision.UPDATE, DedupDecision.SUPERSEDE):
        return replacement
    if not incoming and not _is_snapshot(existing):
        return None
    if _has_edges(existing.hierarchy):
        raise ValidationError(
            "已挂树节点的 UPDATE/SUPERSEDE 需要结构维护，本次仅支持未挂树 snapshot"
        )
    if not existing.hierarchy.is_empty and not _is_snapshot(existing):
        raise ValidationError("snapshot 去重不得覆盖其他树角色")
    validate_ref(existing.hierarchy, unit_id=existing.id)
    return replacement
