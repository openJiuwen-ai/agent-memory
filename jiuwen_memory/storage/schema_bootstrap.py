"""Explicit, opt-in registration for Schema storage extensions."""

from __future__ import annotations

from importlib import import_module

_REGISTERED = False


def register_schema_storage() -> None:
    """Register Schema Storage targets without editing the official bootstrap."""

    global _REGISTERED
    if _REGISTERED:
        return
    import_module("jiuwen_memory.storage.storage_impl.schema_composite_storage")
    _REGISTERED = True
