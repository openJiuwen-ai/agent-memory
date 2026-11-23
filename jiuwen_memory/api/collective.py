# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Collective request binding for authenticated SDK hosts and trusted HTTP gateways."""

import json
from collections.abc import Mapping

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import RequestSecurityContext, Surface
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.collective_members import Dimension, Member

_ATTR = "collective.context"
COLLECTIVE_CONTEXT_HEADER = "X-Jiuwen-Memory-Collective"
COLLECTIVE_MANAGER_ID = "swarm-memory-admin"
COLLECTIVE_MANAGEMENT_OPERATIONS = frozenset({
    "init_collective_dimension", "add_collective_members", "remove_collective_members",
    "list_collective_members",
})
DIMENSION_KEYS = ("project", "team", "group")


def normalize_collective_coordinates(raw: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(raw, Mapping) or raw.keys() - set(DIMENSION_KEYS):
        raise ValidationError("collective coordinates must contain only project, team, and group")
    if any(not isinstance(value, str) for value in raw.values()):
        raise ValidationError("collective coordinates must be strings")
    return {kind: value.strip() for kind, value in raw.items() if value.strip()}


def bind_collective_context(
    security: RequestSecurityContext,
    coordinates: Mapping[str, str],
) -> RequestSecurityContext:
    """Attach host-confirmed dimensions to an already authenticated request."""
    trusted = security.surface is Surface.INTERNAL or security.auth.auth_method == "trusted"
    if not security.has_valid_origin() or not trusted:
        raise PermissionDeniedError("collective context requires a trusted host")
    return new_request_context(
        security.auth,
        surface=security.surface,
        peer=security.peer,
        attributes={
            **security.attributes,
            _ATTR: json.dumps(normalize_collective_coordinates(coordinates)),
        },
        request_id=security.request_id,
    )


def collective_coordinates(security: RequestSecurityContext) -> dict[str, str] | None:
    """Return the coordinates carried by a bound request context, if present."""
    value = security.attributes.get(_ATTR)
    if value is None:
        return None
    if not security.has_valid_origin() or (
        security.surface is not Surface.INTERNAL and security.auth.auth_method != "trusted"
    ):
        raise PermissionDeniedError("collective context requires a trusted host")
    return normalize_collective_coordinates(json.loads(value))


def select_collective_coordinates(bound: dict[str, str], requested=None) -> dict[str, str]:
    if requested is None:
        return bound
    coordinates = normalize_collective_coordinates(requested)
    if any(bound.get(kind) != key for kind, key in coordinates.items()):
        raise PermissionDeniedError("requested collective coordinates exceed the host binding")
    return coordinates


def dimensions_of(scope: Scope, coordinates: Mapping[str, str]) -> tuple[Dimension, ...]:
    return tuple(
        Dimension(scope.org, scope.space, kind, value) for kind, value in coordinates.items()
    )


__all__ = [
    "COLLECTIVE_CONTEXT_HEADER", "COLLECTIVE_MANAGER_ID",
    "Dimension", "Member", "bind_collective_context", "collective_coordinates",
]
