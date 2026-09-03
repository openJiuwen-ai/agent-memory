# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Structured request boundary shared by protocol adapters and the handler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from jiuwen_memory.common.security.types import RequestSecurityContext, Surface
from jiuwen_memory.common.type_def import Scope


@dataclass(frozen=True)
class DispatchBatchItem:
    """One batch item after its optional target has been parsed."""

    target: Scope | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    legacy_raw_item: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class DispatchRequest:
    """Immutable, already-adapted request consumed by shared dispatch."""

    verb: str
    actor: Scope
    target: Scope | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    surface: Surface = Surface.INTERNAL
    request_id: str = ""
    security: RequestSecurityContext | None = None
    grantee: Scope | None = None
    member: Scope | None = None
    batch_items: tuple[DispatchBatchItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if not isinstance(self.batch_items, tuple):
            object.__setattr__(self, "batch_items", tuple(self.batch_items))
