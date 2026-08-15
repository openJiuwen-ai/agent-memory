"""内置默认装配配置：离线进程内栈，用**显式具名 + 引用**复刻系统的共享拓扑。

``build_kernel`` 未传 config 时用它装配；传 config 时把用户配置**合并覆盖**到它之上
（按 namespace/实例名覆盖、globals 按 key 覆盖）。

约定：每种组件在自己的命名空间下声明一个名为 ``default`` 的具名实例；需要**对象共享**的
有状态后端（kv / vector / fulltext / graph store、audit、scheduler 等）由各消费方**同名引用**
``default`` → 经 :meth:`Factory.build_named` 命中同一缓存键 → 同一实例。``recaller`` 有三路
（keyword / vector / graph），故声明三个具名实例。
"""

from __future__ import annotations

from typing import Any

from .context import AssemblyContext

_D = "default"

# 默认 prompts 段（顶层 ``prompts``）：按 phase/key 命名引用。metadata 只写 key，
# 运行时由 PromptRegistry 按 key 查文本。这里给出空骨架——具体 prompt 由用户在 yml
# 里覆盖；空 dict 表示该 phase 下无可用 prompt（consolidate/reflect 步会回退到规则）。
_PROMPTS_DEFAULT: dict[str, dict[str, str]] = {
    "extract": {},
    "consolidate": {},
    "reflect": {},
}


