# server.mcp_server

`server.mcp_server` is the **MCP (Model Context Protocol) service entry point** provided by JiuwenMemory. It uses the official `mcp` SDK's `FastMCP` to expose common `LongTermMemory` capabilities as MCP tools, so any MCP-compatible client (Claude Code, Codex, Cursor, VS Code, …) can write, search, update, delete, and manage long-term memory directly.

Unlike `server.memory_server` (a FastAPI HTTP service), the MCP service assembles the `LongTermMemory` engine **in-process** — the KV / DB / Vector / Embedding assembly is identical to `memory_server` startup, but there is no HTTP layer to go through: the MCP process owns the engine directly. Clients just connect by URL and call tools, with no need to hand-roll an HTTP client.

The service is responsible for:

- assembling KV / DB / Vector Store backends and the embedding model from `.env` (lazily, on the first tool call);
- registering the `LongTermMemory` engine configuration;
- exposing memory write, search, pagination, update, delete, and variable management as MCP tools;
- providing both persistent Streamable HTTP and SSE transports;
- **not crashing** on engine-assembly failure: the server stays up and each tool returns a readable error (the same resilient pattern mem0's `get_memory_client_safe` uses).


## Startup

The service supports two startup methods.

### Method 1: CLI command (available after installation)

After installing with `pip install -e '.[server]'`, you can start it directly with the `memory-mcp` command:

```bash
memory-mcp
```

This command is defined in `pyproject.toml` under `[project.scripts]`:

```toml
[project.scripts]
memory-mcp = "jiuwen_memory.server.mcp_server:main"
```

### Method 2: Source code

Run from the project root:

```bash
python -m jiuwen_memory.server.mcp_server
```

> **Dependencies**: The MCP service depends on the `mcp` and `uvicorn` packages, both included in the `[server]` extras. If missing, startup fails explicitly: `The 'mcp' package is required ... Install it with: pip install -e '.[server]'`.
>
> **Configuration note**: LLM-related variables (`MODEL_PROVIDER`, `MODEL_NAME`, `API_KEY`, `API_BASE`) and embedding-related variables (`EMBED_MODEL_NAME`, `EMBED_API_KEY`, `EMBED_API_BASE`) **must be manually configured** — their defaults are empty and the engine will not function without them. Storage, bind address, and other settings have working defaults and can be overridden as needed.

Both methods are functionally identical. The default transport is Streamable HTTP, listening on:

```text
http://127.0.0.1:8765/mcp
```

MCP clients connect by this URL. You can switch the transport and bind address with environment variables:

```bash
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8765 memory-mcp
# or SSE transport; clients connect to http://127.0.0.1:8765/sse
MCP_TRANSPORT=sse MCP_HOST=127.0.0.1 MCP_PORT=8765 memory-mcp
```

> Unlike `memory_server`, the MCP service has **no built-in Bearer Token authentication**. The default `127.0.0.1` is only reachable from localhost; if you change `MCP_HOST` to `0.0.0.0` or another externally reachable address, enforce access control yourself at the network layer (reverse proxy / firewall).


## Environment Variables

On startup, the service loads `.env` files in the same priority chain as `memory_server`:

1. **First**: `~/.jiuwenmemory/.env`
2. **Second**: `.env` in the current working directory

If neither `.env` file is found, the service automatically creates the `~/.jiuwenmemory/` directory and prompts the user to configure it (a template lives in `server/.env.example`).

> ⚠️ **Important**: Setting an environment variable to an empty string (e.g., `MEMORY_DATA_DIR=`) will **not** trigger the default value — `dotenv` reads it as `""` instead of `None`. To use the default value, **delete the line or comment it out** (e.g., `# MEMORY_DATA_DIR=`).

### MCP service configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `http` | Transport. `http` / `streamable-http` = Streamable HTTP (MCP spec 2025-03-26), clients connect to `http://<host>:<port>/mcp`; `sse` = SSE, clients connect to `http://<host>:<port>/sse`. Any other value fails startup. |
| `MCP_HOST` | `127.0.0.1` | MCP service bind address. |
| `MCP_PORT` | `8765` | MCP service port. Independent of `memory_server`'s `IP`/`PORT`. |
| `MCP_DEFAULT_USER_ID` | `__default__` | Default value for the `user_id` tool parameter. Falls back to the engine default `__default__` **only when the variable is unset**; an empty string (e.g. the template's `MCP_DEFAULT_USER_ID=`) is read as `""`, not the default — delete or comment out the line to use the default (see the dotenv note above). Can still be overridden per call. |
| `MCP_DEFAULT_SCOPE_ID` | `__default__` | Default value for the `scope_id` tool parameter. Falls back to the engine default `__default__` **only when the variable is unset**; same empty-string caveat as above. Can still be overridden per call. |

### Model configuration

The engine reads the same set of variables as `memory_server` during assembly:

| Variable | Default | Description |
|---|---|---|
| `MODEL_NAME` | empty string | LLM model name used for memory generation. Must be configured in `.env`. |
| `MODEL_PROVIDER` | empty string | LLM client provider. Must be configured in `.env`. |
| `API_KEY` | empty string | LLM API key. |
| `API_BASE` | empty string | LLM API base URL. |
| `EMBED_MODEL_NAME` | empty string | Embedding model name. Must be configured in `.env`. |
| `EMBED_API_KEY` | empty string | Embedding API key. Must be configured in `.env`. |
| `EMBED_API_BASE` | empty string | Embedding API endpoint. Must be configured in `.env`. |

### Storage configuration

The MCP service assembles storage backends through the same `server.store_factory`, so the storage variables are **identical to `memory_server`** — see the [memory_server docs · Storage configuration](./memory_server.md#storage-configuration). They are not repeated here.


## Engine Assembly & Initialization

Unlike `memory_server`, which assembles the engine in the FastAPI `startup_event`, the MCP service uses **lazy initialization**:

1. At process startup it only creates the `FastMCP` instance and exposes tools; it does **not** assemble `LongTermMemory`;
2. On the first tool call, `_get_engine()` runs `_Engine.initialize()` under an async lock:
   1. `create_async_engine_from_env()` creates the database engine;
   2. `create_kv_store(engine)` creates the KV Store;
   3. `create_db_store(engine)` creates the DB Store;
   4. `create_vector_store()` creates the Vector Store;
   5. `APIEmbedding` is created from the `EMBED_*` variables;
   6. `LongTermMemory().register_store(...)` registers the stores and embedding model (skipped if the engine already has stores);
   7. `MemoryEngineConfig` is built from `MODEL_*` / `API_*` and applied via `set_config(...)`;
3. On success the engine is cached as a module-level singleton and reused by subsequent tool calls.

> **Resilience**: If assembly fails (missing deps, bad `.env`, …), the service **does not exit** — it logs, keeps a not-ready singleton, and `_get_engine()` retries assembly on every tool call. A failed call returns a JSON string of the form `{"error": "<action> failed: <exc>"}`. Use the `health_check` tool to probe readiness.

`reset_engine()` clears the cached singleton so the engine is rebuilt on the next tool call. Intended for tests that re-wire the engine's dependencies (the `LongTermMemory` source, the `store_factory` functions) and need a fresh build for isolation. Safe in production — the engine is simply rebuilt lazily on the next request.


## Tool List

All tools return **JSON strings** (`ensure_ascii=False`). `user_id` / `scope_id` default to `MCP_DEFAULT_USER_ID` / `MCP_DEFAULT_SCOPE_ID` (or `__default__` if unset); both can be overridden per call.

> **Recall guidance**: call `search_memories` and `search_history_summaries` **together every time** — the former recalls individual profile/semantic/episodic memories, the latter recalls whole-conversation history summaries; combining both gives the fullest context about the user/topic.

### add_messages

Adds a list of conversation messages (each a `{role, content}` dict) to long-term memory. Jiuwen has no plain message store — messages are always extracted into memories (profile / semantic / episodic / summary), so a single user message is just `[{"role": "user", "content": "..."}]`. Set `infer=False` to skip LLM extraction and ingest the batch raw.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `messages` | `list[dict]` | Yes | - | Message list. Each item contains `role` (defaults to `user`) and `content` (defaults to empty string). |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |
| `infer` | `bool` | No | `true` | Whether to run LLM memory extraction. `false` ingests the batch raw without extraction. |

> Unlike `memory_server`'s `/add_messages/`, the MCP tool replaces the multiple `enable_*` switches with a single `infer` flag and does **not** support `mem_variables` (maintain variables via `update_variables` instead). The return value is not a simple `success` — it directly returns the `user_profile` / `semantic_memory` / `episodic_memory` / `summary` / `variables` produced by this extraction.

**Return example** (`infer=true`):

```json
{
  "status": "added",
  "infer": true,
  "user_profile": [{"mem_id": "...", "content": "The user likes jasmine tea", ...}],
  "semantic_memory": [...],
  "episodic_memory": [...],
  "summary": "The user mentioned they like jasmine tea.",
  "variables": {}
}
```

### search_memories

Semantic search across user memories (profile / semantic / episodic). Call this whenever you need to recall what you know about a user or topic — and **pair it with `search_history_summaries` every time** for the fullest context.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | `str` | Yes | - | Search query. |
| `num` | `int` | No | `5` | Number of results to return. |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |
| `threshold` | `float` | No | `0.3` | Similarity threshold. |

**Return example**:

```json
{
  "results": [
    {"mem_id": "mem_123", "content": "The user likes jasmine tea", "type": "user_profile", "score": 0.86}
  ],
  "count": 1
}
```

### search_history_summaries

Search past conversation summaries — higher-level than individual memories: summaries capture whole conversations (topics discussed and conclusions reached). Call this **together with `search_memories` every time** you need context; searching both gives the fullest recall of what you know about the user/topic.

**Parameters**: same as `search_memories` (`query` required, `num` defaults to `3`, `user_id` / `scope_id` / `threshold` as above).

**Return example**:

```json
{
  "results": [
    {"mem_id": "summary_123", "content": "The user recently mentioned that they like jasmine tea.", "type": "summary", "score": 0.78}
  ],
  "count": 1
}
```

### get_memories

List memories page by page (newest first), optionally filtered by type.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `page_size` | `int` | No | `10` | Page size. |
| `page_idx` | `int` | No | `1` | Page index, starting from **1**. |
| `memory_type` | `str` | No | `unknown` | Memory type, matching the `MemoryType` enum: `unknown` (all) / `user_profile` / `semantic_memory` / `episodic_memory` / `summary` / `variable` / `middle_term_memory`. Unrecognized values fall back to `UNKNOWN`. |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |

**Return example**:

```json
{
  "results": [
    {"mem_id": "mem_123", "content": "The user likes jasmine tea", "type": "user_profile", "timestamp": "2026-07-06T10:00:00"}
  ],
  "count": 1,
  "page_idx": 1
}
```

### update_memory

Overwrite a memory's text by its `mem_id`.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `mem_id` | `str` | Yes | - | Memory ID to update. |
| `memory` | `str` | Yes | - | Updated memory content. |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |

**Return example**:

```json
{"status": "updated", "mem_id": "mem_123"}
```

### delete_memory

Delete a single memory by its `mem_id`.

**Parameters**: `mem_id` (required), `user_id`, `scope_id` (both optional, defaults as above).

**Return example**:

```json
{"status": "deleted", "mem_id": "mem_123"}
```

### delete_all_memories

Delete **all** memories (every type) within a scope.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID to clear. |

**Return example**:

```json
{"status": "deleted", "scope_id": "demo"}
```

### get_variables

Read user variables. Omit `names` to return all of them.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `names` | `list[str] \| null` | No | `null` | Variable names to query; `null` returns all. |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |

**Return example**:

```json
{"variables": {"favorite_drink": "jasmine tea"}}
```

### update_variables

Set/update one or more user variables (`name -> value`).

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `variables` | `dict` | Yes | - | Mapping from variable names to values. |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |

**Return example**:

```json
{"status": "updated", "variables": {"favorite_drink": "jasmine tea"}}
```

### delete_variables

Delete one or more user variables by name.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `names` | `list[str]` | Yes | - | Variable names to delete. |
| `user_id` | `str` | No | `MCP_DEFAULT_USER_ID` | User ID. |
| `scope_id` | `str` | No | `MCP_DEFAULT_SCOPE_ID` | Scope ID. |

**Return example**:

```json
{"status": "deleted", "deleted": ["favorite_drink", "city"], "names": ["favorite_drink", "city"]}
```

> The `deleted` field passes through the return value of `LongTermMemory.delete_variables(...)`. Its actual structure depends on the underlying implementation.

### health_check

Report engine readiness — useful for diagnosing init failures. Triggers `_get_engine()`, so it also drives lazy assembly.

**Parameters**: none.

**Return example** (ready):

```json
{"status": "healthy", "ready": true}
```

When assembly fails:

```json
{"status": "unavailable", "error": "<specific error message>"}
```


## Error Responses

Exceptions thrown during a tool call **do not** crash the service. They are caught and serialized into a JSON string returned in the response body (the HTTP status is still 200; the error is in the body):

```json
{"error": "<action> failed: <exc>"}
```

where `<action>` is the tool name, e.g.:

- `add_messages failed: ...`
- `search_memories failed: ...`
- `update_memory failed: ...`
- `delete_variables failed: ...`

The full stack trace is logged via `memory_logger.exception(...)`.


## Client Configuration Examples

### Claude Code

Add this to your project root or `~/.claude.json`:

```json
{
  "mcpServers": {
    "jiuwen-memory": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

> Use the `url` field for Streamable HTTP; for SSE transport, point it at `http://127.0.0.1:8765/sse` and follow the client's `type: "sse"` convention if required.

### Cursor / generic MCP clients

The generic approach is to point the client at the MCP endpoint URL:

- **Streamable HTTP**: `http://<MCP_HOST>:<MCP_PORT>/mcp`
- **SSE**: `http://<MCP_HOST>:<MCP_PORT>/sse`

The exact field name depends on each client's documentation.


## Minimal Example

### 1. Start the service

CLI command method:

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

Source code method:

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

> The recommended approach is to configure all environment variables in `~/.jiuwenmemory/.env`, then simply run `memory-mcp`. Replace `xxxx` in the examples above with the actual configuration values from your LLM/Embedding provider.

### 2. Connect a client and call tools

Once running, the client surfaces `add_messages`, `search_memories`, `search_history_summaries`, and the other tools. For example, to have the client "remember the user likes jasmine tea":

```
Call add_messages with:
  messages = [{"role": "user", "content": "I like jasmine tea"}]
  user_id  = "user_001"
  scope_id = "demo"
```

Then search:

```
Call search_memories with query="What does the user like to drink?", user_id="user_001", scope_id="demo"
```

The engine completes lazy assembly on the first tool call, so the first call may be slightly slower.


## Differences from memory_server

| Aspect | `server.memory_server` | `server.mcp_server` |
|---|---|---|
| Protocol | FastAPI REST (HTTP/JSON) | MCP (Streamable HTTP / SSE) |
| Engine assembly timing | Synchronously at startup; failure exits the process | Lazily on the first tool call; failure does not exit |
| Caller | Any HTTP client (curl / backend service) | MCP-compatible client (Claude Code / Cursor / Codex …) |
| Auth | Built-in Bearer token (`MEMORY_API_KEY`) | No built-in auth; rely on the network layer |
| Add-messages params | `mem_variables` + multiple `enable_*` switches | Single `infer` switch; no variable-extraction definitions |
| Add-messages return | `{"status": "success", ...}` | Directly returns the extracted memory parts |
| Bind config | `IP` / `PORT` | `MCP_HOST` / `MCP_PORT` (default 8765) |
| Transport | HTTP only | Streamable HTTP / SSE |
| Default user/scope | Engine default `__default__` | `MCP_DEFAULT_USER_ID` / `MCP_DEFAULT_SCOPE_ID` (configurable, default `__default__`) |

Both share the same `.env`, the same storage backends, and the same model configuration, and can run simultaneously (note the port split: HTTP service defaults to 8000, MCP to 8765).


## Notes

- The MCP service depends on `mcp` and `uvicorn`; install via `pip install -e '.[server]'`. Missing deps fail startup explicitly.
- Lazy engine assembly: the engine is built on the first tool call, so the first call is slower; assembly failure does not exit the service — probe with `health_check`.
- Call `search_memories` and `search_history_summaries` together for the fullest context (see each tool's description).
- `get_memories`'s `page_idx` starts from **1**; the returned `count` is the length of this response's list, not necessarily the total number of matching records.
- The MCP service has no built-in authentication; enforce access control at the network layer when exposing it externally.
- `add_messages` uses the `infer` switch instead of the HTTP service's `enable_*` and does not support `mem_variables`; manage variables via `get_variables` / `update_variables` / `delete_variables`.
- The defaults for `user_id` / `scope_id` are controlled by `MCP_DEFAULT_USER_ID` / `MCP_DEFAULT_SCOPE_ID`; they fall back to the engine default `__default__` only when unset (note: under dotenv an empty string is not the same as unset — delete or comment out the line to use the default), and both can still be overridden per call.
