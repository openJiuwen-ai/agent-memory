# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""配置层加载：读一层 YAML/JSON 文件并展开 ``${VAR}`` / ``${VAR:-默认值}``。

由 HTTP / MCP 两个 surface 共享——连接串与密钥经环境变量注入，配置文件本身不落密。
（CLI 直接传 JSON 层，不经此模块。）

展开规则与 SDK 侧 ``Config.from_yaml`` 保持一致（见 ``jiuwen_memory/config/config.py``）：
只支持单层占位符，嵌套写法与含 ``}`` 的默认值报错而非静默产出错值。两处实现由
``tests/unit/jiuwen_memory_entry/test_config_loader.py`` 的 parity 用例锁住不漂移。
"""

from __future__ import annotations

import json
import os
import re

from jiuwen_memory_entry.core.import_support import import_required_attr

# 公开面白名单内的错误类型：与内核抛出的是同一个类，调用方可统一 except。
ValidationError = import_required_attr("jiuwen_memory.api", "ValidationError")

# ${VAR} 或 ${VAR:-默认值}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_PLACEHOLDER_RULE = (
    "只支持单层 ${VAR} 与 ${VAR:-默认值}，不支持嵌套占位符，也没有转义写法"
)


def _sub_env(m: "re.Match[str]") -> str:
    name, default = m.group(1), m.group(2)
    return os.environ.get(name, default if default is not None else "")


def _expand_str(text: str) -> str:
    """展开单个字符串叶子，并拦截正则无法正确表达的畸形写法。"""
    if _ENV_RE.search(text) is None:
        return text
    names = "、".join(sorted({match.group(1) for match in _ENV_RE.finditer(text)}))
    expanded = _ENV_RE.sub(_sub_env, text)
    if _ENV_RE.search(expanded):
        raise ValidationError(
            f"配置值里的占位符（{names}）展开后仍有未解析的 ${{...}}：{_PLACEHOLDER_RULE}"
        )
    if expanded.count("{") != expanded.count("}"):
        raise ValidationError(
            f"配置值里的占位符（{names}）展开后花括号不配对：默认值不能包含 '}}'"
            f"（{_PLACEHOLDER_RULE}）"
        )
    return expanded


def expand_env(obj):
    """递归把字符串叶子里的 ``${VAR}`` / ``${VAR:-默认}`` 用环境变量展开。"""
    if isinstance(obj, str):
        return _expand_str(obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    return obj


def load_layer(path: str) -> dict:
    """读一层配置文件：``.yml/.yaml`` 走 YAML，其余按 JSON；读后做环境变量展开。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yml", ".yaml")):
        import yaml  # 部署镜像已装 PyYAML（见 pyproject 的 deploy extra）

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    return expand_env(data)
