from __future__ import annotations

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
    messages_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config import Config
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


def _routing_config() -> Config:
    return Config.from_dict(
        {
            "permission": {
                "default": {
                    "target": "routing",
                    "params": {
                        "route_key": "memory_type",
                        "fallback": "strict",
                        "routes": {"coding": "strict", "episodic": "standard"},
                    },
                },
                "standard": "allow_all",
                "strict": "sqlite",
            }
        }
    )


def test_memory_api_list_supports_pagination_and_memory_type_filter() -> None:
    api = assemble()
    scope = Scope(org="acme", user="owner")

    episodic = api.add(
        "alice joined the sprint planning",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"memory_type": "episodic"},
    )[0]
    coding = api.add(
        "repo uses pytest for unit tests",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"memory_type": "coding"},
    )[0]
    semantic = api.add(
        "alice prefers concise summaries",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"memory_type": "semantic"},
    )[0]

    coding_result = api.list(scope, security=legacy_request_context(scope), memory_types=["coding"])
    all_result = api.list(scope, security=legacy_request_context(scope))
    second_page = api.list(scope, security=legacy_request_context(scope), offset=1, limit=1)

    assert [unit.id for unit in coding_result.items] == [coding.id]
    assert coding_result.count == 1
    assert len(second_page.items) == 1
    assert second_page.items[0].id == all_result.items[1].id
    assert second_page.count == 3
    assert {unit.id for unit in all_result.items} == {episodic.id, coding.id, semantic.id}


def test_memory_api_list_is_scope_bound_and_ignores_message_prefix_records() -> None:
    kv = InMemoryKVStore()
    api = assemble(kv=kv)
    owner = Scope(org="acme", user="owner")
    other = Scope(org="acme", user="other")

    visible = api.add("visible indexed memory", owner, security=legacy_request_context(owner))[0]
    hidden = MemoryUnit(
        id="raw-message",
        scope=owner,
        segments=[Segment(content="hidden infer source")],
        temporal=Temporal(t_ingest=visible.temporal.t_ingest),
    )
    kv.insert(owner, messages_key(hidden.id), dumps(hidden))
    api.add("other tenant memory", other, security=legacy_request_context(other))

    listed = api.list(owner, security=legacy_request_context(owner))

    assert [unit.id for unit in listed.items] == [visible.id]
    assert listed.count == 1


def test_memory_api_list_filters_before_pagination_and_preserves_total_count() -> None:
    api = assemble()
    scope = Scope(org="acme", user="owner")

    first = api.add(
        "first alpha memory",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"memory_type": "coding"},
        user_metadata={"project": "alpha", "priority": 1},
    )[0]
    second = api.add(
        "second alpha memory",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"memory_type": "coding"},
        user_metadata={"project": "alpha", "priority": 2},
    )[0]
    api.add(
        "beta memory",
        scope,
        security=legacy_request_context(scope),
        system_metadata={"memory_type": "coding"},
        user_metadata={"project": "beta", "priority": 3},
    )

    result = api.list(
        scope,
        security=legacy_request_context(scope),
        offset=1,
        limit=1,
        memory_types=["coding"],
        filters={
            "AND": [
                {"user_metadata.project": "alpha"},
                {"user_metadata.priority": {"gte": 1}},
            ]
        },
    )

    assert result.count == 2
    assert len(result.items) == 1
    assert result.items[0].id in {first.id, second.id}


def test_memory_api_list_copies_extensions_and_forwards_normalized_filters() -> None:
    # Kernel.kv 现为 manager 的授权代理（F07/F08），monkey-patch 代理观察不到
    # 数据面内部调用——改为注入 RecordingKV：注入实例同时是 manager 的 KV 端口
    # 与数据面真源，list 透传 kwargs 在同一实例上可见。
    kv = _RecordingKV()
    api = assemble(kv=kv)
    scope = Scope(org="acme", user="owner")
    api.add(
        "alpha memory",
        scope,
        security=legacy_request_context(scope),
        user_metadata={"project": "alpha"},
    )
    extensions = {"vendor_mode": 7}
    filters = FilterClause("user_metadata.project", FilterOp.EQ, "alpha")

    result = api.list(
        scope,
        security=legacy_request_context(scope),
        extensions=extensions,
        filters=filters,
    )

    assert result.count == 1
    assert extensions == {"vendor_mode": 7}
    assert kv.calls[0][0] == scope
    assert kv.calls[0][1]["extensions"] == {"vendor_mode": "7"}
    assert kv.calls[0][1]["extensions"] is not extensions
    assert kv.calls[0][1]["filters"] == filters


class _RecordingKV(InMemoryKVStore):
    """记录 list 入参（scope + kwargs），供透传断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Scope, dict]] = []

    def list(self, target_scope, **kwargs):
        self.calls.append((target_scope, kwargs))
        return super().list(target_scope, **kwargs)


def test_memory_api_list_rejects_invalid_extensions_and_scope_filter() -> None:
    api = assemble()
    scope = Scope(org="acme", user="owner")

    with pytest.raises(ValidationError):
        api.list(scope, security=legacy_request_context(scope), extensions=["invalid"])
    with pytest.raises(ValidationError):
        api.list(scope, security=legacy_request_context(scope), filters={"space": "other"})


def test_memory_api_list_validates_pagination() -> None:
    api = assemble()
    scope = Scope(org="acme", user="owner")

    with pytest.raises(ValidationError):
        api.list(scope, security=legacy_request_context(scope), offset=-1)
    with pytest.raises(ValidationError):
        api.list(scope, security=legacy_request_context(scope), limit=0)


def test_memory_api_list_permission_routes_by_memory_type() -> None:
    api = assemble(config=_routing_config())
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")

    api.list(owner, security=legacy_request_context(reader), memory_types=["episodic"])
    with pytest.raises(PermissionDeniedError):
        api.list(owner, security=legacy_request_context(reader), memory_types=["coding"])
    with pytest.raises(PermissionDeniedError):
        api.list(
            owner, security=legacy_request_context(reader), memory_types=["episodic", "coding"]
        )


def test_memory_api_unfiltered_list_uses_strict_fallback() -> None:
    api = assemble(config=_routing_config())
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    api.add(
        "private coding memory",
        owner,
        security=legacy_request_context(owner),
        system_metadata={"memory_type": "coding"},
    )

    with pytest.raises(PermissionDeniedError):
        api.list(owner, security=legacy_request_context(reader))


def test_memory_api_list_binds_extension_permission_route_to_filter() -> None:
    api = assemble(config=_routing_config())
    owner = Scope(org="acme", user="owner")
    reader = Scope(org="acme", user="reader")
    episodic = api.add(
        "shareable episodic memory",
        owner,
        security=legacy_request_context(owner),
        system_metadata={"memory_type": "episodic"},
    )[0]
    api.add(
        "private coding memory",
        owner,
        security=legacy_request_context(owner),
        system_metadata={"memory_type": "coding"},
    )

    result = api.list(
        owner,
        security=legacy_request_context(reader),
        extensions={"memory_type": "episodic"},
    )

    assert result.count == 1
    assert [unit.id for unit in result.items] == [episodic.id]
