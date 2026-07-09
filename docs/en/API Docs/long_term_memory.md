# jiuwen_memory.memory_core.long_term_memory

`jiuwen_memory.memory_core.long_term_memory` is the unified **long-term memory management engine** in JiuwenMemory, responsible for:

- Managing persistence and retrieval of user conversation messages;
- Managing user variable memories (e.g., preferences, personal information as structured data);
- Managing user profiles (long-term memory extracted from conversations via LLM);
- Supporting multi-tenant isolation based on `scope_id`;
- Supporting vector search, paginated queries, conditional deletion, and more.


## class jiuwen_memory.memory_core.long_term_memory.LongTermMemory

```
class jiuwen_memory.memory_core.long_term_memory.LongTermMemory(metaclass=Singleton)
```

`LongTermMemory` is the unified **long-term memory management engine** in JiuwenMemory, using the singleton pattern.

> **Note**: `LongTermMemory` adopts a **parameterless constructor + step-by-step initialization** approach:
> 1. Call `await register_store(...)` to register underlying storage;
> 2. Call `set_config(MemoryEngineConfig(...))` to set the global configuration, encryption key (optional), and vector index implementation (optional);
> 3. Optionally call `set_scope_config(scope_id, MemoryScopeConfig(...))` to configure independent model/vector parameters for different business scenarios.

```
LongTermMemory()
```

Initialize a `LongTermMemory` instance (singleton pattern; multiple calls return the same instance).

**Internal state initialization**:

- Configuration: `_sys_mem_config: MemoryEngineConfig | None = None`, `_scope_config: dict[str, MemoryScopeConfig] = {}`;
- Storage: `kv_store / vector_store / db_store / message_store` are all `None`, must be registered via `register_store`;
- Memory index: `memory_index: BaseMemoryIndex | None = None`, can be registered via `register_plugin` with a custom index implementation, or auto-registered as `SimpleMemoryIndex` during `register_store`;
- Managers: `scope_user_mapping_manager / message_manager / fragment_memory_manager / variable_manager / write_manager / search_manager / generator` are all `None`, initialized during `set_config`;
- LLM: `_base_llm: Model | None = None` (set during `set_config`);
- Embedding model cache: `_scope_embedding: dict[str, Embedding] = {}`.


### async register_store

```
async def register_store(
    self,
    kv_store: BaseKVStore,
    vector_store: BaseVectorStore | None = None,
    db_store: BaseDbStore | None = None,
    embedding_model: Embedding | None = None,
    message_store: BaseMessageStore | None = None,
    *,
    index_backend: str = "simple",
    file_root_dir: str | None = None,
) -> None
```

Register underlying storage instances. Must be called before `set_config`.

**Parameters**:

* **kv_store**(BaseKVStore): **Required**, key-value store instance for fast access to structured data (e.g., scope configuration, user variables). If `None`, an exception is raised (`MEMORY_REGISTER_STORE_EXECUTION_ERROR`).
* **vector_store**(BaseVectorStore | None, optional): Vector store instance for semantic similarity search. If `None`, semantic search is unavailable. Default: `None`.
* **db_store**(BaseDbStore | None, optional): Relational database store instance for persisting messages, scope-user mappings, etc. If `None`, message persistence is unavailable. Default: `None`.
* **embedding_model**(Embedding | None, optional): Global embedding model instance for initializing vector index embedding capabilities during registration. If `None`, independent embedding models can be configured per scope later via `set_scope_config`. Default: `None`.
* **message_store**(BaseMessageStore | None, optional): Custom message store instance; if `None` and `db_store` is provided, a default `SqlMessageStore` will be created automatically. Default: `None`.
* **index_backend**(str, keyword-only, optional): Memory index backend type, determines which `BaseMemoryIndex` implementation `register_store` auto-registers. Supported: `"simple"` (default, requires `vector_store`+`kv_store`, registers `SimpleMemoryIndex`) / `"file"` (registers `FileMemoryIndex`, long-term memories persisted to markdown + SQLite). Default: `"simple"`.
* **file_root_dir**(str | None, keyword-only, optional): Data root directory for `FileMemoryIndex` when `index_backend="file"`; memory `.md` files and `memory.db` are stored here. **Required when `index_backend="file"`** — raises `MEMORY_REGISTER_STORE_EXECUTION_ERROR` if missing. Ignored when `index_backend="simple"`. Default: `None`.

**Behavior**:

- `index_backend="simple"` (default): when both `vector_store` and `kv_store` are provided, `register_store` automatically calls `register_plugin` to register the default `SimpleMemoryIndex` as `memory_index`. To use a custom `BaseMemoryIndex` implementation, call `register_plugin` manually after `register_store` to override.
- `index_backend="file"`: auto-registers `FileMemoryIndex` as `memory_index`; `vector_store` is not used as the `memory_index` backend (only for middle-term memory / dreaming, may be left unconfigured). Registration **automatically starts the watchdog file watcher** (`start_watcher`) for real-time incremental sync on external `.md` edits; degrades to lazy sync-on-search if watchdog is not installed.
- When `db_store` is provided, an internal `SqlMessageStore` is automatically created (if not provided via the `message_store` parameter).
- After registering storage, `set_config(MemoryEngineConfig())` is automatically called for default initialization, and data migrations are run.

