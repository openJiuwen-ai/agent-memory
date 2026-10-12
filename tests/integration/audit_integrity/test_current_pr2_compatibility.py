"""启用持久审计链后，历史版本读取仍按当前 PR2 的返回快照判权。"""

from datetime import UTC, datetime

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.security.audit_integrity.base import AuditIntegrityStatus
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config import Config
from jiuwen_memory.control import SpaceSpec
from tests.integration.test_audit_integrity_flow import _sec, _sqlite_cfg

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
