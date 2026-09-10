# S05 — 构建层（Construction Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/construction/ |
| 最近一次修订日期 | 2026-09-10 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 归属判定算子 | `Router` 的契约与决策见 [F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md) |
| 关联特性文档 | docs/features/F01-system-spec-design.md, docs/features/construction/F01-construction-spec-design.md, docs/features/construction/F02-dynamic-extraction-consolidation.md, docs/features/construction/F03-extraction-layer-integrity.md, docs/features/construction/F04-cc-memory-compat.md, docs/features/construction/F05-construction-spec-multimodal-design.md, docs/features/construction/F06-unified-index-builder.md, docs/features/construction/F07-memory-write-entry.md, docs/features/construction/F08-entity-schema-extension.md, docs/features/common/F01-memory-layer.md, docs/features/common/F03-scope-space-isolation.md, docs/features/common/F08-memory-tree.md, docs/features/retrieval/F03-metadata-filtering.md |

## Metadata 派生与索引契约

单源派生复制 `user_metadata`；多源派生只保留所有来源都存在且值相等的字段。
`system_metadata` 只保留相等的必要上下文，`infer` / `procedural` / `middle` 不传播。
IndexBuilder 以带命名空的逻辑路径投影两类字段。

## 范围 / 边界

**管什么**：
- 真源落盘（统一经 IndexBuilder 写入记忆单元及派生索引）
- 信息提取（低抽象粒度：事实/事件/偏好）
- 抽象与精炼/升华（高抽象粒度：画像/长期偏好/可复用技能）
- 关联分析（实体共指/因果链/引用关系）
- 多维分类（认知角色/主题/重要度）
- 候选落盘前巩固（ADD/UPDATE/SUPERSEDE/NOOP）
- 多形式索引构建（文档/关键词/向量/图，按配置启用）
- 记忆自演进（抽取 → 关联 → 冲突消解 → 升华 → 遗忘/降权）
- 显式候选上的 TIME snapshot→time_span 构建、受限区间替换与双向边维护

**不管什么**：
- 不做鉴权（由 `jiuwen_memory/api` 层负责）
- 不做检索（由 `jiuwen_memory/retrieval` 层负责）
- 不实现存储后端（通过注入的 Store 抽象间接调用）
- 不实现共享插件（Chunker/Tokenizer/Embedder/FeatureExtractor/LLM 由 `jiuwen_memory/common` 注入；Reranker 不被本层使用——去重 LLM 直接判定）

## 不变量

1. **落盘由本层负责**：接入层产出 MemoryUnit 后，记忆本体的写入由本层完成——统一经 `IndexBuilder`，由其内部调用注入的 StoreManager/DomainStore（端口名可经 `params.<ns>_store` 具名选择）。
2. **索引是可重建派生（目标契约）**：索引应全部从真源重建，`IndexBuilder.rebuild()` 应提供
   非破坏式恢复保障。当前实现中的 Forward/Fulltext/Vector/Hybrid/Unified/Entity Builder
   `rebuild()` 均为 no-op，不能据此宣称已具备“删索引不丢数据”的恢复能力；该缺口需要按本
   spec 的目标契约补齐，而不是将 Entity 视为唯一例外。
3. **provenance 回指来源**：内容抽取/升华产物的 `provenance` 字段记录演进来源；
   结构父的包含关系只写 hierarchy，不因此生成 provenance 或 supersedes。
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
15. **记忆写入只经 IndexBuilder**：Evolver 与上层调用方不得直接调用 `DomainStore` 的
    `add`/`update`/`delete`；正排与各派生索引由 `IndexBuilder` 统一编排，使调用方不感知
    底层存储拓扑。剩余的合法调用方只有 `UnifiedIndexBuilder`（全部写经 DomainStore 领域接口，
    自身只做 `vector_enabled` 门控的 content 向量化并回填 `MemoryUnit.vectors` 随本体下传）与
    `LifecycleManager`（状态回写）。读取（`get`/`list`/`scopes`）不受此约束。
