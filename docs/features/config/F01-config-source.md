# F01 — ConfigSource：六类参数运行时动态配置抽象

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-04 |
| 影响范围 | `src/config/`、`src/api/memory_api_impl/assembly.py`、消费配置的 LLM/Embedder/Reranker/Store/PromptRegistry/检索开关路径；`docs/specs/S08-config.md`、`docs/design/architecture.md` §13、`docs/specs/S02-memory-api.md` |
| 测试基线 | 见文末「验证」；完整 key 清单见决策 2.1 |
| Refs | 借鉴密钥侧 `KeySource` 抽象模式（外部可插来源 + 默认 local/YAML） |

## 背景

商用落地时，产品界面需要让租户在不改业务调用代码的前提下配置并切换：

1. 启用哪些能力（如向量/图/精排开关）
2. 策略对应的 prompt 文本
3. Embedder 模型及相关凭证
4. Reranker 模型及相关凭证
5. LLM 模型及相关凭证
6. Store 存储组件（连接或已预装后端选用）

当前能力主要靠 **YAML + `defaults.py` 装配期合并** 固化进组件实例字段；`PolicyManager` 只覆盖少量策略键；`write`/`recall`/`evolve` 的调用级参数表达业务意图，不适合承载模型名、API Key、连接串或 prompt 全文。

密钥侧已出现同类模式：抽象「从哪取值」的接口、默认实现保证开箱可用、装配期注入、预留运行中按名取值。本特性将同一模式推广到上述六类**业务配置**，形成统一的 `ConfigSource`。

## 决策

### 决策 1：采用统一 `ConfigSource`

新增可插拔配置来源抽象（逻辑名 `ConfigSource`）：

- **默认实现**：`YamlDefaultsConfigSource`——合并 `defaults.py` 与用户 YAML/字典后的投影，**不配也能跑**，行为对齐今天的装配结果；Producer target 为 `yaml_defaults`。
- **产品实现**：HTTP/DB/配置中心等，在 `build_kernel` 装配期注入。
- **消费方**：需要晚绑定值时调用 `fetch(key)`（或等价方法），不从 `write`/`recall`/`evolve` 入参读取这些配置。

这对应两层能力（与密钥抽象同构）：

| 层 | 含义 | 典型动作 |
|---|---|---|
| **A. 换方案** | 换哪一个 `ConfigSource` 实现；换哪些 Producer 实现类被预装配 | `build_kernel` / 重建内核 |
| **B. 方案内取值** | 已注入的来源上，按 key 取最新值 | 运行中 `fetch` |

### 决策 2：六类参数分成「改值」与「改选用」，同实现多套凭证优先晚绑定

| 类别 | 运行时主路径（优先）=「改值」 | 次路径=「改选用」 | 完整 key 见 |
|---|---|---|---|
| ① 能力开关 | `fetch` 布尔/枚举 | —（无 active） | 下文 §2.1.1 |
| ② prompt 文本 | `fetch` 文本 | —（无 active） | 下文 §2.1.2 |
| ③ Embedder | 同实例 `fetch(model/api_key/base_url)` | `embedder.active` | 下文 §2.1.3 |
| ④ Reranker | 同实例 `fetch(model/api_key/base_url)` | `reranker.active` | 下文 §2.1.3 |
| ⑤ LLM | 同实例 `fetch(model/api_key/base_url)` | `llm.active` | 下文 §2.1.3 |
| ⑥ Store | 同后端 `fetch(连接类字段)` | `*_store.active` | 下文 §2.1.4 |

优先级不变量：

1. **同实现、多套 model/Key/URL/hosts：优先晚绑定（B）**——只预装一套 openai/api/redis 等实例，运行中改 ConfigSource 对应 key，下次调用/访问即生效；**不要**为此默认拆成多个同构具名实例。
2. **`*.active` 多具名实例为次选**——用于 hashing↔openai、memory↔redis、或协议不兼容必须换实现类时。
3. **注册 ≠ 预装配**：Producer 有 `openai` target ≠ 默认已装配；仓内未提供的厂商（如 SiliconFlow）若协议兼容，产品可新增注册薄封装或直接用 `target: openai` + `base_url`。
4. **未注册且协议不兼容的实现**：须产品新增 `@XxxProducer.register` 或重建内核（A）。

