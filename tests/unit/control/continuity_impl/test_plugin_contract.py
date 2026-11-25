"""record_batch 与任务变更的插件契约（S09 §14）。"""

import inspect

import pytest

from jiuwen_memory.api.memory_api_impl.continuity_ops import _call_record_batch
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.control.continuity import Continuity, require_task_connector_policy

pytestmark = pytest.mark.unit

_ARGS = ("scope", "events", "connectors", "read", "write")


class _LegacyPlugin:
    """按旧契约覆写：签名里没有 continue_on_error。"""

    def __init__(self):
        self.calls: list[dict] = []

    def record_batch(self, scope, events, connectors, read, write_batch, *,
                     request_fingerprints=None, query_output=None):
        self.calls.append({"request_fingerprints": request_fingerprints})
        return ["legacy"]


class _ModernPlugin:
    """按新契约覆写。"""

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def record_batch(self, scope, events, connectors, read, write_batch, *,
                     request_fingerprints=None, query_output=None,
                     continue_on_error=False):
        self.calls.append({"continue_on_error": continue_on_error})
        if self.fail:
            # 内部组件误传同名参数：消息与「旧签名」完全一致，但整批可能已写入。
            raise TypeError(
                "inner() got an unexpected keyword argument 'continue_on_error'"
            )
        return ["modern"]


class _VarKwargsPlugin:
    def __init__(self):
        self.calls: list[dict] = []

    def record_batch(self, scope, events, connectors, read, write_batch, **kwargs):
        self.calls.append(dict(kwargs))
        return []


def test_base_contract_accepts_continue_on_error():
    parameters = inspect.signature(Continuity.record_batch).parameters
    assert "continue_on_error" in parameters
    assert parameters["continue_on_error"].default is False


def test_legacy_plugin_is_called_exactly_once_without_the_keyword():
    plugin = _LegacyPlugin()
    assert _call_record_batch(plugin, _ARGS, {}, stop_on_error=False) == ["legacy"]
    assert len(plugin.calls) == 1
    assert "continue_on_error" not in plugin.calls[0]


def test_modern_plugin_receives_the_keyword_and_runs_once():
    plugin = _ModernPlugin()
    assert _call_record_batch(plugin, _ARGS, {}, stop_on_error=True) == ["modern"]
    assert plugin.calls == [{"continue_on_error": False}]


def test_var_keyword_plugin_is_treated_as_new_contract():
    plugin = _VarKwargsPlugin()
    _call_record_batch(plugin, _ARGS, {}, stop_on_error=False)
    assert len(plugin.calls) == 1
    assert plugin.calls[0]["continue_on_error"] is True


class _TaskContractPlugin(Continuity):
    """Modern task contract; the remaining abstract methods are irrelevant stubs."""

    def health(self):
        return {}

    def task(self, scope, request, owner, read, *, connector_policy):
        return {}

    def task_owner(self, scope, task_id):
        return ""

    def binding_connector(self, scope, task_id, binding_id):
        return ""

    def record(self, scope, event, connector, read, write, *,
               query_output=None, request_fingerprint=""):
        return {}

    def relation(self, *args, **kwargs):
        raise NotImplementedError

    def relation_current(self, *args, **kwargs):
        raise NotImplementedError

    def validate_resolution(self, *args, **kwargs):
        raise NotImplementedError

    def view(self, *args, **kwargs):
        raise NotImplementedError

    def repair(self, *args, **kwargs):
        raise NotImplementedError

    def recovery_owner(self, *args, **kwargs):
        raise NotImplementedError

    def recover(self, *args, **kwargs):
        raise NotImplementedError


def test_continuity_task_contract_requires_connector_policy():
    require_task_connector_policy(_TaskContractPlugin(), "modern")

    class _LegacyTaskPlugin(_TaskContractPlugin):
        def task(self, scope, request, owner, read):
            return {}

    with pytest.raises(ValidationError, match="connector_policy"):
        require_task_connector_policy(_LegacyTaskPlugin(), "legacy")

    class _VarKwargsTaskPlugin(_TaskContractPlugin):
        def task(self, *args, **kwargs):
            return {}

    require_task_connector_policy(_VarKwargsTaskPlugin(), "kwargs")


def test_matching_error_from_a_modern_plugin_is_not_retried():
    """错误文案与旧签名一致时也不能重试：整批可能已经写入一部分。"""
    plugin = _ModernPlugin(fail=True)
    with pytest.raises(TypeError, match="unexpected keyword"):
        _call_record_batch(plugin, _ARGS, {}, stop_on_error=False)
    assert len(plugin.calls) == 1, "重试会重复执行整批写入"


class _PositionalOnlyPlugin:
    """同名参数但仅限位置：按关键字传入会 TypeError。"""

    def __init__(self):
        self.calls: list[bool] = []

    def record_batch(self, scope, events, connectors, read, write_batch,
                     continue_on_error=False, /):
        self.calls.append(continue_on_error)
        return ["positional-only"]


def test_positional_only_parameter_is_not_treated_as_keyword_support():
    from jiuwen_memory.api.memory_api_impl.continuity_ops import _accepts_continue_on_error

    assert _accepts_continue_on_error(_PositionalOnlyPlugin) is False
    plugin = _PositionalOnlyPlugin()
    assert _call_record_batch(plugin, _ARGS, {}, stop_on_error=False) == ["positional-only"]
    # 未按关键字传：插件用自己的默认值，且只调用一次。
    assert plugin.calls == [False]