16. **索引状态由调用方判定，构建算子不解读 `lifecycle`**：记忆处于什么状态、因而该对索引
    做什么，由调用方判断后调对应方法；`IndexBuilder` 只执行被要求的操作。如归档/遗忘为
    `update(mode=FORWARD_ONLY)`（回写本体新状态）+ `remove(mode=SOFT)`
    （移出检索）两条互不重叠的指令。到期清扫（sweep）的索引移出由控制层
    `MemoryEngine.sweep_expired` 编排（先 `remove(SOFT)`、成功后回写真源，见 S03），
    `LifecycleManager` 不直接触碰索引。
17. **一个子 builder 只负责一种索引形式，端口统一从 `StoreManager` 取**：写侧子 builder 与
    读侧 recaller 因此取自同一个全局 manager 的同一命名端口（`params.<ns>_store` 指名），
    读写不分叉。
18. **正排最先出现、最后消失**：`build`/`update` 正排在前，`remove` 正排最后。正排先删会
    留下孤儿派生索引，而删除路径的扫描源正是正排，此后无法清理。
19. **叶权威、父可重建**：普通写入或来源转换产生的叶是权威事实；
    `HierarchyComposer` 生成的父节点是派生物。重建父层不得删除、改写或归档权威叶内容。
20. **候选层级边双向一致**：父 `child_ids` 与子 `parent_id` 在同一构建操作中维护，
    候选在任何写入前通过同 org+space、无环、单 kind 单父、区间覆盖校验；完整 Scope + id
    定位跨细粒度 Scope 引用。保存中途仍可能失败，失败结果不得被当作全库已一致。
21. **父标注先于持久化和索引**：显式配置 `layer_annotator` 后，新派生父节点经
    `LayerAnnotator` best-effort 生成 L0/L1，再写 KV 和索引；短正文遵守原标注阈值。
    未配置或标注失败时保留空 layers，不得因摘要失败丢失结构结果。

## 接口契约

### ConstructionOperator（基类，`base.py`）

