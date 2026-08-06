"""kv_impl 实现集：工厂 KvProducer + 各实现。

import 各实现模块即触发其 ``@KvProducer.register(...)`` 自注册；
本包只对外暴露工厂 KvProducer。
"""

from importlib import import_module

from storage.kv import KvProducer

import_module(".in_memory_kv_store", __name__)
import_module(".sqlite_kv_store", __name__)
import_module(".redis_kv", __name__)
import_module(".encrypted_kv_store", __name__)
import_module(".postgres_kv", __name__)

__all__ = ["KvProducer"]