**Exceptions**:

* **build_error**: Raised when `kv_store` is `None` or storage type is mismatched.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.foundation.store.kv.db_based_kv_store import DbBasedKVStore
>>> from jiuwen_memory.foundation.store import create_vector_store
>>> from jiuwen_memory.foundation.store.db.default_db_store import DefaultDbStore
>>> from sqlalchemy.ext.asyncio import create_async_engine
>>>
>>> # Create LongTermMemory instance
>>> engine = LongTermMemory()
>>>
>>> # ---------- KV Store ----------
>>> kv_store = DbBasedKVStore(engine)
>>>
>>> # ---------- Vector Store ----------
>>> vector_store = create_vector_store("chroma", persist_directory="./resources/chroma")
>>>
>>> # ---------- DB Store ----------
>>> db_store = DefaultDbStore(create_async_engine(
>>>     f"mysql+aiomysql://{db_user}:{db_passport}@{db_host}:{db_port}/{agent_db_name}?charset=utf8mb4",
>>>     pool_size=20,
>>>     max_overflow=20
>>> ))
>>>
>>> # ---------- Register storage ----------
>>> await engine.register_store(
>>>     kv_store=kv_store,
>>>     vector_store=vector_store,
>>>     db_store=db_store
>>> )
```

**file backend example** (`index_backend="file"`, long-term memories persisted to markdown + SQLite):

```python
>>> # KV / DB store as above; vector_store may be omitted (file backend is not the memory_index)
>>> await engine.register_store(
>>>     kv_store=kv_store,
>>>     db_store=db_store,
>>>     embedding_model=embedding_model,
>>>     index_backend="file",
>>>     file_root_dir="./file_memory_data",
>>> )
```


### async register_plugin

```
async def register_plugin(
    self,
    name: str,
    cls: type,
    params: dict[str, Any],
) -> None
```

Register a custom `BaseMemoryIndex` plugin instance to replace or extend the default vector index implementation.

**Parameters**:

* **name**(str): Plugin name describing the plugin type (e.g., `'vector'`, `'inverted'`, `'hybrid'`).
* **cls**(type): Plugin class, must inherit from `BaseMemoryIndex`.
* **params**(dict[str, Any]): Initialization parameters passed to the plugin class constructor.

**Behavior**:

- This method instantiates the plugin via `cls(**params)`;
- The **first registered** plugin becomes the default `memory_index` (i.e., `self.memory_index`); subsequent registrations do not override the default;
- If `register_store` has already auto-registered `SimpleMemoryIndex`, manually calling `register_plugin` afterward will not override the existing default index.

**Prerequisites**:

- No strict prerequisites, but it is recommended to call after `register_store` and before `set_config`.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
>>> from jiuwen_memory.foundation.store.base_vector_store import BaseVectorStore
>>>
>>> # Use default SimpleMemoryIndex
>>> memory = LongTermMemory()
>>> await memory.register_plugin(
>>>     name="semantic_index",
>>>     cls=SimpleMemoryIndex,
>>>     params={
>>>         "kv_store": my_kv_store,
>>>         "vector_store": my_vector_store,
>>>         "embedding_model": my_embedding_model,
>>>     }
>>> )
```

> **Note**: Custom `BaseMemoryIndex` subclasses must implement the `set_storage_codec(codec)` abstract method to receive the `AesStorageCodec` instance. When `crypto_key` is non-empty during `set_config`, the codec is automatically injected; the subclass calls `codec.encode()` on the `text` field before writing and `codec.decode()` after reading to achieve transparent encryption/decryption.


### set_config

```
def set_config(self, config: MemoryEngineConfig) -> None
```

Set the global memory engine configuration and initialize internal managers.

**Parameters**:

* **config**(MemoryEngineConfig): Global engine configuration, including:
  * `default_model_cfg: ModelRequestConfig`: Default LLM request parameters for memory generation;
  * `default_model_client_cfg: ModelClientConfig`: Default LLM client configuration;
  * `forbidden_variables: str`: Variables forbidden from being memorized (comma-separated variable names). Default: `""` (no variables forbidden).
  * `input_msg_max_len: int`: Maximum input message length (default 8192);
  * `crypto_key: bytes`: AES encryption key (must be exactly 32 bytes; empty means no encryption).
  * `codec: str`: Registered name of a third-party codec (e.g. `"sm4"`, `"hsm"`); empty uses the built-in `AesStorageCodec` built from `crypto_key`. See "Custom codec extension" below.

**Prerequisites**:

- `register_store` must have been called to register `kv_store` and `db_store`, otherwise an exception is raised (`MEMORY_SET_CONFIG_EXECUTION_ERROR`).
- `memory_index` must have been registered (via `register_plugin` or auto-registered during `register_store`), otherwise an exception is raised.

