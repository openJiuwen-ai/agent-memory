# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""阶段 D：审计完整性端到端流（计划 §3、§17 验收）。

经 ``_build_kernel`` 完整装配（真实 SQLite + audit_integrity），验证端到端语义：

- ``add`` 经 ``ProtectedAuditLogger`` -> ``provider.record_chained`` -> ``verify_audit`` CLEAN；
- 重新装配（重开同一 SQLite）链连续，``verify_audit`` 仍 CLEAN（持久化）；
- 直接篡改 SQLite 事件行的受保护字段后，``verify_audit`` 经 API 返回 TAMPERED。

与 unit 互补：unit 在 store/provider 纯算法层覆盖规范化/篡改/CAS/epoch/持久化重启；
此处覆盖完整装配链路与 API ``verify_audit`` 入口的端到端组合。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityError,
    AuditIntegrityStatus,
    AuditSchemaError,
)
from jiuwen_memory.common.security.types import Action, Grant, Role
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control import BatchWriteItem, DeleteMode, DeleteSelector, MemoryPatch
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.integration

SCOPE = Scope(org="acme", user="u1")

_KEY_HEX = "44" * 32


def _sqlite_cfg(db_path: str) -> Config:
    """真实 SQLite 持久链 + 独立 audit key provider（非 test-only，无需 opt-in）。"""
    return Config.from_dict(
        {
            "key_provider": {
                "audit_keys": {"target": "local", "params": {"key_hex": _KEY_HEX}},
            },
            "audit": {"default": {"target": "sqlite", "params": {"db_path": db_path}}},
            "audit_integrity": {
                "default": {
                    "target": "chained_hmac",
                    "params": {"key_provider": "audit_keys", "audit": "default"},
                }
            },
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {"target": "dev"},
                        "authorizer": "default",
                        "audit_integrity": "default",
                    },
                }
            },
            "authorizer": {
                "default": {
                    "target": "standard",
                    "params": {"grant_store": "default", "delegation_store": "default"},
                }
            },
            "grant_store": {"default": {"target": "memory"}},
            "delegation_store": {"default": {"target": "memory"}},
        }
    )


def _sec(actor: Scope, role: Role = Role.USER):
    """测试身份构造器：显式绕过认证边界，由 PDP 决定管理面权限。"""
    return internal_context(ScopedAuthenticator(actor, role=role))


def test_persistent_chain_survives_rebuild(tmp_path) -> None:
    """端到端持久化：add 落链 -> verify CLEAN -> 重开 SQLite -> 链连续仍 CLEAN。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    kernel = build_kernel(config=_sqlite_cfg(str(db)))
    api = kernel.api
    for i in range(3):
        api.add(f"event {i}", SCOPE, security=_sec(SCOPE))
    result = api.verify_audit(security=_sec(Scope(org="acme", user="auditor"), role=Role.ROOT))
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.checked_count >= 3
    assert result.high_water_mark >= 3

    # 关闭底层 store 后重新装配（重开同一 SQLite 文件），验证链未断、事件未丢。
    kernel.audit._audit.close()  # noqa: SLF001 - 测试需访问底层 store 关连接
    kernel2 = build_kernel(config=_sqlite_cfg(str(db)))
    result2 = kernel2.api.verify_audit(
        security=_sec(Scope(org="acme", user="auditor"), role=Role.ROOT)
    )
    assert result2.status is AuditIntegrityStatus.CLEAN
    assert result2.checked_count >= result.checked_count
    assert result2.high_water_mark >= result.high_water_mark


def test_tampered_event_detected_via_api(tmp_path) -> None:
    """端到端篡改检测：绕过 provider 改受保护字段 -> verify_audit 返回 TAMPERED。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    kernel = build_kernel(config=_sqlite_cfg(str(db)))
    api = kernel.api
    api.add("intact", SCOPE, security=_sec(SCOPE))
    assert (
        api.verify_audit(security=_sec(Scope(org="acme", user="auditor"), role=Role.ROOT)).status
        is AuditIntegrityStatus.CLEAN
    )

    # 直接篡改 seq=1 的受保护字段（action）：proof 重算不再匹配，链接也随之断裂。
    store = kernel.audit._audit  # noqa: SLF001 - 测试需访问底层 store 做篡改
    store._conn.execute("UPDATE audit_events SET action='tampered' WHERE seq=1")  # noqa: SLF001
    store._conn.commit()

    result = api.verify_audit(security=_sec(Scope(org="acme", user="auditor"), role=Role.ROOT))
    assert result.status is AuditIntegrityStatus.TAMPERED
    assert result.error_count >= 1


