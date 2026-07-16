# Agent Memory Construction（构建层）

**规约文档**：[S05-construction.md](../../docs/specs/S05-construction.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

接收接入层产出的 `MemoryUnit`，调用 `storage` 落盘，在其上构建多形式索引。七个可插拔算子：`Extractor`（信息提取）→ `Abstractor`（抽象升华）→ `Associator`（关联分析）→ `Classifier`（多维分类）→ `IndexBuilder`（索引构建）→ `Dedup`（去重召回）→ `Evolver`（自演进闭环）。

> 契约（接口签名/数据结构/不变量）见 [`docs/specs/S05-construction.md`](../../docs/specs/S05-construction.md)；设计理念与决策取舍（双通道/演进闭环/依赖关系）见 [`docs/features/construction/F01-construction-spec-design.md`](../../docs/features/construction/F01-construction-spec-design.md)。本文件只记当前实现地图与本地约束。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | ConstructionOperator 基类 + OperatorType 枚举 |
| `extractor.py` | Extractor 接口：信息提取（低抽象粒度） |
| `abstractor.py` | Abstractor 接口：抽象与精炼/升华（高抽象粒度） |
| `associator.py` | Associator 接口：关联分析（实体共指/因果链/引用关系） |
| `classifier.py` | Classifier 接口：多维分类（认知角色/主题/重要度） |
| `index_builder.py` | IndexBuilder 接口：多形式索引构建（文档/关键词/向量/图） |
| `dedup.py` | Dedup 接口：去重召回（向量/倒排两路）+ DedupProducer 工厂 |
| `evolver.py` | Evolver 接口：记忆自演进（抽取/关联/巩固/遗忘）+ EvolveMode + EvolveResult |
| `layer_annotator.py` | LayerAnnotator 接口：分层披露标注（L0/L1 写入 unit.layers）+ LayerAnnotatorProducer 工厂 |
| `extractor_impl/` | Extractor 实现目录（keyword / llm） |
| `abstractor_impl/` | Abstractor 实现目录（concat / llm） |
| `associator_impl/` | Associator 实现目录（keyword / llm） |
| `classifier_impl/` | Classifier 实现目录（keyword / llm） |
| `index_builder_impl/` | IndexBuilder 实现目录（fulltext / hybrid / vector）；vector/fulltext 各扩展 L0/L1 分层索引（独立 store 分表，store None 跳过），详见 F01-memory-layer |
| `layer_annotator_impl/` | LayerAnnotator 实现目录（keyword / llm）；evolver 抽取后调用，对超阈 content 标注 L0/L1 |
| `dedup_impl/` | Dedup 实现目录（vector / keyword） |
| `evolver_impl/` | Evolver 实现目录（orchestrating） |
| `bootstrap.py` | 统一触发所有构建算子注册（含 dedup_impl） |

## 构建链路

```
接入层产出 MemoryUnit
  ↓
1. Classifier.classify(units) → 打上 tier/主题/重要度标签
  ↓
2. KVStore.insert(scope, unit.id, dumps(unit)) → 真源落盘
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
  EXTRACT     → Extractor.extract → Dedup.recall → 去重决策 → 落盘建索引
  CONSOLIDATE → Abstractor.abstract → Dedup.recall → 去重决策 → 落盘建索引
  ASSOCIATE   → Associator.associate → 冲突消解 → 图索引 Edge
  FORGET     → 遗忘候选筛选 → lifecycle 标记 → IndexBuilder.remove
```

## 行为铁律

1. **落盘由本层负责**  
   接入层产出 `MemoryUnit` 后，真源写入由本层调用 `KVStore.insert` 完成。接入层禁止落盘。

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
   `Dedup` 接口只管召回（向量化/分词 → Store.search → 加载 → 聚合取 max），Evolver 做阈值 + LLM 判定。装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`——只配倒排时去重仍可用（向量路在 fulltext-only 下 VectorStore 恒空会失效）。

8. **Evolver 不依赖 control**  
   SUPERSEDE/FORGET 标记由 Evolver 直接通过 `KVStore.update` 完成，不经 `LifecycleManager`（construction → control 严禁）。

9. **Dedup 与 IndexBuilder 共享底层 Store**  
   去重召回检索的是已索引内容，`Dedup` 实现取的 `VectorStore`/`FulltextStore` 必须与 IndexBuilder 写入的是同一实例（按字段名缓存命中）。

10. **L0/L1 分层索引分表且 store None 跳过**  
    `unit.layers.l0`/`l1` 非空时，VectorIndexBuilder/FulltextIndexBuilder 对整段文本（不切片）建独立 store 索引（record id 向量=`{uid}-layer-l0`/`-layer-l1`、全文=`{uid}:l0`/`:l1`，与 content 的 chunk id 不冲突），metadata `content_layer`="l0"/"l1"，content 表 chunk record 补 `content_layer="l2"`。物理分表不混 content。`vector_l0`/`vector_l1`/`fulltext_l0`/`fulltext_l1` 任一为 None 则该层跳过（不报错、不建空记录）。update 先删旧分层 record 再按新 layers 重建（SUPERSEDE 不残留），remove 按 id 幂等删。详见 F01-memory-layer。

## 与其他子目录的边界

**本模块管**：
- 真源落盘（调用 KVStore）
- 信息提取与抽象升华（Extractor / Abstractor）
- 关联分析（Associator）
- 多维分类（Classifier）
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
3. IndexBuilder 必须实现四个方法：`build`（新建）/ `update`（更新）/ `remove`（删除）/ `rebuild`（全量重建）。
4. Evolver 返回 `EvolveResult`（created_ids / updated_ids / superseded_ids / forgotten_ids）。
5. Dedup 必须实现 `recall(candidate) -> list[(MemoryUnit, score)]`；实现内部异常吞掉返回空列表，不阻断演进。
6. 算子内部调用共享插件（Chunker/Embedder/Tokenizer/FeatureExtractor/LLM）必须使用注入的实例，不自行构造。
7. Evolver 持有 `Dedup` 实例做去重，不直接持 `VectorStore`/`Embedder`（已下沉到 Dedup 实现）。
