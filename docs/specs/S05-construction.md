# S05 — 构建层（Construction Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/construction/ |
| 最近一次修订日期 | 2026-08-25 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md, docs/features/construction/F01-construction-spec-design.md, docs/features/construction/F02-dynamic-extraction-consolidation.md, docs/features/construction/F03-extraction-layer-integrity.md, docs/features/construction/F04-cc-memory-compat.md, docs/features/construction/F05-construction-spec-multimodal-design.md, docs/features/construction/F06-unified-index-builder.md, docs/features/construction/F07-memory-write-entry.md, docs/features/common/F01-memory-layer.md, docs/features/common/F03-scope-space-isolation.md, docs/features/common/F08-memory-tree.md, docs/features/retrieval/F03-metadata-filtering.md |

## Metadata 派生与索引契约

单源派生复制 `user_metadata`；多源派生只保留所有来源都存在且值相等的字段。
`system_metadata` 只保留相等的必要上下文，`infer` / `procedural` / `middle` 不传播。
IndexBuilder 以带命名空的逻辑路径投影两类字段。

## 范围 / 边界

**管什么**：
- 真源落盘（调用 Storage 写入记忆单元）
- 信息提取（低抽象粒度：事实/事件/偏好）
- 抽象与精炼/升华（高抽象粒度：画像/长期偏好/可复用技能）
- 关联分析（实体共指/因果链/引用关系）
- 多维分类（认知角色/主题/重要度）
- 候选落盘前巩固（ADD/UPDATE/SUPERSEDE/NOOP）
- 多形式索引构建（文档/关键词/向量/图，按配置启用）
- 记忆自演进（抽取 → 关联 → 冲突消解 → 升华 → 遗忘/降权）
- 树结构派生、区间重建与双向边维护（目标契约，尚未实现）

**不管什么**：
- 不做鉴权（由 `jiuwen_memory/api` 层负责）
- 不做检索（由 `jiuwen_memory/retrieval` 层负责）
- 不实现存储后端（通过注入的 Store 抽象间接调用）
- 不实现共享插件（Chunker/Tokenizer/Embedder/FeatureExtractor/LLM 由 `jiuwen_memory/common` 注入；Reranker 不被本层使用——去重 LLM 直接判定）

## 不变量

1. **落盘由本层负责**：接入层产出 MemoryUnit 后，记忆本体的写入由本层完成——统一经 `IndexBuilder`，由其内部调用 Storage。
2. **索引是可重建派生（目标契约）**：索引应全部从真源重建，`IndexBuilder.rebuild()` 应提供
   非破坏式恢复保障。当前实现中的 Forward/Fulltext/Vector/Hybrid/Unified/Entity Builder
   `rebuild()` 均为 no-op，不能据此宣称已具备“删索引不丢数据”的恢复能力；该缺口需要按本
   spec 的目标契约补齐，而不是将 Entity 视为唯一例外。
3. **provenance 回指来源**：派生记忆单元的 `provenance` 字段记录由哪些 unit 演进而来。
4. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
5. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `ConstructionOperator`。
6. **构建与存储解耦**：算子负责构建逻辑（生成索引投影），持久化由注入的 Store 承担。正排
   同样遵循此模式——`ForwardIndexBuilder` 生成 KV 记录投影，写入注入的 KV 端口。
7. **scope 原生隔离**：构建索引时将来源 `MemoryUnit.scope` 作为 Store 方法的显式
   入参下推；`VectorRecord` / `Document` / `Node` 等记录结构不混入 scope 字段。
