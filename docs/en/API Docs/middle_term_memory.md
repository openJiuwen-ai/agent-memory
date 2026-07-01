# memory_core.manage.index.middle_mem_manager

`memory_core.manage.index.middle_mem_manager` is the **middle-term memory management engine** in JiuwenMemory, responsible for:

- Managing add, delete, and search operations for middle-term memory;
- Interacting with SemanticStore for vector storage and retrieval;
- Serving as a temporary buffer for memory information, supporting subsequent batch processing, deduplication analysis, and asynchronous conversion to long-term memory;
- Supporting multi-tenant isolation based on `scope_id`;
- Determining session boundaries through **conversation continuity detection**, merging consecutive multi-turn conversations before converting to long-term memory.


## Design Principles

The middle-term memory design follows these core principles:

- **Buffer Mechanism**: Serving as a temporary buffer for memory information to avoid performance overhead from directly writing to long-term memory;
- **Conversation Continuity Detection**: Using `check_continuity_analyzer()` to determine whether historical and new conversations belong to the same topic, identifying session boundaries;
- **Batch Merging Processing**: Merging consecutive multi-turn conversations into the same semantic unit, calling LLM once for memory extraction instead of processing each message individually;
- **Asynchronous Processing Optimization**: Supporting background asynchronous processing to reduce response latency in real-time conversations;
- **Smooth Transition Mechanism**: Periodically scanning and batch converting middle-term memory to the long-term memory system.

### Core Advantages

Middle-term memory, through the **continuity detection + batch merging** mechanism, offers significant advantages over traditional per-message processing:

| Advantage Dimension | Traditional Per-Message Processing | Middle-Term Memory Batch Processing | Improvement |
|---------|-------------|----------------|---------|
| **Token Consumption** | Independent LLM call for each message, repeated context transmission | Single call after merging consecutive conversations, shared context | **Saves 50-70% Tokens** |
| **LLM Call Count** | N messages = N calls | Consecutive sessions merged into 1 call | **Reduces 80% API Calls** |
| **Response Latency** | Real-time processing blocks conversation flow | Background asynchronous batch processing | **Reduces 60% Wait Time** |
| **System Cost** | High-frequency calls accumulate expensive costs | Batch processing significantly reduces API costs | **Reduces 40-60% Total Cost** |
| **Memory Redundancy** | Each message extracts similar memories, generating duplicates | Merged extraction produces refined memories, avoiding duplicates | **Reduces 70% Redundant Memories** |

**Workflow Comparison**:

```
Traditional Per-Message Processing:
Message1 -> LLM Call -> Extract Memory A (100 tokens)
Message2 -> LLM Call -> Extract Memory A' (similar content, 100 tokens)  ❌ Redundant
Message3 -> LLM Call -> Extract Memory A'' (similar content, 100 tokens) ❌ Redundant
Total: 3 calls, 300 tokens, 3 duplicate memories

Middle-Term Memory Batch Processing:
Message1 -> Middle Storage (vector embedding)
Message2 -> Middle Storage (vector embedding)  
Message3 -> Middle Storage (vector embedding)
Continuity Detection -> Determined Continuous -> Merge as session unit
Session Unit -> LLM Call -> Extract refined Memory A (120 tokens) ✅
Total: 1 call, 120 tokens, 1 high-quality memory
Savings: 60% tokens, 67% call count, 66% redundant memories
```

**Continuity Detection Example**:

```python
# Check if conversation is continuous
previous = "user: I want to learn Python\nassistant: Python is an excellent language..."
current = "user: What are its advantages?\nassistant: Python syntax is concise..."

result = await generator.check_continuity_analyzer(
    previous_dialogue=previous,
    current_dialogue=current,
    base_chat_model=model
)

# result = "true" -> Continuous conversation, merge processing
# result = "false" -> New topic, independent processing
```


## Relationship with Other Memory Layers

| Memory Type | Storage Duration | Primary Purpose | Processing Method |
|---------|---------|---------|---------|
| Message Store | During session | Conversation history | Immediate write, cleanup after session ends |
| Middle Term Memory | Temporary buffer | Memory buffering, continuity detection, batch merging | Asynchronous processing, batch conversion |
| Fragment Memory | Long-term storage | User profile, semantic memory, episodic memory | Structured storage, long-term retention |
| Summary Memory | Long-term storage | Session summaries | Periodic generation, long-term retention |

