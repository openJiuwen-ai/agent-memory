# F01 — MemoryUnit 内建 L0/L1/L2 内容层

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-07 |
| 影响范围 | src/common/type_def/, src/construction/, src/control/, src/retrieval/, docs/specs/S02-memory-api.md, docs/specs/S03-memory-manage.md, docs/specs/S04-retrieval.md, docs/specs/S05-construction.md, docs/specs/S07-common.md |
| 测试基线 | 待实现后运行 pytest |
| Refs | — |

## 背景

architecture §4 定义了长时记忆的纵向抽象分层：低抽象事实/片段、中抽象事件/关系/主题、高抽象画像/偏好/技能。§8 定义了检索结果的横向渐进披露：L0 概要、L1 片段、L2 全文。§9.1 又把"分层披露标注"列为构建步骤。

这三者不是同一件事，但本特性要求横向内容层进入构建链路：

- **纵向抽象分层**：跨 `MemoryUnit`，由 Extractor/Abstractor/Associator 产出不同抽象粒度的独立记忆。
- **横向内容层**：同一条 `MemoryUnit` 的不同压缩度，L0/L1 是构建阶段生成的预标注内容，L2 是全文。
- **分层索引/召回**：索引层是否把 L0/L1/L2 都作为可检索对象，以及检索策略是否使用这些层级。

当前实现只具备输出概念，不具备持久内容层，也没有让 L0/L1 进入索引构建：

1. `MemoryUnit` 只有 `segments` 和 `content` 合并视图，L0/L1 不存在于数据结构中。
2. `DisclosureLevel` 只在检索输出端生效，`TruncatingDiscloser` / `StructuredDiscloser` 临时截断或渲染 L0/L1。
3. 构建管线不产出 L0/L1。`Classifier` 只分类，`Extractor` / `Abstractor` 产出新的 `MemoryUnit`，不是给已有 unit 做摘要/片段标注。
4. 当前索引构建只基于 `unit.content`：向量索引切 `unit.content` chunk，全文索引写 `unit.content`。L0/L1 没有索引记录，也不会影响召回候选。

本特性落地 **MemoryUnit 内建内容层 + 构建层标注 + 层级索引记录 + 披露端消费**。纵向抽象级别仍不在本特性中建模。

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

- `LayerAnnotator` 是 best effort。单条标注失败不得阻断 write/update/evolve。
- 失败时保留或写入空 `ContentLayers()`，由检索端 fallback。
- LLM 标注失败记录日志/轨迹，不影响派生 unit 落盘。

### 3. L0/L1 生成时机

#### Engine.write 默认路径

标注在分类之后、落盘和索引之前：

```text
Ingestor.ingest
→ Classifier.classify
→ LayerAnnotator.annotate
→ KVStore.insert
→ IndexBuilder.build
```

这样真源中的 `MemoryUnit` 已经带 L0/L1，后续索引构建可从同一对象读取层级内容。

#### Engine.write infer=true 路径

原始 unit 仍先标注再落盘。infer=true 下原始 unit 不建索引，派生 unit 由 Evolver 产出并负责标注：

```text
Ingestor.ingest
→ Classifier.classify
→ LayerAnnotator.annotate(originals)
→ KVStore.insert(originals, unindexed)
→ Evolver.evolve(EXTRACT)
```

#### Evolve EXTRACT / CONSOLIDATE 路径

派生 unit 必须在持久化和建索引之前完成标注：

```text
Extractor.extract / Abstractor.abstract
→ LayerAnnotator.annotate(derived)
→ Dedup / persist decision
→ KVStore.insert or update
→ IndexBuilder.build or update
```

现有 `OrchestratingEvolver._persist` 是 `KVStore.insert` 后立即 `IndexBuilder.build`。因此实现时不能采用"先落盘建索引，再标注写回"的顺序，否则索引 metadata 中的 `l0/l1` 会与真源不一致。

### 4. update/delete 对 layers 的影响

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

#### 召回聚合

当前 Recaller 聚合到 `unit_id` 的行为保留：命中 L0/L1/L2 任一层后，都折叠成同一条 `ScoredUnit(unit_id=...)`。`ChannelEvidence` 应携带命中层级，至少在 metadata 中保留 `content_layer`，便于后续调试和披露策略使用。

