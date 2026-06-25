"""llm_impl 实现集：工厂 LlmProducer + 各实现。

import 各实现模块即触发其 ``@LlmProducer.register(...)`` 自注册；本包只对外暴露工厂 LlmProducer。
可选后端（openai_llm）依赖可选重包，未安装则跳过注册（不连坐默认实现）。
"""

from importlib import import_module

from common.llm.base import LlmProducer

import_module(".echo_llm", __name__)

try:  # 可选后端：依赖未安装则跳过注册
    import_module(".openai_llm", __name__)
except ImportError:
    pass

__all__ = ["LlmProducer"]
