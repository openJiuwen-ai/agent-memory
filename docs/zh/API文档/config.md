# Config 配置与装配指南

Agent Memory 的 Config 不只是把 YAML 字段传给某个构造函数，而是一套基于默认拓扑、
Producer 注册表、具名实例和依赖引用的轻量级装配机制。本指南说明配置如何组织、合并、解析
和装配，以及哪些字段可以在运行时晚绑定。

本文以当前代码为准，主要参考：

- [`config.py`](../../../jiuwen_memory/config/config.py)
- [`context.py`](../../../jiuwen_memory/config/context.py)
- [`defaults.py`](../../../jiuwen_memory/config/defaults.py)
- [`config_source.py`](../../../jiuwen_memory/config/config_source.py)
- [`project.py`](../../../jiuwen_memory/config/project.py)
- [`active.py`](../../../jiuwen_memory/config/active.py)
- [`binding.py`](../../../jiuwen_memory/config/binding.py)
- [`routing.py`](../../../jiuwen_memory/config/routing.py)
- [`factory.py`](../../../jiuwen_memory/common/factory/factory.py)
- [`assembly.py`](../../../jiuwen_memory/api/memory_api_impl/assembly.py)
- [`config_loader.py`](../../../bootstrap/core/config_loader.py)
- [`profiles.py`](../../../bootstrap/core/profiles.py)
- [`server.py`](../../../bootstrap/core/server.py)

## 1. 配置模型概述

配置主要分为四类：

| 配置类别 | 作用 | 主要生效时机 |
|---|---|---|
| 装配拓扑 | 选择组件实现、声明实例、连接组件依赖 | `build_kernel()` |
| 全局参数 | 向多个组件提供能力开关和公共参数 | 主要在 `build_kernel()` |
| `ConfigSource` | 晚绑定模型、凭证、连接地址和 Prompt | 运行时调用阶段 |
| `PolicyManager` | 管理生命周期、Space 等业务策略 | 运行时 |

配置的核心结构是：

```text
命名空间
  └── 具名实例
        ├── target
        ├── params
        └── new_instance
```

例如：

```yaml
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
```

其中：

- `constructor` 是 `IndexBuilderProducer.TOP_NAME`，表示 IndexBuilder 配置命名空间；
- `default` 是具名实例名称；
- `hybrid` 是注册到 `IndexBuilderProducer` 的实现 target；
- `storage: default` 表示引用 `storage.default`；
- `chunker: default` 表示引用 `chunker.default`；
- `embedder: default` 表示引用 `embedder.default`。

因此，`target` 表示“当前抽象接口选择哪个实现”，不是构造函数参数。

## 2. 完整装配链路

SDK 和服务部署最终都会进入 `build_kernel()`：

```text
SDK
  Config.from_dict / Config.from_yaml
                 ┐
                 ├─> build_kernel
                 │     ├─ 注册全部 Producer target
HTTP / MCP       │     ├─ 清空本次装配的具名实例缓存
  load_layer     │     ├─ default_context + 用户配置覆盖
  -> load_config │     ├─ 先装配 ConfigSource
  -> memory_api ─┘     ├─ 装配 KV / Storage / IngestJob
                       ├─ 从根引用递归装配 Engine 及其依赖
                       └─ 返回 Kernel
```

`Kernel` 当前包含：

| 字段 | 作用 |
|---|---|
| `api` | 形态无关的 `MemoryAPI` 入口 |
| `kv` | 按 `kv_store.default` 装配的真源 KV 句柄 |
| `storage` | 上层统一使用的 Storage，默认是 `CompositeStorage` |
| `ingest_jobs` | 长耗时摄入任务控制器 |
| `space` | SpaceManager |
| `config_source` | 运行时晚绑定配置来源 |

组件并不是按 YAML 出现顺序创建，而是从 `ROOT_PARAMS` 中的根引用开始，根据各 builder 的
`dep()` 调用递归装配。没有被根组件或其他组件引用的具名实例不会自动创建。

## 3. 配置入口

### 3.1 SDK 方式

SDK 可以直接构造内核配置：

