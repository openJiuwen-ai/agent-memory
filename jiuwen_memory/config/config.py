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
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .context import AssemblyContext


class Config:
    """一次装配的用户配置（两级命名空间字典，解析后只读）。"""

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        """初始化 Config。

        Args:
            data: 参数 data（Optional[Mapping[str, Any]]）。
        """
        self._data = dict(data or {})

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Config":
        """从配置字典构造。"""
        return cls(data or {})

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """从 YAML 文件解析配置。"""
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(_load_yaml(fh.read()))

    def context(self, known_top_names: Optional[set] = None) -> AssemblyContext:
        """解析成 :class:`AssemblyContext`；``known_top_names`` 非空时校验顶层段名。"""
        return AssemblyContext.from_dict(self._data, known_top_names=known_top_names)

    def is_empty(self) -> bool:
        """执行 `is_empty` 操作。

        Returns:
            返回 bool。
        """
        return not self._data


def _load_yaml(text: str) -> dict:
    """加载并解析输入数据。

    Args:
        text: 参数 text（str）。

    Returns:
        返回 dict。
    """
    import yaml  # type: ignore

    return yaml.safe_load(text) or {}
