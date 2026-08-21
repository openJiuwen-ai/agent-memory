# S08 — 配置层（Config Layer）与 ConfigSource

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `jiuwen_memory/config/` |
| 最近一次修订日期 | 2026-08-21 |
| 关联特性文档 | `docs/features/config/F01-config-source.md`；Storage 实例动态配置见 `docs/features/config/F02-routing-storage.md`；Schema 装配开关见 `docs/features/construction/F07-entity-schema-extension.md` |

## 范围 / 边界

**管什么**：

- 装配配置解析与合并：`Config` / `AssemblyContext` / `defaults` 与用户 YAML/字典的合并覆盖
- 可插拔配置来源抽象 `ConfigSource`：按稳定 key 提供晚绑定配置值
- 默认配置来源：与 YAML/`defaults.py` 对齐的内置实现
- 装配期将 `ConfigSource` 注入内核，供构建/检索/存储/共享插件在需要时取值

**不管什么**：

- 不负责业务编排（add/search/evolve）
- 不替代 `PolicyManager` 的少量已知运行时策略键（见 S03）
- 不承载调用级业务 options（见 S02 `Context.extensions` / 方法参数）
- 不做 Store 数据迁移、向量索引重建
- 不规定产品配置中心的推送/TTL/缓存失效运维接口（实现方可自管缓存，首版契约不要求 invalidate）
- **不**在未使用 `RoutingStorage` 时原地拆换同一 `CompositeStorage` 内部端口拓扑；Store 级异质切换落在各 `*_store.active` + `Routing*Store`（方案 A，F01 §2.1.5）
- **允许**装配期预装多套完整 `Storage`，经 `RoutingStorage` + `storage.active` 动态选用（F02，已落地）；**禁止**把「同实现换连接 / 只换某一 Store」误做成多套 `Storage` + `storage.active`

## 不变量

1. **默认可运行**：未注入自定义 `ConfigSource` 时，行为等价于当前 `defaults.py` + 可选用户 YAML 合并后的装配结果。
2. **A/B 两层分离**：更换 `ConfigSource` 实现类或增减预装配组件属于装配/重建（A）；在已注入来源上 `fetch` 取值属于运行时（B）。
3. **注册 ≠ 预装配**：Producer 已注册的 target 不等于进程内已有实例；运行时 `*.active` 只能指向装配期已创建的具名实例。
4. **统一 Storage 具名共享**：`storage.default` 选择统一 Storage 实现；其下层 Store 参数使用
   对应命名空间的具名引用。Kernel 与 Retriever 必须复用该 Storage 实例。
   `security.default` 同样必须从根组件显式引用，使用户的安全参数覆盖实际作用于
   `EncryptedKVStore`，不得静默退回默认密钥文件。
5. **同实现多套凭证优先晚绑定**：同一 LLM/Embedder/Reranker/Store 实现上切换 model/api_key/base_url/url/hosts/uri，须在调用/取连接路径 `fetch` 对应 key；**不得**把同构多 Key/URL 的首选做成多具名实例 + `*.active`。`*.active` 仅用于异质实现互切或产品明确要求的实例隔离。
6. **配置不进业务入参**：prompt 全文、模型名、API Key、base_url、Store 连接串、全局能力开关的写入路径不得解释自 `add`/`search`/`evolve`/`list` 的调用参数；调用侧最多传 prompt **key**（含本轮 extract/consolidate/reflect 策略选用，见 F01 决策 2.2）、`memory_type`/pipeline 等业务选择子。
7. **key 稳定、值为传输安全字符串**：`fetch` 返回的值以 `str` 为主契约；布尔/数字由消费方解析。缺失 key 的语义由方法约定（返回 `None` 或抛错），实现须文档化且默认源与自定义源一致。
8. **双侧同配置**：Embedder/Tokenizer 等构建侧与检索侧必须观察到同一 `ConfigSource` 快照语义，避免两侧模型或开关不一致。
9. **与 PolicyManager 边界**：lifecycle / `scope.require_space` 等已有策略键仍走 `PolicyManager`；六类动态配置（能力开关、prompt、模型凭证、store 端点/`active`）走 `ConfigSource`。
10. **Schema 默认关闭**：`globals.schema_enabled` 默认为 `false`，只在
    `build_kernel` 装配期决定是否注册 Schema target。它不是运行时热切换键，
    改值后必须重新装配。开关开启不得自动改写 Extractor/Evolver target。
    开关关闭却出现 Schema target 配置时必须 fail-closed，即使它们曾在同一进程注册过。

## 接口契约

### Config / AssemblyContext（既有，保留）

- `Config.from_yaml` / `from_dict` → `AssemblyContext`
- `default_context()` 与用户 context `merged`：globals 按 key 覆盖，命名空间按实例名覆盖/新增
- `ComponentConfig.get`：实例 params > globals > 代码默认

装配拓扑（选哪个 `target`、有哪些具名实例、依赖引用）仍由上述机制在 **`build_kernel` 时**确定。
`globals.schema_enabled` 是装配期扩展注册开关：为 `true` 时先注册 Schema target，
再按命名空间中显式配置的 target 解析依赖；为 `false` 时不导入该扩展。

### ConfigSource（新增）

逻辑契约（模块路径以实现为准，落在 `jiuwen_memory/config/`）：

```text
ConfigSource
  fetch(key: str) -> str | None
  health() -> None   # 可选；健康返回 None，否则抛错
```

语义：

- `key`：稳定路径，建议使用点分名（见下表）。
- `fetch`：返回当前应生效的配置值；允许实现内部缓存，但**缓存策略不属于本规约强制部分**。
- 装配：经 Producer/依赖注入进入内核；默认 target 为 ``yaml_defaults``
  （``YamlDefaultsConfigSource``，YAML/defaults 投影）。

