"""embedder_impl 实现集：工厂 EmbedderProducer + 各实现。

import 各实现模块即触发其 ``@EmbedderProducer.register(...)`` 自注册；本包只对外暴露工厂 EmbedderProducer。
可选后端（openai_embedder）依赖可选重包，未安装则跳过注册（不连坐默认实现）。
"""

from importlib import import_module

from common.embedder.base import EmbedderProducer

import_module(".hashing_embedder", __name__)
import_module(".bge_m3_embedder", __name__)

try:  # 可选后端：依赖未安装则跳过注册
    import_module(".openai_embedder", __name__)
except ImportError:
    pass

__all__ = ["EmbedderProducer"]
