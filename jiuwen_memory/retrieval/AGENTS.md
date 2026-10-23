# Agent Memory Retrieval（检索层）

**规约文档**：[S04-retrieval.md](../../docs/specs/S04-retrieval.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

混合检索的完整链路编排：查询理解 → 按 Storage 首选路径执行 recall/get/rank → 精排 →
相关性阈值 → 渐进式披露 → 返回 + 检索轨迹。rank 只指 Fuser，Reranker 保持独立阶段。

**单路召回不在本层**：`Recaller` 契约与实现在 `storage/domain_store_impl/`——它是
`CompositeDomainStore` 的内部件，本层只经数据面的 `recall`/`recall_and_get`/`retrieve`
拿结果，不持有也不装配召回路（F07）。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | RetrievalOperator 基类：所有检索层算子的自描述契约 |
| `types.py` | 检索对外类型；Storage 共用的 ParsedQuery/候选/错误类型从 common 重导出 |
| `cross_space.py` | 跨空间检索的取数上界（`allocate_quota`）与结果合并（`merge`）；不访问存储、不调模型的纯函数，与算子并列而非算子，不进工厂 |
| `query_parser.py` | QueryParser 接口：查询理解（去噪/改写/分词/实体/向量化/时间解析） |
| `fuser.py` | Fuser 接口：多路融合排序（重排由 common `Reranker` 独立阶段承担） |
| `discloser.py` | Discloser 接口：渐进式披露（L0 摘要/L1 片段/L2 全文） |
| `retriever.py` | Retriever 接口：检索层入口，编排完整链路 |
| `query_parser_impl/` | QueryParser 实现目录（simple_query_parser / sanitize / time_parse） |
| `fuser_impl/` | Fuser 实现目录（rrf【默认】/ weighted_rrf / score_max）+ `layered_merge` 分层归并前处理 |
| `discloser_impl/` | Discloser 实现目录（structured / truncating） |
| `retriever_impl/` | Retriever 实现目录；pipeline 经 `StoreManagerProducer.resolve` 取全局 manager 并持其 `domain_store()`；multimodal 组合原生、CLM、ELM 三个过滤分支并执行 RRF 融合 |
| `schema_temporal/` | 只读 Schema TemporalEntity 视图：双轴查询、实体/Property 双路、实体内裁剪、邻居扩展与最终 Source fallback；正式输出为每 Entity 一个结果 |
| `bootstrap.py` | 统一触发所有检索算子注册 |

## 检索链路

```
1. QueryParser.parse(query) → ParsedQuery
     ↓ 结构化查询（tokens/keywords/entities/vector/scalar_filters/channels）
2. ParsedQuery.raw 为空时短路返回空结果
     ↓
3. 根据 Storage.preferred_retrieval_pipeline 选择：
     ↓ recall → 去重 id 点读 → 恢复分入口候选
     ↓ 或 recall_and_get → 物化候选
     ↓ 或 Storage.retrieve(parsed, fuser) → 已融合候选
4. Fuser 前完成真源复核，再对 ScoredMemoryUnit 做分层归并和跨通道融合
5. 截断精排预算 budget = fused[:max(rerank_max, top_k)]
     ↓
6. 可选 Reranker 用 `ParsedQuery.rewritten` 精排（记 calibrated 标志；fallback/off 时该值即 sanitized raw）
     ↓
7. 相关性阈值：绝对 min_score（仅校准路径）+ 相对 ratio（校准/未校准两路，默认关闭）
     ↓ + min_results 从正分候选兜底回填（结果数可 < top_k）
8. 截断 top_k
     ↓
9. Discloser.disclose(...) → list[RetrievedItem]
     ↓ 按层级加载内容（L0/L1/L2）
10. 可选 Schema TemporalEntity（最终普通结果作 Entity seed，Property 独立召回）
     ↓ 组装、实体内裁剪、直达 Property 回融、时间线邻居和 Source fallback
→ RetrievalResult（items + trajectory + errors）
```

## L0/L1 分层召回 + 三层披露

L0/L1 分层检索在 content（L2）之外，额外召回预生成的概要（L0）/片段（L1），三层并行召回 + 融合 + 三层一次性披露。详见 `docs/features/common/F01-memory-layer.md` §6；融合策略与分层归并见 `docs/features/retrieval/F04-score-max-fusion.md`。

**构建侧**（已就绪）：`LayerAnnotator` 对长 content 产出 `unit.layers.l0`/`l1`，`VectorIndexBuilder`/`FulltextIndexBuilder` 对非空 layers 整段 embed 建独立分表（`vector_store.layers_l0/l1`、`fulltext_store.layers_l0/l1`）。向量 record id 为 `{unit_id}-layer-l0/l1`，全文 document id 为 `{unit_id}:l0/l1`。

**召回侧**：复用 `VectorRecaller`/`KeywordRecaller` 加 `layer` 参数（l2/l0/l1），注册 `vector_l0/l1`/`keyword_l0/l1` 具名实例，查对应分表 store。`layers_index_enabled` 开时（回退 globals）接入——store 为 None 时 recall 返空（该层未配，向后兼容）。同通道不同层级，Fuser 按 unit_id 聚合：**同通道多层命中一律取 MaxP（最高分）**（F01-memory-layer §6.3）。分层是同通道的多个索引入口，不是独立信号源——按多路处理会让分数偏向"有 layers 的 unit"，那是索引覆盖差异而非相关性差异。三层 store 必须使用同类后端、同一分词/度量配置，并统一满足「分越大越相关」。归并由 `fuser_impl/layered_merge.py` 统一前置，三个 Fuser 实现共用；未启用分层时为恒等变换。跨通道如何合并则取决于所选 Fuser：默认 `rrf` 按名次倒数累加，`weighted_rrf` 加通道权重，可选 `score_max` 取通道归一化分的最大值。

**披露侧**：`RetrievedItem` 三层一次性填充——`abstract`(L0) / `overview`(L1) / `content`(L2)，优先用 `unit.layers.l0/l1`（空则回退截断/取窗兜底）。调用方按需取用：紧预算用 abstract，中等用 overview，全文用 content。`level` 标本次披露主层级（ADAPTIVE 按 max_tokens 选）。

## 行为铁律

0. **metadata 过滤不跨命名空间**：`user_metadata.<key>` 和
   `system_metadata.<key>` 从对应真源字段复核，不 fallback。`RetrievedItem` 同时附带
   两个命名空间，调用方按接入形态决定是否暴露系统字段。

1. **scope 是独立轴**  
   `scope: Scope` 作为 `Retriever.retrieve` / `Recaller.recall` 的显式第一入参贯穿全链路，不随 `RetrievalQuery` 携带、也不混进 `filters`。query 是"找什么"，scope 是"在谁的范围内找"。

2. **读写同一套共享插件**  
   `QueryParser` 必须与构建侧使用同一套 Tokenizer/Embedder/FeatureExtractor，保证同词表/同向量空间。由装配层（build_kernel）保证同一 Producer 的同一 name 返回同一单例。

3. **query 原文先规范化再召回**
   默认 `SimpleQueryParser` 在分词、向量化和时间解析前做保守去噪，剥除 UTC 时间戳与 `Sender (untrusted metadata)` 等上游包装噪声；`ParsedQuery.raw` 是进入检索链路的规范化文本。清洗后为空时，`PipelineRetriever` 必须在召回前短路返回空结果。
   LLM 改写默认关闭；显式开启后把 sanitized raw 与 API 边界传入的 typed `query_context` 交给 strict JSON 改写任务。非空 `rewritten` 驱动软召回与 Reranker 精排；显式 null 表示 no-intent，产出空软信号并在 DomainStore 前短路；解析失败或模型异常回退 sanitized raw。硬过滤、`as_of` 与 event-time 窗不被 LLM 输出或上下文改写。parse/rerank 轨迹记录 `off/used/fallback/no_intent`、fallback 原因和 rerank query source，不记录 query、上下文或模型输出。

4. **通道到物理 Store 非 1:1**
   一路可对应一个 Store（如 VectorStore），多路也可合到一个 Store（如 FusionStore 同时支持向量+关键词），TEMPORAL 通常是叠加在其他通道上的时间过滤。

5. **scalar_filters 与软召回信号分离**
   `ParsedQuery` 中 `scalar_filters`（硬前置过滤）与 `tokens/keywords/entities/vector`（软召回信号）不能互相折叠。前者决定"先排除什么"，后者决定"召回什么"。

6. **双时间轴独立**
   `as_of`（valid-time 回溯点，问"T 时刻哪个版本有效"）与 `time_from/time_to`（event-time 范围，问"事件发生在何时"）是两条独立时间轴，不可混用。

7. **生产过滤必须先于 top-k**
   Milvus / Elasticsearch / pgvector 必须在 limit 前完整下推系统谓词与用户 FilterExpr。
   UnitReader 复核只能防错召，不能找回已经被 top-k 截断的真实命中。

8. **UnitReader 复核保持字段形态**
   标量上的 `EQ` / `IN` 与数组上的 `CONTAINS` 不得互相退化；标量
   `CONTAINS`、数组 `EQ` / `IN` 均判否，否定算子按对应正向谓词取反。

9. **系统谓词以外层 AND 合并**
   lifecycle / valid-time / event-time 谓词不得摊平进用户表达式；用户 OR/NOT 不能稀释
   系统边界。历史 as_of 使用 `[t_valid, t_invalid)`，开放 t_invalid 依赖索引哨兵。

10. **Discloser 只做内容塑形**
   候选记忆单元已由 Retriever 经 UnitReader 点读、有效性过滤、（可选）重排后给定。Discloser 不再做点读/过滤/重排，只按 level 截/取内容产出结果。

11. **Schema TemporalEntity 只读但正式返回**
   临时视图不持久化，所有 Property/Source 必须先经过点读和生命周期复核。
   每个 Entity 可投影为一个 `RetrievedItem`，结构化视图仅写入
   `SchemaTemporalRetrievalResult.schema_temporal`；普通结果无此字段。其 id 是 Entity id，不得伪装成可点读的
   MemoryUnit。必要 Source fallback 仍保留真实 MemoryUnit id。

12. **Schema TemporalEntity 保持两条时间轴**
   `knowledge_as_of` 只选择 `[t_valid, t_invalid)` 内可知的版本；
   `event_at/event_from/event_to` 只选择事件时间。年/月精度使用区间，
   不将 Source message time 伪造为 `t_event`。

## 与其他子目录的边界

**本模块管**：
- 查询理解（QueryParser）
- 多路召回结果的编排消费（Retriever；单路 Recaller 归 `storage` 数据面）
- 融合与重排（Fuser）
- 渐进式披露（Discloser）
- 检索轨迹记录（TrajectoryStep）
- 只读 Schema TemporalEntity 候选选择（不创建或持久化新记忆）

**不管**：
- 鉴权（归 `api`）
- 记忆写入/演进（归 `construction`）
- 单路召回与其装配（`Recaller` 归 `storage/domain_store_impl/`）
- 存储实现（通过注入的 Store 抽象间接调用）
- Tokenizer/Embedder/Reranker 实现（消费 `common` 注入的实例）

## 本地约束

1. 所有 Operator 必须实现 `operator_type()` 和 `health()`（继承自 `RetrievalOperator`）。
   `RetrievalOperatorType` 不含 `RECALLER`——单路召回不是本层算子。
2. 算子实现通过 `@XxxProducer.register("name")` 自注册。
3. Retriever 内部 UnitReader 点读后必须复核 lifecycle、valid-time、event-time 和完整
   FilterExpr；当前态也须按当前 UTC 时间检查 `[t_valid, t_invalid)`。
4. `extensions` 默认透传：RetrievalQuery.extensions → ParsedQuery.extensions。
   `schema_temporal` 是内核保留键，仅在 `globals.schema_temporal_enabled=true`
   时解析；支持 `true`、模式字符串或规范字典。其余 key 依旧透传给自定义
   Recaller，内核不解释。
5. 显式空 `channels` 无效；`RetrievalQuery.channels=None` 时优先使用
   `QueryParser` 建议的通道，parser 未给出建议时再由 Storage 使用已配置入口。
   部分通道失败返回 items 与 `ChannelError`，全部选中通道失败抛
   `StorageRetrievalError`。
6. Fuser 接受物化候选并保持 MemoryUnit 与 evidence；读取前只允许对 id 去重，不得合并
   多通道候选。Fuser 不执行 Reranker。
7. `PipelineRetriever` 的生产装配必须经 `StoreManagerProducer.resolve` 取全局 manager，构造
   持其 `domain_store()`（keyword-only `domain_store=`）；Retriever 不持有召回路——
   `CompositeDomainStore` 的 Recaller 由 `for_manager` 在 manager 装配期按
   `domain_stores.<name>` 的选择键同步组装，非 Composite 实现自带检索路径、无需 Recaller。
   手工/测试接线用 `CompositeDomainStore.bind_recallers`。
8. `MultimodalRetriever` 只组合已注入的基础 Retriever，不得直接依赖 `KvProducer`、
   扫描 KV 或识别具体存储后端。原生、CLM、ELM 分支分别检索；无视频记忆时两个视频
   分支自然为空，再按 RRF 融合并截断到请求的 `top_k`。
10. Schema TemporalEntity 选择器只在普通 Reranker、阈值、top-k 与 disclosure 完成后运行。
   最终普通结果只提供 Entity seed；Entity 索引与直达 Property 路径独立，直达 Property 不受 Entity 名额截断，
   并扩展同实体、同属性前后 `property_extension_step` 条（`default_property`
   除外）。各实体独立截断，不使用全局 `break`。Source fallback 只保留
   普通融合候选中尚未被直达 Property 充分覆盖的 Source，
   不将整个 Scope 平铺到候选。

9. 查询时间边界的 epoch 投影复用 common/time_utils.epoch_ms；真源时间比较复用 as_utc。


### 普通召回的树父隔离

`predicate_builder.build_hierarchy_filters` 对无 kind 请求复用普通父隔离；显式 kind
则下推结构状态、角色与闭区间条件。common 的 is_retrieval_candidate 在真源层复核。
不得仅做返回后过滤，否则父节点会抢占普通叶的候选名额。

### 显式层级召回

- PipelineRetriever 保真传递 kind/role/span，不依赖自定义 Parser 识别新字段。
- 结构条件与用户条件以外层 AND 合并；结构时间索引使用 tree_span_start/tree_span_end。
- recall/get/rank、recall_and_get/rank 和 retrieve 三路都复核本体；索引旧值不能授权命中。
- Discloser 优先使用 ScoredMemoryUnit 自带本体，避免跨完整 Scope 的相同 id 覆盖；
  普通查询不披露父引用，显式层级请求的 parent_id 来自 HierarchyRef。
- MultimodalRetriever 尚不支持层级组合，显式拒绝，不静默退回普通多模态搜索。

### 上卷与展开

- `retriever_impl/hierarchy_rollup.py` 在融合/精排之后、阈值/top_k 之前上卷；父与后代
  共用评分池。MaxP 取最大值，不累加，不为新准入父伪造通道证据。
- `expander.py` 定义内部只读契约；`expander_impl/default_expander.py` 只使用注入的
  同源 DomainStore，按完整 Scope 和有序父子边 BFS，不扫描、不写树。
- `expansion.py` 在单空间或跨空间全局选根后统一分配披露预算和节点上限；
  PreparedRetrievalResult 只在内部传递，不序列化、不直接返回公开调用者。
- 越权 Scope 在点读前拒绝；kind、状态、双向引用、区间覆盖和可见性逐边复核。
  缺节点、坏结构、读失败及截断用 HIERARCHY 诊断表示，不泄露被排除内容。
- 基础展开收尾即限制父子总数为 top_k，后续父子排序在同一收尾处截断之前接入。
- 保留文档模式与可选 Reranker 的 BackendError 降级路径，不用原分支旧实现覆盖。

### 父子结果整理

`child_ranking.py` 使用共享 Embedder 对原始问题和准入后代完整正文同批向量化，
校验数量、维度、有限值和范数后按余弦稳定排序，不复用 Parser 改写向量。
`expansion.py` 中 TIME 父角色查询保留根原序，再全局排列子候选，最后截断到 top_k。
空正文在预算准入后按真源排除、不退还预算；空父仍可展开。无剩余名额、无子或非父角色
时跳过额外 embedding。打分字段仍为原根分或继承分，不伪装成子相关分。
PipelineRetriever.bind_embedder 只负责依赖注入；默认延迟句柄绑定原装配缓存与配置，
Factory.reset_all 后也不能借用另一运行实例的依赖。
