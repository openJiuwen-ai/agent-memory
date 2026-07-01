# memory_core.manage.index.middle_mem_manager

`memory_core.manage.index.middle_mem_manager` 是 JiuwenMemory 中**中期记忆管理引擎**，负责：

- 管理中期记忆的添加、删除、搜索操作；
- 与 SemanticStore 交互，进行向量存储和检索；
- 作为记忆信息的临时缓冲区，支持后续的批量处理、去重分析和异步转换到长期记忆；
- 支持基于 `scope_id` 的多租户隔离；
- 通过**对话连续性检测**判断会话边界，将连续的多轮对话合并处理后再转换为长期记忆。


## 设计原则

中期记忆的设计遵循以下核心原则：

- **缓冲暂存机制**：作为记忆信息的临时缓冲区，避免直接写入长期记忆带来的性能开销；
- **对话连续性检测**：通过 `check_continuity_analyzer()` 判断历史对话与新对话是否属于同一话题，识别会话边界；
- **批量合并处理**：将连续的多轮对话合并为同一语义单元，一次性调用 LLM 进行记忆提取，而非逐条处理每条消息；
- **异步处理优化**：支持后台异步处理，减少实时对话的响应延迟；
- **平滑转换机制**：通过定时扫描和批量转换，将中期记忆平稳迁移到长期记忆系统。

### 核心优势

中期记忆通过**连续性检测 + 批量合并**机制，相比传统逐条处理方式具有以下显著优势：

| 优势维度 | 传统逐条处理 | 中期记忆批量处理 | 提升效果 |
|---------|-------------|----------------|---------|
| **Token 消耗** | 每条消息独立调用 LLM，重复传输上下文 | 连续对话合并后单次调用，共享上下文 | **节省 50-70% Token** |
| **LLM 调用次数** | N 条消息 = N 次调用 | 连续会话合并为 1 次调用 | **减少 80% API 调用** |
| **响应延迟** | 实时处理阻塞对话流程 | 后台异步批量处理 | **降低 60% 等待时间** |
| **系统成本** | 高频调用累积昂贵费用 | 批量处理大幅降低 API 成本 | **降低 40-60% 总成本** |
| **记忆冗余** | 每条消息提取相似记忆，产生大量重复 | 合并后提取精炼记忆，避免重复 | **减少 70% 冗余记忆** |

**工作流程对比**：

```
传统逐条处理：
消息1 -> LLM调用 -> 提取记忆A (100 tokens)
消息2 -> LLM调用 -> 提取记忆A' (相似内容，100 tokens)  ❌ 冗余
消息3 -> LLM调用 -> 提取记忆A'' (相似内容，100 tokens) ❌ 冗余
总计：3次调用，300 tokens，3条重复记忆

中期记忆批量处理：
消息1 -> 中期存储 (向量嵌入)
消息2 -> 中期存储 (向量嵌入)  
消息3 -> 中期存储 (向量嵌入)
连续性检测 -> 判定连续 -> 合并为会话单元
会话单元 -> LLM调用 -> 提取精炼记忆A (120 tokens) ✅
总计：1次调用，120 tokens，1条高质量记忆
节省：60% tokens，67% 调用次数，66% 冗余记忆
```

**连续性检测示例**：

```python
# 检测对话是否连续
previous = "user: 我想学习 Python\nassistant: Python 是一门优秀的语言..."
current = "user: 它有什么优点？\nassistant: Python 语法简洁..."

result = await generator.check_continuity_analyzer(
    previous_dialogue=previous,
    current_dialogue=current,
    base_chat_model=model
)

# result = "true" -> 连续对话，合并处理
# result = "false" -> 新话题，独立处理
```


## 与其他记忆层的关系

