# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from pydantic import BaseModel, Field, field_validator
from common.schema.param import Param
from common.security.crypt_utils import AES_KEY_LENGTH
from foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from retrieval.common.config import EmbeddingConfig
from common.exception.codes import StatusCode
from common.exception.errors import build_error


class MemoryEngineConfig(BaseModel):
    default_model_cfg: ModelRequestConfig = Field(default=None)
    default_model_client_cfg: ModelClientConfig = Field(default=None)
    forbidden_variables: str = Field(default="")  # forbidden variables config, split by comma
    input_msg_max_len: int = Field(default=8192)  # max length of input message
    crypto_key: bytes = Field(default=b'')  # aes key, length must be 32, not enable encrypt memory if empty
    single_turn_history_summary_max_token: int = Field(default=128, gt=0)

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
    # 是否同时抽取 assistant 角色说话人的记忆（多主体/双人对话场景）；默认仅抽取 user 角色
    extract_assistant_memory: bool = Field(default=False)


class AgentMemoryConfig(BaseModel):
    mem_variables: list[Param] = Field(default_factory=list)  # memory variables config
    enable_long_term_mem: bool = Field(default=True)  # enable long term memory or not
    enable_user_profile: bool = Field(default=True)  # enable user profile memory or not
    enable_semantic_memory: bool = Field(default=True)  # enable semantic memory or not
    enable_episodic_memory: bool = Field(default=True)  # enable episodic memory or not
    enable_summary_memory: bool = Field(default=True)  # enable summary memory or not


class DreamingConfig(BaseModel):
    """
    Config for the offline dreaming (cross-session consolidation) process.

    Constructed by the caller and passed into ``LongTermMemory.start_dreaming``;
    not read from any global config file.
    """
    enabled: bool = Field(default=False)
    interval_seconds: float = Field(default=14400.0, gt=0)   # 4h
    min_session_rounds: int = Field(default=4, ge=1)         # pre-filter: skip sessions with fewer rounds
    max_sessions_per_sweep: int = Field(default=10, ge=1)    # cap sessions processed per sweep
    max_compress_tokens: int = Field(default=30000, gt=0)    # compression token budget
    max_items_per_session: int = Field(default=5, ge=1)      # cap knowledge items extracted per session
