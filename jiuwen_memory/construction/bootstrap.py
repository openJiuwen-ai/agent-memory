"""注册引导：import 各构建实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄定义在接口模块（如 :class:`~construction.evolver.EvolverProducer`），消费方只依赖接口层；
实现的注册发生在 import 实现模块时，由本函数在装配入口统一触发。与各层 bootstrap 同构。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_constructors() -> None:
    """import 各构建实现包，完成自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("jiuwen_memory.construction.abstractor_impl")
    import_module("jiuwen_memory.construction.associator_impl")
    import_module("jiuwen_memory.construction.classifier_impl")
    import_module("jiuwen_memory.construction.dedup_impl")
    import_module("jiuwen_memory.construction.evolver_impl")
    import_module("jiuwen_memory.construction.extractor_impl")
    import_module("jiuwen_memory.construction.index_builder_impl")
    import_module("jiuwen_memory.construction.layer_annotator_impl")
    import_module("jiuwen_memory.construction.router_impl")
    _REGISTERED = True
