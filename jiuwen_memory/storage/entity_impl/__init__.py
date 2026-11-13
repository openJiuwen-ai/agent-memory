# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""entity_impl 实现集：工厂 EntityStoreProducer + 各实现。

import 各实现模块即触发其 ``@EntityStoreProducer.register(...)`` 自注册；本包只对外
暴露工厂 EntityStoreProducer。当前实现：

- ``elasticsearch``：生产后端，复用主链路 FulltextStore 同一 ES 集群（``memory_entities``）；
- ``memory``：内存后端（契约测试 / 无外部服务单测 / 小规模部署）；
- ``shard_routed``：F09 分片包装层（shared / split 两种模式）。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.storage.entity_store import EntityStoreProducer

import_optional(
    ".elasticsearch_entity_store",
    __name__,
)  # 触发 @EntityStoreProducer.register("elasticsearch")
import_optional(".in_memory_entity_store", __name__)
import_optional(".shard_routed_entity", __name__)
import_optional(".legacy_entity_store_adapter", __name__)

__all__ = ["EntityStoreProducer"]
