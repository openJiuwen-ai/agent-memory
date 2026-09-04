"""router_impl 实现集：工厂 RouterProducer + 各实现。

import 各实现模块即触发其 ``@RouterProducer.register(...)`` 自注册；本包只对外暴露工厂
RouterProducer。首版只交付模型实现，不交付规则实现——规则版需要一套「类别到关键词或
正则」的配置，而类别名来自接入方声明，规则缺失时它整体落 fallback，等同判定关闭。
"""

from importlib import import_module

from jiuwen_memory.construction.router import RouterProducer

import_module(".llm_router", __name__)

__all__ = ["RouterProducer"]
