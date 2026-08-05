"""注册认证能力的内置实现。"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_authentication() -> None:
    """导入认证实现包，触发已注册 target 的工厂注册。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("common.credential_store.credential_store_impl")
    import_module("common.authentication.authentication_impl")
    _REGISTERED = True
