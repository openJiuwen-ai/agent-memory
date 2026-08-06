# agent-memory docker compose 部署

使用 docker compose 将 agent-memory 接入真实后端，作为常驻 HTTP 服务运行。

本目录维护三套相互独立的部署文件：

| 模式 | Compose | 镜像 | 配置 | 环境变量示例 |
|---|---|---|---|---|
| 在线模型 | `online/docker-compose.yml` | `online/Dockerfile` | `online/config.yml` | `online/.env.example` |
| 本地模型 | `local/docker-compose.yml` | `local/Dockerfile` | `local/config.yml` | `local/.env.example` |
| PostgreSQL | `postgres/docker-compose.yml` | `postgres/Dockerfile` | `postgres/config.yml` | `postgres/.env.example` |

## 选型

| 角色 | 实现 | 形态 |
|---|---|---|
| LLM | DashScope OpenAI-compatible API（默认关闭思考） | 外部，凭 `LLM_API_KEY` |
| 嵌入 | `bge-m3`（1024 维） | 在线模型：HTTP `/v1/embeddings`；本地模型：进程内 FlagEmbedding |
| 精排 | `bge-reranker-v2-m3` | 在线模型：HTTP `/rerank`；本地模型：进程内 FlagEmbedding |
| 向量召回 | Milvus；PostgreSQL profile 改用 pgvector | 容器 |
| 关键词召回 | Elasticsearch（BM25） | 容器 |
| 真源 | Redis；PostgreSQL profile 改用 PostgreSQL KVStore | 容器 |

召回采用「向量 + 关键词」双通道，图召回关闭。对外暴露 `POST /v1/<verb>` 的 HTTP 接口，
单个内核实例随进程生命周期常驻，跨请求保持状态。

## 前置条件

- Docker + docker compose v2。
- 分配给 Docker 的内存 **≥ 20 GB**（若默认配额偏低，请在 Docker Desktop 设置中上调）。
  全栈常驻约占用 13–18 GB。
- 网络可达：拉取镜像、访问云端 LLM 端点；在线模型模式还需能访问 `EMBEDDER_BASE_URL` /
  `RERANKER_BASE_URL`。
- PostgreSQL 模式会拉取 PostgreSQL 16 + pgvector 0.8.3 镜像，并在首次创建数据卷时自动执行
  [`scripts/pg_schema.sql`](../../scripts/pg_schema.sql)。
- 本地模型模式需通过 ModelScope 下载 bge 模型（约 4 GB，详见「预下载模型」）。
- 无 GPU：本地模型模式下嵌入/精排走 CPU 推理，功能可用，但批量写入与高并发场景下吞吐受限。

## 预下载模型（仅本地模型模式必需）

嵌入/精排使用本地 bge 模型，**不在容器启动时联网下载**。直连 HuggingFace 在部分网络环境下
并不稳定，且下载失败时索引构建会静默降级（捕获嵌入异常后记录日志并继续），导致写入看似
成功、召回却为空。建议在宿主机预先通过 ModelScope 下载至 `deploy/docker/models`，再由
compose 挂载到容器 `/models-local` 离线加载。

```bash
cd deploy/docker/local
./download-models.sh
# 等价手动步骤：
#   pip install modelscope
#   modelscope download --model BAAI/bge-m3             --local_dir ../models/bge-m3
#   modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir ../models/bge-reranker-v2-m3
```

下载完成后，`deploy/docker/models/` 下应包含 `bge-m3/` 与 `bge-reranker-v2-m3` 两个目录。
`local/.env` 中的 `EMBED_MODEL` / `RERANK_MODEL` 默认即指向容器内的 `/models-local/...`，
无需修改。

## 启动

### 在线模型模式

在线模型模式不安装 `torch` / `FlagEmbedding` / `transformers`，也不挂载本地模型目录。嵌入与精排
通过外部 HTTP 服务提供：

```bash
cd deploy/docker/online
cp .env.example .env
# 编辑 .env，至少填入 LLM_API_KEY、MODEL_API_TOKEN、EMBEDDER_BASE_URL、RERANKER_BASE_URL
docker compose up -d --build
```

`EMBEDDER_BASE_URL` 必须包含 `/v1`，例如 `https://models.example.com/v1`；
`RERANKER_BASE_URL` 不要包含 `/rerank`，例如 `https://models.example.com`。

LLM 默认使用 `target: dashscope`。`LLM_ENABLE_THINKING=false` 会通过 DashScope
Adapter 发送 `extra_body.enable_thinking=false`；设为 `true` 可开启思考，设为
`null` 则完全不发送该厂商字段。

### PostgreSQL 模式

该模式保留在线模型 API 与 Elasticsearch，只把 Redis KV、默认 Vector、L0 Vector、
L1 Vector 替换为同一个 PostgreSQL 实例中的四张独立表。Compose 会启动
`pgvector/pgvector:0.8.3-pg16`，不再启动 Redis、Milvus、etcd 或 MinIO：

```bash
cd deploy/docker/postgres
cp .env.example .env
# 编辑 .env，至少填写模型凭据和模型端点；生产环境还应修改数据库密码
docker compose config
docker compose up -d --build
```

PostgreSQL 健康后应用才启动。扩展和四张表由 `/docker-entrypoint-initdb.d/10-agent-memory.sql`
在空数据卷的第一次启动时创建，配置因此固定 `auto_create_schema: false` /
`create_extension: false`。默认 DDL 是 `public` schema、1024 维、COSINE/HNSW；调整
schema、表名或向量维度时，应在首次启动前同步修改 `.env`、`config.yml` 和初始化脚本。
初始化脚本不会在已有数据卷上重复执行。

