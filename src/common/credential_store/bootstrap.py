"""注册凭据存储能力的内置实现。"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_credential_store() -> None:
    """导入凭据存储实现包，触发已注册 target 的工厂注册。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("common.credential_store.credential_store_impl")
    _REGISTERED = True
