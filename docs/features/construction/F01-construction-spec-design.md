# F01 — 构建层实现规约（src/construction/*_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-06-24 |
| 影响范围 | src/construction/（七个算子 + 实现包）、docs/specs/S05-construction.md、src/construction/AGENTS.md |
| 测试基线 | tests/unit/construction/（82 passed，含 test_evolver_dedup.py / test_extractor.py）、examples/demo_classifier.py、examples/quickstart*.py 端到端跑通 |
| Refs | — |

> 本文是构建层的特性文档（features）——记来龙去脉与决策取舍 + 各算子实现的落地规约。接口契约见 [`docs/specs/S05-construction.md`](../../specs/S05-construction.md)；当前实现地图见 [`src/construction/AGENTS.md`](../../../src/construction/AGENTS.md)。

## 背景

记忆系统要服务 AI agent，核心诉求是「写入快、召回准、记忆不膨胀」。这背后有三组矛盾：

1. **在线时延 vs 离线深加工**：写入要在 ~250ms 内返回（agent 等待），但提取事实/升华画像/发现关联这类智能操作动辄分钟级 LLM 调用。若写入时同步做，agent 卡死；若全不做，记忆质量塌陷。

2. **真源唯一 vs 多形式索引**：检索需要关键词/向量/图/文档多种召回通道，每种索引各擅其场。但多份索引一旦与真源不一致，召回就错位。如何保证「删索引不丢数据」？

3. **可演进 vs 可审计**：记忆会过时、会矛盾、会重复——需要持续去重/冲突消解/遗忘。但这些智能操作若与写入耦合，写入路径变得脆弱且昂贵；若完全不维护，记忆膨胀失控。

构建层（Construction，E 层）就是为了消化这三组矛盾而存在的层。它承接接入层产出的 `MemoryUnit`，在其上挖掘分层记忆、构建多形式索引、持续自演进维护记忆质量，是整个记忆系统的「落盘边界」——唯一可写 storage 的层。

## 决策：各算子实现规约

七个算子各走 `@XxxProducer.register("target")` 自注册，`construction.bootstrap.register_constructors()` 在装配前统一触发（幂等）。装配按配置树 `dep(...)` 选实现，算子内部不自构造依赖。所有实现共享以下设计前提：

### 共享设计前提

1. **双通道**：写入路径只做 Classifier 快速分类 → KVStore 落盘 → IndexBuilder 建索引（< 250ms）；提取/去重/冲突消解/遗忘放 Evolver。Classifier 是写入路径唯一智能操作例外（tier/tags 是索引前提，规则优先 ~80% 覆盖、~0 LLM）；写入直接 ADD 不去重，由 SUPERSEDE/UPDATE 补偿形成闭环。Evolver 不调 Classifier——派生 unit 的 tier 由 Extractor/Abstractor 预设，遗忘策略读写入路径产出的 importance/freshness metadata。

   > **演进触发方式已调整**（见 [`docs/features/api/F02-write-infer-extract.md`](../api/F02-write-infer-extract.md)）：默认路径不再由 `control.Scheduler` 在 write 后自动提交 background EXTRACT（`InProcessScheduler` 同步执行下"自动提交"实为同步阻塞，与异步初衷相悖）；演进由调用方显式 `evolve()` 触发，或经 `write(metadata={"infer":"true"})` 同步走 EXTRACT。双通道"写入轻量、提取重"的立场不变——同步抽取是显式 opt-in 开关，非默认行为（不违背下方拒绝方案 A）。