### 本地模型模式

```bash
cd deploy/docker/local
cp .env.example .env
# 编辑 .env，至少填入 LLM_API_KEY
docker compose up -d --build
```

首次启动顺序：etcd / MinIO → Milvus、Elasticsearch、Redis 健康后，应用才会启动
（`depends_on: service_healthy`）。本地模型模式下 bge 模型从挂载的 `/models-local` 直接加载，
全程不联网；模型在**应用首次收到请求时**载入内存，该次请求耗时较长，此后常驻复用。

查看状态与日志：

```bash
docker compose ps
docker compose logs -f agent-memory
```

## 验证

```bash
# 健康检查
curl http://localhost:8137/healthz

# 写入一条记忆（首次请求会触发模型载入内存，需等待数秒）
curl -X POST http://localhost:8137/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","scope":"alice","content":"用户偏好用 Python 写代码"}'

# 召回
curl -X POST http://localhost:8137/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","scope":"alice","query":"用什么语言","k":5}'
```

其他接口参见 [bootstrap/core/handler.py](../../bootstrap/core/handler.py)：
`add / search / list / get / update / delete / evolve / job / inspect / trace / audit / admin / grant`。

## 端口

| 服务 | 端口 | 用途 |
|---|---|---|
| agent-memory | 8137 | HTTP API |
| elasticsearch | 9200 | ES REST |
| milvus | 19530 / 9091 | SDK / healthz |
| PostgreSQL（PostgreSQL profile） | `${POSTGRES_PORT:-5432}` | 数据库连接 / 排障 |

## 常见调整

- **换 LLM 厂商**：同时修改 `config.yml` 的 `llm.default.target` 和 `.env` 中的
  `LLM_BASE_URL` / `LLM_MODEL`。阿里云使用 `dashscope`，通用 OpenAI-compatible
  端点使用 `openai`；不要只换 URL 而保留其他厂商的专属参数。
- **换在线模型服务**：改 `online/.env` 的 `EMBEDDER_BASE_URL` / `RERANKER_BASE_URL` /
  `MODEL_API_TOKEN`。`EMBEDDER_BASE_URL` 带 `/v1`，`RERANKER_BASE_URL` 不带 `/rerank`。
- **降低内存占用（省去 Milvus 三件套）**：将 `vector_store` 改用 Milvus Lite（pymilvus 内嵌、
  文件存储），在对应模式目录的 `config.yml` 中把 `uri` 改为本地文件（如 `./milvus.db`），并从 compose 中移除
  etcd / minio / milvus 三个服务。注意 Milvus Lite 不适用于生产环境。
- **提升中文关键词检索精度**：为 Elasticsearch 安装 IK 分词插件（需自建 ES 镜像）；默认的 standard
  分析器对中文按单字切分。

## 配置文件

服务读哪个配置由两处决定：模式目录下的 `docker-compose.yml` 把宿主机文件挂到容器 `/config/config.yml`，
同目录 `Dockerfile` 的 `CMD` 把该路径作为参数传给 `__main__.py`。即**真正"用哪个文件"的开关是 compose
里的挂载源**：

```yaml
# online/docker-compose.yml → 服务 agent-memory
volumes:
  - ./config.yml:/config/config.yml:ro    # 左 = 宿主机文件，右 = 容器内固定路径
```

配置是**两级命名空间**且会**合并覆盖内置默认**（纯内存离线栈），故配置文件只需写「与默认不同」
的部分（详见 [online/config.yml](online/config.yml)、[local/config.yml](local/config.yml) 与 `src/config`）。

### 新增并启用一个配置文件

1. 在对应模式目录下新建文件，例如 `online/config.lite.yml`，只写要改动的命名空间/参数（其余继承默认）。
2. 把 compose 的挂载源指过去（**整体替换**，无需改 CMD）：

   ```yaml
   volumes:
     - ./config.lite.yml:/config/config.yml:ro
   ```
3. 重新创建容器使挂载生效（改 compose 须 recreate，`restart` 不够）：

   ```bash
   docker compose up -d agent-memory
   ```

### 多文件分层（进阶）

`__main__.py` 接收**多个**配置路径，依次叠在内置 `OFFLINE` 之上、靠后覆盖靠前。可挂多个文件并用
`command:` 覆盖 `CMD` 显式指定：

```yaml
volumes:
  - ./config.yml:/config/config.yml:ro
  - ./config.prod.yml:/config/config.prod.yml:ro
command: ["python", "bootstrap/http_server/__main__.py",
          "--host", "0.0.0.0", "--port", "8137",
          "/config/config.yml", "/config/config.prod.yml"]
```

⚠️ 注意：**文件之间是顶层键浅合并** —— 后一个文件的 `memory_api` 会**整体替换**前一个的，不做
深合并。所以「只覆盖内核里某几项」应写在**单个文件的 `memory_api` 内**（它本就只需写与默认的差异），
而不要把 `memory_api` 拆到多个文件。分层更适合覆盖 `profile` / `policies` 这类顶层键。

> 不走 docker 直接本地跑同理：`scripts/run-server.sh <配置路径> [更多路径…]`。

## 关停 / 清数据

```bash
docker compose down           # 停止服务，保留数据卷
docker compose down -v        # 停止服务并删除当前 profile 声明的本地数据卷
```

在 `postgres` profile 中执行 `down -v` 会同时删除 Elasticsearch 与 PostgreSQL 数据卷，
包括所有 KV 和向量数据；仅停止服务请使用不带 `-v` 的命令。
