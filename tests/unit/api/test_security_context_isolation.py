"""独立安全装配隔离、SDK 单次认证及审计资源关闭。"""

import sqlite3

import pytest

from jiuwen_memory.api import (
    AuthenticationError,
    Credentials,
    Grant,
    PermissionDeniedError,
    RateLimitedError,
    Scope,
    ValidationError,
    assemble_runtime,
    build_configured_security_runtime,
)
from jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from tests.integration.test_audit_integrity_flow import _sqlite_cfg
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _config(root_key):
    return {
        "security": {
            "default": {
                "target": "standard",
                "params": {
                    "authenticator": {"target": "api_key", "params": {"root_api_key": root_key}}
                },
            }
        }
    }


@pytest.fixture(name="runtimes")
def _runtimes():
    Factory.reset_all()
    created = []
    yield created
    for runtime in reversed(created):
        runtime.close()
    Factory.reset_all()


def test_configured_builds_do_not_reuse_other_credentials(runtimes):
    first = build_configured_security_runtime(_config("root-A"))
    runtimes.append(first)
    second = build_configured_security_runtime(_config("root-B"))
    runtimes.append(second)
    assert second is not first, "新装配必须使用自身凭据配置"
    assert first.authenticator.authenticate(Credentials(api_key="root-A")).role is Role.ROOT
    assert second.authenticator.authenticate(Credentials(api_key="root-B")).role is Role.ROOT
    with pytest.raises(AuthenticationError):
        first.authenticator.authenticate(Credentials(api_key="root-B"))
    with pytest.raises(AuthenticationError):
        second.authenticator.authenticate(Credentials(api_key="root-A"))
    first.close()
    second.health()


def test_sdk_authentication_uses_its_own_single_assembly(runtimes):
    first = assemble_runtime(config=_config("root-A"))
    runtimes.append(first)
    second = assemble_runtime(config=_config("root-B"))
    runtimes.append(second)
    security = first.authenticate(Credentials(api_key="root-A"))
    unit = first.api.add("single assembly", Scope(org="system", user="root"), security=security)[0]
    assert first.api.get(unit.id, unit.scope, security=security).content == "single assembly"
    with pytest.raises(AuthenticationError):
        second.authenticate(Credentials(api_key="root-A"))
    first.close()
    assert second.authenticate(Credentials(api_key="root-B")).auth.role is Role.ROOT
    with pytest.raises(ValidationError, match="closed"):
        first.authenticate(Credentials(api_key="root-A"))


def test_sdk_authentication_requires_explicit_configuration(runtimes):
    runtime = assemble_runtime()
    runtimes.append(runtime)
    with pytest.raises(ValidationError, match="configured"):
        runtime.authenticate(Credentials())


def test_sdk_authentication_releases_budget_after_rejection(runtimes):
    # 真实 guard 占满时必须失闭；认证异常也须释放槽位。
    # pylint: disable=protected-access
    config = _config("root-A")
    config["security"]["default"]["params"]["workload_guard"] = {
        "target": "semaphore",
        "params": {"max_concurrent": 1},
    }
    runtime = assemble_runtime(config=config)
    runtimes.append(runtime)
    guard = runtime._kernel.security_runtime.workload_guard
    assert guard.acquire()
    try:
        with pytest.raises(RateLimitedError):
            runtime.authenticate(Credentials(api_key="root-A"))
    finally:
        guard.release()
    with pytest.raises(AuthenticationError):
        runtime.authenticate(Credentials(api_key="invalid"))
    assert runtime.authenticate(Credentials(api_key="root-A")).auth.role is Role.ROOT


def test_id_only_revocation_reads_ownership_from_the_store(runtimes):
    from jiuwen_memory.common.security.types import Action

    runtime = assemble_runtime()
    runtimes.append(runtime)
    owner = Scope(org="acme", user="alice")
    reader = Scope(org="acme", user="bob")
    owner_context = internal_context(ScopedAuthenticator(owner))
    reader_context = internal_context(ScopedAuthenticator(reader))
    unit = runtime.api.add("shared", owner, security=owner_context)[0]
    saved = runtime.api.grant(
        Grant(owner, reader, frozenset({Action.READ})), security=owner_context
    )
    assert runtime.api.get(unit.id, owner, security=reader_context).id == unit.id
    reference = Grant.for_revocation(saved.grant_id)
    with pytest.raises(PermissionDeniedError):
        runtime.api.revoke(reference, security=reader_context)
    runtime.api.revoke(reference, security=owner_context)
    with pytest.raises(PermissionDeniedError):
        runtime.api.get(unit.id, owner, security=reader_context)
    with pytest.raises(ValueError, match="grant_id"):
        Grant.for_revocation("")


@pytest.mark.parametrize("protected", [False, True])
def test_runtime_closes_its_sqlite_audit_store(tmp_path, runtimes, protected, monkeypatch):
    # 保存真实审计 Store 验证资源释放，不增加生产调试接口。
    # pylint: disable=protected-access
    config = (
        _sqlite_cfg(str(tmp_path / "audit.sqlite3"))
        if protected
        else {
            "audit": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(tmp_path / "audit.sqlite3")},
                }
            }
        }
    )
    runtime = assemble_runtime(config=config)
    runtimes.append(runtime)
    logger = runtime._kernel.audit
    store = logger._audit if protected else logger
    closed = []
    close = store.close

    def track_close():
        closed.append(store)
        close()

    monkeypatch.setattr(store, "close", track_close)
    runtime.close()
    runtime.close()
    assert closed == [store], "共享链 Store、普通审计和重复关闭必须按身份去重"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.health()


def test_failed_assembly_closes_its_already_created_audit_connection(tmp_path, monkeypatch):
    closed = []
    close = SqliteAuditLogger.close

    def track_close(store):
        closed.append(store)
        close(store)

    monkeypatch.setattr(SqliteAuditLogger, "close", track_close)
    config = {
        "audit": {
            "default": {
                "target": "sqlite",
                "params": {"db_path": str(tmp_path / "failed-audit.sqlite3")},
            }
        },
        "globals": {"audit_verify_max_page_size": 0},
    }
    with pytest.raises(ValidationError):
        assemble_runtime(config=config)
    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        closed[0].health()