```python
from jiuwen_memory.api import build_kernel
from jiuwen_memory.config import Config

config = Config.from_dict(
    {
        "globals": {
            "vector_enabled": True,
            "graph_enabled": False,
        },
        "kv_store": {
            "default": {
                "target": "sqlite",
                "params": {
                    "db_path": "./data/memory.db",
                },
            }
        },
    }
)

kernel = build_kernel(config=config)
memory_api = kernel.api
```

也可以从只包含内核配置的 YAML 文件读取：

```python
config = Config.from_yaml("./memory-config.yml")
kernel = build_kernel(config=config)
```

`Config.from_yaml()` 只负责解析 YAML，不会展开 `${ENV_VAR}`。SDK 场景需要调用方自行读取
环境变量，或提前构造配置字典。

### 3.2 HTTP、MCP 和部署配置

部署配置在内核配置之外还有一层服务配置：

```yaml
profile: docker

policies:
  scope.require_space: "true"

memory_api:
  globals:
    vector_enabled: true
    graph_enabled: false

  kv_store:
    default:
      target: redis
      params:
        url: "${REDIS_URL:-redis://redis:6379/0}"
```

| 字段 | 作用 |
|---|---|
| `profile` | 服务启动 profile，不属于内核组件命名空间 |
| `policies` | 传给 `PolicyManager` 的便捷策略配置 |
| `memory_api` | 真正传入 `Config.from_dict()` 和 `build_kernel()` 的内核配置 |

HTTP/MCP 启动过程会：

1. 读取 YAML 或 JSON；
2. 展开 `${VAR}` 和 `${VAR:-default}`；
3. 合并服务配置层；
4. 只取 `memory_api` 段；
5. 构造内核 `Config`；
6. 调用 `build_kernel()`。

不能把包含 `profile`、`policies` 和 `memory_api` 的完整部署配置直接传给内核 `Config`，
否则 `profile` 等字段会被当成未知 Producer 命名空间。

## 4. 配置基本结构

### 4.1 `globals`

`globals` 存放跨组件公共参数：

```yaml
globals:
  vector_enabled: true
  graph_enabled: false
  rerank_enabled: true
  layers_index_enabled: true

  embedder_dim: 1024
  chunk_size: 512

  llm_model: qwen-plus
  llm_base_url: https://example.com/v1
  llm_api_key: ${LLM_API_KEY}
```

组件通过 `config.get(key, default)` 读取参数，优先级为：

```text
当前实例 params
  > globals
  > 实现代码中的默认值
```

例如：

```yaml
globals:
  embedder_dim: 1024

embedder:
  default:
    target: hashing
    params:
      tokenizer: default
      embedder_dim: 768
```

当 Hashing Embedder builder 查询 `embedder_dim` 时，实例参数中的 `768` 优先于全局参数。

只有 builder 主动读取的参数才会生效。`globals` 不是任意字段都能自动注入的通用配置中心。

### 4.2 `prompts`

Prompt 使用独立的顶层段：

```yaml
prompts:
  extract:
    preference: "抽取用户偏好并返回约定 JSON。"
  consolidate:
    preference: "判断候选是新增、更新、取代还是忽略。"
  reflect:
    preference: "落盘前检查候选。"
```

装配时，`prompts` 会进入 `AssemblyContext.globals["prompts"]`，供 `PromptRegistry` 使用。

调用侧通常只传 Prompt key：

```python
system_metadata = {
    "_extract_prompt_preference": "preference",
}
```

业务接口不应传递完整 Prompt 文本。

### 4.3 组件命名空间

每个 Producer 使用唯一的 `TOP_NAME` 对应一个配置命名空间：

```yaml
kv_store:
  default:
    target: redis
    params:
      url: redis://localhost:6379/0

vector_store:
  default:
    target: milvus
    params:
      uri: http://localhost:19530
      dim: 1024

constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
```

常见命名空间：

| 分类 | 命名空间 |
|---|---|
| 共享组件 | `tokenizer`、`chunker`、`embedder`、`llm`、`reranker` |
| Storage | `storage`、`kv_store`、`vector_store`、`fulltext_store`、`graph_store` |
| Construction | `extractor`、`classifier`、`constructor`、`dedup`、`evolver` |
| Retrieval | `query_parser`、`recaller`、`fuser`、`discloser`、`retriever` |
| Control | `engine`、`scheduler`、`permission`、`policy`、`lifecycle` |