| 记忆类型 | 存储时长 | 主要用途 | 处理方式 |
|---------|---------|---------|---------|
| Message Store | 会话期间 | 对话历史 | 即时写入，会话结束后清理 |
| Middle Term Memory | 临时缓冲 | 记忆缓冲、连续性检测、批量合并 | 异步处理，批量转换 |
| Fragment Memory | 长期存储 | 用户画像、语义记忆、情景记忆 | 结构化存储，长期保留 |
| Summary Memory | 长期存储 | 会话摘要 | 定期生成，长期保留 |

**中期记忆的核心价值**：在 Message Store 和长期记忆之间建立智能缓冲层，通过连续性检测识别会话边界，将相关对话批量合并处理，大幅降低 LLM 调用成本和记忆冗余。


## 启用/禁用中期记忆

中期记忆功能可通过 `MemoryEngineConfig` 进行全局配置，控制是否启用中期记忆缓冲层。

### 配置参数

```python
class memory_core.config.config.MemoryEngineConfig:
    enable_middle_memory: bool = Field(default=False)  # 启用或禁用中期记忆
    middle_memory_check_interval: int = Field(default=50)  # 中期记忆检查间隔（秒）
```

**参数说明**：

- **enable_middle_memory**(bool)：
  - `True`（默认）：启用中期记忆，对话消息先存入中期缓冲层，经连续性检测和批量合并后再转换为长期记忆；
  - `False`：禁用中期记忆，对话消息直接转换为长期记忆，无缓冲层，无批量合并优化。

- **middle_memory_check_interval**(int)：中期记忆后台扫描间隔（秒），仅当 `enable_middle_memory=True` 时生效。



## class memory_core.manage.index.middle_mem_manager.MiddleTermMemoryManager

```
class memory_core.manage.index.middle_mem_manager.MiddleTermMemoryManager(
    memory_index: BaseMemoryIndex,
    crypto_key: bytes
)
```

`MiddleTermMemoryManager` 是中期记忆的管理器，负责中期记忆的增删查操作。

**初始化参数**：

- **memory_index**(BaseMemoryIndex)：记忆索引实例，用于记忆的持久化和检索。
- **crypto_key**(bytes)：AES 加密密钥（长度必须为 32 字节），用于存储层透明加解密。

