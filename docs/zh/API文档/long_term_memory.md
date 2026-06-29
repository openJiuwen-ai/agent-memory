# jiuwen_memory.memory_core.long_term_memory

`jiuwen_memory.memory_core.long_term_memory` 是 JiuwenMemory 中统一的**长期记忆管理引擎**，负责：

- 管理用户对话消息的持久化与检索；
- 管理用户变量记忆（如偏好、个人信息等结构化数据）；
- 管理用户画像（长期记忆，通过 LLM 从对话中提取）；
- 支持基于 `scope_id` 的多租户隔离；
- 支持向量检索、分页查询、按条件删除等操作。


## class jiuwen_memory.memory_core.long_term_memory.LongTermMemory

```
class jiuwen_memory.memory_core.long_term_memory.LongTermMemory(metaclass=Singleton)
```

`LongTermMemory` 是 JiuwenMemory 中统一的**长期记忆管理引擎**，采用单例模式。

> **说明**：`LongTermMemory` 采用**无参构造 + 分步初始化**的方式：
> 1. 先调用 `await register_store(...)` 注册底层存储；
> 2. 再调用 `set_config(MemoryEngineConfig(...))` 设置全局配置，设置加密密钥（可选）和向量索引实现（可选）；
> 3. 可选地通过 `set_scope_config(scope_id, MemoryScopeConfig(...))` 为不同业务场景配置独立的模型/向量参数。

```
LongTermMemory()
```

初始化 `LongTermMemory` 实例（单例模式，多次调用返回同一实例）。

**内部状态初始化**：

- 配置相关：`_sys_mem_config: MemoryEngineConfig | None = None`、`_scope_config: dict[str, MemoryScopeConfig] = {}`；
- 存储相关：`kv_store / vector_store / db_store / message_store` 均为 `None`，需通过 `register_store` 注册；
- 记忆索引：`memory_index: BaseMemoryIndex | None = None`，可通过 `register_plugin` 注册自定义索引实现，或在 `register_store` 时自动注册 `SimpleMemoryIndex`；
- 管理器相关：`scope_user_mapping_manager / message_manager / fragment_memory_manager / variable_manager / write_manager / search_manager / generator` 均为 `None`，在 `set_config` 时初始化；
- LLM 相关：`_base_llm: Model | None = None`（在 `set_config` 时设置）；
- 嵌入模型缓存：`_scope_embedding: dict[str, Embedding] = {}`。


### async register_store

```
async def register_store(
    self,
    kv_store: BaseKVStore,
    vector_store: BaseVectorStore | None = None,
    db_store: BaseDbStore | None = None,
    embedding_model: Embedding | None = None,
    message_store: BaseMessageStore | None = None,
) -> None
```

注册底层存储实例，必须在调用 `set_config` 之前完成。

**参数**：

* **kv_store**(BaseKVStore)：**必填**，键值存储实例，用于快速访问结构化数据（如 scope 配置、用户变量等）。若为 `None`，会抛出异常（`MEMORY_REGISTER_STORE_EXECUTION_ERROR`）。
* **vector_store**(BaseVectorStore | None, 可选)：向量存储实例，用于语义相似度检索。若为 `None`，则语义检索功能不可用。默认值：`None`。
* **db_store**(BaseDbStore | None, 可选)：关系型数据库存储实例，用于持久化消息、scope-user 映射等。若为 `None`，则消息持久化功能不可用。默认值：`None`。
* **embedding_model**(Embedding | None, 可选)：全局嵌入模型实例，用于在注册时初始化向量索引的嵌入能力。若为 `None`，后续可通过 `set_scope_config` 为不同 scope 配置独立的嵌入模型。默认值：`None`。
* **message_store**(BaseMessageStore | None, 可选)：自定义消息存储实例；若为 `None` 且提供了 `db_store`，将自动创建默认的 `SqlMessageStore`。默认值：`None`。

**行为说明**：

