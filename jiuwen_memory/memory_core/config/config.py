# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from jiuwen_memory.common.schema.param import Param
from jiuwen_memory.common.security.crypt_utils import AES_KEY_LENGTH
from jiuwen_memory.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from jiuwen_memory.retrieval.common.config import EmbeddingConfig
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.memory_core.process.forgetting.evaluator import ForgetEvaluator


class MemoryEngineConfig(BaseModel):
    default_model_cfg: ModelRequestConfig = Field(default=None)
    default_model_client_cfg: ModelClientConfig = Field(default=None)
    forbidden_variables: str = Field(default="")  # forbidden variables config, split by comma
    input_msg_max_len: int = Field(default=8192)  # max length of input message
    crypto_key: bytes = Field(default=b'')  # aes key, length must be 32, not enable encrypt memory if empty
    single_turn_history_summary_max_token: int = Field(default=128, gt=0)
    enable_middle_memory: bool = Field(default=False)  # enable middle memory or not
    middle_memory_check_interval: int = Field(default=50)  # middle memory check interval, default 50
    # registered codec name (e.g. "sm4", "hsm"). Empty -> default
    # AesStorageCodec built from crypto_key. The name must have been
    # registered via register_storage_codec() (a pre-built instance) before
    # set_config is called; if not registered, falls back to AesStorageCodec.
    codec: str = Field(default="")

    @field_validator('crypto_key')
    @classmethod
    def check_crypto_key(cls, v: bytes) -> bytes:
        if len(v) == 0:
            return b''

        if len(v) == AES_KEY_LENGTH:
            return v

        raise build_error(
            StatusCode.MEMORY_SET_CONFIG_EXECUTION_ERROR,
            config_type="crypto_key",
            error_msg=f"crypto_key must be empty or {AES_KEY_LENGTH} bytes length",
        )


class MemoryScopeConfig(BaseModel):
    model_cfg: ModelRequestConfig = Field(default=None)
    model_client_cfg: ModelClientConfig = Field(default=None)
    embedding_cfg: EmbeddingConfig = Field(default=None)
    # user-defined rules for user profile extraction
    user_profile_definition: str = Field(default="用户本人的肯定或否定表述（包含不限于基本身份、兴趣偏好、人际关系、资产状况）")
    # user-defined rules for semantic memory extraction
    semantic_memory_definition: str = Field(default="用户对话中涉及的和时间无明确关系的事实性内容或概念")
    # user-defined rules for episodic memory extraction
    episodic_memory_definition: str = Field(default="用户对话中涉及的和时间有明确关系的事实性内容或概念")
    # user-defined rules for important memory tagging — feeds
    # the ``important_memory_definition`` placeholder in extraction prompts.
    # Memories matching this definition get ``is_important=True`` from the
    # LLM extractor, which protects them from Ebbinghaus forgetting.
    important_memory_definition: str = Field(
        default="涉及用户身份、关键决策、长期承诺、不可替代的事实性内容"
    )
    # 是否同时抽取 assistant 角色说话人的记忆（多主体/双人对话场景）；默认仅抽取 user 角色
    extract_assistant_memory: bool = Field(default=False)


class AgentMemoryConfig(BaseModel):
    mem_variables: list[Param] = Field(default_factory=list)  # memory variables config
    enable_long_term_mem: bool = Field(default=True)  # enable long term memory or not
    enable_user_profile: bool = Field(default=True)  # enable user profile memory or not
    enable_semantic_memory: bool = Field(default=True)  # enable semantic memory or not
    enable_episodic_memory: bool = Field(default=True)  # enable episodic memory or not
    enable_summary_memory: bool = Field(default=True)  # enable summary memory or not


class ForgettingConfig(BaseModel):
    """
    Config for the Ebbinghaus forgetting step that runs inside dreaming.

    The forgetting step scores candidate memories using an Ebbinghaus-style
    decay formula and marks low-score memories as ``blacklisted`` (soft
    forgetting — they are kept in storage for recall but excluded from
    normal search).

    The actual scoring algorithm is implemented by ``ForgetEvaluator``
    subclasses. ``evaluator`` is optional: when ``None``, the framework
    falls back to the built-in ``EbbinghausEvaluator`` constructed with
    the runtime ``kv_store``.

    ``model_config`` enables ``arbitrary_types_allowed`` so that the
    ``ForgetEvaluator`` ABC (an arbitrary Python type, not a pydantic
    model) can be carried on the field. The evaluator instance is
    opaque to pydantic — it is never serialized, only passed by
    reference to the dreaming orchestrator.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = Field(default=False)
    threshold: float = Field(default=0.15, ge=0, le=1)  # memories with score < threshold are evicted
    max_evict: int = Field(default=1000, ge=1)  # cap on evictions per sweep (anti-avalanche)
    min_retention_days: int = Field(default=30, ge=0)  # never evict memories accessed within this window
    evaluator: Optional["ForgetEvaluator"] = Field(default=None)
    # None → default EbbinghausEvaluator(self.kv_store)
    # instance → use as-is (custom subclasses)


class DreamingConfig(BaseModel):
    """
    Config for the offline dreaming (cross-session consolidation) process.

    Constructed by the caller and passed into ``LongTermMemory.start_dreaming``;
    not read from any global config file.
    """
    enabled: bool = Field(default=False)
    interval_seconds: float = Field(default=14400.0, gt=0)  # 4h
    min_session_rounds: int = Field(default=4, ge=1)  # pre-filter: skip sessions with fewer rounds
    max_sessions_per_sweep: int = Field(default=10, ge=1)  # cap sessions processed per sweep
    max_compress_tokens: int = Field(default=30000, gt=0)  # compression token budget
    max_items_per_session: int = Field(default=5, ge=1)  # cap knowledge items extracted per session
    forgetting: Optional[ForgettingConfig] = Field(default=None)
    # None = forgetting step is not wired into dreaming
