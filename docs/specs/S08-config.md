# S08 — 配置层（Config Layer）与 ConfigSource

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `jiuwen_memory/config/` |
| 最近一次修订日期 | 2026-10-30 |
| 关联特性文档 | `docs/features/config/F01-config-source.md`；Storage 实例动态配置见 `docs/features/config/F02-routing-storage.md`；Schema 抽取与更新装配见 `docs/features/construction/F08-entity-schema-extension.md`；Schema TemporalEntity 检索见 `docs/features/retrieval/F07-schema-temporal-entity.md`、[F04 安全能力与设计决策](../features/common/F04-security-interfaces-and-encryption.md) |

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
- **不**在未使用 `RoutingStoreManager` 时原地拆换同一 `CompositeStoreManager` 内部端口拓扑；Store 级异质切换落在各 `*_store.active` + `Routing*Store`（方案 A，F01 §2.1.5）
- **允许**装配期预装多套完整 `StoreManager`，经 `RoutingStoreManager` + `store_manager.active` 动态选用（F02/F07，已落地）；**禁止**把「同实现换连接 / 只换某一 Store」误做成多套 manager + `store_manager.active`

## 不变量

1. **默认可运行**：未注入自定义 `ConfigSource` 时，行为等价于当前 `defaults.py` + 可选用户 YAML 合并后的装配结果。
2. **A/B 两层分离**：更换 `ConfigSource` 实现类或增减预装配组件属于装配/重建（A）；在已注入来源上 `fetch` 取值属于运行时（B）。
3. **注册 ≠ 预装配**：Producer 已注册的 target 不等于进程内已有实例；运行时 `*.active` 只能指向装配期已创建的具名实例。
4. **全局唯一 StoreManager 具名共享（F07）**：`store_manager.default` 选择统一 manager 实现
   （配置段名 F07 起从 `storage` 更名）；全局实例由 `globals.store_manager` 指名，消费者不逐个
   声明 storage 引用，统一经 `StoreManagerProducer.resolve` 解析。其下层 Store 参数使用对应
   命名空间的具名引用；六类 `*_store` 命名空间下所有非 `default` 具名实例自动成为 manager
   命名端口（声明即端口）。
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
11. **Schema TemporalEntity 是独立装配门**：`globals.schema_temporal_enabled`
    默认为 `false`，仅决定 Retriever 是否装配时序选择器。它不自动开启
    Schema 抽取，也不改写已选 Extractor/Evolver target。硬门开启后，
    单次查询仍需 `RetrievalQuery.schema_temporal`（或兼容扩展键）显式请求，
    或同时开启 `schema_temporal_auto_enabled`，且 Parser 已有时间窗或内置
    规则能从原始 query 解析出时间窗。

## 接口契约

### Config / AssemblyContext（既有，保留）

- `Config.from_yaml` / `Config.from_yaml_str` / `from_dict` → `AssemblyContext`
  （`from_yaml_str` 接受 YAML 文本 + 按变量名的覆盖参数；`from_yaml` 读文件后复用同一实现）
- 文本入口展开 `${VAR}` / `${VAR:-默认值}`，取值优先级：显式传参 > 环境变量 > `:-` 默认值 >
  空串；显式传参值为 `None` 时视同未传（继续回落），其余标量按 `str()` 落到字符串叶子；
  `from_dict` 是纯数据入口，不展开
- 只支持单层占位符：不支持嵌套（如 `${A:-${B}}`）、默认值不得含 `}`、没有产出字面
  `${...}` 的转义写法。含占位符的叶子展开后若仍残留 `${...}` 或花括号计数不配对，
  在解析阶段抛 `ValidationError`（不静默返回错值）。接入层 `load_layer` 遵循同一规则，
  两处实现由 `tests/unit/jiuwen_memory_entry/test_config_loader.py` 的 parity 用例锁定
- `default_context()` 与用户 context `merged`：globals 按 key 覆盖，命名空间按实例名覆盖/新增
- `ComponentConfig.get`：实例 params > globals > 代码默认