**Core Value of Middle-Term Memory**: Establishing an intelligent buffer layer between Message Store and long-term memory, identifying session boundaries through continuity detection, batch merging related conversations to significantly reduce LLM call costs and memory redundancy.


## Enable/Disable Middle-Term Memory

Middle-term memory functionality can be globally configured through `MemoryEngineConfig` to control whether to enable the middle-term memory buffer layer.

### Configuration Parameters

```python
class memory_core.config.config.MemoryEngineConfig:
    enable_middle_memory: bool = Field(default=False)  # Enable or disable middle-term memory
    middle_memory_check_interval: int = Field(default=50)  # Middle-term memory check interval (seconds)
```

**Parameter Description**:

- **enable_middle_memory**(bool):
  - `True` (default): Enable middle-term memory, conversation messages are first stored in the middle buffer layer, undergo continuity detection and batch merging before converting to long-term memory;
  - `False`: Disable middle-term memory, conversation messages are directly converted to long-term memory without buffer layer or batch merging optimization.

- **middle_memory_check_interval**(int): Middle-term memory background scan interval (seconds), only effective when `enable_middle_memory=True`.


## class memory_core.manage.index.middle_mem_manager.MiddleTermMemoryManager

```
class memory_core.manage.index.middle_mem_manager.MiddleTermMemoryManager(
    memory_index: BaseMemoryIndex,
    crypto_key: bytes
)
```

`MiddleTermMemoryManager` is the manager for middle-term memory, responsible for add, delete, and query operations.

**Initialization Parameters**:

- **memory_index**(BaseMemoryIndex): Memory index instance for memory persistence and retrieval.
- **crypto_key**(bytes): AES encryption key (must be 32 bytes in length) for transparent encryption/decryption at storage layer.

**Complete Initialization Example**:

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> from foundation.store.index.simple_memory_index import SimpleMemoryIndex
>>> from foundation.store import create_vector_store
>>> from foundation.store.kv.db_based_kv_store import DbBasedKVStore
>>> from retrieval.embedding import OpenAIEmbedding
>>> from sqlalchemy.ext.asyncio import create_async_engine
>>> 
>>> # ---------- Create Vector Store ----------
>>> vector_store = create_vector_store("milvus", host="localhost", port="19530")
>>> 
>>> # ---------- Create Embedding Model ----------
>>> embedding_model = OpenAIEmbedding(
>>>     model_name="text-embedding-3-small",
>>>     api_key="sk-xxxx",
>>>     base_url="https://api.openai.com/v1"
>>> )
>>> 
>>> # ---------- Create KV Store ----------
>>> db_engine = create_async_engine(
>>>     "mysql+aiomysql://user:pass@localhost:3306/memory_db?charset=utf8mb4",
>>>     pool_size=20,
>>>     max_overflow=20
>>> )
>>> kv_store = DbBasedKVStore(db_engine)
>>> 
>>> # ---------- Create Memory Index ----------
>>> memory_index = SimpleMemoryIndex(
>>>     kv_store=kv_store,
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> # ---------- Create Encryption Key ----------
>>> crypto_key = b"your-32-byte-aes-key-here!!"  # Must be 32 bytes
>>> 
>>> # ---------- Initialize Middle-Term Memory Manager ----------
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=crypto_key
>>> )
>>> 
>>> # ---------- Create Semantic Store (for all operations) ----------
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> print("Middle-term memory manager initialized")
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

Batch add middle-term memories.

**Parameters**:

* **user_id**(str): User identifier.
* **scope_id**(str): Scope identifier; throws exception if format is invalid.
* **memories**(dict[str, list[BaseMemoryUnit]]): Memory dictionary where keys are memory types and values are lists of corresponding memory units. Only `MemoryType.MIDDLE_TERM_MEMORY` type memories are processed.
* **llm**(Tuple[str, Model] | None, optional): LLM instance (currently unused). Default: `None`.
* **kwargs**: Other parameters, must include `semantic_store` (SemanticStore instance).

**Returns**:

* **list[MiddleTermUnit]**: List of successfully added middle-term memory units; returns empty list if no valid memory units.

**Exceptions**:

