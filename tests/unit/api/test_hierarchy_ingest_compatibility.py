# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开 add 的叶提示直写链路：本地和云端都使用真实 SimpleIngestor 与 KV。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, HierarchyStatus, Scope
from jiuwen_memory.control.types import MemoryPatch

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="hierarchy-org", user="alice", agent="assistant", session="conversation")
_SECURITY = legacy_request_context(Scope(org="hierarchy-org", user="alice", agent="assistant"))
_MOMENT = "2026-08-18T09:30:00+00:00"
_HINTS = {
    "hierarchy_kind": "time",
    "hierarchy_role": "snapshot",
    "hierarchy_span_start": _MOMENT,
    "hierarchy_span_end": _MOMENT,
}
_KERNEL_PROJECTION_KEYS = ("hierarchy_status", "parent_id", "span_start", "span_end")


@pytest.fixture(name="builder_kind", params=["default", "unified"])
def builder_kind_fixture(request):
    return request.param


@pytest.fixture(name="api", params=["in_memory", "cloud"], ids=["local", "cloud"])
def hierarchy_api_fixture(request, builder_kind):
    components = ("ingestor", "index_builder", "retriever", "scheduler", "evolver", "lifecycle")
    config = {
        "engine": {
            "default": {
                "target": request.param,
                "params": {name: "default" for name in components},
            }
        },
        "security": {"default": {"target": "local", "params": {"key_hex": "0" * 64}}},
    }
    if builder_kind == "unified":
        config["constructor"] = {
            "default": {"target": "unified", "params": {"vector_enabled": False}}
        }
    runtime = assemble_runtime(config=config)
    try:
        yield runtime.api
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "infer_metadata", [{}, {"infer": False}], ids=["default", "explicit-false"]
)
def test_add_persists_one_unattached_leaf_without_enabling_infer(
    api, infer_metadata, builder_kind,
) -> None:
    metadata = {**_HINTS, "app": "chat", **infer_metadata}
    original = dict(metadata)
    user_metadata = {"hierarchy_kind": "not-interpreted", "project": "alpha"}

    written = api.add(
        "authoritative leaf evidence", _SCOPE, security=_SECURITY,
        system_metadata=metadata, user_metadata=user_metadata,
    )
    stored = api.get(written[0].id, _SCOPE, security=_SECURITY)
    listed = api.list(_SCOPE, security=_SECURITY)

    assert len(written) == 1 and listed.count == 1, "直写只落一条叶，不创建父节点"
    assert stored is not written[0], "通过 get 从 KV 反序列化读取，不能只检查返回的内存对象"
    assert stored.hierarchy == written[0].hierarchy
    assert stored.hierarchy.kind is HierarchyKind.TIME
    assert stored.hierarchy.role is HierarchyRole.SNAPSHOT
    assert stored.hierarchy.status is HierarchyStatus.ACTIVE
    assert stored.hierarchy.span_start == datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc)
    assert stored.hierarchy.span_end == stored.hierarchy.span_start
    assert stored.hierarchy.parent_id == "" and stored.hierarchy.child_ids == []
    assert stored.hierarchy.parent_scope is None and stored.hierarchy.child_scopes == []
    assert stored.content == "authoritative leaf evidence" and stored.provenance == []
    assert "hierarchy_span_start" not in stored.system_metadata
    assert "hierarchy_span_end" not in stored.system_metadata
    expected_projection = {
        "hierarchy_kind": stored.hierarchy.kind.value,
        "hierarchy_role": stored.hierarchy.role.value,
        "hierarchy_status": stored.hierarchy.status.value,
        "parent_id": stored.hierarchy.parent_id,
        "span_start": int(stored.hierarchy.span_start.timestamp() * 1000),
        "span_end": int(stored.hierarchy.span_end.timestamp() * 1000),
    }
    projection = {
        key: stored.system_metadata[key]
        for key in expected_projection
        if key in stored.system_metadata
    }
    assert projection == (expected_projection if builder_kind == "unified" else {})
    assert stored.system_metadata["app"] == "chat"
    assert stored.system_metadata.get("infer", False) is False
    assert stored.system_metadata["author_principal"] == "user:alice"
    assert stored.system_metadata["author_agent"] == "assistant"
    assert stored.user_metadata == user_metadata
    assert metadata == original


