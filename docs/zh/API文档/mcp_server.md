# server.mcp_server

`server.mcp_server` 是 JiuwenMemory 提供的 **MCP（Model Context Protocol）服务入口**，基于 `mcp` 官方 SDK 的 `FastMCP` 将 `LongTermMemory` 的常用能力封装为 MCP 工具（tools），使任何兼容 MCP 的客户端（Claude Code、Codex、Cursor、VS Code ……）都能直接对长期记忆进行写入、检索、更新、删除和变量管理。

与 `server.memory_server`（FastAPI HTTP 服务）不同，MCP 服务**进程内**装配 `LongTermMemory` 引擎 —— 即 KV / DB / Vector / Embedding 的装配与 `memory_server` 启动时完全一致，但不再经过一层 HTTP 转发，而是由 MCP 进程直接持有引擎。客户端只需按 URL 连接即可调用工具，无需自建 HTTP 客户端代码。

该服务主要负责：

- 启动时（延迟到首次工具调用）根据 `.env` 自动装配 KV / DB / Vector Store 与嵌入模型；
- 注册 `LongTermMemory` 引擎配置；
- 将记忆的写入、检索、分页、更新、删除、变量管理等能力以 MCP 工具形式暴露；
- 提供常驻 Streamable HTTP / SSE 两种传输方式；
- 引擎装配失败时**不崩溃**：服务保持运行，每个工具返回可读的错误信息（与 mem0 的 `get_memory_client_safe` 同样的容错模式）。


## 启动方式

服务支持两种启动方式。

### 方式一：CLI 命令（安装后可用）

通过 `pip install -e '.[server]'` 安装后，可直接使用 `memory-mcp` 命令启动：

```bash
memory-mcp
```

该命令定义在 `pyproject.toml` 的 `[project.scripts]` 中：

```toml
[project.scripts]
memory-mcp = "jiuwen_memory.server.mcp_server:main"
```

### 方式二：源码运行

在项目根目录下运行：

```bash
python -m jiuwen_memory.server.mcp_server
```

> **依赖说明**：MCP 服务依赖 `mcp` 与 `uvicorn` 两个包，均包含在 `[server]` extras 中。若未安装会显式报错：`The 'mcp' package is required ... Install it with: pip install -e '.[server]'`。
>
> **配置说明**：大模型（`MODEL_PROVIDER`、`MODEL_NAME`、`API_KEY`、`API_BASE`）和嵌入模型（`EMBED_MODEL_NAME`、`EMBED_API_KEY`、`EMBED_API_BASE`）相关环境变量**必须手动配置**，默认值为空，未配置时引擎无法正常工作。存储、监听地址等其他配置项均有默认值，可按需覆盖。

两种方式效果完全一致。默认采用 Streamable HTTP 传输，启动后监听：

```text
http://127.0.0.1:8765/mcp
```

MCP 客户端按此 URL 连接即可。也可通过环境变量切换传输方式与监听地址：

```bash
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8765 memory-mcp
# 或 SSE 传输，客户端连接 http://127.0.0.1:8765/sse
MCP_TRANSPORT=sse MCP_HOST=127.0.0.1 MCP_PORT=8765 memory-mcp
```

> 与 `memory_server` 不同，MCP 服务**没有内置 Bearer Token 鉴权**。默认监听 `127.0.0.1` 仅本机可达；若将 `MCP_HOST` 改为 `0.0.0.0` 等对外地址，请自行在网络层（反向代理 / 防火墙）做好访问控制。


## 环境变量

服务启动时按优先级加载 `.env` 文件（与 `memory_server` 完全一致的加载链）：

1. **优先** `~/.jiuwenmemory/.env`
2. **其次** 当前工作目录下的 `.env`

如果两个路径都找不到 `.env`，服务会自动创建 `~/.jiuwenmemory/` 目录并提示用户配置（模板见 `server/.env.example`）。