* **build_error**: Thrown when `semantic_store` is not provided or adding to vector storage fails (`MEMORY_ADD_MEMORY_EXECUTION_ERROR`).

**Behavior**:

- Filters `memories` dictionary, only processing memory units of type `MemoryType.MIDDLE_TERM_MEMORY`;
- For each memory unit, call add_memories to convert it to vector embedding and stored in SemanticStore;
- Collection naming rule: `uid_{user_id}_gid_{scope_id}_mtype_middle_term_memory`.

**Example**:

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> from memory_core.manage.mem_model.memory_unit import MiddleTermUnit
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # Create middle-term memory manager
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=b"your-32-byte-aes-key-here!!"
>>> )
>>> 
>>> # Prepare middle-term memory unit
>>> middle_unit = MiddleTermUnit(
>>>     mem_id="mid_001",
>>>     content="User prefers Python programming language",
>>>     message_mem_id="msg_123",
>>>     timestamp="2026-06-26 10:30:00"
>>> )
>>> 
>>> # Create semantic_store
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> # Add middle-term memory
>>> memories = {
>>>     "middle_term_memory": [middle_unit]
>>> }
>>> result = await middle_manager.add_memories(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     memories=memories,
>>>     semantic_store=semantic_store
>>> )
>>> print(f"Added {len(result)} middle-term memories")
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

Delete a specific middle-term memory by ID.

**Parameters**:

* **user_id**(str): User identifier.
* **scope_id**(str): Scope identifier.
* **mem_id**(str): Memory unique identifier.
* **kwargs**: Other parameters, must include `semantic_store` (SemanticStore instance).

**Returns**:

* **bool**: Returns `True` on successful deletion.

**Exceptions**:

* **build_error**: Thrown when `semantic_store` is not provided (`MEMORY_DELETE_MEMORY_EXECUTION_ERROR`).

**Example**:

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> 
>>> # Delete specific middle-term memory
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
>>> print(f"Deletion result: {success}")
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

Search middle-term memories based on semantic similarity, returning the top_k most relevant memories.

**Parameters**:

* **user_id**(str): User identifier.
* **scope_id**(str): Scope identifier.
* **query**(str): Query text.
* **top_k**(int): Number of memories to return (internally fixed at 10).
* **kwargs**: Other parameters, must include `semantic_store` (SemanticStore instance).

**Returns**:

* **list[Tuple[str, float, str, str]]**: Memory result list, each tuple contains:
  * `mem_id: str` (memory unique identifier)
  * `score: float` (similarity score)
  * `content: str` (memory content)
  * `timestamp: str` (timestamp)

**Exceptions**:

* **build_error**: Thrown when `semantic_store` is not provided (`MEMORY_GET_MEMORY_EXECUTION_ERROR`).

**Behavior**:

- Uses embedding model to convert query text to vector, then performs similarity search in vector storage;
- Collection naming rule: `uid_{user_id}_gid_{scope_id}_mtype_middle_term_memory`.

**Example**:

```python
>>> from memory_core.manage.index.middle_mem_manager import MiddleTermMemoryManager
>>> 
>>> # Search middle-term memories
>>> middle_manager = MiddleTermMemoryManager(
>>>     memory_index=memory_index,
>>>     crypto_key=b"your-32-byte-aes-key-here!!"
>>> )
>>> 
>>> results = await middle_manager.search(
>>>     user_id="user123",
>>>     scope_id="my_scope",
>>>     query="user programming preferences",
>>>     top_k=10,
>>>     semantic_store=semantic_store
>>> )
>>> 
>>> for mem_id, score, content, timestamp in results:
>>>     print(f"ID: {mem_id}, Similarity: {score}")
>>>     print(f"Content: {content}")
>>>     print(f"Time: {timestamp}")
>>>     print("---")
```


## Conversation Continuity Detection

### check_continuity_analyzer

```
async def check_continuity_analyzer(
    self,
    previous_dialogue: str,
    current_dialogue: str,
    base_chat_model: Model
) -> str
```

Detect semantic continuity between historical and new conversations to determine if they belong to the same session context.

**Parameters**:

* **previous_dialogue**(str): Historical conversation content, formatted as multi-turn conversation text (including roles and content).
* **current_dialogue**(str): New conversation content, formatted as multi-turn conversation text.
* **base_chat_model**(Model): Large language model instance for semantic analysis.

