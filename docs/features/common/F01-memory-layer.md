# F01 — MemoryUnit 内建 L0/L1/L2 内容层

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-07（初版）/ 2026-07-03（落地修订） |
| 影响范围 | jiuwen_memory/common/type_def/memory.py、jiuwen_memory/common/type_def/memory_codec.py、jiuwen_memory/construction/layer_annotator.py、jiuwen_memory/construction/layer_annotator_impl/、jiuwen_memory/construction/evolver_impl/orchestrating_evolver.py、jiuwen_memory/construction/index_builder_impl/、jiuwen_memory/construction/base.py、jiuwen_memory/construction/bootstrap.py、docs/specs/S05-construction.md、docs/specs/S07-common.md |
| 测试基线 | `tests/unit/construction/test_layer_annotator.py`（10 passed）、`tests/unit/construction/test_layers_index.py`（10 passed）、全量 `tests/` 426 passed / 54 skipped |
| Refs | — |

## 背景

architecture §4 定义了长时记忆的纵向抽象分层：低抽象事实/片段、中抽象事件/关系/主题、高抽象画像/偏好/技能。§8 定义了检索结果的横向渐进披露：L0 概要、L1 片段、L2 全文。§9.1 又把"分层披露标注"列为构建步骤。

这三者不是同一件事，但本特性要求横向内容层进入构建链路：

- **纵向抽象分层**：跨 `MemoryUnit`，由 Extractor/Abstractor/Associator 产出不同抽象粒度的独立记忆。
- **横向内容层**：同一条 `MemoryUnit` 的不同压缩度，L0/L1 是构建阶段生成的预标注内容，L2 是全文。
- **分层索引/召回**：索引层是否把 L0/L1/L2 都作为可检索对象，以及检索策略是否使用这些层级。

落地前的状态（已克服）：`MemoryUnit` 只有 `segments`/`content` 合并视图，L0/L1 不在数据结构中；构建管线不产出 L0/L1；索引只基于 `unit.content`。

本特性已落地 **MemoryUnit 内建内容层 + 构建层标注 + 分层索引记录 + 分层召回与披露**。纵向抽象级别不在本特性中建模；跨 `MemoryUnit` 的树结构也不属于本特性。

## 决策

### 1. MemoryUnit 新增 `layers` 字段

新增 `ContentLayers`，承载同一条记忆的横向内容层：

```python
@dataclass
class ContentLayers:
    """architecture §9.1 分层披露标注的承载结构。"""

    l0: str = ""  # 概要，50-100 字，供紧预算注入/快速浏览
    l1: str = ""  # 片段/要点，200-500 字，供上下文增强
```

```python
@dataclass
class MemoryUnit:
    # ... 既有字段不变 ...
    layers: ContentLayers = field(default_factory=ContentLayers)
```

设计约束：

- **L2 不存**：`unit.content`（`segments` 合并视图）就是 L2，避免全文双写。
- **空值合法**：`layers.l0 == ""` 或 `layers.l1 == ""` 表示未标注，Discloser 回退现有逻辑。
- **不改变纵向抽象分层**：`layers` 不是低/中/高抽象级别字段。一条 EPISODIC 事件、一条 SEMANTIC 事实、一条 PROCEDURAL 技能都可以有自己的 L0/L1。

### 2. 构建层新增 `LayerAnnotator`

新增构建算子，负责给已有 `MemoryUnit` 标注 L0/L1：

```python
class LayerAnnotator(ConstructionOperator):
    def annotate(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """为一批记忆单元生成 L0/L1 内容标注，写入 unit.layers。"""
```

实现：

| 实现 | target | 产出方式 | 依赖 | 适用路径 |
|---|---|---|---|---|
| `KeywordLayerAnnotator` | `keyword` | 规则提取 L0/L1 | Tokenizer / FeatureExtractor 可选 | hot path |
| `LLMLayerAnnotator` | `llm` | LLM 生成 L0/L1 | LLM | background / infer=true |

规则版策略：