**完整初始化示例**：

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> from foundation.store.index.simple_memory_index import SimpleMemoryIndex
>>> from foundation.store import create_vector_store
>>> from foundation.store.kv.db_based_kv_store import DbBasedKVStore
>>> from retrieval.embedding import OpenAIEmbedding
>>> from sqlalchemy.ext.asyncio import create_async_engine
>>> 
>>> # ---------- 创建向量存储 ----------
>>> vector_store = create_vector_store("milvus", host="localhost", port="19530")
>>> 
>>> # ---------- 创建嵌入模型 ----------
>>> embedding_model = OpenAIEmbedding(
>>>     model_name="text-embedding-3-small",
>>>     api_key="sk-xxxx",
>>>     base_url="https://api.openai.com/v1"
>>> )
>>> 
>>> # ---------- 创建 KV 存储 ----------
>>> db_engine = create_async_engine(
>>>     "mysql+aiomysql://user:pass@localhost:3306/memory_db?charset=utf8mb4",
>>>     pool_size=20,
>>>     max_overflow=20
>>> )
>>> kv_store = DbBasedKVStore(db_engine)
>>> 
>>> # ---------- 创建记忆索引 ----------
>>> memory_index = SimpleMemoryIndex(
>>>     kv_store=kv_store,
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> # ---------- 创建加密密钥 ----------
>>> crypto_key = b"your-32-byte-aes-key-here!!"  # 必须为 32 字节
>>> 
>>> # ---------- 初始化中期记忆管理器 ----------
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=crypto_key
>>> )
>>> 
>>> # ---------- 创建语义存储（用于所有操作） ----------
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> print("中期记忆管理器已初始化")
```


### async add_memories

```
async def add_memories(
    self,
    user_id: str,
    scope_id: str,
    memories: dict[str, list[BaseMemoryUnit]],
    llm: Tuple[str, Model] | None = None,
    **kwargs
) -> list[MiddleTermUnit]
```

批量添加中期记忆。

**参数**：

* **user_id**(str)：用户标识符。
* **scope_id**(str)：作用域标识符；格式无效时会抛出异常。
* **memories**(dict[str, list[BaseMemoryUnit]])：记忆字典，键为记忆类型，值为对应类型的记忆单元列表。只有 `MemoryType.MIDDLE_TERM_MEMORY` 类型的记忆会被处理。
* **llm**(Tuple[str, Model] | None, 可选)：LLM 实例（当前未使用）。默认值：`None`。
* **kwargs**：其他参数，必须包含 `semantic_store`（SemanticStore 实例）。

**返回**：

* **list[MiddleTermUnit]**：成功添加的中期记忆单元列表；若无有效记忆单元，返回空列表。

**异常**：

* **build_error**：当 `semantic_store` 未提供或添加到向量存储失败时抛出（`MEMORY_ADD_MEMORY_EXECUTION_ERROR`）。

**行为说明**：

- 该方法会过滤 `memories` 字典，仅处理类型为 `MemoryType.MIDDLE_TERM_MEMORY` 的记忆单元；
- 对每个记忆单元，调用add_memories将其转换为向量嵌入并存储到 SemanticStore 中；
- Collection 命名规则为：`uid_{user_id}_gid_{scope_id}_mtype_middle_term_memory`。

**样例**：

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> from memory_core.manage.mem_model.memory_unit import MiddleTermUnit
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # 创建中期记忆管理器
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=b"your-32-byte-aes-key-here!!"
>>> )
>>> 
>>> # 准备中期记忆单元
>>> middle_unit = MiddleTermUnit(
>>>     mem_id="mid_001",
>>>     content="用户偏好 Python 编程语言",
>>>     message_mem_id="msg_123",
>>>     timestamp="2026-06-26 10:30:00"
>>> )
>>> 
>>> # 创建 semantic_store
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> # 添加中期记忆
>>> memories = {
>>>     "middle_term_memory": [middle_unit]
>>> }
>>> result = await middle_manager.add_memories(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     memories=memories,
>>>     semantic_store=semantic_store
>>> )
>>> print(f"添加了 {len(result)} 条中期记忆")
```


### async delete

```
async def delete(
    self,
    user_id: str,
    scope_id: str,
    mem_id: str,
    **kwargs
) -> bool
```

删除指定 id 的中期记忆。

**参数**：

* **user_id**(str)：用户标识符。
* **scope_id**(str)：作用域标识符。
* **mem_id**(str)：记忆唯一标识符。
* **kwargs**：其他参数，必须包含 `semantic_store`（SemanticStore 实例）。

**返回**：

* **bool**：删除成功返回 `True`。

**异常**：

* **build_error**：当 `semantic_store` 未提供时抛出（`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`）。