> ⚠️ **注意**：`MEMORY_DATA_DIR` 等环境变量若设为空字符串（如 `MEMORY_DATA_DIR=`），不会触发默认值——`dotenv` 会将其读为 `""` 而非 `None`。要使用默认值，请**删掉该行或注释掉**（如 `# MEMORY_DATA_DIR=`）。

### MCP 服务配置

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `MCP_TRANSPORT` | `http` | 传输方式。`http` / `streamable-http` 为 Streamable HTTP（MCP spec 2025-03-26），客户端连 `http://<host>:<port>/mcp`；`sse` 为 SSE，客户端连 `http://<host>:<port>/sse`。其他值会启动失败。 |
| `MCP_HOST` | `127.0.0.1` | MCP 服务监听地址。 |
| `MCP_PORT` | `8765` | MCP 服务监听端口。与 `memory_server` 的 `IP`/`PORT` 互不干扰。 |
| `MCP_DEFAULT_USER_ID` | `__default__` | 工具参数 `user_id` 的默认值。**仅当变量未设置时**回退到引擎默认值 `__default__`；若设为空字符串（如模板中的 `MCP_DEFAULT_USER_ID=`）则实际值为 `""`，需删行或注释掉该行才使用默认值（见上文 dotenv 注意事项）。工具调用时仍可逐次覆盖。 |
| `MCP_DEFAULT_SCOPE_ID` | `__default__` | 工具参数 `scope_id` 的默认值。**仅当变量未设置时**回退到引擎默认值 `__default__`；空字符串的注意事项同上。工具调用时仍可逐次覆盖。 |

### 模型配置

与 `memory_server` 共用同一组变量，引擎装配时直接读取：

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