- L1：取 content 前 N 字（默认 200），或取关键词密度最高的句子段落。
- L0：`tags` + content 首句；content ≤ 100 字时 `L0 = content`。
- L0 不能只是更短截断，应尽量形成独立可读的一句话概要。规则实现能力有限时允许退化为空，由 Discloser fallback。

LLM 版策略：

- L0：一句话概要，约 50-100 字。
- L1：3-5 条关键信息，约 200-500 字。
- temperature=0，输出尽量幂等。

失败策略：

- `LayerAnnotator` 是 best effort。单条/单批标注失败不得阻断 write/update/evolve。
- 失败时保留或写入空 `ContentLayers()`，由检索端 fallback。
- LLM 标注失败记录日志/轨迹，不影响派生 unit 落盘。

阈值筛选（落地补充）：

- `LayerAnnotator` 按 `layer_annotator_threshold`（默认 512）筛选：仅对
  `len(content) > threshold` 的 unit 标注，短 content 留空（不调 LLM、不硬凑摘要）。
  避免海量短记忆都调 LLM 的成本。规则版与 LLM 版共用此筛选。

调用时机（落地补充）：

- 当前由 `OrchestratingEvolver` 在 EXTRACT/CONSOLIDATE 抽取（升华）后、去重落盘前
  调用 `LayerAnnotator.annotate`（`_annotate_layers`），保证落盘的派生 unit 带 layers。
- `layer_annotator` 经 `LayerAnnotatorProducer` 注入；未配置则 evolver 跳过标注（向后兼容）。
- Engine.write 默认路径（原始 unit）未接 LayerAnnotator——原始 unit 无 layers。后续如需
  原始 unit 也分层，再接 write 路径。

### 3. L0/L1 生成时机

#### Evolve EXTRACT / CONSOLIDATE 路径（已落地）

派生 unit 在持久化和建索引之前完成标注：

```text
Extractor.extract / Abstractor.abstract
→ LayerAnnotator.annotate(derived)        # _annotate_layers，best effort
→ Dedup / persist decision
→ KVStore.insert or update
→ IndexBuilder.build or update
```

`OrchestratingEvolver._persist` 是 `KVStore.insert` 后立即 `IndexBuilder.build`，故标注必须在
`_dedup_batch` 之前完成，否则索引 metadata 中的 `l0/l1` 会与真源不一致。当前实现：procedural
与非 procedural 的 EXTRACT 路径、CONSOLIDATE 路径均在抽取（升华）后立即调 `_annotate_layers`。

#### Engine.write 默认 / infer=true 路径（未落地）

蓝图规划原始 unit 在分类后、落盘前标注：

```text
Ingestor.ingest → Classifier.classify → LayerAnnotator.annotate → KVStore.insert → IndexBuilder.build
```

当前未接 Engine.write 路径——原始 unit 无 layers。infer=true 下原始 unit 落 `/messages/`
（不建索引），派生 unit 由 Evolver 产出并标注（已落地，见上）。后续如需原始 unit 也分层，再接 write 路径。

### 4. update/delete 对 layers 的影响（未落地）

> 以下为规划，当前 LayerAnnotator 只在 evolver 抽取后调用，未接 update 路径，
> 故 update 不会自动重标注（layers 随 SUPERSEDE/OVERWRITE 的版本语义自然继承/清空）。
> 后续接 write/update 路径时再实现。

#### update

`layers` 是否刷新由 patch 类型决定：

- `patch.content` 变化：必须清空旧 layers 并重新标注。
- `patch.tags` 变化：如果当前装配的 annotator 会用 tags 生成 L0，也应重新标注。
- 仅修改 `metadata` / `t_valid` / `t_invalid` / lifecycle 时，不自动重标注，除非 metadata 明确包含摘要相关字段。

摘要来源优先级统一为：

```text
unit.layers.l0 > unit.metadata["summary"] > fallback 首句/截断
```

SUPERSEDE 模式下，新版本重新标注，旧版本保留旧 layers。OVERWRITE 模式下，原 id 内容变化后重新标注并覆盖原 layers。