**Returns**:

* **str**: Continuity detection result, value is `"true"` or `"false"`:
  * `"true"`: Conversation is continuous (topic-related, context-continued, semantically-associated or no historical conversation);
  * `"false"`: Conversation is discontinuous (completely switched topic, no semantic association, scene-separated).

**Behavior**:

- Calls `MemoryAnalyzer.check_conversation_continuity` for semantic analysis;
- Uses LLM to determine conversation semantic continuity, following these rules:
  * **Determined continuous** (returns `true`): Highly relevant topic, context continuation, semantic association, weak association extension, same-topic follow-up questions, same-domain derivative questions;
  * **Determined discontinuous** (returns `false`): Completely new topic, no semantic association, completely separated scene, irrelevant chat insertion, cross-domain jump without transition;
- Continuity detection determines if conversation belongs to same session, optimizing memory extraction and session management strategies;
- Internal implementation includes retry mechanism (max 3 times) to handle LLM output format exceptions.

**Application Scenarios**:

- **Session Boundary Identification**: Determine if user started a new topic, decide whether to create new session;
- **Memory Association Optimization**: Continuous conversations can share memory context, discontinuous conversations need independent processing;
- **Middle-Term Memory Clustering**: Cluster related conversations based on continuity to improve memory extraction quality.

**Example**:

```python
>>> from memory_core.process.extract.generation import Generator
>>> from memory_core.manage.search.search_manager import SearchManager
>>> from memory_core.manage.mem_model.data_id_manager import DataIdManager
>>> 
>>> # Create Generator instance
>>> data_id_manager = DataIdManager()
>>> generator = Generator(data_id_manager=data_id_manager)
>>> 
>>> # Prepare conversation content
>>> previous_dialogue = "user: Hello, I want to learn about Python\nassistant: Python is a popular programming language..."
>>> current_dialogue = "user: What are its advantages?\nassistant: Python syntax is concise, easy to learn..."
>>> 
>>> # Detect conversation continuity
>>> result = await generator.check_continuity_analyzer(
>>>     previous_dialogue=previous_dialogue,
>>>     current_dialogue=current_dialogue,
>>>     base_chat_model=model
>>> )
>>> 
>>> if result == "true":
>>>     print("Conversation is continuous, belongs to same session")
>>>     # Share memory context, extract related memories
>>> else:
>>>     print("Conversation is discontinuous, new topic started")
>>>     # Create new session, process memory independently
```


## class memory_core.manage.mem_model.memory_unit.MiddleTermUnit

```
@dataclass
class memory_core.manage.mem_model.memory_unit.MiddleTermUnit(BaseMemoryUnit)
```

Middle-term memory unit data model, describing basic information of a middle-term memory.

**Fields**:

| Field | Type | Default Value | Description |
|------|------|--------|------|
| `mem_type` | `MemoryType` | `MemoryType.MIDDLE_TERM_MEMORY` | Memory type (fixed value) |
| `mem_id` | `str` | - | Memory unique identifier |
| `content` | `str` | - | Memory text content |
| `message_mem_id` | `Optional[str]` | `None` | Associated original message ID |
| `timestamp` | `str` | `""` | Memory creation time |

**Example**:

```python
>>> from memory_core.manage.mem_model.memory_unit import MiddleTermUnit
>>> from datetime import datetime, timezone
>>> 
>>> # Create middle-term memory unit
>>> middle_unit = MiddleTermUnit(
>>>     mem_id="mid_001",
>>>     content="User prefers Python programming language, has strong interest in machine learning",
>>>     message_mem_id="msg_12345",
>>>     timestamp=datetime.now(timezone.utc).isoformat()
>>> )
>>> 
>>> print(f"Memory ID: {middle_unit.mem_id}")
>>> print(f"Memory Type: {middle_unit.mem_type}")
>>> print(f"Memory Content: {middle_unit.content}")
```


## class memory_core.manage.mem_model.semantic_store.SemanticStore

```
class memory_core.manage.mem_model.semantic_store.SemanticStore(
    vector_store: BaseVectorStore,
    embedding_model: Embedding | None = None
)
```

Semantic storage engine, providing vector embedding generation, storage, and retrieval functionality.

**Initialization Parameters**:

