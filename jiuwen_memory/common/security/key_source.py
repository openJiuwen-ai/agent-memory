# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""旧 ``KeySource`` 导入路径的一个发布周期兼容契约。

新实现应使用 ``common.security.cryptography.KeyProvider``；该旧接口仅保证现有第三方导入
和类型定义继续工作，不参与新的 ENC1 装配。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class KeySource(ABC):
    """已弃用的按名称取密钥接口。"""

    @abstractmethod
    def fetch_key(self, key_name: str) -> bytes:
        """按名称返回密钥材料；不存在时抛 ``KeyError``。"""

    def health(self) -> None:
        """健康时返回 ``None``。"""
        return None


__all__ = ["KeySource"]