MCP 服务通过同一个 `server.store_factory` 装配存储后端，存储相关变量与 `memory_server` **完全相同**，详见 [memory_server 文档 · 存储配置](./memory_server.md#存储配置)。此处不再重复列表。


## 引擎装配与初始化流程

与 `memory_server` 在 FastAPI `startup_event` 中装配引擎不同，MCP 服务采用**延迟初始化**：

1. 进程启动时仅创建 `FastMCP` 实例并暴露工具，**不**装配 `LongTermMemory`；
2. 首次有工具被调用时，`_get_engine()` 在异步锁内执行 `_Engine.initialize()`：
   1. `create_async_engine_from_env()` 创建数据库连接；
   2. `create_kv_store(engine)` 创建 KV Store；
   3. `create_db_store(engine)` 创建 DB Store；
   4. `create_vector_store()` 创建 Vector Store；
   5. 根据 `EMBED_*` 创建 `APIEmbedding`；
   6. `LongTermMemory().register_store(...)` 注册存储和嵌入模型（若引擎已有 store 则跳过）；
   7. 根据 `MODEL_*` / `API_*` 创建 `MemoryEngineConfig` 并 `set_config(...)`；
3. 装配成功后引擎缓存为模块级单例，后续工具调用直接复用。

> **容错**：如果装配失败（缺依赖、`.env` 配置错误等），服务**不会退出**，而是记日志、保留未就绪的引擎单例；每次工具调用都会触发 `_get_engine()` 尝试重新装配，装配失败则该次调用返回 `{"error": "<action> failed: <exc>"}` 形式的 JSON 字符串。可用 `health_check` 工具探活。

`reset_engine()` 用于清空缓存的单例，使下一次工具调用重建引擎。主要用于测试场景（重写 `store_factory`、`LongTermMemory` 源后需要干净重建）；生产环境调用也安全——引擎会在下一次请求时延迟重建。


## 工具列表

所有工具的返回值均为 **JSON 字符串**（`ensure_ascii=False`）。`user_id` / `scope_id` 默认取 `MCP_DEFAULT_USER_ID` / `MCP_DEFAULT_SCOPE_ID`（未配置则为 `__default__`），每次调用均可覆盖。

> **记忆检索建议**：`search_memories` 与 `search_history_summaries` 每次应**成对调用**——前者检索画像/语义/情节等单条记忆，后者检索整段对话的历史摘要，两者结合才能得到关于用户/话题的最完整上下文。

### add_messages

将一组对话消息（每条为 `{role, content}` 字典）写入长期记忆。Jiuwen 没有纯消息存储——消息总是被抽取为记忆（profile / semantic / episodic / summary），因此单条用户消息即 `[{"role": "user", "content": "..."}]`。设 `infer=False` 可跳过 LLM 抽取、原样写入整批消息。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `messages` | `list[dict]` | 是 | - | 消息列表，每条含 `role`（缺失默认 `user`）和 `content`（缺失默认空串）。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |
| `infer` | `bool` | 否 | `true` | 是否进行 LLM 记忆抽取。`false` 时原样写入，不抽取。 |

> 与 `memory_server` 的 `/add_messages/` 不同：MCP 工具用单一 `infer` 开关替代了多个 `enable_*` 开关，也**不**支持 `mem_variables` 变量抽取定义（变量请用 `update_variables` 单独维护）。返回值也不是简单的 `success`，而是直接返回本次抽取得到的 `user_profile` / `semantic_memory` / `episodic_memory` / `summary` / `variables` 各部分。

**返回示例**（`infer=true`）：

```json
{
  "status": "added",
  "infer": true,
  "user_profile": [{"mem_id": "...", "content": "用户喜欢喝茉莉花茶", ...}],
  "semantic_memory": [...],
  "episodic_memory": [...],
  "summary": "用户提到自己喜欢茉莉花茶。",
  "variables": {}
}
```

### search_memories

对用户记忆（profile / semantic / episodic）做语义检索。需要回忆“我对某个用户/话题了解什么”时调用，并**每次都与 `search_history_summaries` 配对**以获得最完整上下文。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | `str` | 是 | - | 搜索查询。 |
| `num` | `int` | 否 | `5` | 返回结果数量。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |
| `threshold` | `float` | 否 | `0.3` | 相似度阈值。 |

**返回示例**：

```json
{
  "results": [
    {"mem_id": "mem_123", "content": "用户喜欢喝茉莉花茶", "type": "user_profile", "score": 0.86}
  ],
  "count": 1
}
```

### search_history_summaries

检索历史对话摘要——比单条记忆更高层：摘要捕获整段对话（讨论了哪些话题、得出了什么结论）。需要上下文时**与 `search_memories` 成对调用**，两者结合才能得到对用户/话题最完整的回忆。

**参数**：与 `search_memories` 相同（`query` 必填，`num` 默认 `3`，`user_id` / `scope_id` / `threshold` 同上）。

**返回示例**：

```json
{
  "results": [
    {"mem_id": "summary_123", "content": "用户最近提到自己喜欢茉莉花茶。", "type": "summary", "score": 0.78}
  ],
  "count": 1
}
```

### get_memories

分页列出记忆（最新在前），可按类型过滤。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `page_size` | `int` | 否 | `10` | 每页数量。 |
| `page_idx` | `int` | 否 | `1` | 页码，从 **1** 开始。 |
| `memory_type` | `str` | 否 | `unknown` | 记忆类型，对应 `MemoryType` 枚举：`unknown`（全部）/ `user_profile` / `semantic_memory` / `episodic_memory` / `summary` / `variable` / `middle_term_memory`。无法识别的值回退为 `UNKNOWN`。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |

**返回示例**：

```json
{
  "results": [
    {"mem_id": "mem_123", "content": "用户喜欢喝茉莉花茶", "type": "user_profile", "timestamp": "2026-07-06T10:00:00"}
  ],
  "count": 1,
  "page_idx": 1
}
```

### update_memory

按 `mem_id` 覆盖某条记忆的文本内容。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `mem_id` | `str` | 是 | - | 待更新的记忆 ID。 |
| `memory` | `str` | 是 | - | 更新后的记忆内容。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |

**返回示例**：

```json
{"status": "updated", "mem_id": "mem_123"}
```

### delete_memory

按 `mem_id` 删除单条记忆。

**参数**：`mem_id`（必填）、`user_id`、`scope_id`（后两者均可选，默认值同上）。

**返回示例**：

```json
{"status": "deleted", "mem_id": "mem_123"}
```

### delete_all_memories

删除指定 `scope_id` 下的**所有**记忆（全部类型）。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 待清空的作用域 ID。 |

**返回示例**：

```json
{"status": "deleted", "scope_id": "demo"}
```

### get_variables

读取用户变量。省略 `names` 时返回全部变量。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `names` | `list[str] \| null` | 否 | `null` | 指定变量名列表；为 `null` 时返回全部。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |

**返回示例**：

```json
{"variables": {"favorite_drink": "茉莉花茶"}}
```

### update_variables

设置/更新一个或多个用户变量（`name -> value`）。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `variables` | `dict` | 是 | - | 变量名到变量值的映射。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |

**返回示例**：

```json
{"status": "updated", "variables": {"favorite_drink": "茉莉花茶"}}
```

### delete_variables

按变量名删除一个或多个用户变量。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `names` | `list[str]` | 是 | - | 待删除的变量名列表。 |
| `user_id` | `str` | 否 | `MCP_DEFAULT_USER_ID` | 用户 ID。 |
| `scope_id` | `str` | 否 | `MCP_DEFAULT_SCOPE_ID` | 作用域 ID。 |

**返回示例**：

```json
{"status": "deleted", "deleted": ["favorite_drink", "city"], "names": ["favorite_drink", "city"]}
```

> `deleted` 字段透传 `LongTermMemory.delete_variables(...)` 的返回值，实际结构取决于底层实现。

### health_check

报告引擎就绪状态，用于排查初始化失败。会触发 `_get_engine()`，因此也能驱动延迟装配。

**参数**：无。

**返回示例**（就绪）：

```json
{"status": "healthy", "ready": true}
```

装配失败时：

```json
{"status": "unavailable", "error": "<具体错误信息>"}
```


## 错误响应

工具执行过程中抛出的异常**不会**让服务崩溃，而是被捕获并序列化为 JSON 字符串返回（HTTP 状态码仍为 200，错误在响应体内）：

```json
{"error": "<action> failed: <exc>"}
```

其中 `<action>` 为工具名，例如：

- `add_messages failed: ...`
- `search_memories failed: ...`
- `update_memory failed: ...`
- `delete_variables failed: ...`

同时会通过 `memory_logger.exception(...)` 记录完整堆栈。


## 客户端配置示例

### Claude Code

在项目根目录或 `~/.claude.json` 中加入：

```json
{
  "mcpServers": {
    "jiuwen-memory": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

> Streamable HTTP 传输用 `url` 字段；若使用 SSE 传输则填 `http://127.0.0.1:8765/sse`，并按客户端要求改用 `url` 或对应的 `type: "sse"` 配置。

### Cursor / 通用 MCP 客户端

通用做法是指向 MCP 端点 URL：

- **Streamable HTTP**：`http://<MCP_HOST>:<MCP_PORT>/mcp`
- **SSE**：`http://<MCP_HOST>:<MCP_PORT>/sse`

具体字段名以各客户端文档为准。


## 最小使用示例

### 1. 启动服务

CLI 命令方式：

```bash
MODEL_PROVIDER="xxxx" \
MODEL_NAME="xxxx" \
API_KEY="xxxx" \
API_BASE="xxxx" \
EMBED_MODEL_NAME="xxxx" \
EMBED_API_KEY="xxxx" \
EMBED_API_BASE="xxxx" \
memory-mcp
```

源码运行方式：

```bash
MODEL_PROVIDER="xxxx" \
MODEL_NAME="xxxx" \
API_KEY="xxxx" \
API_BASE="xxxx" \
EMBED_MODEL_NAME="xxxx" \
EMBED_API_KEY="xxxx" \
EMBED_API_BASE="xxxx" \
python -m jiuwen_memory.server.mcp_server
```

> 更推荐的方式是在 `~/.jiuwenmemory/.env` 中配置好所有环境变量，然后直接运行 `memory-mcp`。上方示例中的 `xxxx` 需替换为你实际使用的 LLM/Embedding 提供方的配置值。

### 2. 连接客户端并调用工具

启动后，在 MCP 客户端中即可看到 `add_messages`、`search_memories`、`search_history_summaries` 等工具。例如让客户端“记住用户喜欢茉莉花茶”：

```
调用 add_messages，传入：
  messages = [{"role": "user", "content": "我喜欢喝茉莉花茶"}]
  user_id  = "user_001"
  scope_id = "demo"
```

随后检索：

```
调用 search_memories，query="用户喜欢喝什么？", user_id="user_001", scope_id="demo"
```

引擎在首次工具调用时完成延迟装配，因此首次调用可能略慢。


## 与 memory_server 的差异

| 维度 | `server.memory_server` | `server.mcp_server` |
|---|---|---|
| 协议 | FastAPI REST（HTTP/JSON） | MCP（Streamable HTTP / SSE） |
| 引擎装配时机 | 启动时同步装配，失败则进程退出 | 首次工具调用时延迟装配，失败不退出 |
| 调用方 | 任意 HTTP 客户端（curl / 后端服务） | 兼容 MCP 的客户端（Claude Code / Cursor / Codex …） |
| 鉴权 | 内置 Bearer Token（`MEMORY_API_KEY`） | 无内置鉴权，需网络层兜底 |
| 消息写入参数 | `mem_variables` + 多个 `enable_*` 开关 | 单一 `infer` 开关，不支持变量抽取定义 |
| 消息写入返回 | `{"status": "success", ...}` | 直接返回抽取出的各部分记忆 |
| 监听配置 | `IP` / `PORT` | `MCP_HOST` / `MCP_PORT`（默认 8765） |
| 传输方式 | 仅 HTTP | Streamable HTTP / SSE |
| 默认 user/scope | 引擎默认值 `__default__` | `MCP_DEFAULT_USER_ID` / `MCP_DEFAULT_SCOPE_ID`（可配置，默认 `__default__`） |

两者共享同一份 `.env`、同一组存储后端与模型配置，可同时运行（注意端口分离：HTTP 服务默认 8000，MCP 默认 8765）。


## 注意事项

- MCP 服务依赖 `mcp` 与 `uvicorn`，需通过 `pip install -e '.[server]'` 安装；缺失时启动会显式报错。
- 引擎延迟装配：首次工具调用才建引擎，首次调用耗时较高；装配失败服务不退出，可用 `health_check` 探活。
- `search_memories` 与 `search_history_summaries` 应成对调用以获得最完整上下文（见各工具描述）。
- `get_memories` 的 `page_idx` 从 **1** 开始；返回的 `count` 为本次响应列表长度，不一定代表全量总数。
- MCP 服务无内置鉴权，对外暴露时务必在网络层做好访问控制。
- `add_messages` 工具用 `infer` 开关替代了 HTTP 服务的 `enable_*`，且不支持 `mem_variables`；变量维护请使用 `get_variables` / `update_variables` / `delete_variables`。
- `user_id` / `scope_id` 的默认值由 `MCP_DEFAULT_USER_ID` / `MCP_DEFAULT_SCOPE_ID` 控制，仅当变量未设置时回退引擎默认值 `__default__`（注意 dotenv 下空字符串不等于未设置，需删行或注释才使用默认值），每次调用仍可覆盖。
