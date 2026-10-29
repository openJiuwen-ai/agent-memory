"""Tool schemas derived from public DTOs, shared by JiuwenSwarm and remote MCP."""

from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from jiuwen_memory.api import ContinuityEvent, RelationRecord, RoleViewQuery, TaskRequest


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


def tool_schemas() -> list[dict]:
    specs = [
        (
            "role_view",
            "request",
            RoleViewQuery,
            "Locate memory by query or anchor, or restore saved task background; expand typed "
            "edges by duty (general/diagnosis/testing/execution or configured policy). "
            "Read paths and gaps before acting. No predecessor handoff is required.",
        ),
        (
            "role_task",
            "request",
            TaskRequest,
            "Create/get/find/bind/unbind/attach/close task mappings. Mutations require stable "
            "request_id and expected_revision. Bindings never grant source read permissions.",
        ),
        (
            "role_record",
            "event",
            ContinuityEvent,
            "Record an observation, result or experience in an authorized binding. Reuse event_id "
            "on retry. Inspect committed/partial/unknown; reconcile unknown before retrying.",
        ),
        (
            "role_relation",
            "relation",
            RelationRecord,
            "Register or review a relation between committed MemoryRefs. Unknown model output "
            "stays candidate. Confirm with evidence; revoke with a new revision. To review "
            "a contradiction supply conflict_kind, resolution and resolution_reason.",
        ),
    ]
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
        if verb == "role_relation":
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
    return out