8. **去重召回与判定分离**：去重召回（用哪个索引）由 `Dedup` 接口承担，判定（ADD/UPDATE/SUPERSEDE/NOOP）与落盘由 Evolver 实现承担——`OrchestratingEvolver._evolve_extract`（legacy，`_dedup_batch` 判定+落盘耦合）或 `DynamicEvolver._evolve_extract`（dynamic，consolidate 只判定、落盘延后到 reflect 之后）。装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`。
9. **构建层不依赖 control**：`DynamicEvolver`/`OrchestratingEvolver` 的 SUPERSEDE 与 FORGET 经 `IndexBuilder` 完成，不经 `LifecycleManager`。
10. **Dedup 与 IndexBuilder 共享底层 Store**：去重召回检索的是已索引内容，`Dedup` 实现取的 `VectorStore`/`FulltextStore` 必须与 IndexBuilder 写入的是同一实例（按字段名缓存命中）。
11. **派生 metadata 键保持类型稳定**：当前 Classifier 只更新
    `MemoryUnit.tier` / `MemoryUnit.tags`，不约定额外分类 metadata 键；
    LLM Extractor / LLM Abstractor 写出 `system_metadata.confidence` 时使用浮点字符串，
    非 LLM 实现不保证存在该键。Evolver 写回 `metadata.dedup_similarity`、
    `DeleteMode.DOWNWEIGHT` 写回 `system_metadata.importance` 时也使用浮点字符串。
    查询侧不对这些键做隐式类型转换。
12. **consolidate 只判定不落盘**：`DynamicEvolver` 的 consolidate 步只产出
    `ConsolidateDecision`（候选 + 决策 + 已有记忆 + 相似度），落盘延后到
    reflect 之后统一执行。reflect 默认 no-op；当前只有对子候选的原地修改能影响落盘。
13. **索引投影保留 metadata 命名空与类型**：Vector/Fulltext IndexBuilder
    分别投影 `system_metadata.<key>` 和 `user_metadata.<key>`，再写入一级系统真源字段；
    时间投影为 epoch 毫秒，
    `t_invalid=None` 仅在索引中写为 `T_INVALID_OPEN`，`t_event=None` 恒写为
    `T_EVENT_UNKNOWN=0`（F07 派生常为此值，避免事件窗下推按缺失字段排他），
    不改写真源。
14. **索引删除按 MemoryUnit 定位**：`IndexBuilder.remove` 接收带 Scope 的 MemoryUnit，禁止维护仅按 unit id 的单值 Scope 缓存；同一逻辑 id 在不同 Scope 的索引互不影响。
15. **记忆写入只经 IndexBuilder**：Evolver 与上层调用方不得直接调用 `Storage` 的
    `add`/`update`/`delete`；正排与各派生索引由 `IndexBuilder` 统一编排，使调用方不感知
    底层存储拓扑。剩余的合法调用方只有 `UnifiedIndexBuilder`（转发给一体化后端）与
    `LifecycleManager`（状态回写）。读取（`get`/`list`/`scopes`）不受此约束。
16. **索引状态由调用方判定，构建算子不解读 `lifecycle`**：记忆处于什么状态、因而该对索引
    做什么，由调用方判断后调对应方法；`IndexBuilder` 只执行被要求的操作。如归档/遗忘为
    `update(mode=FORWARD_ONLY)`（回写本体新状态）+ `remove(mode=SOFT)`
    （移出检索）两条互不重叠的指令。
17. **一个子 builder 只负责一种索引形式，端口统一从 `Storage` 取**：写侧子 builder 与读侧
    recaller 因此取自同一个 `Storage` 实例的同一端口，读写不分叉。
18. **正排最先出现、最后消失**：`build`/`update` 正排在前，`remove` 正排最后。正排先删会
    留下孤儿派生索引，而删除路径的扫描源正是正排，此后无法清理。
19. **叶权威、父可重建**（目标契约，尚未实现）：普通写入或来源转换产生的叶是权威事实；
    `HierarchyComposer` 生成的父节点是派生物。重建父层不得删除、改写或归档权威叶内容。
20. **层级边双向一致**（目标契约，尚未实现）：父 `child_ids` 与子 `parent_id` 必须在同一构建操作中维护，
    并在写索引前通过同 org+space、无环、单 kind 单父、区间覆盖校验（跨细粒度 scope 时边可解析）。
21. **父标注先于持久化和索引**（目标契约，尚未实现）：新派生父节点先经 `LayerAnnotator` best-effort 生成 L0/L1，
    再写 KV 和索引。标注失败保留空 layers 并继续，不得因摘要失败丢失结构结果。

## 接口契约

### ConstructionOperator（基类，`base.py`）

```python
class OperatorType(str, Enum):
    EXTRACTOR / ABSTRACTOR / ASSOCIATOR / CLASSIFIER / INDEX_BUILDER / EVOLVER / LAYER_ANNOTATOR
    # 目标新增：HIERARCHY_COMPOSER

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

