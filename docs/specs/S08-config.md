# S08 — 配置层（Config Layer）与 ConfigSource

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `jiuwen_memory/config/` |
| 最近一次修订日期 | 2026-11-10 |
| 关联特性文档 | `docs/features/config/F01-config-source.md`；Storage 实例动态配置见 `docs/features/config/F02-routing-storage.md`；Schema 抽取与更新装配见 `docs/features/construction/F08-entity-schema-extension.md`；Schema TemporalEntity 检索见 `docs/features/retrieval/F07-schema-temporal-entity.md`；Portal 设计见 `docs/features/F08-personal-memory-portal.md` |

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

## Portal 配置保存与恢复

SDK 禁用 Portal 时，记忆实例直接使用启动配置，跳过 Portal 的配置库。旧库损坏时，
也可以按启动配置继续运行。
jiuwenswarm 适配器按原有 JSON／YAML 规则解析 `config_path`，将配置内容和实际加载的
文件路径传给 Runtime。文件路径用于定位配置库，字符串中的环境变量占位符保留原样。

可信宿主提供可管理的具名 LLM、配置存储、地址策略和授权回调。模块在创建模型前先
校验并加载保存的修改，未修改的参数和依赖引用继续来自启动配置。普通 ConfigSource
只负责读取，不因为打开 Portal 就自动成为可持久写入的配置源。

内置实现用独立的 SQLite 配置库保存修改和版本。SDK 和 HTTP 共用定位规则，优先使用
已有本地绝对文档根下的 `memory/.shadow/portal-config.db`。其次按实际加载的配置文件
路径和顺序，在最后一个文件旁确定配置库。两处都不可用时，页面无法编辑配置。只传配置
字典且没有可用文档根时，自动装配会使用启动配置。可信宿主也可通过公开装配接口提供配置存储。

HTTP 只在安全配置声明固定个人身份，或宿主显式启用配置管理时访问配置库。
其他 HTTP 启动方式跳过旧库，继续用启动配置运行。若所有候选位置都无法创建配置库，
记忆服务继续运行，页面无法编辑设置。如果已有的配置库无法读取，启动会报错，
避免悄悄回退到启动值。启用统一静态加密时，内置配置管理不可用。

### 允许编辑的字段

支持的模型实现为 echo、openai、dashscope。只对宿主确认且实际创建的具名模型开放
以下字段，未知实现、锁定字段按只读原因显示：

- target、llm_model、llm_base_url、llm_api_key；
- llm_temperature、llm_max_tokens、llm_timeout、llm_max_retries；
- DashScope 的 enable_thinking。

字段键使用 llm.<name>.target 或 llm.<name>.params.<parameter>。保留实例的配置命名
空间规则；多个实例共用名称、地址、Key 的命名空间时，不允许按单实例修改共享字段。
模型地址须遵守已有 TLS 约束，并通过宿主提供的地址策略。拒绝未知键和非法值。
模型服务的协议、主机或端口变化时，服务端要求在同一次保存中提交完整网址、非空
API Key 和模型名。切换提供方也要按实际服务地址执行该规则。恢复整组启动设置时，
使用启动地址及其配套的凭据。Portal 保存 Key 时会加密记录对应的服务地址。加载时，
旧 Key 缺少地址绑定或与实际服务不匹配，或宿主锁定字段使已保存的一组设置无法使用，
则该模型的服务设置回到启动值。保存记录保留，读取接口返回 `ignored_endpoint_fields`。
提交温度或更换提供方时，按最终使用的提供方校验保存的温度。OpenAI 兼容模式为
`[0, 2]`，DashScope 为 `[0, 2)`。切换时可以同时提交新的温度。

Portal 不编辑 Prompt、Embedding 模型与维度、存储、全局检索策略或组件依赖。原
ConfigSource／PromptRegistry 的读取规则和文档 agent_llm 内置模板使用原入口。

### 保存和应用

1. 页面预览与读取版本、草稿绑定；保存时比较版本并提交整份修改。冲突、校验或写入
   失败不改变已保存配置或运行配置。恢复启动值表示删除指定覆盖后再保存、应用。
   修改或恢复模型提供方后，以最终提供方判断参数适用性；不再使用 DashScope 时，
   同步移除 enable_thinking 覆盖，不因隐藏参数残留导致恢复失败。
   保存模型字段时保留配置库中其他未知字段。托管装配继续忽略这些字段。
2. 优先级为宿主明确锁定值、允许的保存覆盖、启动输入、默认值。SDK／HTTP 在创建
   模型前恢复保存值，不改环境变量解析或未修改字段的行为。
3. 支持字段的 apply_mode 为 next_call，requires_reassembly 为 false。模块内部替换
   模型客户端，不要求重启宿主；不支持编辑的实现保持只读。
4. 一次调用使用同一版本的名称、地址、Key 和默认参数。在途调用用旧客户端完成后才
   释放；延迟创建及 new_instance=true 的具名模型也按当前完整应用配置创建。新句柄
   创建、登记与应用切换共用互斥边界，不能将旧构造参数与新端点混用。
5. 保存成功但应用失败返回 applied=false，保留原运行配置；retry_publication 只读取
   最新保存版本重试。旧请求不能使运行版本倒退。应用成功只表示本地切换完成，不表示
   已访问远端服务。默认值不覆盖单次调用明确传入的 options。
6. 刷新配置或发生冲突后，旧预览失效；草稿保留，重新比较最新值后才能提交。前端禁用
   控件不能代替后端版本、字段和权限校验。

### 配置描述与结果

