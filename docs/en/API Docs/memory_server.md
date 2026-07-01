# server.memory_server

`server.memory_server` is the **Memory Engine HTTP service entry point** provided by JiuwenMemory. It uses FastAPI to expose common `LongTermMemory` capabilities as REST APIs, allowing external systems to write, search, update, and manage long-term memory over HTTP.

The service is responsible for:

- assembling KV / DB / Vector Store backends from `.env` at startup;
- registering the embedding model and `LongTermMemory` engine configuration;
- exposing APIs for message ingestion, memory update, variable management, semantic search, and paginated queries;
- providing optional Bearer Token authentication;
- requiring `MEMORY_API_KEY` when binding to a non-local address, preventing unauthenticated network exposure.


## Startup

The service supports two startup methods:

### Method 1: CLI command (available after installation)

After installing JiuwenMemory via `pip install`, you can start the service directly with the `memory-server` command:

```bash
memory-server
```

This command is defined in `pyproject.toml` under `[project.scripts]`:

```toml
[project.scripts]
memory-server = "server.memory_server:main"
```

### Method 2: Source code

Run from the project root:

```bash
python -m server.memory_server
```

Both methods are functionally identical. After startup, the service listens on:

```text
127.0.0.1:8000
```

You can also configure the host and port with environment variables:

```bash
IP=127.0.0.1 PORT=8000 memory-server
```

Or using the source code method:

```bash
IP=127.0.0.1 PORT=8000 python -m server.memory_server
```

> **Security note**: If `IP` is not `127.*` and not `localhost`, the service checks whether `MEMORY_API_KEY` is configured. If it is missing, the process exits immediately to avoid exposing an unauthenticated API to the network.


## Environment Variables

On startup, the service loads `.env` files in the following priority order:

1. **First**: `~/.jiuwenmemory/.env`
2. **Second**: `.env` in the current working directory

If neither `.env` file is found, the service automatically creates the `~/.jiuwenmemory/` directory and prompts the user to configure it.

> ⚠️ **Important**: Setting an environment variable to an empty string (e.g., `MEMORY_DATA_DIR=`) will **not** trigger the default value — `dotenv` reads it as `""` instead of `None`. To use the default value, **delete the line or comment it out** (e.g., `# MEMORY_DATA_DIR=`).

### Service configuration

| Variable | Default | Description |
|---|---|---|
| `IP` | `127.0.0.1` | Service bind address. Non-local addresses require `MEMORY_API_KEY`. |
| `PORT` | `8000` | Service port. |
| `MEMORY_API_KEY` | empty string | API authentication key. If empty, authentication is disabled; this is recommended only for local development. |
| `MEMORY_DATA_DIR` | `~/.jiuwenmemory/memory_data` | Default data directory for local SQLite, Chroma, Shelve, and related storage. **Do not set this to an empty string** — data would then be stored in the current working directory. |

### Model configuration

| Variable | Default | Description |
|---|---|---|
| `MODEL_NAME` | `default-model` | LLM model name used for memory generation. |
| `MODEL_PROVIDER` | `SiliconFlow` | LLM client provider. |
| `API_KEY` | empty string | LLM API key. |
| `API_BASE` | empty string | LLM API base URL. |
| `EMBED_MODEL_NAME` | `BAAI/bge-m3` | Embedding model name. |
| `EMBED_API_KEY` | empty string | Embedding API key. |
| `EMBED_API_BASE` | `https://api.siliconflow.cn/v1/embeddings` | Embedding API endpoint. |

### Storage configuration

`memory_server` assembles storage backends through `server.store_factory`.

| Variable | Default | Description |
|---|---|---|
| `DB_URL` | `sqlite+aiosqlite:///{MEMORY_DATA_DIR}/sqlite_db.db` | SQLAlchemy async database URL. If omitted, local SQLite is used. |
| `KV_STORE_TYPE` | `db` | KV Store type. Supported values: `db` / `in_memory` / `shelve`. |
| `KV_SHELVE_PATH` | `{MEMORY_DATA_DIR}/shelve_kv` | Shelve file path when `KV_STORE_TYPE=shelve`. |
| `DB_STORE_TYPE` | `default` | DB Store type. Supported values: `default` / `gauss`. |
| `VECTOR_STORE_TYPE` | `chroma` | Vector Store type. Supported values: `chroma` / `milvus` / `elasticsearch` / `gauss`. |
| `VECTOR_CHROMA_PERSIST_DIR` | `MEMORY_DATA_DIR` | Chroma persistence directory. |
| `VECTOR_MILVUS_URI` | empty string | Milvus service URI. |
| `VECTOR_MILVUS_TOKEN` | empty string | Milvus token; optional. |
| `VECTOR_MILVUS_DATABASE` | `default` | Milvus database name. |
| `VECTOR_ES_HOSTS` | empty string | Elasticsearch hosts, comma-separated. |
| `VECTOR_ES_INDEX_PREFIX` | `agent_vector` | Elasticsearch index prefix. |
| `VECTOR_GAUSS_HOST` | `localhost` | Gauss vector store host. |
| `VECTOR_GAUSS_PORT` | `5432` | Gauss vector store port. |
| `VECTOR_GAUSS_DATABASE` | `postgres` | Gauss vector store database name. |
| `VECTOR_GAUSS_USER` | `postgres` | Gauss vector store user. |
| `VECTOR_GAUSS_PASSWORD` | empty string | Gauss vector store password. |