#### delete / lifecycle 转换

- FORGET / ARCHIVE / DOWNWEIGHT：不修改 layers。生命周期是状态变化，不是内容变化。
- PURGE：物理删除真源和索引，layers 随真源一起删除。

### 5. 索引构建：L0/L1/L2 都进入可重建索引

`IndexBuilder` 必须消费 `unit.layers`，把 L0/L1/L2 作为层级内容进入索引构建。索引是派生物，必须可从 KV 真源中的 `MemoryUnit` 重建。

#### 向量索引

向量索引为同一 unit 生成三类记录：

| 层级 | 文本来源 | 记录粒度 | 说明 |
|---|---|---|---|
| L0 | `unit.layers.l0` | 单条记录 | 空值则不建 |
| L1 | `unit.layers.l1` | 单条记录或短 chunk | 空值则不建 |
| L2 | `unit.content` | 现有 chunk 逻辑 | 保持当前全文 chunk 召回能力 |

每条 `VectorRecord.metadata` 必须包含：

```text
unit_id
content_layer = "l0" | "l1" | "l2"
tier
lifecycle
seq
```

L0/L1 记录 id 可使用稳定格式：

```text
{unit.id}-layer-l0
{unit.id}-layer-l1
```

L2 chunk 记录保留现有 `{unit.id}-{chunk.id}` 格式，并补充 `content_layer="l2"`。

**分表与 store 注入（落地补充）**：L0/L1 不与 content 混表——`VectorIndexBuilder`/
`FulltextIndexBuilder` 构造增 `vector_l0`/`vector_l1`/`fulltext_l0`/`fulltext_l1` 四个可选
store（默认 None），L0/L1 record 写独立 store 实例（不同 collection/index = 分表）。store 为
None 则该层跳过（不报错、不建空记录）。`layers_index_enabled`（globals）+ 具名实例
（`vector_store.layers_l0/l1`、`fulltext_store.layers_l0/l1`）控制。L0/L1 不切片（整段 embed），
一条 unit 在 L0/L1 表各最多一条。update 先删旧分层 record 再按新 layers 重建（SUPERSEDE 不残留），
remove 按 id 幂等删。

#### 全文索引

全文索引同样写入层级文档：

| 层级 | Document.id | Document.text |
|---|---|---|
| L0 | `{unit.id}:l0` | `unit.layers.l0` |
| L1 | `{unit.id}:l1` | `unit.layers.l1` |
| L2 | `{unit.id}:l2` 或兼容旧 `unit.id` | `unit.content` |

Document metadata 必须包含：

```text
unit_id
content_layer = "l0" | "l1" | "l2"
tier
lifecycle
tags
source
```

为了兼容现有全文索引删除/update 逻辑，实现可短期保留旧 `unit.id` 作为 L2 文档 id，但必须在 metadata 中写 `content_layer="l2"`。

#### 召回聚合（已实施）

Recaller 聚合到 `unit_id` 的行为保留，命中 L0/L1/L2 任一层后都折叠成同一条
`ScoredUnit(unit_id=...)`，多路多层级命中经 Fuser 聚合到 unit 粒度：**同通道多层命中一律
取 MaxP**（分层是同通道的多个索引入口，非独立信号源）；跨通道如何合并由所选 Fuser 决定。
详见 §6 检索层分层召回。

本特性要求 L0/L1 **参与构建和召回候选生成**，但最终返回仍以 `MemoryUnit` 为单位，由 UnitReader 点读真源后进入 recheck/rerank/disclose。

### 6. 检索层 L0/L1 分层召回 + 三层披露（已实施）

> 状态：已实施（2026-07）。复用 vector/keyword recaller 加 `layer` 参数查 L0/L1 分表，
> RetrievedItem 三层一次性填充（abstract/overview/content）。经 `extractor_demo.py` 验证：
> 6 路（content/L0/L1 × vector/keyword）并行召回 + 融合 + 三层披露全链路生效。

#### 6.1 召回侧：recaller 加 layer 参数 + 分表 store（已实施）

