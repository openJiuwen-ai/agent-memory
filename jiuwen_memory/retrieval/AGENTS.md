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
| `evidence_packing.py` | 对已复核的候选按精确上下文 Token 选择证据；提供 ranked/grouped 对照和显式结果扩展，不取数、不调用模型 |
| `graphmem_relation_plan.py` | 将已有查询需求编译为选择/扩展/合并/排序计划，并在 ID 物化前限制初选，可用已返回的原生索引字段及互补分项证据调整一个末位 |
| `graphmem_relation_selection.py` | 逐事件绑定查询实体与场景；边元数据预选、事件区间约束和有界语义探测 |
| `graphmem_relational_navigation.py` | 可选 relational 策略：问题驱动的关系端点选择、有界递归下钻、逐边复核与结构化理由 |
| `graphmem_evidence_graph.py` | 在同一预算中选择摘要与原文、按已验证路径排序、渲染文本图和导航引用 |
| `graphmem_budget.py` | 同一有界图计划中的可选证据 Token 分阶段分配；只核对已提供原文信号，不取数、不调模型 |
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

- **明文拒绝不可降级**：扩展、rollup、多模态及 Schema Temporal 的容错分支必须继续传播 PlaintextDataError，由 API 返回明确错误，不吞成缺失节点或成功空结果。

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

   可选 EvidencePacker 在统一阈值之后、Discloser 之前运行；消费同一候选池，按完整
   Scope+ID 去重。grouped 只组合已召回的同会话 Snapshot 兄弟，不额外加载邻居；
   组选不下时尝试原锚点。来源头部、说话者、消息/事件时间和分隔符均计入 Token。
   同一候选组合的完整渲染与计数可在请求内复用，不能用独立片段计数相加替代整段计量。
   模型预算 tokenizer 与用于倒排检索的 tokenizer 用途不同，不能混用其计量结果。

   首版不与 rollup、展开或 Schema Temporal 同时启用。跨空间合并不能丢弃预算证据包：
   单份不截断的结果原样保留，多份或需要再次截断时明确拒绝。关闭新策略沿用旧行为。

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
6. Fuser 接受物化候选并保持 MemoryUnit 与 evidence。普通路径读取前只对 id 去重；
   显式 relational 路径可先以通道名次选择有界 ID，保留各通道 batch 和 evidence，
   同通道分层只计一次排名信号。该初选不替代物化后的 Fuser，不执行 Reranker。
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
- 展开收尾限制父子总数为 top_k；补位达到窗口即停止，结构 BFS 最后统一截断。
- 保留文档模式与可选 Reranker 的 BackendError 降级路径，不用原分支旧实现覆盖。

### 父子结果整理

- TIME 指定角色上卷时，初次召回及真源复核只允许该角色及其下层，不能仅在返回后筛选。
- TIME 父角色、rollup=True 且 expand_depth>0 时，复用融合后、budget_n 截断后、精排前
  的候选顺序。MaxP 记录已校验祖先归属与实际路径深度，仅补入所选父的合法命中后代。
- keep_recalled 完成后标记 recalled_children_ready，合法空候选也完成准备；窗口未满时
  仅校验已选父对应的提供方，未准备明确拒绝。内部 child_depth 独立校验非负整数并排除 bool。
- 父保持最终查询顺序，后代按融合原序补至 top_k；不重查子树或额外 embedding 排序。
  候选不足允许少于 top_k。普通 Reranker、阈值及父分数逻辑保持。
- 其他展开请求继续 BFS，按遍历顺序返回。跨空间先延迟至全局选根，再按现有空间
  处理顺序、空间内融合原序补入所选父的后代；不比较跨空间分数或按完成时间重排。
- 共用披露预算及完整 Scope+id 去重；空正文在预算准入后按真源排除，不退还预算。
  空父仍可带回合法后代。score 仍表示原根分或继承分。
- 上卷结果保留后代到目标父的真实边距离，经 keep_recalled 传给补位，不为轨迹另查子树。
  expansion 按完整 Scope+id 累计实际返回后代数量、最大深度及停止原因，逐根记录
  expand（mode=recalled_children），最后保留 child_select 汇总。预算/节点上限触发根
  保留原错误；后续未完成根只记录原因。满窗口不新增错误或提供方校验。

### GraphMem 导航与来源补全