可选扩展（非首版必选）：批量 fetch、结构化 JSON；若增加，不得破坏字符串 key 主路径。

### 推荐 key 路径（首版规范表）

**权威完整清单（改值 / 改选用分列、落地状态）见** `docs/features/config/F01-config-source.md` **决策 2.1**。下表为契约摘要：

| 类别 | 改值 key（优先） | 改选用 key（次选） |
|---|---|---|
| 能力开关 | `globals.vector_enabled`、`globals.graph_enabled`、`globals.rerank_enabled`、`globals.layers_index_enabled` | — |
| prompt | `prompts.extract.<name>`、`prompts.consolidate.<name>`、`prompts.reflect.<name>` | — |
| Embedder | `embedder.model`、`embedder.api_key`、`embedder.base_url` | `embedder.active` |
| LLM | `llm.model`、`llm.api_key`、`llm.base_url` | `llm.active` |
| Reranker | `reranker.model`、`reranker.api_key`、`reranker.base_url` | `reranker.active` |
| KV | `kv_store.url`（及 `dsn`/`db_path`/`host`/`port`/`db`/`password`，按后端） | `kv_store.active` |
| Vector | `vector_store.uri`、`vector_store.dsn` | `vector_store.active` |
| Fulltext | `fulltext_store.hosts` | `fulltext_store.active` |
| Graph | `graph_store.working_dir` | `graph_store.active` |
| Fusion | `fusion_store.uri`、`fusion_store.working_dir` | `fusion_store.active` |
| FS | `fs_store.root` | `fs_store.active` |
| Storage 实例选用（F02） | — | `storage.active`（仅当 `storage.default` 为 `RoutingStorage`） |

说明：

- 具体后端连接字段名与装配 `params` 对齐，完整表与「已接线 / 待接线」见 F01 决策 2.1。
- 对某实现无意义的 key（如 hashing 的 `api_key`）消费方必须忽略，不得当作错误。
- **消费约定**：OpenAI 兼容 Embedder/LLM、API Reranker 须在每次业务调用路径解析 `model`/`api_key`/`base_url`；连接型 Store 须在取客户端路径解析连接类 key；凭证或连接串变化时重建客户端。
- PolicyManager 键（`scope.require_space`、lifecycle 目标等）**不是** ConfigSource key。

### 同实例晚绑定（优先）与多实例切换（次选）

```text
优先——同实例晚绑定：
  装配期仅预装一套 embedder.default → target: openai
  运行期 put/fetch("embedder.api_key"|"embedder.base_url"|"embedder.model")
  → 下次 embed() 使用新凭证/端点/模型，无需第二套具名实例

次选——异质多实例 + active：
  装配期：
    embedder:
      hashing: { target: hashing, ... }
      openai:  { target: openai, ... }
    # ConfigSource 初始值 embedder.active = "hashing"
  运行期：
    fetch("embedder.active") -> "openai"
    → 仅允许解析为已存在的具名实例名
    → 未知 active → ValidationError，禁止静默落到错误实例
```

### 与统一 Storage（CompositeStorage）的关系

- `storage.default` 是 Kernel / Retriever 共享的统一入口：可以是单套 `composite`，也可以是
  产品注入的 `RoutingStorage`（F02）；下层端口仍引用各预装实例内的 `kv_store` / `vector_store` / …。
- **Store 级**：ConfigSource 的连接改值 / `*_store.active` 作用于**端口背后的 Store**（含
  `Routing*Store`，F01）。
- **Storage 级**：`storage.active` 仅当 `storage.default` 为 `RoutingStorage` 时，在已预装的完整
  `Storage` 实例间选用（F02）；二者诉求不同，勿混用。
- **EncryptedKV 为 F04 opt-in**（`258f398` 起）：`build_kernel` **不再**默认外包加密层；
  产品以 `kv_store.*.target=encrypted`（或等价）显式启用。启用时 `RoutingKVStore` 必须作为
  **raw** 包在 `EncryptedKVStore` 之内，禁止 Routing 包在加密层外。
- Store 级接线真值表见 F01 §2.1.5；Storage 实例动态配置见 F02。

### 与业务 API 的关系

| 通道 | 允许 |
|---|---|
| `ConfigSource.fetch` | 六类配置值的唯一读取路径（相对业务 API） |
| `admin_get/set/all` | 仅既有 PolicyManager 键 |
| `add`/`search`/`evolve`/`list` | 业务数据与单次 options；可含 prompt **key**、memory_type 等，不含配置机密与 prompt 全文 |

## 数据结构

本规约不新增 MemoryUnit 字段。`ConfigSource` 为横切依赖，不进入 Scope/Filter 模型。

## 错误语义

| 情况 | 期望 |
|---|---|
| 必填配置缺失且无默认 | 装配期或首次使用期失败，错误明确指向 key |
| `*.active` 指向未装配实例 | 失败，不静默回退 |
| 自定义 `ConfigSource.health` 失败 | 探活失败，不得假装配置可用 |

## 与其它 spec 的关系

| 关联 spec | 关系 |
|---|---|
| S02-memory-api | 业务 API 不承载六类配置写入；调用级 options 边界 |
| S03-control | PolicyManager 与 ConfigSource 分工 |
| S05-construction | PromptRegistry / Evolver / IndexBuilder 消费 fetch |
| S04-retrieval | 能力开关与 rerank/embedder 晚绑定 |
| S06-storage | `storage` 选择统一实现，Store 命名空间配置其下层端口；连接/`active` 晚绑定不做迁移 |
| S07-common | 插件实现与 Factory 注册；配置数据在 `jiuwen_memory/config` |
| architecture.md §13 | 可配置化分层与落点 |
