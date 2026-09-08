# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Config — 装配配置的门面：把 YAML/字典解析成 :class:`AssemblyContext`（两级命名空间，纯数据）。

配置顶层是一个可选的 ``globals``（跨切面参数）+ 若干**命名空间**（每个对应一个 Producer 的
``TOP_NAME``），命名空间下是若干**具名实例**（``target`` / ``params`` / ``new_instance``）::

    globals:
      embedder_dim: 64
    kv_store:                 # KvProducer.TOP_NAME
      main_kv: { target: redis, params: { url: "..." } }
    constructor:              # IndexBuilderProducer.TOP_NAME
      main_ib:
        target: hybrid
        params: { vector_store: main_vec }   # 字符串=引用；映射=内联匿名

本模块只解析成纯数据；「装哪些、怎么串、共享谁」由各 ``Producer`` 经 ``build_named`` / ``dep``
顺着引用落地（见 :mod:`config.context` 与 ``api.build_kernel``）。用户配置会被
``build_kernel`` **合并覆盖**到内置默认（:mod:`config.defaults`）之上。

读配置文本的 :meth:`Config.from_yaml` / :meth:`Config.from_yaml_str` 会把字符串叶子里的
``${VAR}`` / ``${VAR:-默认值}`` 展开（连接串与密钥经环境变量注入，配置文件本身不落密）；
``from_yaml_str`` 的 ``${VAR}`` 优先取调用方传入的同名参数，其次才取环境变量。
:meth:`Config.from_dict` 是纯数据入口，不做展开。
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

from .context import AssemblyContext

# ${VAR} 或 ${VAR:-默认值}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve_env(match: "re.Match[str]", overrides: Mapping[str, Any]) -> str:
    name, default = match.group(1), match.group(2)
    if name in overrides:
        return str(overrides[name])
    return os.environ.get(name, default if default is not None else "")


def _expand_env(obj: Any, overrides: Mapping[str, Any] | None = None) -> Any:
    """递归把字符串叶子里的 ``${VAR}`` / ``${VAR:-默认}`` 展开。

    取值优先级：``overrides``（调用方显式传参）> 环境变量 > ``:-`` 默认值 > 空串。
    """
    overrides = overrides or {}
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: _resolve_env(m, overrides), obj)
    if isinstance(obj, dict):
        return {key: _expand_env(value, overrides) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(value, overrides) for value in obj]
    return obj


class Config:
    """一次装配的用户配置（两级命名空间字典，解析后只读）。"""

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        self._data = dict(data or {})

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Config":
        """从配置字典构造。"""
        return cls(data or {})

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """从 YAML 文件解析配置，并展开 ``${VAR}`` / ``${VAR:-默认值}``。"""
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_yaml_str(fh.read())

    @classmethod
    def from_yaml_str(cls, yaml_str: str, **args: Any) -> "Config":
        """从 YAML 文本解析配置，并展开 ``${VAR}`` / ``${VAR:-默认值}``。

        ``args`` 按变量名提供覆盖值，优先级高于环境变量：
        ``Config.from_yaml_str(text, DASHSCOPE_API_KEY="sk-...")``。
        """
        return cls.from_dict(_expand_env(_load_yaml(yaml_str), args))

    def context(self, known_top_names: Optional[set] = None) -> AssemblyContext:
        """解析成 :class:`AssemblyContext`；``known_top_names`` 非空时校验顶层段名。"""
        return AssemblyContext.from_dict(self._data, known_top_names=known_top_names)

    def is_empty(self) -> bool:
        return not self._data


def _load_yaml(text: str) -> dict:
    import yaml  # type: ignore

    return yaml.safe_load(text) or {}
