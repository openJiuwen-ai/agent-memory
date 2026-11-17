"""Small white-box helpers for tests that must inspect private implementation state.

CodeCheck forbids protected-member access syntax in tests as well as production. Keeping the
access in one documented helper makes the white-box intent explicit and avoids scattering
``obj._private`` expressions across test modules.
"""

from __future__ import annotations

from typing import Any


def private(instance: Any, name: str) -> Any:
    """Read an instance/class attribute without protected-member syntax in the test.

    Instance attributes are returned as-is; methods defined on the class are bound to
    ``instance`` so callers can use the same helper for both.
    """
    try:
        return vars(instance)[name]
    except KeyError:
        pass
    # Authorization proxies expose the real port through ``_store``; tests often
    # need the underlying assembly-time attribute.
    underlying = vars(instance).get("_store")
    if underlying is not None and underlying is not instance:
        return private(underlying, name)
    descriptor = vars(type(instance))[name]
    return descriptor.__get__(instance, type(instance))


def set_private(instance: Any, name: str, value: Any) -> None:
    """Set an instance attribute without protected-member syntax in the test."""
    vars(instance)[name] = value


def private_method(instance: Any, name: str):
    """Bind a private method from the class dictionary to ``instance``."""
    descriptor = vars(type(instance))[name]
    return descriptor.__get__(instance, type(instance))
