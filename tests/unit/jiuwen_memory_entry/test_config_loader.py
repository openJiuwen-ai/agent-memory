# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""接入层 ``load_layer`` 的文本解析与环境变量展开，并与内核 SDK 入口做 parity 校验。

两条路径（HTTP/MCP 的 ``load_layer``、SDK 的 ``Config.from_yaml``）必须给出同样的展开
结果与同样的畸形占位符报错，否则 #199 报的「两条路径语义不一致」会重新出现。
"""

from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.config import Config
from jiuwen_memory_entry.core import config_loader

pytestmark = pytest.mark.unit

_CONFIG_YAML = """
asr:
  video:
    target: dashscope_filetrans
    params:
      asr_model: ${ENTRY_MODEL}
      asr_base_url: ${ENTRY_URL:-http://127.0.0.1:8000/v1}
      asr_api_key: ${ENTRY_KEY}
      chunk_seconds: 600
      tags: ["${ENTRY_MODEL}", fixed]
""".strip()


@pytest.fixture
def entry_env(monkeypatch):
    monkeypatch.setenv("ENTRY_MODEL", "qwen3-asr")
    monkeypatch.delenv("ENTRY_URL", raising=False)
    monkeypatch.delenv("ENTRY_KEY", raising=False)


def _write(tmp_path, text: str, name: str = "config.yml") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_layer_expands_yaml_env(tmp_path, entry_env) -> None:
    params = config_loader.load_layer(_write(tmp_path, _CONFIG_YAML))["asr"]["video"]["params"]

    assert params["asr_model"] == "qwen3-asr", "环境变量注入"
    assert params["asr_base_url"] == "http://127.0.0.1:8000/v1", "未设置时回落 :- 默认值"
    assert params["asr_api_key"] == "", "未设置且无默认值时展开为空串"
    assert params["chunk_seconds"] == 600, "非字符串叶子不动"
    assert params["tags"] == ["qwen3-asr", "fixed"], "列表叶子同样展开"


def test_load_layer_expands_json_layer(tmp_path, entry_env) -> None:
    text = json.dumps({"llm": {"default": {"params": {"url": "${ENTRY_URL:-http://x/v1}"}}}})

    layer = config_loader.load_layer(_write(tmp_path, text, name="config.json"))

    assert layer["llm"]["default"]["params"]["url"] == "http://x/v1"


def _value_yaml(placeholder: str) -> str:
    return f'llm:\n  default:\n    target: openai\n    params:\n      v: "{placeholder}"\n'


def test_load_layer_rejects_nested_placeholder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ENTRY_SET", "value")
    monkeypatch.delenv("ENTRY_MISSING", raising=False)

    # 变量有值 → 默认值被截断到第一个 } ，花括号不配对。
    with pytest.raises(ValidationError, match="花括号不配对"):
        config_loader.load_layer(_write(tmp_path, _value_yaml("${ENTRY_SET:-${ENTRY_OTHER}}")))

    # 变量缺失 → 展开后残留一个看起来合法的占位符。
    with pytest.raises(ValidationError, match="未解析的"):
        config_loader.load_layer(_write(tmp_path, _value_yaml("${ENTRY_MISSING:-${ENTRY_OTHER}}")))


def test_load_layer_rejects_default_containing_brace(tmp_path) -> None:
    with pytest.raises(ValidationError, match="默认值不能包含"):
        config_loader.load_layer(_write(tmp_path, _value_yaml("${ENTRY_MISSING:-a}b}")))


def test_load_layer_leaves_plain_braces_untouched(tmp_path) -> None:
    layer = config_loader.load_layer(_write(tmp_path, _value_yaml("pass}word{mix")))

    assert layer["llm"]["default"]["params"]["v"] == "pass}word{mix", "无占位符即不校验"


def test_entry_and_kernel_paths_agree(tmp_path, entry_env) -> None:
    """parity：同一份文本 + 同一组环境变量，两条路径结果必须完全相同。"""
    path = _write(tmp_path, _CONFIG_YAML)

    assert Config.from_dict(config_loader.load_layer(path)).context() == Config.from_yaml(
        path
    ).context(), "两条路径的解析与展开结果必须一致"


def test_entry_and_kernel_reject_same_malformed_input(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ENTRY_MISSING", raising=False)
    text = _value_yaml("${ENTRY_MISSING:-${ENTRY_OTHER}}")
    path = _write(tmp_path, text)

    with pytest.raises(ValidationError):
        config_loader.load_layer(path)
    with pytest.raises(ValidationError):
        Config.from_yaml_str(text)
    with pytest.raises(ValidationError):
        Config.from_yaml(path)


def test_validation_error_is_the_same_class() -> None:
    """接入层不得为了绕开边界铁律而自造异常类型，否则调用方无法统一 except。"""
    assert config_loader.ValidationError is ValidationError
