# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned scope restriction for delivering continuity results."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from jiuwen_memory.common.type_def.scope import Scope

_DELIVERY_SCOPE: ContextVar[Scope | None] = ContextVar("continuity_delivery_scope", default=None)


@contextmanager
def restrict_continuity_delivery(scope: Scope) -> Iterator[None]:
    """Set a request-local restriction without granting any record access."""
    token = _DELIVERY_SCOPE.set(replace(scope))
    try:
        yield
    finally:
        _DELIVERY_SCOPE.reset(token)


def continuity_delivery_scope() -> Scope | None:
    return _DELIVERY_SCOPE.get()
