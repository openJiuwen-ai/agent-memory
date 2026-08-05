"""注册准入控制能力的内置实现。"""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_admission() -> None:
    """导入准入控制实现包，触发已注册 target 的工厂注册。"""
    global _REGISTERED
    if _REGISTERED:
        return
    import_module("common.admission.admission_impl")
    _REGISTERED = True
