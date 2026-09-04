# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ConfigSource 稳定 key 约定（S08 推荐路径的常量与拼接辅助）。

业务配置与加密密钥（:class:`~common.security.key_source.KeySource`）分属不同抽象：
本模块只服务 ConfigSource 的字符串 key（``globals.*`` / ``prompts.*`` / ``llm.model`` 等）。
"""

from __future__ import annotations

# 跨切面开关前缀：globals.<name>
GLOBALS_PREFIX = "globals."

# prompt：prompts.<phase>.<name>
PROMPTS_PREFIX = "prompts."

# 插件命名空间（与 Producer TOP_NAME 对齐）
NS_LLM = "llm"
NS_EMBEDDER = "embedder"
NS_RERANKER = "reranker"
NS_KV_STORE = "kv_store"
NS_VECTOR_STORE = "vector_store"
NS_FULLTEXT_STORE = "fulltext_store"
NS_GRAPH_STORE = "graph_store"
NS_FUSION_STORE = "fusion_store"
NS_FS_STORE = "fs_store"
# 统一 Storage 命名空间（F02 RoutingStorage；与底层 *_store 命名空间分层）
NS_STORAGE = "storage"

# 晚绑定字段名
FIELD_ACTIVE = "active"
FIELD_MODEL = "model"
FIELD_API_KEY = "api_key"
FIELD_BASE_URL = "base_url"


def global_key(name: str) -> str:
    """构造 ``globals.<name>``（如 ``globals.vector_enabled``）。"""
    return f"{GLOBALS_PREFIX}{name}"


def prompt_key(phase: str, name: str) -> str:
    """构造 ``prompts.<phase>.<name>``（如 ``prompts.extract.episodic``）。"""
    return f"{PROMPTS_PREFIX}{phase}.{name}"


def namespaced_key(namespace: str, field: str) -> str:
    """构造 ``<namespace>.<field>``，如 ``embedder.active``、``llm.api_key``。"""
    return f"{namespace}.{field}"


def active_key(namespace: str) -> str:
    """构造 ``<namespace>.active``（异质多实例次选路径）。"""
    return namespaced_key(namespace, FIELD_ACTIVE)