具体可选 target 以各层 API 文档和 Producer 注册结果为准。

## 5. 具名实例结构

### 5.1 完整形式

```yaml
kv_store:
  default:
    target: redis
    params:
      url: redis://localhost:6379/0
    new_instance: false
```

| 字段 | 必填 | 作用 |
|---|---:|---|
| `target` | 是 | Producer 注册的实现名 |
| `params` | 否 | 实现参数和依赖引用 |
| `new_instance` | 否 | 是否跳过具名实例缓存，默认 `false` |

### 5.2 简写形式

没有参数时，可以直接写 target：

```yaml
kv_store:
  default: memory

fuser:
  default: rrf
```

等价于：

```yaml
kv_store:
  default:
    target: memory

fuser:
  default:
    target: rrf
```

## 6. 依赖引用方式

组件 builder 通过对应 Producer 的 `dep()` 解析依赖。

### 6.1 具名引用

字符串表示引用另一个命名空间下的具名实例：

```yaml
storage:
  default:
    target: composite
    params:
      kv_store: default
      vector_store: default
```

装配时分别解析为：

```text
KvProducer.build_named("default")
VectorProducer.build_named("default")
```

相同 Producer 下引用相同名称会命中同一实例缓存。这也是 Construction、Retrieval 和
Control 共享同一 Storage、Embedder、Tokenizer 的主要机制。

### 6.2 匿名内联实例

映射表示构造一个匿名实例：

```yaml
query_parser:
  default:
    target: simple
    params:
      tokenizer:
        target: whitespace
      llm:
        target: echo
```

匿名实例：

- 不需要在对应命名空间中提前声明；
- 不进入具名缓存；
- 每次解析都会新建；
- 不适合需要跨组件共享状态的 Store、Embedder 或 Tokenizer。

### 6.3 缺省依赖

如果 builder 调用：

```python
EmbedderProducer.dep(config, default="hashing")
```

而 `params` 没有配置 `embedder`，系统会匿名创建一个 `hashing` Embedder。

这虽然保证了默认可运行，但可能绕过用户声明的 `embedder.default`。因此覆盖依赖较多的实例时，
建议显式写全仍需使用的具名依赖。

### 6.4 字符串不一定是依赖

`params` 中的字符串只有在 builder 使用某个 Producer 的 `dep()` 读取时，才表示具名引用。

```yaml
params:
  storage: default
  collection: agent_memory_vectors
```

- `storage` 被 `StorageProducer.dep()` 或 `StorageProducer.resolve()` 读取，是具名引用；
- `collection` 被 `config.get()` 读取，是普通字符串。

配置解析器本身不会根据字符串内容推断它是不是依赖。

## 7. 实例共享

具名实例默认共享：

```yaml
embedder:
  default:
    target: openai
```

如果 Construction 和 Retrieval 都引用：

```yaml
params:
  embedder: default
```

两侧得到的是同一个 `embedder.default` 实例。

默认配置也使用这一方式共享：

```text
Kernel
  ├── storage.default
  │    ├── kv_store.default
  │    ├── vector_store.default
  │    ├── fulltext_store.default
  │    └── graph_store.default
  │
  └── engine.default
       ├── constructor.default -> storage.default
       ├── retriever.default   -> storage.default
       ├── evolver.default     -> storage.default
       └── lifecycle.default   -> storage.default
```

因此，默认情况下：

- Kernel、Engine 和 Retriever 使用同一个 `Storage` 实例；
- Construction 和 Retrieval 使用同一个底层 Embedder；
- 写入和召回使用同一组 Store；
- `CompositeStorage` 暴露的 Store 端口可能是授权代理，但代理背后仍是对应的具名 Store。

### 7.1 `new_instance`

```yaml
llm:
  isolated:
    target: openai
    new_instance: true
    params:
      llm_model: qwen-plus
```

每次引用 `llm.isolated` 都会重新构造实例，不进入具名缓存。

