# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""batch_update 单元测试：统一 patch / 逐条 patch / 错误处理 / 校验。"""

from __future__ import annotations

import pytest

from jiuwen_memory.api import MemoryPatch, Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryTier
from jiuwen_memory.control.types import UpdateSelector
from tests.unit.api.fixtures import security_for

# -- helpers -----------------------------------------------------------------


def _make_scope() -> Scope:
    return Scope(org="acme", user="u1", agent="a1", session="s1")


def _add_units(kernel, scope, actor, count=3, tags=None):
    """Add ``count`` memory units and return the list of created units."""
    units = []
    for i in range(count):
        unit = kernel.api.add(
            f"content-{i}",
            scope,
            security=security_for(kernel.api, actor),
            tags=tags or [],
        )[0]
        units.append(unit)
    return units


# -- 1. Unified patch mode with unit_ids -------------------------------------


def test_batch_update_unified_patch_with_unit_ids() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=3)
    unit_ids = {u.id for u in units}

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=list(unit_ids), scope=scope),
        [MemoryPatch(tags=["updated"])],
        security=security_for(kernel.api, actor),
    )

    assert len(result.outcomes) == 3
    outcome_ids = set()
    for outcome in result.outcomes:
        assert outcome.error == ""
        assert outcome.error_type == ""
        assert outcome.updated_unit is not None
        assert "updated" in outcome.updated_unit.tags
        outcome_ids.add(outcome.unit_id)
    assert outcome_ids == unit_ids


# -- 2. Unified patch mode without unit_ids (scan by scope+tags) -------------


def test_batch_update_unified_patch_scan_by_tags() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    _add_units(kernel, scope, actor, count=2, tags=["batch"])
    other = kernel.api.add(
        "unrelated",
        scope,
        security=security_for(kernel.api, actor),
    )[0]

    result = kernel.api.batch_update(
        UpdateSelector(scope=scope, tags=["batch"]),
        [MemoryPatch(tags=["modified"])],
        security=security_for(kernel.api, actor),
    )

    assert len(result.outcomes) == 2
    for outcome in result.outcomes:
        assert outcome.error == ""
        assert outcome.updated_unit is not None
        assert "modified" in outcome.updated_unit.tags

    stored = kernel.api.get(other.id, scope, security=security_for(kernel.api, actor))
    assert "modified" not in stored.tags


# -- 3. Per-item patch mode (different patches per unit) --------------------


def test_batch_update_per_item_patches() -> None:
    """逐条模式 patch[i] ↔ selector.unit_ids[i] 严格配对，与存储扫描顺序无关。

    unit_ids 顺序故意与 units 插入顺序相反：若配对错用候选扫描下标，
    B 会拿到 patched-A、A 拿到 patched-B（内容静默写错条目）。
    """
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)
    reversed_ids = [units[1].id, units[0].id]

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=reversed_ids, scope=scope),
        [
            MemoryPatch(content="patched-B"),
            MemoryPatch(content="patched-A"),
        ],
        security=security_for(kernel.api, actor),
    )

    assert len(result.outcomes) == 2
    # outcomes 与 unit_ids 顺序一一对应，而非存储扫描顺序
    assert [outcome.unit_id for outcome in result.outcomes] == reversed_ids
    by_id = {outcome.unit_id: outcome for outcome in result.outcomes}
    assert by_id[units[1].id].updated_unit.segments[0].content == "patched-B"
    assert by_id[units[0].id].updated_unit.segments[0].content == "patched-A"


# -- 4. Error handling with continue_on_error -------------------------------
# Use per-item mode with a non-existent unit_id to trigger a failure outcome
# (Issue #6 fix: missing ids generate error outcomes). FORGOTTEN units are
# now correctly excluded from batch_update candidates (Issue #8 fix).


def test_batch_update_continue_on_error_true() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)
    unit_ids = [units[0].id, "no-such-id", units[1].id]

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=unit_ids, scope=scope),
        [
            MemoryPatch(tags=["retagged"]),
            MemoryPatch(tags=["missing"]),
            MemoryPatch(tags=["retagged"]),
        ],
        security=security_for(kernel.api, actor),
        continue_on_error=True,
    )

    assert len(result.outcomes) == 3
    errors = [outcome for outcome in result.outcomes if outcome.error]
    successes = [outcome for outcome in result.outcomes if not outcome.error]
    assert len(errors) == 1
    assert errors[0].unit_id == "no-such-id"
    assert len(successes) == 2
    for outcome in successes:
        assert outcome.updated_unit is not None


