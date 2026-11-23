"""契约回归：只实现 ``record`` 的插件走基类 ``record_batch`` 回退路径。"""

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import ContinuityEvent
from jiuwen_memory.control.continuity import Continuity

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="project")


class _RecordOnlyPlugin(Continuity):
    """第三方插件的典型形态：只实现 ``record``，不覆盖 ``record_batch``。"""

    def __init__(self, failing: set[str]):
        self.failing = failing
        self.records: list[str] = []

    # -- 回退路径需要的两个入口 ------------------------------------------ #
    def record(
        self, scope, event, connector, read, write, *, query_output=None,
        request_fingerprint="",
    ):
        self.records.append(event.event_id)
        if event.event_id in self.failing:
            raise ConflictError("event", message="event ID reused with different content")
        units = write(event.content, {})
        return {
            "status": "committed",
            "operation_id": self.event_key(connector, event.binding_id, event.event_id),
            "refs": [MemoryUnit(id=unit.id, scope=scope) for unit in units],
        }

    # -- 其余抽象方法（本用例不走）-------------------------------------- #
    def health(self):
        return {}

    def binding_connector(self, scope, task_id, binding_id):
        return "conn"

    def recover(self, *args, **kwargs):
        raise NotImplementedError

    def recovery_owner(self, *args, **kwargs):
        raise NotImplementedError

    def relation(self, *args, **kwargs):
        raise NotImplementedError

    def relation_current(self, *args, **kwargs):
        raise NotImplementedError

    def repair(self, *args, **kwargs):
        raise NotImplementedError

    def task(self, *args, **kwargs):
        raise NotImplementedError

    def task_owner(self, *args, **kwargs):
        raise NotImplementedError

    def validate_resolution(self, *args, **kwargs):
        raise NotImplementedError

    def view(self, *args, **kwargs):
        raise NotImplementedError


def _run(plugin, *, continue_on_error):
    events = [
        ContinuityEvent("t", "b", "e-1", "first"),
        ContinuityEvent("t", "b", "e-2", "second"),
    ]

    def write_batch(items):
        return [
            [MemoryUnit(id=f"u-{index}", scope=_SCOPE, segments=[Segment(content=item.content)])]
            for index, item in enumerate(items)
        ]

    return plugin.record_batch(
        _SCOPE, events, ["conn", "conn"], lambda ref: None, write_batch,
        continue_on_error=continue_on_error,
    )


def test_fallback_accepts_continue_on_error_keyword():
    """基类签名必须接受该参数，否则只实现 record 的插件一调用 batch_add 就 TypeError。"""
    results = _run(_RecordOnlyPlugin(failing=set()), continue_on_error=True)
    assert [result["status"] for result in results] == ["committed", "committed"]


def test_fallback_rejects_only_the_failing_item_when_continuing():
    results = _run(_RecordOnlyPlugin(failing={"e-1"}), continue_on_error=True)
    assert results[0]["status"] == "rejected"
    assert isinstance(results[0]["error"], ConflictError)
    assert results[1]["status"] == "committed"


def test_fallback_strict_mode_returns_rejected_and_skipped():
    """严格模式与内置实现一致：失败项 rejected、后续项 skipped，不抛错。

    抛错会让调用方把整段（含已经写好的前缀）都记成同一个错误——前缀其实写成功了。
    """
    results = _run(_RecordOnlyPlugin(failing={"e-2"}), continue_on_error=False)
    assert results[0]["status"] == "committed", "失败项之前的项必须如实报告为成功"
    assert results[1]["status"] == "rejected"
    assert isinstance(results[1]["error"], ConflictError)


def test_fallback_strict_mode_skips_everything_after_the_failure():
    events = [
        ContinuityEvent("t", "b", "e-1", "first"),
        ContinuityEvent("t", "b", "e-2", "second"),
        ContinuityEvent("t", "b", "e-3", "third"),
    ]

    def write_batch(items):
        return [
            [MemoryUnit(id=f"u-{index}", scope=_SCOPE, segments=[Segment(content=i.content)])]
            for index, i in enumerate(items)
        ]

    results = _RecordOnlyPlugin(failing={"e-1"}).record_batch(
        _SCOPE, events, ["conn"] * 3, lambda ref: None, write_batch,
        continue_on_error=False,
    )
    assert [result["status"] for result in results] == ["rejected", "skipped", "skipped"]
    assert "error" not in results[1]


def test_fallback_treats_missing_reference_as_item_failure():
    """证据引用读不到（NotFoundError）也属单条失败，不能让整批失败。"""

    class _MissingRefPlugin(_RecordOnlyPlugin):
        def record(self, scope, event, connector, read, write, **kwargs):
            if event.event_id == "e-1":
                raise NotFoundError("memory", "missing")
            return super().record(scope, event, connector, read, write, **kwargs)

    results = _run(_MissingRefPlugin(failing=set()), continue_on_error=True)
    assert results[0]["status"] == "rejected"
    assert isinstance(results[0]["error"], NotFoundError)
    assert results[1]["status"] == "committed"
