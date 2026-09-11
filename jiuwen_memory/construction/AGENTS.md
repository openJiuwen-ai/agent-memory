# Agent Memory Construction（构建层）

**规约文档**：[S05-construction.md](../../docs/specs/S05-construction.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

接收接入层产出的 `MemoryUnit`，统一经 `IndexBuilder` 交付本体并构建多形式索引。
可插拔算子由 Extractor、Abstractor、Associator、Classifier、IndexBuilder、Router、
Dedup、LayerAnnotator、HierarchyComposer 与 Evolver（默认 `OrchestratingEvolver`、动态四步
`DynamicEvolver`，以及显式启用的 `SchemaOrchestratingEvolver`）组成。

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
| `router.py` | Router 接口与判定表：按归属坐标判定条目落哪个空间，产出候选空间集合与收窄维标签；另含归属坐标的入口校验 `reject_kernel_coords` 与折算 `narrow_dims_of`（后者入参须为已以身份覆盖过内核三项的坐标，本层不接收 `identity`）。内核三项坐标名 `KERNEL_COORD_KEYS` 在 `common/type_def/scope.py`，本模块引用 |
| `dedup.py` | Dedup 接口：去重召回（向量/倒排两路）+ DedupProducer 工厂 |
| `evolver.py` | Evolver 接口：EvolveRequest、五种内部 EvolveMode 与 EvolveResult；HIERARCHY 只委托 Composer |
| `hierarchy_composer.py` | HierarchyComposer 接口、Producer，以及 profile / options / request / incremental context / result / repair 类型 |
| `layer_annotator.py` | LayerAnnotator 接口：分层披露标注（L0/L1 写入 unit.layers）+ LayerAnnotatorProducer 工厂 |
| `extractor_impl/` | Extractor 实现目录（keyword / llm / dynamic_llm / video_memory，以及显式启用的 entity_schema）；video_memory 将视频规约结果转换为 CLM/ELM |
| `abstractor_impl/` | Abstractor 实现目录（concat / llm） |
| `associator_impl/` | Associator 实现目录（keyword / llm） |
| `classifier_impl/` | Classifier 实现目录（keyword / llm） |
| `index_builder_impl/` | IndexBuilder 实现目录（forward / fulltext / hybrid / unified / vector / entity + 共享 `_index_ops.py`）。**IndexBuilder 是记忆写入的唯一入口**：`hybrid`（默认 target）纯编排，组合 forward / fulltext / vector / entity 四个子 builder；`unified` 全部写委托 DomainStore 领域接口（`add`/`update`/`delete`，`mode` 原样透传，不触碰任何底层端口），构建侧完成两件事——(a) `vector_enabled` 时按 VectorIndexBuilder 同管线（Chunker 切片 → 共享 Embedder 逐 chunk embed，经 `_index_ops.vectorize_unit` 与 vector builder 共用实现）回填 `MemoryUnit.vectors`（`list[ChunkVector]`）随本体下传（一体化数据面实现消费该字段自建 chunk 级向量索引，CompositeDomainStore 仅随本体落盘）；(b) 把 `index_metadata` 的过滤投影字段（`content_layer`/`t_valid`/`t_event`/`t_invalid` 哨兵与 epoch 毫秒）直接补进 `unit.system_metadata`，一体化后端从 `system_metadata`/`user_metadata` 直接读取建索引、无需 `index_metadata` 投影下传；`forward` 只交付记忆本体（正排 KV），`fulltext`/`vector`/`entity` 只建各自的检索索引，**不交付记忆本体**，作为独立 target 使用时写路径无人写本体。`_index_ops.py` 承载 fulltext / vector 共享的投影与流水线（metadata 投影、scope 分组、端口解析、全文文档构造、向量切片-embed-写库、L0/L1 分层建删），unified 复用其 scope 分组与 `vectorize_unit` 切片-embed 单元管线。写接口用 `IndexWriteMode`（`ALL`/`FORWARD_ONLY`/`RETRIEVAL_ONLY`）表达写入范围，`remove` 用 `IndexRemoveMode`（`SOFT`/`HARD`）表达删除语义：`SOFT` 只移出检索索引、本体保留且 get/list 可读；`HARD` 物理删除。vector/fulltext 各扩展 L0/L1 分层索引（独立 store 分表，store None 跳过），详见 F01-memory-layer |
| `layer_annotator_impl/` | LayerAnnotator 实现目录（keyword / llm）；evolver 抽取后调用，对超阈 content 标注 L0/L1 |
| `dedup_impl/` | Dedup 实现目录（vector / keyword） |
| `evolver_impl/` | Evolver 实现目录（orchestrating=legacy / dynamic=动态 prompt 四步 / schema_orchestrating=Source-first Schema 属性抽取） |
| `hierarchy_composer_impl/` | `default_composer.py`（候选校验与分层提交）、`time_pipeline.py`（snapshot→time_span）、`scene_pipeline.py`（time_span→scene）、`event_pipeline.py`（scene→event）、`grouping_support.py`（共享正文选择/相邻余弦）、`time_hierarchy_pipeline.py`（先结构后摘要编排）、`parent_enrichment.py`（显式模型摘要/父标注）、`profile_config.py`（TIME 两/三/四层配置解析） |
| `bootstrap.py` | 统一触发所有构建算子注册（含 dedup_impl、hierarchy_composer_impl） |
| `schema_bootstrap.py` | 由统一 assembly 在 Schema 开关开启时内部调用，注册 Schema Extractor/Evolver target；不是独立公共装配入口 |

## 构建链路

```
接入层产出 MemoryUnit
  ↓
1. Classifier.classify(units) → 打上 tier/主题/重要度标签
  ↓
2. IndexBuilder.build(units) → 交付记忆本体（内部调 Storage）+ 构建多形式索引
     │
     ├─ Chunker.chunk → chunks → Embedder.embed → VectorRecord → VectorStore
     ├─ Tokenizer.tokenize → Document → FulltextStore
     ├─ FeatureExtractor → Node → GraphStore
     └─ Associator.associate → Edge → GraphStore（后台 ASSOCIATE 模式）
  ↓
4. Scheduler.submit(scope, EXTRACT, BACKGROUND) → 提交演进任务
  ↓
Evolver.evolve(EvolveRequest(units, mode)):
  EXTRACT     → [orchestrating] _evolve_extract: extract→route→annotate→_dedup_batch(判定+落盘)
              → [dynamic]     _evolve_extract: extract→route→consolidate(判定)→reflect→落盘
              → [schema]      _evolve_extract: Source-first→属性抽取→属性落盘→Source entities 写回
  CONSOLIDATE → _evolve_consolidate: abstract→annotate→_dedup_batch
  ASSOCIATE   → _evolve_associate: associate→冲突消解→图索引 Edge
  FORGET     → _evolve_forget: 遗忘候选筛选→lifecycle 置 FORGOTTEN→
               IndexBuilder.update(mode=FORWARD_ONLY) 回写本体 +
               IndexBuilder.remove(mode=SOFT) 移出检索
  HIERARCHY  → 显式重建：HierarchyComposer.replace_in_span；内部单层增量：build
               → TIME snapshot→time_span→可选 scene→可选 event → 父摘要/标注 → 候选树校验
               → 经 IndexBuilder 分阶段保存
```

三个 Evolver 同属 `evolver` 顶层命名空间。`DynamicEvolver` 与显式启用的
`SchemaOrchestratingEvolver` 都继承 `OrchestratingEvolver`，只覆盖 `_evolve_extract`；
其余四模式继承父类。装配或 pipeline profile 选择注册名即启用对应 EXTRACT 路径。
公开 HIERARCHY 任务由 Control 收齐有界候选和旧根完整子树后进入内部 Composer；
普通 write 不自动触发建树。

## 行为铁律

0. **派生 metadata 按来源合并**：单源复制 `user_metadata`，多源只保留相等交集；
   `infer` / `procedural` / `middle` 不传播。IndexBuilder 分别投影
   `system_metadata.<key>` 和 `user_metadata.<key>`。

1. **落盘由本层负责**
   接入层产出 `MemoryUnit` 后，记忆本体的写入由本层完成——统一经 `IndexBuilder`，由其内部
   调用 Storage。**Evolver 不得直接调用 Storage 的 `add`/`update`/`delete`**（读取与原文接口不受限）。
   接入层禁止落盘。

2. **索引是可重建派生（目标约束）**
   派生索引（向量/关键词/图/文档）应全部可从记忆本体重建，`IndexBuilder.rebuild()` 应提供
   非破坏式保障。当前 Forward/Fulltext/Vector/Hybrid/Unified/Entity 实现均为 no-op，
   因此当前版本不能宣称删索引后可恢复。`IndexBuilder` 不根据 `unit.lifecycle` 推断操作；
   调用方通过 `IndexWriteMode` / `IndexRemoveMode` 明确要求回写本体、刷新检索索引或软删除。

3. **provenance 回指来源**
   派生记忆单元（Extractor/Abstractor 产出）的 `provenance` 字段记录由哪些 unit 演进而来，保证可重建、可审计回溯。
   Composer 的父子包含只写 `hierarchy`，不借结构派生填充 provenance 或 supersedes。

4. **构建与存储解耦**
   算子负责构建逻辑（生成索引投影：Chunk → VectorRecord/Document/Node，MemoryUnit → KV 记录），持久化由注入的 Store 承担。算子不依赖具体后端。正排与派生索引同此模式——`ForwardIndexBuilder` 写 `manager.kv()`，与 `FulltextIndexBuilder` 写 `manager.fulltext()` 同构；端口名可经 `params.<ns>_store` 具名选择。

5. **scope 原生隔离**
   构建索引时将来源 `MemoryUnit.scope` 作为 Store 方法的显式参数下推，记录本身不混入
   scope 字段；跨 Scope 子边使用完整 Scope + id 定位，不能只按 id 建索引或查重。

6. **Evolver 模式独立**
   EXTRACT / ASSOCIATE / CONSOLIDATE / FORGET 保留既有内容演进行为；内部 HIERARCHY
   只委托 Composer，不进入抽取、去重或内容合并。索引维护不作为 evolve 模式。

7. **去重召回与判定分离**
   `Dedup` 接口只管召回（向量化/分词 → Store.search → 加载 → 聚合取 max），判定
   （ADD/UPDATE/SUPERSEDE/NOOP）与落盘归 Evolver 实现：
   `OrchestratingEvolver._evolve_extract`（legacy）由 `_dedup_batch` 耦合判定与落盘；
   `DynamicEvolver._evolve_extract`（dynamic）在 consolidate 只判定，reflect 后统一落盘。
   装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`，保证 fulltext-only 下去重仍可用。
   高相似 direct_noop 短路（`score ≥ dedup_high_similarity`）须经
   `evolver_impl/dedup_direct_noop.should_direct_noop` 校验：仅当相对已有记忆无
   `has_meaningful_delta`（`t_event` 冲突、更正词、日期 span 集合差）时才允许跳过 LLM。

8. **构建层不依赖 control**
   SUPERSEDE/FORGET 标记由 `OrchestratingEvolver`/`DynamicEvolver` 经
   `IndexBuilder.update` 完成，不经 `LifecycleManager`（construction → control 严禁）。

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
    未知事件时间 `t_event=None` 恒写哨兵 `T_EVENT_UNKNOWN=0`（F08 派生常为此值）。
    真源仍保留 None，禁止为适配后端改写 MemoryUnit；`memory_filter._field_value`
    对 `t_event` / `t_invalid` 的 None 同步投影为对应哨兵，使后置复核与下推不分叉。

15. **抽取与分层优先保证完整性**
    派生 L2 只保存紧凑陈述，通过 `source_ref`/`provenance`/`evidence` 回指来源；坏候选
    与坏子批分别隔离，整次抽取无可用候选时才显式失败；动态抽取可隔离单策略失败，但
    全部策略失败必须向上抛错。LLM 分层的重复、越界或遗漏 ID 拒绝整批，单条长度异常
    只跳过该条，其余合法结果在结构校验完成后写入。

16. **结构索引投影只认 HierarchyRef**
    `_index_ops.index_metadata` 为全文与向量路径补结构六键；`UnifiedIndexBuilder`
    使用同一结构投影，把副本补入 `unit.system_metadata` 后交 DomainStore。build/update
    均先移除旧结构投影，再从当前引用生成；空结构移除全部六键，无区间不保留旧 span。
    结构时间使用 UTC epoch 毫秒，与 KV 中 ISO 8601 序列化分开；不得改写
    `user_metadata` 同名键。投影不等于建树、查询贯通或已实现 rebuild 恢复。

17. **Composer 只处理显式输入，不扫描数据库**
    显式重建接受 ACTIVE 的 TIME snapshot 与待替换 time_span/scene/event。请求必须备齐相交旧根
    全部父层与叶；不补齐缺失节点、不暗中扩大查询范围。先在副本上生成并校验候选，再写存储。
    叶的正文、时间、tier、来源和生命周期不变，只有 hierarchy 父边改变。

18. **Composer 只经 IndexBuilder 按可恢复顺序写入**
    新父本体按 event→scene→time_span 分层 build(FORWARD_ONLY) → 子边 update(FORWARD_ONLY)
    → 旧父归档并清边
    update(FORWARD_ONLY) → 旧父 remove(SOFT) → 新父与子补建/更新 RETRIEVAL_ONLY。
    本体阶段失败即停；索引阶段保留逐项 repair 报告。失败不承诺回滚或自动修复，也不硬删叶。

19. **TIME 先确定结构，再增强父内容**
    snapshot 按 UTC span_start、t_event、输入序稳定排序，按会话/配置上下文/相邻 span
    间隔和结束信号切分；scene 按上下文、结束信号、累计跨度及显式可选相邻向量相似度切分。
    event 按上下文、可选实体重叠和相邻向量相似度分组，无时长上限，不重组非相邻场景。
    session 不切 scene/event，模型摘要不得影响分组。新父默认摘录；显式配置 LLM/LayerAnnotator
    时才生成摘要/L0/L1，运行期内容增强失败安全降级、不重写 snapshot。
20. **增量是内部相邻单层构建**
    `hierarchy_composer_impl/incremental_pipeline.py` 用完整只读子树重建确定性摘录视图，
    再分组和封口，不用下层已保存的 LLM 摘要决定结构。只为封口组写新父，旧输入仅改
    父引用，正文/层摘要/子边保持不变。静默封口与下层 pending 边界由 typed context
    表达；不查库、不自行安排周期，缺少显式 profile 或完整子树时拒绝。

## 与其他子目录的边界

**本模块管**：
- 记忆本体落盘（经 IndexBuilder 调用 Storage）
- 原文（`/messages/`）读写（调用独立注入的 `message_store: KVStore`）
- 信息提取与抽象升华（Extractor / Abstractor）
- 关联分析（Associator）
- 多维分类（Classifier）
- 动态 prompt 四步演进（DynamicEvolver：extract→consolidate→reflect→落盘，Evolver 的子类实现）
- 多形式索引构建（IndexBuilder）
- 去重召回（Dedup）
- 记忆自演进（Evolver）
- 显式候选上的 TIME 父层构建、受限替换与失败报告（HierarchyComposer）

**不管**：
- 鉴权（归 `api`）
- 检索（归 `retrieval`；去重召回不经 retrieval 的 Recaller，`Dedup` 直接调 Store）
- 存储实现（通过注入的 Store 抽象间接调用）
- 共享插件实现（Chunker/Embedder/Tokenizer/LLM 等归 `common`）

## 本地约束

1. 所有 Operator 必须实现 `operator_type()` 和 `health()`（继承自 `ConstructionOperator`）。
2. 算子实现通过 `@XxxProducer.register("name")` 自注册。
3. IndexBuilder 必须实现四个方法：`build(units, *, mode=IndexWriteMode.ALL)` /
   `update(units, *, mode=IndexWriteMode.ALL)` /
   `remove(units, *, mode=IndexRemoveMode.HARD)`（按 MemoryUnit 自带 Scope 删除）/ `rebuild()`；
   不得维护仅按 unit id 的单值 Scope 缓存。`FORWARD_ONLY`/`RETRIEVAL_ONLY` 把写入限定到
   正排本体或检索索引单侧（生命周期治理与索引迁移用）；`SOFT` 软删除只移出检索索引、
   记忆本体保留且 get/list 可读。`unified` 全部写委托 Storage 领域接口（`mode` 透传，
   能否单侧操作由该 DomainStore 实现按能力决定，CompositeDomainStore 下 `RETRIEVAL_ONLY`/`SOFT`
   为空操作），自身只做两件事：(a) `vector_enabled` 时按 vector builder 同管线切片-向量化并
   回填 `MemoryUnit.vectors` 随本体下传（单 unit embed 失败不阻断本体写入，vectors 留空）；
   (b) 把 `index_metadata` 过滤投影字段（`content_layer`/`t_valid`/`t_event`/`t_invalid` 哨兵）
   补进 `unit.system_metadata`，一体化后端直接读、无需单独投影下传。
4. Evolver 接收 `EvolveRequest`，返回 `EvolveResult`（四类 id 列表、内容演进的 created_units，
   以及仅 HIERARCHY 返回的 hierarchy_result）。内部调用不再支持旧的 `(units, mode)` 形态。
5. Dedup 必须实现 `recall(candidate) -> list[(MemoryUnit, score)]`；实现内部异常吞掉返回空列表，不阻断演进。
6. 算子内部调用共享插件（Chunker/Embedder/Tokenizer/FeatureExtractor/LLM）必须使用注入的实例，不自行构造。
7. `OrchestratingEvolver` 以 `EvolverDependencies` 聚合注入算子/存储，以 `EvolverOptions`
   聚合图端口和去重阈值；`DynamicEvolver` 与 `SchemaOrchestratingEvolver` 复用这两个对象。
   Dynamic 额外注入 PromptRegistry；两者只覆盖 EXTRACT，其余四模式继承父类。
8. `DynamicEvolver` 与 IndexBuilder/KVStore/Dedup 必须使用同一 profile 的共享实例；pipeline profile 选 evolver 实现名（`orchestrating` / `dynamic`）即切换 EXTRACT 路径。
9. `DynamicLLMExtractor` 子类只覆盖 `parse_response` 完成响应解析与构建；策略遍历、fallback、
   `_extraction_strategy` 标记和 consolidation/reflect prompt key 透传由基类统一执行。
10. `PromptRegistry` 由装配从 `ctx.globals["prompts"]` 加载；`DynamicEvolver._build` 用 `config.get("prompts")` 取该段（params 无 prompts 时回退 globals）构造注册表。
11. `HierarchyComposerProducer` 的命名空间是 `hierarchy_composer`，当前 target 为 `default`。
    Evolver 只有显式配置 `params.hierarchy_composer` 时才注入，未配置不影响普通写入。
    Composer 必须显式注入与叶写入路径相同的具名 index_builder，读取 hierarchy_profiles 的
    TIME 两/三/四层配置及 allow_cross_user。可选 embedder/llm/layer_annotator 必须显式声明；
    不回落占位模型，开启相应能力却缺依赖时装配失败。