```python
class OperatorType(str, Enum):
    EXTRACTOR / ABSTRACTOR / ASSOCIATOR / CLASSIFIER / INDEX_BUILDER / EVOLVER
    LAYER_ANNOTATOR / ROUTER / HIERARCHY_COMPOSER

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

#### 可选 Entity Schema 抽取契约

Schema 抽取先选择本轮相关 entity type/property，再使用同一选中集合生成并校验属性。
生成器不得借完整 Catalog 放行未选中的属性。每个属性候选必须包含 Schema 内的实体类型和
属性名、非空事实文本，以及同 Scope 输入中的一个或多个 `source_unit_ids`。

每个合法属性生成一个独立 MemoryUnit。属性 Unit 的 `entities` 为空；Schema 名称、版本、
实体类型和属性名写系统 metadata。属性成功落盘后，实体明文和属性名聚合写回相应 Source
MemoryUnit 的 `entities`，并经 `IndexBuilder.update(mode=ALL)` 同时回写本体和刷新检索索引；
来源业务 metadata 仍按通用派生规则写入 user metadata。
完整可解析的事件日期/时间可写 `temporal.t_event`，但时间不是属性合法性的必要条件。

Schema Evolver 对非 procedural 写入采用 Source-first，并将属性候选直接 ADD，不进入普通文本
相似度 Dedup。Extractor 连续重试后仍失败时只放弃 Schema 派生，不回滚已持久化 Source。
该链路必须显式装配；默认 Extractor/Evolver 行为不变。

### DynamicEvolver（`evolver_impl/dynamic_evolver.py`）

`OrchestratingEvolver` 的子类，覆盖 `_evolve_extract` 走动态 prompt 四步编排：`extract → consolidate(判定) → reflect → 落盘`。其余四模式（CONSOLIDATE/ASSOCIATE/FORGET/HIERARCHY）继承父类行为。注册名 `dynamic`，与 `orchestrating` 平级，同属 `evolver` 顶层命名空间——装配或 pipeline profile 选哪个 evolver 实例即启用哪条 EXTRACT 路径。

| 方法 | 签名 | 语义 |
|------|------|------|
| `_evolve_extract` | `(units: list[MemoryUnit]) -> EvolveResult`（覆盖父类） | 动态四步：抽取候选 → 巩固判定 → 反思 → 按判定落盘 |

**四步语义**：

1. **extract**：委托父类持有的 `Extractor.extract`，产出派生候选；把源 unit 的 consolidation/reflect prompt key 透传给候选；调 `_annotate_layers` 标注 L0/L1。
2. **consolidate（只判定不落盘）**：对每个候选调 `Dedup.recall` 召回已有记忆，按相似度阈值 + LLM 判定产出 `ConsolidateDecision`（候选 + `DedupDecision` + 已有记忆 + 相似度）。无命中 → ADD；高相似度且 `should_direct_noop` 为真（`score ≥ dedup_high_similarity` 且候选相对已有记忆无 `has_meaningful_delta`）→ NOOP；高相似但有实质差异 → 走 LLM；中段（`dedup_medium_similarity` ~ high）→ 查 `PromptRegistry` 取 consolidate prompt 调 LLM 判定；无 prompt 或 LLM 失败 → 回退规则（高相似且无实质差异才 NOOP，否则 ADD）。
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

### Router（`router.py`）

归属判定算子：逐条决定派生记忆落哪个空间、打哪些收窄维标签。与 `LayerAnnotator` 同构——契约在接口层、实现自注册、不注入即整段跳过。契约细节见 [F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md)。

| 方法 | 签名 | 语义 |
|------|------|------|
| `route` | `(units: list[MemoryUnit], ctx: RouteContext) -> list[RouteDecision]` | 逐条给出目标空间、标签、命中类别与是否丢弃 |

| 约束 | 内容 |
|---|---|
| 落点范围 | 只在 `ctx.candidates`（由 API 层按写权算出的候选空间）内选择，判定不可扩权 |
| 插入点 | 抽取之后、分层标注与去重之前。晚于去重会导致在源空间比对、在目标空间落盘 |
| 上下文通道 | `RouteContext` 经源单元的瞬态 metadata 键传入，存储层写入前移除、不落盘 |
| 生效范围 | 构建层插入点覆盖同步抽取与过程记忆两条路径；结论直写路径没有抽取环节，由 API 层把入参内容整体作为一条候选调本算子；后台演进通道从存储重读原文，取不到上下文键，不判定 |
| 失败处置 | 未装配即原样透传；判定异常时全批落 fallback 空间，不阻断写入 |
| 调用粒度 | 每批一次模型调用，不逐条调用 |

两个落盘不变量由本模块的公共函数承担，不放进任何 `Router` 实现内部——放实现内则换一个实现即可能漏掉，而漏掉的失效方向都是放行或静默收窄：

| 函数 | 作用 |
|---|---|
| `enforce_sanitized` | 目标类别声明「不含主体标识」时做一次确定性检查，命中即改落 fallback；不改写内容，也不阻断整批 |
| `with_all_tag_keys` | 补齐本次未出现的全部收窄维标签键为空串。检索侧的集合谓词在键缺失时判为不匹配，靠「不写键表示默认值」会静默收窄 |

两处调用点：本层的判定应用处与 API 层的单条判定入口。

配套数据类：`RouteContext` / `RouteDecision` / `MemoryClass` / `NarrowDim` / `SpaceNaming`，判定表由 `router` 配置命名空间声明，加载期十四条校验不通过即装配失败。解析产物由实现经 `Router.table` 向上暴露，API 层不另读一次配置。

`route_batch` 是两处调用点共用的入口：调判定、套两个落盘不变量、并对「落点不在候选集内」复判一次。三种情形一律落 fallback——未装配、判定抛异常、落点越出候选集；`apply_decisions` 随后把结果写回单元（改 scope、写判定标签与 `memory_class`、剔除判为丢弃的、剥除判定上下文的瞬态键）。

### IndexBuilder（`index_builder.py`）

多形式索引构建与维护。

**IndexBuilder 是记忆写入的唯一入口**：调用方只调本接口，正排（记忆本体）与各派生索引
由实现内部编排；上层不得自行调用 `DomainStore` 的写接口（见不变量 15）。

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
└─ 统一存储直写路：由 DomainStore 实现按自身能力建立索引时，实现退化为按 Scope 转发
```

