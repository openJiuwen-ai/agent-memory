from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import NotFoundError, PolicyError, ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.config.config import Config
from jiuwen_memory.control.lifecycle_impl.kv_lifecycle_manager import KVLifecycleManager
from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import MemoryListResult
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

# 平台运维主体：调 admin_* 用。必须具名且带 ROOT——空 Scope 在 PR2 已不是特权形态
# （判定实现第 2 步 ``empty_actor`` 直接拒），全局配置属系统级资源，闸门只认 ROOT。
SEC_PLATFORM_OPS = internal_context(
    ScopedAuthenticator(Scope(org="system", user="platform-ops"), role=Role.ROOT)
)

_TEST_KEY_HEX = "00" * 32


def _store(unit) -> tuple[InMemoryKVStore, KVLifecycleManager]:
    kv = InMemoryKVStore()
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    return kv, KVLifecycleManager(kv)


def _load(kv: InMemoryKVStore, unit) -> object:
    return loads(kv.get(unit.scope, memory_key(unit.id)))


class _FakeDocStore:
    """文档模式 DomainStore stub：真源为内存 dict，``should_write_document`` 恒 True。

    ``get``/``update`` 覆盖 transition/supersede 的读写路径（文档模式经 DomainStore
    而非 KV）；``scopes``/``list`` 覆盖 sweep 的全量扫描。写操作记入 ``written`` 供断言。
    """

    def __init__(self, units: list[MemoryUnit]) -> None:
        self._units: dict[str, MemoryUnit] = {u.id: u for u in units}
        self.written: list[str] = []

    def should_write_document(self) -> bool:
        return True

    def get(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        return [self._units[uid] for uid in unit_ids if uid in self._units]

    def update(self, scope: Scope, units: list[MemoryUnit]) -> None:
        for unit in units:
            self._units[unit.id] = unit
            self.written.append(unit.id)

    def scopes(self) -> list[Scope]:
        seen: list[Scope] = []
        for unit in self._units.values():
            if unit.scope not in seen:
                seen.append(unit.scope)
        return seen

    def list(self, scope: Scope, *, limit: int = 100, **kwargs) -> MemoryListResult:
        return MemoryListResult(items=[u for u in self._units.values() if u.scope == scope])


def test_transition_allows_defined_forward_lifecycle_moves(unit_factory) -> None:
    unit = unit_factory("u1", "active memory", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    lifecycle.transition(unit.scope, [unit.id], LifecycleState.ARCHIVED)

    assert _load(kv, unit).lifecycle == LifecycleState.ARCHIVED


def test_transition_rejects_reactivating_forgotten_memory(unit_factory) -> None:
    unit = unit_factory("u1", "forgotten memory", lifecycle=LifecycleState.FORGOTTEN)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.transition(unit.scope, [unit.id], LifecycleState.ACTIVE)

    assert _load(kv, unit).lifecycle == LifecycleState.FORGOTTEN


def test_transition_rejects_reactivating_superseded_memory(unit_factory) -> None:
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.SUPERSEDED)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.transition(unit.scope, [unit.id], LifecycleState.ACTIVE)

    assert _load(kv, unit).lifecycle == LifecycleState.SUPERSEDED


def test_supersede_sets_state_and_invalid_time(unit_factory) -> None:
    invalid_at = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    updated = lifecycle.supersede(unit.scope, unit.id, invalid_at)

    stored = _load(kv, unit)
    assert updated.id == unit.id
    assert updated.lifecycle == LifecycleState.SUPERSEDED
    assert updated.temporal.t_invalid == invalid_at
    assert stored.lifecycle == LifecycleState.SUPERSEDED
    assert stored.temporal.t_invalid == invalid_at


def test_supersede_rejects_invalid_lifecycle_state(unit_factory) -> None:
    invalid_at = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)
    unit = unit_factory("u1", "archived version", lifecycle=LifecycleState.ARCHIVED)
    kv, lifecycle = _store(unit)

    with pytest.raises(ValidationError):
        lifecycle.supersede(unit.scope, unit.id, invalid_at)

    stored = _load(kv, unit)
    assert stored.lifecycle == LifecycleState.ARCHIVED
    assert stored.temporal.t_invalid is None


def test_supersede_raises_not_found_for_missing_unit() -> None:
    lifecycle = KVLifecycleManager(InMemoryKVStore())

    with pytest.raises(NotFoundError):
        lifecycle.supersede(
            Scope(),
            "missing",
            datetime(2026, 6, 17, 11, 0, tzinfo=UTC),
        )


def test_supersede_document_mode_reads_via_domain_store(unit_factory) -> None:
    """文档模式 supersede 经 DomainStore 读写真源（读 get、写 update），不抛 NotFoundError。

    回归锁：#261 第三个入口——文档模式 KV 恒空，supersede 若仍走 ``load_units(KV)``
    会读空→NotFoundError。修复后 transition/supersede 共用 ``_load_units`` 分流。
    """
    invalid_at = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)
    unit = unit_factory("u1", "old version", lifecycle=LifecycleState.ACTIVE)
    doc_store = _FakeDocStore([unit])
    # KV 故意留空（文档模式 KV 恒空），只注入 domain_store
    lifecycle = KVLifecycleManager(InMemoryKVStore(), domain_store=doc_store)

    updated = lifecycle.supersede(unit.scope, unit.id, invalid_at)

    assert updated.lifecycle == LifecycleState.SUPERSEDED
    assert updated.temporal.t_invalid == invalid_at
    # 写走 domain_store（非 KV）
    assert doc_store.written == ["u1"]
    assert doc_store._units["u1"].lifecycle == LifecycleState.SUPERSEDED


