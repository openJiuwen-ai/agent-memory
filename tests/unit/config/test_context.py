"""两级命名空间配置：``AssemblyContext`` 解析 + ``ComponentConfig`` 参数回退。

覆盖：简写/内联实例解析、``new_instance``、``lookup`` 缺失报错、顶层段名校验、
缺 target 报错、``globals`` 回退与本实例覆盖、``Config.from_yaml`` 的 ``${VAR}`` 环境变量展开。
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
