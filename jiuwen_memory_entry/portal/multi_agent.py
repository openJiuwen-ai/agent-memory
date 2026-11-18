# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deployment-bound read-only context for the multi-Agent Portal view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jiuwen_memory.api import Scope, ValidationError

VIEW_HEADER = "X-Memory-Portal-View"
VIEW_VALUE = "multi-agent"


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"multi-agent Portal {name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class MultiAgentPortal:
    available: bool
    reason: str
    scope: Scope
    project_name: str
    connectors: tuple[tuple[str, str], ...]

    def capability(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "scope": self.scope,
            "project_name": self.project_name,
            "mode": None,
            "connectors": [
                {"id": connector_id, "label": label}
                for connector_id, label in self.connectors
            ],
        }

    def continuity_scope(self) -> Scope:
        """Return the full Scope used by continuity records and control operations."""
        return self.scope

    def allows_scope(self, scope: Scope) -> bool:
        """Return whether a concrete scope matches the five-dimensional binding."""
        return scope == self.scope


def _unavailable(reason: str) -> MultiAgentPortal:
    return MultiAgentPortal(False, reason, Scope(), "", ())


def multi_agent_portal(settings: dict[str, Any]) -> MultiAgentPortal:
    """Validate one explicit multi-Agent Portal configuration block.

    ``settings`` is the complete loaded deployment configuration. Only
    ``http.multi_agent`` is consumed here because the HTTP host owns this page
    profile; SDK hosts pass the same mapping through ``portal.multi_agent``.
    """
    http = settings.get("http")
    raw = http.get("multi_agent") if isinstance(http, dict) else None
    return build_multi_agent_portal(raw)


def build_multi_agent_portal(
    raw: Any,
) -> MultiAgentPortal:
    if raw is None:
        return _unavailable("multi-agent Portal is not configured")
    if not isinstance(raw, dict):
        raise ValidationError("multi-agent Portal configuration must be an object")
    unknown = set(raw) - {"enabled", "scope", "project_name", "connectors"}
    if unknown:
        raise ValidationError(
            "unknown multi-agent Portal field(s): " + ", ".join(sorted(unknown))
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError("multi-agent Portal enabled must be a boolean")
    if not enabled:
        return _unavailable("multi-agent Portal is disabled")

    raw_scope = raw.get("scope")
    if not isinstance(raw_scope, dict):
        raise ValidationError("multi-agent Portal scope must be an object")
    allowed_scope = {"org", "space", "user", "agent", "session"}
    if set(raw_scope) - allowed_scope:
        raise ValidationError("multi-agent Portal scope has unknown fields")
    scope = Scope(**raw_scope)
    if not scope.org or not scope.space:
        raise ValidationError("multi-agent Portal scope requires org and space")
    if any(
        not isinstance(getattr(scope, name), str)
        for name in ("org", "space", "user", "agent", "session")
    ):
        raise ValidationError("multi-agent Portal scope values must be strings")

    project_name = _text(raw.get("project_name"), "project_name")

    raw_connectors = raw.get("connectors", {})
    if not isinstance(raw_connectors, dict):
        raise ValidationError("multi-agent Portal connectors must be an object")
    connectors = []
    for connector_id, label in raw_connectors.items():
        connector_id = _text(connector_id, "connector ID")
        label = _text(label, f"connector label for {connector_id}")
        connectors.append((connector_id, label))
    connectors.sort(key=lambda item: item[0])
    return MultiAgentPortal(True, "", scope, project_name, tuple(connectors))


def require_multi_agent(portal: MultiAgentPortal | None) -> MultiAgentPortal:
    if portal is None or not portal.available:
        reason = "multi-agent Portal is unavailable"
        if portal is not None and portal.reason:
            reason = portal.reason
        raise ValidationError(reason)
    return portal
