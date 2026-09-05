# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆存储层（F 层）接口：统一 CRUD 的可插拔存储抽象。

管理面（端口/能力/授权代理）与数据面（领域 CRUD + 检索适配）分处两个 ABC：
:class:`~storage.store_manager.StoreManager` 与 :class:`~storage.domain_store.DomainStore`
（F07 拆分，详见 ``docs/features/storage/F07-storage-manager-domain-store-split.md``）。
"""

from importlib import import_module

from .base import BaseStore, StoreType
from .domain_store import DomainStore, DomainStoreProducer
from .entity_store import EntityStore, EntityStoreProducer
from .fs import FSStore
from .fulltext import FulltextStore
from .fusion import FusionStore
from .graph import GraphStore
from .kv import KVStore
from .store_manager import StorageCapability, StoreManager, StoreManagerProducer
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
    "store_manager_impl",
    "domain_store_impl",
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
    "StoreManager",
    "StorageCapability",
    "StoreManagerProducer",
    "DomainStore",
    "DomainStoreProducer",
]