> 注：文档索引（path → unit_id 映射）与 FusionStore 融合索引不属于本文已固化的构建接口契约，属设计预留。
> L0/L1 分层索引已有 keyword_l0/l1、vector_l0/l1 召回实现，是否启用取决于装配及命名端口；详见 F01。

`layers_index_enabled` 默认 `true`；对应 L0/L1 store 未配置时仅跳过该层。L0/L1/L2
记录均以 `unit_id` 指向同一真源 unit。记录到 unit 的折叠由单路 recaller 完成；
不同 recaller 的结果再由融合阶段按 `unit_id` 累加贡献，IndexBuilder 不负责召回聚合。

hierarchy metadata 的精确键、空值和区间表示由
[S06-storage.md](S06-storage.md) 单点定义。IndexBuilder 必须把同一 unit 的结构
metadata 一致投影到已启用的 L0/L1/L2 索引记录；索引是派生物，必须可从 KV 中的
`MemoryUnit` 重建。

### HierarchyComposer（TIME 两/三层构建）

`HierarchyComposer` 与 `Extractor` / `Abstractor` 等并列，同属 `ConstructionOperator`：
执行建树/区间替换，或由内部 Evolver 的 HIERARCHY 分派调用；不自行鉴权、查库、提交
后台任务，也不替代 IndexBuilder。支持 TIME 的 snapshot→time_span→scene，scene
可省略；公开任务由 Control 收齐候选、旧根及完整子树后进入本层，API/Job 契约见 S02/S03。

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
    tree_home_scope: Scope
    span_start: datetime | None = None
    span_end: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass
class HierarchyComposeRequest:
    leaves: list[MemoryUnit]
    options: HierarchyComposeOptions
    existing_parents: list[MemoryUnit] = field(default_factory=list)

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

#### 输入、范围与替换边界

`leaves` 必须非空，每个节点均为生命周期与结构状态 ACTIVE 的 TIME/snapshot；
`existing_parents` 包含 ACTIVE 的 TIME/time_span、scene，均驻留在 `tree_home_scope`
且具有直接子节点；scene 必须为根，time_span 可为根或 scene 的直接子。
节点以完整 Scope + id 去重；同名 id 位于不同 Scope 不算重复。
kind/role 必须使用 S07 枚举，当前 options 固定 `leaf_role=SNAPSHOT`、
`parent_roles=[TIME_SPAN]` 或 `[TIME_SPAN, SCENE]`；严格使用枚举和顺序。
其他 kind、跳层/逆序/空父角色序列拒绝。

`build` 仅用于首次挂接：不接受旧父或已有父引用。请求区间可整体省略；若给出，必须
成对有效，且每个输入叶与区间相交。`replace_in_span` 必须给出成对有界区间，已提供
旧根必须与区间相交，并带齐其全部父层和 snapshot，所有引用在输入集合内闭合，
scene 只能接 time_span，time_span 只能接 snapshot。旧根内部的父/叶可在请求窗口外，
但 Composer 不自动查询补齐；缺任一节点时在写入前拒绝。未挂接叶仍须与请求区间相交。
支持把完整两层旧树重建为三层；不允许用两层请求降级已有 scene 子树，避免隐式删层。

Composer 不能证明调用方没有遗漏数据库中的其他旧父或叶，范围选择的完整性由调用方
负责。它只在输入副本上生成候选，校验失败不修改输入对象、不写存储。候选树再经
`validate_ref` / `validate_tree` 检查，org+space 为硬边界，session 可跨；
不同非空 user/agent 默认不可跨，只有构造配置 `allow_cross_user=true` 才放开。