派生单元的 `tier` 由 LLM 在抽取时产出；`tags` 为源 unit 的 write tags ∪ LLM 主题
tags ∪ 系统标记（`extracted` / `procedural`）。`layers`（L0/L1 分层标注）不由 Extractor
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

`VideoMemoryExtractor` 是 Extractor 的视频实现：只消费 ACTIVE 的源视频单元，跳过已有
`system_metadata.modal_type=multimodal` 的派生单元，将 Normalizer 输出的 clips/events
转换为 CLM/ELM `MemoryUnit`。两类单元使用 `system_metadata.memory_level` 标识层级，
不生成 L0/L1；事件的 `child_clm_source_ids` 为 `list[str]`，并通过 provenance 保留源
视频血缘。

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
4. **落盘**：按每个 `ConsolidateDecision.decision` 执行 ADD/UPDATE/SUPERSEDE/NOOP——ADD/SUPERSEDE 调 `IndexBuilder.build`，UPDATE 调 LLM 合并内容后 `IndexBuilder.update`，NOOP 跳过。记忆本体的交付含在 IndexBuilder 内部。

**procedural 路径**：`_evolve_extract` 检测到 procedural=true 时 `super()._evolve_extract(units)` 走父类行为（不收集 context、不判定、直接落盘）——procedural 语义是"记成一条 how-to"，无需动态判定。

**PromptRegistry**（`prompt_registry.py`）：按 `phase + key` 查询命名 prompt 文本。metadata 只写 prompt 的 **key**，不写全文。装配列以 yml 顶层 `prompts` / `globals["prompts"]` 为默认数据；引入 `ConfigSource`（S08）后，查询路径应能经 `fetch("prompts.<phase>.<name>")` 晚绑定，而不要求业务 API 传入 prompt 全文。extract 步的 registry 由 `ExtractorProducer._build` 注入 `DynamicLLMExtractor`；consolidate 步的 registry 由 `DynamicEvolver._build` 注入。reflect key 当前只透传给候选，默认实现不查询 registry，子类可按 `PHASE_REFLECT` 扩展。

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

**IndexBuilder 是记忆写入的唯一入口**：调用方只调本接口，正排（记忆本体）与各派生索引
由实现内部编排；上层不得自行调用 `Storage` 的写接口（见不变量 15）。

**正排是一种索引形式**，与倒排、向量、实体反向平级，由 `ForwardIndexBuilder` 承载。

| 方法 | 签名 | 语义 |
|------|------|------|
| `build` | `(units, *, mode: IndexWriteMode = ALL) -> None` | 建立已启用的各形式索引；`RETRIEVAL_ONLY` 表示记忆本体已存在、只补建检索索引，`FORWARD_ONLY` 表示只交付本体 |
| `update` | `(units, *, mode: IndexWriteMode = ALL) -> None` | 更新各形式索引；`FORWARD_ONLY` 表示只回写记忆本体、检索索引不动，`RETRIEVAL_ONLY` 表示只刷新检索索引 |
| `remove` | `(units, *, mode: IndexRemoveMode = HARD) -> None` | 按每个 MemoryUnit 自带 Scope 删除索引（幂等）；`SOFT` 为软删除——只移出检索索引（search/recall 不再召回），记忆本体保留、get/list 仍可读；`HARD` 物理删除本体与全部索引 |
| `rebuild` | `() -> None` | **目标契约**：从记忆本体全量重建派生索引（删索引不丢数据的保障）；重建时也重新投影 hierarchy metadata。当前实现尚未提供该能力。 |

写接口枚举 `IndexWriteMode`（`ALL` / `FORWARD_ONLY` / `RETRIEVAL_ONLY`）表达写入范围，
删除接口枚举 `IndexRemoveMode`（`SOFT` / `HARD`）表达删除语义，均归口
`jiuwen_memory/storage/types.py`：

| 取值 | 用途 |
|---|---|
| `build(mode=RETRIEVAL_ONLY)` | 索引迁移与部分失败后的补建——本体已在，只补检索索引 |
| `update(mode=FORWARD_ONLY)` | 生命周期治理——只回写本体新状态，检索索引另行处置 |
| `remove(mode=SOFT)` | 归档 / 遗忘 / 跨 pipeline 迁移——本体保留，仅退出检索 |

