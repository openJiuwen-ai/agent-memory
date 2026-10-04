# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""llm_impl 实现集：工厂 LlmProducer + 各实现。

import 各实现模块即触发其 ``@LlmProducer.register(...)`` 自注册；本包只对外暴露工厂 LlmProducer。
可选后端（openai_llm / dashscope_llm）依赖可选重包，未安装则跳过注册
（不连坐默认实现）。
"""

from jiuwen_memory.common._import_support import import_optional
from jiuwen_memory.common.llm.base import LlmProducer

import_optional(".echo_llm", __name__)
import_optional(".openai_llm", __name__)
import_optional(".dashscope_llm", __name__)

__all__ = ["LlmProducer"]