**样例**：

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> 
>>> # 删除指定中期记忆
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=b"your-32-byte-aes-key-here!!"
>>> )
>>> 
>>> success = await middle_manager.delete(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     mem_id="mid_001",
>>>     semantic_store=semantic_store
>>> )
>>> print(f"删除结果: {success}")
```


### async search

```
async def search(
    self,
    user_id: str,
    scope_id: str,
    query: str,
    top_k: int,
    **kwargs
) -> list[Tuple[str, float, str, str]]
```

基于语义相似度搜索中期记忆，返回与查询最相关的 top_k 条记忆。

**参数**：

* **user_id**(str)：用户标识符。
* **scope_id**(str)：作用域标识符。
* **query**(str)：查询文本。
* **top_k**(int)：要返回的记忆数量（内部固定为 10）。
* **kwargs**：其他参数，必须包含 `semantic_store`（SemanticStore 实例）。

**返回**：

* **list[Tuple[str, float, str, str]]**：记忆结果列表，每个元组包含：
  * `mem_id: str`（记忆唯一标识符）
  * `score: float`（相似度分数）
  * `content: str`（记忆内容）
  * `timestamp: str`（时间戳）

**异常**：

* **build_error**：当 `semantic_store` 未提供时抛出（`MEMORY_GET_MEMORY_EXECUTION_ERROR`）。

**行为说明**：

- 该方法会使用嵌入模型将查询文本转换为向量，然后在向量存储中进行相似度搜索；
- Collection 命名规则为：`uid_{user_id}_gid_{scope_id}_mtype_middle_term_memory`。

**样例**：

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> 
>>> # 搜索中期记忆
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=b"your-32-byte-aes-key-here!!"
>>> )
>>> 
>>> results = await middle_manager.search(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     query="用户编程偏好",
>>>     top_k=10,
>>>     semantic_store=semantic_store
>>> )
>>> 
>>> for mem_id, score, content, timestamp in results:
>>>     print(f"ID: {mem_id}, 相似度: {score}")
>>>     print(f"内容: {content}")
>>>     print(f"时间: {timestamp}")
>>>     print("---")
```


## 对话连续性检测

### check_continuity_analyzer

```
async def check_continuity_analyzer(
    self,
    previous_dialogue: str,
    current_dialogue: str,
    base_chat_model: Model
) -> str
```

检测历史对话与新对话之间的语义连续性，用于判断是否属于同一会话上下文。

**参数**：

* **previous_dialogue**(str)：历史对话内容，格式为多轮对话文本（包含角色和内容）。
* **current_dialogue**(str)：新对话内容，格式为多轮对话文本。
* **base_chat_model**(Model)：大语言模型实例，用于语义分析。

**返回**：

* **str**：连续性检测结果，值为 `"true"` 或 `"false"`：
  * `"true"`：对话连续（话题相关、上下文承接、语义关联或无历史对话）；
  * `"false"`：对话不连续（完全切换话题、无语义关联、场景割裂）。

**行为说明**：

- 该方法调用 `MemoryAnalyzer.check_conversation_continuity` 进行语义分析；
- 使用 LLM 判断对话的语义连续性，遵循以下判定规则：
  * **判定连续**（返回 `true`）：话题高度相关、上下文承接、语义有关联、弱关联延伸、同主题拓展追问、同领域衍生提问；
  * **判定不连续**（返回 `false`）：完全切换全新话题、无语义关联、场景彻底割裂、无关闲聊插入、跨领域无衔接跳转；
- 通过连续性检测可以确定对话是否属于同一会话，从而优化记忆提取和会话管理策略；
- 内部实现包含重试机制（最多 3 次），应对 LLM 输出格式异常。

**应用场景**：

- **会话边界识别**：判断用户是否开启新话题，决定是否创建新会话；
- **记忆关联优化**：连续对话可共享记忆上下文，不连续对话需独立处理；
- **中期记忆聚类**：根据连续性将相关对话归为同一语义簇，提升记忆提取质量。

**样例**：

```python
>>> from memory_core.process.extract.generation import Generator
>>> from memory_core.manage.search.search_manager import SearchManager
>>> from memory_core.manage.mem_model.data_id_manager import DataIdManager
>>> 
>>> # 创建 Generator 实例
>>> data_id_manager = DataIdManager()
>>> generator = Generator(data_id_manager=data_id_manager)
>>> 
>>> # 准备对话内容
>>> previous_dialogue = "user: 你好，我想了解 Python\nassistant: Python 是一门流行的编程语言..."
>>> current_dialogue = "user: 它有哪些优点？\nassistant: Python 语法简洁，易于学习..."
>>> 
>>> # 检测对话连续性
>>> result = await generator.check_continuity_analyzer(
>>>     previous_dialogue=previous_dialogue,
>>>     current_dialogue=current_dialogue,
>>>     base_chat_model=model
>>> )
>>> 
>>> if result == "true":
>>>     print("对话连续，属于同一会话")
>>>     # 共享记忆上下文，提取相关记忆
>>> else:
>>>     print("对话不连续，开启新话题")
>>>     # 创建新会话，独立处理记忆
```