**本层不解读 `unit.lifecycle`**：记忆处于什么状态、因而该对索引做什么，由调用方判定后
调对应方法。如遗忘为 `update(mode=FORWARD_ONLY)` + `remove(mode=SOFT)` 两条
互不重叠的指令（见不变量 16）。

**顺序约定：正排最先出现、最后消失。** `build`/`update` 正排在前——派生写失败时本体仍在、
索引可重建；`remove` 派生在前、正排最后——正排先删会留下孤儿派生索引，而删除路径的扫描源
正是正排，此后再也清不掉。

目标 hierarchy metadata 重建必须实现真实的 KV `scopes()` + `list(scope)` 枚举，解码
每个 `MemoryUnit` 后重新生成内容层与 hierarchy metadata；不得从旧索引反推。该能力
落地前，不得把 `rebuild()` 接口存在视为“删索引不丢数据”的当前保证。

**build 路径**（按配置启用的索引类型，各实现独立构建）：
```
MemoryUnit
├─ 关键词路：unit.content 整篇不切片
│   → Document(id=unit.id, text=unit.content,
│              metadata={unit_id,tier,lifecycle,tags,source,content_layer="l2",...hierarchy})
│   → FulltextStore.insert
├─ 向量路：Chunker 切片
│   → Chunker.chunk(unit.content) → chunks
│   → Embedder.embed(chunks)
│   → VectorRecord(id={unit.id}-{chunk.id}, vector,
│                  metadata={unit_id,tier,lifecycle,seq,content_layer="l2",...hierarchy})
│   → VectorStore.insert + KVStore 维护 chunk_id 跟踪（供 update/remove 读旧 chunk）
├─ L0/L1 分层路：
│   → unit.layers.l0/l1 非空且对应 store 已注入 → 整段不切片
│   → VectorRecord.id={unit.id}-layer-l0/-layer-l1
│   → Document.id={unit.id}:l0/:l1
│   → metadata 保留 content_layer，并复制同一 unit 的 hierarchy metadata
│   → 写独立 FulltextStore/VectorStore 实例（不同 collection/index = 分表，与 content 物理隔离）
│   → store 为 None 跳过该层（向后兼容 + 配置降级）；update 先删后建，remove 幂等删
├─ 图路（Evolver ASSOCIATE 模式编排）：
│   → FeatureExtractor → Node → GraphStore.insert
│   → Associator.associate → Edge → GraphStore.insert
├─ 实体反向索引路（EntityIndexBuilder，`entity_enabled=true` 时启用）：
│   → EntityIndexAdmissionPolicy.decide（SEMANTIC/CORE/EPISODIC 准入，WORKING/ARCHIVAL 跳过）
│   → 消费 unit.entities 明文构造 EntityMention（type 统一 PROPER；为空跳过该 unit，无 spaCy 兜底）
│   → EntityNormalizer.normalize + hash_entity_text（sha256，精确匹配 key）
│   → EntityLinkService 两级归并：hash 精确命中 → LINK；未命中 → INSERT 新实体（不做向量归并）
│   → EntityStore.execute_operations（bulk，per-item 粒度，partial failure 不抛）
│   → update 走「unlink 旧链接 + link 新内容」；SUPERSEDED（仅 lifecycle 变）不 unlink（保留 as_of 回溯）
│   → 失败全程 try/except 吞异常，不中断 build 主链路（增强层，坏了不拖累主流程）
├─ HybridIndexBuilder：纯编排，组合 forward / fulltext / vector / entity 四个子 builder
│   （默认实现；entity 子 builder 在 entity_linker=None 时跳过）
└─ 统一存储直写路：由 Storage 自身建立全部索引时，实现退化为按 Scope 转发
```

> 注：文档索引（path → unit_id 映射）与 FusionStore 融合索引不属于本文已固化的构建接口契约，属设计预留。
> L0/L1 分层索引的召回接入未落地（为披露层预留），详见 F01。

