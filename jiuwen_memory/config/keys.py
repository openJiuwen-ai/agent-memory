# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ConfigSource 稳定 key 约定（S08 推荐路径的常量与拼接辅助）。

业务配置与加密密钥（:class:`~common.security.cryptography.key_provider.KeyProvider`，
随 PR1 实装已并入安全域）分属不同抽象：
本模块只服务 ConfigSource 的字符串 key（``globals.*`` / ``prompts.*`` / ``llm.model`` 等）。
"""

from __future__ import annotations

from collections.abc import Mapping

from jiuwen_memory.common.errors import ValidationError

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
# StoreManager 命名空间（F07 起顶层段名从 storage 改为 store_manager；与底层 *_store 命名空间分层）
NS_STORE_MANAGER = "store_manager"

# 晚绑定字段名
FIELD_ACTIVE = "active"
FIELD_MODEL = "model"
FIELD_API_KEY = "api_key"
FIELD_BASE_URL = "base_url"

# globals.store_manager：全局共享 StoreManager 的指名键（值 = store_manager 命名空间下的实例名）。
# 消费者不再逐个声明 storage 引用；StoreManagerProducer.resolve 统一经此键取全局实例。
GLOBALS_STORAGE_MANAGER = "store_manager"


def global_key(name: str) -> str:
    """构造 ``globals.<name>``（如 ``globals.vector_enabled``）。"""
    return f"{GLOBALS_PREFIX}{name}"


def prompt_key(phase: str, name: str) -> str:
    """构造 ``prompts.<phase>.<name>``（如 ``prompts.extract.episodic``）。"""
    return f"{PROMPTS_PREFIX}{phase}.{name}"


def namespaced_key(namespace: str, field: str) -> str:
    """构造 ``<namespace>.<field>``，如 ``embedder.active``、``llm.api_key``。"""
    return f"{namespace}.{field}"


def instance_namespace(namespace: str, instance_name: str) -> str:
    """返回具名实例的晚绑定命名空间；default 复用命名空间简写。"""
    if not instance_name or instance_name == "default":
        return namespace
    return f"{namespace}.{instance_name}"


def resolve_instance_namespace(
    namespace: str,
    instance_name: str,
    params: Mapping[str, object],
) -> str:
    """解析实例命名空间；显式值只读实例 params，且必须是非空字符串。"""
    if "config_namespace" not in params:
        return instance_namespace(namespace, instance_name)
    explicit_namespace = params["config_namespace"]
    if not isinstance(explicit_namespace, str):
        raise ValidationError(
            f"{namespace}.{instance_name or 'default'} params.config_namespace 必须是字符串"
        )
    normalized = explicit_namespace.strip()
    if not normalized:
        raise ValidationError(
            f"{namespace}.{instance_name or 'default'} params.config_namespace 不能为空"
        )
    return normalized


def active_key(namespace: str) -> str:
    """构造 ``<namespace>.active``（异质多实例次选路径）。"""
    return namespaced_key(namespace, FIELD_ACTIVE)