装配拓扑（选哪个 `target`、有哪些具名实例、依赖引用）仍由上述机制在 **`build_kernel` 时**确定。
每个 AssemblyContext 持有独立具名缓存；同上下文显式引用才共享，新上下文即使
名称相同也不得复用旧配置的有状态实例。不得在装配入口全局 reset 破坏已有实例。
缓存属于该上下文的装配纪元，根关闭后不得复用其中已关闭的依赖。失败装配的回滚只能
清理本次新建对象，不能关闭先前借用的存活依赖；旧根关闭不能失效较新装配的缓存。
同纪元视图由装配根显式绑定生命周期共享协议；纪元与创建观察规则见 S07。此机制仅
服务装配/关闭，不将 health 故障解释为拓扑切换，也不增加 ConfigSource 热切换键。
`globals.schema_enabled` 是装配期扩展注册开关：为 `true` 时先注册 Schema target，
再按命名空间中显式配置的 target 解析依赖；为 `false` 时不导入该扩展。

Schema 构建参数中，`schema_entity_resolution_enabled` 与
`schema_entity_merge_decision_enabled` 默认均为 `true`，用于 canonical identity 的
精确名称、别名与 LLM CREATE/UPDATE 判断；`use_property_merge` 默认 `false`，只有显式
开启后才合并普通 set 属性。显式 delete 始终由 Schema Planner 消费并归档命中旧事实。

`vector_store.schema_entities` 与 `fulltext_store.schema_entities` 是 canonical Schema
Entity 的独立命名端口，避免 Entity 与普通 MemoryUnit 混索引。`schema_enabled=true`
时，IndexBuilder 维护 Entity → Property MemoryUnit ID 的 KV 派生反向索引；该索引可由
Property MemoryUnit 重建，不是事实真源。

`globals.schema_temporal_enabled` 是 Retriever 的装配期硬门。开启后，
Retriever 参数接受以下 Schema TemporalEntity 调优键：

| 参数 | 默认值 | 语义 |
|---|---:|---|
| `schema_temporal_auto_enabled` | `false` | Parser 已有时间窗或原始 query 可解析出时间窗时自动启用 |
| `schema_temporal_entity_top_k` | `20` | 独立 Entity 全文/向量路径的单路召回上限 |
| `schema_temporal_property_top_k` | `50` | Property 直达路径的召回上限 |
| `schema_temporal_property_top_n` | `25` | 直达 Property 进入晚融合的数量上限 |
| `schema_temporal_rrf_k` | `60` | Entity/Property 信号合并的 RRF 常数 |
| `schema_temporal_max_properties_per_entity` | `20` | 实体内二次裁剪的基准 Property 条目预算 |
| `schema_temporal_property_allocation_min_factor` | `0.5` | 小实体相对基准预算的下界因子 |
| `schema_temporal_property_allocation_max_factor` | `1.5` | 大实体相对基准预算的上界因子 |
| `schema_temporal_shrink_enabled` | `true` | 是否执行实体内 Property 预算裁剪 |
| `schema_temporal_property_rerank_enabled` | `true` | 是否复用 Retriever Reranker 做实体内 Property 精排 |
| `schema_temporal_direct_property_rerank_enabled` | `false` | 是否在 Property 直达路径 Top-N 前精排 |
| `schema_temporal_entity_rerank_enabled` | `false` | 是否对组装完成的实体结果做最终精排 |
| `schema_temporal_entity_rerank_max_chars` | `4000` | 单个实体送最终精排的最大字符数 |
| `schema_temporal_property_extension_step` | `3` | 命中属性前后各扩展的同属性条目数 |
| `schema_temporal_formatting_enabled` | `true` | 是否把每个 TemporalEntity 投影为一个正式结果项 |
| `schema_temporal_formatting_max_chars` | `16000` | 每个实体格式化后的独立字符上限 |
| `schema_temporal_source_fallback_enabled` | `true` | 是否保留受控 Source-first 兜底 |
| `schema_temporal_source_policy` | `missing_or_incomplete` | Source 策略：none/missing/missing_or_incomplete/always |
| `schema_temporal_source_top_k` | `20` | Source 兜底通道的最大保留数 |
| `schema_temporal_source_ratio` | `0.3` | 最终结果为 Source fallback 预留的预算比例 |
| `schema_temporal_source_max_chars` | `1200` | 单条 Source fallback 的字符上限 |

