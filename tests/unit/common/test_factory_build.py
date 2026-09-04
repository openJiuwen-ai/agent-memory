"""Factory 新装配接口：接口 1 匿名 ``build`` / 接口 2 具名 ``build_named`` / 取依赖 ``dep``。

覆盖：TOP_NAME 注册、匿名不缓存、具名默认共享、``new_instance`` 每次新建不缓存、
``dep`` 三分派（引用共享 / 内联匿名 / 缺省匿名）、缺省且无默认报错。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.context import AssemblyContext


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
    assert not getattr(_PartProducer, "_instances")  # 匿名不入缓存


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
    assert "p1" not in getattr(_PartProducer, "_instances")


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
    assert not getattr(_PartProducer, "_instances")  # 内联 = 匿名、不入缓存


def test_dep_default_is_anonymous_independent():
    ctx = AssemblyContext.from_dict({"holder": {"h1": "h", "h2": "h"}})
    h1 = _HolderProducer.build_named("h1", ctx)
    h2 = _HolderProducer.build_named("h2", ctx)
    assert h1.part is not h2.part  # 各自匿名默认 → 不共享
    assert not getattr(_PartProducer, "_instances")


def test_dep_missing_without_default_raises():
    ctx = AssemblyContext.from_dict({})
    cfg_ctx = ctx
    from jiuwen_memory.config.context import ComponentConfig

    config = ComponentConfig(params={}, ctx=cfg_ctx)
    with pytest.raises(ValidationError, match="未配置且无默认实现"):
        _PartProducer.dep(config)  # 无默认、无配置
