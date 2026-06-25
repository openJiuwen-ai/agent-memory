"""两级命名空间配置：``AssemblyContext`` 解析 + ``ComponentConfig`` 参数回退。

覆盖：简写/内联实例解析、``new_instance``、``lookup`` 缺失报错、顶层段名校验、
缺 target 报错、``globals`` 回退与本实例覆盖。
"""

from __future__ import annotations

import pytest

from common.errors import ValidationError
from config.context import AssemblyContext, ComponentConfig, RawSpec


def test_parse_shorthand_and_inline():
    ctx = AssemblyContext.from_dict(
        {
            "globals": {"embedder_dim": 64},
            "kv_store": {
                "k1": "memory",  # 简写：name: target
                "k2": {  # 内联：target + params + new_instance
                    "target": "redis",
                    "params": {"url": "u"},
                    "new_instance": True,
                },
            },
        }
    )
    assert ctx.globals["embedder_dim"] == 64
    assert ctx.lookup("kv_store", "k1") == RawSpec(target="memory")
    k2 = ctx.lookup("kv_store", "k2")
    assert k2.target == "redis"
    assert k2.params["url"] == "u"
    assert k2.new_instance is True


def test_lookup_missing_raises():
    ctx = AssemblyContext.from_dict({"kv_store": {"k1": "memory"}})
    with pytest.raises(ValidationError, match="引用的具名配置不存在"):
        ctx.lookup("kv_store", "nope")


def test_unknown_top_name_raises_when_validated():
    with pytest.raises(ValidationError, match="未知的顶层配置段"):
        AssemblyContext.from_dict(
            {"kvstore": {"k1": "memory"}}, known_top_names={"kv_store"}
        )


def test_instance_missing_target_raises():
    with pytest.raises(ValidationError, match="缺少 'target'"):
        AssemblyContext.from_dict({"kv_store": {"k1": {"params": {"url": "u"}}}})


def test_component_config_param_overrides_global():
    ctx = AssemblyContext.from_dict({"globals": {"embedder_dim": 64}})
    assert ComponentConfig(params={"embedder_dim": 128}, ctx=ctx).get("embedder_dim") == 128
    bare = ComponentConfig(params={}, ctx=ctx)
    assert bare.get("embedder_dim") == 64  # 回退 globals
    assert bare.get("missing", "d") == "d"
