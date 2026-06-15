"""记忆存储层（F 层）接口：统一 CRUD 的可插拔存储抽象。"""

from .base import BaseStore, StoreType
from .fs import FSStore
from .fulltext import FulltextStore
from .fusion import FusionStore
from .graph import GraphStore
from .kv import KVStore
from .types import (
    Document,
    Edge,
    FileStat,
    FusionQuery,
    FusionRecord,
    GraphQuery,
    Node,
    ScoredID,
    TextQuery,
    VectorQuery,
    VectorRecord,
)
from .vector import VectorStore

__all__ = [
    "BaseStore",
    "StoreType",
    "KVStore",
    "FulltextStore",
    "VectorStore",
    "GraphStore",
    "FusionStore",
    "FSStore",
    "ScoredID",
    "Document",
    "TextQuery",
    "VectorRecord",
    "VectorQuery",
    "Node",
    "Edge",
    "GraphQuery",
    "FusionRecord",
    "FusionQuery",
    "FileStat",
]
