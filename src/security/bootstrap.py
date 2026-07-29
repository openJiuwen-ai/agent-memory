"""注册引导：import 各安全实现包，触发其 ``@Producer.register`` 自注册。

工厂句柄定义在接口模块（:class:`~security.authenticator.AuthProducer`、
:class:`~security.key_store.KeyStoreProducer`），消费方只依赖接口层；实现的
注册发生在 import 实现模块时，由本函数在装配入口统一触发。与各层 bootstrap 同构。

**调用点**：``bootstrap/core/server.py:Server.build`` 的开头，必须在
``KernelConfig.from_dict(...)`` **之前**——否则 ``authenticator`` / ``key_store`` /
``rate_limiter`` 三个顶层段会因未注册进 ``Factory.known_top_names()`` 而被配置
解析期的段名校验拒掉。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_security() -> None:
    """import 各安全实现包，完成自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("security.key_store_impl")
    import_module("security.authenticator_impl")
    import_module("security.rate_limit_impl")
    _REGISTERED = True
