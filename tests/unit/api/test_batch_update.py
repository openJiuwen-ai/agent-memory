# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""batch_update 单元测试：统一 patch / 逐条 patch / 错误处理 / 校验。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwen_memory.api import DeleteSelector, MemoryPatch, Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryTier
from jiuwen_memory.control.application import MemoryCommandService
from jiuwen_memory.control.types import (
    BatchUpdateOutcome,
    BatchUpdateResult,
    UpdateSelector,
)
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
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=2)
    unit_ids = [u.id for u in units]

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=unit_ids, scope=scope),
        [
            MemoryPatch(content="patched-A"),
            MemoryPatch(content="patched-B"),
        ],
        security=security_for(kernel.api, actor),
    )

    assert len(result.outcomes) == 2
    contents = {outcome.updated_unit.segments[0].content for outcome in result.outcomes}
    assert contents == {"patched-A", "patched-B"}


# -- 4. Error handling with continue_on_error -------------------------------
# Delete one unit with FORGET (lifecycle → FORGOTTEN). The unit remains in KV
# so it still appears as a candidate, but update(SUPERSEDE) fails because
# the lifecycle manager rejects FORGOTTEN → SUPERSEDED.


def test_batch_update_continue_on_error_true() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=3)
    forgotten_id = units[1].id
    kernel.api.delete(
        DeleteSelector(unit_ids=[forgotten_id], scope=scope),
        security=security_for(kernel.api, actor),
    )

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=[u.id for u in units], scope=scope),
        [MemoryPatch(tags=["retagged"])],
        security=security_for(kernel.api, actor),
        continue_on_error=True,
    )

    assert len(result.outcomes) == 3
    errors = [o for o in result.outcomes if o.error]
    successes = [o for o in result.outcomes if not o.error]
    assert len(errors) == 1
    assert errors[0].unit_id == forgotten_id
    assert len(successes) == 2
    for o in successes:
        assert o.updated_unit is not None


def test_batch_update_continue_on_error_false_skips_remaining() -> None:
    scope = _make_scope()
    actor = scope
    kernel = build_kernel()
    units = _add_units(kernel, scope, actor, count=3)
    forgotten_id = units[1].id
    kernel.api.delete(
        DeleteSelector(unit_ids=[forgotten_id], scope=scope),
        security=security_for(kernel.api, actor),
    )

    result = kernel.api.batch_update(
        UpdateSelector(unit_ids=[u.id for u in units], scope=scope),
        [MemoryPatch(tags=["retagged"])],
        security=security_for(kernel.api, actor),
        continue_on_error=False,
    )

    assert len(result.outcomes) == 3
    # At least one error, and all outcomes after the first error are skipped.
    error_indices = [i for i, o in enumerate(result.outcomes) if o.error]
    assert len(error_indices) >= 1
    first_error_idx = error_indices[0]
    # All outcomes after the first error should be skipped.
    for i in range(first_error_idx + 1, len(result.outcomes)):
        assert result.outcomes[i].error_type == "Skipped"
    # The first error itself should be a real error (not Skipped).
    assert result.outcomes[first_error_idx].error_type != "Skipped"
    assert result.outcomes[first_error_idx].unit_id == forgotten_id


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


def test_command_batch_update_delegates_to_engine() -> None:
    engine = AsyncMock()
    selector = UpdateSelector(unit_ids=["u1"], scope=_make_scope())
    patches = [MemoryPatch(content="patched")]
    expected = BatchUpdateResult(
        outcomes=[BatchUpdateOutcome(index=0, unit_id="u1")]
    )
    engine.batch_update.return_value = expected
    service = MemoryCommandService(engine)

    result = asyncio.run(
        service.batch_update(selector, patches, continue_on_error=False)
    )

    assert result is expected
    engine.batch_update.assert_awaited_once_with(
        selector, patches, continue_on_error=False
    )


# -- 8. Unified patch updates tier on multiple units -------------------------


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
