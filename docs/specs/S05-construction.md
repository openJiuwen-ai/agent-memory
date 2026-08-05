# S05 — 构建层（Construction Layer）

## LLM-native Wiki construction contract

The evaluation-side open-domain Wiki path accepts `SemanticSource` records and
produces provenance-preserving `SemanticMemory` records. The supported memory
ontology is `entity`, `fact`, `event`, `preference`, `skill`, `relationship`,
`decision`, `constraint`, `context`, and `artifact`.

`WikiBuilder(llm=None, mode="llm")` uses the LLM extractor, entity resolver,
consolidator, and profile/timeline/decision synthesis when an LLM is injected.
With no model, or after an LLM failure, it falls back to the deterministic
`TemplateExtractor`; retained benchmark adapters may continue using their
historical deterministic renderer for exact regression compatibility. Every
generated page carries evidence, provenance, metadata, stable IDs, and source
links. Query understanding is optional and must not replace the deterministic
qmd retrieval path.

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/construction/ |
| 最近一次修订日期 | 2026-08-05 |
| 关联特性文档 | docs/features/F01-system-spec-design.md, docs/features/construction/F01-construction-spec-design.md, docs/features/construction/F02-dynamic-extraction-consolidation.md, docs/features/construction/F03-extraction-layer-integrity.md, docs/features/F02-wikimem-compat.md, docs/features/construction/F04-wikimem-compat.md, docs/features/common/F01-memory-layer.md, docs/features/common/F03-scope-space-isolation.md, docs/features/retrieval/F03-metadata-filtering.md |

## 范围 / 边界

**管什么**：
- 真源落盘（调用 KVStore 写入记忆单元序列化）
- 信息提取（低抽象粒度：事实/事件/偏好）
- 抽象与精炼/升华（高抽象粒度：画像/长期偏好/可复用技能）
- 关联分析（实体共指/因果链/引用关系）
- 多维分类（认知角色/主题/重要度）
- 候选落盘前巩固（ADD/UPDATE/SUPERSEDE/NOOP）
- 多形式索引构建（文档/关键词/向量/图，按配置启用）
- 记忆自演进（抽取 → 关联 → 冲突消解 → 升华 → 遗忘/降权）

**不管什么**：
- 不做鉴权（由 `src/api` 层负责）
- 不做检索（由 `src/retrieval` 层负责）
- 不实现存储后端（通过注入的 Store 抽象间接调用）
- 不实现共享插件（Chunker/Tokenizer/Embedder/FeatureExtractor/LLM 由 `src/common` 注入；Reranker 不被本层使用——去重 LLM 直接判定）

## 不变量

1. **落盘由本层负责**：接入层产出 MemoryUnit 后，真源写入由本层调用 KVStore 完成。
2. **索引是可重建派生**：索引全部可从真源重建，IndexBuilder.rebuild() 是非破坏式保障。
3. **provenance 回指来源**：派生记忆单元的 `provenance` 字段记录由哪些 unit 演进而来。
4. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
5. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `ConstructionOperator`。
6. **构建与存储解耦**：算子负责构建逻辑（生成索引投影），持久化由注入的 Store 承担。
7. **scope 原生隔离**：构建索引时将来源 `MemoryUnit.scope` 作为 Store 方法的显式
   入参下推；`VectorRecord` / `Document` / `Node` 等记录结构不混入 scope 字段。
