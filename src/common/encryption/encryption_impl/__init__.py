"""encryption_impl 实现集：工厂 EncryptionProducer + 各实现。

import 各实现模块即触发其 ``@EncryptionProducer.register(...)`` 自注册；
本包只对外暴露工厂 EncryptionProducer。
"""

from importlib import import_module

from common.encryption.base import EncryptionProducer

import_module(".local_envelope", __name__)

__all__ = ["EncryptionProducer"]