* **vector_store**(BaseVectorStore): Vector storage instance for storing and retrieving vector embeddings.
* **embedding_model**(Embedding | None, optional): Embedding model instance for generating vector representations of text. If `None`, can be initialized later via `initialize_embedding_model` method. Default: `None`.


### initialize_embedding_model

```
def initialize_embedding_model(self, embedding_model: Embedding) -> None
```

Initialize or update the embedding model.

**Parameters**:

* **embedding_model**(Embedding): Embedding model instance.

**Example**:

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> from retrieval.embedding import OpenAIEmbedding
>>> 
>>> # Create semantic storage
>>> semantic_store = SemanticStore(vector_store=vector_store)
>>> 
>>> # Initialize embedding model
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

Add documents to vector storage, automatically generating vector embeddings.

**Parameters**:

* **docs**(List[Tuple[str, str]] | List[Tuple[str, str, str]]): Document list, tuple format:
  * Normal mode: `(id, text)`
  * Middle-term memory mode (`is_middle=True`): `(id, text, timestamp)`
* **table_name**(str): Collection name.
* **scope_id**(str | None, optional): Scope identifier. Default: `None`.
* **is_middle**(bool | None, optional): Whether in middle-term memory mode. When `True`, `docs` parameter needs to provide `(id, text, timestamp)` triple. Default: `False`.

**Returns**:

* **bool**: Returns `True` on successful addition, `False` on failure.

**Exceptions**:

* **build_error**: Thrown when `embedding_model` is not initialized or `memory_ids` and `embeddings` length mismatch (`MEMORY_STORE_VALIDATION_INVALID`).

**Behavior**:

- Automatically creates Collection if it doesn't exist;
- Collection Schema includes: `id` (VARCHAR, primary key), `embedding` (FLOAT_VECTOR), `content` (VARCHAR, only for middle-term memory), `timestamp` (VARCHAR, only for middle-term memory);
- Automatically adds `schema_version` metadata when creating Collection.

