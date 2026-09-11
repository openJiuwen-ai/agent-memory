"""Explicit, opt-in registration for Schema construction implementations."""

from __future__ import annotations

import logging
from importlib import import_module

_REGISTERED = False


def register_schema_constructors() -> None:
    """Register Schema construction targets without editing the official bootstrap."""

    global _REGISTERED
    if _REGISTERED:
        return
    try:
        import_module("jiuwen_memory.construction.extractor_impl.entity_schema_extractor")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    try:
        import_module("jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver")
    except ImportError as exc:
        logging.getLogger(__name__).warning("optional import skipped: %s", exc)
    _REGISTERED = True