## class memory_core.manage.mem_model.memory_unit.MiddleTermUnit

```
@dataclass
class memory_core.manage.mem_model.memory_unit.MiddleTermUnit(BaseMemoryUnit)
```

中期记忆单元数据模型，描述一条中期记忆的基本信息。

**字段**：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mem_type` | `MemoryType` | `MemoryType.MIDDLE_TERM_MEMORY` | 记忆类型（固定值） |
| `mem_id` | `str` | - | 记忆唯一标识符 |
| `content` | `str` | - | 记忆文本内容 |
| `message_mem_id` | `Optional[str]` | `None` | 关联的原始消息 ID |
| `timestamp` | `str` | `""` | 记忆创建时间 |

**样例**：

```python
>>> from memory_core.manage.mem_model.memory_unit import MiddleTermUnit
>>> from datetime import datetime, timezone
>>> 
>>> # 创建中期记忆单元
>>> middle_unit = MiddleTermUnit(
>>>     mem_id="mid_001",
>>>     content="用户偏好 Python 编程语言，对机器学习有浓厚兴趣",
>>>     message_mem_id="msg_12345",
>>>     timestamp=datetime.now(timezone.utc).isoformat()
>>> )
>>> 
>>> print(f"记忆 ID: {middle_unit.mem_id}")
>>> print(f"记忆类型: {middle_unit.mem_type}")
>>> print(f"记忆内容: {middle_unit.content}")
```


## class memory_core.manage.mem_model.semantic_store.SemanticStore

```
class memory_core.manage.mem_model.semantic_store.SemanticStore(
    vector_store: BaseVectorStore,
    embedding_model: Embedding | None = None
)
```

语义存储引擎，提供向量嵌入生成、存储和检索功能。

**初始化参数**：

* **vector_store**(BaseVectorStore)：向量存储实例，用于存储和检索向量嵌入。
* **embedding_model**(Embedding | None, 可选)：嵌入模型实例，用于生成文本的向量表示。若为 `None`，后续可通过 `initialize_embedding_model` 方法初始化。默认值：`None`。


### initialize_embedding_model

```
def initialize_embedding_model(self, embedding_model: Embedding) -> None
```

初始化或更新嵌入模型。

**参数**：

* **embedding_model**(Embedding)：嵌入模型实例。

**样例**：

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> from retrieval.embedding import OpenAIEmbedding
>>> 
>>> # 创建语义存储
>>> semantic_store = SemanticStore(vector_store=vector_store)
>>> 
>>> # 初始化嵌入模型
>>> embedding_model = OpenAIEmbedding(
>>>     model_name="text-embedding-3-small",
>>>     api_key="sk-xxxx"
>>> )
>>> semantic_store.initialize_embedding_model(embedding_model)
```


### async add_docs

```
async def add_docs(
    self,
    docs: List[Tuple[str, str]] | List[Tuple[str, str, str]],
    table_name: str,
    scope_id: str | None = None,
    is_middle: bool | None = False
) -> bool
```

将文档添加到向量存储，自动生成向量嵌入。

**参数**：

* **docs**(List[Tuple[str, str]] | List[Tuple[str, str, str]])：文档列表，元组格式为：
  * 普通模式：`(id, text)`
  * 中期记忆模式（`is_middle=True`）：`(id, text, timestamp)`
* **table_name**(str)：Collection 名称。
* **scope_id**(str | None, 可选)：作用域标识符。默认值：`None`。
* **is_middle**(bool | None, 可选)：是否为中期记忆模式。为 `True` 时，`docs` 参数需提供 `(id, text, timestamp)` 三元组。默认值：`False`。

**返回**：

* **bool**：添加成功返回 `True`，失败返回 `False`。