## Authentication

`memory_server` uses a lightweight HTTP middleware for authentication:

- `GET` requests are always allowed, for endpoints such as `/health` and `/`;
- if `MEMORY_API_KEY` is not configured, all requests are allowed;
- if `MEMORY_API_KEY` is configured, all `POST` / `PUT` / `DELETE` requests must include:

```http
Authorization: Bearer <MEMORY_API_KEY>
```

If the header is missing or invalid, the service returns:

```json
{
  "detail": "Unauthorized: invalid or missing API key"
}
```

with status code `401`.


## Initialization Flow

On startup, `startup_event` performs the following steps:

1. Call `create_async_engine_from_env()` to create the database engine;
2. Call `create_kv_store(engine)` to create the KV Store;
3. Call `create_db_store(engine)` to create the DB Store;
4. Call `create_vector_store()` to create the Vector Store;
5. Create `APIEmbedding` from `EMBED_*` environment variables;
6. Call `memory_engine.register_store(...)` to register stores and the embedding model;
7. Create `MemoryEngineConfig` from `MODEL_*` / `API_*` environment variables;
8. Call `memory_engine.set_config(config)` to complete engine configuration.

If initialization fails, the service logs the error and raises the exception, causing startup to fail.


## API List

### GET /health

Health check endpoint.

**Response example**:

```json
{
  "status": "healthy",
  "message": "Memory Engine API is running"
}
```


### GET /

Root endpoint. Returns a welcome message and the list of exposed endpoints.

**Response example**:

```json
{
  "message": "Welcome to Memory Engine API",
  "endpoints": [
    "POST /add_messages/",
    "POST /update_mem_by_id/",
    "POST /update_variables/",
    "POST /delete_variables/",
    "POST /delete_mem_by_scope/",
    "POST /get_variables/",
    "POST /search_memory/",
    "POST /search_user_history_summary/",
    "POST /get_user_mem_by_page/",
    "GET /health"
  ]
}
```


### POST /add_messages/

Adds a list of conversation messages to the long-term memory engine.

The service converts request `messages` to `BaseMessage` objects and constructs an `AgentMemoryConfig` from the provided `mem_variables` and extraction switches, then calls `LongTermMemory.add_messages(...)`.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `messages` | `list[dict[str, str]]` | Yes | - | Message list. Each item usually contains `role` and `content`. Missing values default to `user` / empty string. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID for business isolation. |
| `mem_variables` | `list[Param]` | No | `[]` | Variable definitions to extract from conversations. Each element is a `Param` object with `name`, `description`, `type`, `required`, etc. If omitted, no variables are extracted. |
| `enable_long_term_mem` | `bool` | No | `true` | Enable long-term memory extraction. |
| `enable_user_profile` | `bool` | No | `true` | Enable user profile memory extraction. |
| `enable_semantic_memory` | `bool` | No | `true` | Enable semantic memory extraction. |
| `enable_episodic_memory` | `bool` | No | `true` | Enable episodic memory extraction. |
| `enable_summary_memory` | `bool` | No | `true` | Enable summary memory extraction. |

**`mem_variables` field details**:

The `Param` object defines variables to extract from conversations, with the following structure:

| Sub-field | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Variable name. |
| `description` | `str` | Yes | Variable description, used to guide LLM extraction. |
| `type` | `str` | Yes | Variable type. Supported values: `string` / `boolean` / `integer` / `number` / `array` / `object`. |
| `required` | `bool` | Yes | Whether the variable is required. |
| `default` | `any` | No | Default value. |
| `items` | `Param` | No | Only used when `type=array`. Defines the array element type. |
| `properties` | `list[Param]` | No | Only used when `type=object`. Defines the object property list. |

**Request example** (no `mem_variables`, basic memory extraction only):

