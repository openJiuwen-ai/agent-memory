"""跨层共用的结构体定义。"""

from .audit import AuditEvent
from .chat import ChatMessage
from .chunk import Chunk
from .context import EXT_MAX_TOKENS, Context
from .feature import Entity, FeatureSet, Relation
from .filter import (
    MEMORY_TYPE_FILTER_FIELD,
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    and_merge,
    canonical_filter_field,
    evaluate,
    extract_required_equality,
    filter_field_metadata_key,
    from_dict,
    iter_clauses,
    normalize,
    validate,
)
from .memory import (
    MEMORY_KEY_PREFIX,
    RESERVED_METADATA_KEYS,
    T_INVALID_OPEN,
    ContentLayers,
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
    "ContentLayers",
    "RawPayload",
    "Chunk",
    "Entity",
    "Relation",
    "FeatureSet",
    "ChatMessage",
    "AuditEvent",
    "FilterClause",
    "FilterOp",
    "FilterLogic",
    "FilterGroup",
    "FilterExpr",
    "MEMORY_TYPE_FILTER_FIELD",
    "normalize",
    "validate",
    "canonical_filter_field",
    "filter_field_metadata_key",
    "and_merge",
    "iter_clauses",
    "evaluate",
    "extract_required_equality",
    "from_dict",
    "MEMORY_KEY_PREFIX",
    "RESERVED_METADATA_KEYS",
    "T_INVALID_OPEN",
    "MESSAGES_KEY_PREFIX",
    "memory_key",
    "messages_key",
]