上述键是装配参数，不是业务数据；请求级的 mode、事件区间、
`knowledge_as_of`、属性白名单和归档可见性继续通过
`RetrievalQuery.schema_temporal` 表达；传输层可继续使用
`RetrievalQuery.extensions["schema_temporal"]`。

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
| 能力开关 | `globals.vector_enabled`、`globals.graph_enabled`、`globals.rerank_enabled`、`globals.layers_index_enabled`、`globals.schema_enabled`、`globals.schema_temporal_enabled` | — |
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
| StoreManager 实例选用（F02/F07） | — | `store_manager.active`（仅当 `store_manager.default` 为 `RoutingStoreManager`） |

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

### 与统一 StoreManager（CompositeStoreManager）的关系

- `store_manager.default` 是全局共享的统一入口（`globals.store_manager` 指名）：可以是单套
  `composite`，也可以是产品注入的 `RoutingStoreManager`（F02）；下层端口仍引用各预装实例内
  的 `kv_store` / `vector_store` / …。
- **Store 级**：ConfigSource 的连接改值 / `*_store.active` 作用于**端口背后的 Store**（含
  `Routing*Store`，F01）。
- **StoreManager 级**：`store_manager.active` 仅当全局 manager 为 `RoutingStoreManager` 时，
  在已预装的完整 manager 实例间选用（F02/F07）；二者诉求不同，勿混用。
- **EncryptedKV 为 F04 opt-in**（`258f398` 起）：`build_kernel` **不再**默认外包加密层；
  产品以 `kv_store.*.target=encrypted`（或等价）显式启用。启用时 `RoutingKVStore` 必须作为
  **raw** 包在 `EncryptedKVStore` 之内，禁止 Routing 包在加密层外。
- Store 级接线真值表见 F01 §2.1.5；StoreManager 实例动态配置见 F02/F07。

### 与业务 API 的关系

| 通道 | 允许 |
|---|---|
| `ConfigSource.fetch` | 六类配置值的唯一读取路径（相对业务 API） |
| `admin_get/set/all` | 仅既有 PolicyManager 键 |
| `add`/`search`/`evolve`/`list` | 业务数据与单次 options；可含 prompt **key**、memory_type 等，不含配置机密与 prompt 全文 |

## 数据结构

### PR1-1 静态加密配置

| 配置 | 契约 |
|---|---|
| globals.storage_encryption_enabled | 默认 false；bool 或 true/false 字符串，其他值拒绝；装配期，不支持热切换 |
| globals.storage_encryption_cryptography | 共享 CryptographyProvider 具名实例，默认 default；用于自动 KV/FS 包装 |
| globals.storage_encryption_kv / storage_encryption_fs | 自动包装配置，形如 `{target: encrypted, params: {}}`；经对应 Producer 构建；原始依赖和 cryptography 由策略注入，不允许 params 覆盖 |
| globals.storage_encryption_max_plaintext_bytes / storage_encryption_max_ciphertext_bytes | 自动 FS 包装的大小上限；默认 64 MiB / 0（明文上限+4 KiB），与显式 FS 参数同语义 |
| 索引/文件型 SQLite 的 params.at_rest | method=volume/backend、非空 reference、bindings 映射；绑定受保护实际位置 |

