# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实装配下的授权插件替换、写入失败与连接关闭回归。"""

import asyncio
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jiuwen_memory.api import (
    CloseableSecurityCapability,
    assemble,
    assemble_runtime,
    build_configured_security_runtime,
)
from jiuwen_memory.common.errors import (
    BackendError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.authorization.authorization_impl.standard_authorizer import (
    StandardAuthorizer,
)
from jiuwen_memory.common.security.authorization.base import AuthorizationProducer
from jiuwen_memory.common.security.authorization.store import (
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from jiuwen_memory.common.security.runtime import SecurityRuntimeProducer
from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.context import AssemblyContext
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


class PublicManagedStore(GrantStore):
    """只实现公开契约的第三方替身；没有内置 Store 的私有钩子。"""

    def __init__(self):
        self.records = {}
        self.lock = threading.RLock()

    def add(self, grant):
        with self.lock:
            old = self.records.get(grant.grant_id)
            if old is None or not old.revoked:
                self.records[grant.grant_id] = deepcopy(grant)

    def create_grant(self, grant):
        snapshot = deepcopy(grant)
        with self.lock:
            old = self.records.get(grant.grant_id)
            if old is not None:
                if old != snapshot:
                    raise BackendError("grant creation ID conflict")
                return
            self.records[grant.grant_id] = snapshot

    def get_grant(self, grant_id):
        with self.lock:
            return deepcopy(self.records.get(grant_id))

    def revoke_if_grantor(self, grant_id, expected_grantor):
        with self.lock:
            old = self.records.get(grant_id)
            if old is not None:
                if old.grantor != expected_grantor:
                    raise BackendError("grant changed during revocation")
                self.revoke(grant_id)

    def revoke(self, grant_id):
        with self.lock:
            old = self.records.get(grant_id)
            if old is not None:
                self.records[grant_id] = replace(old, revoked=True)

    def find_active(self, *, grantee, grantor_org, action, now):
        active = []
        with self.lock:
            records = deepcopy(tuple(self.records.values()))
        for grant in records:
            if grant.grantor.org != grantor_org or grant.grantee.org != grantee.org:
                continue
            if action in grant.actions and grant.is_active(now=now):
                active.append(deepcopy(grant))
        return active

    def health(self):
        return None


class FailingStore(PublicManagedStore):
    def create_grant(self, grant):
        raise BackendError("injected grant creation failure")


class MultiStoreAuthorizer(StandardAuthorizer):
    def __init__(self, first, second, delegations):
        super().__init__(first, delegations)
        self.second = second

    def management_grant_stores(self):
        return [self.management_grant_store(), self.second]


@GrantStoreProducer.register("contract_public_managed")
def _build_public_store(config):
    return PublicManagedStore()


@GrantStoreProducer.register("contract_failing_grant")
def _build_failing_store(config):
    return FailingStore()


@AuthorizationProducer.register("contract_multi_grant")
def _build_multi(config):
    return MultiStoreAuthorizer(
        GrantStoreProducer.dep(config, "grant_store"),
        GrantStoreProducer.dep(config, "secondary_grant_store"),
        DelegationStoreProducer.dep(config, "delegation_store"),
    )


def _grant():
    return Grant(
        grantor=Scope(org="review", user="alice"),
        grantee=Scope(org="review", user="bob"),
        actions=frozenset({Action.READ}),
    )


def test_multi_store_management_rejected_before_service():
    config = {
        "grant_store": {"first": "contract_public_managed", "second": "contract_failing_grant"},
        "delegation_store": {"default": "memory"},
        "authorizer": {
            "default": {
                "target": "contract_multi_grant",
                "params": {
                    "grant_store": "first",
                    "secondary_grant_store": "second",
                    "delegation_store": "default",
                },
            }
        },
    }
    runtime = None
    try:
        with pytest.raises(ValidationError, match="GrantStore"):
            runtime = assemble_runtime(config=config)
    finally:
        if runtime is not None:
            runtime.close()


def test_public_managed_plugin_can_grant_and_revoke():
    runtime = assemble_runtime(config={"grant_store": {"default": "contract_public_managed"}})
    try:
        api = runtime.api
        request = _grant()
        security = internal_context(ScopedAuthenticator(request.grantor))
        stored = api.grant(request, security=security)
        api.revoke(stored, security=security)
        api.revoke(stored, security=security)
    finally:
        runtime.close()


def test_creation_failure_does_not_leave_effective_grant():
    # 白盒确认失败请求不曾留下可以判权的记录。
    # pylint: disable=protected-access
    runtime = assemble_runtime(config={"grant_store": {"default": "contract_failing_grant"}})
    try:
        request = _grant()
        with pytest.raises(BackendError):
            runtime.api.grant(
                request, security=internal_context(ScopedAuthenticator(request.grantor))
            )
        store = runtime.api._authorizer.management_grant_store()
        assert not store.find_active(
            grantee=request.grantee,
            grantor_org=request.grantor.org,
            action=Action.READ,
            now=datetime.now(UTC),
        )
    finally:
        runtime.close()


def test_security_runtime_closes_sqlite_authorization_connections():
    runtime = SecurityRuntimeProducer.build(
        "standard",
        {
            "authenticator": {"target": "dev"},
            "authorizer": {
                "target": "standard",
                "params": {
                    "grant_store": {"target": "sqlite", "params": {"db_path": ":memory:"}},
                    "delegation_store": {"target": "sqlite", "params": {"db_path": ":memory:"}},
                },
            },
        },
        AssemblyContext(),
    )
    grants = runtime.authorizer.management_grant_store()
    delegations = runtime.authorizer.delegation_stores()[0]
    try:
        runtime.close()
        with pytest.raises(BackendError):
            grants.health()
        with pytest.raises(BackendError):
            delegations.health()
    finally:
        grants.close()
        delegations.close()


class PublicReadOnlyStore(GrantStore):
    """完整实现旧 ABC，不声明新增管理能力，仍可供 PDP 查询。"""

    def __init__(self):
        self.state = PublicManagedStore()

    def add(self, grant):
        self.state.add(grant)

    def revoke(self, grant_id):
        self.state.revoke(grant_id)

    def find_active(self, *, grantee, grantor_org, action, now):
        return self.state.find_active(
            grantee=grantee, grantor_org=grantor_org, action=action, now=now
        )

    def health(self):
        return None


@GrantStoreProducer.register("contract_read_only")
def _build_read_only(config):
    return PublicReadOnlyStore()


def test_old_public_plugin_remains_usable_for_read_only_decisions():
    # 白盒取到配置选择的真实 Store，模拟已有外部管理系统写入。
    # pylint: disable=protected-access
    runtime = assemble_runtime(config={"grant_store": {"default": "contract_read_only"}})
    try:
        api = runtime.api
        request = _grant()
        owner = internal_context(ScopedAuthenticator(request.grantor))
        unit = api.add("existing third-party authorization", request.grantor, security=owner)[0]
        store = api._authorizer.management_grant_store()
        store.add(replace(request, grant_id="external-record"))
        bob = internal_context(ScopedAuthenticator(request.grantee))
        assert api.get(unit.id, request.grantor, security=bob).id == unit.id
        with pytest.raises(UnsupportedCapabilityError, match="management"):
            api.grant(request, security=owner)
        with pytest.raises(UnsupportedCapabilityError, match="management"):
            api.revoke(replace(request, grant_id="external-record"), security=owner)
        assert len(store.state.records) == 1, "unsupported management must not write"
    finally:
        runtime.close()


def test_explicit_full_management_requires_capability_at_assembly():
    with pytest.raises(ValidationError, match="requires grant management"):
        assemble_runtime(
            config={
                "globals": {"require_grant_management": True},
                "grant_store": {"default": "contract_read_only"},
            }
        )


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_management_requirement_rejects_non_boolean_configuration(value):
    with pytest.raises(ValidationError, match="must be bool"):
        assemble_runtime(config={"globals": {"require_grant_management": value}})


@pytest.mark.parametrize("secured", [False, True])
def test_sdk_runtime_closes_authorization_stores_with_or_without_authentication(secured):
    # 白盒保存实际授权连接；API 公共协议不暴露 Store。
    # pylint: disable=protected-access
    config = {
        "grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        "delegation_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
    }
    if secured:
        config["security"] = {
            "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
        }
    runtime = assemble_runtime(config=config)
    grants = runtime.api._authorizer.management_grant_store()
    delegations = runtime.api._authorizer.delegation_stores()[0]
    try:
        runtime.close()
        runtime.close()
        for store in (grants, delegations):
            with pytest.raises(BackendError):
                store.health()
    finally:
        grants.close()
        delegations.close()


def test_plain_api_handle_can_close_its_owning_runtime():
    # 白盒保存配置选择的连接；对外仅使用显式关闭能力。
    # pylint: disable=protected-access
    api = assemble(
        config={
            "grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
            "delegation_store": {
                "default": {"target": "sqlite", "params": {"db_path": ":memory:"}}
            },
        }
    )
    grants = api._authorizer.management_grant_store()
    delegations = api._authorizer.delegation_stores()[0]
    assert isinstance(api, CloseableSecurityCapability)
    try:
        api.close()
        for store in (grants, delegations):
            with pytest.raises(BackendError):
                store.health()
    finally:
        api.close()


def test_configured_helper_rebuilds_a_closed_runtime():
    Factory.reset_all()
    config = {
        "security": {
            "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
        }
    }
    first = build_configured_security_runtime(config)
    first.close()
    with pytest.raises(ValidationError, match="closed"):
        first.health()
    second = build_configured_security_runtime(config)
    try:
        assert second is not first, "new assembly must not reuse a closed Runtime"
        second.health()
    finally:
        second.close()


def test_failed_background_shutdown_still_closes_security_resources(monkeypatch):
    # 白盒故障注入只作用本次实例；保留关闭异常及授权连接释放断言。
    # pylint: disable=protected-access
    runtime = assemble_runtime(
        config={"grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}}
    )
    grants = runtime.api._authorizer.management_grant_store()
    original_close = runtime._kernel.ingest_jobs.close

    def interrupted_close(*, wait=True):
        original_close(wait=wait)
        raise BackendError("injected background shutdown failure")

    with monkeypatch.context() as injection:
        injection.setattr(runtime._kernel.ingest_jobs, "close", interrupted_close)
        with pytest.raises(BackendError, match="background shutdown"):
            runtime.close()
    with pytest.raises(BackendError):
        grants.health()
    runtime.close()


def test_cancelled_grant_does_not_allow_the_recipient_to_read(monkeypatch):
    # 白盒注入 SQL 插入后取消；读取验证仍经过真实 PEP。
    # pylint: disable=protected-access
    runtime = assemble_runtime(
        config={"grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}}
    )
    try:
        grant = _grant()
        owner = internal_context(ScopedAuthenticator(grant.grantor))
        reader = internal_context(ScopedAuthenticator(grant.grantee))
        unit = runtime.api.add("cancelled grant regression", grant.grantor, security=owner)[0]
        store = runtime.api._authorizer.management_grant_store()
        original_add = store.add

        def interrupted_add(record):
            original_add(record)
            raise asyncio.CancelledError("request cancelled before commit")

        with monkeypatch.context() as injection:
            injection.setattr(store, "add", interrupted_add)
            with pytest.raises(asyncio.CancelledError):
                runtime.api.grant(grant, security=owner)
        with pytest.raises(PermissionDeniedError):
            runtime.api.get(unit.id, grant.grantor, security=reader)
        stored = runtime.api.grant(grant, security=owner)
        assert stored.grant_id
        assert runtime.api.get(unit.id, grant.grantor, security=reader).id == unit.id
    finally:
        runtime.close()


@pytest.fixture(name="tracked_sqlite_stores")
def _tracked_sqlite_stores():
    from jiuwen_memory.common.security.authorization.authorization_impl.sqlite_stores import (
        SQLiteDelegationStore,
        SQLiteGrantStore,
    )

    created = []

    @GrantStoreProducer.register("contract_tracked_sqlite_grant")
    def build_grant(config):
        store = SQLiteGrantStore(":memory:")
        created.append(store)
        return store

    @DelegationStoreProducer.register("contract_tracked_sqlite_delegation")
    def build_delegation(config):
        store = SQLiteDelegationStore(":memory:")
        created.append(store)
        return store

    @DelegationStoreProducer.register("contract_failed_delegation_build")
    def fail_delegation(config):
        raise BackendError("injected delegation construction failure")

    yield created
    for store in created:
        store.close()
    Factory.reset_all()


@pytest.mark.parametrize(
    "globals_config", [{"require_grant_management": 1}, {"audit_verify_max_page_size": 0}]
)
def test_rejected_assembly_closes_created_authorization_stores(
    tracked_sqlite_stores, globals_config
):
    with pytest.raises(ValidationError):
        assemble_runtime(
            config={
                "globals": globals_config,
                "grant_store": {"default": "contract_tracked_sqlite_grant"},
                "delegation_store": {"default": "contract_tracked_sqlite_delegation"},
            }
        )
    for store in tracked_sqlite_stores:
        with pytest.raises(BackendError):
            store.health()


def test_failed_authorizer_build_closes_its_already_created_grant_store(tracked_sqlite_stores):
    with pytest.raises(BackendError, match="construction failure"):
        assemble_runtime(
            config={
                "grant_store": {"default": "contract_tracked_sqlite_grant"},
                "delegation_store": {"default": "contract_failed_delegation_build"},
            }
        )
    assert len(tracked_sqlite_stores) == 1
    with pytest.raises(BackendError):
        tracked_sqlite_stores[0].health()


def test_configured_helper_does_not_reuse_closed_unauthenticated_sdk_dependencies():
    config = {
        "grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        "delegation_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
    }
    sdk = assemble_runtime(config=config)
    sdk.close()
    configured = build_configured_security_runtime(
        {
            **config,
            "security": {
                "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
            },
        }
    )
    try:
        configured.health()
    finally:
        configured.close()


@pytest.mark.parametrize("secured", [False, True])
def test_closing_an_older_runtime_keeps_the_newer_assembly_cache(secured):
    # 白盒校验缓存仍绑定新装配的同一 PDP，而不是重建成第三份。
    # pylint: disable=protected-access
    config = {
        "security": {
            "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
        }
    }
    first = assemble_runtime(config=config if secured else None)
    second = assemble_runtime(config=config if secured else None)
    try:
        first.close()
        configured = build_configured_security_runtime(config)
        assert configured.authorizer is second.api._authorizer
    finally:
        second.close()
        first.close()


def test_failed_configured_helper_preserves_the_live_borrowed_authorizer(monkeypatch):
    # 白盒对已装配 Store 注入健康故障；不得通过重建绕开或关闭借用对象。
    # pylint: disable=protected-access
    sdk = assemble_runtime(
        config={"grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}}
    )
    authorizer = sdk.api._authorizer
    store = authorizer.management_grant_store()
    config = {
        "security": {
            "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
        }
    }

    def unavailable():
        raise BackendError("injected temporary backend outage")

    try:
        with monkeypatch.context() as injection:
            injection.setattr(store, "health", unavailable)
            with pytest.raises(ValidationError, match="unhealthy"):
                build_configured_security_runtime(config)
        store.health()
        configured = build_configured_security_runtime(config)
        assert configured.authorizer is authorizer, "health failure must not replace live PDP"
        configured.close()
    finally:
        sdk.close()


def test_same_epoch_sdk_and_configured_helper_share_close_once(monkeypatch):
    # 白盒保存实际连接和消费者；共享根关闭任一视图都先停止后台、再只关一次 Store。
    # pylint: disable=protected-access
    sdk = assemble_runtime(
        config={"grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}}
    )
    store = sdk.api._authorizer.management_grant_store()
    configured = build_configured_security_runtime(
        {
            "security": {
                "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
            }
        }
    )
    events = []
    original_close = store.close
    original_stop = sdk._kernel.ingest_jobs.close

    def close_store():
        events.append("store")
        original_close()

    def stop_jobs(*, wait=True):
        events.append("background")
        original_stop(wait=wait)

    monkeypatch.setattr(store, "close", close_store)
    monkeypatch.setattr(sdk._kernel.ingest_jobs, "close", stop_jobs)
    try:
        configured.close()
        sdk.close()
        assert events == ["background", "store"]
    finally:
        sdk.close()
        configured.close()