def default_config_dict() -> dict[str, Any]:
    """返回内置默认配置（两级命名空间字典）。"""
    return {
        "globals": {
            "vector_enabled": True,
            "graph_enabled": True,
            "rerank_enabled": True,
            "embedder_dim": 64,
            "chunk_size": 120,
        },
        # 顶层 prompts 段：按 phase（consolidate/reflect）→ key → prompt 文本。
        # 与 globals 同级，由 AssemblyContext 抽取到 globals["prompts"] 供
        # PromptRegistry 加载。metadata 只引用 key，不内联 prompt 文本。
        "prompts": _PROMPTS_DEFAULT,
        # -- 存储（有状态，必须对象共享）-------------------------------------- #
        "kv_store": {_D: "memory"},
        # 安全 provider：默认声明为 local 信封加密（AES-256-GCM），仅供 opt-in encrypted KV 引用。
        # F04 §5.4：默认装配不强制包装 EncryptedKVStore；用户配 kv_store.default.target=encrypted
        # 时由 @KvProducer.register("encrypted") builder 经 SecurityProducer.dep(config) 取此实例。
        # local provider 的 create_key_file 默认 False：未注入密钥源且 key_file 不存在时装配 fail-closed。
        "security": {_D: "local"},
        "vector_store": {
            _D: "memory",
            # L0/L1 分表（与构建侧同命名 layers_l0/l1；同后端不同 collection）
            "layers_l0": "memory",
            "layers_l1": "memory",
        },
        "graph_store": {_D: "memory"},
        "fulltext_store": {
            _D: {"target": "memory", "params": {"tokenizer": _D}},
            # L0/L1 分表（与构建侧同命名 layers_l0/l1）
            "layers_l0": {"target": "memory", "params": {"tokenizer": _D}},
            "layers_l1": {"target": "memory", "params": {"tokenizer": _D}},
        },
        "storage": {
            _D: {
                "target": "composite",
                "params": {
                    "kv_store": _D,
                    "vector_store": _D,
                    "fulltext_store": _D,
                    "graph_store": _D,
                    "preferred_retrieval_pipeline": "recall_get_rank",
                },
            }
        },
        # -- 共享插件 -------------------------------------------------------- #
        "tokenizer": {_D: "whitespace"},
        "embedder": {_D: {"target": "hashing", "params": {"tokenizer": _D}}},
        "chunker": {_D: "fixed_window"},
        "feature_extractor": {_D: {"target": "keyword", "params": {"tokenizer": _D}}},
        "llm": {_D: "echo"},
        "reranker": {_D: {"target": "overlap", "params": {"tokenizer": _D}}},
        "normalizer": {_D: "passthrough"},
        "audit": {_D: {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        # -- 检索 ------------------------------------------------------------ #
        "recaller": {
            "keyword": {"target": "keyword", "params": {"storage": _D}},
            "keyword_l0": {"target": "keyword_l0"},
            "keyword_l1": {"target": "keyword_l1"},
            "vector": {
                "target": "vector",
                "params": {"storage": _D, "min_similarity": 0.0},
            },
            "vector_l0": {"target": "vector_l0"},
            "vector_l1": {"target": "vector_l1"},
            "graph": {"target": "graph", "params": {"storage": _D}},
        },
        "query_parser": {
            _D: {
                "target": "simple",
                "params": {
                    "tokenizer": _D,
                    "embedder": _D,
                    "llm": _D,
                    "feature_extractor": _D,
                    "sanitize_enabled": True,
                    "sanitize_strip_code": False,
                },
            }
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
                    # L0/L1 分层召回开关：回退 globals.layers_index_enabled；构建/召回侧默认均 true
                    # （默认建默认查）。不在 params 硬编码，让 get 回退 globals，便于全局关停。
                    "keyword_l0_recaller": "keyword_l0",
                    "keyword_l1_recaller": "keyword_l1",
                    "vector_l0_recaller": "vector_l0",
                    "vector_l1_recaller": "vector_l1",
                    "reranker": _D,
                    "query_parser": _D,
                    "fuser": _D,
                    "discloser": _D,
                    "storage": _D,
                    # 召回超采样 + 精排预算 + 相关性阈值（本算子私有调参，非跨切面）
                    "over_fetch_factor": 4,
                    "over_fetch_floor": 60,
                    "recall_max": 100,
                    "rerank_max": 60,
                    "min_score": 0.0,
                    # 相对阈值默认关闭（校准/未校准两路均是）：按最高分比例裁剪会随
                    # 融合分布变化误杀尾部候选（分层召回下尤甚——有无 layers 属索引
                    # 覆盖差异，不是相关性差异）。裁剪交由调用方 top_k 决定，需要时
                    # 按场景显式配置。
                    "min_score_ratio": 0.0,
                    "min_score_ratio_uncalibrated": 0.0,
                    "min_results": 0,
                },
            }
        },
        # -- 构建 ------------------------------------------------------------ #
        "extractor": {
            _D: {
                "target": "dynamic_llm",
                "params": {"llm": _D, "fallback": "legacy"},
            },
            "legacy": {"target": "keyword", "params": {"chunker": _D}},
        },
        "abstractor": {_D: "concat"},
        "associator": {_D: {"target": "keyword", "params": {"feature_extractor": _D}}},
        "classifier": {_D: "llm"},  # infer=false 默认路径用 LLM classifier 打 tier+tags
        "constructor": {
            _D: {
                "target": "hybrid",
                "params": {
                    "storage": _D,
                    "chunker": _D,
                    "embedder": _D,
                },
            }
        },
        "dedup": {
            _D: {
                "target": "vector",
                "params": {"storage": _D, "embedder": _D},
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
                    "storage": _D,
                    "dedup": _D,
                    "llm": _D,
                },
            },
            # dynamic：EXTRACT 走动态 prompt 四步编排（extract→consolidate→reflect→落盘）。
            # 与 orchestrating 平级；装配或 pipeline profile 选它即启用动态路径。
            "dynamic": {
                "target": "dynamic",
                "params": {
                    "extractor": _D,
                    "abstractor": _D,
                    "associator": _D,
                    "index_builder": _D,
                    "storage": _D,
                    "dedup": _D,
                    "llm": _D,
                },
            },
        },
        # -- 摄取 ------------------------------------------------------------ #
        "ingestor": {_D: {"target": "simple", "params": {"normalizer": _D}}},
        # -- 控制 ------------------------------------------------------------ #
        "engine": {
            _D: {
                "target": "in_memory",
                "params": {
                    "ingestor": _D,
                    "index_builder": _D,
                    "retriever": _D,
                    "storage": _D,
                    "scheduler": _D,
                    "evolver": _D,
                    "lifecycle": _D,
                    "job_factory": _D,
                },
            }
        },
        # JobFactory 顶层命名空间：装配期把各 Job 类型的 Spec 注册到 JobFactory。
        "job_factory": {
            _D: {
                "target": "default",
                "params": {
                    # 与 engine.default 共享统一 Storage 具名实例。
                    "storage": _D,
                    "evolver": _D,
                    "lifecycle": _D,
                    "index_builder": _D,
                    "llm": _D,
                    # MiddleToLongJob 业务参数
                    "middle_max_fetch": 100,    # _list_working_units 取最近 N 条
                    "middle_batch_size": 10,    # 连续性切批上限
                    "middle_concurrency": 4,    # 批间并发（1=串行）
                },
            }
        },
        # scheduler 只接收 Job（Job 自带数据源），无 params。
        "scheduler": {_D: {"target": "in_process", "params": {}}},
        "lifecycle": {_D: {"target": "kv", "params": {"storage": _D, "policy": _D}}},
        "policy": {_D: "dict"},
        "governor": {_D: {"target": "in_memory", "params": {"audit": _D, "storage": _D}}},
        "permission": {_D: {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        "space": {_D: {"target": "kv", "params": {"storage": _D}}},
        # 可插拔配置来源：默认装配快照；产品可覆盖为 dict/overlay/自研 target
        "config_source": {_D: "yaml_defaults"},
    }


# 根组件（LocalMemoryAPI）对各顶层组件的引用——全部指向各命名空间下的 default 实例。
ROOT_PARAMS: dict[str, str] = {
    "engine": _D,
    "permission": _D,
    "scheduler": _D,
    "policy": _D,
    "governor": _D,
    "audit": _D,
    "kv_store": _D,
    "security": _D,
    "storage": _D,
    "space": _D,
    "config_source": _D,
}

KV_DEFAULT_NAME = _D  # 注入的真源 kv 预置进缓存时用的具名键（与各处引用一致）


def default_context() -> AssemblyContext:
    """构造内置默认装配上下文。"""
    return AssemblyContext.from_dict(default_config_dict())