bindings 覆盖 ES hosts（ENTITY 兼容 endpoint）、Milvus uri、pgvector dsn、
nano_graphrag working_dir、milvus_graph uri+working_dir、audit/permission/shadow_index/grant_store/delegation_store SQLite db_path、markdown_store root。
统一加密开启时 Markdown 必须显式配置 root，影子 SQLite 必须显式配置 db_path
（`:memory:` 仅豁免部署证明）；默认当前目录或跨命名空间派生路径不满足位置绑定契约。
space_aware 使用 SQLite，也检查 db_path；纯内存后端和 SQLite :memory: 无需部署声明，后者的 db_path 仍须固定。
持久化需求由各插件注册的 `at_rest_locations` 配置检查器公开声明，返回字段→实际位置的
映射；普通值等价于 `AtRestLocation(value, requires_declaration=True)`。插件也可返回
`AtRestLocation(value, requires_declaration=False)` 表达无需部署证明但仍须固定的位置。
所有声明的位置都进入 ConfigSource 初始覆盖与运行时变更检查（含共享键和具名键）；
只有 requires_declaration=true 的字段要求 params.at_rest.bindings。位置描述值不能为空，
requires_declaration 必须是 bool；不同实例复用同一晚绑定键时，豁免位置也参与冲突检查。
SQLite :memory: 必须声明 db_path 位置，不能用空映射豁免连接约束。空映射仅用于没有可
切换存储位置的纯内存或关闭插件。检查器必须无构建/写入副作用，且与 builder
共用注册生命周期。替换同名 target 不继承旧检查器；缺少检查器或返回无效位置一律拒绝。
配置层不按 target 名或通用地址字段缺失推断豁免，新增后端通过插件注册扩展。
permission.routing 只引用同命名空间具名依赖，自身无落盘，其依赖仍逐实例检查。
开启后检查所有具名/L0/L1 实例；关闭时保持原有配置行为。
运营方手写声明不等于远端磁盘的密码学证明，真实卷保护由部署管理执行。

EncryptionBoundConfigSource 拒绝受保护连接初始 override 和运行中变更。当前 namespace.field
晚绑定键为实例共享，开启时这些实例必须声明相同连接/目录；其他模型/prompt 键不受影响。
改变受保护位置须重核查并重装配，不执行自动迁移。相关决策见
[F04](../features/common/F04-security-interfaces-and-encryption.md)。
统一开关开启时 KV/FS 具名端口要求 new_instance=false，避免后续依赖匿名重建 raw 而绕过保护。

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
| S04-retrieval | 能力开关、Schema TemporalEntity 装配参数与 rerank/embedder 晚绑定 |
| S06-storage | `store_manager` 选择统一实现（globals.store_manager 指名），Store 命名空间配置其下层端口与命名端口；连接/`active` 晚绑定不做迁移 |
| S07-common | 插件实现与 Factory 注册；配置数据在 `jiuwen_memory/config` |
| architecture.md §13 | 可配置化分层与落点 |


## TIME 构建与自动任务配置

`globals.hierarchy_profiles.time` 是构建配置：Snapshot→TimeSpan→Scene→Event，
TimeSpan 累计跨度 7200 秒，Scene 最大跨度 1209600 秒，Event 相似阈值 0.65；
各层 summary_mode=llm。摘录限额不截断完整事实或 LLM 输入，不能当作分组限制。
内置尾组静默为 TimeSpan 7200 秒、Scene 86400 秒、Event 259200 秒；
Scene 最大分组跨度与静默独立。该预设不包含查询参数或调度周期。

未指定 Evolver.params.hierarchy_composer 时，使用内置预设的延迟 Composer，
复用该 Evolver 的 IndexBuilder/LLM；embedder.default 在首次需要时解析。
显式引用或内联 Composer 配置优先；None/空字符串显式禁用装配。构建是否执行
仍由 policy 中 hierarchy.enabled 控制。自定义 policies 完整替换默认表，缺键关闭。

Factory.bind_named 绑定原 AssemblyContext 和创建观察者；缓存与锁归该上下文持有，
全局 reset_all 不影响旧实例，独立上下文不复用同名依赖。并发首次解析共享同一
上下文的具名实例，失败不缓存并恢复调用者上下文。
ConfigSource.get_cached 同样遵循绑定上下文，不串用后续装配的配置源。