`metadata` 只复制到新父系统元数据，不覆盖 id、scope、tier、temporal、provenance、
supersedes、lifecycle 或 hierarchy。父用户元数据取子叶的相等交集并深拷贝；父系统元数据
先写请求 metadata，再上提配置键中一致的非空字符串，同名键以一致子值优先。
不自动全量继承系统字段，且
`infer` / `procedural` / `middle` 不传播。作者和权限模型没有因建树新增继承机制。

#### TIME 规则与 profile

输入按 UTC 的 `span_start`、`temporal.t_event`、原输入序稳定排序；朴素时间视为 UTC，
未知 t_event 只影响同起点排序，不替代 span。缺失/非法 span 在分组前拒绝。
相邻记录 session 变化、配置的系统上下文键变化、前条存在配置的非空结束信号，
或“当前 span_start − 前条 span_end”
大于 `gap_seconds` 时切段；恰等于阈值不切。它不是总时长限制，不计算语义相似度。

每组生成一个新 UUID 的 EPISODIC/time_span 父，这是本算法的产物选择，不把 tier
与 role 定义成同一枚举轴。父区间为覆盖直接子区间的最小包络（最早开始至最晚结束），
child_ids/child_scopes 有序且等长，
子父引用携带 tree_home_scope。父正文是“UTC 时间区间、记录数”表头加有限原文摘录，
保留省略数量。默认使用这一确定性摘录，显式启用 llm 时再替换父正文；
父实体保序合并，provenance/supersedes 为空；权威叶除 hierarchy 外全部字段不变。

scene 在稳定排序的 time_span 上依次判定：系统上下文变化 → 前段非空结束信号 →
累计区间包络超过时长上限 → 可选的相邻向量余弦低于阈值，任一命中即切分。
session 变化不是 scene 边界；默认 86400 秒是滚动跨度而非自然日，单个超长 time_span
保持原子性、不强拆。父覆盖直接子的最小包络，表头的片段/记录数量由代码统计。
scene 的 boundary/end-signal 键同时下传给 TimeSpanMerger，避免底层合并丢失切点；
结束信号只上提组尾值。信号按系统字段非空判断，不作自然语言或字符串布尔推断。

`HierarchyComposeProfile` 承载装配时固定的树形配置。当前 `hierarchy_profiles` 只接受
`time`，其 leaf_role 缺省为 snapshot，parent_roles 显式为 `[time_span]` 或
`[time_span, scene]`；单次请求可用配置链的短前缀，但不能超出配置上限。
未配置 profile 时，两种链都可用算法默认值；运行时 Policy 不修改 profile。
stage_options 接受 `TimeSpanMerger` 和 `SceneSegmenter`（后者要求配置链包含 scene）。
TimeSpanMerger 内层配置如下：

| 选项 | 默认 | 约束 |
|---|---|---|
| gap_seconds | 7200 | 正整数秒 |
| boundary_metadata_keys | 空 | 逗号分隔的系统上下文键，变化时切分 |
| carry_metadata_keys | 空 | 逗号分隔的系统键，只上提一致值，不参与切分 |
| end_signal_metadata_keys | 空 | 前条非空即切分，只将组尾信号上提到父 |
| summary_max_leaves | 20 | 正整数，最多摘录多少条子记录 |
| summary_max_chars_per_leaf | 60 | 正整数，每条摘录最多字符数 |
| summary_mode | structural | structural / llm；llm 要求显式模型依赖 |

SceneSegmenter 的 boundary_metadata_keys、end_signal_metadata_keys、carry_metadata_keys、
summary_mode 同上；其余配置为：

| 选项 | 默认 | 约束 |
|---|---|---|
| max_duration_seconds | 86400 | 正整数；累计包络超过才切 |
| similarity_threshold | 缺省关闭 | [0,1] 有限数值；显式 embedder；相等不切 |
| summary_max_children | 20 | 正整数；摘要输入最多片段数 |
| summary_max_chars_per_child | 80 | 正整数；每个输入片段最多字符数 |

