# server.memory_server

`server.memory_server` 是 JiuwenMemory 提供的 **Memory Engine HTTP 服务入口**，基于 FastAPI 将 `LongTermMemory` 的常用能力封装为 REST API，方便外部系统通过 HTTP 调用长期记忆的写入、检索、变量管理和健康检查能力。

该服务主要负责：

- 启动时根据 `.env` 自动装配 KV / DB / Vector Store；
- 注册嵌入模型与 `LongTermMemory` 引擎配置；
- 提供消息写入、记忆更新、变量增删查、语义检索、分页查询等 API；
- 提供可选的 Bearer Token 鉴权；
- 在非本地监听时强制要求配置 `MEMORY_API_KEY`，避免服务裸露到网络。


## 启动方式

服务支持两种启动方式：

### 方式一：CLI 命令（安装后可用）

通过 `pip install` 安装 JiuwenMemory 后，可直接使用 `memory-server` 命令启动：

```bash
memory-server
```

该命令定义在 `pyproject.toml` 的 `[project.scripts]` 中：

```toml
[project.scripts]
memory-server = "jiuwen_memory.server.memory_server:main"
```

### 方式二：源码运行

在项目根目录下运行：

```bash
python -m jiuwen_memory.server.memory_server
```

> **配置说明**：大模型（`MODEL_PROVIDER`、`MODEL_NAME`、`API_KEY`、`API_BASE`）和嵌入模型（`EMBED_MODEL_NAME`、`EMBED_API_KEY`、`EMBED_API_BASE`）相关环境变量**必须手动配置**，默认值为空，未配置时服务无法正常工作。存储、监听地址等其他配置项均有默认值，可按需覆盖。

两种方式效果完全一致，启动后默认监听：

```text
127.0.0.1:8000
```

也可以通过环境变量指定监听地址和端口：

```bash
IP=127.0.0.1 PORT=8000 memory-server
```

或源码方式：

```bash
IP=127.0.0.1 PORT=8000 python -m jiuwen_memory.server.memory_server
```

> **安全说明**：当 `IP` 不是 `127.*` 且不是 `localhost` 时，服务会检查是否配置了 `MEMORY_API_KEY`。如果未配置，进程会直接退出，避免无鉴权服务暴露到网络。


## 环境变量

服务启动时按优先级加载 `.env` 文件：

1. **优先** `~/.jiuwenmemory/.env`
2. **其次** 当前工作目录下的 `.env`

如果两个路径都找不到 `.env`，服务会自动创建 `~/.jiuwenmemory/` 目录并提示用户配置。

> ⚠️ **注意**：`MEMORY_DATA_DIR` 等环境变量若设为空字符串（如 `MEMORY_DATA_DIR=`），不会触发默认值——`dotenv` 会将其读为 `""` 而非 `None`。要使用默认值，请**删掉该行或注释掉**（如 `# MEMORY_DATA_DIR=`）。

### 服务配置

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `IP` | `127.0.0.1` | 服务监听地址。非本地地址必须配置 `MEMORY_API_KEY`。 |
| `PORT` | `8000` | 服务监听端口。 |
| `MEMORY_API_KEY` | 空字符串 | API 鉴权密钥。为空时不启用鉴权，仅建议本地开发使用。 |
| `MEMORY_DATA_DIR` | `~/.jiuwenmemory/memory_data` | 默认数据目录，用于 SQLite、Chroma、Shelve 等本地存储。**不要设为空字符串**，否则数据会存到当前工作目录。 |

### 模型配置

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `MODEL_NAME` | 空字符串 | 记忆生成使用的大模型名称，需在 `.env` 中配置。 |
| `MODEL_PROVIDER` | 空字符串 | 大模型客户端提供方，需在 `.env` 中配置。 |
| `API_KEY` | 空字符串 | 大模型 API Key。 |
| `API_BASE` | 空字符串 | 大模型 API Base URL。 |
| `EMBED_MODEL_NAME` | 空字符串 | 嵌入模型名称，需在 `.env` 中配置。 |
| `EMBED_API_KEY` | 空字符串 | 嵌入模型 API Key，需在 `.env` 中配置。 |
| `EMBED_API_BASE` | 空字符串 | 嵌入模型接口地址，需在 `.env` 中配置。 |