**不新建独立 recaller 类**——复用 `VectorRecaller`/`KeywordRecaller` 加 `layer` 参数
（默认 "l2"=content，可 "l0"/"l1"），注入对应分表 store（vector_store.layers_l0/l1、
fulltext_store.layers_l0/l1），**store 为 None 时 recall 返空**（该层未注入，向后兼容）。

- `VectorRecaller.__init__(vector_store, min_similarity, layer)`；`KeywordRecaller.__init__(fulltext, layer)`。
- 注册具名实例：`vector`/`vector_l0`/`vector_l1`、`keyword`/`keyword_l0`/`keyword_l1`。
- **不新增 RecallChannel**：L0/L1 复用 VECTOR/KEYWORD 通道——同通道不同层级，
  Fuser 按 unit_id 聚合（同 unit 被 content/L0/L1 多路命中**取 MaxP，不累加**；
  归并由 `fuser_impl/layered_merge.py` 前置，各 Fuser 实现共用）。
- 分层 MaxP 统一按「分越大越相关」处理；L0/L1/L2 必须使用同类后端和同一
  分词/度量配置，向量路不接受 L2 等 lower-is-better 度量。
- `PipelineRetriever._build` 按 `layers_index_enabled`（回退 globals）接入 L0/L1 recaller。
- `layers_index_enabled` 开时（出厂默认）→ 6 路并行召回；显式关闭时→ 3 路
  （content keyword/vector + graph，具体受通道开关控制）。

**store 为 None 降级**：recaller recall 返空，不报错、不影响其他层级/通道。未配 L0/L1
退化为现状（向后兼容）。

#### 6.2 披露侧：RetrievedItem 三层一次性填充（已实施）

`RetrievedItem` 加 `abstract`(L0)/`overview`(L1) 字段，`content` 对应 L2 全文——三层
一次性填充，调用方按需取用：

```python
@dataclass
class RetrievedItem:
    unit_id: str = ""
    score: float = 0.0
    abstract: str = ""   # L0 摘要（unit.layers.l0，50-100 字）
    overview: str = ""   # L1 片段（unit.layers.l1，200-500 字）
    content: str = ""    # L2 全文（unit.content）
    level: DisclosureLevel = DisclosureLevel.L0  # 本次披露主层级
```

`TruncatingDiscloser._l0`/`_l1` 优先用 `unit.layers.l0/l1`（空则截断/取窗兜底）；
`StructuredDiscloser._render` L0/L1 优先用 layers（空则卡片/证据片段兜底）。disclose 时
三层都填，`level` 标本次主层级（ADAPTIVE 按 max_tokens 选 L0/L1/L2）。

**降级兜底**：layers 为空（未跑 LayerAnnotator）回退原截断/卡片逻辑——向后兼容。

#### 6.3 多路融合

```
query → query_parser → ParsedQuery
  ↓
并行召回（每层级 store 非空才参与，layers_index_enabled 开时）：
  ├─ VectorRecaller vector（content L2 chunk）
  ├─ VectorRecaller vector_l0（L0 整段，store 非空时）
  ├─ VectorRecaller vector_l1（L1 整段，store 非空时）
  ├─ KeywordRecaller keyword（content L2）
  ├─ KeywordRecaller keyword_l0（L0，store 非空时）
  ├─ KeywordRecaller keyword_l1（L1，store 非空时）
  └─ GraphRecaller（GRAPH）
  ↓
Fuser 融合（同 unit_id 多路多层级命中聚合——同通道取 MaxP；跨通道由所选 Fuser 决定）
  ↓
UnitReader 点读 KV → Discloser 三层披露（用预生成 l0/l1 + content 全文）
  ↓
RetrievedItem（abstract/overview/content + level）
```

**去重**：同一 unit 被 content/L0/L1 多路命中，Fuser 聚合到 unit 粒度（融合后按 unit_id
全局唯一一条 ScoredUnit，取融合分 + 全部 evidence）。多路共识的 unit 排名靠前。

#### 6.4 装配配置