2. **全部可重建**：`MemoryUnit` 序列化存 KVStore 是唯一真源；向量/关键词/图索引全是从真源派生的可重建数据。`IndexBuilder.rebuild()` 从 KVStore 全量扫描重建，是非破坏式保障——存储故障恢复、换 embedding 模型都靠它。
3. **接口与实现严格分离**：顶层 `.py` 纯抽象（不 import `*_impl/`），实现经 `@Producer.register` 自注册。端侧用规则/小模型（keyword classifier、hashing embedder）、云侧用强 LLM，只改配置不改代码。共享插件（Embedder/Tokenizer/Chunker/FeatureExtractor）须与 retrieval 侧同一实例，装配按字段名缓存保证同实例。
4. **去重召回与判定分离**：去重召回抽象成独立 `Dedup` 接口，Evolver 只做阈值 + LLM 判定。装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`——只配倒排时去重仍可用（向量路在 fulltext-only 下 VectorStore 恒空会失效）。两路 score 同为 0~1 量纲（cosine / 词重叠率），medium/high 阈值统一复用。
5. **SUPERSEDE 不经 LifecycleManager**：Evolver 标记旧版 SUPERSEDED 直接 `KVStore.update`，不经 control 层 LifecycleManager（construction → control 严禁）。版本链由 `supersedes` 字段记录，非破坏式、保留血缘。
6. **去重不用 Reranker**：LLM 直接做最终语义判定，Reranker 中间层不增精度只增开销。若未来需降 LLM 调用成本，可考虑在 LLM 前加 Reranker 过滤器。

### Extractor（`extractor.py` · `ExtractorProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `keyword` | `KeywordExtractor` | `chunker`（注入） | `list[MemoryUnit]` | 用 Chunker 把原始 active unit 内容切成 chunk，每个 chunk 提升为 SEMANTIC 派生 unit（provenance 回指、`extracted` 标签）；可复现占位，只处理原始 active 单元避免反复再抽取 |
| `llm` | `LLMExtractor` | `llm`、`feature_extractor`（`dep`） | `list[MemoryUnit]` | 4 Phase：预处理→**批量** LLM 提取（全部 unit 拼一个 prompt 一次调用，每条带 `[ID:unit_id]` 标记，LLM 输出裸 `source_id` 回指来源，解析时校验 source_id 在本批 unit 内；同 source 同主题合并成一条自包含陈述）→特征富化→构建 unit（provenance=[source_id]）；temperature=0 幂等 |

### Abstractor（`abstractor.py` · `AbstractorProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `concat` | `ConcatAbstractor` | — | `list[MemoryUnit]` | 把同 scope 的多条 EPISODIC 拼接成一条 SEMANTIC 概括（content 拼接、provenance 汇集）；轻量占位 |
| `llm` | `LLMAbstractor` | `llm`、`feature_extractor`（`dep`） | `list[MemoryUnit]` | 4 Phase + 三路径（SUMMARY→SEMANTIC / PATTERN→PROCEDURAL / PORTRAIT→CORE），按 tier/数量路由 AbstractionTarget，按组构建 target-specific prompt；temperature=0 幂等 |

### Associator（`associator.py` · `AssociatorProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `keyword` | `KeywordAssociator` | `feature_extractor`（注入） | `list[Relation]` | 按「共享关键词」发现关联：FeatureExtractor 抽各单元关键词，两两比较 Jaccard，超阈值产 `similar_to`；轻量占位 |
| `llm` | `LLMAssociator` | `feature_extractor`、`embedder`、`llm`（`dep`） | `list[Relation]` | 三层发现：L1 实体+向量相似→L1/L2 候选生成→L3 LLM 验证与深度发现；六类关系（caused_by/refers_to/corefers/follows_from/similar_to/contradicts），非全量 N² |

### Classifier（`classifier.py` · `ClassifierProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `keyword` | `KeywordClassifier` | — | `list[MemoryUnit]`（原地改） | 关键词启发式判 tier（偏好→SEMANTIC / 流程→PROCEDURAL / 其余 EPISODIC）+ 追加一个主题 tag；可复现占位，仅两维 |
| `llm` | `LLMClassifier` | `llm`、`feature_extractor`（`dep`） | `list[MemoryUnit]`（原地改） | 4 Phase 规则优先 + LLM 深度补充，五维（tier/topic/importance/confidence/freshness）；来源标 `classify_source`（rule/default/llm/provenance）；~80% 规则覆盖、~0 LLM |

