"""跨层共用的结构体定义。"""

from .audit import AuditEvent
from .chat import ChatMessage
from .chunk import Chunk
from .context import Context
from .feature import Entity, FeatureSet, Relation
from .filter import FilterClause, FilterOp
from .memory import DedupDecision, LifecycleState, MemoryTier, MemoryUnit, Modality, Segment, Temporal
from .raw import RawPayload
from .scope import Scope

__all__ = [
    "Scope",
    "Context",
    "Modality",
    "MemoryTier",
    "DedupDecision",
    "LifecycleState",
    "Temporal",
    "Segment",
    "MemoryUnit",
    "RawPayload",
    "Chunk",
    "Entity",
    "Relation",
    "FeatureSet",
    "ChatMessage",
    "AuditEvent",
    "FilterClause",
    "FilterOp",
]