- 当同时提供 `vector_store` 和 `kv_store` 时，`register_store` 会自动调用 `register_plugin` 注册默认的 `SimpleMemoryIndex` 作为 `memory_index`。若需要使用自定义的 `BaseMemoryIndex` 实现，可在 `register_store` 之后手动调用 `register_plugin` 进行覆盖。
- 当提供 `db_store` 时，会自动创建内部 `SqlMessageStore`（若未通过 `message_store` 参数提供）。
- 注册存储后会自动调用 `set_config(MemoryEngineConfig())` 进行默认初始化，并运行数据迁移。

**异常**：

* **build_error**：当 `kv_store` 为 `None` 或存储类型不匹配时抛出。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.foundation.store.kv.db_based_kv_store import DbBasedKVStore
>>> from jiuwen_memory.foundation.store import create_vector_store
>>> from jiuwen_memory.foundation.store.db.default_db_store import DefaultDbStore
>>> from sqlalchemy.ext.asyncio import create_async_engine
>>>
>>> # 创建 LongTermMemory 实例
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
>>> # ---------- 注册存储 ----------
>>> await engine.register_store(
>>>     kv_store=kv_store,
>>>     vector_store=vector_store,
>>>     db_store=db_store
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

注册自定义 `BaseMemoryIndex` 插件实例，用于替换或扩展默认的向量索引实现。

**参数**：

* **name**(str)：插件名称，描述插件类型（如 `'vector'`、`'inverted'`、`'hybrid'`）。
* **cls**(type)：插件类，必须继承自 `BaseMemoryIndex`。
* **params**(dict[str, Any])：传递给插件类构造函数的初始化参数。

**行为说明**：

- 该方法会将 `cls(**params)` 实例化为插件实例；
- **首次注册**的插件会成为默认的 `memory_index`（即 `self.memory_index`），后续注册的插件不会覆盖默认值；
- 若 `register_store` 已自动注册了 `SimpleMemoryIndex`，则后续手动调用 `register_plugin` 不会覆盖已有的默认索引。

**前置条件**：

- 无严格前置条件，但建议在 `register_store` 之后、`set_config` 之前调用。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
>>> from jiuwen_memory.foundation.store.base_vector_store import BaseVectorStore
>>>
>>> # 使用默认 SimpleMemoryIndex
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

> **说明**：自定义 `BaseMemoryIndex` 子类需实现 `set_storage_codec(codec)` 抽象方法，用于接收 `AesStorageCodec` 实例。`set_config` 时若 `crypto_key` 非空，会自动注入 codec；子类在写入前对 `text` 字段调用 `codec.encode()`，读取后调用 `codec.decode()` 即可实现透明加解密。


### set_config

```
def set_config(self, config: MemoryEngineConfig) -> None
```

设置全局记忆引擎配置，并初始化内部管理器。

**参数**：

* **config**(MemoryEngineConfig)：全局引擎配置，包含：
  * `default_model_cfg: ModelRequestConfig`：默认用于生成记忆的大模型请求参数；
  * `default_model_client_cfg: ModelClientConfig`：默认大模型客户端配置；
  * `forbidden_variables: str`：禁止记忆的变量（逗号分隔的变量名）。默认值：`""`（不禁止任何变量）。
  * `input_msg_max_len: int`：输入消息最大长度（默认 8192）；
  * `crypto_key: bytes`：AES 加密密钥（长度必须为 32 字节；为空则不加密）。

**前置条件**：

- 必须已调用 `register_store` 注册 `kv_store` 和 `db_store`，否则会抛出异常（`MEMORY_SET_CONFIG_EXECUTION_ERROR`）。
- 必须已注册 `memory_index`（通过 `register_plugin` 或 `register_store` 自动注册），否则会抛出异常。

**行为说明**：

- 管理器（`FragmentMemoryManager`、`SummaryManager`、`WriteManager`）统一使用 `memory_index`（`BaseMemoryIndex`）作为后端。
- 若 `crypto_key` 非空，会自动创建 `AesStorageCodec` 并调用 `memory_index.set_storage_codec()`，对记忆内容 `text` 字段进行存储层透明 AES-256-GCM 加解密。

**异常**：

* **build_error**：当未调用 `register_store` 或配置无效时抛出。

**初始化的内部管理器**：

此方法初始化以下内部管理器：

* `scope_user_mapping_manager`：管理作用域与用户的映射关系；
* `message_manager`：处理消息的存储和检索操作。若已通过 `register_store` 的 `message_store` 参数提供了自定义消息存储，则使用已注册的存储；否则，使用注册的 `db_store` 创建默认的 `SqlMessageStore`；
* `fragment_memory_manager`：管理用户画像、情景记忆和语义记忆；
* `variable_manager`：管理用户变量的存储和检索；
* `summary_manager`：管理用户摘要记忆；
* `write_manager`：协调所有记忆类型的写入操作；
* `search_manager`：处理所有记忆类型的搜索查询；
* `generator`：使用 LLM 从消息生成记忆内容；
* `_base_llm`：基础大语言模型实例（仅当提供了 `default_model_cfg` 和 `default_model_client_cfg` 时初始化）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import MemoryEngineConfig
>>> from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> 
>>> # 创建配置
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
>>> # 设置配置
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

将数据从一个 `BaseMemoryIndex` 复制到另一个 `BaseMemoryIndex`。适用于不同索引实现之间的数据迁移（如从 `SimpleMemoryIndex` 迁移到 `VectorMemoryIndex`）。源数据在迁移后保留。

**参数**：

* **source_index**(BaseMemoryIndex)：源 `BaseMemoryIndex` 实例，从中读取待迁移的数据。
* **target_index**(BaseMemoryIndex)：目标 `BaseMemoryIndex` 实例，数据将写入此索引。

**行为说明**：

- 该方法会遍历 `source_index` 中的所有 `(user_id, scope_id)` 组合，分批（每批 100 条）读取文档并写入 `target_index`；
- 源数据在迁移后保持不变；
- 迁移是幂等的——若目标索引中已存在相同 ID 的文档，会被覆盖（upsert 语义）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.foundation.store.index.simple_memory_index import SimpleMemoryIndex
>>>
>>> # 假设已有旧的 SimpleMemoryIndex 实例
>>> old_index = SimpleMemoryIndex(kv_store=kv_store, vector_store=vector_store, embedding_model=embed)
>>> new_index = VectorMemoryIndex(...)
>>>
>>> # 从旧索引迁移数据到新索引
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

为指定 `scope_id` 设置作用域级记忆配置，并持久化到 `kv_store`。

**参数**：

* **scope_id**(str)：作用域标识符，不能包含 `/`，长度不能超过 128 字符；若格式无效，会抛出异常（`MEMORY_SET_CONFIG_EXECUTION_ERROR`）。
* **memory_scope_config**(MemoryScopeConfig)：作用域配置，包含：
  * `model_cfg: ModelRequestConfig | None`：该 scope 下使用的大模型请求配置；
  * `model_client_cfg: ModelClientConfig | None`：该 scope 下使用的大模型客户端配置；
  * `embedding_cfg: EmbeddingConfig | None`：该 scope 下使用的嵌入模型配置。

**返回**：

* **bool**：设置成功返回 `True`。

**异常**：

* **build_error**：当 `scope_id` 格式无效时抛出。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import MemoryScopeConfig
>>> from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> from jiuwen_memory.retrieval.common.config import EmbeddingConfig
>>> 
>>> # 创建作用域配置
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
>>> # 设置作用域配置
>>> memory = LongTermMemory()
>>> success = await memory.set_scope_config("my_scope", scope_config)
>>> print(f"设置结果: {success}")
```


### async get_scope_config

```
async def get_scope_config(self, scope_id: str) -> MemoryScopeConfig | None
```

从 `kv_store` 中读取指定 `scope_id` 的作用域配置，并解密 API key。

**参数**：

* **scope_id**(str)：作用域标识符。

**返回**：

* **MemoryScopeConfig | None**：若配置存在，返回解密后的配置对象；若不存在或 `scope_id` 格式无效，返回 `None`。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 获取作用域配置
>>> memory = LongTermMemory()
>>> scope_config = await memory.get_scope_config("my_scope")
>>> 
>>> if scope_config:
>>>     print(f"模型配置: {scope_config.model_cfg}")
>>>     print(f"客户端配置: {scope_config.model_client_cfg}")
>>> else:
>>>     print("未找到作用域配置")
```


### async delete_scope_config

```
async def delete_scope_config(self, scope_id: str) -> bool
```

删除指定 `scope_id` 的作用域配置（从 `kv_store` 和内存缓存中移除）。

**参数**：

* **scope_id**(str)：作用域标识符。

**返回**：

* **bool**：删除成功返回 `True`。

**异常**：

* **build_error**：当 `scope_id` 格式无效或删除失败时抛出（`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 删除作用域配置
>>> memory = LongTermMemory()
>>> success = await memory.delete_scope_config("my_scope")
>>> print(f"删除结果: {success}")
```


### async delete_mem_by_scope

```
async def delete_mem_by_scope(self, scope_id: str) -> bool
```

删除指定 `scope_id` 下的所有记忆数据（包括消息、用户画像、变量等）。

**参数**：

* **scope_id**(str)：作用域标识符。

**返回**：

* **bool**：删除成功返回 `True`。

**异常**：

* **build_error**：当 `scope_id` 格式无效时抛出（`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 删除作用域下的所有记忆
>>> memory = LongTermMemory()
>>> success = await memory.delete_mem_by_scope("my_scope")
>>> print(f"删除结果: {success}")
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

添加对话消息到记忆引擎，并根据 `agent_config` 生成记忆（用户画像、变量等）。同时支持**指令性记忆**功能：当用户在对话中包含显式记忆指令（如"把...改为..."、"删除..."）时，引擎会自动识别并执行对应的增删改操作。

**参数**：

* **messages**(list[BaseMessage])：要添加的消息列表（通常包含用户消息和 AI 回复）。
* **agent_config**(AgentMemoryConfig)：Agent 记忆策略配置，包含：
  * `mem_variables: list[Param]`：需要提取的变量记忆配置（变量名、描述、类型等）；
  * `enable_long_term_mem: bool`：是否开启长期记忆生成（默认 `True`）。
  * `enable_user_profile: bool`：是否开启用户画像生成和使用（默认 `True`）。
  * `enable_semantic_memory: bool`：是否开启语义记忆生成和使用（默认 `True`）。
  * `enable_episodic_memory: bool`：是否开启情景记忆生成和使用（默认 `True`）。  
  * `enable_summary_memory: bool`：是否开启用户摘要记忆生成（默认 `True`）。  
* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时会抛出异常。默认值：`"__default__"`。
* **session_id**(str, 可选)：会话标识符。默认值：`"__default__"`。
* **timestamp**(datetime | None, 可选)：消息时间戳，若为 `None` 则使用当前 UTC 时间。默认值：`None`。
* **gen_mem**(bool, 可选)：是否生成记忆；为 `False` 时仅保存消息，不触发记忆提取。默认值：`True`。
* **gen_mem_with_history_msg_num**(int, 可选)：生成记忆时参考的历史消息数量。默认值：2。

**返回**：

* **AddMemResult**：本次记忆提取的结果，包含以下字段：
  * `variables: list[VariableUnit]`：提取的变量记忆列表；
  * `user_profile: list[FragmentMemoryUnit]`：提取的用户画像记忆列表；
  * `semantic_memory: list[FragmentMemoryUnit]`：提取的语义记忆列表；
  * `episodic_memory: list[FragmentMemoryUnit]`：提取的情景记忆列表；
  * `summary: list[SummaryUnit]`：提取的摘要记忆列表。

当 `gen_mem=False`、`scope_id` 格式无效、LLM 未初始化或消息中不包含用户消息时，返回空 `AddMemResult()`（所有字段为空列表）。

**异常**：

* **build_error**：当 `scope_id` 格式无效、LLM 未初始化或写入记忆失败时抛出（`MEMORY_ADD_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import AgentMemoryConfig
>>> from jiuwen_memory.common.schema.param import Param
>>> from jiuwen_memory.foundation.llm.schema.message import UserMessage, AssistantMessage
>>> 
>>> # 创建 Agent 记忆策略配置
>>> agent_config = AgentMemoryConfig(
>>>     mem_variables=[
>>>         Param(
>>>             name="favorite_color",
>>>             description="用户喜欢的颜色",
>>>             type="string",
>>>             required=False,
>>>         ),
>>>         Param(
>>>             name="age",
>>>             description="用户年龄",
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
>>> # 准备消息
>>> messages = [
>>>     UserMessage(content="我喜欢蓝色，我今年25岁"),
>>>     AssistantMessage(content="好的，我记住了您喜欢蓝色，今年25岁。")
>>> ]
>>> 
>>> # 添加消息
>>> memory = LongTermMemory()
>>> await memory.add_messages(
>>>     messages=messages,
>>>     agent_config=agent_config,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     session_id="session456"
>>> )
```
**指令性记忆样例**：

```python
>>> # 用户通过显式指令修改已有记忆
>>> update_messages = [
>>>     UserMessage(content="把我的年龄改为30岁"),
>>>     AssistantMessage(content="好的，已更新您的年龄信息。")
>>> ]
>>> result = await memory.add_messages(
>>>     messages=update_messages,
>>>     agent_config=agent_config,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>> )
>>> 
>>> # 用户通过显式指令删除已有记忆
>>> delete_messages = [
>>>     UserMessage(content="删除我的年龄信息"),
>>>     AssistantMessage(content="好的，已删除您的年龄信息。")
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

`add_messages` 方法的返回值模型，封装了本次记忆提取的所有结果。

**字段**：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `variables` | `list[VariableUnit]` | `[]` | 提取的变量记忆列表 |
| `user_profile` | `list[FragmentMemoryUnit]` | `[]` | 提取的用户画像记忆列表 |
| `semantic_memory` | `list[FragmentMemoryUnit]` | `[]` | 提取的语义记忆列表 |
| `episodic_memory` | `list[FragmentMemoryUnit]` | `[]` | 提取的情景记忆列表 |
| `summary` | `list[SummaryUnit]` | `[]` | 提取的摘要记忆列表 |

**说明**：

- 每个 `FragmentMemoryUnit` 包含 `operation_type` 字段（`ADD` / `UPDATE` / `DELETE`），用于区分本次操作类型。
- 指令性记忆的 UPDATE 和 DELETE 操作在返回结果中体现为对应 `operation_type` 的 `FragmentMemoryUnit`。
- 当 `add_messages` 因各种原因（`gen_mem=False`、`scope_id` 无效、LLM 未初始化等）未执行记忆提取时，返回空 `AddMemResult()`。


## class jiuwen_memory.memory_core.long_term_memory.MemInfo

```
class jiuwen_memory.memory_core.long_term_memory.MemInfo(BaseModel)
```

记忆信息模型，描述一条记忆的基本信息。

**字段**：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mem_id` | `str` | `""` | 记忆唯一标识符 |
| `content` | `str` | `""` | 记忆内容 |
| `type` | `MemoryType` | `MemoryType.USER_PROFILE` | 记忆类型 |
| `timestamp` | `datetime | None` | `None` | 记忆时间戳 |


## class jiuwen_memory.memory_core.long_term_memory.MemResult

```
class jiuwen_memory.memory_core.long_term_memory.MemResult(BaseModel)
```

记忆搜索结果模型，包含记忆信息与相似度分数。

**字段**：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mem_info` | `MemInfo | None` | `None` | 记忆信息 |
| `score` | `float` | `0.0` | 相似度分数 |


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

获取指定用户/scope/会话的最近 N 条消息，按写入顺序返回。

**参数**：

* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时会抛出异常。默认值：`"__default__"`。
* **session_id**(str, 可选)：会话标识符。默认值：`"__default__"`。
* **num**(int, 可选)：要获取的消息数量。默认值：10。

**返回**：

* **list[BaseMessage]**：消息列表，按写入时间顺序排列；若 `scope_id` 格式无效或 `message_manager` 未初始化，会抛出异常。

**异常**：

* **build_error**：当 `scope_id` 格式无效时抛出（`MEMORY_GET_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 获取最近消息
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

根据消息 id 获取单条消息及其创建时间戳。

**参数**：

* **msg_id**(str)：消息唯一标识符。

**返回**：

* **Tuple[BaseMessage, datetime] | None**：若消息存在，返回 `(消息对象, 创建时间)`；若 `message_manager` 未初始化或消息不存在，返回 `None`。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 根据ID获取消息
>>> memory = LongTermMemory()
>>> result = await memory.get_message_by_id("msg_12345")
>>> 
>>> if result:
>>>     message, timestamp = result
>>>     print(f"消息内容: {message.content}")
>>>     print(f"创建时间: {timestamp}")
>>> else:
>>>     print("未找到消息")
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

删除指定 id 的记忆条目（用户画像或变量）。

**参数**：

* **mem_id**(str)：记忆唯一标识符。
* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时直接返回。默认值：`"__default__"`。

**异常**：

* **build_error**：当 `write_manager` 未初始化时抛出（`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 删除指定记忆
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

删除指定用户在某 scope 下的所有类型记忆（用户画像、变量等）。

**参数**：

* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时直接返回。默认值：`"__default__"`。

**异常**：

* **build_error**：当 `write_manager` 未初始化时抛出。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 删除用户的所有记忆
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

更新指定 id 的记忆内容。

**参数**：

* **mem_id**(str)：记忆唯一标识符。
* **memory**(str)：新的记忆内容。
* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时直接返回。默认值：`"__default__"`。

**异常**：

* **build_error**：当 `write_manager` 未初始化时抛出（`MEMORY_UPDATE_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 更新记忆内容
>>> memory_engine = LongTermMemory()
>>> await memory_engine.update_mem_by_id(
>>>     mem_id="mem_12345",
>>>     memory="更新后的记忆内容",
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

获取用户变量（一个或多个）。

**参数**：

* **names**(list[str] | str | None, 可选)：
  * 若为 `None`：返回该用户在该 scope 下的所有变量；
  * 若为 `str`：返回单个变量（`{name: value}`）；
  * 若为 `list[str]`：返回多个变量（`{name1: value1, name2: value2, ...}`）。
  默认值：`None`。
* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时返回空字典。默认值：`"__default__"`。

**返回**：

* **dict[str, str]**：变量名到变量值的映射；若 `scope_id` 格式无效或 `search_manager` 未初始化，返回空字典或抛出异常。

**异常**：

* **build_error**：当 `search_manager` 未初始化或 `names` 类型不符合预期时抛出（`MEMORY_GET_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 获取所有变量
>>> memory = LongTermMemory()
>>> all_vars = await memory.get_variables(
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"所有变量: {all_vars}")
>>> 
>>> # 获取单个变量
>>> favorite_color = await memory.get_variables(
>>>     names="favorite_color",
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"喜欢的颜色: {favorite_color}")
>>> 
>>> # 获取多个变量
>>> some_vars = await memory.get_variables(
>>>     names=["favorite_color", "age"],
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"部分变量: {some_vars}")
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

基于语义相似度搜索用户记忆（用户画像、变量等），返回与查询最相关的 N 条记忆。

**参数**：

* **query**(str)：查询文本。
* **num**(int)：要返回的记忆数量（top-k）。
* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时返回空列表。默认值：`"__default__"`。
* **threshold**(float, 可选)：相似度阈值，低于该阈值的记忆会被过滤。默认值：0.3。

**返回**：

* **list[MemResult]**：记忆结果列表，每个 `MemResult` 包含：
  * `mem_info: MemInfo`（`mem_id / content / type / timestamp`）；
  * `score: float`（相似度分数）。

**异常**：

* **build_error**：当 `search_manager` 未初始化时抛出。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 搜索用户记忆
>>> memory = LongTermMemory()
>>> results = await memory.search_user_mem(
>>>     query="用户的兴趣爱好",
>>>     num=5,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     threshold=0.4
>>> )
>>> 
>>> for result in results:
>>>     print(f"内容: {result.mem_info.content}")
>>>     print(f"相似度: {result.score}")
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

返回指定用户在某 scope 下的记忆总数。

**参数**：

* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时返回 0。默认值：`"__default__"`。

**返回**：

* **int**：记忆总数；若 `scope_id` 格式无效，返回 0。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 获取记忆总数
>>> memory = LongTermMemory()
>>> total = await memory.user_mem_total_num(
>>>     user_id="user123",
>>>     scope_id="my_scope"
>>> )
>>> print(f"记忆总数: {total}")
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

基于语义相似度搜索用户摘要记忆，返回与查询最相关的 N 条摘要记忆。

**参数**：

* **query**(str)：搜索查询字符串。
* **num**(int)：要返回的结果数量（top-k）。
* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时返回空列表。默认值：`"__default__"`。
* **threshold**(float, 可选)：结果的最小相似度阈值；低于该阈值的记忆会被过滤。默认值：0.3。

**返回**：

* **list[MemResult]**：记忆结果列表，每个 `MemResult` 包含：
  * `mem_info: MemInfo`（`mem_id / content / type / timestamp`）；
  * `score: float`（相似度分数）。

**异常**：

* **build_error**：当 `search_manager` 未初始化时抛出。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 搜索用户摘要记忆
>>> memory = LongTermMemory()
>>> results = await memory.search_user_history_summary(
>>>     query="最近关于工作的对话",
>>>     num=5,
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     threshold=0.4
>>> )
>>> 
>>> for result in results:
>>>     print(f"内容: {result.mem_info.content}")
>>>     print(f"相似度: {result.score}")
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

分页获取指定用户在某 scope 下的记忆。

**参数**：

* **user_id**(str, 可选)：用户标识符。默认值：`"__default__"`。
* **scope_id**(str, 可选)：作用域标识符；格式无效时返回空字典。默认值：`"__default__"`。
* **page_num**(int, 可选)：页码，从 1 开始。默认值：1。
* **page_size**(int, 可选)：每页大小。默认值：10。

**返回**：

* **dict[str, Any]**：包含以下字段：
  * `total: int`：总记忆数；
  * `page_num: int`：当前页码；
  * `page_size: int`：每页大小；
  * `total_pages: int`：总页数；
  * `data: list[MemInfo]`：当前页的记忆列表。

**异常**：

* **build_error**：当 `search_manager` 未初始化时抛出（`MEMORY_GET_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> 
>>> # 分页获取用户记忆
>>> memory = LongTermMemory()
>>> result = await memory.get_user_mem_by_page(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     page_num=2,
>>>     page_size=5
>>> )
>>> 
>>> print(f"总记忆数: {result['total']}")
>>> print(f"当前页: {result['page_num']}/{result['total_pages']}")
>>> 
>>> for mem_info in result['data']:
>>>     print(f"ID: {mem_info.mem_id}, 内容: {mem_info.content[:50]}...")
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

为 `(scope_id, user_id)` 启动后台**睡时记忆巩固**（Dreaming）流程：一个后台调度器，周期性地重新读取该用户已存储的会话，通过 LLM 提炼可复用知识，并经由正常的记忆写入路径写回（与 `add_messages` 使用相同的管理器、用户级锁和冲突检测）。产出的就是普通的用户画像 / 语义 / 情景记忆单元——不引入新的记忆类型。

**参数**：

* **scope_id**(str)：作用域标识；格式非法时抛出异常。
* **user_id**(str)：用户标识。
* **config**(DreamingConfig | None, 可选)：睡时巩固配置。为 `None` 时使用默认的 `DreamingConfig()`（其 `enabled=False`，因此不会启动）。默认：`None`。
* **busy_checker**(Callable[[], bool] | None, 可选)：可选回调，每次 sweep 前调用；返回 `True` 则跳过本次 sweep（例如 agent 正忙于服务用户时）。默认：`None`。

**返回**：

* **DreamingOrchestrator | None**：后台 orchestrator 实例；当 `config.enabled` 为 `False` 时返回 `None`。**幂等**：对同一 `(scope_id, user_id)` 再次调用会返回已有的 orchestrator，而不会重复启动。

**前置条件**：

- 需先通过 `register_store` 注册 `kv_store`、消息存储以及向量存储 + 嵌入模型，并配置好该 scope 的 LLM（通过 `set_scope_config` 或 `set_config` 中的全局默认模型），否则抛出异常。

**行为**：

- orchestrator 在后台运行，每隔 `config.interval_seconds` 执行一次 sweep（首次有一段预热延迟）。已扫描的会话在 `kv_store` 中记录 checkpoint（键为 `dreaming/checkpoint/{scope_id}/{user_id}`），因此进程重启后不会重复处理旧会话。
- 会话按 `session_id` 分组；请在调用 `add_messages` 时传入有意义的 `session_id`，以保证巩固效果符合预期。

**异常**：

* **build_error**：当 `scope_id` 格式非法、所需存储未注册，或 LLM 未初始化时抛出（`MEMORY_ADD_MEMORY_EXECUTION_ERROR`）。

**示例**：

```python
>>> from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
>>> from jiuwen_memory.memory_core.config import DreamingConfig
>>> 
>>> memory = LongTermMemory()
>>> # ... 事先完成 register_store + set_scope_config ...
>>> 
>>> # 启动后台睡时记忆巩固（启动即不管，sweep 在后台按间隔运行）
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

停止正在运行的睡时巩固 orchestrator。不带参数时停止全部；否则只停止匹配所给 `scope_id` 和/或 `user_id` 的实例。

**参数**：

* **scope_id**(str | None, 可选)：若提供，只停止该 scope 的 orchestrator。默认：`None`。
* **user_id**(str | None, 可选)：若提供，只停止该用户的 orchestrator。默认：`None`。

**示例**：

```python
>>> # 停止指定 (scope, user) 的 orchestrator
>>> await memory.stop_dreaming(scope_id="my_scope", user_id="user123")
>>> 
>>> # 全部停止（例如服务关闭时）
>>> await memory.stop_dreaming()
```


> **说明**：所有方法中涉及的 `user_id`、`scope_id`、`session_id` 若使用默认值 `"__default__"`，表示使用系统默认标识符；在实际业务中，建议传入有意义的业务标识符以支持多租户隔离和精确查询。

## 相关模块

`LongTermMemory` 管理用户画像、语义记忆、情景记忆、变量和摘要等扁平记忆单元。如果需要把对话、文档或 JSON 字符串沉淀为实体、关系和事件片段组成的知识图谱，可以使用独立的 Graph Memory 模块。详见 [jiuwen_memory.memory_core.graph.graph_memory](graph_memory.md)。