`layers_index_enabled` 默认 `true`；对应 L0/L1 store 未配置时仅跳过该层。L0/L1/L2
记录均以 `unit_id` 指向同一真源 unit。记录到 unit 的折叠由单路 recaller 完成；
不同 recaller 的结果再由融合阶段按 `unit_id` 累加贡献，IndexBuilder 不负责召回聚合。

目标 hierarchy metadata 的精确键、空值和区间表示由
[S06-storage.md](S06-storage.md) 单点定义。IndexBuilder 必须把同一 unit 的结构
metadata 一致投影到已启用的 L0/L1/L2 索引记录；索引是派生物，必须可从 KV 中的
`MemoryUnit` 重建。

### HierarchyComposer（目标契约，尚未实现）

`HierarchyComposer` 与 `Extractor` / `Abstractor` 等并列，同属 `ConstructionOperator`：
实现建树/区间替换算法，由控制层 `evolve(..., mode=HIERARCHY)` → Evolver 调度调用；
不自行鉴权、不自行提交后台任务，也不替代 IndexBuilder。普通 EXTRACT/CONSOLIDATE
等模式不暗改 `HierarchyRef`。

```python
@dataclass(frozen=True)
class HierarchyComposeProfile:
    kind: HierarchyKind
    leaf_role: HierarchyRole
    parent_roles: tuple[HierarchyRole, ...]
    stage_options: dict[str, dict[str, str]] = field(default_factory=dict)

@dataclass
class HierarchyComposeOptions:
    kind: HierarchyKind
    leaf_role: HierarchyRole
    parent_roles: list[HierarchyRole]
    span_start: datetime | None = None
    span_end: datetime | None = None
    replace_existing: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass
class HierarchyComposeRequest:
    scope: Scope
    leaf_ids: list[str]
    options: HierarchyComposeOptions

@dataclass
class HierarchyRepair:
    unit_id: str
    issue: str
    expected_parent_id: str = ""
    observed_parent_id: str = ""

@dataclass
class HierarchyComposeResult:
    created_parent_ids: list[str] = field(default_factory=list)
    updated_child_ids: list[str] = field(default_factory=list)
    replaced_parent_ids: list[str] = field(default_factory=list)
    repair_required: list[HierarchyRepair] = field(default_factory=list)
    complete: bool = True

class HierarchyComposer(ConstructionOperator):
    def build(self, request: HierarchyComposeRequest) -> HierarchyComposeResult: ...
    def replace_in_span(self, request: HierarchyComposeRequest) -> HierarchyComposeResult: ...
```

`HierarchyComposeProfile` 是装配期不可变配置，按 `kind` 唯一注册；重复 kind、
空 `parent_roles`、重复 role、`leaf_role` 出现在父序列中、未注册 stage 或 kind
不支持该 role 序列时拒绝装配。`stage_options` 的外层键是稳定 stage 名，内层值只允许
字符串配置；运行时 Policy 不修改 profile。

`leaf_ids` 必须非空、无重复并全部解析到 `request.scope`；其顺序是输入稳定顺序。
`parent_roles` 必须非空，是从近叶到远叶的待构建父角色序列；不得重复，也不得包含
`leaf_role`。
kind/role 必须使用 S07 定义的枚举。TIME 请求必须给出成对且有效的 span；非 TIME
可省略。`metadata` 只复制到新派生父节点，不得覆盖 id、scope、tier、temporal、
provenance、supersedes、lifecycle 或 hierarchy 等核心字段。

调用方按 `replace_existing` 确定唯一分派：`false` 调用 `build`，发现冲突旧父时整体
失败；`true` 调用 `replace_in_span`，并要求请求具有成对且有界的 span。显式
`evolve(HIERARCHY)` 使用调用方给出的值；S03 的 ensure 和 auto derive 固定组装为
`replace_existing=true`，使同一区间的重复任务成为受控重建，而不是产生第二套父层。

`build` 读取权威叶，在请求 span 内创建指定父角色，写入父的有序 `child_ids` 并回写
直接子的 `parent_id`。它不得隐式替换 span 外的父节点；发现已有冲突父边时返回校验
错误，不做部分挂接。父节点的 tier 由内容决定，不得从 role 硬推导；角色与 tier 的
设计指导映射由 [F08-memory-tree.md](../features/common/F08-memory-tree.md)
记录，不构成本接口的枚举等价约束。

