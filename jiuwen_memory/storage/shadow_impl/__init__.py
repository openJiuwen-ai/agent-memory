"""shadow_impl 实现集：工厂 ShadowIndexProducer + 各实现。

import 各实现模块即触发其 ``@ShadowIndexProducer.register(...)`` 自注册；
本包只对外暴露工厂 ShadowIndexProducer。
"""

from importlib import import_module

from jiuwen_memory.storage.shadow import ShadowIndexProducer

import_module(".sqlite_shadow_index", __name__)

__all__ = ["ShadowIndexProducer"]
