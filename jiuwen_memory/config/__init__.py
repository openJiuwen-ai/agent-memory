# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配配置：把 YAML/字典解析成 :class:`AssemblyContext`（两级命名空间，纯数据）。

- :class:`Config`：门面，``from_yaml`` / ``from_dict`` 解析，
  ``context()`` 取 :class:`AssemblyContext`。
- :class:`AssemblyContext`：全局命名空间（top_name -> name -> RawSpec）+ 跨切面 ``globals``。
- :class:`ComponentConfig`：传给各 ``_build`` 的 ``config`` 视图
  （本实例 params 回退 globals + ctx 句柄）。
- :class:`RawSpec`：一个具名实例的纯数据。
- :func:`default_context`：内置默认装配上下文（离线进程内栈，复刻共享拓扑）。
- :class:`ConfigSource` / :class:`ConfigSourceProducer`：运行时晚绑定配置来源
  （S08；默认 yaml_defaults，见 ``config_source_impl/``）。

「装哪些、怎么串、共享谁」由各 ``Producer`` 经 ``build_named`` / ``dep`` 顺着引用落地
（见 ``api.build_kernel``）。业务配置运行时取值走 ConfigSource，不经 add/search 入参。
"""

from .config import Config
from .config_source import ConfigSource, ConfigSourceProducer
from .context import AssemblyContext, ComponentConfig, RawSpec
from .defaults import default_context

__all__ = [
    "Config",
    "AssemblyContext",
    "ComponentConfig",
    "RawSpec",
    "default_context",
    "ConfigSource",
    "ConfigSourceProducer",
]
