# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""外部 update 改写事实字段时清掉冲突巩固标记。"""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from jiuwen_memory.api import MemoryAPI, MemoryPatch, Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.security.types import RequestSecurityContext
from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, RawPayload
from jiuwen_memory.control.types import UpdateMode, UpdateSelector
from tests.unit.api.fixtures import security_for

_DONE_KEY = "consolidation_done_at"
_RUN_KEY = "consolidation_run_id"
_DONE_AT = "2026-01-01T00:00:00+00:00"
_RUN_ID = "run-1"


class _RebuildPatchPrivacy:
    """模拟 privacy ingress 重建 patch：只保留调用方 metadata 之外的空集合。

    update 的清标记若发生在 ingress 之前，注入键会被这里丢掉——用于锁定顺序契约。
    """

    @staticmethod
    def staging_scope():
        return nullcontext()

    @staticmethod
    def ingress(payload: RawPayload) -> RawPayload:
        return RawPayload(
            id=payload.id,
            scope=payload.scope,
            data=payload.data,
            system_metadata={},
            user_metadata=dict(payload.user_metadata),
        )

    @staticmethod
    def inherit_classification(protected, units) -> None:
        return None


def _kernel_for(tag: str) -> tuple[MemoryAPI, Scope, RequestSecurityContext]:
    scope = Scope(org=f"cc-{tag}", user="u1", agent="a1", session="s1")
    kernel = build_kernel()
    return kernel.api, scope, security_for(kernel.api, scope)


def _marked_unit(api: MemoryAPI, scope: Scope, security: RequestSecurityContext) -> MemoryUnit:
    """先写一条普通记忆，再经 update 打上巩固标记。"""
    unit = api.add("Jon is a banker", scope, security=security)[0]
    return api.update(
        unit.id,
        scope,
        MemoryPatch(
            system_metadata={_DONE_KEY: _DONE_AT, _RUN_KEY: _RUN_ID}
        ),
        security=security,
    )


def test_overwrite_with_content_clears_done_keeps_run() -> None:
    """OVERWRITE 原地改内容：done 清空，run_id 保留。"""
    api, scope, security = _kernel_for("overwrite")
    marked = _marked_unit(api, scope, security)
    assert marked.system_metadata[_DONE_KEY] == _DONE_AT

    updated = api.update(
        marked.id,
        scope,
        MemoryPatch(content="Jon is a teacher", mode=UpdateMode.OVERWRITE),
        security=security,
    )

    assert updated.id == marked.id
    assert updated.system_metadata[_DONE_KEY] == ""
    assert updated.system_metadata[_RUN_KEY] == _RUN_ID

    # 真源读回同样为空串。
    reloaded = api.get(updated.id, scope, security=security)
    assert reloaded.system_metadata[_DONE_KEY] == ""


def test_supersede_with_content_clears_done_on_new_version() -> None:
    """SUPERSEDE 产生新版本：新版本不带 settled 标记（run_id 保留）。"""
    api, scope, security = _kernel_for("supersede")
    marked = _marked_unit(api, scope, security)

    updated = api.update(
        marked.id, scope, MemoryPatch(content="Jon is a teacher"), security=security
    )

    assert updated.id != marked.id
    assert updated.supersedes == marked.id
    assert updated.system_metadata[_DONE_KEY] == ""
    assert updated.system_metadata[_RUN_KEY] == _RUN_ID


@pytest.mark.parametrize(
    "patch",
    [
        MemoryPatch(tags=["reviewed"], mode=UpdateMode.OVERWRITE),
        MemoryPatch(tier=MemoryTier.CORE, mode=UpdateMode.OVERWRITE),
        MemoryPatch(user_metadata={"reviewed": "yes"}, mode=UpdateMode.OVERWRITE),
    ],
    ids=["tags", "tier", "user_metadata"],
)
def test_non_factual_change_keeps_done(patch: MemoryPatch) -> None:
    """只改 tags / tier / user_metadata：不是事实字段变更，标记保持不变。"""
    api, scope, security = _kernel_for("non-factual")
    marked = _marked_unit(api, scope, security)

    updated = api.update(marked.id, scope, patch, security=security)

    assert updated.system_metadata[_DONE_KEY] == _DONE_AT
    assert updated.system_metadata[_RUN_KEY] == _RUN_ID


def test_batch_update_per_item_clears_done() -> None:
    """batch_update 逐条模式复用同一清理逻辑。"""
    api, scope, security = _kernel_for("batch-item")
    first = _marked_unit(api, scope, security)
    second = _marked_unit(api, scope, security)

    result = api.batch_update(
        UpdateSelector(unit_ids=[first.id, second.id], scope=scope),
        [MemoryPatch(content="Jon is a banker"), MemoryPatch(content="Jon is a banker too")],
        security=security,
    )

    assert len(result.outcomes) == 2
    for outcome in result.outcomes:
        updated = outcome.updated_unit
        assert updated.system_metadata[_DONE_KEY] == ""
        assert updated.system_metadata[_RUN_KEY] == _RUN_ID


def test_batch_update_unified_clears_done_for_every_candidate() -> None:
    """统一模式（单 patch 作用多个候选）：每个候选都清标记，patch 不被污染。"""
    api, scope, security = _kernel_for("batch-unified")
    first = _marked_unit(api, scope, security)
    second = _marked_unit(api, scope, security)

    result = api.batch_update(
        UpdateSelector(unit_ids=[first.id, second.id], scope=scope),
        [MemoryPatch(content="Jon is a banker")],
        security=security,
    )

    assert len(result.outcomes) == 2
    for outcome in result.outcomes:
        assert outcome.updated_unit.system_metadata[_DONE_KEY] == ""


def test_clear_marks_survives_privacy_patch_rebuild() -> None:
    """顺序契约：清标记必须在 privacy 重建 patch 之后，否则注入键会被覆盖掉。"""
    api, scope, security = _kernel_for("privacy")
    marked = _marked_unit(api, scope, security)
    api._privacy = _RebuildPatchPrivacy()  # pylint: disable=protected-access

    updated = api.update(
        marked.id,
        scope,
        MemoryPatch(content="Jon is a teacher", mode=UpdateMode.OVERWRITE),
        security=security,
    )

    assert updated.system_metadata[_DONE_KEY] == ""
