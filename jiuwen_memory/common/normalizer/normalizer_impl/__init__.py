# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""normalizer_impl 实现集：工厂 NormalizerProducer + 各实现。

import 各实现模块即触发其 ``@NormalizerProducer.register(...)`` 自注册；
本包只对外暴露工厂 NormalizerProducer。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.normalizer.base import NormalizerProducer

import_optional(".passthrough_normalizer", __name__)
import_optional(".routing_normalizer", __name__)
import_optional(".video_asr", __name__)
import_optional(".video_normalizer", __name__)

__all__ = ["NormalizerProducer"]