TIME 构建以 `MemoryUnit.temporal.t_event` 作为叶事件时间，以
`HierarchyRef.span_start/span_end` 作为结构覆盖区间。父区间覆盖所有直接子区间，
直接子按区间起点、事件时间和输入稳定顺序排序。`HierarchyKind.TIME` 不替代
`MemoryUnit.temporal`，也不替代 `RecallChannel.TEMPORAL`。

新父正文和 segments 先构造，再调用 `LayerAnnotator`；无 annotator 或标注失败时以空
layers 降级。只有通过结构校验后，才按“KV 真源 → 内容索引”顺序持久化父与被改写的子。

`replace_in_span` 仅选择与请求 span 相交、kind 匹配且角色位于 `parent_roles` 的旧派生
父节点。它必须先计算完整替换集并验证新树，然后：

1. 从旧父 `child_ids` 移除边，并清空仍指向旧父的直接子 `parent_id`；
2. 将相交旧派生父默认转为 `LifecycleState.ARCHIVED`，并从活动内容索引移除；
   `replace_in_span` 本身不物理 PURGE 真源，物理回收必须走 S03 的显式生命周期策略；
3. 保留全部权威叶及其 segments、temporal、provenance 和生命周期；
4. 写入新父，回挂双向边，再更新受影响索引；
5. 边界切过旧父时扩大替换范围到完整旧父，或拒绝请求，不留下半父节点。

期望的原子边界是同 org+space 下“旧边断开、新父写入、新边挂接、旧父退役”的一次提交
（子叶可驻留不同 session/user Scope，由边定位）。
支持事务的 KV 后端必须原子提交；不支持事务时必须先暂存并验证新父，按可恢复顺序写入，
具体顺序是“写入尚未挂活动边的新父 → 按稳定顺序切换子边 → 归档旧父 → 更新索引”。
失败后返回 `complete=false` 和逐项 `repair_required`，且不得删除权威叶。调用方不得把
带 repair 项的结果当作成功；construction/control 负责重试或一致性修复。

### Evolver（`evolver.py`）

记忆自演进，持续驱动演进闭环。两个实现：`OrchestratingEvolver`（注册名 `orchestrating`，legacy）与 `DynamicEvolver`（注册名 `dynamic`，子类，EXTRACT 走动态 prompt 四步）。`evolve` 按模式分派到 `_evolve_extract` / `_evolve_consolidate` / `_evolve_associate` / `_evolve_forget` 四个可覆盖方法。

| 方法 | 签名 | 语义 |
|------|------|------|
| `evolve` | `(request: EvolveRequest) -> EvolveResult` | 对一批记忆单元执行指定阶段的演进，返回变更结果 |

**EvolveMode**：
- `EXTRACT` — 信息提取
- `ASSOCIATE` — 关联分析
- `CONSOLIDATE` — 冲突消解（近重复融合/矛盾标记失效）
- `FORGET` — 遗忘/降权（过期/低价值记忆归档）
- `HIERARCHY` — 显式创建或重建父节点及双向包含边（目标新增）

```python
@dataclass
class EvolveRequest:
    units: list[MemoryUnit]
    mode: EvolveMode
    metadata: dict[str, str] = field(default_factory=dict)
    hierarchy_options: HierarchyComposeOptions | None = None
```

`metadata` 承载 correlation id、触发来源等请求级透传信息，不写回 unit 核心字段。
仅 `HIERARCHY` 接受 `hierarchy_options`，且必须提供 kind、leaf_role、parent_roles 与
TIME 所需 span；其他 mode 提供该 options 时拒绝。实现迁移期间可以保留
`evolve(units, mode)` 作为兼容入口，其语义等价于构造不带 metadata/options 的请求；
该入口不能触发 HIERARCHY。

**EvolveResult**：

```python
@dataclass
class EvolveResult:
    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    forgotten_ids: list[str] = field(default_factory=list)
    hierarchy_result: HierarchyComposeResult | None = None
```

`hierarchy_result` 只在 HIERARCHY 模式返回结构结果与修复报告，其他 mode 为 `None`。

各模式对 hierarchy 的行为：

