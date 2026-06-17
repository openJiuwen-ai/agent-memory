# memory_core.config

`memory_core.config` is the unified **memory configuration management module** in JiuwenMemory, responsible for:

- Defining `MemoryEngineConfig` — global engine configuration;
- Defining `MemoryScopeConfig` — scope-level configuration (for model/vector parameters in different business scenarios);
- Defining `AgentMemoryConfig` — agent-level memory strategy configuration (for defining variable memories to extract and whether to enable long-term memory).


## class memory_core.config.config.MemoryEngineConfig

```
class memory_core.config.config.MemoryEngineConfig(default_model_cfg: ModelRequestConfig | None = None, default_model_client_cfg: ModelClientConfig | None = None, input_msg_max_len: int = 8192, crypto_key: bytes = b'')
```

Global memory engine configuration for setting engine-level common parameters.

**Parameters**:

* **default_model_cfg**(ModelRequestConfig | None, optional): Default LLM request parameters for memory generation (model name, temperature, max tokens, etc.); if `None`, memory cannot be generated (unless configured per scope via `MemoryScopeConfig`). Default: `None`.
* **default_model_client_cfg**(ModelClientConfig | None, optional): Default LLM client configuration (`client_id / client_provider / api_base / api_key / verify_ssl`, etc.); if `None`, memory cannot be generated (unless configured per scope). Default: `None`.
* **forbidden_variables**(str, optional): Variables forbidden from being memorized (comma-separated variable names); default: `""` (no variables forbidden).
* **input_msg_max_len**(int, optional): Maximum input message length (in characters); messages exceeding this length will be truncated during memory generation. Default: 8192.
* **crypto_key**(bytes, optional): AES-256-GCM encryption key, must be exactly 32 bytes. If set to a non-empty byte string, `set_config` will automatically inject `AesStorageCodec` into `memory_index` (`BaseMemoryIndex`) for transparent encryption/decryption of the `text` field at the storage layer; it will also be used to encrypt sensitive parameters like `api_key` in `MemoryScopeConfig`. If empty `b''`, all encryption/decryption is disabled. Default: `b''` (no encryption).
* **single_turn_history_summary_max_token**(int, optional): Maximum number of tokens for single-turn history summary generation; must be greater than 0. Default: 128.

**Parameter Validation**:

The `crypto_key` parameter has a `field_validator`:

- If length is 0, it remains empty (no encryption);
- If length equals `AES_KEY_LENGTH` (32), validation passes;
- Otherwise, an exception is raised (`MEMORY_SET_CONFIG_EXECUTION_ERROR`) with message: `"crypto_key must be empty or {AES_KEY_LENGTH} bytes length"`.

**Example**:

```python
>>> from memory_core.config import MemoryEngineConfig
>>> from foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> 
>>> # Create global engine configuration
>>> engine_config = MemoryEngineConfig(
>>>     default_model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.0,
>>>     ),
>>>     default_model_client_cfg=ModelClientConfig(
>>>         client_id="default_memory_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-xxxx",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     forbidden_variables="user_id, phone_number, email",
>>>     input_msg_max_len=8192,
>>>     crypto_key=b"your-32-byte-aes-key-here!!",  # 32 bytes
>>> )
```


## class memory_core.config.config.MemoryScopeConfig

```
class memory_core.config.config.MemoryScopeConfig(model_cfg: ModelRequestConfig | None = None, model_client_cfg: ModelClientConfig | None = None, embedding_cfg: EmbeddingConfig | None = None, user_profile_definition: str = "Affirmative or negative statements about the user (including but not limited to identity, interests, relationships, assets)", semantic_memory_definition: str = "Factual content or concepts in user conversations that have no explicit temporal relationship", episodic_memory_definition: str = "Factual content or concepts in user conversations that have an explicit temporal relationship")
```

Scope-level memory configuration for defining independent model and vector parameters for different `scope_id` values.

**Parameters**:

* **model_cfg**(ModelRequestConfig | None, optional): LLM request configuration for this scope (model name, temperature, etc.); if `None`, falls back to the global `MemoryEngineConfig.default_model_cfg`. Default: `None`.
* **model_client_cfg**(ModelClientConfig | None, optional): LLM client configuration for this scope (`client_id / api_base / api_key`, etc.); if `None`, falls back to the global `MemoryEngineConfig.default_model_client_cfg`. Default: `None`.
* **embedding_cfg**(EmbeddingConfig | None, optional): Embedding model configuration for this scope (`model_name / base_url / api_key`, etc.); if `None`, semantic search may be unavailable (depending on whether a global embedding model is provided). Default: `None`.
* **user_profile_definition**(str, optional): Definition rule for user profile memory extraction, used to customize the scope of user profile information extracted from conversations. Default: `"Affirmative or negative statements about the user (including but not limited to identity, interests, relationships, assets)"`.
* **semantic_memory_definition**(str, optional): Definition rule for semantic memory extraction, used to customize the scope of semantic memory information extracted from conversations. Default: `"Factual content or concepts in user conversations that have no explicit temporal relationship"`.
* **episodic_memory_definition**(str, optional): Definition rule for episodic memory extraction, used to customize the scope of episodic memory information extracted from conversations. Default: `"Factual content or concepts in user conversations that have an explicit temporal relationship"`.

> **Note**: The `api_key` parameter in `MemoryScopeConfig` is automatically encrypted when saved to `kv_store` (using `MemoryEngineConfig.crypto_key`) and automatically decrypted when read.