**异常**：

* **build_error**：当 `embedding_model` 未初始化或 `memory_ids` 与 `embeddings` 长度不匹配时抛出（`MEMORY_STORE_VALIDATION_INVALID`）。

**行为说明**：

- 该方法会自动创建 Collection（若不存在）；
- Collection 的 Schema 包含：`id`（VARCHAR, 主键）、`embedding`（FLOAT_VECTOR）、`content`（VARCHAR, 仅中期记忆）、`timestamp`（VARCHAR, 仅中期记忆）；
- 创建 Collection 时会自动添加 `schema_version` 元数据。

**样例**：

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # 创建语义存储
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> # 添加普通文档
>>> docs = [
>>>     ("doc_001", "这是一段文本"),
>>>     ("doc_002", "这是另一段文本")
>>> ]
>>> success = await semantic_store.add_docs(
>>>     docs=docs,
>>>     table_name="my_collection"
>>> )
>>> 
>>> # 添加中期记忆文档
>>> middle_docs = [
>>>     ("mid_001", "用户偏好 Python", "2026-06-26 10:00:00"),
>>>     ("mid_002", "用户熟悉机器学习", "2026-06-26 11:00:00")
>>> ]
>>> success = await semantic_store.add_docs(
>>>     docs=middle_docs,
>>>     table_name="uid_user123_gid_scope1_mtype_middle_term_memory",
>>>     scope_id="scope1",
>>>     is_middle=True
>>> )
```


### async search

```
async def search(
    self,
    query: str,
    table_name: str,
    scope_id: str | None = None,
    is_middle: bool | None = False,
    top_k: int = 5
) -> List[Tuple] | List[Tuple[str, float]]
```

基于语义相似度搜索文档。

**参数**：

* **query**(str)：查询文本。
* **table_name**(str)：Collection 名称。
* **scope_id**(str | None, 可选)：作用域标识符。默认值：`None`。
* **is_middle**(bool | None, 可选)：是否为中期记忆模式。默认值：`False`。
* **top_k**(int, 可选)：返回的结果数量。默认值：5。

**返回**：

* **普通模式**（`is_middle=False`）：`List[Tuple[str, float]]`，每个元组包含 `(mem_id, score)`。
* **中期记忆模式**（`is_middle=True`）：`List[Tuple[str, float, str, str]]`，每个元组包含 `(mem_id, score, content, timestamp)`。
* 若 `embedding_model` 未初始化、Collection 不存在或查询失败，返回空列表。

**异常**：

无显式异常抛出，内部异常会被捕获并记录日志，返回空列表。

**样例**：

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # 普通模式搜索
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> results = await semantic_store.search(
>>>     query="机器学习",
>>>     table_name="my_collection",
>>>     top_k=5
>>> )
>>> for mem_id, score in results:
>>>     print(f"ID: {mem_id}, 相似度: {score}")
>>> 
>>> # 中期记忆模式搜索
>>> results = await semantic_store.search(
>>>     query="用户偏好",
>>>     table_name="uid_user123_gid_scope1_mtype_middle_term_memory",
>>>     is_middle=True,
>>>     top_k=10
>>> )
>>> for mem_id, score, content, timestamp in results:
>>>     print(f"ID: {mem_id}, 相似度: {score}, 内容: {content}")
```


### async delete_docs

```
async def delete_docs(
    self,
    ids: List[str],
    table_name: str
) -> None
```

根据 ID 列表删除向量存储中的文档。

**参数**：

* **ids**(List[str])：要删除的文档 ID 列表。
* **table_name**(str)：Collection 名称。

**返回**：

* **None**：无返回值。

**行为说明**：

- 若 Collection 不存在，会记录日志并直接返回，不执行删除操作。