| 配置位置 | 参数 | 默认值与含义 |
|---|---|---|
| globals.policies | hierarchy.enabled | "false"；树总开关 |
| globals.policies | hierarchy.auto_derive | "false"；与总开关共同控制后台注册及每轮执行，开启后仍须 Runtime 显式注册 |
| scheduler.default | target | 默认 in_process；自动建树须改为支持周期的 async_timer |
| scheduler.default.params | tick_interval | async_timer 默认 10 秒；任务间隔不得小于它 |
| job_factory.default.params | hierarchy_derive_interval | 1800 秒；正整数，后台运行间隔 |
| job_factory.default.params | hierarchy_derive_lookback | 604800 秒；正整数，仅筛选未挂父 Snapshot |
| job_factory.default.params | hierarchy_max_leaves / hierarchy_page_size | 5000 / 200；完整候选保护上限 / 读取分页大小，容量语义见下文 |
| job_factory.default.params | lock / hierarchy_lock_wait_ms | 可选具名锁 / 30000 毫秒；沿用显式构建互斥 |
| hierarchy_profiles.time.stage_options.TimeSpanMerger | settle_seconds | 内置 7200；自定义 profile 省略时沿用 gap_seconds |
| hierarchy_profiles.time.stage_options.SceneSegmenter | settle_seconds | 内置 86400；自定义 profile 省略时沿用 max_duration_seconds |
| hierarchy_profiles.time.stage_options.EventBuilder | settle_seconds | 259200；尾组静默 |

分组与静默参数属于构建 profile，可在 `globals.hierarchy_profiles` 内置预设或显式
Composer 的 `params.hierarchy_profiles` 配置；显式 Composer 不自动合并内置 profile。
静默仅影响自动尾组封口，显式建树不等待静默。周期/回看参数归 JobFactory，首次启用
注册时校验；双开关未开启时不解析未使用的自动任务参数。
策略可动态设置；调度间隔、回看和 profile 是装配配置，修改后重新装配并注册。

`hierarchy_max_leaves` 是完整读取的容量保护，不是分批处理数量，也不是分组切分阈值：

- 每层自动候选满足 `len(initial) + len(pending) <= max_leaves`。initial 包含当前时间内
  未挂父的合格输入（含尚未满足尾组静默条件的输入）；pending 专指 span_end 晚于本轮
  当前时间的输入。被跳过的缺时、迟到、过窗叶不占这项额度。
- 补齐候选的完整后代时，Snapshot 叶数量也受 max_leaves 约束；完整子树读取还有逐层
  子引用数量保护。父节点数量上限为 3 * max_leaves，自动水位扫描同样限制 home 中
  全部活动 TIME 父节点的数量，不只统计本轮新增父节点。
- 超限在候选收集阶段报告 FAILED，不截断后继续、不触发持续故障保护。下一周期仍会
  尝试，数量持续超限时可能连续失败；本轮此前成功持久化的下层结果保留。
- 显式建树也使用这些限额，不能保证一次重建就解除容量问题。可评估内存和读取成本后
  调高上限并重新装配，或合理划分处理范围；相交旧树仍须补齐全部后代，不得截断旧子树。

## 隐私配置

隐私策略在装配期生效，修改总开关、规则、密钥引用或认证器配置后须重建 Runtime；
不属于 `ConfigSource` 晚绑定或 PolicyManager 动态策略。与全后端静态加密总开关独立。

### 总开关与共享装配

| 配置条件 | 装配行为 |
|---|---|
| 未写 `globals.privacy_enabled`，无 `privacy` 段 | 关闭隐私能力 |
| 未写总开关，有 `privacy.default` | 启用该显式配置 |
| 总开关为 `false` | 保留策略，停止新保护和人类还原入口；不扫描或解密既有占位符 |
| 总开关为 `true`，缺少 `privacy.default` | 拒绝启动 |

总开关只接受真正布尔值。启用时 `privacy` 仅允许一个共享 `default` 实例，禁止
`new_instance=true`。没有人机认证器仍可脱敏写入及召回，但不能查看原文。开启前检查
已装配逻辑 KV 中的 `/memory/`、`/messages/`；未迁移语料、文档模式和文件监听组合拒绝
启动，不在普通读请求里改写旧数据。

### `privacy.default.target=regex`

下表字段位于 `privacy.default.params`。