**Behavior**:

- Managers (`FragmentMemoryManager`, `SummaryManager`, `WriteManager`) uniformly use `memory_index` (`BaseMemoryIndex`) as the backend.
- Codec resolution logic:
  - **Built-in AES (default)**: when `config.codec` is empty, an `AesStorageCodec` is built from `config.crypto_key`. A non-empty `crypto_key` enables transparent AES-256-GCM encryption/decryption of the `text` field at the storage layer.
  - **Third-party codec extension**: when `config.codec` names a registered codec, the engine looks up a **pre-built** `StorageCodec` instance by name from the registry and injects it, ignoring `crypto_key`; if not found it falls back to built-in AES with a warning.
  - The resolved codec is injected into `memory_index` (via `set_storage_codec`), `message_store` (via `set_codec`), and `VariableManager`, uniformly covering string payloads such as memory content, message content, and variable values.

#### Custom codec extension

Only `AesStorageCodec` (AES-256-GCM) is built in. To integrate non-AES schemes such as national-crypto SM4 or HSM hardware security modules, use the **look-up-by-name** registry mechanism — no engine code changes required:

```python
>>> from jiuwen_memory.foundation.codec import (
...     StorageCodec,
...     register_storage_codec,
... )
>>> from jiuwen_memory.memory_core.config import MemoryEngineConfig
>>>
>>> # 1. Custom codec: implement encode/decode (duck typing; no inheritance required)
>>> class SM4StorageCodec:
...     def __init__(self, key: bytes):       # any signature — single-param SM4
...         self._key = key
...     def encode(self, text: str) -> str: ...
...     def decode(self, data: str) -> str: ...
...
>>> # Multi-param codecs (e.g. HSM: cert/slot/pin) are also supported, because
>>> # the engine only looks up instances and never calls a constructor:
>>> # register_storage_codec("hsm", HSMCodec(cert=..., slot=..., pin=...))
>>>
>>> # 2. Register the pre-built instance (registration must happen before set_config)
>>> register_storage_codec("sm4", SM4StorageCodec(key=sm4_key))
>>>
>>> # 3. Reference the registered name in config; the engine resolves and injects it
>>> config = MemoryEngineConfig(crypto_key=b"", codec="sm4")
>>> memory.set_config(config)
```

