"""key_store_impl 实现集：工厂 KeyStoreProducer + 各实现。

import 各实现模块即触发其 ``@KeyStoreProducer.register(...)`` 自注册；
本包只对外暴露工厂 KeyStoreProducer。
"""

from importlib import import_module

from security.key_store import KeyStoreProducer

import_module(".memory_key_store", __name__)

__all__ = ["KeyStoreProducer"]
