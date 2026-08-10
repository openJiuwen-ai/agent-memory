"""记忆存储层（F 层）接口：统一 CRUD 的可插拔存储抽象。"""

from importlib import import_module

from .base import BaseStore, StoreType
from .entity_store import EntityStore, EntityStoreProducer
from .fs import FSStore
from .fulltext import FulltextStore
from .fusion import FusionStore
from .graph import GraphStore
from .kv import KVStore
from .storage import Storage, StorageCapability, StorageProducer
from .storage_impl import CompositeStorage
from .types import (
    Document,
    Edge,
    FileStat,
    FusionQuery,
    FusionRecord,
    GraphQuery,
    KVMemoryListResult,
    Node,
    ScoredHit,
    ScoredID,
    TextQuery,
    VectorQuery,
    VectorRecord,
)
from .vector import VectorStore

# 导入各 *_impl 包（仅为触发其内部各后端向 ``*Producer`` 自注册）；置于抽象定义之后避免
# 循环导入。只 import 包、不重导出具体后端类——取实例一律经各 Producer，不直接引用实现类。
# 集中式注册引导另见 :func:`storage.bootstrap.register_backends`（装配入口在组装前调用一次）。
for _module_name in (
    "fs_impl",
    "fulltext_impl",
    "fusion_impl",
    "graph_impl",
    "kv_impl",
    "vector_impl",
    "entity_impl",
):
    import_module(f"{__name__}.{_module_name}")

__all__ = [
    "BaseStore",
    "StoreType",
    "KVStore",
    "FulltextStore",
    "VectorStore",
    "GraphStore",
    "FusionStore",
    "FSStore",
    "EntityStore",
    "EntityStoreProducer",
    "ScoredID",
    "ScoredHit",
    "Document",
    "TextQuery",
    "VectorRecord",
    "VectorQuery",
    "Node",
    "Edge",
    "GraphQuery",
    "KVMemoryListResult",
    "FusionRecord",
    "FusionQuery",
    "FileStat",
    "Storage",
    "StorageCapability",
    "StorageProducer",
    "CompositeStorage",
]