> **Constraints**: a custom codec must satisfy `decode(encode(x)) == x` and pass `None`/empty strings through unchanged (matching `AesStorageCodec`'s empty-key pass-through). The protocol is defined in `jiuwen_memory.foundation.codec.StorageCodec`. The registry is a process-level singleton (`get_default_registry()`) and is **not thread-safe** — register at import/startup time, never concurrently on the hot path.

**Exceptions**:

* **build_error**: Raised when `register_store` has not been called or configuration is invalid.

**Internal managers initialized**:

This method initializes the following internal managers:

* `scope_user_mapping_manager`: Manages scope-to-user mapping relationships;
* `message_manager`: Handles message storage and retrieval. If a custom message store was provided via the `message_store` parameter of `register_store`, it uses the registered store; otherwise, it creates a default `SqlMessageStore` using the registered `db_store`;
* `fragment_memory_manager`: Manages user profiles, episodic memory, and semantic memory;
* `variable_manager`: Manages user variable storage and retrieval;
* `summary_manager`: Manages user summary memories;
* `write_manager`: Coordinates write operations for all memory types;
* `search_manager`: Handles search queries for all memory types;
* `generator`: Generates memory content from messages using LLM;
* `_base_llm`: Base LLM instance (initialized only when both `default_model_cfg` and `default_model_client_cfg` are provided).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import MemoryEngineConfig
>>> from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> 
>>> # Create configuration
>>> config = MemoryEngineConfig(
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
>>>     crypto_key=b"your-32-byte-aes-key-here!!",
>>> )
>>> 
>>> # Set configuration
>>> memory = LongTermMemory()
>>> memory.set_config(config)
```


### async migrate_between_indices

```
async def migrate_between_indices(
    source_index: BaseMemoryIndex,
    target_index: BaseMemoryIndex,
) -> None
```

Copy data from one `BaseMemoryIndex` to another. Suitable for data migration between different index implementations (e.g., from `SimpleMemoryIndex` to `VectorMemoryIndex`). Source data is preserved after migration.

**Parameters**:

* **source_index**(BaseMemoryIndex): Source `BaseMemoryIndex` instance to read migration data from.
* **target_index**(BaseMemoryIndex): Target `BaseMemoryIndex` instance to write data into.

**Behavior**:

- This method iterates over all `(user_id, scope_id)` combinations in `source_index`, reads documents in batches (100 per batch), and writes them to `target_index`;
- Source data is preserved after migration;
- Migration is idempotent — if a document with the same ID already exists in the target index, it will be overwritten (upsert semantics).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
>>>
>>> # Assume an existing SimpleMemoryIndex instance
>>> old_index = SimpleMemoryIndex(kv_store=kv_store, vector_store=vector_store, embedding_model=embed)
>>> new_index = VectorMemoryIndex(...)
>>>
>>> # Migrate data from old index to new index
>>> await LongTermMemory.migrate_between_indices(source_index=old_index, target_index=new_index)
```


### async set_scope_config

```
async def set_scope_config(
    self,
    scope_id: str,
    memory_scope_config: MemoryScopeConfig,
) -> bool
```

Set scope-level memory configuration for the specified `scope_id` and persist it to `kv_store`.

**Parameters**:

* **scope_id**(str): Scope identifier; cannot contain `/`, length cannot exceed 128 characters; if the format is invalid, an exception is raised (`MEMORY_SET_CONFIG_EXECUTION_ERROR`).
* **memory_scope_config**(MemoryScopeConfig): Scope configuration, including:
  * `model_cfg: ModelRequestConfig | None`: LLM request configuration for this scope;
  * `model_client_cfg: ModelClientConfig | None`: LLM client configuration for this scope;
  * `embedding_cfg: EmbeddingConfig | None`: Embedding model configuration for this scope.

**Returns**:

* **bool**: `True` if set successfully.

**Exceptions**:

* **build_error**: Raised when `scope_id` format is invalid.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import MemoryScopeConfig
>>> from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> from jiuwen_memory.retrieval.common.config import EmbeddingConfig
>>> 
>>> # Create scope configuration
>>> scope_config = MemoryScopeConfig(
>>>     model_cfg=ModelRequestConfig(
>>>         model="gpt-4",
>>>         temperature=0.1,
>>>     ),
>>>     model_client_cfg=ModelClientConfig(
>>>         client_id="scope_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-yyyy",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     embedding_cfg=EmbeddingConfig(
>>>         model_name="text-embedding-3-large",
>>>         base_url="https://api.openai.com/v1",
>>>         api_key="sk-zzzz",
>>>     ),
>>> )
>>> 
>>> # Set scope configuration
>>> memory = LongTermMemory()
>>> success = await memory.set_scope_config("my_scope", scope_config)
>>> print(f"Result: {success}")
```


### async get_scope_config

```
async def get_scope_config(self, scope_id: str) -> MemoryScopeConfig | None
```

Read the scope configuration for the specified `scope_id` from `kv_store`, with API keys decrypted.

**Parameters**:

* **scope_id**(str): Scope identifier.

**Returns**:

* **MemoryScopeConfig | None**: If the configuration exists, returns the decrypted configuration object; if not found or `scope_id` format is invalid, returns `None`.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Get scope configuration
>>> memory = LongTermMemory()
>>> scope_config = await memory.get_scope_config("my_scope")
>>> 
>>> if scope_config:
>>>     print(f"Model config: {scope_config.model_cfg}")
>>>     print(f"Client config: {scope_config.model_client_cfg}")
>>> else:
>>>     print("Scope configuration not found")
```


### async delete_scope_config

```
async def delete_scope_config(self, scope_id: str) -> bool
```

Delete the scope configuration for the specified `scope_id` (removed from `kv_store` and in-memory cache).

**Parameters**:

* **scope_id**(str): Scope identifier.

**Returns**:

* **bool**: `True` if deleted successfully.

**Exceptions**:

* **build_error**: Raised when `scope_id` format is invalid or deletion fails (`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Delete scope configuration
>>> memory = LongTermMemory()
>>> success = await memory.delete_scope_config("my_scope")
>>> print(f"Deletion result: {success}")
```


### async delete_mem_by_scope

```
async def delete_mem_by_scope(self, scope_id: str) -> bool
```

Delete all memory data under the specified `scope_id` (including messages, user profiles, variables, etc.).

**Parameters**:

* **scope_id**(str): Scope identifier.

**Returns**:

* **bool**: `True` if deleted successfully.

**Exceptions**:

* **build_error**: Raised when `scope_id` format is invalid (`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Delete all memories under a scope
>>> memory = LongTermMemory()
>>> success = await memory.delete_mem_by_scope("my_scope")
>>> print(f"Deletion result: {success}")
```


### async add_messages

```
async def add_messages(
    self,
    messages: list[BaseMessage],
    agent_config: AgentMemoryConfig,
    *,
    user_id: str = "__default__",
    scope_id: str = "__default__",
    session_id: str = "__default__",
    timestamp: datetime | None = None,
    gen_mem: bool = True,
    gen_mem_with_history_msg_num: int = 2,
) -> AddMemResult
```

Add conversation messages to the memory engine and generate memories (user profiles, variables, etc.) based on `agent_config`. Also supports **directive memory** functionality: when a user includes explicit memory directives in the conversation (e.g., "change ... to ...", "delete ..."), the engine automatically recognizes and executes the corresponding add/update/delete operations.

**Parameters**:

* **messages**(list[BaseMessage]): List of messages to add (typically including user messages and AI replies).
* **agent_config**(AgentMemoryConfig): Agent memory strategy configuration, including:
  * `mem_variables: list[Param]`: Variable memory configurations to extract (variable name, description, type, etc.);
  * `enable_long_term_mem: bool`: Whether to enable long-term memory generation (default `True`).
  * `enable_user_profile: bool`: Whether to enable user profile generation and usage (default `True`).
  * `enable_semantic_memory: bool`: Whether to enable semantic memory generation and usage (default `True`).
  * `enable_episodic_memory: bool`: Whether to enable episodic memory generation and usage (default `True`).
  * `enable_summary_memory: bool`: Whether to enable user summary memory generation (default `True`).
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, an exception is raised. Default: `"__default__"`.
* **session_id**(str, optional): Session identifier. Default: `"__default__"`.
* **timestamp**(datetime | None, optional): Message timestamp; if `None`, current UTC time is used. Default: `None`.
* **gen_mem**(bool, optional): Whether to generate memories; when `False`, only messages are saved without triggering memory extraction. Default: `True`.
* **gen_mem_with_history_msg_num**(int, optional): Number of historical messages to reference when generating memories. Default: 2.

**Returns**:

* **AddMemResult**: The result of this memory extraction, containing the following fields:
  * `variables: list[VariableUnit]`: Extracted variable memory list;
  * `user_profile: list[FragmentMemoryUnit]`: Extracted user profile memory list;
  * `semantic_memory: list[FragmentMemoryUnit]`: Extracted semantic memory list;
  * `episodic_memory: list[FragmentMemoryUnit]`: Extracted episodic memory list;
  * `summary: list[SummaryUnit]`: Extracted summary memory list.

Returns an empty `AddMemResult()` (all fields as empty lists) when `gen_mem=False`, `scope_id` format is invalid, LLM is not initialized, or messages contain no user messages.

**Exceptions**:

* **build_error**: Raised when `scope_id` format is invalid, LLM is not initialized, or memory write fails (`MEMORY_ADD_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import AgentMemoryConfig
>>> from jiuwen_memory.common.schema.param import Param
>>> from jiuwen_memory.foundation.llm.schema.message import UserMessage, AssistantMessage
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
>>> 
>>> # Prepare messages
>>> messages = [
>>>     UserMessage(content="I like blue, I'm 25 years old"),
>>>     AssistantMessage(content="Got it, I'll remember you like blue and you're 25.")
>>> ]
>>> 
>>> # Add messages
>>> memory = LongTermMemory()
>>> await memory.add_messages(
>>>     messages=messages,
>>>     agent_config=agent_config,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     session_id="session456"
>>> )
```
**Directive memory example**:

```python
>>> # User modifies existing memory via explicit directive
>>> update_messages = [
>>>     UserMessage(content="Change my age to 30"),
>>>     AssistantMessage(content="OK, I've updated your age information.")
>>> ]
>>> result = await memory.add_messages(
>>>     messages=update_messages,
>>>     agent_config=agent_config,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>> )
>>> 
>>> # User deletes existing memory via explicit directive
>>> delete_messages = [
>>>     UserMessage(content="Delete my age information"),
>>>     AssistantMessage(content="OK, I've deleted your age information.")
>>> ]
>>> result = await memory.add_messages(
>>>     messages=delete_messages,
>>>     agent_config=agent_config,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>> )
```


## class jiuwen_memory.memory_core.long_term_memory.AddMemResult

```
class jiuwen_memory.memory_core.long_term_memory.AddMemResult(BaseModel)
```

Return value model for the `add_messages` method, encapsulating all results of this memory extraction.

**Fields**:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `variables` | `list[VariableUnit]` | `[]` | Extracted variable memory list |
| `user_profile` | `list[FragmentMemoryUnit]` | `[]` | Extracted user profile memory list |
| `semantic_memory` | `list[FragmentMemoryUnit]` | `[]` | Extracted semantic memory list |
| `episodic_memory` | `list[FragmentMemoryUnit]` | `[]` | Extracted episodic memory list |
| `summary` | `list[SummaryUnit]` | `[]` | Extracted summary memory list |

**Notes**:

- Each `FragmentMemoryUnit` contains an `operation_type` field (`ADD` / `UPDATE` / `DELETE`) to distinguish the operation type.
- UPDATE and DELETE operations for directive memory appear in the results as `FragmentMemoryUnit` with the corresponding `operation_type`.
- When `add_messages` does not perform memory extraction for any reason (`gen_mem=False`, invalid `scope_id`, LLM not initialized, etc.), an empty `AddMemResult()` is returned.


## class jiuwen_memory.memory_core.long_term_memory.MemInfo

```
class jiuwen_memory.memory_core.long_term_memory.MemInfo(BaseModel)
```

Memory information model describing the basic information of a memory entry.

**Fields**:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mem_id` | `str` | `""` | Memory unique identifier |
| `content` | `str` | `""` | Memory content |
| `type` | `MemoryType` | `MemoryType.USER_PROFILE` | Memory type |
| `timestamp` | `datetime | None` | `None` | Memory timestamp |


## class jiuwen_memory.memory_core.long_term_memory.MemResult

```
class jiuwen_memory.memory_core.long_term_memory.MemResult(BaseModel)
```

Memory search result model containing memory information and a similarity score.

**Fields**:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mem_info` | `MemInfo | None` | `None` | Memory information |
| `score` | `float` | `0.0` | Similarity score |


### async get_recent_messages

```
async def get_recent_messages(
    self,
    user_id: str = "__default__",
    scope_id: str = "__default__",
    session_id: str = "__default__",
    num: int = 10,
) -> list[BaseMessage]
```

Get the most recent N messages for the specified user/scope/session, returned in write order.

**Parameters**:

* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, an exception is raised. Default: `"__default__"`.
* **session_id**(str, optional): Session identifier. Default: `"__default__"`.
* **num**(int, optional): Number of messages to retrieve. Default: 10.

**Returns**:

* **list[BaseMessage]**: Message list in write order; if `scope_id` format is invalid or `message_manager` is not initialized, an exception is raised.

**Exceptions**:

* **build_error**: Raised when `scope_id` format is invalid (`MEMORY_GET_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Get recent messages
>>> memory = LongTermMemory()
>>> messages = await memory.get_recent_messages(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     session_id="session456",
>>>     num=5
>>> )
>>> 
>>> for msg in messages:
>>>     print(f"{msg.role}: {msg.content}")
```


### async get_message_by_id

```
async def get_message_by_id(self, msg_id: str) -> Tuple[BaseMessage, datetime] | None
```

Get a single message and its creation timestamp by message ID.

**Parameters**:

* **msg_id**(str): Message unique identifier.

**Returns**:

* **Tuple[BaseMessage, datetime] | None**: If the message exists, returns `(message object, creation time)`; if `message_manager` is not initialized or the message does not exist, returns `None`.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Get message by ID
>>> memory = LongTermMemory()
>>> result = await memory.get_message_by_id("msg_12345")
>>> 
>>> if result:
>>>     message, timestamp = result
>>>     print(f"Content: {message.content}")
>>>     print(f"Created at: {timestamp}")
>>> else:
>>>     print("Message not found")
```


### async delete_mem_by_id

```
async def delete_mem_by_id(
    self,
    mem_id: str,
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> None
```

Delete a memory entry by its ID (user profile or variable).

**Parameters**:

* **mem_id**(str): Memory unique identifier.
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns directly. Default: `"__default__"`.

**Exceptions**:

* **build_error**: Raised when `write_manager` is not initialized (`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Delete a specific memory
>>> memory = LongTermMemory()
>>> await memory.delete_mem_by_id(
>>>     mem_id="mem_12345",
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
```


### async delete_mem_by_user_id

```
async def delete_mem_by_user_id(
    self,
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> None
```

Delete all types of memories for a specified user under a scope (user profiles, variables, etc.).

**Parameters**:

* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns directly. Default: `"__default__"`.

**Exceptions**:

* **build_error**: Raised when `write_manager` is not initialized.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Delete all memories for a user
>>> memory = LongTermMemory()
>>> await memory.delete_mem_by_user_id(
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
```


### async update_mem_by_id

```
async def update_mem_by_id(
    self,
    mem_id: str,
    memory: str,
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> None
```

Update the content of a memory entry by its ID.

**Parameters**:

* **mem_id**(str): Memory unique identifier.
* **memory**(str): New memory content.
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns directly. Default: `"__default__"`.

**Exceptions**:

* **build_error**: Raised when `write_manager` is not initialized (`MEMORY_UPDATE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Update memory content
>>> memory_engine = LongTermMemory()
>>> await memory_engine.update_mem_by_id(
>>>     mem_id="mem_12345",
>>>     memory="Updated memory content",
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
```


### async get_variables

```
async def get_variables(
    self,
    names: list[str] | str | None = None,
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> dict[str, str]
```

Get user variables (one or more).

**Parameters**:

* **names**(list[str] | str | None, optional):
  * If `None`: Returns all variables for the user under the scope;
  * If `str`: Returns a single variable (`{name: value}`);
  * If `list[str]`: Returns multiple variables (`{name1: value1, name2: value2, ...}`).
  Default: `None`.
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns an empty dict. Default: `"__default__"`.

**Returns**:

* **dict[str, str]**: Mapping of variable names to variable values; if `scope_id` format is invalid or `search_manager` is not initialized, returns an empty dict or raises an exception.

**Exceptions**:

* **build_error**: Raised when `search_manager` is not initialized or `names` type is unexpected (`MEMORY_GET_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Get all variables
>>> memory = LongTermMemory()
>>> all_vars = await memory.get_variables(
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"All variables: {all_vars}")
>>> 
>>> # Get a single variable
>>> favorite_color = await memory.get_variables(
>>>     names="favorite_color",
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"Favorite color: {favorite_color}")
>>> 
>>> # Get multiple variables
>>> some_vars = await memory.get_variables(
>>>     names=["favorite_color", "age"],
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"Some variables: {some_vars}")
```


### async update_variables

```
async def update_variables(
    self,
    variables: dict[str, str],
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> None
```

Update user variables. **Upsert semantics**: inserts the variable if its name does not exist, overwrites the value if it does (a name absent from the kv store is treated as a first-time write rather than a silent failure).

**Parameters**:

* **variables**(dict[str, str]): Mapping of variable names to their values; keys are variable names, values are the new values.
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; raises on invalid format. Default: `"__default__"`.

**Returns**:

* **None**: No return value. Each variable is written in turn under a distributed lock.

**Exceptions**:

* **build_error**: Raised when the `scope_id` format is invalid or `variable_manager` is not initialized (`MEMORY_UPDATE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>>
>>> memory = LongTermMemory()
>>> # First-time write (name absent -> insert) and overwrite of an existing
>>> # variable share the same semantics
>>> await memory.update_variables(
>>>     variables={"favorite_color": "blue", "city": "Shenzhen"},
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
```


### async delete_variables

```
async def delete_variables(
    self,
    names: list[str],
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> bool
```

Delete user variables. Deletes each specified variable name in turn; deleting a name that does not exist is not an error.

**Parameters**:

* **names**(list[str]): List of variable names to delete.
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; raises on invalid format. Default: `"__default__"`.

**Returns**:

* **bool**: Returns `True` on success.

**Exceptions**:

* **build_error**: Raised when the `scope_id` format is invalid or `variable_manager` is not initialized (`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>>
>>> memory = LongTermMemory()
>>> await memory.delete_variables(
>>>     names=["favorite_color", "city"],
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
```


### async search_user_mem

```
async def search_user_mem(
    self,
    query: str,
    num: int,
    user_id: str = "__default__",
    scope_id: str = "__default__",
    threshold: float = 0.3,
) -> list[MemResult]
```

Search user memories (user profiles, variables, etc.) based on semantic similarity, returning the top-N most relevant memories.

**Parameters**:

* **query**(str): Query text.
* **num**(int): Number of memories to return (top-k).
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns an empty list. Default: `"__default__"`.
* **threshold**(float, optional): Similarity threshold; memories below this threshold are filtered out. Default: 0.3.

**Returns**:

* **list[MemResult]**: Memory result list, where each `MemResult` contains:
  * `mem_info: MemInfo` (`mem_id / content / type / timestamp`);
  * `score: float` (similarity score).

**Exceptions**:

* **build_error**: Raised when `search_manager` is not initialized.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Search user memories
>>> memory = LongTermMemory()
>>> results = await memory.search_user_mem(
>>>     query="user's hobbies and interests",
>>>     num=5,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     threshold=0.4
>>> )
>>> 
>>> for result in results:
>>>     print(f"Content: {result.mem_info.content}")
>>>     print(f"Similarity: {result.score}")
>>>     print("---")
```


### async user_mem_total_num

```
async def user_mem_total_num(
    self,
    user_id: str = "__default__",
    scope_id: str = "__default__",
) -> int
```

Return the total number of memories for a specified user under a scope.

**Parameters**:

* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns 0. Default: `"__default__"`.

**Returns**:

* **int**: Total memory count; if `scope_id` format is invalid, returns 0.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Get total memory count
>>> memory = LongTermMemory()
>>> total = await memory.user_mem_total_num(
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"Total memories: {total}")
```


### async search_user_history_summary

```
async def search_user_history_summary(
    self,
    query: str,
    num: int,
    user_id: str = "__default__",
    scope_id: str = "__default__",
    threshold: float = 0.3,
) -> list[MemResult]
```

Search user summary memories based on semantic similarity, returning the top-N most relevant summary memories.

**Parameters**:

* **query**(str): Search query string.
* **num**(int): Number of results to return (top-k).
* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns an empty list. Default: `"__default__"`.
* **threshold**(float, optional): Minimum similarity threshold for results; memories below this threshold are filtered out. Default: 0.3.

**Returns**:

* **list[MemResult]**: Memory result list, where each `MemResult` contains:
  * `mem_info: MemInfo` (`mem_id / content / type / timestamp`);
  * `score: float` (similarity score).

**Exceptions**:

* **build_error**: Raised when `search_manager` is not initialized.

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Search user summary memories
>>> memory = LongTermMemory()
>>> results = await memory.search_user_history_summary(
>>>     query="recent conversations about work",
>>>     num=5,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     threshold=0.4
>>> )
>>> 
>>> for result in results:
>>>     print(f"Content: {result.mem_info.content}")
>>>     print(f"Similarity: {result.score}")
>>>     print("---")
```


### async get_user_mem_by_page

```
async def get_user_mem_by_page(
    self,
    user_id: str = "__default__",
    scope_id: str = "__default__",
    page_num: int = 1,
    page_size: int = 10,
) -> dict[str, Any]
```

Paginate memories for a specified user under a scope.

**Parameters**:

* **user_id**(str, optional): User identifier. Default: `"__default__"`.
* **scope_id**(str, optional): Scope identifier; if format is invalid, returns an empty dict. Default: `"__default__"`.
* **page_num**(int, optional): Page number, starting from 1. Default: 1.
* **page_size**(int, optional): Page size. Default: 10.

**Returns**:

* **dict[str, Any]**: Contains the following fields:
  * `total: int`: Total memory count;
  * `page_num: int`: Current page number;
  * `page_size: int`: Page size;
  * `total_pages: int`: Total number of pages;
  * `data: list[MemInfo]`: Memory list for the current page.

**Exceptions**:

* **build_error**: Raised when `search_manager` is not initialized (`MEMORY_GET_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # Paginate user memories
>>> memory = LongTermMemory()
>>> result = await memory.get_user_mem_by_page(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     page_num=2,
>>>     page_size=5
>>> )
>>> 
>>> print(f"Total memories: {result['total']}")
>>> print(f"Current page: {result['page_num']}/{result['total_pages']}")
>>> 
>>> for mem_info in result['data']:
>>>     print(f"ID: {mem_info.mem_id}, Content: {mem_info.content[:50]}...")
```


### async start_dreaming

```
async def start_dreaming(
    self,
    scope_id: str,
    user_id: str,
    *,
    config: DreamingConfig | None = None,
    busy_checker: Callable[[], bool] | None = None,
) -> DreamingOrchestrator | None
```

Start the background **dreaming** process for a `(scope_id, user_id)` pair: a scheduler that periodically re-reads the user's stored sessions, distills durable knowledge via the LLM, and writes it back through the normal memory write path (same managers, user-level lock, and conflict detection as `add_messages`). Dreamed memories are ordinary user profile / semantic / episodic units — no new memory type is introduced.

**Parameters**:

* **scope_id**(str): Scope identifier; if the format is invalid, an exception is raised.
* **user_id**(str): User identifier.
* **config**(DreamingConfig | None, optional): Dreaming configuration. When `None`, a default `DreamingConfig()` is used (which has `enabled=False`, so nothing starts). Default: `None`.
* **busy_checker**(Callable[[], bool] | None, optional): Optional callback polled before each sweep; return `True` to defer the current sweep (e.g., while the agent is busy serving the user). Default: `None`.

**Returns**:

* **DreamingOrchestrator | None**: The background orchestrator instance, or `None` when `config.enabled` is `False`. **Idempotent**: a second call for the same `(scope_id, user_id)` returns the existing orchestrator instead of starting a new one.

**Prerequisites**:

- `register_store` must have registered `kv_store`, a message store, and a vector store + embedding model; a scope LLM must be available (via `set_scope_config` or the global default model in `set_config`). Otherwise an exception is raised.

**Behavior**:

- The orchestrator runs in the background, performing a sweep every `config.interval_seconds` (after an initial warm-up). Scanned sessions are checkpointed in `kv_store` (key `dreaming/checkpoint/{scope_id}/{user_id}`), so a restarted process does not re-process old sessions.
- Sessions are grouped by `session_id`; pass meaningful `session_id` values to `add_messages` so consolidation behaves as expected.

**Exceptions**:

* **build_error**: Raised when `scope_id` format is invalid, the required stores are not registered, or the LLM is not initialized (`MEMORY_ADD_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import DreamingConfig
>>> 
>>> memory = LongTermMemory()
>>> # ... register_store + set_scope_config beforehand ...
>>> 
>>> # Start background dreaming (fire-and-forget; sweeps run on the interval)
>>> orchestrator = await memory.start_dreaming(
>>>     scope_id="my_scope",
>>>     user_id="user123",
>>>     config=DreamingConfig(enabled=True, interval_seconds=3600, min_session_rounds=2),
>>> )
```


### async stop_dreaming

```
async def stop_dreaming(
    self,
    scope_id: str | None = None,
    user_id: str | None = None,
) -> None
```

Stop running dreaming orchestrators. With no arguments, stops all of them; otherwise stops only those matching the provided `scope_id` and/or `user_id`.

**Parameters**:

* **scope_id**(str | None, optional): If provided, only orchestrators with this scope are stopped. Default: `None`.
* **user_id**(str | None, optional): If provided, only orchestrators for this user are stopped. Default: `None`.

**Example**:

```python
>>> # Stop a specific (scope, user) orchestrator
>>> await memory.stop_dreaming(scope_id="my_scope", user_id="user123")
>>> 
>>> # Stop everything (e.g., on shutdown)
>>> await memory.stop_dreaming()
```


> **Note**: For all methods involving `user_id`, `scope_id`, and `session_id`, using the default value `"__default__"` means using the system default identifier; in production, it is recommended to pass meaningful business identifiers to support multi-tenant isolation and precise queries.

## Related Modules

`LongTermMemory` manages flat memory units such as user profiles, semantic memories, episodic memories, variables, and summaries. If you need to turn conversations, documents, or JSON strings into a knowledge graph of entities, relations, and source episodes, use the independent Graph Memory module. See [jiuwen_memory.memory_core.graph.graph_memory](graph_memory.md).