def test_add_ignores_hierarchy_hints_in_user_metadata(api) -> None:
    user_metadata = {**_HINTS, "hierarchy_parent_id": "not-an-edge", "infer": True}

    unit = api.add(
        "ordinary evidence", _SCOPE, security=_SECURITY,
        system_metadata={"app": "chat"}, user_metadata=user_metadata,
    )[0]
    stored = api.get(unit.id, _SCOPE, security=_SECURITY)

    assert stored.hierarchy.is_empty and stored.provenance == []
    assert stored.user_metadata == user_metadata
    assert api.list(_SCOPE, security=_SECURITY).count == 1


@pytest.mark.parametrize(
    "invalid", [{"hierarchy_role": "scene"}, {"hierarchy_parent_id": "fake-parent"}]
)
def test_invalid_system_hints_fail_before_any_memory_is_written(api, invalid) -> None:
    with pytest.raises(ValidationError):
        api.add(
            "invalid evidence", _SCOPE, security=_SECURITY,
            system_metadata={**_HINTS, **invalid},
        )

    assert api.list(_SCOPE, security=_SECURITY).count == 0


@pytest.mark.parametrize("key", ["author_principal", "author_agent", "memory_class"])
def test_leaf_hints_do_not_relax_existing_reserved_metadata_keys(api, key) -> None:
    with pytest.raises(ValidationError, match="内核保留 key"):
        api.add(
            "forged evidence", _SCOPE, security=_SECURITY,
            system_metadata={**_HINTS, key: "forged"},
        )

    assert api.list(_SCOPE, security=_SECURITY).count == 0


@pytest.mark.parametrize("key", _KERNEL_PROJECTION_KEYS)
def test_add_rejects_kernel_projection_keys_without_leaf_hints(api, key) -> None:
    with pytest.raises(ValidationError, match="内核保留 key"):
        api.add(
            "ordinary evidence", _SCOPE, security=_SECURITY,
            system_metadata={key: "forged"},
        )

    assert api.list(_SCOPE, security=_SECURITY).count == 0


@pytest.mark.parametrize("key", _KERNEL_PROJECTION_KEYS)
def test_update_rejects_kernel_projection_keys_without_leaf_hints(api, key) -> None:
    unit = api.add("ordinary evidence", _SCOPE, security=_SECURITY)[0]
    original_metadata = dict(unit.system_metadata)

    with pytest.raises(ValidationError, match="内核保留 key"):
        api.update(
            unit.id, _SCOPE, MemoryPatch(system_metadata={key: "forged"}),
            security=_SECURITY,
        )

    stored = api.get(unit.id, _SCOPE, security=_SECURITY)
    assert stored.hierarchy.is_empty
    assert stored.system_metadata == original_metadata
    assert api.list(_SCOPE, security=_SECURITY).count == 1


@pytest.mark.parametrize("with_hints", [False, True], ids=["ordinary", "leaf"])
def test_projection_names_remain_user_metadata_for_add_and_update(api, with_hints) -> None:
    user_metadata = {key: f"user-{key}" for key in _KERNEL_PROJECTION_KEYS}
    original = dict(user_metadata)
    unit = api.add(
        "user metadata evidence", _SCOPE, security=_SECURITY,
        system_metadata=dict(_HINTS) if with_hints else None,
        user_metadata=user_metadata,
    )[0]
    stored = api.get(unit.id, _SCOPE, security=_SECURITY)

    assert stored.user_metadata == original
    assert stored.hierarchy.is_empty is (not with_hints)
    assert user_metadata == original

    changed = {key: f"updated-{key}" for key in _KERNEL_PROJECTION_KEYS}
    updated = api.update(
        unit.id, _SCOPE, MemoryPatch(user_metadata=changed), security=_SECURITY,
    )
    reloaded = api.get(updated.id, _SCOPE, security=_SECURITY)

    assert reloaded.user_metadata == changed
    assert reloaded.hierarchy == stored.hierarchy
