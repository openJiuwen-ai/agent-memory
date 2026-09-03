# SDK 部署

SDK 部署是指在使用方的 Python 进程中安装和装配 `jiuwen_memory`。运行形态可以是直接调用
`MemoryAPI`，也可以是在本机启动 HTTP 服务；存储可以使用默认进程内实现，也可以连接由 Docker
启动的真实后端。

> 当前 `jiuwen_memory_entry/sdk/__init__.py` 还是空的，项目尚未提供独立封装的 SDK 客户端。
> 本文以 `jiuwen_memory.api.assemble()` 和 `MemoryAPI` 作为当前稳定的 Python 接入入口。

## 1. 组合方式

| 组合 | 应用位置 | 存储位置 | 是否持久化 | 适用场景 |
|---|---|---|---|---|
| 内存 + MemoryAPI | Python 进程 | 同一进程内 | 否 | 单测、开发、最小功能验证 |
| 内存 + 本地 HTTP | 本机 HTTP 进程 | 同一进程内 | 否 | HTTP 协议联调、跨语言快速接入 |
| Docker 后端 + MemoryAPI | Python 进程 | Docker 容器 | 是 | Python 应用内嵌、避免 HTTP 跳转 |
| Docker 后端 + 本地 HTTP | 本机 HTTP 进程 | Docker 容器 | 是 | 本地常驻服务、跨进程或跨语言调用 |

```text
直接调用：Python 应用 ──> MemoryAPI ──> Storage 接口 ──> 内存或 Docker 后端

HTTP 调用：客户端 ──> 本地 HTTP ──> MemoryAPI ──> Storage 接口 ──> 内存或 Docker 后端
```

## 2. 安装

项目要求 Python 3.11 或更高版本。仓库开发环境可使用：

```bash
uv sync
```

若要连接 Redis、Milvus、Elasticsearch 或 PostgreSQL/pgvector，需要安装 `deploy` extra：

```bash
uv sync --extra deploy
```

若要在当前 Python 进程中加载本地 BGE 嵌入与精排模型，还需要：

```bash
uv sync --extra embed
```

使用 pip 时，对应命令为：

```bash
python -m pip install -e .
python -m pip install -e '.[deploy]'
python -m pip install -e '.[embed]'
```

## 3. 方式一：内存存储 + 直接调用 MemoryAPI

不传配置时，`assemble()` 使用内置离线装配：`CompositeStorage` 组合进程内 KV、Vector、
Fulltext 与 Graph Store，嵌入、LLM 和精排也使用无外部依赖的默认实现。

```python
from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Context, Scope

runtime = assemble_runtime()
api = runtime.api
scope = Scope(org="demo", user="alice")
security = legacy_request_context(scope)

try:
    units = api.add(
        "用户偏好用 Python 写代码",
        scope,
        security=security,
        tags=["preference"],
    )
    result = api.search(
        "用户喜欢用什么语言",
        Context(scope),
        security=security,
        top_k=5,
    )
    print(units[0].id)
    print([item.content for item in result.items])
finally:
    runtime.close(wait=True)
```

该模式的所有数据都在当前进程内，进程退出后丢失。它不需要 Docker，也不需要模型服务。

## 4. 方式二：内存存储 + 本地 HTTP 启动器

在仓库根目录运行：

```bash
uv run --no-sync -- ./scripts/run-server.sh --host 127.0.0.1 --port 8137
```

如果没有使用 uv，也可以在已安装依赖的环境中运行：

```bash
./scripts/run-server.sh --host 127.0.0.1 --port 8137
```

另开终端可以验证健康检查：

```bash
curl http://127.0.0.1:8137/healthz
```

HTTP 数据请求必须由可信 `SecurityRuntime` 完成认证后才会 dispatch。当前仓库尚未提供
`SecurityRuntimeProducer` 的生产装配器，因此通过启动脚本启动的参考服务会对 `POST /v1/<verb>`
安全地返回 503，而不会回退到请求体中的 actor。集成应用应在调用
`HttpServer.build(..., security_runtime=runtime)` 时注入认证 runtime；认证成功后的请求体使用嵌套
`target`，并携带认证头：