| 路径/模式 | hierarchy 契约 |
|---|---|
| 普通 `write` | 默认空；调用方提供经校验的叶字段时可保留 kind/role/span，但不得写父或子边 |
| `EXTRACT` | 既有节点不变；新派生节点默认空，`provenance` 来源不自动成为父 |
| `ASSOCIATE` | hierarchy 不变；关系只写 GraphStore，不写 `parent_id` |
| `CONSOLIDATE` | 既有节点不变；新合成节点默认空，需单独建树 |
| `FORGET` | 不改其他节点 kind/role/span；断开直接父边和全部直接子边，不级联删除父或任何子孙 |
| `HIERARCHY` | 委托 HierarchyComposer 创建/替换父节点并一致回写直接子边 |

### Dedup（`dedup.py`）

去重召回，由 Evolver 实现（`OrchestratingEvolver._dedup_batch` / `DynamicEvolver._consolidate_step`）及 infer 上下文收集调用。召回 + 阈值过滤 + 加载 + 聚合取 max 全在实现内完成；判定与落盘动作归调用方（evolver）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `recall` | `(candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]` | 对候选召回已有相似记忆，返回 (unit, score) 列表（按 score 降序）；已完成过滤自身、过滤非 ACTIVE、按 unit 聚合取 max、按 min_similarity 过滤低分。空列表 → 调用方判 ADD |

**score 量纲**：向量路=cosine（0~1）；倒排路=FulltextStore 后端原生分，`memory` / `elasticsearch` 均为无上界的 Okapi BM25 原始分。（**遗留**：`KeywordDedup` 的 `min_similarity` 缺省 0.5 是按旧的词重叠率标定的；`memory` 与 `elasticsearch` 两个 FulltextStore 后端现均返回无上界的 BM25 原始分，该阈值需重新标定——`elasticsearch` 后端一直如此，本次仅使 `memory` 与之一致。缺省装配走 `VectorDedup`，仅 `vector_enabled=False` 时受影响。）

**两个实现**（装配按 `vector_enabled` 选）：
- `VectorDedup`（`vector`）— Embedder → VectorStore.search，cosine；record_id 为 `{unit_id}-{chunk_id}` 需解析
- `KeywordDedup`（`keyword`）— FulltextStore.search，BM25 原始分；Document.id = unit.id 恒等无需解析

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
| `entities` | list[str] | L2 记忆里由大模型抽取得到的实体文本（明文）。entity linker 建反向索引时只消费本字段构造 `EntityMention`，为空时直接跳过该 unit（已砍 spaCy 兜底，无回退抽取，见 [F06](../features/retrieval/F06-entity-recall-channel.md)）。默认空，向后兼容 |

构建层直接消费 S07 定义的 `HierarchyRef` 和层级枚举。目标父节点复用既有
segments、layers、tier 和 metadata 槽位，结构边只写 hierarchy；`content/assets/source`
仍是基于 segments 的只读合并视图。精确类型与默认值见 [S07-common.md](S07-common.md)。

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
jiuwen_memory/construction/<算子>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`ExtractorProducer` / `AbstractorProducer` / `AssociatorProducer` / `ClassifierProducer` / `IndexBuilderProducer` / `DedupProducer` / `EvolverProducer`。
注册由 `construction.bootstrap.register_constructors` 统一触发。

> 当前有哪些实现、文件职责、行为铁律归 [`jiuwen_memory/construction/AGENTS.md`](../../jiuwen_memory/construction/AGENTS.md)，本 spec 只列契约。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | 本层接收接入层产出的 MemoryUnit 做落盘+索引 |
| S03-control | Engine.write 路径调用本层 IndexBuilder.build，Engine.evolve 路径调用本层 Evolver |
| S04-retrieval | 检索层消费本层构建的索引 |
| S06-storage | 本层通过注入的 Store 抽象做真源与索引持久化 |
| S07-common | 本层消费 Chunker/Tokenizer/Embedder/FeatureExtractor/LLM/Reranker 共享插件 |
| S08-config | Prompt 文本与模型晚绑定经 ConfigSource；业务入参只传 prompt key |
| architecture.md §4/§6/§8 | 分层记忆结构 / 多形式索引 / 记忆自演进 |
