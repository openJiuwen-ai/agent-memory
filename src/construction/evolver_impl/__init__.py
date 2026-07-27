"""evolver_impl 实现集：工厂 EvolverProducer + 各实现。

import 各实现模块即触发其 ``@EvolverProducer.register(...)`` 自注册；本包只对外暴露工厂 EvolverProducer。
"""

from importlib import import_module

from construction.evolver import EvolverProducer

import_module(".orchestrating_evolver", __name__)
import_module(".dynamic_evolver", __name__)

__all__ = ["EvolverProducer"]
