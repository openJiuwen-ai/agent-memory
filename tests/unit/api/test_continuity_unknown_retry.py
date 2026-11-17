"""复现：continuity add 写失败后重试不重写、恢复被空 refs 堵死。"""
# pylint: disable=protected-access
import pytest
from jiuwen_memory.api import ContinuityWrite, Scope, TaskRequest
from jiuwen_memory.common.errors import ValidationError
from tests.unit.api.test_continuity_governance import _runtime, _security
from tests.support.scoped_authenticator import ScopedAuthenticator
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role


def test_unknown_receipt_retries_write(tmp_path):
    runtime, api, project, owner, ops, config = _runtime(tmp_path)
    actors = {
        name: Scope(**value["actor"])
        for name, value in config["globals"]["continuity_connectors"].items()
    }
    try:
        task = api.continuity_task(project, TaskRequest("create", "create"), security=_security(owner))
        bound = api.continuity_task(
            project,
            TaskRequest("bind", "workbuddy", task["task_id"], task["revision"],
                        connector_id="workbuddy", context_id="session"),
            security=_security(owner),
        )
        binding_id = bound["task"]["bindings"][-1]["binding_id"]
        continuity = ContinuityWrite(task["task_id"], binding_id, "wb-evt-repro")
        original = api._role_writer
        calls = {"n": 0}

        def failing_writer(scope, security, **_kwargs):
            def write(content, metadata):
                calls["n"] += 1
                raise OSError("content store unavailable")
            return write

        def ok_writer(scope, security, **_kwargs):
            def write(content, metadata):
                calls["n"] += 1
                return original(scope, security, **_kwargs)(content, metadata)
            return write

        # 第一次：写失败
        monkeypatch_fail = None
        api._role_writer = failing_writer
        from jiuwen_memory.api import WriteIncompleteError
        with pytest.raises(WriteIncompleteError):
            api.add("lost", project, security=_security(actors["workbuddy"]), continuity=continuity)
        assert calls["n"] == 1, calls

        # 第二次：恢复写入能力后重试同一事件
        api._role_writer = ok_writer
        result = api.add("lost", project, security=_security(actors["workbuddy"]), continuity=continuity)
        print("RETRY calls:", calls["n"], "units:", len(result))
        # 期望重试真的重写（write 被再次调用）并返回落盘的 unit
        assert calls["n"] == 2, f"重试未重写 write，calls={calls['n']}"
        assert len(result) >= 1, f"重试未落盘 unit，result={result}"
    finally:
        runtime.close()