建议仅在组件明确需要实例隔离时使用。对有状态 Store 使用 `new_instance: true`，容易造成
写入侧和读取侧连接到不同实例。

## 8. 默认配置与用户覆盖

`build_kernel()` 首先创建内置默认配置，再合并用户配置。

默认配置是一套可离线运行的进程内组合：

```text
kv_store.default       -> memory
vector_store.default   -> memory
fulltext_store.default -> memory
graph_store.default    -> memory
storage.default        -> composite

embedder.default       -> hashing
llm.default            -> echo
constructor.default    -> hybrid
retriever.default      -> pipeline
engine.default         -> in_memory
```

用户通常只需要声明与默认配置不同的部分：

```yaml
kv_store:
  default:
    target: redis
    params:
      url: redis://redis:6379/0

vector_store:
  default:
    target: milvus
    params:
      uri: http://milvus:19530
      dim: 1024
```

默认 `storage.default` 已经引用 `kv_store.default` 和 `vector_store.default`，所以这里只替换
Store target 即可，不需要重新声明完整 Storage。

## 9. 合并规则

### 9.1 内核配置合并

内置配置与用户配置按以下方式合并：

- `globals`：按字段覆盖；
- 命名空间：按具名实例名称覆盖或新增；
- 同名实例：整个 `RawSpec` 被替换；
- 同名实例内部的 `params`：不做深合并。

例如默认配置：

```yaml
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
```

用户配置：

```yaml
constructor:
  default:
    target: hybrid
    params:
      layers_index_enabled: false
```

合并后不会保留默认的三个依赖引用，只剩 `layers_index_enabled`。Hybrid builder 可能回退到
匿名默认 Chunker/Embedder，从而绕过用户配置的具名实例。

推荐写法：

```yaml
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
      layers_index_enabled: false
```

如果参数本身是跨组件公共开关，更推荐放在 `globals`：

```yaml
globals:
  layers_index_enabled: false
```

这样不需要覆盖整个 `constructor.default`。

### 9.2 多个部署文件合并

HTTP/MCP 支持依次加载多个配置文件，但服务配置层只做顶层浅合并：

```text
base.yml
  memory_api: {...}

prod.yml
  memory_api: {...}
```

后加载的 `prod.yml.memory_api` 会整体替换 `base.yml.memory_api`，不会合并两个
`memory_api` 内部字段。

因此：

- 不建议把一个内核配置拆到多个部署文件；
- 多文件更适合覆盖 `profile`、`policies` 等服务层字段；
- 内核差异配置最好集中放在同一个 `memory_api` 段。

## 10. 后端配置示例

下面配置使用 Redis、Milvus 和 Elasticsearch，同时关闭图和分层索引：

```yaml
profile: docker

memory_api:
  globals:
    vector_enabled: true
    graph_enabled: false
    rerank_enabled: true
    layers_index_enabled: false
    embedder_dim: 1024
    chunk_size: 512

    llm_model: qwen-plus
    llm_base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    llm_api_key: "${LLM_API_KEY}"

    embedder_model: bge-m3
    embedder_base_url: "${EMBEDDER_BASE_URL}"
    embedder_api_key: "${MODEL_API_TOKEN}"

  llm:
    default:
      target: dashscope

  embedder:
    default:
      target: openai

  tokenizer:
    default:
      target: jieba

  kv_store:
    default:
      target: redis
      params:
        url: "${REDIS_URL:-redis://redis:6379/0}"

  vector_store:
    default:
      target: milvus
      params:
        uri: "${MILVUS_URI:-http://milvus:19530}"
        collection: agent_memory_vectors
        dim: 1024
        metric_type: COSINE

  fulltext_store:
    default:
      target: elasticsearch
      params:
        hosts: "${ES_HOSTS:-http://elasticsearch:9200}"
        index: agent_memory_fulltext
        text_analyzer: english
```

没有显式覆盖的以下组件继续使用默认拓扑：

- `storage.default=composite`；
- `constructor.default=hybrid`；
- `retriever.default=pipeline`；
- `engine.default=in_memory`；
- `scheduler.default=in_process`。

这些组件通过具名引用自动使用已经覆盖后的 Redis、Milvus、Elasticsearch 和模型实例。

