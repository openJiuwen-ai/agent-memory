# S05 — 构建层（Construction Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/construction/ |
| 最近一次修订日期 | 2026-07-03 |
| 关联特性文档 | docs/features/F01-system-spec-design.md, docs/features/construction/F01-construction-spec-design.md, docs/features/common/F01-memory-layer.md |

## 范围 / 边界

**管什么**：
- 真源落盘（调用 KVStore 写入记忆单元序列化）
- 信息提取（低抽象粒度：事实/事件/偏好）
- 抽象与精炼/升华（高抽象粒度：画像/长期偏好/可复用技能）
- 关联分析（实体共指/因果链/引用关系）
- 多维分类（认知角色/主题/重要度）
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
7. **scope 原生隔离**：构建索引记录时把来源 `MemoryUnit.scope` 落到记录的专用 `scope` 字段（`VectorRecord`/`Document`/`Node` 等），使检索得以按 scope 原生隔离。
8. **去重召回与判定分离**：去重召回（用哪个索引）由 `Dedup` 接口承担，Evolver 只做阈值 + LLM 判定。装配按 `vector_enabled` 选 `VectorDedup`/`KeywordDedup`，保证只配倒排索引时去重仍可用（向量路在 fulltext-only 下 VectorStore 恒空会失效）。
9. **Evolver 不依赖 control**：SUPERSEDE/FORGET 标记由 Evolver 直接通过 `KVStore.update` 完成，不经 `LifecycleManager`（construction → control 严禁）。
10. **Dedup 与 IndexBuilder 共享底层 Store**：去重召回检索的是已索引内容，`Dedup` 实现取的 `VectorStore`/`FulltextStore` 必须与 IndexBuilder 写入的是同一实例（按字段名缓存命中）。
11. **分类 metadata 键约定**：Classifier 写入 `unit.metadata` 的 `importance`/`confidence`/`freshness`/`classify_source` 是跨模块契约——Evolver 遗忘策略读 `importance`/`freshness`，检索层前置过滤读 `tier`/`tags`。键名稳定，值的类型固定（importance/confidence 为浮点字符串，freshness 为 hot/warm/cold，classify_source 为 JSON 字符串）。

## 接口契约

### ConstructionOperator（基类，`base.py`）

```python
class OperatorType(str, Enum):
    EXTRACTOR / ABSTRACTOR / ASSOCIATOR / CLASSIFIER / INDEX_BUILDER / EVOLVER

class ConstructionOperator(ABC):
    def operator_type(self) -> OperatorType  # 自描述
    def health(self) -> None                 # 存活探测
```

> `OperatorType` 枚举无独立 DEDUP 值——`Dedup` 实现复用 `OperatorType.EVOLVER`（去重召回服务于 evolver）。

### Extractor（`extractor.py`）

信息提取，产出低抽象粒度的派生记忆单元。

| 方法 | 签名 | 语义 |
|------|------|------|
| `extract` | `(units: list[MemoryUnit]) -> list[MemoryUnit]` | 从一批原始记忆单元中提取零或多条低抽象粒度的派生单元 |

派生单元的 `tier`/`tags` 由 LLM 在抽取时产出。`layers`（L0/L1 分层标注）不由 Extractor
产出——由 Evolver 抽取后委托 `LayerAnnotator` 生成（见下文 LayerAnnotator 节 + F01-memory-layer）。

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
标注，短 content 留空。best effort——失败降级为空 layers，不阻断演进。Evolver 在
EXTRACT/CONSOLIDATE 抽取（升华）后、去重落盘前调用，保证落盘 unit 带 layers。详见 F01。

### IndexBuilder（`index_builder.py`）

多形式索引构建与维护。

| 方法 | 签名 | 语义 |
|------|------|------|
| `build` | `(units: list[MemoryUnit]) -> None` | 为一批记忆单元构建已启用的各形式索引 |
| `update` | `(units: list[MemoryUnit]) -> None` | 记忆变更后增量更新对应索引条目 |
| `remove` | `(unit_ids: list[str]) -> None` | 删除一批记忆单元对应的索引条目（幂等） |
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

> 注：文档索引（path → unit_id 映射）与 FusionStore 融合索引在当前实现中**未落地**，属设计预留。
> L0/L1 分层索引的召回接入未落地（为披露层预留），详见 F01。

### Evolver（`evolver.py`）

记忆自演进，持续驱动演进闭环。

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

去重召回，Evolver 的 EXTRACT/CONSOLIDATE 模式调用。召回 + 阈值过滤 + 加载 + 聚合取 max 全在实现内完成；判定（中/高阈值 + LLM）留 Evolver。

| 方法 | 签名 | 语义 |
|------|------|------|
| `recall` | `(candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]` | 对候选召回已有相似记忆，返回 (unit, score) 列表（按 score 降序）；已完成过滤自身、过滤非 ACTIVE、按 unit 聚合取 max、按 min_similarity 过滤低分。空列表 → Evolver 判 ADD |

**score 量纲 0~1**：向量路=cosine，倒排路=词重叠率，阈值统一复用。

**两个实现**（装配按 `vector_enabled` 选）：
- `VectorDedup`（`vector`）— Embedder → VectorStore.search，cosine；record_id 为 `{unit_id}-{chunk_id}` 需解析
- `KeywordDedup`（`keyword`）— FulltextStore.search，词重叠率；Document.id = unit.id 恒等无需解析

**降级契约**：实现内部任何异常（Embedder/Store 失败）都吞掉并返回空列表——去重是尽力而为，不可阻断演进。

## 数据结构

### MemoryUnit（`common/type_def/memory.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | 全局唯一 id |
| `scope` | Scope | 归属 scope |
| `tier` | MemoryTier | 认知角色（working/core/episodic/semantic/procedural/archival） |
| `segments` | list[Segment] | 内容段列表（多段内容投影，每段含 content+assets+source） |
| `source_ref` | str | 来源引用（RawPayload id / 会话 id 等，可溯源） |
| `temporal` | Temporal | 时间：t_event / t_ingest / t_valid / t_invalid |
| `provenance` | list[str] | 演进血缘（多→一）：由哪些 unit 抽取/升华/合并而来 |
| `supersedes` | str | 版本链（一→一）：本版取代的上一版 id（空=首版） |
| `tags` | list[str] | 标签（检索前置过滤用） |
| `metadata` | dict[str, str] | 元数据（importance/confidence/freshness/classify_source 等） |
| `lifecycle` | LifecycleState | 生命周期状态 |

**注**：`MemoryUnit.content` / `assets` / `source` 是基于 segments 的只读合并视图，非独立字段。

### Relation（`common/type_def/feature.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `source_id` | str | 关联起点（记忆单元/实体 id） |
| `target_id` | str | 关联终点（记忆单元/实体 id） |
| `relation` | str | 关系类型（caused_by / refers_to / corefers ...） |
| `score` | float | 关联置信度 |
| `metadata` | dict[str, str] | 附加信息 |

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
| S03-memory_manage | Engine.write 路径调用本层 IndexBuilder.build，Engine.evolve 路径调用本层 Evolver |
| S04-retrieval | 检索层消费本层构建的索引 |
| S06-storage | 本层通过注入的 Store 抽象做真源与索引持久化 |
| S07-common | 本层消费 Chunker/Tokenizer/Embedder/FeatureExtractor/LLM/Reranker 共享插件 |
| architecture.md §4/§6/§8 | 分层记忆结构 / 多形式索引 / 记忆自演进 |
