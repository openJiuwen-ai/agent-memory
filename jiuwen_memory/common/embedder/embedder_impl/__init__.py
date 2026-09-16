# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""embedder_impl 实现集：工厂 EmbedderProducer + 各实现。

import 各实现模块即触发其 ``@EmbedderProducer.register(...)`` 自注册；本包只对外暴露工厂
EmbedderProducer。
可选后端（openai_embedder）依赖可选重包，未安装则跳过注册（不连坐默认实现）。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.embedder.base import EmbedderProducer

import_optional(".hashing_embedder", __name__)
import_optional(".bge_m3_embedder", __name__)
import_optional(".openai_embedder", __name__)

__all__ = ["EmbedderProducer"]