```python
# defaults.py
"vector_store": {_D: "memory", "layers_l0": "memory", "layers_l1": "memory"},  # L0/L1 分表
"fulltext_store": {_D: {...}, "layers_l0": {...}, "layers_l1": {...}},
"recaller": {
    "keyword": {...}, "keyword_l0": {"target": "keyword_l0"}, "keyword_l1": {"target": "keyword_l1"},
    "vector": {...}, "vector_l0": {"target": "vector_l0"}, "vector_l1": {"target": "vector_l1"},
    "graph": {...},
},
"retriever": {
    _D: {
        "params": {
            "keyword_recaller": "keyword", "vector_recaller": "vector", "graph_recaller": "graph",
            # L0/L1 分层召回开关：回退 globals.layers_index_enabled（demo config 设 true 启用）
            "keyword_l0_recaller": "keyword_l0", "keyword_l1_recaller": "keyword_l1",
            "vector_l0_recaller": "vector_l0", "vector_l1_recaller": "vector_l1",
            ...
        },
    },
},
```

构建侧 `constructor`（HybridIndexBuilder）经 `_opt_dep(VectorProducer, "layers_l0/l1")`
取具名实例注入——`layers_index_enabled`（默认 true）开且 layers 非空才建 L0/L1 分表。
在线配置中，全文 L0/L1/L2 分别落独立 ES index 且共用同一 analyzer；向量
L0/L1/L2 分别落独立 Milvus collection，共用同一维度与 COSINE 度量。

#### 6.5 接口变更

| 组件 | 变更 |
|---|---|
| `retrieval/types.py` | `RetrievedItem` 加 `abstract`/`overview` 字段（content 对应 L2） |
| `retrieval/recaller_impl/vector_recaller.py` | `VectorRecaller` 加 `layer` 参数；注册 `vector_l0`/`vector_l1` |
| `retrieval/recaller_impl/keyword_recaller.py` | `KeywordRecaller` 加 `layer` 参数；注册 `keyword_l0`/`keyword_l1` |
| `retrieval/retriever_impl/pipeline_retriever.py` | `_build` 按 `layers_index_enabled` 接入 L0/L1 recaller |
| `retrieval/discloser_impl/*.py` | 优先用 `unit.layers.l0/l1`，空则兜底；RetrievedItem 三层填充 |
| `config/defaults.py` | 加 vector_store/fulltext_store.layers_l0/l1、recaller 具名实例、retriever 接入 |
| `construction/index_builder_impl/*` | 无改动（已支持分表） |

**核心**：不新建 recaller 类，复用加 layer 参数；RecallChannel 不扩；pipeline_retriever 只加
接入逻辑。改动面最小。`RecallChannel` 复用 VECTOR/KEYWORD——同通道不同层级，Fuser 按 unit_id 聚合。

### 7. 序列化兼容（已落地）

`memory_codec` 沿用 `_v=2` + 新增 `layers` 字段（加字段是兼容演进，老数据缺省读出，不升版本，见 S07）：

- `dumps` 写入 `"layers": {"l0": str, "l1": str}`。
- `loads` 读回构造 `ContentLayers`，缺失取空串（老数据无迁移读出）。
- 未知字段继续忽略，保持向前兼容。

## 后续扩展：树结构层级展开（交由 F08）

本特性只处理同一 unit 内由 `DisclosureLevel` 表达的 L0/L1/L2 压缩度；它不表示跨 unit 的父子关系。若后续要支持目录、主题、聚类等父子结构，需要另立 feature：

1. 引入 `parent_id` / `parent_uri` 或主题节点。
2. 支持父节点摘要命中后展开子节点。
3. 支持父子分数传播和收敛策略。
4. 支持按预算从父级概要升级到子级全文。

## 拒绝的方案

### 方案 A：继续纯检索端截断

拒绝。它与 architecture §9.1 的"构建阶段分层披露标注"不一致，且 L0/L1 每次检索临时计算，质量不稳定。

### 方案 B：用独立摘要 MemoryUnit 承载 L0/L1