**Example**:

```python
>>> from memory_core.config import MemoryScopeConfig
>>> from foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> from retrieval.common.config import EmbeddingConfig
>>> 
>>> # Create scope configuration
>>> scope_config = MemoryScopeConfig(
>>>     model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.1,
>>>     ),
>>>     model_client_cfg=ModelClientConfig(
>>>         client_id="scope_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-yyyy",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     embedding_cfg=EmbeddingConfig(
>>>         model_name="text-embedding-3-small",
>>>         base_url="https://api.openai.com/v1",
>>>         api_key="sk-zzzz",
>>>     ),
>>> )
```


## class memory_core.config.config.AgentMemoryConfig

```
class memory_core.config.config.AgentMemoryConfig(mem_variables: list[Param] = [], enable_long_term_mem: bool = True, enable_user_profile: bool = True, enable_semantic_memory: bool = True, enable_episodic_memory: bool = True, enable_summary_memory: bool = True)
```

Agent-level memory strategy configuration that describes which types of memory an agent wants to extract and manage.

**Parameters**:

* **mem_variables**(list[Param], optional): Variable memory configuration list; each `Param` defines a variable name, description, type, whether it is required, etc.; during `LongTermMemory.add_messages`, variable values are extracted from conversations based on these configurations and saved. Default: `[]`.
* **enable_long_term_mem**(bool, optional): Whether to enable long-term memory generation; when `True`, user profiles (long-term memory) are extracted from conversations and saved to the semantic store; when `False`, only messages and variable memories are saved without generating user profiles. Default: `True`.
* **enable_user_profile**(bool, optional): Whether to enable user profile generation and usage; when `True`, user personal information (such as name, phone number, etc.) is extracted from conversations and saved to the semantic store, and user profiles are used in subsequent searches; when `False`, user profiles are not generated or used. Default: `True`.
* **enable_semantic_memory**(bool, optional): Whether to enable semantic memory generation; when `True`, semantic memories are extracted from conversations and saved to the semantic store, and used in subsequent searches; when `False`, semantic memories are not generated or used. Default: `True`.
* **enable_episodic_memory**(bool, optional): Whether to enable episodic memory generation; when `True`, episodic memories are extracted from conversations and saved to the semantic store, and used in subsequent searches; when `False`, episodic memories are not generated or used. Default: `True`.
* **enable_summary_memory**(bool, optional): Whether to enable user summary memory generation; when `True`, user summaries (such as recent conversation content) are extracted from conversations and saved to the semantic store; when `False`, user summary memories are not generated. Default: `True`.

> **Note**: The `Param` type is defined in `common.schema.param` and typically includes `name / description / type / required` parameters.

**Example**:

```python
>>> from memory_core.config import AgentMemoryConfig
>>> from common.schema.param import Param
>>> 
>>> # Create agent memory strategy configuration
>>> agent_config = AgentMemoryConfig(
>>>     mem_variables=[
>>>         Param(
>>>             name="favorite_color",
>>>             description="User's favorite color",
>>>             type="string",
>>>             required=False,
>>>         ),
>>>         Param(
>>>             name="age",
>>>             description="User's age",
>>>             type="number",
>>>             required=False,
>>>         ),
>>>     ],
>>>     enable_long_term_mem=True,
>>>     enable_user_profile=True,
>>>     enable_semantic_memory=True,
>>>     enable_episodic_memory=True,
>>>     enable_summary_memory=True,
>>> )
```


## Usage Example

```python
>>> from memory_core import (
>>>     MemoryEngineConfig,
>>>     MemoryScopeConfig,
>>>     AgentMemoryConfig,
>>> )
>>> from foundation.llm.schema.config import (
>>>     ModelClientConfig,
>>>     ModelRequestConfig,
>>> )
>>> from retrieval.common.config import EmbeddingConfig
>>> from common.schema.param import Param
>>> 
>>> # 1. Create global engine configuration
>>> engine_config = MemoryEngineConfig(
>>>     default_model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.0,
>>>     ),
>>>     default_model_client_cfg=ModelClientConfig(
>>>         client_id="default_memory_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-xxxx",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     forbidden_variables="user_id, phone_number, email",
>>>     input_msg_max_len=8192,
>>>     crypto_key=b"your-32-byte-aes-key-here!!",  # 32 bytes
>>> )
>>> 
>>> # 2. Create scope configuration (optional)
>>> scope_config = MemoryScopeConfig(
>>>     model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.1,
>>>     ),
>>>     model_client_cfg=ModelClientConfig(
>>>         client_id="scope_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-yyyy",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     embedding_cfg=EmbeddingConfig(
>>>         model_name="text-embedding-3-small",
>>>         base_url="https://api.openai.com/v1",
>>>         api_key="sk-zzzz",
>>>     ),
>>> )
>>> 
>>> # 3. Create agent memory strategy configuration
>>> agent_config = AgentMemoryConfig(
>>>     mem_variables=[
>>>         Param(
>>>             name="favorite_color",
>>>             description="User's favorite color",
>>>             type="string",
>>>             required=False,
>>>         ),
>>>         Param(
>>>             name="age",
>>>             description="User's age",
>>>             type="number",
>>>             required=False,
>>>         ),
>>>     ],
>>>     enable_long_term_mem=True,
>>>     enable_user_profile=True,
>>>     enable_semantic_memory=True,
>>>     enable_episodic_memory=True,
>>>     enable_summary_memory=True,
>>> )
>>>
```