本特性要求 L0/L1 **参与构建和召回候选生成**，但最终返回仍以 `MemoryUnit` 为单位，由 UnitReader 点读真源后进入 recheck/rerank/disclose。

### 6. 检索层 Discloser 读层

召回候选聚合到 unit 后，披露阶段按请求层级读取 `unit.layers` / `unit.content`。

`TruncatingDiscloser`：

```python
def _content(self, unit: MemoryUnit, level: DisclosureLevel, keywords: list[str]) -> str:
    if level == DisclosureLevel.L2:
        return unit.content
    if level == DisclosureLevel.L0:
        return unit.layers.l0 or self._fallback_l0(unit, keywords)
    if level == DisclosureLevel.L1:
        return unit.layers.l1 or self._fallback_l1(unit, keywords)
```

`StructuredDiscloser`：

- `_summary()` 优先读 `unit.layers.l0`，再读 `metadata["summary"]`，最后回退首句/截断。
- `_best_snippet()` 优先读 `unit.layers.l1`；使用 layers 时 `matched=[]`，`[matched]` 可显示 `-`。
- ADAPTIVE 预算选择逻辑不变，只是选中层级后优先读 layers。

`FilterClause` 不扩展 layers 过滤。L0/L1 是内容披露字段，不是结构化筛选维度。

### 7. 序列化兼容

`memory_codec` 升级到 V3：

- `_v = 3`
- 新增字段：`layers: {"l0": str, "l1": str}`
- 读取 V2 或更老数据时，缺 `layers` 默认 `ContentLayers(l0="", l1="")`
- 未知字段继续忽略，保持向前兼容

## 后续扩展：层级展开

本特性只处理同一 unit 内的 L0/L1/L2 内容层。若后续要支持目录、主题、聚类等父子结构，需要另立 feature：

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

### 方案 D：本次引入父子层级展开

拒绝作为本次范围。父子层级展开需要新增主题/目录节点、父子关系、分数传播和展开策略，超出同一 `MemoryUnit` 内容层的边界。

## 验证

- [ ] `ContentLayers` 定义在 `common/type_def/memory.py`
- [ ] `MemoryUnit` 新增 `layers: ContentLayers`
- [ ] `memory_codec` V3 序列化/反序列化包含 layers，V2 数据默认空 layers
- [ ] `LayerAnnotator` 接口和 Producer 注册
- [ ] `KeywordLayerAnnotator` 标注 L0/L1，失败不阻断
- [ ] `LLMLayerAnnotator` 标注 L0/L1，LLM 失败回退空 layers
- [ ] Engine.write 默认路径在 KVStore.insert / IndexBuilder.build 前完成标注
- [ ] infer=true 原始 unit 标注后落盘，派生 unit 标注后再持久化/建索引
- [ ] Evolver EXTRACT / CONSOLIDATE 派生 unit 在 `_persist` 前完成标注
- [ ] Engine.update 在 content/tags 变化时重新标注
- [ ] delete/lifecycle 转换不修改 layers，PURGE 删除真源与索引
- [ ] VectorIndexBuilder 为 L0/L1/L2 建层级记录，metadata 含 `content_layer`
- [ ] FulltextIndexBuilder 为 L0/L1/L2 建层级文档，metadata 含 `content_layer`
- [ ] Recaller 聚合层级命中到 `unit_id`，并在 evidence/metadata 中保留命中层级
- [ ] TruncatingDiscloser 优先读 layers，空值回退原截断逻辑
- [ ] StructuredDiscloser 优先读 layers，空值回退原结构化逻辑
- [ ] specs 同步更新 S02/S03/S04/S05/S07

## 已知遗留

1. **父子层级展开未实现**：本次只做同一 unit 的 L0/L1/L2 内容层，不做目录/主题节点展开。
2. **从索引命中直接返回 L0/L1 未实现**：当前 Discloser 仍依赖 UnitReader 点读真源后读取 layers。
3. **KeywordLayerAnnotator 质量有限**：规则标注无法保证 LLM 级语义浓缩，hot path 接受该折衷。
4. **抽象粒度字段未建模**：低/中/高抽象分层仍由 `tier/tags/metadata/provenance` 间接表达；如需一等字段，应另立纵向抽象分层 feature。