def test_transition_document_mode_reads_and_writes_via_domain_store(unit_factory) -> None:
    """文档模式 transition 经 DomainStore 读写真源（读 get、写 update），不读 KV。"""
    unit = unit_factory("u1", "active memory", lifecycle=LifecycleState.ACTIVE)
    doc_store = _FakeDocStore([unit])
    # KV 故意留空（文档模式 KV 恒空），只注入 domain_store
    lifecycle = KVLifecycleManager(InMemoryKVStore(), domain_store=doc_store)

    lifecycle.transition(unit.scope, [unit.id], LifecycleState.ARCHIVED)

    assert doc_store.written == ["u1"]
    assert doc_store._units["u1"].lifecycle == LifecycleState.ARCHIVED


def test_sweep_document_mode_reads_via_domain_store(unit_factory) -> None:
    """文档模式 sweep 经 DomainStore scopes()+list 读全量，纯计算不回写。"""
    now = datetime.now(UTC)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    active = unit_factory("active", "still active", lifecycle=LifecycleState.ACTIVE)
    superseded = unit_factory("superseded", "old version", lifecycle=LifecycleState.SUPERSEDED)
    doc_store = _FakeDocStore([expired, active, superseded])
    # KV 故意留空（文档模式 KV 恒空），只注入 domain_store
    lifecycle = KVLifecycleManager(InMemoryKVStore(), domain_store=doc_store)

    transitions = lifecycle.sweep()

    assert [t.unit_id for t in transitions] == ["expired", "superseded"]
    assert all(t.to_state == LifecycleState.FORGOTTEN for t in transitions)
    # 纯计算：sweep 不回写真源
    assert doc_store.written == []
    assert doc_store._units["expired"].lifecycle == LifecycleState.ACTIVE


def test_domain_store_none_degenerates_to_kv(unit_factory) -> None:
    """domain_store=None 显式退化为纯 KV 路径（transition 读写走 KV，不经分流）。"""
    unit = unit_factory("u1", "active memory", lifecycle=LifecycleState.ACTIVE)
    kv = InMemoryKVStore()
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    lifecycle = KVLifecycleManager(kv, domain_store=None)  # 显式 None

    lifecycle.transition(unit.scope, [unit.id], LifecycleState.FORGOTTEN)

    assert _load(kv, unit).lifecycle == LifecycleState.FORGOTTEN


def test_targeted_transition_does_not_mutate_same_id_in_another_scope(unit_factory) -> None:
    scope_a = Scope(org="acme", space="space-a", user="alice")
    scope_b = Scope(org="acme", space="space-b", user="alice")
    unit_a = unit_factory("shared-id", "space A", lifecycle=LifecycleState.ACTIVE)
    unit_b = unit_factory("shared-id", "space B", lifecycle=LifecycleState.ACTIVE)
    unit_a.scope = scope_a
    unit_b.scope = scope_b
    kv = InMemoryKVStore()
    kv.insert(scope_a, memory_key(unit_a.id), dumps(unit_a))
    kv.insert(scope_b, memory_key(unit_b.id), dumps(unit_b))
    lifecycle = KVLifecycleManager(kv)

    lifecycle.transition(scope_b, [unit_b.id], LifecycleState.FORGOTTEN)

    assert _load(kv, unit_a).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, unit_b).lifecycle == LifecycleState.FORGOTTEN


def test_sweep_returns_pending_transitions_without_mutating_units(unit_factory) -> None:
    now = datetime.now(UTC)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    superseded = unit_factory("superseded", "old version", lifecycle=LifecycleState.SUPERSEDED)
    active = unit_factory(
        "active",
        "still active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now + timedelta(days=1),
    )
    archived = unit_factory("archived", "archived memory", lifecycle=LifecycleState.ARCHIVED)
    forgotten = unit_factory("forgotten", "forgotten memory", lifecycle=LifecycleState.FORGOTTEN)
    kv = InMemoryKVStore()
    for unit in [expired, superseded, active, archived, forgotten]:
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    lifecycle = KVLifecycleManager(kv)

    transitions = lifecycle.sweep()

    # 纯计算：只返回到期/旧版的待执行 transition，不改真源（回写由 Engine 编排）。
    assert [t.unit_id for t in transitions] == ["expired", "superseded"]
    assert all(t.to_state == LifecycleState.FORGOTTEN for t in transitions)
    assert all(
        t.from_state in (LifecycleState.ACTIVE, LifecycleState.SUPERSEDED) for t in transitions
    )
    assert all(t.unit.id == t.unit_id for t in transitions)
    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, superseded).lifecycle == LifecycleState.SUPERSEDED
    assert _load(kv, active).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, archived).lifecycle == LifecycleState.ARCHIVED
    assert _load(kv, forgotten).lifecycle == LifecycleState.FORGOTTEN


