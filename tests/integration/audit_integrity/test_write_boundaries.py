"""审计故障先于隐式建空间副作用，系统请求标识在 detail 满额时仍保留。"""

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityError,
    AuditSchemaError,
)
from jiuwen_memory.common.security.request_context import reset_request_id, set_request_id
from jiuwen_memory.common.type_def import AuditEvent, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.control import BatchWriteItem
from tests.integration.test_audit_integrity_flow import _sec, _sqlite_cfg
from tests.unit.api.test_collective_routing import ROUTE_TABLE  # registers keyword_stub

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["add", "batch_add"])
@pytest.mark.parametrize("failure", ["health", "degraded"])
def test_fallback_creation_is_blocked_before_side_effects(
    tmp_path, monkeypatch, operation, failure
):
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    data = _sqlite_cfg(str(tmp_path / "routing.sqlite3"))._data.copy()
    components = (
        "ingestor",
        "index_builder",
        "retriever",
        "kv_store",
        "scheduler",
        "evolver",
        "lifecycle",
    )
    data["engine"] = {"default": {"target": "cloud", "params": {k: "default" for k in components}}}
    data["authorizer"] = {
        "default": {"target": "space_aware", "params": {"delegate": "standard"}},
        "standard": {
            "target": "standard",
            "params": {"grant_store": "default", "delegation_store": "default"},
        },
    }
    data["router"] = {"default": {"target": "keyword_stub", "params": dict(ROUTE_TABLE)}}
    kernel = _build_kernel(config=Config.from_dict(data))
    if failure == "health":

        def unhealthy():
            raise AuditSchemaError("injected audit health failure")

        monkeypatch.setattr(kernel.api._audit_integrity, "health", unhealthy)
    else:
        original = kernel.audit._provider.record_chained

        def broken(event):
            raise OSError("injected disk failure")

        monkeypatch.setattr(kernel.audit._provider, "record_chained", broken)
        with pytest.raises(AuditIntegrityError):
            kernel.audit.record(AuditEvent(action="probe"))
        monkeypatch.setattr(kernel.audit._provider, "record_chained", original)
    with pytest.raises(NotFoundError):
        kernel.space.get("acme", "u_alice")
    kwargs = dict(
        scope=Scope(org="acme"),
        security=_sec(Scope(org="acme", user="alice")),
        system_metadata={"coords": {}},
    )
    with pytest.raises(AuditIntegrityError):
        if operation == "add":
            kernel.api.add("hello", **kwargs)
        else:
            kernel.api.batch_add([BatchWriteItem(content="hello")], **kwargs)
    with pytest.raises(NotFoundError, match=".*"):
        kernel.space.get("acme", "u_alice")


def test_reserved_request_id_survives_detail_limit(tmp_path):
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kernel = _build_kernel(config=_sqlite_cfg(str(tmp_path / "reserved.sqlite3")))
    token = set_request_id("server-request-123")
    try:
        kernel.api._record_audit(
            Scope(org="acme", user="u1"),
            "probe",
            decision="deny",
            detail={f"k{i}": "v" for i in range(64)},
        )
    finally:
        reset_request_id(token)
    stored = kernel.audit.query({"action": "probe"}, 10)[0]
    assert stored.detail.get("request_id") == "server-request-123", (
        "server-owned request_id appended after 64 keys is discarded"
    )