```bash

curl -X POST http://127.0.0.1:8137/v1/add \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $AGENT_MEMORY_API_KEY" \
  -d '{"target":{"tenant_id":"demo","scope":"alice"},"content":"用户偏好用 Python 写代码"}'

curl -X POST http://127.0.0.1:8137/v1/search \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $AGENT_MEMORY_API_KEY" \
  -d '{"target":{"tenant_id":"demo","scope":"alice"},"query":"用户喜欢用什么语言","k":5}'
```

HTTP 进程内只装配一个 Kernel，请求之间能够共享状态；服务停止后，默认内存数据丢失。

## 5. 方式三：Docker 后端存储 + 直接调用 MemoryAPI

推荐优先复用 PostgreSQL profile 的后端，因为它只需要 PostgreSQL/pgvector 和
Elasticsearch 两个容器。先启动存储，不启动 agent-memory 应用容器：

```bash
cd deploy/docker/postgres
cp .env.example .env
# 修改 .env 中的 POSTGRES_PASSWORD 等数据库配置
docker compose up -d postgres elasticsearch
docker compose ps
```

首次创建数据卷时，Compose 会执行
[scripts/pg_schema.sql](../../../scripts/pg_schema.sql) 创建 KV 和 pgvector 表。

随后准备一份宿主机配置，例如 `local-real-storage.yml`：

```yaml
profile: local-real-storage

memory_api:
  globals:
    vector_enabled: true
    graph_enabled: false
    embedder_dim: 1024

  kv_store:
    default:
      target: postgres
      params:
        dsn: "${PG_DSN}"
        schema: public
        table: agent_memory_kv
        auto_create_schema: false

  vector_store:
    default:
      target: pgvector
      params: &pgvector_params
        dsn: "${PG_DSN}"
        schema: public
        table: agent_memory_vectors
        dim: 1024
        metric_type: COSINE
        auto_create_schema: false
        create_extension: false
    layers_l0:
      target: pgvector
      params:
        <<: *pgvector_params
        table: agent_memory_vectors_l0
    layers_l1:
      target: pgvector
      params:
        <<: *pgvector_params
        table: agent_memory_vectors_l1

  fulltext_store:
    default:
      target: elasticsearch
      params: &es_params
        hosts: "${ES_HOSTS:-http://127.0.0.1:9200}"
        index: agent_memory_fulltext
        text_analyzer: cjk
    layers_l0:
      target: elasticsearch
      params:
        <<: *es_params
        index: agent_memory_fulltext_l0
    layers_l1:
      target: elasticsearch
      params:
        <<: *es_params
        index: agent_memory_fulltext_l1
```

该示例仍使用内置 hashing embedder、echo LLM 与 overlap reranker，只把存储换成真实后端，因此
不需要模型服务。`embedder_dim: 1024` 必须和初始化 SQL 中的 pgvector 维度一致。

设置宿主机连接地址：

```bash
export PG_DSN='postgresql://agent_memory:请替换密码@127.0.0.1:5432/agent_memory'
export ES_HOSTS='http://127.0.0.1:9200'
```

直接调用时，`assemble()` 只接受 `memory_api` 内部的两级命名空间。为了同时复用 HTTP
配置格式和环境变量展开逻辑，可以这样加载：

```python
from jiuwen_memory_entry.core.config_loader import load_layer
from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.config import Config

layer = load_layer("local-real-storage.yml")
kernel_config = Config.from_dict(layer["memory_api"])
runtime = assemble_runtime(config=kernel_config)
api = runtime.api

scope = Scope(org="demo", user="alice")
security = legacy_request_context(scope)
try:
    api.add("需要持久化的记忆", scope, security=security)
    result = api.search("持久化", Context(scope), security=security, top_k=5)
    print([item.content for item in result.items])
finally:
    runtime.close(wait=True)
```

不要直接对这份带 `profile`、`memory_api` 外壳的文件调用 `Config.from_yaml()`；那会把服务层键
当成内核组件命名空间。另一个区别是 `Config.from_yaml()` 只解析 YAML，不会展开 `${VAR}`；
上例使用的 `load_layer()` 才会展开环境变量。

## 6. 方式四：Docker 后端存储 + 本地 HTTP 服务