配置装配把内层非 None 值转成字符串后校验；布尔值不是有效数值阈值。未知键、其他
stage、event、settle 等选项拒绝。启用相似度却缺 embedder，或 llm 模式缺 llm，装配即失败。
相似度只使用确定性父摘录、不含时间/数量表头；缺向量、零向量、维度不齐、非有限值或
调用异常均在写前失败，不静默改用另一种分组算法。

#### 父摘要与 L0/L1

先完成整个结构，再自下向上生成父正文：time_span 的 JSON 只取 `summary`（连续活动），
scene 只取 `goal/actions/outcome`（目标、行动、结果）；不采纳模型返回的 ID/边/区间/计数。
输入沿 child_ids 顺序按上述条数/字符上限截取；temperature=0，输出 max_tokens=1024。
JSON 非法、字段缺失/空值/类型错误或调用异常时保留确定性摘录，并记录 warning。
这是结构化解析与失败降级，不保证模型摘要质量或事实完全正确，质量仍需评测。

显式注入 LayerAnnotator 时，它只接收新父副本；成功后只采纳合法字符串 L0/L1，
不采纳正文和结构修改。阈值由已有 annotator 配置决定，短内容留空；整批抛错丢弃该批
标注结果，内部自行处理的逐条失败遵循原 annotator 行为。摘要/标注降级不加入 repair，
不让 Job 因非结构增强失败而标成 FAILED；日志可区分降级和持久化错误。

#### 保存顺序与失败结果

完整候选通过校验后，全部落盘只经同一 IndexBuilder：

1. 新父 `build(FORWARD_ONLY)`，按 scene → time_span 分层交付父本体；
2. 子叶 `update(FORWARD_ONLY)`，切换父引用；
3. 旧父改为 ARCHIVED，清空上下行结构边，再 `update(FORWARD_ONLY)`；
4. 旧父 `remove(SOFT)` 退出检索，保留归档本体；
5. 新父 `build(RETRIEVAL_ONLY)`、子叶 `update(RETRIEVAL_ONLY)` 刷新索引。

本体阶段按完整 Scope 分组，某组写入失败立即停止后续步骤；索引阶段分别尝试各操作，
失败累积 `repair_required` 并返回 `complete=false`。各 id 列表只记录已正常返回的本体
写入批次；底层批次抛错前可能已部分写入，因此修复项不是事务回滚证明。
`HierarchyRepair.unit_id` 需结合原请求 Scope 定位，不能跨 Scope 仅凭 id 修复。
Composer 不提供事务、自动重试、自动修复或并发闸门，不得把不完整结果当作成功。
阶段 3 的可选共享锁由 Control Job 持有，覆盖取数至本层调用；它不改变这里的部分
写入语义，也不提供数据库事务或普通 write/update/delete 的互斥。

阶段 3 已在 Control 实现显式任务和存储读取补齐，Composer 本身仍不查库。
event/其他 kind、ensure/auto derive 与结构维护器仍是后续目标，
不能由当前方法存在推断为已实现。

### Evolver（`evolver.py`）

记忆自演进，按 EvolveRequest 分派内容演进或树构建。EXTRACT/CONSOLIDATE/ASSOCIATE/
FORGET 保持原处理语义；HIERARCHY 单独委托 Composer，不调用抽取、去重或内容合并。
不同 EXTRACT 变体共享统一请求契约。

| 方法 | 签名 | 语义 |
|------|------|------|
| `evolve` | `(request: EvolveRequest) -> EvolveResult` | 对一批记忆单元执行指定阶段的演进，返回变更结果 |

**EvolveMode**：
- `EXTRACT` — 信息提取
- `ASSOCIATE` — 关联分析
- `CONSOLIDATE` — 冲突消解（近重复融合/矛盾标记失效）
- `FORGET` — 遗忘/降权（过期/低价值记忆归档）
- `HIERARCHY` — 显式创建或重建父节点及双向包含边；公开任务先由 Control 收齐本层输入

