# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""数据面实现目录：``CompositeDomainStore`` + 其检索适配器 Recaller 全家。

Recaller 是数据面的内部件（唯一消费方是 :class:`CompositeDomainStore`），故契约
``recaller.py`` 与三个实现都落在本包。import 各实现模块即触发其
``@RecallerProducer.register(...)`` 自注册——由
:func:`storage.bootstrap.register_backends` import 本包时统一完成。
"""

from jiuwen_memory.common._import_support import import_optional

from .composite_domain_store import CompositeDomainStore
from .recaller import Recaller, RecallerProducer

import_optional(".graph_recaller", __name__)
import_optional(".keyword_recaller", __name__)
import_optional(".vector_recaller", __name__)

__all__ = ["CompositeDomainStore", "Recaller", "RecallerProducer"]