def test_sweep_returns_empty_list_when_no_units_are_changed(unit_factory) -> None:
    unit = unit_factory("u1", "still active", lifecycle=LifecycleState.ACTIVE)
    kv, lifecycle = _store(unit)

    assert lifecycle.sweep() == []
    assert _load(kv, unit).lifecycle == LifecycleState.ACTIVE


def test_sweep_uses_policy_targets_for_expired_active_and_superseded(unit_factory) -> None:
    now = datetime.now(UTC)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    superseded = unit_factory("superseded", "old version", lifecycle=LifecycleState.SUPERSEDED)
    kv = InMemoryKVStore()
    for unit in [expired, superseded]:
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    policy = DictPolicyManager(
        {
            "lifecycle.expired_active.target": "archived",
            "lifecycle.superseded.target": "archived",
        }
    )
    lifecycle = KVLifecycleManager(kv, policy)

    transitions = lifecycle.sweep()

    assert [t.unit_id for t in transitions] == ["expired", "superseded"]
    assert all(t.to_state == LifecycleState.ARCHIVED for t in transitions)
    # 纯计算：目标态来自策略，但真源回写由 Engine 编排执行。
    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE
    assert _load(kv, superseded).lifecycle == LifecycleState.SUPERSEDED


def test_sweep_rejects_invalid_policy_target(unit_factory) -> None:
    now = datetime.now(UTC)
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=now - timedelta(days=1),
    )
    kv = InMemoryKVStore()
    kv.insert(expired.scope, memory_key(expired.id), dumps(expired))
    policy = DictPolicyManager(
        {
            "lifecycle.expired_active.target": "active",
            "lifecycle.superseded.target": "forgotten",
        }
    )
    lifecycle = KVLifecycleManager(kv, policy)

    with pytest.raises(PolicyError):
        lifecycle.sweep()

    assert _load(kv, expired).lifecycle == LifecycleState.ACTIVE


def test_default_kernel_exposes_lifecycle_policy_keys() -> None:
    api = build_kernel().api

    assert (
        api.admin_get("lifecycle.expired_active.target", security=SEC_PLATFORM_OPS) == "forgotten"
    )
    assert api.admin_get("lifecycle.superseded.target", security=SEC_PLATFORM_OPS) == "forgotten"

    api.admin_set("lifecycle.expired_active.target", "archived", security=SEC_PLATFORM_OPS)
    assert api.admin_get("lifecycle.expired_active.target", security=SEC_PLATFORM_OPS) == "archived"


def test_default_kernel_lifecycle_sweep_uses_runtime_policy(unit_factory) -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    kv = InMemoryKVStore()
    kernel = build_kernel(
        kv=kv,
        config=Config.from_dict(
            {"security": {"default": {"target": "local", "params": {"key_hex": _TEST_KEY_HEX}}}}
        ),
    )
    api = kernel.api
    expired = unit_factory(
        "expired-policy-smoke",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=datetime.now(UTC) - timedelta(days=1),
    )
    kv.insert(scope, memory_key(expired.id), dumps(expired))

    api.admin_set("lifecycle.expired_active.target", "archived", security=SEC_PLATFORM_OPS)
    engine = api._engine  # pylint: disable=protected-access
    result = asyncio.run(engine.sweep_expired())

    stored = loads(kernel.kv.get(scope, memory_key(expired.id)))
    assert result.swept == [expired.id]
    assert result.failed == []
    assert stored.lifecycle == LifecycleState.ARCHIVED


def test_build_injects_domain_store_for_document_mode(tmp_path) -> None:
    """``_build`` 装配注入 domain_store：文档模式（write_document=true）domain_store 非 None。"""
    # pylint: disable=protected-access  # 测试直取装配结果的私有 domain_store 断言接线
    from jiuwen_memory.common.bootstrap import register_plugins
    from jiuwen_memory.config.context import AssemblyContext
    from jiuwen_memory.control.lifecycle import LifecycleProducer
    from jiuwen_memory.storage import bootstrap as sb
    from jiuwen_memory.storage.store_manager import StoreManagerProducer

    sb.register_backends()
    register_plugins()
    StoreManagerProducer.reset_all()

    ctx = AssemblyContext.from_dict({
        "globals": {"write_document": True},
        "kv_store": {"default": "memory"},
        "markdown_store": {"default": {"target": "local", "params": {"root": str(tmp_path)}}},
        "shadow_index": {"default": {"target": "sqlite"}},
        "store_manager": {"default": {"target": "composite", "params": {
            "domain_stores": {"default": {"kv_store": "default"}},
        }}},
    })
    lifecycle = LifecycleProducer.build(
        "kv",
        {"kv_store": "default", "domain_store": "default"},
        ctx,
    )

    assert isinstance(lifecycle, KVLifecycleManager)
    assert lifecycle._domain_store is not None
    assert lifecycle._domain_store.should_write_document() is True
