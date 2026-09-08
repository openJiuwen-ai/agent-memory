"""两级命名空间配置：``AssemblyContext`` 解析 + ``ComponentConfig`` 参数回退。

覆盖：简写/内联实例解析、``new_instance``、``lookup`` 缺失报错、顶层段名校验、
缺 target 报错、``globals`` 回退与本实例覆盖、``Config.from_yaml`` / ``from_yaml_str`` 的
``${VAR}`` 展开与畸形占位符校验。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config import Config
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


def test_from_yaml_expands_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "asr:\n"
        "  video:\n"
        "    target: dashscope_filetrans\n"
        "    params:\n"
        "      asr_api_key: ${UNIT_TEST_KEY}\n"
        "      asr_base_url: ${UNIT_TEST_URL:-http://127.0.0.1:8000/v1}\n"
        "      asr_missing: ${UNIT_TEST_MISSING}\n"
        "      asr_chunk_seconds: 600\n"
        '      asr_tags: ["${UNIT_TEST_KEY}", fixed]\n'
        '      dsn: "postgresql://${UNIT_TEST_USER:-agent_memory}:${UNIT_TEST_KEY}@db:5432/app"\n'
        '      literal: "pass$word and 100% and ${NOT_A_PLACEHOLDER"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UNIT_TEST_KEY", "secret-key")

    spec = Config.from_yaml(str(cfg_path)).context().lookup("asr", "video")

    assert spec.params["asr_api_key"] == "secret-key"
    assert spec.params["asr_base_url"] == "http://127.0.0.1:8000/v1", "未设置时回落 :- 默认值"
    assert spec.params["asr_missing"] == "", "未设置且无默认值时展开为空串"
    assert spec.params["asr_chunk_seconds"] == 600, "非字符串叶子不动"
    assert spec.params["asr_tags"] == ["secret-key", "fixed"], "列表叶子同样展开"
    assert (
        spec.params["dsn"] == "postgresql://agent_memory:secret-key@db:5432/app"
    ), "同一字符串内多个占位符各自展开（默认值与环境变量混排）"
    assert (
        spec.params["literal"] == "pass$word and 100% and ${NOT_A_PLACEHOLDER"
    ), "非法占位符原样保留"


def test_from_dict_does_not_expand_env():
    config = Config.from_dict(
        {"llm": {"default": {"target": "openai", "params": {"llm_api_key": "${UNIT_TEST_KEY}"}}}}
    )
    assert (
        config.context().lookup("llm", "default").params["llm_api_key"] == "${UNIT_TEST_KEY}"
    ), "纯数据入口不展开"


_YAML_WITH_ENV = (
    "llm:\n"
    "  default:\n"
    "    target: openai\n"
    "    params:\n"
    "      llm_api_key: ${UNIT_TEST_KEY}\n"
    "      llm_base_url: ${UNIT_TEST_URL:-http://127.0.0.1:8000/v1}\n"
)


def _llm_params(config: Config) -> dict:
    return config.context().lookup("llm", "default").params


def test_from_yaml_str_resolves_args_before_env(monkeypatch):
    monkeypatch.setenv("UNIT_TEST_KEY", "from-env")
    monkeypatch.setenv("UNIT_TEST_URL", "http://from-env:8000/v1")

    params = _llm_params(
        Config.from_yaml_str(_YAML_WITH_ENV, UNIT_TEST_KEY="from-args")
    )

    assert params["llm_api_key"] == "from-args", "args 里的同名值优先于环境变量"
    assert params["llm_base_url"] == "http://from-env:8000/v1", "args 未提供时回落环境变量"


def test_from_yaml_str_falls_back_to_default_then_empty(monkeypatch):
    monkeypatch.delenv("UNIT_TEST_KEY", raising=False)
    monkeypatch.delenv("UNIT_TEST_URL", raising=False)

    params = _llm_params(Config.from_yaml_str(_YAML_WITH_ENV))

    assert params["llm_api_key"] == "", "无 args、无环境变量、无默认值时展开为空串"
    assert params["llm_base_url"] == "http://127.0.0.1:8000/v1", "都没有时回落 :- 默认值"

    args_only = _llm_params(Config.from_yaml_str(_YAML_WITH_ENV, UNIT_TEST_KEY="from-args"))
    assert args_only["llm_api_key"] == "from-args", "args 对无默认值的占位符生效"


def test_from_yaml_matches_from_yaml_str(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIT_TEST_KEY", "from-env")
    path = tmp_path / "config.yml"
    path.write_text(_YAML_WITH_ENV, encoding="utf-8")

    assert _llm_params(Config.from_yaml(str(path))) == _llm_params(
        Config.from_yaml_str(_YAML_WITH_ENV)
    ), "读文件与读文本结果一致"


def _leaf(value: str) -> str:
    """把一个标量值塞进 YAML 文本，经 from_yaml_str 取回展开结果。"""
    yaml_text = f"llm:\n  default:\n    target: openai\n    params:\n      v: \"{value}\"\n"
    return _llm_params(Config.from_yaml_str(yaml_text))["v"]


def test_nested_placeholder_rejected_regardless_of_env(monkeypatch):
    monkeypatch.setenv("UNIT_TEST_KEY", "secret-key")
    monkeypatch.delenv("UNIT_TEST_URL", raising=False)

    # 变量有值：正则会把默认值截断到第一个 } ，产出 "secret-key}" 这类错值 → 花括号不配对。
    with pytest.raises(ValidationError, match="花括号不配对"):
        _leaf("${UNIT_TEST_KEY:-${UNIT_TEST_URL}}")

    # 变量缺失：展开后残留一个看起来合法的 ${UNIT_TEST_URL} → 残留占位符。
    with pytest.raises(ValidationError, match="未解析的"):
        _leaf("${UNIT_TEST_MISSING:-${UNIT_TEST_URL}}")


def test_default_value_with_brace_rejected():
    with pytest.raises(ValidationError, match="默认值不能包含"):
        _leaf("${UNIT_TEST_MISSING:-a}b}")


def test_plain_leaf_with_brace_is_untouched():
    assert _leaf("pass}word{and{mix") == "pass}word{and{mix", "无占位符即不校验"
    assert _leaf("no-placeholder") == "no-placeholder"


def test_none_override_falls_through(monkeypatch):
    monkeypatch.setenv("UNIT_TEST_KEY", "from-env")

    # None 视同未传：有环境变量时取环境变量，绝不产出字符串 "None"。
    params = _llm_params(Config.from_yaml_str(_YAML_WITH_ENV, UNIT_TEST_KEY=None))
    assert params["llm_api_key"] == "from-env"

    monkeypatch.delenv("UNIT_TEST_KEY", raising=False)
    params = _llm_params(Config.from_yaml_str(_YAML_WITH_ENV, UNIT_TEST_KEY=None))
    assert params["llm_api_key"] == "", "None 且无环境变量时按未设置处理"


def test_non_string_override_is_stringified():
    params = _llm_params(Config.from_yaml_str(_YAML_WITH_ENV, UNIT_TEST_KEY=128))
    assert params["llm_api_key"] == "128", "标量按 str() 落进字符串叶子"


def test_from_yaml_rejects_malformed_placeholder(tmp_path):
    yaml_text = "llm:\n  default:\n    target: openai\n    params:\n      v: \"${A:-${B}}\"\n"
    path = tmp_path / "config.yml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValidationError, match="不支持嵌套占位符"):
        Config.from_yaml(str(path))
    with pytest.raises(ValidationError, match="不支持嵌套占位符"):
        Config.from_yaml_str(yaml_text)