## 11. 多实例配置

同一个命名空间可以声明多个具名实例：

```yaml
llm:
  default:
    target: dashscope
    params:
      llm_model: qwen-plus

  reasoning:
    target: openai
    params:
      llm_model: reasoning-model

extractor:
  default:
    target: llm
    params:
      llm: reasoning

classifier:
  default:
    target: llm
    params:
      llm: default
```

装配结果：

```text
Extractor  -> llm.reasoning
Classifier -> llm.default
```

新增具名实例不会自动生效，必须被其他组件显式引用。仅仅注册了某个 target，也不代表该实现
已经被装配。

## 12. 运行时 ConfigSource

装配拓扑由 `Config` 和 `AssemblyContext` 决定。拓扑构建完成后，部分配置可通过
`ConfigSource` 晚绑定。

### 12.1 默认 `yaml_defaults`

```yaml
config_source:
  default:
    target: yaml_defaults
```

该实现把合并后的装配配置投影为只读的 `key -> str` 快照。

常见 key：

```text
globals.vector_enabled
prompts.extract.preference
llm.model
llm.api_key
llm.base_url
embedder.model
embedder.api_key
embedder.base_url
kv_store.url
vector_store.uri
fulltext_store.hosts
```

它不会监听 YAML 文件变化。修改文件后必须重新执行 `build_kernel()`。

### 12.2 可变 `dict`

```yaml
config_source:
  default:
    target: dict
    params:
      values:
        llm.model: qwen-plus
        llm.api_key: initial-key
        llm.base_url: https://example.com/v1
```

装配后，产品侧可以更新：

```python
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

source = kernel.config_source
if not isinstance(source, DictConfigSource):
    raise TypeError("config_source.default 不是 dict target")

source.put("llm.model", "qwen-max")
source.put("llm.api_key", "new-key")
```

下一次受支持的 LLM 调用会读取新值。

`put()` 是配置中心或产品管理侧能力，不属于 `MemoryAPI.add/search/evolve` 的业务参数。

### 12.3 `overlay`

`overlay` 先查 primary，缺失时再查 fallback：

```yaml
config_source:
  mutable:
    target: dict
    params:
      values:
        llm.model: qwen-max

  snapshot:
    target: yaml_defaults

  default:
    target: overlay
    params:
      primary: mutable
      fallback: snapshot
```

适合把配置中心的少量动态值覆盖在 YAML/defaults 快照之上。

需要注意，内置 `dict` target 在创建时也会先装载当前 `AssemblyContext` 的投影，
再用 `params.values` 覆盖。因此上例的 primary 通常已包含大部分 YAML 投影键，
fallback 主要处理 primary 中真正缺失的键；它不会把 `dict` 变成仅含 `values` 的稀疏配置源。

### 12.4 当前晚绑定范围

当前已接入运行时晚绑定的主要配置包括：

- LLM、Embedder、Reranker 的 model、api_key、base_url；
- Redis、PostgreSQL、Milvus、Elasticsearch、FS 等连接字段；
- 动态 Prompt。

以下内容仍然主要是装配期配置：

- `target`；
- 具名实例数量；
- 组件依赖关系；
- `vector_enabled`、`graph_enabled`、`rerank_enabled` 等管线拓扑开关；
- `CompositeStorage` 内部端口组合。

即使 `ConfigSource` 中存在 `globals.vector_enabled`，更新它也不会自动删除或新增已经装配好的
Recaller/IndexBuilder。此类变更需要重新装配。

### 12.5 `*.active`

代码提供 `embedder.active`、`kv_store.active`、`storage.active` 等解析能力，用于已预装实例之间
的动态选择。

但当前默认 YAML 装配没有注册通用的 `target: routing`。只有产品侧显式构造并注入
`RoutingEmbedder`、`RoutingStorage` 等包装器时，`*.active` 才能真正切换实例。

普通配置中切换实现仍应：

1. 修改对应 `default.target`；
2. 重新执行 `build_kernel()`。

## 13. PolicyManager 与 ConfigSource 的边界

以下配置属于业务策略，应交给 `PolicyManager`：

