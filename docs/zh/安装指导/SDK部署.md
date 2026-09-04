# SDK 部署

最近一次修订日期：2026-09-05

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
from jiuwen_memory.api import Context, Scope, assemble_runtime, legacy_request_context

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

示例中的 `legacy_request_context` 仅为接口迁移过渡期的本地测试桥，不验证凭据。
生产调用应由应用的可信认证边界生成 `RequestSecurityContext`；不能将业务 `scope`
直接当成已认证身份。HTTP / CLI 已使用独立认证边界，不走这条 legacy 桥。

## 4. 方式二：内存存储 + 本地 HTTP 启动器

在仓库根目录运行：

```bash
uv run --no-sync -- ./scripts/run-server.sh --auth-mode dev --host 127.0.0.1 --port 8137
```

如果没有使用 uv，也可以在已安装依赖的环境中运行：

```bash
./scripts/run-server.sh --auth-mode dev --host 127.0.0.1 --port 8137
```

另开终端可以验证健康检查：

```bash
curl http://127.0.0.1:8137/healthz
```

`--auth-mode dev` 会启用仅供本地功能测试的固定身份认证器。它忽略认证头，由服务端生成
`Scope(org="local", user="developer")` 的 ROOT 身份并继续执行 `MemoryAPI` 的授权检查；因此示例把
业务目标也设为该 Scope。该模式默认只允许绑定回环地址，不得用于生产环境。请求体保持同名
`MemoryAPI` 方法的参数结构：

```bash

curl -X POST http://127.0.0.1:8137/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"content":"用户偏好用 Python 写代码","scope":{"org":"local","space":"","user":"developer","agent":"","session":""}}'

curl -X POST http://127.0.0.1:8137/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"用户喜欢用什么语言","context":{"scope":{"org":"local","space":"","user":"developer","agent":"","session":""},"extensions":{}},"top_k":5}'
```

HTTP 认证模式按 `--auth-mode`、环境变量 `JIUWEN_MEMORY_HTTP_AUTH_MODE`、默认值
`required` 的优先级选择。例如，不传该参数但环境变量为 `dev` 时仍会启用开发认证。
`required` 模式下，当前启动器没有生产 `SecurityRuntimeProducer`，业务接口保持
fail-closed 返回 503；集成应用应通过
`HttpServer.build(..., security_runtime=security_runtime)` 注入可信认证 runtime。
这里的 `security_runtime` 是安全组件容器，不是 `assemble_runtime()` 返回的记忆内核运行时。
开发启动器使用的 `DevHttpSecurityRuntime` 仅带固定身份认证器，不含限流、并发保护或
surface 审计组件，但 API 自身的授权与业务审计仍然执行。

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
from jiuwen_memory.api import Context, Scope, assemble_runtime, legacy_request_context
from jiuwen_memory_entry.core.config_loader import load_layer

layer = load_layer("local-real-storage.yml")
runtime = assemble_runtime(config=layer["memory_api"])
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
  --auth-mode dev \
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
./scripts/run-server.sh --auth-mode dev config.yml
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

HTTP 服务一一公开 `MemoryAPI` 的全部 36 个方法，统一使用 `POST /v1/<method_name>`。请求参数的
名称、嵌套结构、必填项和默认值与同名方法一致；成功响应是原返回值的 JSON 表达，不增加额外响应
外壳。`security` 是唯一不从请求体接收的参数，由认证 runtime 构造并注入。

常规方法已全部暴露，直接调用和 HTTP 通常按运行形态选择：

- 需要进程内低延迟、Python 类型检查和原始 Python 对象时，直接调用 `MemoryAPI`；
- 需要跨语言或跨进程访问时，使用 HTTP；
- 调用异步 HTTP 路由仍是一次普通请求—响应，服务会等待同名异步方法完成，不额外生成 job。

当前例外：写入 `system_metadata.coords` 的归属判定扩展仍会被 HTTP/CLI 的类型解析拒绝，
需要直接调用 Python API；这不影响常规 Scope 写入。详见 [API F05 已知遗留](../../features/api/F05-http-memory-api-alignment.md#已知遗留)。

### CLI 使用同一套参数

CLI 当前提供全部 36 个 API 同名命令，参数保留原名，复杂对象使用 JSON；例如：

```bash
uv run --no-sync -- ./scripts/run-cli.sh --auth-mode dev add \
  --content 'CLI 写入测试' --scope '{"org":"local","user":"developer"}'
```

本地模式直接调用 API；`--server http://127.0.0.1:8137` 模式原样发送 HTTP 请求并读取原返回值。
本地不启用 dev 且未注入认证器时返回 503；远程认证由服务器决定，CLI 的
`AGENT_MEMORY_API_KEY` 作为 Bearer 凭据发送，不能同时加本地 `--auth-mode dev`。
默认内存后端不跨 CLI 进程保存数据，连续调用需 `batch` 会话、持久化后端或常驻 HTTP。
完整参数见 [CLI 说明](../../../jiuwen_memory_entry/cli/DESIGN.md)。

## 10. 运行与安全注意事项

- 默认内存栈随进程退出丢失；真实后端的数据生命周期由 Docker volume 决定；
- 本地脚本不会自动读取 `.env`，需要在 shell 中 `export`，或使用其他进程管理工具注入环境变量；
- 开发环境建议绑定 `127.0.0.1`，不要直接把参考 HTTP 服务暴露到公网；
- HTTP actor 仅来自认证上下文，请求体中的 `actor_*`、`identity` 等身份声明会被拒绝；
- 默认 `required` 模式未装配生产认证 runtime 时返回 503，不会降级采用空身份或请求体身份；
- `dev` 模式固定使用 `local/developer` ROOT 身份、忽略认证头但仍执行授权，仅限回环地址上的功能测试；
- 生产场景应提供可信认证 runtime，并增加 TLS、限流、超时、监控、备份和可靠的进程管理；
- 应用退出前应调用 `runtime.close(wait=True)`，等待并释放进程内摄入任务线程池。

## 11. 相关文档

- [部署方式概览](部署方式概览.md)
- [容器化部署](容器化部署.md)
- [Storage API 文档](../API文档/storage.md)
- [Retrieval API 文档](../API文档/retrieval.md)
- [HTTP 启动脚本](../../../scripts/run-server.sh)