```bash
curl -X POST http://127.0.0.1:8000/add_messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${MEMORY_API_KEY}" \
  -d '{
    "messages": [
      {"role": "user", "content": "I like jasmine tea"},
      {"role": "assistant", "content": "Got it, I will remember your preference."}
    ],
    "user_id": "user_001",
    "scope_id": "demo"
  }'
```

**Request example** (with `mem_variables` and extraction switches):

```bash
curl -X POST http://127.0.0.1:8000/add_messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${MEMORY_API_KEY}" \
  -d '{
    "messages": [
      {"role": "user", "content": "I like jasmine tea and live in Shenzhen"},
      {"role": "assistant", "content": "Noted."}
    ],
    "user_id": "user_001",
    "scope_id": "demo",
    "mem_variables": [
      {"name": "favorite_drink", "description": "The user's favorite drink", "type": "string", "required": true},
      {"name": "city", "description": "The city where the user lives", "type": "string", "required": false}
    ],
    "enable_user_profile": true,
    "enable_summary_memory": false
  }'
```

**Response example**:

```json
{
  "status": "success",
  "message": "Messages added successfully"
}
```


### POST /update_mem_by_id/

Updates memory content by memory ID.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `mem_id` | `str` | Yes | - | Memory ID to update. |
| `memory` | `str` | Yes | - | Updated memory content. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |

**Request example**:

```json
{
  "mem_id": "mem_123",
  "memory": "The user likes jasmine tea",
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**Response example**:

```json
{
  "status": "success",
  "message": "Memory mem_123 updated successfully"
}
```


### POST /update_variables/

Updates user variable memories.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `variables` | `dict[str, str]` | Yes | - | Mapping from variable names to variable values. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |

**Request example**:

```json
{
  "variables": {
    "favorite_drink": "jasmine tea",
    "city": "Shenzhen"
  },
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**Response example**:

```json
{
  "status": "success",
  "message": "Variables updated successfully"
}
```


### POST /delete_variables/

Deletes specified user variables.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `names` | `list[str]` | Yes | - | Variable names to delete. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |

**Request example**:

```json
{
  "names": ["favorite_drink", "city"],
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**Response example**:

```json
{
  "status": "success",
  "deleted": ["favorite_drink", "city"]
}
```

> The `deleted` field directly passes through the return value of `LongTermMemory.delete_variables(...)`. Its actual structure depends on the underlying implementation.


### POST /delete_mem_by_scope/

Deletes all memories under the specified `scope_id`.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `scope_id` | `str` | Yes | - | Scope ID to delete. |

**Request example**:

```json
{
  "scope_id": "demo"
}
```

**Response example**:

```json
{
  "status": "success",
  "deleted": 12
}
```

> The `deleted` field directly passes through the return value of `LongTermMemory.delete_mem_by_scope(...)`. Its actual structure depends on the underlying implementation.


### POST /get_variables/

Gets user variables.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `names` | `list[str]` | No | `null` | Variable names to query. If omitted, the returned range is determined by the underlying implementation. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |

**Request example**:

```json
{
  "names": ["favorite_drink"],
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**Response example**:

```json
{
  "variables": {
    "favorite_drink": "jasmine tea"
  }
}
```


### POST /search_memory/

Searches user long-term memories.

The service calls `LongTermMemory.search_user_mem(...)` and serializes results into a list containing `mem_id`, `content`, `type`, and `score`.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | `str` | Yes | - | Search query. |
| `num` | `int` | No | `10` | Number of results to return. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |
| `threshold` | `float` | No | `0.3` | Similarity threshold. |

**Request example**:

```json
{
  "query": "What tea does the user like?",
  "num": 5,
  "user_id": "user_001",
  "scope_id": "demo",
  "threshold": 0.3
}
```

**Response example**:

```json
{
  "results": [
    {
      "mem_id": "mem_123",
      "content": "The user likes jasmine tea",
      "type": "user_profile",
      "score": 0.86
    }
  ]
}
```


### POST /search_user_history_summary/

Searches user history summaries.

The service calls `LongTermMemory.search_user_history_summary(...)` and returns the same result structure as `/search_memory/`.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | `str` | Yes | - | Search query. |
| `num` | `int` | No | `10` | Number of results to return. |
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |
| `threshold` | `float` | No | `0.3` | Similarity threshold. |

**Request example**:

```json
{
  "query": "What drink preferences has the user recently discussed?",
  "num": 5,
  "user_id": "user_001",
  "scope_id": "demo",
  "threshold": 0.3
}
```

**Response example**:

```json
{
  "results": [
    {
      "mem_id": "summary_123",
      "content": "The user recently mentioned that they like jasmine tea.",
      "type": "summary",
      "score": 0.78
    }
  ]
}
```


### POST /get_user_mem_by_page/

Gets user memories by page.

`memory_type` is converted to the `MemoryType` enum. Unrecognized values fall back to `MemoryType.UNKNOWN`.

**Request parameters**:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `user_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | User ID. |
| `scope_id` | `str` | No | `LongTermMemory.DEFAULT_VALUE` | Scope ID. |
| `page_size` | `int` | No | `10` | Page size. |
| `page_idx` | `int` | No | `1` | Page index, starting from 1. |
| `memory_type` | `str` | No | `UNKNOWN` | Memory type string corresponding to the `MemoryType` enum. |

**Request example**:

```json
{
  "user_id": "user_001",
  "scope_id": "demo",
  "page_size": 10,
  "page_idx": 1,
  "memory_type": "UNKNOWN"
}
```

**Response example**:

```json
{
  "results": [
    {
      "mem_id": "mem_123",
      "content": "The user likes jasmine tea",
      "type": "user_profile"
    }
  ],
  "total": 1
}
```


## Error Responses

Except for authentication failures, business endpoint exceptions are converted to `500` responses in the following format:

```json
{
  "detail": "Error searching memory: <specific error message>"
}
```

Different endpoints use different error prefixes, for example:

- `Error adding messages: ...`
- `Error updating memory: ...`
- `Error updating variables: ...`
- `Error deleting variables: ...`
- `Error deleting memory by scope: ...`
- `Error getting variables: ...`
- `Error searching memory: ...`
- `Error searching user history summary: ...`
- `Error getting user memory by page: ...`


## Minimal Example

### 1. Start the service

CLI command method:

```bash
MEMORY_API_KEY="dev-secret" \
API_KEY="your-llm-api-key" \
EMBED_API_KEY="your-embedding-api-key" \
memory-server
```

Source code method:

```bash
MEMORY_API_KEY="dev-secret" \
API_KEY="your-llm-api-key" \
EMBED_API_KEY="your-embedding-api-key" \
python -m server.memory_server
```

> The recommended approach is to configure all environment variables in `~/.jiuwenmemory/.env`, then simply run `memory-server`.

### 2. Add messages

```bash
curl -X POST http://127.0.0.1:8000/add_messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-secret" \
  -d '{
    "messages": [
      {"role": "user", "content": "I like jasmine tea"},
      {"role": "assistant", "content": "I will remember that you like jasmine tea."}
    ],
    "user_id": "user_001",
    "scope_id": "demo"
  }'
```

### 3. Search memory

```bash
curl -X POST http://127.0.0.1:8000/search_memory/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-secret" \
  -d '{
    "query": "What does the user like to drink?",
    "num": 5,
    "user_id": "user_001",
    "scope_id": "demo",
    "threshold": 0.3
  }'
