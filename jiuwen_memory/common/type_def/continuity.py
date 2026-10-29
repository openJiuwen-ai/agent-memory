"""S10 typed inputs and stable, content-addressed memory references."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def.memory import MemoryUnit
from jiuwen_memory.common.type_def.scope import Scope

MODEL_VERSION = "memory-relations/1"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass
class MemoryRef:
    scope: Scope
    unit_id: str
    content_digest: str

    def __post_init__(self) -> None:
        if not self.unit_id or len(self.content_digest) != 64:
            raise ValidationError("MemoryRef requires an ID and SHA-256 content digest")
        if any(c not in "0123456789abcdef" for c in self.content_digest):
            raise ValidationError("invalid content digest")

    @classmethod
    def of(cls, unit: MemoryUnit) -> MemoryRef:
        segments = [
            {"content": s.content, "assets": s.assets, "source": s.source.value}
            for s in unit.segments
        ]
        return cls(unit.scope, unit.id, fingerprint({"segments": segments}))

    @classmethod
    def parse(cls, value: dict) -> MemoryRef:
        return cls(Scope(**value["scope"]), value["unit_id"], value["content_digest"])

    @property
    def key(self) -> str:
        return "continuity:memory:" + fingerprint({"scope": asdict(self.scope), "id": self.unit_id})


class Duty(str, Enum):
    GENERAL = "general"
    DIAGNOSIS = "diagnosis"
    TESTING = "testing"
    EXECUTION = "execution"


class EvidenceRelation(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    POSSIBLE_CAUSE_OF = "possible_cause_of"
    VERIFICATION_METHOD_FOR = "verification_method_for"
    VERIFIES = "verifies"
    REQUIRES = "requires"


class RelationStatus(str, Enum):
    CANDIDATE = "candidate"
    PROPOSED = "candidate"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    REVOKED = "revoked"
    RETRACTED = "revoked"

    @classmethod
    def _missing_(cls, value):
        return {"proposed": cls.CANDIDATE, "retracted": cls.REVOKED}.get(value)


@dataclass
class RelationRecord:
    relation_id: str
    source: MemoryRef
    target: MemoryRef
    relation_type: str
    revision: int = 1
    status: RelationStatus = RelationStatus.PROPOSED
    evidence_refs: list[MemoryRef] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    schema_version: str = "memory-relation/1"
    model_version: str = MODEL_VERSION
    batch_id: str = ""
    conflict_kind: str = ""
    resolution: str = ""
    resolution_reason: str = ""
    resolution_origin: str = ""
    resolution_refs: list[MemoryRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.relation_type = str(getattr(self.relation_type, "value", self.relation_type))
        if not self.relation_type:
            raise ValidationError("relation type required")
        try:
            self.status = RelationStatus(self.status)
        except ValueError:
            raise ValidationError("unknown relation status") from None
        if self.conflict_kind not in {"", "factual", "temporal", "advisory"}:
            raise ValidationError("unknown conflict kind")
        if self.resolution not in {"", "prefer_a", "prefer_b", "both_valid", "escalate", "defer"}:
            raise ValidationError("unknown conflict resolution")
        if self.resolution and (self.relation_type != "contradicts" or not self.resolution_reason):
            raise ValidationError("resolution requires a contradiction and reason")
        if self.conflict_kind == "advisory" and self.resolution in {"prefer_a", "prefer_b"}:
            raise ValidationError("advisory differences cannot be merged")
        if self.relation_id.startswith("continuity:"):
            raise ValidationError("reserved relation ID prefix")
        if not self.relation_id or type(self.revision) is not int or self.revision < 1:
            raise ValidationError("relation ID and positive revision required")
        if self.schema_version != "memory-relation/1" or self.model_version != MODEL_VERSION:
            raise ValidationError("unsupported relation model")
        if self.source.key == self.target.key:
            raise ValidationError("self relations are not supported")
        if self.status == RelationStatus.CONFIRMED and not self.evidence_refs:
            raise ValidationError("confirmed relations require explicit evidence")
        if self.relation_type == EvidenceRelation.VERIFIES and not self.conditions:
            raise ValidationError("verification requires conditions")

    @classmethod
    def parse(cls, value: dict) -> RelationRecord:
        v = dict(value)
        for key in ("source", "target"):
            v[key] = MemoryRef.parse(v[key])
        v["evidence_refs"] = [MemoryRef.parse(r) for r in v.get("evidence_refs", [])]
        v["resolution_refs"] = [MemoryRef.parse(r) for r in v.get("resolution_refs", [])]
        return cls(**v)


@dataclass
class ViewBudget:
    max_depth: int = 2
    max_nodes: int = 30
    max_edges: int = 60
    timeout_ms: int = 1000

    def __post_init__(self) -> None:
        limits = dict(max_depth=4, max_nodes=200, max_edges=500, timeout_ms=10000)
        for name, maximum in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise ValidationError(f"{name} must be between 1 and {maximum}")


@dataclass
class RoleViewQuery:
    query: str = ""
    anchor: MemoryRef | None = None
    task_id: str = ""
    duty: str = "general"
    budget: ViewBudget = field(default_factory=ViewBudget)

    def __post_init__(self) -> None:
        self.duty = str(getattr(self.duty, "value", self.duty))
        if not self.query.strip() and self.anchor is None and not self.task_id:
            raise ValidationError("query, anchor or task_id is required")


@dataclass
class TaskRequest:
    operation: str
    request_id: str = ""
    task_id: str = ""
    expected_revision: int = 0
    connector_id: str = ""
    context_id: str = ""
    context_source: str = "native"
    binding_id: str = ""
    background: list[MemoryRef] = field(default_factory=list)
    required: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValidationError("nonnegative revision required")
        if self.operation not in {"create", "get", "find", "bind", "unbind", "attach", "close"}:
            raise ValidationError("unknown task operation")
        if self.operation not in {"get", "find"} and not self.request_id:
            raise ValidationError("stable request_id required")
        if self.context_source not in {"native", "adapter"}:
            raise ValidationError("unknown context source")


@dataclass
class ContinuityEvent:
    task_id: str
    binding_id: str
    event_id: str
    content: str
    kind: str = "unclassified"
    classification_confidence: str = "unknown"
    duty_ids: list[str] = field(default_factory=list)
    evidence_refs: list[MemoryRef] = field(default_factory=list)
    source_locator: str | None = None
    occurred_at: str | None = None
    claimed_author: str | None = None
    schema_version: str = "continuity-event/1"

    def __post_init__(self) -> None:
        if self.classification_confidence not in {"low", "high", "unknown"}:
            raise ValidationError("unknown classification confidence")
        if self.classification_confidence == "low":
            self.kind = "unclassified"
        if self.occurred_at:
            try:
                timestamp = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("timezone required")
            except ValueError:
                raise ValidationError("occurred_at must be RFC 3339 with timezone") from None
        if self.schema_version != "continuity-event/1":
            raise ValidationError("unsupported event version")
        if not all((self.task_id, self.binding_id, self.event_id, self.content.strip())):
            raise ValidationError("task, binding, event and content required")
        if self.kind not in {
            "observation",
            "statement",
            "judgment",
            "result",
            "experience",
            "correction",
            "unclassified",
        }:
            raise ValidationError("unknown event kind")


@dataclass
class ContinuityRecovery:
    kind: str
    operation_id: str
    refs: list[MemoryRef]

    def __post_init__(self):
        if self.kind not in {"event", "relation"} or not self.operation_id or not self.refs:
            raise ValidationError("recovery requires a kind, operation ID and committed references")