### 决策 2.1：六类参数完整 key 清单（改值 / 改选用）

约定：

- **改值**：同实例/同后端运行时 `ConfigSource.fetch`（或产品侧 `put`）变更的字符串 key。
- **改选用**：`*.active`，值为装配期已预装的**具名实例名**（不是 target 名）；未知名须失败。
- 装配 YAML 的 `params` 字段名（如 `embedder_api_key`）经投影可落到约定简写 key（如 `embedder.api_key`）；**运行时产品与内核消费一律以本清单的点分 key 为准**。
- 对某实现无意义的改值 key（如 hashing 的 `embedder.api_key`）消费方**忽略**，不得当错误。
- 下表「落地」列：`已接线` = 当前代码调用路径已 `fetch`；`约定/待接线` = S08 契约已定、实现按后端逐个补齐。

#### 2.1.1 能力开关（改值）

| ConfigSource key | 类型语义 | 落地 | 说明 |
|---|---|---|---|
| `globals.vector_enabled` | bool 字符串（`true`/`false`） | 约定/待接线* | 向量通道开关；未预装向量链路时仅改开关不够 |
| `globals.graph_enabled` | bool | 约定/待接线* | 图召回开关 |
| `globals.rerank_enabled` | bool | 约定/待接线* | 精排开关 |
| `globals.layers_index_enabled` | bool | 约定/待接线* | L0/L1 分层索引/召回开关 |

\*装配期已通过 `globals` / `ComponentConfig.get` 读取；演进目标是可选能力路径统一改走 `ConfigSource.fetch`（与双侧同配置一致）。**无「改选用」active key。**

不属于 ConfigSource（仍走 PolicyManager / `admin_*`），勿写入本表：

| PolicyManager 键（对照，非 ConfigSource） |
|---|
| `rerank.enabled`（历史占位，与 `globals.rerank_enabled` 边界见 S03） |
| `lifecycle.expired_active.target` |
| `lifecycle.superseded.target` |
| `scope.require_space` |

#### 2.1.2 prompt 文本（改值）

| ConfigSource key | 类型语义 | 落地 | 说明 |
|---|---|---|---|
| `prompts.extract.<name>` | 长文本 | 已接线 | `<name>` 为 yml `prompts.extract` 段下的命名 key（如 `episodic`）；调用侧只传该 name |
| `prompts.consolidate.<name>` | 长文本 | 已接线 | 巩固步 |
| `prompts.reflect.<name>` | 长文本 | 已接线 | 反思步 |

模式：`prompts.<phase>.<name>`，其中 `phase ∈ {extract, consolidate, reflect}`，`name` 由产品/yml 自由命名（非固定枚举）。**无 active。**