### IndexBuilder（`index_builder.py` · `IndexBuilderProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `fulltext` | `FulltextIndexBuilder` | `fulltext_store`（`dep`） | 倒排索引 | `Document(id=unit.id, text=unit.content 整篇不切片)` → FulltextStore；自留 `id→scope` 映射供 remove 定位 |
| `vector` | `VectorIndexBuilder` | `vector_store`、`kv`、`chunker`、`embedder`（`dep`） | 向量索引 | Chunker 切片 → Embedder → `VectorRecord(id={unit.id}-{chunk.id})` → VectorStore；KVStore 维护 chunk_id 跟踪供 update/remove |
| `hybrid` | `HybridIndexBuilder` | `fulltext_store`、`vector_store`、`kv`、`chunker`、`embedder`（`dep`） | 倒排+向量 | 组合 fulltext + vector 两个子 builder（默认实现）；build/update/remove 委托两者 |

### Dedup（`dedup.py` · `DedupProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `vector` | `VectorDedup` | `vector_store`、`embedder`、`kv`（`dep`） | `list[(MemoryUnit, score)]` | Embedder → VectorStore.search（cosine）；过滤自身（id 前缀）、解析 `{unit_id}-{chunk_id}`、按 unit 聚合取 max；装配在 `vector_enabled=True` 时选 |
| `keyword` | `KeywordDedup` | `fulltext_store`、`kv`（`dep`） | `list[(MemoryUnit, score)]` | FulltextStore.search（词重叠率，0~1 与 cosine 同量纲）；Document.id=unit.id 恒等无需解析、tier 过滤在加载后；装配在 `vector_enabled=False` 时选 |

> 两路 score 量纲统一，Evolver 的 medium/high 阈值直接复用。实现内部异常吞掉返回空列表，不阻断演进。

### Evolver（`evolver.py` · `EvolverProducer`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `orchestrating` | `OrchestratingEvolver` | `extractor`、`abstractor`、`associator`、`index_builder`、`kv`、`graph`、`dedup`、`llm`（`dep`） | `EvolveResult` | 四模式编排（EXTRACT/CONSOLIDATE 走去重，ASSOCIATE 走冲突消解，FORGET 走遗忘筛选）；持有 `Dedup` 不持 VectorStore/Embedder；SUPERSEDE 直接写 KV 不经 LifecycleManager |

> 各算子的处理流水线细节（4 Phase、五维体系、三层发现等）见各 `*_impl/llm_*.py` 实现文件的模块 docstring（代码层自含）。

## 拒绝的方案

### 方案 A：写入路径同步做提取/去重

**描述**：write 时直接调 Extractor 抽取 + Dedup 去重，一步到位，不用 Background 补偿。

**拒绝原因**：
- 写入时延失控——一次 write 触发多次 LLM 调用（抽取 + 去重判定），agent 等待分钟级
- 写入路径变脆弱——LLM 不可用时 write 直接失败，而数据面本应始终可用
- 闭环反馈失效——没有「写入先 ADD → 后台修正」的闭环，矛盾/冗余只能靠写入时一次性发现，遗漏无补偿

> 这里拒绝的是"**默认**同步抽取"。后续 [`F02-write-infer-extract`](../api/F02-write-infer-extract.md) 新增的 `metadata["infer"]=="true"` 是**可选 opt-in 开关**——调用方按场景显式承担同步时延代价（如外接记忆 provider 的 `sync_turn` 契约需要"写完即可召回派生事实"），不违背本方案"默认不同步"的立场。

### 方案 B：索引与真源同库，不区分派生

**描述**：MemoryUnit 和索引记录都存 KVStore，不分真源/派生。

**拒绝原因**：
- 失去可重建保障——索引坏了没法从真源重算（真源和索引混在一起，删索引等于删数据）
- 换索引结构/模型要迁移数据，而非重建
- 多形式索引（向量要 ANN、关键词要倒排、图要遍历）各需专门后端，硬塞 KV 牺牲各索引的检索效率

### 方案 C：去重召回硬编码在 Evolver（不抽象 Dedup 接口）

**描述**：Evolver 直接持有 VectorStore/Embedder，`_dedup_single` 里写死 Embedder → VectorStore.search。

**拒绝原因**：
- 只配倒排索引时去重完全失效（VectorStore 恒空）——这是实际踩过的坑
- 新增召回路要改 Evolver 内部逻辑，违反开闭原则
- Evolver 职责膨胀——既做判定又管召回细节，难维护

