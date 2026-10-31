"""启用持久审计链后，历史版本读取仍按当前 PR2 的返回快照判权。"""

from datetime import UTC, datetime

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityError,
    AuditIntegrityStatus,
)
from jiuwen_memory.common.security.authorization.store import GrantStoreProducer
from jiuwen_memory.common.security.types import Action, Grant, Role
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config import Config
from jiuwen_memory.control import SpaceSpec
from tests.integration.test_audit_integrity_flow import _sec, _sqlite_cfg
from tests.support.public_managed_store import PublicManagedStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["backward", "forward"])
@pytest.mark.parametrize("other_author", [False, True])
def test_protected_audit_checks_selected_version(tmp_path, direction, other_author):
    # 构造合法版本链的跨作者边界；直接写 KV 只为固定历史时间，不扩充产品接口。
    # pylint: disable=protected-access
    settings = _sqlite_cfg(str(tmp_path / "versions.sqlite3"))._data.copy()
    components = (
        "ingestor",
        "index_builder",
        "retriever",
        "kv_store",
        "scheduler",
        "evolver",
        "lifecycle",
    )
    settings["engine"] = {
        "default": {"target": "cloud", "params": {name: "default" for name in components}}
    }
    settings["authorizer"] = {
        "default": {"target": "space_aware", "params": {"delegate": "standard"}},
        "standard": {
            "target": "standard",
            "params": {"grant_store": "default", "delegation_store": "default"},
        },
    }
    runtime = assemble_runtime(config=Config.from_dict(settings))
    try:
        api = runtime.api
        actor = Scope(org="acme", agent="a1")
        scope = Scope(org="acme", space="agent-space")
        root = _sec(Scope(org="acme", user="ops"), Role.ROOT)
        security = _sec(actor)
        api.create_space(SpaceSpec(org=scope.org, space=scope.space, owner=actor), security=root)
        requested = api.add("visible version", scope, security=security)[0]
        selected = api.add("selected version", scope, security=root if other_author else security)[
            0
        ]
        old, new = (selected, requested) if direction == "backward" else (requested, selected)
        old.temporal.t_valid = datetime(2026, 1, 1, tzinfo=UTC)
        new.temporal.t_valid = datetime(2026, 1, 2, tzinfo=UTC)
        new.supersedes = old.id
        for unit in (old, new):
            api._governor._kv.update(unit.scope, memory_key(unit.id), dumps(unit))

        assert api.get(requested.id, scope, security=security).id == requested.id
        if other_author:
            with pytest.raises(PermissionDeniedError):
                api.get(requested.id, scope, security=security, as_of=selected.temporal.t_valid)
            events = api.audit(filters={"action": "get"}, security=root)
            assert any(event.decision == "deny" for event in events), "历史版本拒绝必须落审计链"
        else:
            result = api.get(
                requested.id, scope, security=security, as_of=selected.temporal.t_valid
            )
            assert result.id == selected.id, "同作者的历史版本读取应继续可用"
        assert api.verify_audit(security=root).status is AuditIntegrityStatus.CLEAN
    finally:
        runtime.close()


def test_public_grant_store_remains_atomic_with_protected_audit(tmp_path, monkeypatch):
    """公开插件能力与审计预检组合：故障不撤权，恢复后原子撤权且链保持完整。"""
    # 确认真实 PDP Store 和故障注入点；不为兼容测试新增生产调试接口。
    # pylint: disable=protected-access
    # 测试注册只修改副本，结束后由 monkeypatch 恢复，避免导入其他测试模块触发注册。
    monkeypatch.setattr(GrantStoreProducer, "_registry", dict(GrantStoreProducer._registry))

    @GrantStoreProducer.register("audit_contract_public_managed")
    def build(config):
        return PublicManagedStore()

    settings = _sqlite_cfg(str(tmp_path / "managed.sqlite3"))._data.copy()
    settings["grant_store"] = {"default": {"target": "audit_contract_public_managed"}}
    runtime = assemble_runtime(config=Config.from_dict(settings))
    try:
        api = runtime.api
        assert isinstance(api._authorizer.management_grant_store(), PublicManagedStore)
        owner = Scope(org="acme", user="owner")
        reader = Scope(org="acme", user="reader")
        security = _sec(owner)
        unit = api.add("shared through a public Store", owner, security=security)[0]
        stored = api.grant(
            Grant(grantor=owner, grantee=reader, actions=frozenset({Action.READ})),
            security=security,
        )
        assert api.get(unit.id, owner, security=_sec(reader)).id == unit.id

        def unavailable():
            raise AuditIntegrityError("synthetic audit health failure")

        with monkeypatch.context() as patch:
            patch.setattr(api._audit_integrity, "health", unavailable)
            with pytest.raises(AuditIntegrityError):
                api.revoke(stored, security=security)
            assert api.get(unit.id, owner, security=_sec(reader)).id == unit.id

        api.revoke(stored, security=security)
        api.revoke(stored, security=security)
        with pytest.raises(PermissionDeniedError):
            api.get(unit.id, owner, security=_sec(reader))
        root = _sec(Scope(org="acme", user="ops"), Role.ROOT)
        assert api.verify_audit(security=root).status is AuditIntegrityStatus.CLEAN
    finally:
        runtime.close()