| 字段 | 默认值 / 要求 | 语义 |
|---|---|---|
| `cryptography` | 必填 | 显式引用密码学依赖；失败不回退明文 |
| `vault_path` | 必填 | 独立敏感区状态路径，不注册为普通存储端口 |
| `levels` | S1/S2/S3 | 自定义等级列表；显式列表替换默认等级 |
| `rules` | 内置规则 | 每项含 `level`、`pattern` 和可选 `group`；显式列表替换整套默认规则 |
| `rules[].group` | `0` | 完整命中或有效捕获组名称/序号，确定要保护的片段 |
| `staging_ttl` | `300` 秒 | 暂存片段的正数有效期 |
| `staging_limit` | `10000` | 暂存片段正数容量上限 |
| `regex_timeout` | `2.0` 秒 | 只接受 (0, 30] 内有限数值；单次文本/元数据保护的总匹配预算 |
| `llm_enabled` / `llm` | `false` / 无 | 保留分类开关及引用；开启分类明确拒绝，引用不解析、不调用 |

默认规则覆盖大陆手机号、身份证形态、邮箱、标记凭据及私钥，不承诺全部语义隐私。
等级序号、保留秒数和匹配超时不接受布尔值；`reveal` 及总开关只接受布尔值。
规则必须引用已配置等级；重叠命中采用较高 `rank`。超时拒绝本次处理，该预算不替代
业务或 LLM 超时。可选 `[security]` 提供有界正则和 WebAuthn 依赖，选中实现时才加载。

| 等级字段 | 默认值 / 要求 | 语义 |
|---|---|---|
| `name` / `rank` | 必填且各自唯一 | 非空标识和非负整数敏感度；未命中使用配置中最低等级 |
| `label` / `description` | 空字符串 | 展示标签及说明 |
| `write` | 自定义等级缺省 `redact` | 仅 `allow`（完整直接写入豁免）或 `redact` |
| `reveal` | `true` | 允许经本次人的证明及资源授权还原，不表示免认证 |
| `sync_mode` | 自定义等级缺省 `none` | `full / none / index_only`，仅保留，TODO |
| `retention_mode` / `retention_seconds` | `keep` / `0` | `keep / purge_after_ttl` 及非负秒数，仅保留，TODO |
| `share_mode` | 自定义等级缺省 `no_share` | `shareable / internal_only / no_share`，仅保留，TODO |

内置 S1 为 `allow / reveal=true / sync_mode=full / share_mode=shareable`；S2/S3 为
`redact / reveal=true / sync_mode=none / share_mode=no_share`。均为 `retention_mode=keep`。
显式改写等级时须同步规则映射；关闭某等级 `reveal` 后相应片段继续显示为占位符。

### `human_authenticator.default.target=passkey`

字段位于该实例的 `params`；启用人类查看时同样只允许共享 `default` 实例。

| 字段 | 默认值 / 要求 | 语义 |
|---|---|---|
| `rp_id` | 必填 | 与 `origin` 的主机名完全一致，不含端口 |
| `origin` | 必填 | 固定 HTTPS origin，无路径/查询/片段/用户信息；仅 `http://localhost[:port]` 例外 |
| `credential_path` | 必填 | 人机凭据、公钥计数器及挑战的独立状态路径 |
| `challenge_ttl` | `120` 秒 | 范围 10～300 秒，一次使用，失败及过期不可重放 |
| `enrollment_invites` | `[]` | 每项为 `digest` 与 `actor`；空列表不提供新登记邀请 |

`digest` 是高熵登记码的 64 位小写十六进制 SHA-256；登记码至少 32 字符，经独立可信
渠道交给本人，不写入配置。`actor` 是服务端确定的具名 USER Scope（有 `user`、无
`agent`），不从机器 API Key、目标 Scope 或浏览器自述推导。

可选具名 `rate_limiter.human`、`workload_guard.human` 控制独立查看预算；未配置时分别
使用容量 10、补充 1/s 的令牌桶和并发 4 的信号量。限流按 HTTP 真实对端地址执行，代理
部署须评估共享对端的预算，不信任客户端转发头。

完整叠加示例见 [privacy-human-view.yml](../../examples/privacy-human-view.yml)，部署及
首次登记见[配置指南](../zh/API文档/config.md#45-隐私分级与人类查看)，协议见
[S10](S10-security.md#隐私保护与人类终端披露)。
