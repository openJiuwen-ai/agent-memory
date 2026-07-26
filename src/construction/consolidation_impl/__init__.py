"""Consolidator 实现注册入口。"""

from importlib import import_module

from construction.consolidation import ConsolidatorProducer

import_module(".consolidation_1", __name__)
import_module(".consolidation_2", __name__)

__all__ = ["ConsolidatorProducer"]
