# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""kv_impl 实现集：工厂 KvProducer + 各实现。

import 各实现模块即触发其 ``@KvProducer.register(...)`` 自注册；
本包只对外暴露工厂 KvProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.kv import KvProducer

import_optional(".in_memory_kv_store", __name__)
import_optional(".sqlite_kv_store", __name__)
import_optional(".redis_kv", __name__)
import_optional(".encrypted_kv_store", __name__)
import_optional(".postgres_kv", __name__)

__all__ = ["KvProducer"]
