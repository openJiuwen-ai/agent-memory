"""安全能力的统一注册入口（F05 §装配不变量 2：注册在装配前统一完成）。

各实现包不互相 import；注册顺序由本模块单点管理。import 实现包即触发其
``@<X>Producer.register(...)`` 自注册，本函数只负责按固定顺序把它们 import 进来。

**必须在配置解析之前调用**：``authenticator`` / ``cryptography`` / ``security`` 等顶层
段名要先进 ``Factory.known_top_names()``，否则解析期会把它们当未知段拒掉。
"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_security() -> None:
    """import 全部安全实现包，完成自注册（幂等；import 已缓存，重复调用近乎零成本）。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("common.security.runtime")  # SecurityRuntimeProducer + standard
    import_module("common.security.authentication.authentication_impl")
    import_module("common.security.protection.protection_impl")
    import_module("common.security.cryptography.cryptography_impl")
    _REGISTERED = True