### 方案 D：SUPERSEDE 经 LifecycleManager

**描述**：Evolver 调 control 层 LifecycleManager 标记旧版 SUPERSEDED。

**拒绝原因**：
- 违反 construction → control 严禁的铁律，构建层依赖治理层
- Evolver 已有 KVStore 和 existing_unit，绕路调 LifecycleManager 是多余间接层
- LifecycleManager 是 control 层治理面能力（定时扫描/策略驱动），Evolver 的即时版本链标记不需要它

### 方案 E：Classifier 也放 Background，写入路径纯落盘

**描述**：写入路径只 KVStore.insert + IndexBuilder.build，分类完全交 Background。

**拒绝原因**：
- tier/tags 是索引构建前提——写入时无分类，索引建完要 Background 补分类并重建索引，代价远高于写入时一次规则分类
- 规则分类 ~0 LLM 调用、< 50ms，对写入时延影响可忽略
- LLM 深度通道仍可选在写入路径触发（`llm_enabled=True`），但规则兜底保证 LLM 不可用时分类仍可用

## 验证

### 单元测试

- `tests/unit/construction/test_evolver_dedup.py` — 去重决策四态（ADD/UPDATE/SUPERSEDE/NOOP）+ 降级场景（Embedder/VectorStore/LLM 失败）+ 自身过滤。其中 supersede/update/json-fallback 三例用 `dedup_high_similarity=1.01` 抬高短路阈值，强制走 LLM 判定分支（默认 `≥high(0.9)→NOOP` 短路会跳过 LLM，测不到 LLM 判定路径）
- `tests/unit/construction/test_extractor.py` — Extractor 4 Phase；`test_extract_batch` 验证批量提取一次 LLM 调用返回全部候选、`source_id` 回指正确源 unit
- `tests/unit/construction/test_e2e_evolution.py` — 演进闭环端到端

### 端到端验证

- `examples/quickstart.py` / `quickstart_bge_m3.py` — write → recall → get → update → evolve(EXTRACT/CONSOLIDATE/ASSOCIATE/FORGET) 全链路跑通
- `examples/demo_classifier.py` — Classifier 五维分类 + 来源追踪 + tier 优先级链 + provenance 重分类，端到端验证写入路径分类功能

### 关键场景验证

| 场景 | 验证方式 | 结果 |
|------|---------|------|
| 只配倒排时去重仍可用 | `vector_enabled=False` 装配，`KeywordDedup` 召回命中现有记忆 score=1.000 | ✅（修复前 score=0.000 完全失效） |
| 写入路径低时延 | quickstart write 链路（分类+落盘+建索引）秒级内完成 | ✅ |
| 索引可重建 | `IndexBuilder.rebuild()` 从 KVStore 全量重建 | ✅（契约保障，单测覆盖） |
| 端侧降级 | 无 LLM/无 spaCy 时 classifier 规则兜底、demo 仍可跑 | ✅ |

## 已知遗留

### 实现层面

1. **InProcessScheduler 同步执行**：write 触发的后台 EXTRACT 在当前实现里是同步执行（非真异步线程），write 时延受 EXTRACT 的 LLM 调用拖累。这是 control 层实现问题（S03 范围），构建层契约不受影响，但生产部署需换真异步 Scheduler。

2. **InMemoryFulltextStore 不消费 filters**：`KeywordDedup` 的 tier 过滤只能在加载 unit 后做（召回后过滤），无法下推到 Store 层。若未来 FulltextStore 后端支持 filters 下推，`KeywordDedup` 可优化为召回前过滤。

3. **去重 access_frequency 未实现**：Classifier 的 importance 计算中 `access_frequency` 项（被召回 M 次 → +0.05×min(M,10)）当前未实现——检索层尚无访问频次统计数据源。待检索层 recall 统计对接后补齐。

4. **部分算子仍是占位实现**：M1 阶段部分算子（如 KeywordExtractor/KeywordAssociator）是轻量占位，真实 LLM 驱动实现（llm_*）需配 LLM API。装配按配置切换。

