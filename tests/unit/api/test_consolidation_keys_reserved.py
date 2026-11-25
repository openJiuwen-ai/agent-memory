# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""调用方不可经 add / update / batch_update 写入内核保留的系统元数据键。

参数化遍历 `KERNEL_SYSTEM_METADATA_KEYS`，新增保留键时自动纳入覆盖。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api import MemoryAPI, MemoryPatch, Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    CONSOLIDATION_DONE_AT_KEY,
    CONSOLIDATION_RUN_ID_KEY,
    KERNEL_SYSTEM_METADATA_KEYS,
)
from jiuwen_memory.control.types import UpdateMode, UpdateSelector
from tests.unit.api.fixtures import security_for

_RESERVED_KEYS = sorted(KERNEL_SYSTEM_METADATA_KEYS)


@pytest.fixture(scope="module")
def api() -> MemoryAPI:
    return build_kernel().api


def _scope_for(*parts: str) -> Scope:
    slug = "-".join(part.lower().replace("_", "-") for part in parts)
    return Scope(org=f"reserved-{slug}", user="u1", agent="a1", session="s1")


def test_consolidation_markers_are_reserved() -> None:
    assert CONSOLIDATION_RUN_ID_KEY in KERNEL_SYSTEM_METADATA_KEYS
    assert CONSOLIDATION_DONE_AT_KEY in KERNEL_SYSTEM_METADATA_KEYS


@pytest.mark.parametrize("key", _RESERVED_KEYS)
def test_add_rejects_reserved_key(api: MemoryAPI, key: str) -> None:
    scope = _scope_for("add", key)
    security = security_for(api, scope)

    with pytest.raises(ValidationError):
        api.add("Jon is a banker", scope, security=security, system_metadata={key: "x"})


@pytest.mark.parametrize("mode", [UpdateMode.OVERWRITE, UpdateMode.SUPERSEDE])
@pytest.mark.parametrize("key", _RESERVED_KEYS)
def test_update_rejects_reserved_key(api: MemoryAPI, key: str, mode: UpdateMode) -> None:
    scope = _scope_for("update", key, mode.value)
    security = security_for(api, scope)
    unit = api.add("Jon is a banker", scope, security=security)[0]

    with pytest.raises(ValidationError):
        api.update(
            unit.id,
            scope,
            MemoryPatch(system_metadata={key: "x"}, mode=mode),
            security=security,
        )


@pytest.mark.parametrize("unified", [True, False], ids=["unified", "per-item"])
@pytest.mark.parametrize("key", _RESERVED_KEYS)
def test_batch_update_rejects_reserved_key(
    api: MemoryAPI, key: str, unified: bool
) -> None:
    scope = _scope_for("batch", key, "unified" if unified else "per-item")
    security = security_for(api, scope)
    first = api.add("Jon is a banker", scope, security=security)[0]
    second = api.add("Jon is a banker too", scope, security=security)[0]
    patches = (
        [MemoryPatch(system_metadata={key: "x"})]
        if unified
        else [MemoryPatch(system_metadata={key: "x"}) for _ in range(2)]
    )

    with pytest.raises(ValidationError):
        api.batch_update(
            UpdateSelector(unit_ids=[first.id, second.id], scope=scope),
            patches,
            security=security,
        )
