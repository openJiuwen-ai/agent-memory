# Agent Memory Construction（构建层）

**规约文档**：[S05-construction.md](../../docs/specs/S05-construction.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

接收接入层产出的 `MemoryUnit`，调用 `storage` 落盘，在其上构建多形式索引。可插拔算子由 Extractor、Abstractor、Associator、Classifier、IndexBuilder、Dedup、LayerAnnotator 与 Evolver 组成；官方实现为 `OrchestratingEvolver` / `DynamicEvolver`，Schema 可选扩展提供隔离子类。

> 契约（接口签名/数据结构/不变量）见 [`docs/specs/S05-construction.md`](../../docs/specs/S05-construction.md)；设计理念与决策取舍（双通道/演进闭环/依赖关系）见 [`docs/features/construction/F01-construction-spec-design.md`](../../docs/features/construction/F01-construction-spec-design.md)。本文件只记当前实现地图与本地约束。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | ConstructionOperator 基类 + OperatorType 枚举 |
| `extractor.py` | Extractor 接口：信息提取（低抽象粒度） |
| `abstractor.py` | Abstractor 接口：抽象与精炼/升华（高抽象粒度） |
| `associator.py` | Associator 接口：关联分析（实体共指/因果链/引用关系） |
| `classifier.py` | Classifier 接口：多维分类（认知角色/主题/重要度） |
| `prompt_strategy.py` | 动态抽取/巩固/反思 prompt metadata 的解析与传递（key 透传） |
| `prompt_registry.py` | PromptRegistry：从 yml `prompts` 段加载命名 prompt，按 phase+key 查询 |
| `index_builder.py` | IndexBuilder 接口：多形式索引构建（文档/关键词/向量/图） |
| `dedup.py` | Dedup 接口：去重召回（向量/倒排两路）+ DedupProducer 工厂 |
| `evolver.py` | Evolver 接口：记忆自演进（抽取/关联/巩固/遗忘）+ EvolveMode + EvolveResult |
| `layer_annotator.py` | LayerAnnotator 接口：分层披露标注（L0/L1 写入 unit.layers）+ LayerAnnotatorProducer 工厂 |
| `extractor_impl/` | Extractor 实现目录（keyword / llm / dynamic_llm / entity_schema 可选扩展） |
| `abstractor_impl/` | Abstractor 实现目录（concat / llm） |
| `associator_impl/` | Associator 实现目录（keyword / llm） |
| `classifier_impl/` | Classifier 实现目录（keyword / llm） |
| `index_builder_impl/` | IndexBuilder 实现目录（fulltext / hybrid / unified / vector）；`unified` 仅按 Scope 直调统一 Storage 的 add/update/delete，不派生检索索引，调用方不得预先写入同一 unit；vector/fulltext 各扩展 L0/L1 分层索引（独立 store 分表，store None 跳过），详见 F01-memory-layer |
| `layer_annotator_impl/` | LayerAnnotator 实现目录（keyword / llm）；evolver 抽取后调用，对超阈 content 标注 L0/L1 |
| `dedup_impl/` | Dedup 实现目录（vector / keyword） |
| `evolver_impl/` | Evolver 实现目录（orchestrating / dynamic；可选 schema_orchestrating / schema_dynamic，并含 Entity Identity、Registry、Property Merge） |
| `bootstrap.py` | 统一触发所有构建算子注册（含 dedup_impl） |
| `schema_bootstrap.py` | 显式注册隔离 Schema Extractor/Evolver，不修改官方 bootstrap |

## 构建链路

```
接入层产出 MemoryUnit
  ↓
1. Classifier.classify(units) → 打上 tier/主题/重要度标签
  ↓
2. Storage.add(scope, units) → 真源落盘
  ↓
3. IndexBuilder.build(units) → 构建多形式索引
     │
     ├─ Chunker.chunk → chunks → Embedder.embed → VectorRecord → VectorStore
     ├─ Tokenizer.tokenize → Document → FulltextStore
     ├─ FeatureExtractor → Node → GraphStore
     └─ Associator.associate → Edge → GraphStore（后台 ASSOCIATE 模式）
  ↓
4. Scheduler.submit(scope, EXTRACT, BACKGROUND) → 提交演进任务
  ↓
（后台）Evolver.evolve(units, mode):
  EXTRACT     → [orchestrating] _evolve_extract: extract→annotate→_dedup_batch(判定+落盘)
              → [dynamic]     _evolve_extract: extract→consolidate(判定)→reflect→落盘
  CONSOLIDATE → _evolve_consolidate: abstract→annotate→_dedup_batch
  ASSOCIATE   → _evolve_associate: associate→冲突消解→图索引 Edge
  FORGET     → _evolve_forget: 遗忘候选筛选→lifecycle 标记→IndexBuilder.remove
```

`OrchestratingEvolver` 与 `DynamicEvolver` 平级（同属 `evolver` 顶层命名空间，注册名 `orchestrating` / `dynamic`）。`DynamicEvolver` 继承 `OrchestratingEvolver`，只覆盖 `_evolve_extract` 走动态 prompt 四步；其余三模式继承父类。装配或 pipeline profile 选哪个 evolver 实例即启用哪条 EXTRACT 路径。

Schema 可选扩展通过 `jiuwen_memory.schema.assemble_schema()` 注册 `entity_schema`、
`schema_orchestrating` 和 `schema_dynamic`。Schema 非 procedural EXTRACT 先持久化 Source；
Entity Identity 后写隐藏 Entity Registry，再由 Property Merge 处理 property MemoryUnit。

## 行为铁律

0. **派生 metadata 按来源合并**：单源复制 `user_metadata`，多源只保留相等交集；
   `infer` / `procedural` / `middle` 不传播。IndexBuilder 分别投影
   `system_metadata.<key>` 和 `user_metadata.<key>`。

1. **落盘由本层负责**
   接入层产出 `MemoryUnit` 后，真源写入由本层调用 Storage 的写接口完成。接入层禁止落盘。

2. **索引是可重建派生**
   索引（向量/关键词/图/文档）全部可从真源（KVStore 中的 MemoryUnit）重建。`IndexBuilder.rebuild()` 是非破坏式保障——删索引不丢数据。

3. **provenance 回指来源**
   派生记忆单元（Extractor/Abstractor 产出）的 `provenance` 字段记录由哪些 unit 演进而来，保证可重建、可审计回溯。

4. **构建与存储解耦**
   算子负责构建逻辑（生成索引投影：Chunk → VectorRecord/Document/Node），持久化由注入的 Store 承担。算子不依赖具体后端。

5. **scope 原生隔离**
   构建索引记录时把来源 `MemoryUnit.scope` 落到记录的专用 `scope` 字段（`VectorRecord.scope` / `Document.scope` / `Node.scope`），使检索得以按 scope 原生隔离。

6. **Evolver 四阶段独立**
   `EvolveMode.EXTRACT`（信息提取）/ `ASSOCIATE`（关联分析）/ `CONSOLIDATE`（冲突消解/近重复融合）/ `FORGET`（遗忘/降权）四阶段独立，可单独触发。索引维护不作为 evolve 模式。

7. **去重召回与判定分离**
   `Dedup` 接口只管召回（向量化/分词 → Store.search → 加载 → 聚合取 max），判定
   （ADD/UPDATE/SUPERSEDE/NOOP）与落盘归 Evolver 实现：
   `OrchestratingEvolver._evolve_extract`（legacy）由 `_dedup_batch` 耦合判定与落盘；
   `DynamicEvolver._evolve_extract`（dynamic）在 consolidate 只判定，reflect 后统一落盘。
   装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`，保证 fulltext-only 下去重仍可用。

8. **构建层不依赖 control**
   SUPERSEDE/FORGET 标记由 `OrchestratingEvolver`/`DynamicEvolver` 直接通过
   `KVStore.update` 完成，不经 `LifecycleManager`（construction → control 严禁）。

9. **Dedup 与 IndexBuilder 共享底层 Store**
   去重召回检索的是已索引内容，`Dedup` 实现取的 `VectorStore`/`FulltextStore` 必须与 IndexBuilder 写入的是同一实例（按字段名缓存命中）。

10. **L0/L1 分层索引分表且 store None 跳过**
    `unit.layers.l0`/`l1` 非空时，VectorIndexBuilder/FulltextIndexBuilder 对整段文本（不切片）建独立 store 索引（record id 向量=`{uid}-layer-l0`/`-layer-l1`、全文=`{uid}:l0`/`:l1`，与 content 的 chunk id 不冲突），metadata `content_layer`="l0"/"l1"，content 表 chunk record 补 `content_layer="l2"`。物理分表不混 content。`vector_l0`/`vector_l1`/`fulltext_l0`/`fulltext_l1` 任一为 None 则该层跳过（不报错、不建空记录）。update 先删旧分层 record 再按新 layers 重建（SUPERSEDE 不残留），remove 使用 MemoryUnit 自带 Scope 幂等删除。详见 F01-memory-layer。

11. **动态抽取格式在实现内收敛**
    `_extract_prompt_<strategy>` 支持任意非空策略名，其值是引用 yml `prompts.extract` 段的
    prompt **key**，运行时由 `PromptRegistry` 按 `phase=extract + key` 查真实文本作为 system
    prompt 发送；registry 未配置或 key 缺失时回退把值本身当文本用（兼容内联文本）。不由内核
    追加输出契约。`DynamicLLMExtractor` 默认按 JSON 解析，子类可覆盖 `parse_response` 支持
    XML 等格式，但必须在该方法内转换为 `list[MemoryUnit]`；格式相关中间结构不得传给 Evolver。

12. **consolidate 只判定不落盘**
    `DynamicEvolver` 的 consolidate 步只产出 `ConsolidateDecision`（候选 + 决策 +
    已有记忆 + 相似度），不调 KVStore / IndexBuilder；落盘在 reflect 之后统一执行。
    reflect 默认 no-op，子类可覆盖 `_reflect_step` 在落盘前做反思修正。

13. **prompt key 而非文本**
    metadata 只写 prompt 的 **key**（引用 yml `prompts` 段的命名 prompt），不内联 prompt 文本。
    运行时由 `PromptRegistry` 按 `phase + key` 查真实文本。三步
    （extract/consolidate/reflect）共享同一份 `prompts` 配置与查询规则；
    Extractor 和 Evolver 的 builder 分别构造并注入 registry。

14. **过滤索引投影与真源语义对齐**
    Vector/Fulltext IndexBuilder 原样复制业务 metadata，再由真源系统字段覆盖保留 key；
    时间写 epoch 毫秒，开放 `t_invalid=None` 在索引中投影为 `T_INVALID_OPEN`，
    未知事件时间 `t_event=None` 恒写哨兵 `T_EVENT_UNKNOWN=0`（F07 派生常为此值）。
    真源仍保留 None，禁止为适配后端改写 MemoryUnit；`memory_filter._field_value`
    对 `t_event` / `t_invalid` 的 None 同步投影为对应哨兵，使后置复核与下推不分叉。

14. **抽取与分层优先保证完整性**
    派生 L2 只保存紧凑陈述，通过 `source_ref`/`provenance`/`evidence` 回指来源；坏候选
    与坏子批分别隔离，整次抽取无可用候选时才显式失败；动态抽取可隔离单策略失败，但
    全部策略失败必须向上抛错。LLM 分层的重复、越界或遗漏 ID 拒绝整批，单条长度异常
    只跳过该条，其余合法结果在结构校验完成后写入。

## 与其他子目录的边界

**本模块管**：
- 真源落盘（调用 KVStore）
- 信息提取与抽象升华（Extractor / Abstractor）
- 关联分析（Associator）
- 多维分类（Classifier）
- 动态 prompt 四步演进（DynamicEvolver：extract→consolidate→reflect→落盘，Evolver 的子类实现）
- 多形式索引构建（IndexBuilder）
- 去重召回（Dedup）
- 记忆自演进（Evolver）

**不管**：
- 鉴权（归 `api`）
- 检索（归 `retrieval`；去重召回不经 retrieval 的 Recaller，`Dedup` 直接调 Store）
- 存储实现（通过注入的 Store 抽象间接调用）
- 共享插件实现（Chunker/Embedder/Tokenizer/LLM 等归 `common`）

## 本地约束

1. 所有 Operator 必须实现 `operator_type()` 和 `health()`（继承自 `ConstructionOperator`）。
2. 算子实现通过 `@XxxProducer.register("name")` 自注册。
3. IndexBuilder 必须实现四个方法：`build`（新建）/ `update`（更新）/ `remove(units)`（按 MemoryUnit 自带 Scope 删除）/ `rebuild`（全量重建）；不得维护仅按 unit id 的单值 Scope 缓存。`unified` 实现须按 Scope 分组，分别直调 Storage 的 `add` / `update` / `delete`，只可由未预先写入同一 unit 的调用方使用；当前 InMemoryEngine/CloudEngine 标准路径会先写 Storage，不能直接将其 profile 替换为 `unified`。
4. Evolver 返回 `EvolveResult`（created_ids / updated_ids / superseded_ids / forgotten_ids）。
5. Dedup 必须实现 `recall(candidate) -> list[(MemoryUnit, score)]`；实现内部异常吞掉返回空列表，不阻断演进。
6. 算子内部调用共享插件（Chunker/Embedder/Tokenizer/FeatureExtractor/LLM）必须使用注入的实例，不自行构造。
7. `DynamicEvolver` 继承 `OrchestratingEvolver`，复用父类全部依赖（extractor/abstractor/associator/index_builder/kv/graph/dedup/llm/layer_annotator），额外注入 `PromptRegistry`；只覆盖 `_evolve_extract`，其余三模式继承父类。
8. `DynamicEvolver` 与 IndexBuilder/KVStore/Dedup 必须使用同一 profile 的共享实例；pipeline profile 选 evolver 实现名（`orchestrating` / `dynamic`）即切换 EXTRACT 路径。
9. `DynamicLLMExtractor` 子类只覆盖 `parse_response` 完成响应解析与构建；策略遍历、fallback、
   `_extraction_strategy` 标记和 consolidation/reflect prompt key 透传由基类统一执行。
10. `PromptRegistry` 由装配从 `ctx.globals["prompts"]` 加载；`DynamicEvolver._build` 用 `config.get("prompts")` 取该段（params 无 prompts 时回退 globals）构造注册表。