```yaml
policies:
  rerank.enabled: "true"
  lifecycle.expired_active.target: "forgotten"
  lifecycle.superseded.target: "forgotten"
  scope.require_space: "true"
```

也可以配置到内核 `globals.policies`：

```yaml
memory_api:
  globals:
    policies:
      rerank.enabled: "true"
      lifecycle.expired_active.target: "forgotten"
      lifecycle.superseded.target: "forgotten"
      scope.require_space: "true"
```

以下配置属于技术组件配置，适合 `ConfigSource`：

- 模型和凭证；
- Store 地址；
- Prompt；
- 运行时 endpoint；
- 已装配实例的 active 选择。

PolicyManager 和 ConfigSource 是两个独立机制，不应混用。

`rerank.enabled` 与 `globals.rerank_enabled` 也不是同一配置：前者是
`PolicyManager` 中的已知策略键，后者由 `PipelineRetriever` 在装配时决定是否注入
Reranker。当前实现中，运行时修改 `rerank.enabled` 不会自动重建检索管线。

## 14. 配置校验和错误时机

| 错误 | 发现时机 |
|---|---|
| 未知顶层命名空间 | `Config` 转为 `AssemblyContext` 时 |
| 具名实例缺少 `target` | 配置解析时 |
| 引用不存在的具名实例 | 递归装配该依赖时 |
| target 未注册 | Producer 构建该实例时 |
| Redis/Milvus 等必填参数缺失 | 对应 builder 装配时 |
| 外部服务不可连接 | 首次连接或 `health()` 时 |
| `*.active` 指向未预装实例 | Routing 组件解析 active 时 |

装配过程按根组件依赖递归进行。没有被任何组件引用的具名实例通常不会被构建，因此其错误
target 或缺失后端依赖可能不会在启动阶段暴露。

部署验收时应对已装配且对外暴露探活能力的组件显式执行 `health()`，
不能仅以 `build_kernel()` 成功作为外部服务可用的证明。

## 15. 使用建议与常见问题

1. 优先覆盖 `*.default`，除非确实需要多个具名实例；
2. 有状态 Store、Embedder、Tokenizer 使用具名引用，不使用匿名内联实例；
3. 覆盖同名实例时写全仍需保留的 `params` 依赖；
4. 跨组件能力开关优先放在 `globals`；
5. 部署配置只把 `memory_api` 段传给内核；
6. SDK 的 `Config.from_yaml()` 不会展开环境变量；
7. 多个部署文件之间不要拆分 `memory_api`；
8. 运行时切换凭证或连接地址优先使用 `ConfigSource`；
9. 修改 target、依赖拓扑或实例数量后重新装配；
10. 不要把字符串 `"false"` 当成布尔值 `false`。

最后一项尤其需要注意。配置系统不会统一把所有字符串转换为布尔值，部分 builder 直接使用
Python 真值判断：

```yaml
globals:
  vector_enabled: "false"  # 非空字符串，可能被当成 True
```

应写成：

```yaml
globals:
  vector_enabled: false
```

通过环境变量控制布尔开关时，需要调用侧先完成显式布尔转换，或确认对应 builder 已使用统一的
布尔解析函数。

## 16. 当前方案总结

当前配置方式可以概括为：

```text
默认对象拓扑
  + 用户按具名实例覆盖
  + Producer target 注册
  + 字符串依赖引用
  + 具名实例缓存共享
  + ConfigSource 运行时晚绑定
```

它的主要特点是：

- 默认配置即可离线运行；
- 新实现只需注册 target；
- 后端和算法实现可以独立替换；
- 通过具名引用保证构建侧和检索侧共享组件；
- 不需要集中维护一个包含所有实现的巨大工厂。

使用时需要特别理解：

- 配置同时包含实现选择、依赖注入和普通参数；
- 同名实例的 `params` 不深合并；
- 字符串既可能是普通值，也可能是实例引用；
- 一部分错误只能在递归装配或首次连接时发现；
- `ConfigSource` 只能动态修改已接线字段，不能自动重建对象拓扑；
- Factory 具名缓存是进程级类变量，同一进程内不适合并发执行多个 `build_kernel()`。