### 存储配置

`memory_server` 通过 `server.store_factory` 装配存储后端。

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `DB_URL` | `sqlite+aiosqlite:///${MEMORY_DATA_DIR}/sqlite_db.db` | SQLAlchemy 异步数据库连接 URL。未配置时使用本地 SQLite。 |
| `KV_STORE_TYPE` | `db` | KV Store 类型，支持 `db` / `in_memory` / `shelve`。 |
| `KV_SHELVE_PATH` | `{MEMORY_DATA_DIR}/shelve_kv` | `KV_STORE_TYPE=shelve` 时的 shelve 文件路径。 |
| `DB_STORE_TYPE` | `default` | DB Store 类型，支持 `default` / `gauss`。 |
| `VECTOR_STORE_TYPE` | `chroma` | Vector Store 类型，支持 `chroma` / `milvus` / `elasticsearch` / `gauss`。 |
| `VECTOR_CHROMA_PERSIST_DIR` | `MEMORY_DATA_DIR` | Chroma 向量库持久化目录。 |
| `VECTOR_MILVUS_URI` | 空字符串 | Milvus 服务地址。 |
| `VECTOR_MILVUS_TOKEN` | 空字符串 | Milvus Token，可为空。 |
| `VECTOR_MILVUS_DATABASE` | `default` | Milvus 数据库名。 |
| `VECTOR_ES_HOSTS` | 空字符串 | Elasticsearch hosts，多个地址用英文逗号分隔。 |
| `VECTOR_ES_INDEX_PREFIX` | `agent_vector` | Elasticsearch 紀引前缀。 |
| `VECTOR_GAUSS_HOST` | `localhost` | Gauss 向量库主机。 |
| `VECTOR_GAUSS_PORT` | `5432` | Gauss 向量库端口。 |
| `VECTOR_GAUSS_DATABASE` | `postgres` | Gauss 向量库数据库名。 |
| `VECTOR_GAUSS_USER` | `postgres` | Gauss 向量库用户名。 |
| `VECTOR_GAUSS_PASSWORD` | 空字符串 | Gauss 向量库密码。 |


## 鉴权机制

`memory_server` 使用一个简单的 HTTP 中间件进行鉴权：

- `GET` 请求始终放行，用于 `/health` 和 `/` 等只读接口；
- 如果未配置 `MEMORY_API_KEY`，所有请求都放行；
- 如果配置了 `MEMORY_API_KEY`，所有 `POST` / `PUT` / `DELETE` 请求必须携带：

```http
Authorization: Bearer <MEMORY_API_KEY>
```

未携带或密钥不匹配时返回：

```json
{
  "detail": "Unauthorized: invalid or missing API key"
}
```

状态码为 `401`。


## 初始化流程

服务启动时会执行 `startup_event`，完成以下步骤：

1. 调用 `create_async_engine_from_env()` 创建数据库连接；
2. 调用 `create_kv_store(engine)` 创建 KV Store；
3. 调用 `create_db_store(engine)` 创建 DB Store；
4. 调用 `create_vector_store()` 创建 Vector Store；
5. 根据 `EMBED_*` 环境变量创建 `APIEmbedding`；
6. 调用 `memory_engine.register_store(...)` 注册存储和嵌入模型；
7. 根据 `MODEL_*` / `API_*` 环境变量创建 `MemoryEngineConfig`；
8. 调用 `memory_engine.set_config(config)` 完成引擎配置。

如果初始化失败，服务会记录错误日志并抛出异常，启动失败。


## API 列表

### GET /health

健康检查接口。

**响应示例**：

```json
{
  "status": "healthy",
  "message": "Memory Engine API is running"
}
```


### GET /

根接口，返回服务欢迎信息和当前暴露的 endpoint 列表。

**响应示例**：

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

添加一组对话消息到长期记忆引擎。

