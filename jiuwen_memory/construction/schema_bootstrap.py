"""Explicit, opt-in registration for Schema construction implementations."""

from __future__ import annotations

from jiuwen_memory.common._import_support import import_optional

_REGISTERED = False


def register_schema_constructors() -> None:
    """Register Schema construction targets without editing the official bootstrap."""

    global _REGISTERED
    if _REGISTERED:
        return
    import_optional("jiuwen_memory.construction.extractor_impl.entity_schema_extractor")
    import_optional("jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver")
    _REGISTERED = True
