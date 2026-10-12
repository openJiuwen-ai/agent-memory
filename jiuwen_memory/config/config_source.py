# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ConfigSource — 可插拔配置来源抽象（S08 / F01-config-source）。

与密钥侧 ``KeyProvider``（``common.security.cryptography``）同构：
- 抽象接口：别人可插自己的来源（配置中心 / DB / HTTP …）
- 默认实现：对齐 YAML + ``defaults.py`` 装配快照，不配也能跑
- 装配注入：``build_kernel`` 时装上
- 运行中 ``fetch(key)``：取能力开关、prompt、模型凭证、Store 连接 / ``*.active``

本模块只定义契约与 Producer；具体实现见 ``config_source_impl/``。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jiuwen_memory.common.factory.factory import Factory


class ConfigSourceProducer(Factory):
    """ConfigSource 的注册式工厂；配置顶层段名为 ``config_source``。"""

    TOP_NAME = "config_source"

    @classmethod
    def get_cached(cls, name: str = "default") -> ConfigSource | None:
        """取装配期已缓存的具名实例；未装配返回 ``None``（不触发 build）。

        供 PromptRegistry 等在组件 ``_build`` 时挂接同一 ConfigSource，
        避免直接摸 ``_instances`` 私有表。
        """
        inst = cls._instances.get(name)
        return inst if isinstance(inst, ConfigSource) else None


class ConfigSource(ABC):
    """按稳定 key 提供晚绑定配置值。

    ``fetch`` 返回 ``str`` 或 ``None``（缺失）。布尔/数字由消费方自行解析。
    实现方可自行缓存；首版契约不要求 invalidate 接口。
    """

    @abstractmethod
    def fetch(self, key: str) -> str | None:
        """读取当前应生效的配置值；key 不存在时返回 ``None``。"""

    def health(self) -> None:
        """存活探测：健康返回 ``None``，否则由实现抛错。"""
        return None
