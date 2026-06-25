"""配置层加载：读一层 YAML/JSON 文件并展开 ``${VAR}`` / ``${VAR:-默认值}``。

由 HTTP / MCP 两个 surface 共享——连接串与密钥经环境变量注入，配置文件本身不落密。
（CLI 直接传 JSON 层，不经此模块。）
"""

from __future__ import annotations

import json
import os
import re

# ${VAR} 或 ${VAR:-默认值}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _sub_env(m: "re.Match[str]") -> str:
    name, default = m.group(1), m.group(2)
    return os.environ.get(name, default if default is not None else "")


def expand_env(obj):
    """递归把字符串叶子里的 ``${VAR}`` / ``${VAR:-默认}`` 用环境变量展开。"""
    if isinstance(obj, str):
        return _ENV_RE.sub(_sub_env, obj)
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