def test_batch_update_continue_on_error_false_skips_remaining() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)
    unit_ids = [units[0].id, "no-such-id", units[1].id]

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=unit_ids, scope=scope),
        [
            MemoryPatch(tags=["retagged"]),
            MemoryPatch(tags=["missing"]),
            MemoryPatch(tags=["retagged"]),
        ],
        security=security_for(kernel.api, actor),
        continue_on_error=False,
    )

    assert len(result.outcomes) == 3
    error_indices = [idx for idx, outcome in enumerate(result.outcomes) if outcome.error]
    assert len(error_indices) >= 1
    first_error_idx = error_indices[0]
    for i in range(first_error_idx + 1, len(result.outcomes)):
        assert result.outcomes[i].error_type == "Skipped"
    assert result.outcomes[first_error_idx].error_type != "Skipped"
    assert result.outcomes[first_error_idx].unit_id == "no-such-id"


# -- 5. Validation errors ----------------------------------------------------


def test_batch_update_empty_selector_raises() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()

    with pytest.raises(ValidationError):
        kernel.api.batch_update(
            UpdateSelector(),
            [MemoryPatch(tags=["x"])],
            security=security_for(kernel.api, actor),
        )


def test_batch_update_empty_patches_raises() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=1)

    with pytest.raises(ValidationError):
        kernel.api.batch_update(
            UpdateSelector(unit_ids=[units[0].id], scope=scope),
            [],
            security=security_for(kernel.api, actor),
        )


def test_batch_update_per_item_count_mismatch_raises() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=3)

    with pytest.raises(ValidationError, match="must equal"):
        kernel.api.batch_update(
            UpdateSelector(unit_ids=[u.id for u in units], scope=scope),
            [MemoryPatch(tags=["a"]), MemoryPatch(tags=["b"])],
            security=security_for(kernel.api, actor),
        )


def test_batch_update_per_item_without_unit_ids_raises() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()

    with pytest.raises(ValidationError, match="per-item"):
        kernel.api.batch_update(
            UpdateSelector(scope=scope, tags=["t"]),
            [MemoryPatch(tags=["a"]), MemoryPatch(tags=["b"])],
            security=security_for(kernel.api, actor),
        )


def test_batch_update_selector_limit_must_be_positive() -> None:
    """selector.limit 与 API 参数 limit 同为显式契约：0/负数拒绝而非静默忽略。

    静默忽略会把「更新全部候选」当宽放行为，调用方拿不到任何信号。
    """
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)

    for bad_limit in (0, -1):
        with pytest.raises(ValidationError, match="positive"):
            kernel.api.batch_update(
                UpdateSelector(
                    unit_ids=[u.id for u in units], scope=scope, limit=bad_limit
                ),
                [MemoryPatch(tags=["x"])],
                security=security_for(kernel.api, actor),
            )


def test_batch_update_per_item_limit_below_patch_count_raises() -> None:
    """逐条模式 limit 只做下界承诺（不小于补丁数），不做候选截断。

    截断会让 unit_ids 后段查不到 candidate_map 被误判 NotFoundError。
    """
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)

    with pytest.raises(ValidationError, match=r"len\(patches\)"):
        kernel.api.batch_update(
            UpdateSelector(unit_ids=[u.id for u in units], scope=scope, limit=1),
            [MemoryPatch(tags=["a"]), MemoryPatch(tags=["b"])],
            security=security_for(kernel.api, actor),
        )


# -- 6. Kernel metadata key rejection ---------------------------------------


def test_batch_update_rejects_kernel_system_metadata() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=1)

    with pytest.raises(ValidationError, match="内核保留"):
        kernel.api.batch_update(
            UpdateSelector(unit_ids=[units[0].id], scope=scope),
            [MemoryPatch(system_metadata={"unit_id": "evil"})],
            security=security_for(kernel.api, actor),
        )


# -- 7. Engine-level direct test (command service delegation) ---------------


def test_batch_update_unified_patch_tier() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)
    unit_ids = [u.id for u in units]

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=unit_ids, scope=scope),
        [MemoryPatch(tier=MemoryTier.PROCEDURAL)],
        security=security_for(kernel.api, actor),
    )

    assert len(result.outcomes) == 2
    for outcome in result.outcomes:
        assert outcome.error == ""
        assert outcome.updated_unit is not None
        assert outcome.updated_unit.tier == MemoryTier.PROCEDURAL
