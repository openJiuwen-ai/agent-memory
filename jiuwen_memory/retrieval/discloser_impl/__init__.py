# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""discloser_impl 实现集：工厂 DiscloserProducer + 各实现。

import 各实现模块即触发其 ``@DiscloserProducer.register(...)`` 自注册；本包只对外暴露工厂 DiscloserProducer。
"""

from importlib import import_module

from jiuwen_memory.retrieval.discloser import DiscloserProducer

import_module(".truncating_discloser", __name__)
import_module(".structured_discloser", __name__)

__all__ = ["DiscloserProducer"]