# ====================================================================== #
# 独立验收故障样本镜像（security-plans/problems/2026-09-22-pr3-*
# 独立验收报告 PR3-06/08 的复现样本沉淀为正式测试）
# ====================================================================== #


def _inline_cfg(db_path: str) -> Config:
    """audit_integrity 以 security.params 内联组件配置（无顶层具名段）。"""
    return Config.from_dict(
        {
            "key_provider": {
                "audit_keys": {"target": "local", "params": {"key_hex": _KEY_HEX}},
            },
            "audit": {"default": {"target": "sqlite", "params": {"db_path": db_path}}},
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": {"target": "dev"},
                        "authorizer": "default",
                        "audit_integrity": {
                            "target": "chained_hmac",
                            "params": {"key_provider": "audit_keys", "audit": "default"},
                        },
                    },
                }
            },
            "authorizer": {
                "default": {
                    "target": "standard",
                    "params": {"grant_store": "default", "delegation_store": "default"},
                }
            },
            "grant_store": {"default": {"target": "memory"}},
            "delegation_store": {"default": {"target": "memory"}},
        }
    )


def test_inline_security_params_audit_integrity_is_installed(tmp_path) -> None:
    """
    security.params.audit_integrity 配成内联组件时同样完成接线（PR3-06）：以实际
    provider 为准安装受保护审计，不能只看顶层具名段——否则部署以为受链式保护，
    实际仍是普通审计（verify 返回 unsupported、写入无 proof）。
    """
    kernel = build_kernel(config=_inline_cfg(str(tmp_path / "inline.sqlite3")))
    result = kernel.api.verify_audit(
        security=_sec(Scope(org="acme", user="auditor"), role=Role.ROOT)
    )
    assert result.status is AuditIntegrityStatus.CLEAN


def test_add_checks_audit_health_before_mutation(tmp_path, monkeypatch) -> None:
    """
    业务 mutation 前审计完整性健康预检（PR3-08，计划 §9.3）：审计子系统不健康时
    拒绝执行写入，业务命令不得被调用。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "health.sqlite3")))
    writes: list[bool] = []

    def _fail_health():
        raise AuditSchemaError("injected unhealthy audit subsystem")

    async def _write(*args, **kwargs):
        writes.append(True)
        return []

    monkeypatch.setattr(kernel.api._audit_integrity, "health", _fail_health)  # noqa: SLF001
    monkeypatch.setattr(kernel.api._commands, "write", _write)  # noqa: SLF001
    with pytest.raises(AuditIntegrityError):
        kernel.api.add("payload", SCOPE, security=_sec(SCOPE))
    assert not writes


def test_authenticated_fails_closed_on_integrity_write_failure(tmp_path, monkeypatch) -> None:
    """
    认证成功事件的受保护审计写入失败 -> 请求不得进入业务主体（PR3-08）：关键安全
    事件 fail-closed；普通审计后端的吞错语义不适用于链式完整性。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    from jiuwen_memory.common.security.types import Credentials
    from jiuwen_memory_entry.core.auth_middleware import authenticated

    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "auth.sqlite3")))

    def _fail(event):
        raise AuditIntegrityError("injected protected audit failure")

    monkeypatch.setattr(kernel.api._audit_integrity, "record_chained", _fail)  # noqa: SLF001
    entered: list[bool] = []
    with pytest.raises(AuditIntegrityError):
        with authenticated(
            ScopedAuthenticator(Scope(org="acme", user="alice")),
            Credentials(peer_address="127.0.0.1"),
            audit=kernel.audit,
        ):
            entered.append(True)
    assert not entered


# ====================================================================== #
# 复验故障样本镜像（security-plans/problems/2026-09-22-pr3-reacceptance.md
# R1 真实后端错误传播 / R3 全 mutation 预检 / R8b detail 保留键）
# ====================================================================== #


