"""注册引导：import 各控制实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄定义在接口模块（如 :class:`~control.engine.EngineProducer`），消费方只依赖接口层；
实现的注册发生在 import 实现模块时，由本函数在装配入口统一触发。与各层 bootstrap 同构。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_controllers() -> None:
    """import 各控制实现包，完成自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("jiuwen_memory.control.pipeline_impl")
    import_module("jiuwen_memory.control.engine_impl")
    import_module("jiuwen_memory.control.governance_impl")
    import_module("jiuwen_memory.control.lifecycle_impl")
    import_module("jiuwen_memory.control.membership_impl")
    import_module("jiuwen_memory.control.permission_impl")
    import_module("jiuwen_memory.control.policy_impl")
    import_module("jiuwen_memory.control.scheduler_impl")
    import_module("jiuwen_memory.control.job_impl")
    import_module("jiuwen_memory.control.space_impl")
    _REGISTERED = True