服务会将请求中的 `messages` 转换为 `BaseMessage`，并根据请求中提供的 `mem_variables` 和记忆抽取开关构造 `AgentMemoryConfig`，调用 `LongTermMemory.add_messages(...)`。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `messages` | `list[dict[str, str]]` | 是 | - | 消息列表，每条消息通常包含 `role` 和 `content`。缺失时分别默认 `user` / 空字符串。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID，用于业务隔离。 |
| `mem_variables` | `list[MemVariable]` | 否 | `[]` | 需抽取的变量定义列表。每个元素只需 `name` + `description`（其余字段可选、有默认值）。不传则不抽取变量。 |
| `enable_long_term_mem` | `bool` | 否 | `true` | 是否启用长期记忆抽取。 |
| `enable_user_profile` | `bool` | 否 | `true` | 是否启用用户画像记忆抽取。 |
| `enable_semantic_memory` | `bool` | 否 | `true` | 是否启用语义记忆抽取。 |
| `enable_episodic_memory` | `bool` | 否 | `true` | 是否启用情节记忆抽取。 |
| `enable_summary_memory` | `bool` | 否 | `true` | 是否启用摘要记忆抽取。 |

**`mem_variables` 字段说明**：

`MemVariable` 对象用于定义需要从对话中抽取的变量，结构如下。仅 `name`、`description` 必填，其余由 server 补默认值后透传给引擎：

| 子字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `name` | `str` | 是 | - | 变量名称。 |
| `description` | `str` | 是 | - | 变量描述，用于指导 LLM 抽取。 |
| `type` | `str` | 否 | `string` | 变量类型，仅支持简单类型 `string` / `boolean` / `integer` / `number`。传 `array`/`object` 或未知值会被 422 拒绝（变量抽取是扁平 key/value 结构，不支持嵌套类型）。 |
| `required` | `bool` | 否 | `true` | 是否必填。 |
| `default` | `any` | 否 | `null` | 默认值。 |

> 传未知字段会被 422 拒绝（`extra='forbid'`），因此 `descripton` 这类字段名拼错会被显式报出，而非静默忽略。

**请求示例**（不传 `mem_variables`，仅做基础记忆抽取）：

```bash
curl -X POST http://127.0.0.1:8000/add_messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${MEMORY_API_KEY}" \
  -d '{
    "messages": [
      {"role": "user", "content": "我喜欢喝茉莉花茶"},
      {"role": "assistant", "content": "好的，我会记住你的偏好。"}
    ],
    "user_id": "user_001",
    "scope_id": "demo"
  }'
```

**请求示例**（传入 `mem_variables`，指定抽取变量）：

```bash
curl -X POST http://127.0.0.1:8000/add_messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${MEMORY_API_KEY}" \
  -d '{
    "messages": [
      {"role": "user", "content": "我喜欢喝茉莉花茶，住在深圳"},
      {"role": "assistant", "content": "记住了。"}
    ],
    "user_id": "user_001",
    "scope_id": "demo",
    "mem_variables": [
      {"name": "favorite_drink", "description": "用户最喜欢的饮品"},
      {"name": "city", "description": "用户所在城市", "type": "string", "required": false}
    ],
    "enable_user_profile": true,
    "enable_summary_memory": false
  }'
```

**响应示例**：

```json
{
  "status": "success",
  "message": "Messages added successfully"
}
```


### POST /update_mem_by_id/

根据记忆 ID 更新记忆内容。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `mem_id` | `str` | 是 | - | 待更新的记忆 ID。 |
| `memory` | `str` | 是 | - | 更新后的记忆内容。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |

**请求示例**：