保持上一节的 PostgreSQL 和 Elasticsearch 容器运行，然后启动本地 HTTP 服务：

```bash
export PG_DSN='postgresql://agent_memory:请替换密码@127.0.0.1:5432/agent_memory'
export ES_HOSTS='http://127.0.0.1:9200'

uv run --no-sync -- ./scripts/run-server.sh \
  --host 127.0.0.1 \
  --port 8137 \
  local-real-storage.yml
```

HTTP 启动器会读取最外层的 `profile`，只把 `memory_api` 交给内核装配，并展开配置中的环境变量。
应用进程退出不会删除 PostgreSQL 和 Elasticsearch 中的数据；后端容器及其数据卷仍需单独管理。

## 7. 为什么示例优先使用 PostgreSQL profile

`deploy/docker/postgres/docker-compose.yml` 已经把 PostgreSQL `5432` 和 Elasticsearch
`9200` 映射到宿主机，所以可以直接供宿主机 Python 进程访问。

Redis/Milvus profile 虽然暴露了 Milvus 和 Elasticsearch，但当前 Compose 没有把 Redis
`6379` 映射到宿主机。因此“宿主机应用 + Redis/Milvus/ES”不能原样复用现有 Compose，需要另写
Compose override 或 storage-only Compose，为 Redis 增加受控的端口映射。不要把容器内的
`redis://redis:6379`、`http://milvus:19530` 等地址直接写进宿主机配置。

## 8. 配置形态对照

### 启动本地 HTTP

HTTP 启动器接收服务层配置：

```yaml
profile: local-profile
memory_api:
  globals: {}
  kv_store: {}
```

启动命令可以接收一个或多个 YAML/JSON 文件：

```bash
./scripts/run-server.sh config.yml
```

### 直接调用 MemoryAPI

`jiuwen_memory.config.Config` 接收的是内核配置本体：

```yaml
globals: {}
kv_store: {}
```

如果要直接读取这种不带 `memory_api` 外壳的文件，可以使用：

```python
from jiuwen_memory.api import assemble
from jiuwen_memory.config import Config

api = assemble(config=Config.from_yaml("memory-api.yml"))
```

注意，此方式不会自动展开环境变量，应写入已经解析好的值，或由应用先完成环境变量注入。

## 9. HTTP 接口与 MemoryAPI 的选择

HTTP 服务覆盖 28 个 verb，包括常用的 add、batch_add、search、list、get、update、delete、
evolve、job、inspect、trace、audit、admin、grant/revoke 和 space 管理，但它是参数收窄后的适配层。

以下需求应优先直接调用 MemoryAPI：

- 使用 `add_async()`、`batch_add_async()` 或取消后台任务；
- 指定检索 `as_of`、披露层级等完整参数；
- 使用 update 的完整版本模式，或 delete 的完整选择器与治理策略；
- 使用完整五维 `Scope(org, space, user, agent, session)`；
- 需要 Python 对象返回值，而不是 HTTP JSON 视图。

需要跨语言、跨进程访问，或只使用通用 CRUD/检索能力时，可以选择 HTTP。

## 10. 运行与安全注意事项

- 默认内存栈随进程退出丢失；真实后端的数据生命周期由 Docker volume 决定；
- 本地脚本不会自动读取 `.env`，需要在 shell 中 `export`，或使用其他进程管理工具注入环境变量；
- 开发环境建议绑定 `127.0.0.1`，不要直接把参考 HTTP 服务暴露到公网；
- HTTP actor 仅来自认证上下文，请求体中的 `actor_*`、`identity` 等身份声明会被拒绝；
- 未装配认证 runtime 的 HTTP 启动器会返回 503，不会降级采用空身份或请求体身份；
- 生产场景应提供可信认证 runtime，并增加 TLS、限流、超时、监控、备份和可靠的进程管理；
- 应用退出前应调用 `runtime.close(wait=True)`，等待并释放进程内摄入任务线程池。

## 11. 相关文档

- [部署方式概览](部署方式概览.md)
- [容器化部署](容器化部署.md)
- [Storage API 文档](../API文档/storage.md)
- [Retrieval API 文档](../API文档/retrieval.md)
- [HTTP 启动脚本](../../../scripts/run-server.sh)