```python
@dataclass
class EvolveRequest:
    units: list[MemoryUnit]
    mode: EvolveMode
    metadata: dict[str, str] = field(default_factory=dict)
    hierarchy_options: HierarchyComposeOptions | None = None
```

`metadata` 承载 correlation id、触发来源等请求级透传信息，不写回 unit 核心字段。
仅 HIERARCHY 接受 hierarchy_options，且必须提供 kind、leaf_role、parent_roles、
tree_home_scope 与有界 span；其他 mode 提供 options 时拒绝。内部调用已统一到
`evolve(EvolveRequest(...))`，不保留旧的 `evolve(units, mode)` 兼容入口。

HIERARCHY 按请求 role 将 units 拆成叶与旧父，其他角色直接拒绝，然后固定委托
`replace_in_span`；无旧父时也可以完成有界首次构建。它不从存储补齐单位，不按 infer
值另做筛选，不执行整个 scope 的自动建树。未注入 Composer 或缺 options 时抛
ValidationError。请求级 metadata 不自动复制到父，需显式使用 hierarchy_options.metadata。

依赖注入以 `EvolverDependencies` 聚合 extractor/abstractor/associator/index_builder/
storage/message_store/dedup/llm，以及可选 layer_annotator/router/hierarchy_composer；
`EvolverOptions` 聚合 graph_name 和 medium/high 去重阈值。直接 Python 构造方需要使用
这两个对象；动态变体另接 prompt_registry。历史 YAML 参数名、依赖引用与默认值不变。

**EvolveResult**：

```python
@dataclass
class EvolveResult:
    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    forgotten_ids: list[str] = field(default_factory=list)
    created_units: list[MemoryUnit] = field(default_factory=list)
    hierarchy_result: HierarchyComposeResult | None = None
```

`hierarchy_result` 只在 HIERARCHY 模式返回结构结果与修复报告，其他 mode 为 `None`。
HIERARCHY 不复用外层内容演进 id 列表或 created_units，调用方应读取其专用结果。

各模式对 hierarchy 的行为：

| 路径/模式 | hierarchy 契约 |
|---|---|
| 普通 `write` | 默认空；调用方提供经校验的叶字段时可保留 kind/role/span，但不得写父或子边 |
| `EXTRACT` | 保留既有抽取实现行为，不调用 Composer；新建 unit 的实现默认空，深复制源 unit 的实现可能保留其结构，继承差异尚未统一；provenance 不自动成为父 |
| `ASSOCIATE` | hierarchy 不变；关系只写 GraphStore，不写 `parent_id` |
| `CONSOLIDATE` | 既有节点不变；新合成节点默认空，需单独建树 |
| `FORGET` | 当前保持原有生命周期与索引处理，不维护相邻结构边；完整断边属于后续 Maintainer/生命周期联动目标 |
| `HIERARCHY` | 委托 HierarchyComposer 创建/替换父节点并一致回写直接子边 |

- `created_units: list[MemoryUnit]` — 落盘产物本身。判定改写派生单元的 scope 后，调用方按原 scope 回读真源会落空，只有回传实际落盘的对象才取得到。新增与版本替换两条分支都回填；引擎的 `write` 优先取该字段，为空时才回落按 id 回读，以兼容不回填它的第三方 `Evolver` 实现。

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
| `system_metadata` / `user_metadata` | dict[str, MetadataValueType] | 系统/用户双命名空间元数据（保留 JSON 标量原生类型，见 F04-memory-metadata-separation） |
| `lifecycle` | LifecycleState | 生命周期状态 |
| `entities` | list[str] | L2 记忆里由大模型抽取得到的实体文本（明文）。entity linker 建反向索引时只消费本字段构造 `EntityMention`，为空时直接跳过该 unit（已砍 spaCy 兜底，无回退抽取，见 [F06](../features/retrieval/F06-entity-recall-channel.md)）。默认空，向后兼容 |
| `vectors` | list[ChunkVector] | content 的 chunk 级向量投影：构建期由 IndexBuilder 在 `vector_enabled` 时按 VectorIndexBuilder 同管线（Chunker 切片 → 共享 Embedder 逐 chunk embed）填充，`id`/`seq` 与 Chunk 对齐，随本体经 `DomainStore.add/update` 下传；一体化数据面实现消费它自建 chunk 级向量索引（record id 沿用 `{unit_id}-{chunk_id}`），CompositeDomainStore 仅随本体持久化。空列表表示未向量化；codec 加字段兼容演进，`_v` 不升（见 F06-unified-index-builder） |