8. **去重召回与判定分离**：去重召回（用哪个索引）由 `Dedup` 接口承担，判定（ADD/UPDATE/SUPERSEDE/NOOP）与落盘由 Evolver 实现承担——`OrchestratingEvolver._evolve_extract`（legacy，`_dedup_batch` 判定+落盘耦合）或 `DynamicEvolver._evolve_extract`（dynamic，consolidate 只判定、落盘延后到 reflect 之后）。装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`。
9. **构建层不依赖 control**：`DynamicEvolver`/`OrchestratingEvolver` 的 SUPERSEDE 与 FORGET 直接通过 `KVStore.update` 完成，不经 `LifecycleManager`。
10. **Dedup 与 IndexBuilder 共享底层 Store**：去重召回检索的是已索引内容，`Dedup` 实现取的 `VectorStore`/`FulltextStore` 必须与 IndexBuilder 写入的是同一实例（按字段名缓存命中）。
11. **派生 metadata 键保持类型稳定**：当前 Classifier 只更新
    `MemoryUnit.tier` / `MemoryUnit.tags`，不约定额外分类 metadata 键；
    LLM Extractor / LLM Abstractor 写出 `metadata.confidence` 时使用浮点字符串，
    非 LLM 实现不保证存在该键。Evolver 写回 `metadata.dedup_similarity`、
    `DeleteMode.DOWNWEIGHT` 写回 `metadata.importance` 时也使用浮点字符串。
    查询侧不对这些键做隐式类型转换。
12. **consolidate 只判定不落盘**：`DynamicEvolver` 的 consolidate 步只产出
    `ConsolidateDecision`（候选 + 决策 + 已有记忆 + 相似度），落盘延后到
    reflect 之后统一执行。reflect 默认 no-op；当前只有对子候选的原地修改能影响落盘。
13. **索引投影保留业务 metadata 类型**：Vector/Fulltext IndexBuilder 先复制
    `MemoryUnit.metadata`，再用系统真源字段覆盖保留 key；时间投影为 epoch 毫秒，
    `t_invalid=None` 仅在索引中写为 `T_INVALID_OPEN`，不改写真源。
14. **索引删除按 MemoryUnit 定位**：`IndexBuilder.remove` 接收带 Scope 的 MemoryUnit，禁止维护仅按 unit id 的单值 Scope 缓存；同一逻辑 id 在不同 Scope 的索引互不影响。

## 接口契约

### ConstructionOperator（基类，`base.py`）

```python
class OperatorType(str, Enum):
    EXTRACTOR / ABSTRACTOR / ASSOCIATOR / CLASSIFIER / INDEX_BUILDER / EVOLVER / LAYER_ANNOTATOR

class ConstructionOperator(ABC):
    def operator_type(self) -> OperatorType  # 自描述
    def health(self) -> None                 # 存活探测
