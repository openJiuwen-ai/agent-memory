# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""entity_impl 实现集：工厂 EntityStoreProducer + 各实现。

import 各实现模块即触发其 ``@EntityStoreProducer.register(...)`` 自注册；本包只对外
暴露工厂 EntityStoreProducer。当前仅有 Elasticsearch 后端（``ElasticsearchEntityStore``），
复用主链路 FulltextStore 同一 ES 集群，index 为 ``memory_entities``。
"""

import logging
from importlib import import_module

from jiuwen_memory.storage.entity_store import EntityStoreProducer

try:
    import_module(".elasticsearch_entity_store", __name__)  # 触发 @EntityStoreProducer.register("elasticsearch")
except ImportError as exc:
    logging.getLogger(__name__).warning("optional import skipped: %s", exc)

__all__ = ["EntityStoreProducer"]