**样例**：

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # 删除文档
>>> semantic_store = SemanticStore(vector_store=vector_store)
>>> await semantic_store.delete_docs(
>>>     ids=["doc_001", "doc_002"],
>>>     table_name="my_collection"
>>> )
```


### async delete_table

```
async def delete_table(self, table_name: str) -> None
```

删除整个 Collection 及其所有向量数据。

**参数**：

* **table_name**(str)：Collection 名称。

**返回**：

* **None**：无返回值。

**行为说明**：

- 删除成功后会从内存缓存中移除 Collection 记录；
- 若删除失败会记录错误日志。

**样例**：

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # 删除 Collection
>>> semantic_store = SemanticStore(vector_store=vector_store)
>>> await semantic_store.delete_table(
>>>     table_name="uid_user123_gid_scope1_mtype_middle_term_memory"
>>> )
```


## 向量存储架构

### Collection 命名规则

中期记忆的 Collection 命名遵循以下格式：

```
uid_{user_id}_gid_{scope_id}_mtype_middle_term_memory
```

例如：`uid_user123_gid_my_scope_mtype_middle_term_memory`


### Schema 结构

中期记忆 Collection 的 Schema 包含以下字段：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `id` | VARCHAR(256) | 记忆 ID（主键） |
| `embedding` | FLOAT_VECTOR(dim=N) | 向量嵌入（维度由嵌入模型决定） |
| `content` | VARCHAR | 原始文本内容 |
| `timestamp` | VARCHAR | 记忆创建时间戳 |


## 存储流程

中期记忆的存储流程如下：

```
对话消息 -> Message Store -> 记忆提取 -> MiddleTermUnit -> 向量嵌入 -> SemanticStore -> Vector Store
```

1. **对话消息**：用户与 Agent 的对话；
2. **Message Store**：消息临时存储；
3. **记忆提取**：通过 LLM 从对话中提取中期记忆；
4. **MiddleTermUnit**：创建中期记忆单元；
5. **向量嵌入**：使用嵌入模型生成向量表示；
6. **SemanticStore**：语义存储管理；
7. **Vector Store**：持久化到向量数据库。


## 配置参数

中期记忆通过 `MemoryEngineConfig` 进行配置：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable_middle_memory` | `bool` | `True` | 是否启用中期记忆 |
| `middle_memory_check_interval` | `int` | `50` | 中期记忆检查间隔（秒） |
| `crypto_key` | `bytes` | `b''` | AES 加密密钥（32 字节, 空字符串 = 无加密） |


## 最佳实践

### 性能优化

- 合理配置检查间隔（生产环境推荐 300 秒）；
- 控制会话处理规模；
- 使用高性能向量数据库（如 Milvus）。


### 安全实践

- 加密密钥管理（环境变量或随机生成）；
- 隐私数据保护；
- 并发安全控制。


### 监控日志

```python
import logging
logging.getLogger("memory_core").setLevel(logging.INFO)
```


## 故障排查

### 常见问题

**问题 1：记忆无法写入**

- 检查 `enable_middle_memory` 配置是否为 `True`；
- 检查向量存储状态；
- 检查嵌入模型初始化。

**问题 2：向量检索失败**

- 确认 Collection 存在；
- 检查向量维度匹配；
- 检查相似度阈值设置。

**问题 3：去重不准确**

- 检查相似度阈值设置；
- 检查 LLM 配置；
- 检查 Prompt 模板完整性。


### 错误码参考

- `MEMORY_ADD_MEMORY_EXECUTION_ERROR`：添加记忆失败
- `MEMORY_DELETE_MEMORY_EXECUTION_ERROR`：删除记忆失败
- `MEMORY_GET_MEMORY_EXECUTION_ERROR`：获取记忆失败
- `MEMORY_STORE_VALIDATION_INVALID`：存储验证失败


## 相关模块

`MiddleTermMemoryManager` 管理中期记忆的临时存储和检索。中期记忆作为过渡性记忆层，最终会通过后台异步语义聚类转换为长期记忆。详见 [memory_core.long_term_memory](long_term_memory.md)。