```


## Data Storage Location

By default (when `MEMORY_DATA_DIR` is not set or is commented out), all local storage data is stored under:

```text
~/.jiuwenmemory/memory_data/
├── sqlite_db.db              ← Shared SQLite database for DB Store + KV Store (db mode)
├── chroma.sqlite3            ← Chroma Vector Store index file
└── (UUID directories)       ← Chroma collection data (one per scope)
```

| Storage type | Default location | Notes |
|----------|----------|------|
| DB Store + KV Store (db mode) | `~/.jiuwenmemory/memory_data/sqlite_db.db` | Shares the same SQLite file |
| Vector Store (Chroma) | `~/.jiuwenmemory/memory_data/` | Chroma persistence directory |
| KV Store (in_memory) | In-process memory | Lost on restart |
| KV Store (shelve) | `~/.jiuwenmemory/memory_data/shelve_kv` | Local file |

When switching to remote backends (PostgreSQL, Milvus, Elasticsearch, etc.), data is stored on the corresponding server, not locally.


## Notes

- `GET /health` and `GET /` do not require authentication. Other write, delete, and query endpoints require Bearer Token when `MEMORY_API_KEY` is configured.
- If `DB_URL` is not configured, local SQLite is used. If `VECTOR_STORE_TYPE` is not configured, Chroma is used and persisted under `MEMORY_DATA_DIR`.
- `/add_messages/` supports `mem_variables` for specifying variable definitions and `enable_*` switches for controlling each type of memory extraction. When these fields are omitted, all memory extraction is enabled and no variables are extracted.
- `/search_memory/` and `/search_user_history_summary/` return service-layer serialized results instead of exposing internal objects directly.
- The `total` field of `/get_user_mem_by_page/` is currently the length of the returned list in this response, not necessarily the total number of matching records in storage.
- The `page_idx` of `/get_user_mem_by_page/` starts from **1** (not 0).