_FAIL_TRIGGER = (
    "CREATE TRIGGER fail_audit_insert BEFORE INSERT ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'injected protected append failure'); END"
)


def _install_append_failure(store) -> None:
    """真实 SQLite 触发器：让审计追加在数据库层失败，不走任何 mock。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store._conn.execute(_FAIL_TRIGGER)  # noqa: SLF001 - 故障注入需直写后端连接
    store._conn.commit()  # noqa: SLF001


def _fail_audit_health(monkeypatch, kernel) -> None:
    """把 provider.health 注入为不健康：预检路径（R3）的故障样本。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access

    def _fail():
        raise AuditSchemaError("injected unhealthy audit subsystem")

    monkeypatch.setattr(kernel.api._audit_integrity, "health", _fail)  # noqa: SLF001


def test_authenticated_fails_closed_on_real_backend_append_failure(tmp_path) -> None:
    """
    R1：真实 SQLite 追加失败（触发器 ABORT）不得被认证中间件吞掉——后端原生异常
    经 ProtectedAuditLogger 归一为 AuditIntegrityError 上抛，请求不得进入业务主体。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    from jiuwen_memory.common.security.types import Credentials
    from jiuwen_memory_entry.core.auth_middleware import authenticated

    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "auth-trigger.sqlite3")))
    _install_append_failure(kernel.audit._audit)  # noqa: SLF001

    entered: list[bool] = []
    with pytest.raises(AuditIntegrityError, match="IntegrityError"):
        with authenticated(
            ScopedAuthenticator(Scope(org="acme", user="alice")),
            Credentials(peer_address="127.0.0.1"),
            audit=kernel.audit,
        ):
            entered.append(True)
    assert not entered


def test_degraded_latch_isolates_mutations_until_rebuild(tmp_path) -> None:
    """
    R1/R8c：add 的受保护审计追加真实失败 -> fail-closed + degraded 闩隔离后续
    mutation；闩不自动清除，恢复靠重建 Runtime（重开同一 SQLite 仍 CLEAN）。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "degraded.sqlite3"
    kernel = build_kernel(config=_sqlite_cfg(str(db)))
    api = kernel.api
    api.add("intact", SCOPE, security=_sec(SCOPE))

    _install_append_failure(kernel.audit._audit)  # noqa: SLF001

    # 审计追加在业务写入之后失败：抛稳定错误，业务库多一条无证据记录（计划 §9.3
    # 不宣称跨库原子，预检只把故障挡在扩大之前）。
    with pytest.raises(AuditIntegrityError):
        api.add("unprotected", SCOPE, security=_sec(SCOPE))

    # degraded 闩：后续 mutation 在业务副作用之前被隔离，第二条 add 不再落业务库。
    with pytest.raises(AuditIntegrityError, match="degraded"):
        api.add("isolated", SCOPE, security=_sec(SCOPE))

    # 闩是单向的：故障源消除也不自动恢复，同一 Runtime 继续隔离。
    store = kernel.audit._audit  # noqa: SLF001
    store._conn.execute("DROP TRIGGER fail_audit_insert")  # noqa: SLF001
    store._conn.commit()  # noqa: SLF001
    with pytest.raises(AuditIntegrityError, match="degraded"):
        api.add("still-isolated", SCOPE, security=_sec(SCOPE))

    # 恢复语义是进程重启 / 运维修复后重建 Runtime：重开同一 SQLite，链可续写。
    store.close()
    kernel2 = build_kernel(config=_sqlite_cfg(str(db)))
    kernel2.api.add("recovered", SCOPE, security=_sec(SCOPE))
    result = kernel2.api.verify_audit(
        security=_sec(Scope(org="acme", user="auditor"), role=Role.ROOT)
    )
    assert result.status is AuditIntegrityStatus.CLEAN