构建层直接消费 S07 定义的 `HierarchyRef` 和层级枚举。父节点复用既有
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

各 Producer：`ExtractorProducer` / `AbstractorProducer` / `AssociatorProducer` / `ClassifierProducer` / `IndexBuilderProducer` / `DedupProducer` / `EvolverProducer` / `HierarchyComposerProducer`；`RouterProducer`（可选装配，首版只有模型实现）。
注册由 `construction.bootstrap.register_constructors` 统一触发。

Composer 位于 hierarchy_composer 命名空间；Evolver 只有显式声明
`params.hierarchy_composer` 依赖时才装配。Composer 的 index_builder 引用必须显式提供，
并与写入该批叶的 Evolver 使用同一具名 builder，缺少引用立即拒绝装配；不得为建树
另建一套默认真源。hierarchy_profiles 与 allow_cross_user 是 Composer 构造配置，
不是运行时 Policy 开关。`embedder` / `llm` / `layer_annotator` 均为可选显式依赖，
经各自 Producer 解析；Python 构造以 `HierarchyModelDependencies` 聚合为 models 参数。
不复用 IndexBuilder 的占位 embedder 来冒充主题切分模型。

以下仅示意依赖引用；`shared_memory_index` 必须指向部署已配置的、负责叶本体与索引的
同一实例，其余既有配置省略。仅装配 Composer 不等于开启公开入口：还需要 S03 的
JobFactory、Engine 同源依赖，以及 S02 的权限和 `hierarchy.enabled` 策略许可。

```yaml
evolver:
  default:
    target: orchestrating
    params:
      index_builder: shared_memory_index
      hierarchy_composer: tree_builder
hierarchy_composer:
  tree_builder:
    target: default
    params:
      index_builder: shared_memory_index
      hierarchy_profiles:
        time:
          parent_roles: [time_span, scene]
          stage_options:
            TimeSpanMerger:
              gap_seconds: 7200
            SceneSegmenter:
              max_duration_seconds: 86400
              summary_mode: structural
```

若开启 `summary_mode: llm`，还须在 Composer params 显式增加 `llm: <已配置的模型引用>`；
若设置 similarity_threshold，则增加 `embedder: <已配置的语义模型引用>`。
需要父 L0/L1 时增加 `layer_annotator: <已配置的标注器引用>`；三者互不隐式替代。

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
| F07-collective-memory | `Router` 是本层承担的归属判定算子；判定表配置、生效范围与失败方向由该规约定义 |
| architecture.md §4/§6/§8 | 分层记忆结构 / 多形式索引 / 记忆自演进 |

## 修订历史

| 日期 | 内容 |
|---|---|
| 2026-09-10 | 阶段 2：固化 EvolveRequest、依赖/选项聚合、最小 TIME Composer 的显式输入与受限替换、profile 装配、按序写入和不完整结果；区分内部能力与尚未开放的公开入口及后续维护能力。 |
| 2026-09-10 | 阶段 3：同步公开显式两层 TIME 任务接入，明确 Control 负责完整候选及可选锁，本层 EvolveRequest、snapshot→time_span 算法与部分写入契约不变。 |
| 2026-09-10 | 阶段 7：扩展可选 scene 层、完整旧子树替换、显式可选模型摘要和父 L0/L1，新增配置校验、确定性结构与降级边界；保留两层兼容及既有部分失败契约。 |