`graph_navigation.py` 在阈值后消费已复核候选，经同源 DomainStore.related_edges/get
读取关系与真源。端点版本、Scope、生命周期、过滤条件和来源父子链必须逐项检查。
纯摘要模式只接受摘要角色，不读源 Snapshot 或 TreeFact 文本。
`graph_source_selection.py` 在已验证的来源池内融合现有关键词和向量排名。
明确第一人称问题可按来源标签交错 user 与 assistant，各角色内保持原序。
源码读取和索引探测共享请求预算，纯摘要模式也禁止探测原文索引。
`evidence_packing.py` 的 ranked 保留原召回头部；sources_first 优先原文、最多补充配置数量
的摘要，导航根不强制展示。compact 头部用 E1 等映射 sources 顺序，完整 Scope/ID
仍保留在结构化引用中，摘要覆盖时间不冒充事件时间。最终 rendered_context 包含来源头
和分隔符。navigation 的缺失检查发生在打包后，只表示规划证据是否保留。
图导航不绕过 Discloser 边界，不增加模型调用；与现有 rollup/expand/Schema 的组合仍拒绝。

GraphMem 专用导航已消费关系时，对应数据面的 graph_enabled 应关闭通用 GraphRecaller，
防止同一批关系先以统一分数改变融合入口、再被导航扩展。此开关不关闭图存储或
related_edges；其他数据面可保留自己的通用图召回配置。

递归 TIME 节点可以重复使用 time_span 角色。GraphMem 导航使用独立物理高度和
验证后的直接引用，原文附件回溯由 visited 集合和共享读取预算限界，不写死四层。
递归节点不携带 TreeFact；原文模式按直接子树读取，纯摘要模式先检查直接子类型
再读取，不能为判断节点类型而读取原文。

`graphmem_query.py` 定义可选的 typed 查询需求与软匹配；
软需求中唯一、明确的句末 `as of` 日历日期表示累计截止上界；不能把更早事件当成
窗口外。解析不借机器时钟猜测相对日期，不改 API valid-time `as_of` 或硬过滤。
`query_parser_impl/graphmem_query_parser.py` 注册 graphmem_query，最多一次既有 LLM
调用，不替换原问题向量、不产生授权谓词。问题时间在此目标下是对多事件摘要的软
导航信号，调用方硬过滤与 as_of 仍透传。
可选 resolve_relative_dates 复用共同的日历解析函数，按 graphmem_reference_time 校验
模型已给出的单个相对日窗口。唯一明确的 today/yesterday/tomorrow、last/next 星期
和整数天 ago 可以校验；显式 before/after/since/as of 等边界保留开闭方向。不凭机器
日期填补，保留已有等价字符串；多日期、模糊日期、时段、引号、空窗口及宽泛状态不改。
默认关闭且严格 bool；模型请求、原向量和分析费用不变。
`graphmem_request_reads.py` 按完整 Scope+ID 缓存点读。关系规划和各预算阶段共用
一次候选计划。语义查询词仅用于池内排序。分析发生后可扣证据预留，模型用量以实际
账单为准。显式 channels 约束后续索引和关系探测。打包轨迹记录有限池内的覆盖信号。

### 问题驱动的有界关系计划

relational 路径从已有 QueryNeeds 编译查询计划；入口 ID 初选、关系端点、摘要下钻和
原文额度分别限制。先在已到达 ID 范围内调用已有索引，再做真源及父子链复核；原文
名额跨分支轮流分配，避免按遍历顺序耗尽。经
pack_closure 入口保留文本图与结构化图输出。原文全局顺序复用规则 RRF 与可选角色
交错，不增加排序模型。初选以后的所有正文尝试和索引 Scope 探测受请求级缓存约束。

关系端点排序可复用同一索引结果中的事件元数据与原生向量名次。只有在已分配的
阶段额度内才能调整末位探测，继续通过统一真源读取和边指纹核验。探索理由和逐事件
覆盖分开返回，结果仍共用一次探索额度。
结构化图保留有来源的补边路径，示例回答入口只让经核验的普通关系参与段落重排和
关系说明。补边找到的原文保留在证据段内。



场景词匹配须区分计数操作与证据内容：collection 的 number/total/count/quantity
不要求在每条证据中重复出现；其余词面匹配、否定、区分性场景词与实体归属保持。原查询、分析输出、
向量、初选索引词和查询计划不因此改写，空场景词不能算作已覆盖需求。

E 标识只对应完整披露的内容，N 标识只对应省略正文的导航节点；两类来源分别记录。
文本图可移除重复环，仅展示连接路径，完整选中边及其理由仍返回在结构化图中。
最终证据顺序沿已验证图路径，图文字和所有内容同预算计量；摘要额度不能挤占全部
原文。纯摘要仍禁止读取原文索引或正文；记录未命中、受限与失效，不解释为事实不存在。