def test_update_isolated_when_audit_health_fails(tmp_path, monkeypatch) -> None:
    """R3 预检镜像：update 在审计子系统不健康时拒绝执行，条目内容不被改写。"""
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "update.sqlite3")))
    api = kernel.api
    unit = api.add("before", SCOPE, security=_sec(SCOPE))[0]

    _fail_audit_health(monkeypatch, kernel)
    with pytest.raises(AuditIntegrityError):
        api.update(unit.id, SCOPE, MemoryPatch(content="after"), security=_sec(SCOPE))

    assert api.get(unit.id, SCOPE, security=_sec(SCOPE)).content == "before"


def test_delete_isolated_when_audit_health_fails(tmp_path, monkeypatch) -> None:
    """R3 预检镜像：delete 在审计子系统不健康时拒绝执行，条目不被删除。"""
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "delete.sqlite3")))
    api = kernel.api
    unit = api.add("kept", SCOPE, security=_sec(SCOPE))[0]

    _fail_audit_health(monkeypatch, kernel)
    with pytest.raises(AuditIntegrityError):
        api.delete(
            DeleteSelector(scope=SCOPE, unit_ids=[unit.id], mode=DeleteMode.PURGE),
            security=_sec(SCOPE),
        )

    assert api.get(unit.id, SCOPE, security=_sec(SCOPE)) is not None


def test_batch_add_isolated_when_audit_health_fails(tmp_path, monkeypatch) -> None:
    """R3 预检镜像：batch_add 在审计子系统不健康时整批拒绝，业务写入不被调用。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "batch.sqlite3")))
    api = kernel.api
    writes: list[bool] = []

    async def _batch(*args, **kwargs):
        writes.append(True)
        return []

    monkeypatch.setattr(kernel.api._commands, "batch_write_aligned", _batch)  # noqa: SLF001
    _fail_audit_health(monkeypatch, kernel)
    with pytest.raises(AuditIntegrityError):
        api.batch_add([BatchWriteItem(content="x")], SCOPE, security=_sec(SCOPE))

    assert not writes


def test_evolve_isolated_when_audit_health_fails(tmp_path, monkeypatch) -> None:
    """R3 预检镜像：evolve 在审计子系统不健康时拒绝，演进任务不被提交。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "evolve.sqlite3")))
    api = kernel.api
    submitted: list[bool] = []

    async def _evolve(*args, **kwargs):
        submitted.append(True)
        return "job-1"

    monkeypatch.setattr(kernel.api._commands, "evolve", _evolve)  # noqa: SLF001
    _fail_audit_health(monkeypatch, kernel)
    with pytest.raises(AuditIntegrityError):
        api.evolve(SCOPE, EvolveMode.EXTRACT, security=_sec(SCOPE))

    assert not submitted


def test_grant_isolated_when_audit_health_fails(tmp_path, monkeypatch) -> None:
    """R3 预检镜像：grant 在审计子系统不健康时拒绝执行，被授权方拿不到任何权限。"""
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "grant.sqlite3")))
    api = kernel.api
    unit = api.add("private", SCOPE, security=_sec(SCOPE))[0]
    eve = Scope(org="acme", user="eve")

    _fail_audit_health(monkeypatch, kernel)
    with pytest.raises(AuditIntegrityError):
        api.grant(
            Grant(grantor=SCOPE, grantee=eve, actions=[Action.READ]),
            security=_sec(SCOPE),
        )

    with pytest.raises(PermissionDeniedError):
        api.get(unit.id, SCOPE, security=_sec(eve))


def test_reserved_detail_keys_cannot_be_forged(tmp_path) -> None:
    """
    R8b：detail 保留键（decision / request_id）由服务端权威写入，调用方自带
    同名键被剥除，不能把 deny 事件伪装成 allow（计划 §10）。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kernel = build_kernel(config=_sqlite_cfg(str(tmp_path / "detail.sqlite3")))
    kernel.api._record_audit(  # noqa: SLF001 - 直接驱动 PEP 审计入口验证保留键剥除
        SCOPE,
        "probe",
        decision="deny",
        detail={"decision": "allow", "request_id": "forged", "note": "x"},
    )

    events = kernel.audit.query({"action": "probe"}, 10)
    assert len(events) == 1
    stored = events[0]
    assert stored.decision == "deny"
    assert stored.detail["decision"] == "deny"
    assert stored.detail.get("request_id") != "forged"
    assert stored.detail["note"] == "x"
