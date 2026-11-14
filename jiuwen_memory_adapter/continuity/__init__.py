"""Tool schemas derived from public DTOs, shared by JiuwenSwarm and remote MCP."""

from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from jiuwen_memory.api import (
    ContinuityRecovery,
    RelationRecord,
    TaskRequest,
)


def _schema(annotation):
    if is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return {
            "type": "object",
            "properties": {f.name: _schema(hints[f.name]) for f in fields(annotation)},
            "required": [
                f.name
                for f in fields(annotation)
                if f.default is MISSING and f.default_factory is MISSING
            ],
            "additionalProperties": False,
        }
    origin = get_origin(annotation)
    if origin is list:
        return {"type": "array", "items": _schema(get_args(annotation)[0])}
    if origin is UnionType:
        return {"anyOf": [_schema(a) for a in get_args(annotation)]}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"type": "string", "enum": [v.value for v in annotation]}
    return {
        "type": {str: "string", int: "integer", bool: "boolean", type(None): "null"}[annotation]
    }


def tool_schemas(*, mode: str = "role", include_governance: bool = False) -> list[dict]:
    """Agent-facing tool schemas for the continuity surface.

    ``continuity_relation`` only exists in role mode (D2); the source-governance tools are
    admin-only and stay out of the agent list unless ``include_governance`` is passed (D4).
    """
    specs = [
        (
            "continuity_task",
            "request",
            TaskRequest,
            "Create/get/find/bind/unbind/attach/close task mappings. Mutations require stable "
            "request_id and expected_revision. Find checks connector_id/context_id first; "
            "if no exact match, optional query searches task title/description. Omit both "
            "context fields to search for another task. Confirm semantic candidates before bind. "
            "Bindings never grant source read permissions.",
        ),
    ]
    if mode == "role":
        specs.append((
            "continuity_relation",
            "relation",
            RelationRecord,
            "Register or review a relation between committed MemoryRefs. Unknown model output "
            "stays candidate. Confirm with evidence; revoke with a new revision. To review "
            "a contradiction supply conflict_kind, resolution and resolution_reason.",
        ))
    out = []
    for verb, arg, dto, description in specs:
        properties = {
            "scope": {
                "type": "object",
                "properties": {"org": {"type": "string"}, "space": {"type": "string"}},
                "required": ["org", "space"],
                "additionalProperties": False,
            },
            arg: _schema(dto),
        }
        required = ["scope", arg]
        if verb == "continuity_relation":
            properties["expected_revision"] = {"type": "integer", "minimum": 0}
            required.append("expected_revision")
        out.append(
            {
                "name": f"agent_memory_{verb}",
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    # continuity_recover has no required business payload: optional recovery reconciles an
    # unknown write acknowledgment; omitting it rebuilds graph projections.
    out.append(
        {
            "name": "agent_memory_continuity_recover",
            "description": (
                "Rebuild graph projections from authoritative relation heads, or reconcile "
                "an unknown/partial write acknowledgment via a ContinuityRecovery payload. "
                "Do not invent new IDs when a write is unknown; recover against the saved "
                "references first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "object",
                        "properties": {"org": {"type": "string"}, "space": {"type": "string"}},
                        "required": ["org", "space"],
                        "additionalProperties": False,
                    },
                    "recovery": {"anyOf": [{"type": "null"}, _schema(ContinuityRecovery)]},
                },
                "required": ["scope"],
                "additionalProperties": False,
            },
        }
    )
    _scope_schema = {
        "type": "object",
        "properties": {"org": {"type": "string"}, "space": {"type": "string"}},
        "required": ["org", "space"],
        "additionalProperties": False,
    }

    def _governance(name, description, required, extra_properties=None):
        properties = {"scope": _scope_schema, **(extra_properties or {})}
        return {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["scope", *required],
                "additionalProperties": False,
            },
        }

    if not include_governance:
        return out
    out.append(
        _governance(
            "agent_memory_quarantine_source",
            "Isolate an anomalous source range with a reason; ordinary reads and role views "
            "stop using it immediately.",
            ["source_key", "reason"],
            {"source_key": {"type": "string"}, "reason": {"type": "string"}},
        )
    )
    out.append(
        _governance(
            "agent_memory_release_source",
            "Release a previously quarantined source range.",
            ["source_key"],
            {"source_key": {"type": "string"}},
        )
    )
    return out
