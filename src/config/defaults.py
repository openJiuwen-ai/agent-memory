"""内置默认装配配置：纯内存离线栈，用**显式具名 + 引用**复刻系统的共享拓扑。

``build_kernel`` 未传 config 时用它装配；传 config 时把用户配置**合并覆盖**到它之上
（按 namespace/实例名覆盖、globals 按 key 覆盖）。

约定：每种组件在自己的命名空间下声明一个名为 ``default`` 的具名实例；需要**对象共享**的
有状态后端（kv / vector / fulltext / graph store、audit、scheduler 等）由各消费方**同名引用**
``default`` → 经 :meth:`Factory.build_named` 命中同一缓存键 → 同一实例。``recaller`` 有三路
（keyword / vector / graph），故声明三个具名实例。
"""

from __future__ import annotations

from typing import Any, Dict

from .context import AssemblyContext

_D = "default"


def default_config_dict() -> Dict[str, Any]:
    """返回内置默认配置（两级命名空间字典）。"""
    return {
        "globals": {
            "vector_enabled": True,
            "graph_enabled": True,
            "rerank_enabled": True,
            "embedder_dim": 64,
            "chunk_size": 120,
        },
        # -- 存储（有状态，必须对象共享）-------------------------------------- #
        "kv_store": {_D: "memory"},
        "vector_store": {_D: "memory"},
        "graph_store": {_D: "memory"},
        "fulltext_store": {_D: {"target": "memory", "params": {"tokenizer": _D}}},
        # -- 共享插件 -------------------------------------------------------- #
        "tokenizer": {_D: "whitespace"},
        "embedder": {_D: {"target": "hashing", "params": {"tokenizer": _D}}},
        "chunker": {_D: "fixed_window"},
        "feature_extractor": {_D: {"target": "keyword", "params": {"tokenizer": _D}}},
        "llm": {_D: "echo"},
        "reranker": {_D: {"target": "overlap", "params": {"tokenizer": _D}}},
        "normalizer": {_D: "passthrough"},
        "audit": {_D: "in_memory"},
        # -- 检索 ------------------------------------------------------------ #
        "recaller": {
            "keyword": {"target": "keyword", "params": {"fulltext_store": _D}},
            "vector": {"target": "vector", "params": {"vector_store": _D}},
            "graph": {"target": "graph", "params": {"graph_store": _D}},
        },
        "query_parser": {
            _D: {"target": "simple", "params": {"tokenizer": _D, "embedder": _D, "llm": _D}}
        },
        "fuser": {_D: "rrf"},
        "discloser": {_D: "truncating"},
        "retriever": {
            _D: {
                "target": "pipeline",
                "params": {
                    "keyword_recaller": "keyword",
                    "vector_recaller": "vector",
                    "graph_recaller": "graph",
                    "reranker": _D,
                    "query_parser": _D,
                    "fuser": _D,
                    "discloser": _D,
                    "kv_store": _D,
                },
            }
        },
        # -- 构建 ------------------------------------------------------------ #
        "extractor": {_D: {"target": "keyword", "params": {"chunker": _D}}},
        "abstractor": {_D: "concat"},
        "associator": {_D: {"target": "keyword", "params": {"feature_extractor": _D}}},
        "classifier": {_D: "keyword"},
        "constructor": {
            _D: {
                "target": "hybrid",
                "params": {
                    "fulltext_store": _D,
                    "vector_store": _D,
                    "kv_store": _D,
                    "chunker": _D,
                    "embedder": _D,
                },
            }
        },
        "dedup": {
            _D: {
                "target": "vector",
                "params": {"vector_store": _D, "embedder": _D, "kv_store": _D},
            }
        },
        "evolver": {
            _D: {
                "target": "orchestrating",
                "params": {
                    "extractor": _D,
                    "abstractor": _D,
                    "associator": _D,
                    "index_builder": _D,
                    "kv_store": _D,
                    "graph_store": _D,
                    "dedup": _D,
                    "llm": _D,
                },
            }
        },
        # -- 摄取 ------------------------------------------------------------ #
        "ingestor": {_D: {"target": "simple", "params": {"normalizer": _D}}},
        # -- 控制 ------------------------------------------------------------ #
        "engine": {
            _D: {
                "target": "in_memory",
                "params": {
                    "ingestor": _D,
                    "classifier": _D,
                    "index_builder": _D,
                    "retriever": _D,
                    "kv_store": _D,
                    "scheduler": _D,
                    "evolver": _D,
                    "lifecycle": _D,
                },
            }
        },
        "scheduler": {
            _D: {"target": "in_process", "params": {"kv_store": _D, "evolver": _D}}
        },
        "lifecycle": {_D: {"target": "kv", "params": {"kv_store": _D, "policy": _D}}},
        "policy": {_D: "dict"},
        "governor": {_D: {"target": "in_memory", "params": {"audit": _D, "kv_store": _D}}},
        "permission": {_D: "allow_all"},
    }


# 根组件（LocalMemoryAPI）对各顶层组件的引用——全部指向各命名空间下的 default 实例。
ROOT_PARAMS: Dict[str, str] = {
    "engine": _D,
    "permission": _D,
    "scheduler": _D,
    "policy": _D,
    "governor": _D,
    "audit": _D,
    "kv_store": _D,
}

KV_DEFAULT_NAME = _D  # 注入的真源 kv 预置进缓存时用的具名键（与各处引用一致）


def default_context() -> AssemblyContext:
    """构造内置默认装配上下文。"""
    return AssemblyContext.from_dict(default_config_dict())
