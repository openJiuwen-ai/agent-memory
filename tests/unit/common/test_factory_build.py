"""Factory 新装配接口：接口 1 匿名 ``build`` / 接口 2 具名 ``build_named`` / 取依赖 ``dep``。

覆盖：TOP_NAME 注册、匿名不缓存、具名默认共享、``new_instance`` 每次新建不缓存、
``dep`` 三分派（引用共享 / 内联匿名 / 缺省匿名）、缺省且无默认报错。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig


class _PartProducer(Factory):
    TOP_NAME = "part"


class _HolderProducer(Factory):
    TOP_NAME = "holder"


class _Part:
    def __init__(self, size: int = 0) -> None:
        self.size = size


class _Holder:
    def __init__(self, part: _Part) -> None:
        self.part = part


@_PartProducer.register("simple")
def _build_part(config):
    return _Part(config.get("size", 0))


@_HolderProducer.register("h")
def _build_holder(config):
    # 字段名默认取 _PartProducer.TOP_NAME == "part"
    return _Holder(_PartProducer.dep(config, default="simple"))


@pytest.fixture(autouse=True)
def _reset():
    Factory.reset_all()
    yield
    Factory.reset_all()


def test_top_name_registered():
    names = Factory.known_top_names()
    assert {"part", "holder"} <= names


def test_build_anonymous_not_cached():
    ctx = AssemblyContext.from_dict({})
    a = _PartProducer.build("simple", {"size": 1}, ctx)
    b = _PartProducer.build("simple", {"size": 1}, ctx)
    assert a is not b
    assert a.size == 1
    assert _PartProducer.get_cached("", ctx=ctx) is None  # 匿名不入缓存


def test_build_named_shared_by_default():
    ctx = AssemblyContext.from_dict({"part": {"p1": {"target": "simple", "params": {"size": 5}}}})
    a = _PartProducer.build_named("p1", ctx)
    b = _PartProducer.build_named("p1", ctx)
    assert a is b
    assert a.size == 5


def test_build_named_new_instance_not_shared():
    ctx = AssemblyContext.from_dict({"part": {"p1": {"target": "simple", "new_instance": True}}})
    a = _PartProducer.build_named("p1", ctx)
    b = _PartProducer.build_named("p1", ctx)
    assert a is not b
    assert _PartProducer.get_cached("p1", ctx=ctx) is None


def test_dep_reference_shares_named_instance():
    ctx = AssemblyContext.from_dict(
        {
            "part": {"shared": {"target": "simple", "params": {"size": 9}}},
            "holder": {
                "h1": {"target": "h", "params": {"part": "shared"}},
                "h2": {"target": "h", "params": {"part": "shared"}},
            },
        }
    )
    h1 = _HolderProducer.build_named("h1", ctx)
    h2 = _HolderProducer.build_named("h2", ctx)
    assert h1.part is h2.part  # 同名引用 → 共享
    assert h1.part.size == 9


def test_dep_inline_is_anonymous():
    ctx = AssemblyContext.from_dict(
        {
            "holder": {
                "h1": {
                    "target": "h",
                    "params": {"part": {"target": "simple", "params": {"size": 3}}},
                }
            }
        }
    )
    h1 = _HolderProducer.build_named("h1", ctx)
    assert h1.part.size == 3
    assert _PartProducer.get_cached("", ctx=ctx) is None  # 内联 = 匿名、不入缓存


def test_dep_default_is_anonymous_independent():
    ctx = AssemblyContext.from_dict({"holder": {"h1": "h", "h2": "h"}})
    h1 = _HolderProducer.build_named("h1", ctx)
    h2 = _HolderProducer.build_named("h2", ctx)
    assert h1.part is not h2.part  # 各自匿名默认 → 不共享
    assert _PartProducer.get_cached("", ctx=ctx) is None


def test_dep_missing_without_default_raises():
    ctx = AssemblyContext.from_dict({})
    cfg_ctx = ctx
    config = ComponentConfig(params={}, ctx=cfg_ctx)
    with pytest.raises(ValidationError, match="未配置且无默认实现"):
        _PartProducer.dep(config)  # 无默认、无配置


def test_creation_observer_reports_nested_and_anonymous_dependencies_not_cache_hits():
    ctx = AssemblyContext.from_dict({"part": {"borrowed": "simple"}})
    borrowed = _PartProducer.build_named("borrowed", ctx)
    _PartProducer.put("injected", borrowed, ctx=ctx)
    created = []
    with Factory.observe_builds(created.append):
        assert _PartProducer.build_named("borrowed", ctx) is borrowed
        assert _PartProducer.build_named("injected", ctx) is borrowed
        holder = _HolderProducer.build("h", {}, ctx)
    assert created == [holder.part, holder]


def test_creation_observer_restores_outer_scope_after_failure():
    outer = []
    inner = []
    ctx = AssemblyContext()
    with Factory.observe_builds(outer.append):
        with pytest.raises(ValidationError):
            with Factory.observe_builds(inner.append):
                first = _PartProducer.build("simple", {}, ctx)
                raise ValidationError("injected observer scope failure")
        second = _PartProducer.build("simple", {}, ctx)
    _PartProducer.build("simple", {}, ctx)
    assert outer == [first, second]
    assert inner == [first]


def test_discard_created_keeps_preexisting_named_instances():
    ctx = AssemblyContext.from_dict({"part": {"borrowed": "simple", "new": "simple"}})
    borrowed = _PartProducer.build_named("borrowed", ctx)
    created = []
    with Factory.observe_builds(created.append):
        original = _PartProducer.build_named("new", ctx)
    Factory.discard_created(tuple(created), ctx=ctx)
    assert _PartProducer.build_named("borrowed", ctx) is borrowed
    assert _PartProducer.build_named("new", ctx) is not original


def test_cache_invalidation_does_not_discard_a_newer_epoch():
    ctx = AssemblyContext.from_dict({"part": {"current": "simple"}})
    old_epoch = Factory.cache_epoch(ctx=ctx)
    Factory.reset_all(ctx=ctx)
    current_epoch = Factory.cache_epoch(ctx=ctx)
    current = _PartProducer.build_named("current", ctx)
    Factory.reset_if_current_epoch(old_epoch, ctx=ctx)
    assert _PartProducer.build_named("current", ctx) is current
    Factory.reset_if_current_epoch(current_epoch, ctx=ctx)
    assert Factory.cache_epoch(ctx=ctx) is not current_epoch
    assert _PartProducer.build_named("current", ctx) is not current


def test_cache_lifecycle_binding_is_explicit_and_resets_with_epoch():
    bindings = []
    left, right = object(), object()

    def share(first, second):
        bindings.append((first, second))

    Factory.bind_cache_lifecycle(left, share)
    Factory.bind_cache_lifecycle(right, share)
    assert bindings == [(left, right)]
    Factory.reset_all()
    Factory.bind_cache_lifecycle(right, share)
    assert bindings == [(left, right)]


def test_independent_contexts_isolate_same_named_configuration_and_reset():
    left = AssemblyContext.from_dict({"part": {"p": {"target": "simple", "params": {"size": 1}}}})
    right = AssemblyContext.from_dict({"part": {"p": {"target": "simple", "params": {"size": 2}}}})
    first = _PartProducer.build_named("p", left)
    second = _PartProducer.build_named("p", right)
    assert first is not second
    assert (first.size, second.size) == (1, 2)
    Factory.reset_all()
    Factory.reset_all(ctx=left)
    assert _PartProducer.build_named("p", right) is second
    assert _PartProducer.build_named("p", left) is not first


def test_concurrent_builds_share_only_within_their_context():
    from concurrent.futures import ThreadPoolExecutor

    ctx = AssemblyContext.from_dict({"part": {"p": "simple"}})
    with ThreadPoolExecutor(max_workers=8) as pool:
        parts = list(pool.map(lambda _: _PartProducer.build_named("p", ctx), range(32)))
    assert all(part is parts[0] for part in parts)


@pytest.mark.unit
def test_registration_scope_restores_builders_and_capability_inspectors_after_error():
    ctx = AssemblyContext.from_dict({})
    config = ComponentConfig({}, ctx, "simple", "test")
    with _PartProducer.registration_scope():
        _PartProducer.register("simple", config_inspectors={"probe": lambda c: "original"})(
            lambda c: _Part(7)
        )
        with pytest.raises(RuntimeError, match="abort"):
            with _PartProducer.registration_scope():
                _PartProducer.register("simple")(lambda c: _Part(99))
                assert _PartProducer.build("simple", {}, ctx).size == 99
                with pytest.raises(ValidationError, match="unknown"):
                    _PartProducer.inspect_config("simple", "probe", config)
                raise RuntimeError("abort")
        assert _PartProducer.build("simple", {}, ctx).size == 7
        assert _PartProducer.inspect_config("simple", "probe", config) == "original"
    assert _PartProducer.build("simple", {}, ctx).size == 0
    with pytest.raises(ValidationError, match="unknown"):
        _PartProducer.inspect_config("simple", "probe", config)
