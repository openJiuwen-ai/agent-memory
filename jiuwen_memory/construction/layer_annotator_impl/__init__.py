# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""layer_annotator_impl 实现集：工厂 LayerAnnotatorProducer + 各实现。

import 各实现模块即触发其 ``@LayerAnnotatorProducer.register(...)`` 自注册；
本包只对外暴露工厂 LayerAnnotatorProducer。
"""

from importlib import import_module

from jiuwen_memory.construction.layer_annotator import LayerAnnotatorProducer

import_module(".keyword_layer_annotator", __name__)
import_module(".llm_layer_annotator", __name__)

__all__ = ["LayerAnnotatorProducer"]
