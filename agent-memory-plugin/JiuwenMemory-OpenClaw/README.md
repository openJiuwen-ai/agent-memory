# agent-memory-plugin

## 简介
这是一个最小可用的 OpenClaw lifecycle 插件，功能是：
- **添加记忆**：在每轮对话结束后把消息存储到 agent-memory 服务中。
- **召回记忆**：每轮对话开始前从 agent-memory 服务中召回相关记忆并注入上下文。

## 安装部署

### 1. 按照官网指导安装 openclaw
[openclaw 官网](https://github.com/openclaw/openclaw)

### 2. 克隆并安装本插件
在终端依次执行以下三条命令：

```bash
git clone https://gitcode.com/openJiuwen/agent-memory.git
cd agent-memory/agent-memory-plugin/JiuwenMemory-OpenClaw
openclaw plugins install .
```

### 3. 本地启动记忆服务

记忆服务是一个基于 FastAPI 的 Python 服务，需要先在本地跑起来，插件才能读写记忆。

**3.1 安装依赖**
```bash
# 安装本仓库（JiuwenMemory）并带上 SQLite + ChromaDB 存储后端
pip install -e .[sqlite,chromadb]
# 记忆服务额外依赖的 Web 框架
pip install fastapi uvicorn
```

**3.2 配置 `.env`**
在 `server/` 目录下创建 `.env` 文件（可参考同目录的 `server/.env.example`），必须配置大模型和 Embedding 模型，其他按需配置：

| 变量 | 说明 |
| --- | --- |
| `API_BASE` | 大模型服务提供商的 API 地址 |
| `MODEL_NAME` | 大模型调用名称 |
| `API_KEY` | 大模型的 API Key |
| `MODEL_PROVIDER` | 大模型 API 协议，如 `OpenAI`、`SiliconFlow` |
| `EMBED_API_BASE` | Embedding 模型服务地址 |
| `EMBED_MODEL_NAME` | Embedding 模型调用名称 |
| `EMBED_API_KEY` | Embedding 模型的 API Key |
| `MEMORY_DATA_DIR` | 记忆数据文件保存目录，默认 `./memory_data` |
| `IP` | 记忆服务监听 IP，默认 `127.0.0.1`（仅本机访问；对外暴露需改 `0.0.0.0` 并设置 `MEMORY_API_KEY`） |
| `PORT` | 记忆服务监听端口，默认 `8000` |
| `MEMORY_API_KEY` | API 鉴权 Key，留空则不鉴权（仅限本地）；非本机部署时必填，插件需在 `openclaw.json` 的 `apiKey` 配同一值 |

**3.3 选择存储后端（可选）**

记忆服务的 KV / DB / Vector 三类存储都可以通过 `.env` 切换实现，留空则全部走默认（SQLite + Chroma），首次跑通建议保持默认。

通用：

| 变量 | 说明 |
| --- | --- |
| `DB_URL` | SQLAlchemy 异步连接 URL，KV(db 模式) 与 DB store 共用；留空则回退到 `sqlite+aiosqlite:///${MEMORY_DATA_DIR}/sqlite_db.db` |

KV Store：

| 变量 | 说明 |
| --- | --- |
| `KV_STORE_TYPE` | `db`（默认，复用 `DB_URL`）/ `in_memory`（进程内，不持久化）/ `shelve`（本地文件） |
| `KV_SHELVE_PATH` | 仅 `KV_STORE_TYPE=shelve` 时生效，留空默认 `${MEMORY_DATA_DIR}/shelve_kv` |

DB Store：

| 变量 | 说明 |
| --- | --- |
| `DB_STORE_TYPE` | `default`（默认）/ `gauss`（GaussDB，需在 `DB_URL` 配 GaussDB 连接串） |

Vector Store：

| 变量 | 说明 |
| --- | --- |
| `VECTOR_STORE_TYPE` | `chroma`（默认）/ `milvus` / `elasticsearch` / `gauss` |
| `VECTOR_CHROMA_PERSIST_DIR` | 仅 chroma；留空默认 `${MEMORY_DATA_DIR}` |
| `VECTOR_MILVUS_URI` / `VECTOR_MILVUS_TOKEN` / `VECTOR_MILVUS_DATABASE` | 仅 milvus；URI 形如 `http://localhost:19530`，DATABASE 默认 `default` |
| `VECTOR_ES_HOSTS` / `VECTOR_ES_INDEX_PREFIX` | 仅 elasticsearch；HOSTS 逗号分隔，例如 `http://localhost:9200`，前缀默认 `agent_vector` |
| `VECTOR_GAUSS_HOST` / `VECTOR_GAUSS_PORT` / `VECTOR_GAUSS_DATABASE` / `VECTOR_GAUSS_USER` / `VECTOR_GAUSS_PASSWORD` | 仅 gauss 向量库；默认 `localhost:5432`、库名 `postgres`、用户 `postgres` |

> 第三方依赖（pymilvus / elasticsearch / psycopg2 等）按需懒导入，只在切到对应后端时才需要安装；停留在默认 chroma 不受影响。

**3.4 启动记忆服务**
在仓库根目录下执行：
```bash
python -m server.memory_server
```
看到提示 `Memory engine initialized successfully` 即启动成功。

### 4. 修改 openclaw.json 配置

openclaw.json 的默认路径一般为：
```
C:/Users/用户名/.openclaw/openclaw.json
```

需要在 `plugins.entries.openjiuwen-memory-index` 这一段中确认/修改以下内容：

- **`hooks.allowConversationAccess`**：必须设为 `true`。  
  OpenClaw 默认对非内置插件屏蔽 `agent_end` 等会话类 hook，不开启的话**只有"召回记忆"能工作，"添加记忆"不会触发**。

- **`config.baseUrl`**：改成记忆服务的地址  
  本地部署默认为：`http://127.0.0.1:8000`  
  如果第 3.3 步修改了 `IP`/`PORT`，这里要对应改。

- **`config.userId` / `config.scopeId`**：可任意命名，用于不同用户/会话间的记忆数据隔离。

完整配置片段示例：
```json
"plugins": {
  "entries": {
    "openjiuwen-memory-index": {
      "enabled": true,
      "hooks": {
        "allowConversationAccess": true
      },
      "config": {
        "baseUrl": "http://127.0.0.1:8000",
        "apiKey": "",
        "userId": "openclaw-user",
        "scopeId": "openclaw-scope",
        "scopeIdPrefix": "",
        "scopeIdSuffix": "",
        "scopeSuffixMode": "none",
        "resetOnNew": true,
        "searchEnabled": true,
        "addEnabled": true,
        "captureStrategy": "last_turn",
        "maxMessageChars": 20000,
        "includeAssistant": true,
        "threshold": 0.3,
        "num": 10,
        "recallGlobal": true,
        "timeoutMs": 30000,
        "retries": 1,
        "throttleMs": 1000
      }
    }
  }
}
```

### 5. 重启 OpenClaw
在终端执行以下命令使配置生效：

```bash
openclaw gateway restart
```

## 验证插件是否生效

重启后开启一轮新对话，记忆服务的日志中应该能看到这两条接口被调用：
- `POST /search_memory/`（每轮对话开始前的记忆召回）
- `POST /add_messages/`（每轮对话结束后的记忆写入）

如果只看到 `search_memory` 而没有 `add_messages`，请检查 `hooks.allowConversationAccess` 是否已设为 `true`。
