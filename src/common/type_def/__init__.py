"""跨层共用的结构体定义。"""

from .audit import AuditEvent
from .chat import ChatMessage
from .chunk import Chunk
from .context import EXT_MAX_TOKENS, Context
from .feature import Entity, FeatureSet, Relation
from .filter import FilterClause, FilterOp
from .memory import (
    MEMORY_KEY_PREFIX,
    DedupDecision,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Segment,
    Temporal,
    memory_key,
)
from .raw import MESSAGES_KEY_PREFIX, RawPayload, messages_key
from .scope import Scope

__all__ = [
    "Scope",
    "Context",
    "EXT_MAX_TOKENS",
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
    "MEMORY_KEY_PREFIX",
    "MESSAGES_KEY_PREFIX",
    "memory_key",
    "messages_key",
]