```json
{
  "mem_id": "mem_123",
  "memory": "用户喜欢喝茉莉花茶",
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**响应示例**：

```json
{
  "status": "success",
  "message": "Memory mem_123 updated successfully"
}
```


### POST /update_variables/

更新用户变量记忆。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `variables` | `dict[str, str]` | 是 | - | 变量名到变量值的映射。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |

**请求示例**：

```json
{
  "variables": {
    "favorite_drink": "茉莉花茶",
    "city": "深圳"
  },
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**响应示例**：

```json
{
  "status": "success",
  "message": "Variables updated successfully"
}
```


### POST /delete_variables/

删除指定用户变量。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `names` | `list[str]` | 是 | - | 要删除的变量名列表。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |

**请求示例**：

```json
{
  "names": ["favorite_drink", "city"],
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**响应示例**：

```json
{
  "status": "success",
  "deleted": ["favorite_drink", "city"]
}
```

> `deleted` 字段直接透传 `LongTermMemory.delete_variables(...)` 的返回值，实际结构取决于底层实现。


### POST /delete_mem_by_scope/

删除指定 `scope_id` 下的所有记忆。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `scope_id` | `str` | 是 | - | 待删除的作用域 ID。 |

**请求示例**：

```json
{
  "scope_id": "demo"
}
```

**响应示例**：

```json
{
  "status": "success",
  "deleted": 12
}
```

> `deleted` 字段直接透传 `LongTermMemory.delete_mem_by_scope(...)` 的返回值，实际结构取决于底层实现。


### POST /get_variables/

获取用户变量。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `names` | `list[str]` | 否 | `null` | 指定变量名列表；为空时由底层实现决定返回范围。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |

**请求示例**：

```json
{
  "names": ["favorite_drink"],
  "user_id": "user_001",
  "scope_id": "demo"
}
```

**响应示例**：

```json
{
  "variables": {
    "favorite_drink": "茉莉花茶"
  }
}
```


### POST /search_memory/

搜索用户长期记忆。

服务会调用 `LongTermMemory.search_user_mem(...)`，并将结果序列化为包含 `mem_id`、`content`、`type`、`score` 的列表。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | `str` | 是 | - | 搜索查询。 |
| `num` | `int` | 否 | `10` | 返回结果数量。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |
| `threshold` | `float` | 否 | `0.3` | 相似度阈值。 |

**请求示例**：

```json
{
  "query": "用户喜欢喝什么茶？",
  "num": 5,
  "user_id": "user_001",
  "scope_id": "demo",
  "threshold": 0.3
}
```

**响应示例**：

```json
{
  "results": [
    {
      "mem_id": "mem_123",
      "content": "用户喜欢喝茉莉花茶",
      "type": "user_profile",
      "score": 0.86
    }
  ]
}
```


### POST /search_user_history_summary/

搜索用户历史摘要。

服务会调用 `LongTermMemory.search_user_history_summary(...)`，并返回与 `/search_memory/` 相同结构的结果列表。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | `str` | 是 | - | 搜索查询。 |
| `num` | `int` | 否 | `10` | 返回结果数量。 |
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |
| `threshold` | `float` | 否 | `0.3` | 相似度阈值。 |

**请求示例**：

```json
{
  "query": "用户最近聊过什么饮品偏好？",
  "num": 5,
  "user_id": "user_001",
  "scope_id": "demo",
  "threshold": 0.3
}
```

**响应示例**：

```json
{
  "results": [
    {
      "mem_id": "summary_123",
      "content": "用户最近提到自己喜欢茉莉花茶。",
      "type": "summary",
      "score": 0.78
    }
  ]
}
```


### POST /get_user_mem_by_page/

分页获取用户记忆。

`memory_type` 会被转换为 `MemoryType` 枚举。无法识别的值会回退为 `MemoryType.UNKNOWN`。

**请求参数**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `user_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 用户 ID。 |
| `scope_id` | `str` | 否 | `LongTermMemory.DEFAULT_VALUE` | 作用域 ID。 |
| `page_size` | `int` | 否 | `10` | 每页数量。 |
| `page_idx` | `int` | 否 | `1` | 页码索引，从 1 开始。 |
| `memory_type` | `str` | 否 | `UNKNOWN` | 记忆类型字符串，对应 `MemoryType` 枚举值。 |

**请求示例**：

```json
{
  "user_id": "user_001",
  "scope_id": "demo",
  "page_size": 10,
  "page_idx": 1,
  "memory_type": "UNKNOWN"
}
```

**响应示例**：

```json
{
  "results": [
    {
      "mem_id": "mem_123",
      "content": "用户喜欢喝茉莉花茶",
      "type": "user_profile"
    }
  ],
  "total": 1
}
```


## 错误响应

除鉴权失败外，业务接口内部异常会被转换为 `500` 响应，格式为：

```json
{
  "detail": "Error searching memory: <具体错误信息>"
}
```

不同接口的错误前缀不同，例如：

- `Error adding messages: ...`
- `Error updating memory: ...`
- `Error updating variables: ...`
- `Error deleting variables: ...`
- `Error deleting memory by scope: ...`
- `Error getting variables: ...`
- `Error searching memory: ...`
- `Error searching user history summary: ...`
- `Error getting user memory by page: ...`


## 最小使用示例

### 1. 启动服务

CLI 命令方式：

```bash
MEMORY_API_KEY="dev-secret" \
MODEL_PROVIDER="xxxx" \
MODEL_NAME="xxxx" \
API_KEY="xxxx" \
API_BASE="xxxx" \
EMBED_MODEL_NAME="xxxx" \
EMBED_API_KEY="xxxx" \
EMBED_API_BASE="xxxx" \
memory-server
```

源码运行方式：

```bash
MEMORY_API_KEY="dev-secret" \
MODEL_PROVIDER="xxxx" \
MODEL_NAME="xxxx" \
API_KEY="xxxx" \
API_BASE="xxxx" \
EMBED_MODEL_NAME="xxxx" \
EMBED_API_KEY="xxxx" \
EMBED_API_BASE="xxxx" \
python -m jiuwen_memory.server.memory_server
```

> 更推荐的方式是在 `~/.jiuwenmemory/.env` 中配置好所有环境变量，然后直接运行 `memory-server` 即可。上方示例中的 `xxxx` 需替换为你实际使用的 LLM/Embedding 提供方的配置值。

### 2. 写入对话

```bash
curl -X POST http://127.0.0.1:8000/add_messages/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-secret" \
  -d '{
    "messages": [
      {"role": "user", "content": "我喜欢喝茉莉花茶"},
      {"role": "assistant", "content": "我记住了，你喜欢喝茉莉花茶。"}
    ],
    "user_id": "user_001",
    "scope_id": "demo"
  }'
```

### 3. 搜索记忆

```bash
curl -X POST http://127.0.0.1:8000/search_memory/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-secret" \
  -d '{
    "query": "用户喜欢喝什么？",
    "num": 5,
    "user_id": "user_001",
    "scope_id": "demo",
    "threshold": 0.3
  }'
```


## 数据存储位置

默认情况下（不设置 `MEMORY_DATA_DIR` 或将其注释掉），所有本地存储数据统一存放在：

```text
~/.jiuwenmemory/memory_data/
├── sqlite_db.db              ← DB Store + KV Store (db模式) 共用的 SQLite 数据库
├── chroma.sqlite3            ← Chroma Vector Store 向量索引
└── (UUID 目录)               ← Chroma collection 数据（每个 scope 对应一个）
```

| 存储类型 | 默认位置 | 说明 |
|----------|----------|------|
| DB Store + KV Store (db模式) | `~/.jiuwenmemory/memory_data/sqlite_db.db` | 共用同一个 SQLite 文件 |
| Vector Store (Chroma) | `~/.jiuwenmemory/memory_data/` | Chroma 持久化目录 |
| KV Store (in_memory) | 进程内存 | 重启即丢失 |
| KV Store (shelve) | `~/.jiuwenmemory/memory_data/shelve_kv` | 本地文件 |

切换存储后端（如 PostgreSQL、Milvus、Elasticsearch）时，数据存储在对应服务端，不在本地。


## 注意事项

- `GET /health` 和 `GET /` 不需要鉴权；其他写入、删除、查询类接口在配置 `MEMORY_API_KEY` 后需要 Bearer Token。
- 未配置 `DB_URL` 时会使用本地 SQLite；未配置 `VECTOR_STORE_TYPE` 时会使用 Chroma，并将数据持久化到 `MEMORY_DATA_DIR`。
- `/add_messages/` 支持通过 `mem_variables` 指定变量定义，并通过 `enable_*` 开关控制各类记忆抽取。不传这些字段时默认开启所有记忆抽取、不抽取变量。
- `/search_memory/` 与 `/search_user_history_summary/` 返回的是服务层序列化后的结果，不直接暴露底层对象。
- `/get_user_mem_by_page/` 的 `total` 当前为本次响应列表长度，并不一定代表数据库中满足条件的全量总数。
- `/get_user_mem_by_page/` 的 `page_idx` 从 **1** 开始（非 0）。