说明：`prompts.extract` / `prompts.consolidate` 段是 **prompt 文本目录**（有哪些命名策略、全文是什么），**不是**「默认启用哪些策略」的开关表。本轮真正跑哪些 extract/consolidate 策略由**调用级 metadata** 决定，见 [决策 2.2](#决策-22extractconsolidate-策略选用调用级非-configsource)。

#### 2.1.3 Embedder / LLM / Reranker（③④⑤）

**改值（优先，同实例）**

| ConfigSource key | 落地 | 消费方 |
|---|---|---|
| `embedder.model` | 已接线（OpenAI 兼容 Embedder） | `OpenAIEmbedder` 每次 `embed`/`health` |
| `embedder.api_key` | 已接线 | 同上；凭证变化重建 client |
| `embedder.base_url` | 已接线 | 同上；兼容 SiliconFlow 等 OpenAI 协议端点 |
| `llm.model` | 已接线（OpenAI / DashScope） | `OpenAILLM` 每次 `chat`/`health` |
| `llm.api_key` | 已接线 | 同上 |
| `llm.base_url` | 已接线 | 同上 |
| `reranker.model` | 已接线（`api` dialect） | `APIReranker` 每次 `rerank`/`health` |
| `reranker.api_key` | 已接线 | 同上 |
| `reranker.base_url` | 已接线 | 同上 |

**改选用（次选，异质多实例）**

| ConfigSource key | 落地 | 取值 |
|---|---|---|
| `embedder.active` | 已接线（`RoutingEmbedder` + `ActiveRouter`） | 已预装具名实例名，如 `hashing` / `openai` / 产品注册的 `siliconflow` |
| `llm.active` | 已接线（`RoutingLLM`） | 已预装 LLM 具名实例名 |
| `reranker.active` | 已接线（`RoutingReranker`） | 已预装 Reranker 具名实例名 |

#### 2.1.4 Store

**改值（优先，同后端连接）**——字段名与各后端装配 `params` 对齐：

| ConfigSource key | 对应后端 params | 落地 | 说明 |
|---|---|---|---|
| `kv_store.url` | redis `url` | 已接线（`RedisKVStore`） | Redis 连接串；变化则重连 |
| `kv_store.dsn` | postgres `dsn` | 约定/待接线 | Postgres KV |
| `kv_store.db_path` | sqlite `db_path` | 约定/待接线 | SQLite 文件路径；memory 后端可忽略 |
| `kv_store.host` / `kv_store.port` / `kv_store.db` / `kv_store.password` | redis 非 url 分支 | 约定/待接线 | 与 `url` 二选一风格，产品择一主路径即可 |
| `vector_store.uri` | milvus `uri` | 约定/待接线 | Milvus |
| `vector_store.dsn` | pgvector `dsn` | 约定/待接线 | PGVector |
| `fulltext_store.hosts` | elasticsearch `hosts` | 约定/待接线 | ES；值建议为可解析的 hosts 串（与 params 投影一致） |
| `graph_store.working_dir` | nano_graphrag `working_dir` | 约定/待接线 | 工作目录 |
| `fusion_store.uri` | milvus_graph `uri` | 约定/待接线 | 融合存储（若产品启用 fusion 命名空间） |
| `fusion_store.working_dir` | milvus_graph `working_dir` | 约定/待接线 | 同上 |
| `fs_store.root` | local FS `root` | 约定/待接线 | 本地文件系统根路径 |

**改选用（次选，异质后端）**

| ConfigSource key | 落地 | 取值 |
|---|---|---|
| `kv_store.active` | 约定/待接线（缺 `RoutingKVStore`） | 已预装 KV 具名实例名，如 `memory` / `redis` |
| `vector_store.active` | 约定/待接线 | 已预装 Vector 具名实例名 |
| `fulltext_store.active` | 约定/待接线 | 已预装 Fulltext 具名实例名 |
| `graph_store.active` | 约定/待接线 | 已预装 Graph 具名实例名 |
| `fusion_store.active` | 约定/待接线 | 已预装 Fusion 具名实例名 |
| `fs_store.active` | 约定/待接线 | 已预装 FS 具名实例名 |

Store 切换后旧库数据不自动迁移。`memory` 等无连接串的后端忽略连接类改值 key。

#### 2.1.5 清单外扩展规则

- 新增改值字段：优先复用上表字段名；后端独有连接参数用 ``<producer_top_name>.<params 字段名>``，并同步修订本决策与 `docs/specs/S08-config.md`。
- 多租户若采用 key 前缀方案，稳定后缀仍须符合本清单（前缀由产品 ConfigSource 剥除后再按本表消费）。
- 加密 master key **不在**本清单，走 `KeySource.fetch_key`，勿与 `*.api_key` 混淆。

### 决策 2.2：extract / consolidate 策略选用（调用级，非 ConfigSource）

结论：「本轮启用哪些抽取/巩固策略」属于**单次请求的业务选择**，已由调用 metadata 动态决定；**不纳入 ConfigSource**，也不新增「默认启用列表」类全局配置键。

| 维度 | 归属 | 机制 |
|---|---|---|
| 策略 **prompt 全文** | ConfigSource（六类之②） | `prompts.extract.<name>` / `prompts.consolidate.<name>` 可运行时改值 |
| 本轮 **跑哪些策略** | 调用级 metadata | `_extract_prompt_<strategy>` / `_consolidation_prompt_<strategy>` = 指向目录的 **prompt key**（不是全文） |

#### 行为（与当前代码对齐）

1. **YAML `prompts.extract`**：登记可用策略名 → 文本（目录）。例：同时存在 `episodic`、`preference`、`procedural` 三条文本，**并不等于**默认三选都跑。
2. **write / evolve 入参 metadata**（已实现，`construction/prompt_strategy.py`）：
   - `_extract_prompt_episodic: episodic` → 本轮跑 extract 策略 `episodic`，文本取自 `prompts.extract.episodic`（经 PromptRegistry，可再经 ConfigSource 晚绑定）。
   - 关闭 preference、开启 procedural：本轮**不要**写 `_extract_prompt_preference`，改写 `_extract_prompt_procedural: procedural`（可与 episodic 并存）。
3. **无任何 `_extract_prompt_*`**：`DynamicLLMExtractor` 回退旧 Extractor（非「按 YAML 目录全开」）。
4. **consolidate**：由透传的 `_consolidation_prompt_<strategy>`（及候选上的 `_extraction_strategy`）解析；同样只传 **key**，文本来自 `prompts.consolidate.<name>`。reflect 同理（`_reflect_prompt_*`）。

#### 与决策 3 的关系

- **允许**入参：策略名对应的 prompt **key**（metadata 前缀键）。
- **禁止**入参：prompt **全文**、以及把「全局默认启用策略表」伪装进 write/recall。

产品若要在 UI 上「默认勾选 episodic+preference」，应在**产品层**生成每次请求的 metadata；内核保持「请求写什么策略就跑什么」，避免与 ConfigSource 六类全局配置混淆。

#### 拒绝：用 ConfigSource 做「策略启用列表」

曾考虑 `extract.strategies.enabled=episodic,procedural` 一类全局键。拒绝原因：与已落地的 metadata 驱动模型重复；易和「目录里有哪些 prompt」混淆；且策略选用是**单次业务意图**，更适合调用级 options，而非租户级配置中心主路径。

### 决策 3：配置不走业务 API 入参

`write` / `recall` / `evolve` / `list` 等：

- **可以**传业务选择信息：prompt 的 **key**（含 extract/consolidate/reflect 的 `_extract_prompt_*` / `_consolidation_prompt_*` / `_reflect_prompt_*`）、`memory_type` / pipeline 名、top_k、filters 等。
- **不可以**传：prompt 全文、模型名、API Key、base_url、Store 连接串、能力开关的「全局覆盖伪装」。

产品改 **六类**配置 → 写产品配置中心 → 内核下次 `fetch` 读到新值。  
产品改 **本轮策略组合** → 改本次请求 metadata 中的 prompt key 列表（决策 2.2）。  
不需要、也不应通过记忆写入/召回接口「灌」六类配置机密或 prompt 全文。

### 决策 4：与现有三路配置的边界

| 机制 | 管什么 | 与 ConfigSource 关系 |
|---|---|---|
| YAML / `defaults.py` | 装配拓扑、默认实现、默认值快照；含 `prompts.*` **文本目录** | 默认 `ConfigSource` 的数据来源 |
| `PolicyManager` + `admin_*` | 少量已知运行时策略键（如 lifecycle 目标、`scope.require_space`） | 短期并存；六类模型/prompt/store **不**塞进 PolicyManager |
| 调用级 options / metadata | 单次请求业务参数；含 **本轮 extract/consolidate 策略选用**（prompt key） | **不**经 ConfigSource；见决策 2.2 |

### 决策 5：消费侧晚绑定约定（强制优先路径）

- **PromptRegistry**：优先 `ConfigSource.fetch("prompts.<phase>.<name>")`，缺失回落构造快照。
- **LLM / Embedder / Reranker（OpenAI 兼容与 API 类）**：在 **每次** `chat` / `embed` / `rerank`（及 health）路径上 `fetch` `model` / `api_key` / `base_url`；凭证变化时重建客户端。hashing/overlap 等无凭证实现忽略这些 key。
- **Store（连接型后端）**：在惰性取客户端/连接路径上 `fetch` `url` / `hosts` / `uri` 等；连接串变化时丢弃旧客户端并按新值重连。旧库数据不自动迁移。
- **`*.active` 路由门面**：仅当需要在**不同实现类**或产品明确要求多实例隔离时使用；不得作为同构多 Key/URL 的首选。
- **能力开关**：使用可选能力前读取开关；未预装通道仅改开关不够，须预装配或重建。

### 决策 6：本次明确不做的优化

以下内容**不纳入本特性首版设计**（留给产品侧或后续特性）：

- 运维向缓存失效 / `invalidate` 管理接口
- ConfigSource 实现内部的 TTL、推送刷新策略（由各实现自行决定；契约只要求 `fetch` 语义）
- Store 数据迁移、双写、重建向量索引的自动化

## 拒绝的方案

### 拒绝：全部塞进 `PolicyManager`

PolicyManager 是少量已知键的策略表，不适合 prompt 长文本、连接串、多租户配置树和模型凭证。

### 拒绝：通过 `write`/`recall`/`evolve` 传入配置

安全风险高、难审计、与现有 key→registry 模型冲突，且无法统一 Store/模型切换。

### 拒绝：按域拆多个 Source（PromptSource / CredentialSource / …）为首版

类型更细但装配与文档成本高；首版一个 `ConfigSource` + 稳定 key 路径足够。需要时再拆。

### 拒绝：默认强行预装配仓内全部实现

例如默认同时实例化全部 embedder（hashing/openai/bge_m3）会破坏轻量/离线部署，并引入可选依赖失败。预装配必须由产品配置显式声明。

## 产品故事（六类）

### 能力开关

产品改配置中心 `globals.rerank_enabled=false` → 下次 recall 前 `fetch` → 跳过精排。目标通道须在装配期已存在。

### prompt

- **改文本（ConfigSource）**：产品改 `prompts.extract.episodic` 全文 → 下次仍选用该 key 的 extract 使用新文本。
- **改本轮策略组合（调用级，非 ConfigSource）**：metadata 写 `_extract_prompt_episodic` + `_extract_prompt_procedural`，不写 preference → 本轮只跑 episodic 与 procedural；目录里仍可保留 preference 文本供他次启用。consolidate / reflect 同理。

### Embedder / Reranker / LLM（额度用尽换 B）

- **首选——同实例晚绑定**：只预装一套 openai（或 api reranker）实例；改 `embedder.api_key` / `llm.model` / `reranker.base_url` → 下次调用 `fetch` 即走新凭证/模型/端点。
- **次选——已预装异质实例**：hashing↔openai 等才改 `*.active` 门面选用。
- **从未注册的实现**：协议兼容则产品新增 `@register` 薄封装或 `target: openai` + `base_url`；协议不兼容须新产品实现后重建内核（A）。

换 embedding 模型后旧向量空间可能不一致；本特性不自动 reindex，由产品提示。

### Store

- **首选——同后端晚绑定**：改 `kv_store.url` / `vector_store.uri` 等 → 下次访问按新连接重连（旧数据仍在旧库）。
- **次选——换已预装异质后端**：改 `kv_store.active` 等。
- **数据迁移**：产品职责，本仓不管。

## 验证

实现基线：

- OpenAI Embedder/LLM、API Reranker：同实例改 `*.model`/`*.api_key`/`*.base_url` 后，下次调用使用新值（`tests/unit/config/test_late_binding.py`）。
- Redis 等连接型 Store：改 `kv_store.url` 后惰性客户端按新 URL 重建。
- 默认 `YamlDefaultsConfigSource` 零配置可跑；`PromptRegistry` 优先 `fetch`。
- `*.active` 对未预装实例抛 `ValidationError`（次选路径）。
- 业务 API 入参不得被解释为六类配置写入；策略选用仅允许 prompt **key**（决策 2.2）。

## 已知遗留

- 与 `PolicyManager` 键集合是否长期合并/迁移，待策略面整理时另开特性。
- 多租户「每 space 一份 ConfigSource」还是「全局一份 + key 带 space 前缀」待产品隔离模型确定后补充。
- 缓存失效运维接口不在本次范围。
- Milvus / ES 等其余连接型 Store 的 `uri`/`hosts` 晚绑定对齐 Redis 模式，按后端逐个补齐（key 已列入决策 2.1.4「约定/待接线」）。
- Store 异质 `*.active` 门面（`RoutingKVStore` 等）尚未实现，key 已列入决策 2.1.4。