```

> `OperatorType` 枚举无独立 DEDUP 值——`Dedup` 实现复用 `OperatorType.EVOLVER`（去重召回服务于 evolver）。`DynamicEvolver` 是 `OrchestratingEvolver` 的子类，同样返回 `OperatorType.EVOLVER`——它是 evolver 的动态 prompt 变体，通过覆盖 `_evolve_extract` 切换 EXTRACT 路径。

### Extractor（`extractor.py`）

信息提取，产出低抽象粒度的派生记忆单元。

| 方法 | 签名 | 语义 |
|------|------|------|
| `extract` | `(units: list[MemoryUnit], *, context: ExtractContext \| None = None) -> list[MemoryUnit]` | 从一批原始记忆单元中提取零或多条低抽象粒度的派生单元；context 只作 prompt 参考 |

派生单元的 `tier`/`tags` 由 LLM 在抽取时产出。`layers`（L0/L1 分层标注）不由 Extractor
产出——由 Evolver 抽取后委托 `LayerAnnotator` 生成（见下文 LayerAnnotator 节 + F01-memory-layer）。

LLM 抽取只合并同一实体同一关系或同一事件。派生单元的 L2 只保存紧凑抽取陈述，
通过 `source_ref`、`provenance` 和模型返回的 `metadata["evidence"]` 回指来源（兼容
自定义 prompt，允许 evidence 为空）。表格独立记录使用 `structured_record`，可复用助手
产物使用 `artifact`。非法 JSON 作为子批失败显式记录；候选结构逐条校验并隔离坏候选，
同批合法候选继续保留。单个子批失败不阻断其它子批；仅当整次抽取没有产生任何可用候选
时向上抛出首个错误，以区别于模型明确返回合法 `[]`。

动态实现识别 `_extract_prompt_<strategy>`。普通 write 路径以 `infer=true` 触发抽取，
procedural write 或显式 `evolve(EXTRACT)` 也会进入同一 Extractor；每个非空自定义策略
执行一次 LLM 调用。metadata 中 `_extract_prompt_<strategy>` 的值是 prompt 的
**key**（引用 yml `prompts.extract` 段的命名 prompt），运行时由
`PromptRegistry` 按 `phase=extract + key` 查真实文本作为 system prompt 发给 LLM；
registry 未配置或 key 缺失时回退把值本身当文本用（兼容内联文本）。响应格式由 prompt
自身约定，调用方在 prompt 文本里写清。`DynamicLLMExtractor` 默认按 JSON 解析，并开放
`parse_response(response, sources, strategy) -> list[MemoryUnit]` 继承扩展点，允许新实现
解析 XML 或其它响应格式。格式相关中间结构不得越过 `parse_response` 边界；所有实现最终
仍向 Evolver 返回 `list[MemoryUnit]`。单个策略失败与其它策略隔离；若所有策略都失败则
向上抛出最后一个错误，以区别于策略成功返回合法空结果。没有动态 prompt 时委托配置的旧
Extractor。

### DynamicEvolver（`evolver_impl/dynamic_evolver.py`）

`OrchestratingEvolver` 的子类，覆盖 `_evolve_extract` 走动态 prompt 四步编排：`extract → consolidate(判定) → reflect → 落盘`。其余三模式（CONSOLIDATE/ASSOCIATE/FORGET）继承父类行为。注册名 `dynamic`，与 `orchestrating` 平级，同属 `evolver` 顶层命名空间——装配或 pipeline profile 选哪个 evolver 实例即启用哪条 EXTRACT 路径。

| 方法 | 签名 | 语义 |
|------|------|------|
| `_evolve_extract` | `(units: list[MemoryUnit]) -> EvolveResult`（覆盖父类） | 动态四步：抽取候选 → 巩固判定 → 反思 → 按判定落盘 |

**四步语义**：

1. **extract**：委托父类持有的 `Extractor.extract`，产出派生候选；把源 unit 的 consolidation/reflect prompt key 透传给候选；调 `_annotate_layers` 标注 L0/L1。
2. **consolidate（只判定不落盘）**：对每个候选调 `Dedup.recall` 召回已有记忆，按相似度阈值 + LLM 判定产出 `ConsolidateDecision`（候选 + `DedupDecision` + 已有记忆 + 相似度）。无命中 → ADD；高相似度（≥ `dedup_high_similarity`）→ NOOP；中段（`dedup_medium_similarity` ~ high）→ 查 `PromptRegistry` 取 consolidate prompt 调 LLM 判定；无 prompt 或 LLM 失败 → 回退规则。
3. **reflect（默认 no-op）**：基类 `_reflect_step` 直接返回候选；子类可覆盖
   `_reflect_step` 在落盘前原地修正候选。当前持久化仍读取
   `ConsolidateDecision.candidate`，替换候选对象不会生效。
4. **落盘**：按每个 `ConsolidateDecision.decision` 执行 ADD/UPDATE/SUPERSEDE/NOOP——ADD/SUPERSEDE 调 `KVStore.insert` + `IndexBuilder.build`，UPDATE 调 LLM 合并内容后 `KVStore.update` + `IndexBuilder.update`，NOOP 跳过。

**procedural 路径**：`_evolve_extract` 检测到 procedural=true 时 `super()._evolve_extract(units)` 走父类行为（不收集 context、不判定、直接落盘）——procedural 语义是"记成一条 how-to"，无需动态判定。

**PromptRegistry**（`prompt_registry.py`）：从 yml 顶层 `prompts` 段加载的命名 prompt 文本，按 `phase + key` 查询。metadata 只写 prompt 的 **key**（引用 yml 命名 prompt），运行时按 key 查真实文本。extract 步的 registry 由 `ExtractorProducer._build` 注入 `DynamicLLMExtractor`；consolidate 步的 registry 由 `DynamicEvolver._build` 注入。reflect key 当前只透传给候选，默认实现不查询 registry，子类可按 `PHASE_REFLECT` 扩展。两个 builder 都从 `ctx.globals["prompts"]` 加载，共享同一份 yml `prompts` 段而非同一 registry 实例。

**prompt key 透传**：源 unit 的 `_consolidation_prompt_<strategy>` / `_reflect_prompt_<strategy>` 由 `copy_consolidation_prompts` / `copy_reflect_prompts` 透传给派生候选，供后续步骤按 key 查 PromptRegistry。`_extract_prompt_<strategy>` 由调用方在 write 时直接传入，extract 步就地消费。

### Abstractor（`abstractor.py`）

抽象与精炼/升华，产出高抽象粒度的新记忆单元。

| 方法 | 签名 | 语义 |
|------|------|------|
| `abstract` | `(units: list[MemoryUnit]) -> list[MemoryUnit]` | 对一批记忆单元做抽象与精炼，产出高抽象粒度的新记忆单元 |

### Associator（`associator.py`）

关联分析，发现记忆间的关联关系。

| 方法 | 签名 | 语义 |
|------|------|------|
| `associate` | `(units: list[MemoryUnit]) -> list[Relation]` | 在一批记忆单元间做关联分析，返回发现的关联关系 |

产出的 `Relation` 交由 IndexBuilder 写入图索引。

### Classifier（`classifier.py`）

多维分类，为记忆单元打上分类标签。

| 方法 | 签名 | 语义 |
|------|------|------|
| `classify` | `(units: list[MemoryUnit]) -> list[MemoryUnit]` | 为一批记忆单元打上 tier/主题/重要度等分类标签，返回更新后的单元 |

### LayerAnnotator（`layer_annotator.py`）

分层披露标注，给已有 `MemoryUnit` 写 `layers.l0`/`layers.l1`（不产出新记忆）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `annotate` | `(units: list[MemoryUnit]) -> list[MemoryUnit]` | 为一批 unit 生成 L0/L1 标注，原地写 `unit.layers`，返回原列表 |

按 `layer_annotator_threshold`（默认 512）筛选：仅对 `len(content) > threshold` 的 unit
标注，短 content 留空。LLM 批量结果必须以合法、唯一的 ID 完整覆盖输入；重复、越界或
遗漏 ID 时整批不写。每条结果应满足 `0 < len(L0) < len(L1) < len(L2)`，仅长度不合法的
条目单独跳过，其余条目在结构校验完成后写入。Evolver 在 EXTRACT/CONSOLIDATE 抽取
（升华）后、去重落盘前调用。

### IndexBuilder（`index_builder.py`）

多形式索引构建与维护。

| 方法 | 签名 | 语义 |
|------|------|------|
| `build` | `(units: list[MemoryUnit]) -> None` | 为一批记忆单元构建已启用的各形式索引 |
| `update` | `(units: list[MemoryUnit]) -> None` | 记忆变更后增量更新对应索引条目 |
| `remove` | `(units: list[MemoryUnit]) -> None` | 按每个 MemoryUnit 自带 Scope 删除对应索引条目（幂等） |
| `rebuild` | `() -> None` | 从真源全量重建索引（删索引不丢数据的保障） |

**build 路径**（按配置启用的索引类型，各实现独立构建）：
```
MemoryUnit
├─ 关键词路（FulltextIndexBuilder）：unit.content 整篇不切片
│   → Document(id=unit.id, text=unit.content, metadata={tier,tags,source})
│   → FulltextStore.insert
├─ 向量路（VectorIndexBuilder）：Chunker 切片
│   → Chunker.chunk(unit.content) → chunks
│   → Embedder.embed(chunks) → VectorRecord(id={unit.id}-{chunk.id}, vector, metadata={unit_id,tier})
│   → VectorStore.insert + KVStore 维护 chunk_id 跟踪（供 update/remove 读旧 chunk）
├─ L0/L1 分层路（FulltextIndexBuilder + VectorIndexBuilder 扩展）：
│   → unit.layers.l0/l1 非空且对应 store 已注入 → 整段不切片
│   → Document/VectorRecord(id={unit.id}-l0/-l1, text/vector=layers.l0/l1, metadata={unit_id,layer})
│   → 写独立 FulltextStore/VectorStore 实例（不同 collection/index = 分表，与 content 物理隔离）
│   → store 为 None 跳过该层（向后兼容 + 配置降级）；update 先删后建，remove 幂等删
├─ 图路（Evolver ASSOCIATE 模式编排）：
│   → FeatureExtractor → Node → GraphStore.insert
│   → Associator.associate → Edge → GraphStore.insert
└─ HybridIndexBuilder：组合 fulltext + vector 两个子 builder（默认实现）
```

> 注：文档索引（path → unit_id 映射）与 FusionStore 融合索引不属于本文已固化的构建接口契约，属设计预留。
> L0/L1 分层索引的召回接入未落地（为披露层预留），详见 F01。

### Evolver（`evolver.py`）

记忆自演进，持续驱动演进闭环。两个实现：`OrchestratingEvolver`（注册名 `orchestrating`，legacy）与 `DynamicEvolver`（注册名 `dynamic`，子类，EXTRACT 走动态 prompt 四步）。`evolve` 按模式分派到 `_evolve_extract` / `_evolve_consolidate` / `_evolve_associate` / `_evolve_forget` 四个可覆盖方法。

| 方法 | 签名 | 语义 |
|------|------|------|
| `evolve` | `(units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult` | 对一批记忆单元执行指定阶段的演进，返回变更结果 |

**EvolveMode**：
- `EXTRACT` — 信息提取
- `ASSOCIATE` — 关联分析
- `CONSOLIDATE` — 冲突消解（近重复融合/矛盾标记失效）
- `FORGET` — 遗忘/降权（过期/低价值记忆归档）

**EvolveResult**：
- `created_ids: list[str]` — 新增记忆单元 id
- `updated_ids: list[str]` — 更新记忆单元 id
- `superseded_ids: list[str]` — 被取代记忆单元 id
- `forgotten_ids: list[str]` — 被遗忘记忆单元 id

### Dedup（`dedup.py`）

去重召回，由 Evolver 实现（`OrchestratingEvolver._dedup_batch` / `DynamicEvolver._consolidate_step`）及 infer 上下文收集调用。召回 + 阈值过滤 + 加载 + 聚合取 max 全在实现内完成；判定与落盘动作归调用方（evolver）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `recall` | `(candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]` | 对候选召回已有相似记忆，返回 (unit, score) 列表（按 score 降序）；已完成过滤自身、过滤非 ACTIVE、按 unit 聚合取 max、按 min_similarity 过滤低分。空列表 → 调用方判 ADD |

**score 量纲 0~1**：向量路=cosine，倒排路=词重叠率，阈值统一复用。

**两个实现**（装配按 `vector_enabled` 选）：
- `VectorDedup`（`vector`）— Embedder → VectorStore.search，cosine；record_id 为 `{unit_id}-{chunk_id}` 需解析
- `KeywordDedup`（`keyword`）— FulltextStore.search，词重叠率；Document.id = unit.id 恒等无需解析

**降级契约**：实现内部任何异常（Embedder/Store 失败）都吞掉并返回空列表——去重是尽力而为，不可阻断演进。

## 数据结构

### MemoryUnit（`common/type_def/memory.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | Scope 内唯一 id |
| `scope` | Scope | 归属 scope |
| `tier` | MemoryTier | 认知角色（working/core/episodic/semantic/procedural/archival） |
| `segments` | list[Segment] | 内容段列表（多段内容投影，每段含 content+assets+source） |
| `source_ref` | str | 来源引用（RawPayload id / 会话 id 等，可溯源） |
| `temporal` | Temporal | 时间：t_event / t_ingest / t_valid / t_invalid |
| `provenance` | list[str] | 演进血缘（多→一）：由哪些 unit 抽取/升华/合并而来 |
| `supersedes` | str | 版本链（一→一）：本版取代的上一版 id（空=首版） |
| `tags` | list[str] | 标签（检索前置过滤用） |
| `metadata` | dict[str, Any] | 元数据（保留 JSON 标量原生类型） |
| `lifecycle` | LifecycleState | 生命周期状态 |

**注**：`MemoryUnit.content` / `assets` / `source` 是基于 segments 的只读合并视图，非独立字段。

### Relation（`common/type_def/feature.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `source_id` | str | 关联起点（记忆单元/实体 id） |
| `target_id` | str | 关联终点（记忆单元/实体 id） |
| `relation` | str | 关系类型（caused_by / refers_to / corefers ...） |
| `score` | float | 关联置信度 |
| `metadata` | dict[str, Any] | 附加信息 |

### Segment（`common/type_def/memory.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `content` | str | 文本内容（索引与检索的对象） |
| `assets` | list[str] | 本段原模态资产引用（图像/音频原件等） |
| `source` | Modality | 本段来源模态 |

`MemoryUnit.content` 是所有段 `content` 以换行连接的只读合并视图。

## 实现注册机制

```
src/construction/<算子>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`ExtractorProducer` / `AbstractorProducer` / `AssociatorProducer` / `ClassifierProducer` / `IndexBuilderProducer` / `DedupProducer` / `EvolverProducer`。
注册由 `construction.bootstrap.register_constructors` 统一触发。

> 当前有哪些实现、文件职责、行为铁律归 [`src/construction/AGENTS.md`](../../src/construction/AGENTS.md)，本 spec 只列契约。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | 本层接收接入层产出的 MemoryUnit 做落盘+索引 |
| S03-control | Engine.write 路径调用本层 IndexBuilder.build，Engine.evolve 路径调用本层 Evolver |
| S04-retrieval | 检索层消费本层构建的索引 |
| S06-storage | 本层通过注入的 Store 抽象做真源与索引持久化 |
| S07-common | 本层消费 Chunker/Tokenizer/Embedder/FeatureExtractor/LLM/Reranker 共享插件 |
| architecture.md §4/§6/§8 | 分层记忆结构 / 多形式索引 / 记忆自演进 |