比例开启时，PipelineRetriever 在初选物化前调用 candidate_limits，读取索引元数据
计数并把本次限额传到打包；导航不能再次增大。摘要探测包含初选和祖先，原文名额在
查索引前跨分支分配，通道与相邻补全合用。unsupported 计数不降级为全量 list/get。
原文模式记录实际池占库比例；纯摘要模式不做原文计数。规则路径可在已读摘要的已存
实体/别名中匹配问题显式姓名，使用词边界并不修改调用方 QueryNeeds，无需模型调用。
代词与 user/assistant 等角色名不作为命名实体；也不能通过存储别名把“I”推断为某人。
分项明确提到的实体只绑定该分项；没有显式名字的分项仅继承所有分项都未提到的
公共实体，不能从其他分项借名字。单实体、单分项中的所属对象可在已读摘要里解析
明确的所有者，但必须保留对象词作为场景条件；已有完整名称命中或所有者有歧义时
保持原查询需求。此规则不新增读取、模型调用或候选名额。
初选 ID 在原名额内依次保留标准 vector_l2 首位和原融合排序的首个其他候选，
然后将标准 vector_l1 首位放入剩余名额；去重后其余位置保持原融合顺序。
详细与概览视图的首位同分选择不依赖 batch 枚举顺序，融合余位保持原有稳定次序。
缺 vector_l2 时沿用原融合；缺 vector_l1 时不补造概览候选，不为重复层增加通道票数。
关系端点探测仍只保留 vector_l2 首位，不应用概览优先级。
保留名额不代表事实命中，不能跳过真源、实体、时间、边版本或场景核验；
原文子节点仍按原有分通道配额选择，不应用这项摘要入口优先级。
initial_summary_fraction 只在有效摘要总限额内分配入口名额，不增加任何候选总上限。
多跳关系探索按剩余跳数分配本轮新增摘要探测名额，保持广度优先；已读取端点不重复
占用新增名额。如果本轮已读端点没有合法的新分支，再用保留名额检查同轮剩余排名
候选。单跳沿用原额度；所有失败探测仍计入统一上限，实体、时间、版本及语义探索
准入条件不变。relation_probe_stages 记录各轮额度、实际新增探测和无新分支回补。
可选 reserve_descendant_probes 在关系正文探测前，为已选递归摘要尚未读取的直接子摘要
保留原有分支宽度内的额度。只读已到达节点的子引用与角色元数据，按完整 Scope/ID
去重；原文、无效角色或越界引用不占额度。每轮重新计算，已探测子节点不再占新增
名额。无新关系分支时的回补也不能花掉这些额度；真正下钻仍须索引命中及父子链复核。
默认关闭，不增加候选、读取、跳数或模型调用；该预留只覆盖当前已知的直接子层。
可选 refill_entry_probes 仅支持 ID-first 召回。保留本次原始索引返回的有限 ID 元数据，
不扩大各路初选 recall_limit；全池不超过摘要总限额。原关系导航未增加通过需求匹配的邻居且原树
下钻完成后，最多补查一批、数量不超过本次初选额度，仍共用摘要及追加正文读取上限。
单次语义或实体试探不算需求匹配，不能仅凭它停止补查；任何非试探邻居仍保留原停止行为。
初选失败点读同样占摘要额度，不重试。补查按原 Scope 和摘要角色复核，沿已验证子树
取原文；纯摘要模式仍不访问原文。deferred_vector_keyword_match 是独立索引入口，
不能伪造 related 边或覆盖证明；其 contains 路径可作为独立根展示。默认关闭。
可选 reserve_corroborated_entry 复用同一批原生索引 ID，在较小的摘要限额内为
两个高排名视图同时命中的候补保留一个额度。失败读取同样计费，入口和总上限保持原值。
可选 prefer_coarse_keyword_entry 在单实体、单需求且初选之后仅余 2 至 4 个摘要额度时，
可将这一个预留给跨两个词项索引层级得到支持的粗粒度首位命中。
可选 prefer_temporal_keyword_entry 在同一名额内为单实体、单需求的时间问题提供
中层原生词项候补。已有合格的跨层词项候补时优先保留它；整个请求最多预留一个。
补查子树不参与原路径的原文名额重新分配。先按原限额完成原路径的索引选择与相邻
补全，再用尚未占用的原文 ID 名额查询新增子树；总额度保持。两部分仍共用最终
排序和 Token 打包，候选保留不等同于保证每条原文最终都被披露。
文本证据图先走 contains 子分支，再走 related 关系；子分支无已知事件时间优先级时
按原生 child_ids/child_scopes 排序，不按检索名次打散同一会话或消息分片。
relational 的各原文索引通道先分配整数名额；原文和摘要下钻只从索引返回 ID 中选择。
空结果或通道重复不得以未命中的子节点前缀补位。关系端点同样必须在本次索引返回 ID
内，empty_index_branches 记录空分支，index_fallbacks 保持零；相邻原文仍共用分支名额。
可选 prefer_indexed_operands 只在第一人称集合查询中将已到达原文分支的一个相邻名额
换给现有词项/向量索引排名，且至少保留一个相邻名额。原文总限额和分支限额不增加。
递归事件的 sources 仅用于在同一名额内选择直接子节点索引子集；编号必须是范围内的
整数且匹配完整 Scope 与角色。索引命中、真源可见性和父子链检查均不能被事件引用绕过。
父事件的需求命中不能直接记作子节点证据覆盖；原文候选只记录选择理由并独立核对内容。
可选 detail_source_hints 在没有匹配事件引用时，使用已读父正文中的有效逐子摘要区间
定位候选子集。单句核对实体与场景，明确日历窗口仍走有日期依据的事件路径。来源
先经同一配额内的索引查询再读正文；摘要区间只给选择理由，不充当原文或事件证明。
detail_references 与 event_references 分开返回，前者不参与事件代表计数；纯摘要
模式仍不查询原文索引。绑定失效、缺失或索引未命中时不得按子节点前缀补位。
可选动态证据预算只复用已有计划；各阶段不得重新查索引、扩大候选池或调用模型。
启用 `adaptive_target_tokens` 时，按问题的操作与子需求数决定常规或扩展证据额度。关系闭包检查沿用实际原文、来源归属与分片完整性。评分及 benchmark 标签仅可用于离线分析。
提前停止须检查原文单句信号、未提供的高匹配来源和已选消息分片完整性；摘要和导航
引用不算原文覆盖。第一人称身份仅来自显式说话者，不能将第三人称事件归到该说话者。
图打包可补尚未有来源代表的匹配事件，但只在同一池与预算中进行；事件身份包含完整
父节点键。替代结果须保留全部已有事件代表且严格增加代表集合，重复引用不能触发替换。
这项来源代表计数不得充当原文召回或事实正确性指标。