拒绝。L0/L1 是同一条记忆的压缩表示，不是独立记忆。独立 unit 会带来额外去重、生命周期、版本链和检索关联成本。

### 方案 C：L2 也存进 ContentLayers

拒绝。L2 等于 `unit.content`，重复存储会造成 segments 与 layers.l2 的一致性问题。

### 方案 D：本次引入树结构展开

拒绝作为本次范围。该决定保留为 F01 的历史范围取舍：树结构展开超出同一 `MemoryUnit` 内容层的边界。树结构现已由 [F08-memory-tree.md](F08-memory-tree.md) 独立完成设计，但仍未实现。

## 验证

- [x] `ContentLayers` 定义在 `common/type_def/memory.py`
- [x] `MemoryUnit` 新增 `layers: ContentLayers`
- [x] `memory_codec` 序列化/反序列化包含 layers，老数据默认空 layers（沿用 `_v=2`）
- [x] `LayerAnnotator` 接口和 Producer 注册（`construction/layer_annotator.py`）
- [x] `KeywordLayerAnnotator` 标注 L0/L1，失败不阻断（按阈值筛选）
- [x] `LLMLayerAnnotator` 标注 L0/L1，LLM 失败回退空 layers（按阈值筛选）
- [ ] Engine.write 默认路径在 KVStore.insert / IndexBuilder.build 前完成标注（未落地）
- [ ] infer=true 原始 unit 标注后落盘（未落地；派生 unit 标注已落地）
- [x] Evolver EXTRACT / CONSOLIDATE 派生 unit 在去重落盘前完成标注（`_annotate_layers`）
- [ ] Engine.update 在 content/tags 变化时重新标注（未落地）
- [ ] delete/lifecycle 转换不修改 layers，PURGE 删除真源与索引（未落地）
- [x] VectorIndexBuilder 为 L0/L1/L2 建层级记录，metadata 含 `content_layer`（分表、store None 跳过）
- [x] FulltextIndexBuilder 为 L0/L1/L2 建层级文档，metadata 含 `content_layer`（分表、store None 跳过）
- [x] Recaller 聚合层级命中到 `unit_id`：同通道多层命中取 MaxP，跨通道由所选 Fuser 聚合（已实施，§6）
- [x] TruncatingDiscloser 优先读 layers，空值回退原截断逻辑（已实施，§6）
- [x] StructuredDiscloser 优先读 layers，空值回退原结构化逻辑（已实施，§6）
- [x] RetrievedItem 三层一次性填充（abstract/overview/content，已实施，§6）
- [x] specs 同步更新 S05/S07

## 已知遗留

1. **Engine.write 默认/infer 路径未接 LayerAnnotator**：当前只在 evolver 抽取后标注派生 unit；
   原始 unit 无 layers。后续接 write 路径时再实现。
2. **update/delete 路径未接**：update 不会自动重标注（layers 随版本语义继承/清空）；delete 不
   修改 layers。后续接 update 路径时实现 patch 触发重标注。
3. **树结构展开未实现**：本次只做同一 unit 的 L0/L1/L2 内容层；跨 unit 的结构设计见 [F08-memory-tree.md](F08-memory-tree.md)。
4. **KeywordLayerAnnotator 质量有限**：规则标注无法保证 LLM 级语义浓缩，hot path 接受该折衷。
5. **抽象粒度字段未建模**：低/中/高抽象分层仍由 `tier/tags/metadata/provenance` 间接表达；如需
   一等字段，应另立纵向抽象分层 feature。

## 配置

- `layer_annotator` 命名空间配具名实例（target=keyword/llm），evolver 经
  `LayerAnnotatorProducer` 取；未配则 evolver 跳过标注（向后兼容）。
- `layer_annotator_threshold`（globals）控制阈值筛选，默认 512。
- `layers_index_enabled`（globals）+ `vector_store.layers_l0/l1`、`fulltext_store.layers_l0/l1`
  具名实例控制分层索引（见 S05 IndexBuilder build 路径）。
