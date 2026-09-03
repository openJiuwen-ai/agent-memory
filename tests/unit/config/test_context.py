"""两级命名空间配置：``AssemblyContext`` 解析 + ``ComponentConfig`` 参数回退。

覆盖：简写/内联实例解析、``new_instance``、``lookup`` 缺失报错、顶层段名校验、
缺 target 报错、``globals`` 回退与本实例覆盖。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.types import SecretValue, reveal_secret
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig, RawSpec


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
        AssemblyContext.from_dict({"kvstore": {"k1": "memory"}}, known_top_names={"kv_store"})


def test_instance_missing_target_raises():
    with pytest.raises(ValidationError, match="缺少 'target'"):
        AssemblyContext.from_dict({"kv_store": {"k1": {"params": {"url": "u"}}}})


def test_component_config_param_overrides_global():
    ctx = AssemblyContext.from_dict({"globals": {"embedder_dim": 64}})
    assert ComponentConfig(params={"embedder_dim": 128}, ctx=ctx).get("embedder_dim") == 128
    bare = ComponentConfig(params={}, ctx=ctx)
    assert bare.get("embedder_dim") == 64  # 回退 globals
    assert bare.get("missing", "d") == "d"


def test_secret_params_are_not_printable():
    """AUTH-ENC-03：secret 进 RawSpec 后 repr/str 只给指纹，不落明文。"""
    ctx = AssemblyContext.from_dict(
        {
            "authenticator": {
                "primary": {
                    "target": "api_key",
                    "params": {"root_api_key": "sk-plaintext-root-123"},
                }
            }
        }
    )
    spec = ctx.lookup("authenticator", "primary")
    r = repr(ctx) + repr(spec) + str(spec.params["root_api_key"])
    assert "sk-plaintext-root-123" not in r
    assert repr(spec.params["root_api_key"]).startswith("<SecretValue sha256:")


def test_secret_value_reveal_round_trip():
    """装配边界经 reveal_secret 取回明文；普通字符串与 None 透传（AUTH-ENC-03）。"""
    ctx = AssemblyContext.from_dict(
        {
            "authenticator": {
                "primary": {
                    "target": "api_key",
                    "params": {"root_api_key": "sk-plaintext-root-123"},
                }
            }
        }
    )
    spec = ctx.lookup("authenticator", "primary")
    assert reveal_secret(spec.params["root_api_key"]) == "sk-plaintext-root-123"
    assert reveal_secret("plain") == "plain"
    assert reveal_secret(None) == ""
    assert reveal_secret(SecretValue("")) == ""


def test_secret_in_inline_dependency_is_wrapped():
    """内联依赖里的 secret 同样不落明文：只包第一层会漏掉这条合法配置路径。

    Factory 允许把依赖写成内联 dict（``params: {key_provider: {target: ..., params:
    {...}}}``），嵌套那层照样进 ``RawSpec.params``，也照样出现在 ``repr`` 里。
    """
    ctx = AssemblyContext.from_dict(
        {
            "cryptography": {
                "primary": {
                    "target": "local",
                    "params": {
                        "key_provider": {
                            "target": "local",
                            "params": {"key_hex": "aa" * 32},
                        }
                    },
                }
            }
        }
    )
    spec = ctx.lookup("cryptography", "primary")
    assert "aa" * 32 not in repr(ctx) + repr(spec)
    inner = spec.params["key_provider"]["params"]["key_hex"]
    assert reveal_secret(inner) == "aa" * 32


def test_secret_in_globals_is_wrapped():
    """``globals`` 是另一条合法 secret 路径：``ComponentConfig.get`` 会回退到它取参数。"""
    ctx = AssemblyContext.from_dict({"globals": {"root_api_key": "sk-global-root-999"}})
    assert "sk-global-root-999" not in repr(ctx)
    assert reveal_secret(ctx.globals["root_api_key"]) == "sk-global-root-999"