`ManagedConfiguration.read` 返回以下信息。HTTP 的 `/v1/read_config` 另加
`available`，能力不可用时只返回 `{"available": false}`。

| 字段 | 含义 |
|---|---|
| version | 已保存的版本，也是下一次提交的冲突检查依据 |
| applied_version | 当前运行配置的版本 |
| apply_state | applied 或 apply_failed，按保存版本与运行版本是否一致判断 |
| fields | 字段描述，含 key、base、saved、has_override、effective、effective_known、writable、reason、secret、configured、apply_mode、requires_reassembly、options、visible_for_targets |
| instance | 存储模式、Embedding 模型／维度和文档根等只读启动信息 |
| ignored_overrides | 未识别字段和未采用的模型服务设置数量 |
| ignored_endpoint_fields | 模型地址与 Key 无法配套时，跳过的字段名 |

字段读取区分启动值、保存值和运行默认值；无法确认时 effective_known=false。
API Key 不返回 base、saved 或 effective 原文，只返回 configured 等状态。模型服务地址
只显示协议、主机和端口。
save 和 retry_publication 返回 version、applied；应用失败时不返回可能包含凭据的
任意 builder 异常正文，页面提示未应用并允许重试。

### 模型凭据

API Key 在受保护凭据存储中保存，普通覆盖只保留引用；空输入保持原值。新凭据准备
完成后，与非密配置按同一版本提交。失败不改变旧引用，清理不影响在途调用。没有可靠
凭据保存能力时仅将该字段设为只读，并提供原因。

读取、预览、错误与日志不出现 Key 原文。不借用入站单向认证库，不落明文旁路；内置
加密、密钥文件和部署保护边界见 S10 与既有 common/F04。

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
   对已迁移实例作用域命名空间的组件（当前仅 LLM），晚绑定 key 必须携带实例身份：
   `default` 使用 `<namespace>.<field>`，其他具名实例使用 `<namespace>.<instance>.<field>`。
   装配投影与消费方必须共用同一命名规则。其他命名空间的消费 builder 尚未接入该推导，
   投影不得提前生成看似可用但无人消费的 `<namespace>.<instance>.<short-field>` 键。
   显式 `config_namespace` 只从实例 `params` 读取，不从 `globals` 回退；值必须是非空字符串，
   规范化后作为覆盖命名空间，空白或非字符串在装配期抛 `ValidationError`。它是显式覆盖，
   不是避免实例间串读的必需项。
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
- `AssemblyContext.merged` 按 key 覆盖 globals，同名实例整体替换。
  内核装配有一条例外。选用 `engine.default.target: agent` 时，省略 `params` 或写入非空
  `params` 会继承未覆盖的默认依赖。该规则也适用于 `engine: {default: agent}` 简写。
  此时未显式覆盖的 `constructor.default`
  在文档模式使用 `document`，非文档模式下开启向量用 `hybrid`，关闭向量用保留真源读写能力的
  `unified`。关闭向量时，未显式覆盖的 `dedup.default` 使用 `keyword`。
  显式写 `params: {}` 或 `params: null` 仍整体替换。若默认 AgentEngine 因依赖字段缺失
  或为 null 而绕过已声明的具名默认实例，装配告警列出配置路径和实际匿名实现。
  JobFactory 仍通过具名实例查找。
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

- 具名 LLM 的端点键由实例身份决定：`llm.default` → `llm.model` / `llm.api_key` /
  `llm.base_url`；`llm.<name>` → `llm.<name>.model` / `llm.<name>.api_key` /
  `llm.<name>.base_url`。OpenAI 与 DashScope builder 在未显式配置 `config_namespace` 时
  按实例名推导该命名空间；匿名内联实例仍使用 `llm.*`。显式 `config_namespace` 只可写在
  实例 `params` 中，必须是非空字符串，不从 `globals` 回退；投影与该实例的读取都使用这个
  有效命名空间，空白或非字符串在装配期失败。不得把它当作非 default 实例正常工作的前置条件。
  `default` 是主实例的保留名：`llm.default` 使用共享 `llm.*` 键族；其他名称（包括被当作
  主实例使用的自定义名称）使用自己的实例键族，不生成别名。
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


## 视频本地媒体目录配置

`normalizer.<name>.params.local_media_roots` 是服务端配置的列表，每项仅包含 `scope`
和 `root`。`scope` 可声明 org/space/user/agent/session，org 必须为非空字符串，其他
省略维度按空串精确匹配；不支持通配符。`root` 必须是已存在的绝对本地目录，不能是
卷根目录或 UNC 网络路径。可为同一 Scope 配多个目录；无绑定时拒绝实际文件读取。
服务端必须按数据授权域分别配置，不能向所有租户共同开放含私有媒体的父目录。
匹配对象是最终落点 `RawPayload.scope`，不是认证主体。启用归属判定的部署会将落点
归一为 org/space，user/agent/session 为空，配置只填写 org/space；若配置 user 将无法
匹配。未启用归属判定时按实际落点的五维填写，仍非通配匹配。
POSIX 还校验打开根目录的设备号/inode 与装配时一致，更换根目录后须重新装配。

```yaml
normalizer:
  video:
    target: video
    params:
      local_media_roots:
        - scope: {org: acme, space: project-a}
          root: /srv/agent-memory/imports/acme/project-a
      temp_root: /tmp/agent-memory-video
```

`temp_root` 仅控制处理临时目录，不是源文件白名单。旧视频部署须先创建导入目录并新增
绑定；API 参数与正常视频处理流程保持原样。配置不能由业务 metadata 或请求体覆盖。
该准入能力不依赖隐私分级模块或其开关。