**Example**:

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # Create semantic storage
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> 
>>> # Add normal documents
>>> docs = [
>>>     ("doc_001", "This is a text"),
>>>     ("doc_002", "This is another text")
>>> ]
>>> success = await semantic_store.add_docs(
>>>     docs=docs,
>>>     table_name="my_collection"
>>> )
>>> 
>>> # Add middle-term memory documents
>>> middle_docs = [
>>>     ("mid_001", "User prefers Python", "2026-06-26 10:00:00"),
>>>     ("mid_002", "User familiar with machine learning", "2026-06-26 11:00:00")
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

Search documents based on semantic similarity.

**Parameters**:

* **query**(str): Query text.
* **table_name**(str): Collection name.
* **scope_id**(str | None, optional): Scope identifier. Default: `None`.
* **is_middle**(bool | None, optional): Whether in middle-term memory mode. Default: `False`.
* **top_k**(int, optional): Number of results to return. Default: 5.

**Returns**:

* **Normal mode** (`is_middle=False`): `List[Tuple[str, float]]`, each tuple contains `(mem_id, score)`.
* **Middle-term memory mode** (`is_middle=True`): `List[Tuple[str, float, str, str]]`, each tuple contains `(mem_id, score, content, timestamp)`.
* Returns empty list if `embedding_model` not initialized, Collection doesn't exist, or query fails.

**Exceptions**:

No explicit exception thrown, internal exceptions captured and logged, returning empty list.

**Example**:

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # Normal mode search
>>> semantic_store = SemanticStore(
>>>     vector_store=vector_store,
>>>     embedding_model=embedding_model
>>> )
>>> results = await semantic_store.search(
>>>     query="machine learning",
>>>     table_name="my_collection",
>>>     top_k=5
>>> )
>>> for mem_id, score in results:
>>>     print(f"ID: {mem_id}, Similarity: {score}")
>>> 
>>> # Middle-term memory mode search
>>> results = await semantic_store.search(
>>>     query="user preferences",
>>>     table_name="uid_user123_gid_scope1_mtype_middle_term_memory",
>>>     is_middle=True,
>>>     top_k=10
>>> )
>>> for mem_id, score, content, timestamp in results:
>>>     print(f"ID: {mem_id}, Similarity: {score}, Content: {content}")
```


### async delete_docs

```
async def delete_docs(
    self,
    ids: List[str],
    table_name: str
) -> None
```

Delete documents from vector storage by ID list.

**Parameters**:

* **ids**(List[str]): List of document IDs to delete.
* **table_name**(str): Collection name.

**Returns**:

* **None**: No return value.

**Behavior**:

- If Collection doesn't exist, logs and returns directly without executing deletion.

**Example**:

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # Delete documents
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

Delete entire Collection and all its vector data.

**Parameters**:

* **table_name**(str): Collection name.

**Returns**:

* **None**: No return value.

**Behavior**:

- Removes Collection record from memory cache after successful deletion;
- Logs error if deletion fails.

**Example**:

```python
>>> from memory_core.manage.mem_model.semantic_store import SemanticStore
>>> 
>>> # Delete Collection
>>> semantic_store = SemanticStore(vector_store=vector_store)
>>> await semantic_store.delete_table(
>>>     table_name="uid_user123_gid_scope1_mtype_middle_term_memory"
>>> )
```


## Vector Storage Architecture

### Collection Naming Rule

Middle-term memory Collection naming follows this format:

```
uid_{user_id}_gid_{scope_id}_mtype_middle_term_memory
```

Example: `uid_user123_gid_my_scope_mtype_middle_term_memory`


### Schema Structure

Middle-term memory Collection Schema contains these fields:

| Field Name | Type | Description |
|--------|------|------|
| `id` | VARCHAR(256) | Memory ID (primary key) |
| `embedding` | FLOAT_VECTOR(dim=N) | Vector embedding (dimension determined by embedding model) |
| `content` | VARCHAR | Original text content |
| `timestamp` | VARCHAR | Memory creation timestamp |


## Storage Flow

Middle-term memory storage flow:

```
Conversation Message -> Message Store -> Memory Extraction -> MiddleTermUnit -> Vector Embedding -> SemanticStore -> Vector Store
```

1. **Conversation Message**: User-Agent conversation;
2. **Message Store**: Temporary message storage;
3. **Memory Extraction**: Extract middle-term memory from conversation via LLM;
4. **MiddleTermUnit**: Create middle-term memory unit;
5. **Vector Embedding**: Generate vector representation using embedding model;
6. **SemanticStore**: Semantic storage management;
7. **Vector Store**: Persist to vector database.


## Configuration Parameters

Middle-term memory is configured via `MemoryEngineConfig`:

| Parameter | Type | Default | Description |
|------|------|--------|------|
| `enable_middle_memory` | `bool` | `True` | Whether to enable middle-term memory |
| `middle_memory_check_interval` | `int` | `50` | Middle-term memory check interval (seconds) |
| `crypto_key` | `bytes` | `b''` | AES encryption key (32 bytes, empty = no encryption) |


## Best Practices

### Performance Optimization

- Reasonably configure check interval (300 seconds recommended for production);
- Control session processing scale;
- Use high-performance vector database (such as Milvus).


### Security Practices

- Encryption key management (environment variables or random generation);
- Privacy data protection;
- Concurrency safety control.


### Monitoring Logs

```python
import logging
logging.getLogger("memory_core").setLevel(logging.INFO)
```


## Troubleshooting

### Common Issues

**Issue 1: Memory Cannot Be Written**

- Check if `enable_middle_memory` configuration is `True`;
- Check vector storage status;
- Check embedding model initialization.

**Issue 2: Vector Retrieval Fails**

- Confirm Collection exists;
- Check vector dimension matching;
- Check similarity threshold settings.

**Issue 3: Deduplication Inaccurate**

- Check similarity threshold settings;
- Check LLM configuration;
- Check Prompt template completeness.


### Error Code Reference

- `MEMORY_ADD_MEMORY_EXECUTION_ERROR`: Memory addition failed
- `MEMORY_DELETE_MEMORY_EXECUTION_ERROR`: Memory deletion failed
- `MEMORY_GET_MEMORY_EXECUTION_ERROR`: Memory retrieval failed
- `MEMORY_STORE_VALIDATION_INVALID`: Storage validation failed


## Related Modules

`MiddleTermMemoryManager` manages temporary storage and retrieval of middle-term memory. As a transitional memory layer, middle-term memory is eventually converted to long-term memory through background asynchronous semantic clustering. See [memory_core.long_term_memory](long_term_memory.md).