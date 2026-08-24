# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""KeySource — 外部密钥源抽象接口。

调用方（DynamicKeySecurityProvider）按 key_name 向 KeySource 请求密钥字节；
具体怎么从外部拿到密钥（HTTP 配置中心 / KV 存储 / KMS / Vault …）由各实现完成。

实现方注意事项：
- fetch_key 返回的密钥应为 32 字节（256 位），供 AES-256-GCM 使用；
  不足或超出长度由调用方校验并抛 ValidationError。
- 实现方自行负责缓存、过期刷新、失败重试、降级策略。
- key_name 的格式由调用方（DynamicKeySecurityProvider 的 key_naming 策略）决定，
  实现方只需按 key_name 查找并返回对应密钥，不解析 key_name 语义。
- 若密钥不存在，应抛 KeyError 或自定义异常，不要返回空字节。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class KeySource(ABC):
    """外部密钥源：按 key_name 动态获取密钥。"""

    @abstractmethod
    def fetch_key(self, key_name: str) -> bytes:
        """按 key_name 获取密钥字节。

        :param key_name: 密钥名，格式由调用方的 key_naming 策略决定。
        :return: 32 字节密钥（256 位），供 AES-256-GCM 使用。
        :raises KeyError: key_name 对应的密钥不存在。
        """

    def health(self) -> None:
        """存活探测：健康时返回 None，否则由实现抛出异常。"""
        return None
