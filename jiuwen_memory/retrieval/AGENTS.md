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
| `expander.py` | Expander 接口、内部请求/诊断与完整 Scope 节点身份；不查询索引、不评分 |
| `expander_impl/` | 同源 DomainStore 的有界只读 BFS，逐边范围、结构及可见性复核 |
| `expansion.py` | 准备物化根、单/跨空间共享预算收尾；只依赖接口，内部准备结果不序列化 |
| `retriever.py` | Retriever 接口：检索层入口，编排完整链路 |
| `retriever_impl/hierarchy_rollup.py` | 同源数据面有界祖先点读、完整身份聚合与 MaxP；不扫描或写树 |
| `query_parser_impl/` | QueryParser 实现目录（simple_query_parser / sanitize / time_parse） |
| `fuser_impl/` | Fuser 实现目录（rrf【默认】/ weighted_rrf / score_max）+ `layered_merge` 分层归并前处理 |
| `discloser_impl/` | Discloser 实现目录（structured / truncating） |
| `retriever_impl/` | Retriever 实现目录；pipeline 经 `StoreManagerProducer.resolve` 取全局 manager 并持其 `domain_store()`；multimodal 组合原生、CLM、ELM 三个过滤分支并执行 RRF 融合 |
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
4. Fuser 前完成真源复核（含显式 hierarchy 条件），再做分层归并和跨通道融合
5. 截断精排预算 budget = fused[:max(rerank_max, top_k)]
     ↓
6. 可选 Reranker 精排（记 calibrated 标志）
     ↓
   [rollup=True] 祖先准入 + MaxP；此前候选池只移除 typed 输出角色条件
     ↓
7. 相关性阈值：绝对 min_score（仅校准路径）+ 相对 ratio（校准/未校准两路，默认关闭）
     ↓ + min_results 从正分候选兜底回填（结果数可 < top_k）
8. 截断 top_k
     ↓
9. Discloser.disclose(...) → list[RetrievedItem]
     ↓ 塑形 L0/L1/L2 三字段；展开请求暂存根及来源，不读后代
10. [expand_depth>0] 最终选根后共享预算准入根，再逐根 BFS + 子节点披露
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

11. **结构条件不由 Parser 推断或改写**
    `PipelineRetriever` 在 parse 后从 `RetrievalQuery` 回填 kind/role/span，叠加外层 AND
    下推；rollup=True 时只移除 typed 输出角色，父/后代同池评分。三条检索路径与
    关键词实体扩展共用真源复核。span 是独立的闭区间轴，TIME
    查询可不指定窗口，但节点自身必须有有效区间。`parent_id` 从真源返回，不触发遍历。

12. **展开不放宽访问范围**
    只有显式正整数 expand_depth 才展开。点读前校验 Scope，真源复核双向引用、kind、
    状态、span 覆盖和既有过滤；仅移除 typed 父角色，不移除业务/权限过滤。
    按完整 Scope+id 去重，坏分支只读跳过，不自动修复。

13. **先选根、再共享预算展开**
    top_k 限制最终根（含上卷准入父）；跨空间使用内部 defer_expansion 协议，合并选根后才读取
    后代。节点和主披露级由 retrieval.expansion 据 Discloser 实际字段准入，预算与
    节点上限跨根、跨空间共享。子继承根分，不冒充独立相关性评分或 MaxP。
    issues 经 HIERARCHY 诊断返回，即使不开轨迹也不能静默隐藏截断。

14. **上卷与展开独立**
    MaxP 在精排后、阈值/top_k 前，按完整 Scope+id 取父/后代最高分；不累加、不复制
    子 evidence 冒充父命中。只读父引用，逐边核对授权范围、可见性、双向边与 span。
    祖先缓存不能绕过不同子到该父的反向边检查，不能穿过不可见中间节点。

15. **物化披露不退化为裸 id 查找**
    内置 Discloser 优先取候选自身 unit；旧 ScoredUnit 才查 units 表。按候选顺序
    一对一返回，展开准备按顺序绑定来源，不允许同名不同 Scope 父覆盖。

## 与其他子目录的边界

**本模块管**：
- 查询理解（QueryParser）
- 多路召回结果的编排消费（Retriever；单路 Recaller 归 `storage` 数据面）
- 融合与重排（Fuser）
- 渐进式披露（Discloser）
- 只读结构上卷、MaxP、展开及共享披露预算（hierarchy_rollup / Expander / expansion）
- 检索轨迹记录（TrajectoryStep）

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
4. `extensions` 字段透传配置：RetrievalQuery.extensions → ParsedQuery.extensions，供自定义 Recaller 按约定 key 读取，内核核心不解释。
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
   未适配延迟展开前必须显式拒绝非零深度，不能静默截断已经展开的子项。
   未适配上卷后的分支评分前同样明确拒绝 rollup=True。
