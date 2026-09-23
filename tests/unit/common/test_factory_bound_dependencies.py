# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""延迟具名依赖不触发未用配置，并在多轮装配/并发首次使用时保持来源与共享。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.config_source import ConfigSourceProducer
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit


class _BoundDependencyProducer(Factory):
    TOP_NAME = "bound_dependency_test"


class _BoundOwnerProducer(Factory):
    TOP_NAME = "bound_owner_test"


@pytest.fixture(autouse=True)
def reset_factory():
    Factory.reset_all()
    yield
    Factory.reset_all()


def context(*, new_instance=False):
    return AssemblyContext.from_dict(
        {
            "bound_dependency_test": {"default": {"target": "part"}},
            "bound_owner_test": {
                "default": {
                    "target": "owner",
                    "new_instance": new_instance,
                    "params": {"bound_dependency_test": "default"},
                }
            },
        }
    )


def test_bind_does_not_lookup_or_validate_until_resolved():
    resolve = _BoundOwnerProducer.bind_named("missing", AssemblyContext())
    with pytest.raises(ValidationError, match="missing"):
        resolve()


def test_bound_dependencies_config_source_and_put_remain_in_original_assembly(monkeypatch):
    _BoundDependencyProducer.register("part")(lambda config: object())

    def build(config):
        item = SimpleNamespace(
            dependency=_BoundDependencyProducer.dep(config),
            source=ConfigSourceProducer.get_cached(),
        )
        _BoundOwnerProducer.put("written_by_builder", item)
        return item

    _BoundOwnerProducer.register("owner")(build)
    original_ctx = context()
    original_source = DictConfigSource({"name": "A"})
    ConfigSourceProducer.put("default", original_source, ctx=original_ctx)
    original = _BoundOwnerProducer.bind_named("default", original_ctx)
    written = _BoundOwnerProducer.bind_named("written_by_builder", original_ctx)
    original_part = _BoundDependencyProducer.bind_named("default", original_ctx)
    Factory.reset_all()
    current_ctx = context()
    current_source = DictConfigSource({"name": "B"})
    ConfigSourceProducer.put("default", current_source, ctx=current_ctx)
    current = _BoundOwnerProducer.build_named("default", current_ctx)
    resolved = original()
    assert resolved.source is original_source
    assert resolved.dependency is original_part()
    assert resolved.dependency is not current.dependency
    assert written() is resolved
    assert ConfigSourceProducer.get_cached(ctx=current_ctx) is current_source
    assert _BoundOwnerProducer.build_named("written_by_builder", current_ctx) is current


def test_failure_restores_caller_context_and_can_retry(monkeypatch):
    original_ctx = context()
    original_source = DictConfigSource({"name": "A"})
    ConfigSourceProducer.put("default", original_source, ctx=original_ctx)
    resolve = _BoundOwnerProducer.bind_named("default", original_ctx)
    Factory.reset_all()
    current_source = DictConfigSource({"name": "B"})
    ConfigSourceProducer.put("default", current_source)

    def fail(config):
        assert ConfigSourceProducer.get_cached() is original_source
        raise ValidationError("expected failure")

    _BoundOwnerProducer.register("owner")(fail)
    with pytest.raises(ValidationError, match="expected failure"):
        resolve()
    assert ConfigSourceProducer.get_cached() is current_source
    _BoundOwnerProducer.register("owner")(lambda config: object())
    assert resolve() is resolve()


def test_concurrent_bound_handles_construct_shared_dependency_once(monkeypatch):
    entered, release, competing = Event(), Event(), Event()
    calls = []

    def build(config):
        calls.append(config)
        entered.set()
        assert release.wait(5), "test must release first constructor"
        return object()

    _BoundOwnerProducer.register("owner")(build)
    ctx = context()
    first = _BoundOwnerProducer.bind_named("default", ctx)
    second = _BoundOwnerProducer.bind_named("default", ctx)
    Factory.reset_all()

    def resolve_second():
        competing.set()
        return second()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first)
        try:
            assert entered.wait(5)
            second_result = pool.submit(resolve_second)
            assert competing.wait(5)
        finally:
            release.set()
        assert first_result.result(timeout=5) is second_result.result(timeout=5)
    assert len(calls) == 1


def test_bound_new_instance_keeps_explicit_nonsharing(monkeypatch):
    _BoundOwnerProducer.register("owner")(lambda config: object())
    resolve = _BoundOwnerProducer.bind_named("default", context(new_instance=True))
    Factory.reset_all()
    assert resolve() is not resolve()


def test_old_bound_build_does_not_expose_its_cache_to_another_thread(monkeypatch):
    original_ctx = context()
    original_source = DictConfigSource({"name": "A"})
    ConfigSourceProducer.put("default", original_source, ctx=original_ctx)
    original = _BoundOwnerProducer.bind_named("default", original_ctx)
    Factory.reset_all()
    current_source = DictConfigSource({"name": "B"})
    ConfigSourceProducer.put("default", current_source)
    current_ctx = context()
    ConfigSourceProducer.put("default", current_source, ctx=current_ctx)
    entered, release = Event(), Event()

    def build(config):
        source = ConfigSourceProducer.get_cached()
        if source is original_source:
            entered.set()
            assert release.wait(5)
        return source

    _BoundOwnerProducer.register("owner")(build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(original)
        try:
            assert entered.wait(5)
            assert ConfigSourceProducer.get_cached() is current_source
            assert _BoundOwnerProducer.build_named("default", current_ctx) is current_source
        finally:
            release.set()
        assert pending.result(timeout=5) is original_source


def test_bound_late_creation_keeps_original_build_observers():
    _BoundOwnerProducer.register("owner")(lambda config: object())
    original, later = [], []
    with Factory.observe_builds(original.append):
        resolve = _BoundOwnerProducer.bind_named("default", context())
    Factory.reset_all()
    with Factory.observe_builds(later.append):
        value = resolve()
    assert original == [value]
    assert later == []


def test_same_named_bound_handles_isolate_independent_contexts():
    _BoundOwnerProducer.register("owner")(lambda config: ConfigSourceProducer.get_cached())
    left_ctx, right_ctx = context(), context()
    left_source = DictConfigSource({"name": "A"})
    right_source = DictConfigSource({"name": "B"})
    ConfigSourceProducer.put("default", left_source, ctx=left_ctx)
    ConfigSourceProducer.put("default", right_source, ctx=right_ctx)
    left = _BoundOwnerProducer.bind_named("default", left_ctx)
    right = _BoundOwnerProducer.bind_named("default", right_ctx)
    Factory.reset_all()
    assert right() is right_source
    assert left() is left_source
    assert right() is right_source