场景匹配按子需求和单个事件的实体归属判断，不能把摘要里不同事件的名字和词项
拼成已满足条件。详细摘要的补充匹配限定在同一句中；日历窗口独立于场景词项核对。
关系扩展按本轮已覆盖需求重新选择分支；向量正分只供排序，不能
单独证明实体、事件或时间匹配。未命中词面场景的语义探索每请求最多一条，明确记录
semantic_probe，不标为需求命中。
语义探索只准入尚未访问的节点，在生成理由与最终准入时均检查；已有节点上的明确
实体/场景/时间关系仍可保留。原索引排名不变；union 集合问题把探索资格给本轮
已选索引端点中的首个未访问节点，避免已选节点占住寻找其他实例的机会。
project/join/compare 保留原排名第一的资格；首个新端点未通过核验时不逐个放宽。
可选 query_guided_entity_probe 使已有的同一次探索也能沿经过两端实体复核的 entity
边进行；沿用上述端点资格且必须有正查询向量分。问题有具名实体时，共同实体
必须匹配该名字；无具名主体时只表示共享上下文。两种探索共用每请求一次的额度。
理由保留 probe_relation 和 shared_relation_entities，文本图显示 query_probe(entity)，
不把该路径标成场景、实体需求或事件时间已满足；失效边、窗口冲突和候选预算仍拒绝。
相对事件顺序绑定问题中明确的 before/after 锚点，与沿边遍历的方向分开；时间理由
只选择已建边记录的事件对。

图返回包含到证据节点的验证路径。省略正文的关系中继也保留导航引用，避免只保留
树祖先而丢失跨会话路径。clause_bindings 只含已披露的 E 节点；导航线索独立进入
navigation_clause_bindings，不能据此声称已提供所需证据。
N 理由仅保留选择方式和需求标识，不返回被省略摘要里的事件句子。

EvidencePacker 的 Snapshot 头只展示消息时间，不把整个原文单元的 t_event 渲染为
单一事件日期；非 Snapshot 的事件时间和图中已归属的事件区间保持。原文本身、
存储字段和来源摘要哈希不修改，头部变化仍进入同一个精确 Token 计数。
回答客户端示例位于 examples/graphmem_evidence_reader.py：常规材料保持问题在前的
请求；出现行首 Instructions/Query/Task/System prompt 指令块时引用完整材料，并在
明确结束后重申当前问题。输出校验只判断格式和非空性，不判断正确性。至多一次
格式修复复用原问题/原证据，所有调用由客户端计量，不在检索层发起模型调用。

`graphmem_chunk_hints.py` 把有效的 L2 分片位置映射到当前摘要的直接子节点。原索引命中和相邻原文先占用分支配额，位置提示只使用剩余额度。
