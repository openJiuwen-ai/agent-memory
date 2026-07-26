# Agent Memory Retrieval（检索层）

**规约文档**：[S04-retrieval.md](../../docs/specs/S04-retrieval.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

混合检索的完整链路编排：查询理解 → 并行多路召回 → 融合 → 精排 → 相关性阈值 → 渐进式披露 → 返回 + 检索轨迹。记忆接口层的 `recall` 映射到本层 `Retriever`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | RetrievalOperator 基类：所有检索层算子的自描述契约 |
| `types.py` | 检索层数据类型：RetrievalQuery / ParsedQuery / ScoredUnit / RetrievedItem / RetrievalResult 等 |
| `query_parser.py` | QueryParser 接口：查询理解（去噪/改写/分词/实体/向量化/时间解析） |
| `recaller.py` | Recaller 接口：单路召回（向量/关键词/图/文档/时序） |
| `fuser.py` | Fuser 接口：多路融合排序（重排由 common `Reranker` 独立阶段承担） |
| `discloser.py` | Discloser 接口：渐进式披露（L0 摘要/L1 片段/L2 全文） |
| `retriever.py` | Retriever 接口：检索层入口，编排完整链路 |
| `query_parser_impl/` | QueryParser 实现目录（simple_query_parser / sanitize / time_parse） |
| `recaller_impl/` | Recaller 实现目录（keyword / keyword_l0/l1 / vector / vector_l0/l1 / graph） |
| `fuser_impl/` | Fuser 实现目录（rrf【默认】/ weighted_rrf / score_max）+ `layered_merge` 分层归并前处理 |
| `discloser_impl/` | Discloser 实现目录（structured / truncating） |
| `retriever_impl/` | Retriever 实现目录 |
| `bootstrap.py` | 统一触发所有检索算子注册 |

## 检索链路

```
1. QueryParser.parse(query) → ParsedQuery
     ↓ 结构化查询（tokens/keywords/entities/vector/scalar_filters/channels）
2. ParsedQuery.raw 为空时短路返回空结果
     ↓
3. 并行 Recaller[i].recall(scope, parsed_query, recall_k) → list[list[ScoredUnit]]
     ↓ 多路召回；recall_k = min(max(top_k*factor, floor), recall_max) 超采样
     ↓ 向量通道可选 min_similarity 语义前置过滤（打分方向由 store 契约声明）
4. Fuser.fuse(parsed_query, candidates) → list[ScoredUnit]
     ↓ 跨通道融合排序
5. 截断精排预算 budget = fused[:max(rerank_max, top_k)]
     ↓
6. UnitReader 点读 MemoryUnit → 有效性过滤（lifecycle/as_of/event-time/filters）
     ↓
7. 可选 Reranker 精排（记 calibrated 标志）
     ↓
8. 相关性阈值：绝对 min_score（仅校准路径）+ 相对 ratio（校准/未校准两路，出厂默认均为 0=关闭）
     ↓ + min_results 从正分候选兜底回填（结果数可 < top_k）
9. 截断 top_k
     ↓
10. Discloser.disclose(...) → list[RetrievedItem]
     ↓ 按层级加载内容（L0/L1/L2）
→ RetrievalResult（items + trajectory）
```

## L0/L1 分层召回 + 三层披露

L0/L1 分层检索在 content（L2）之外，额外召回预生成的概要（L0）/片段（L1），三层并行召回 + 融合 + 三层一次性披露。详见 `docs/features/common/F01-memory-layer.md` §6；融合策略与分层归并见 `docs/features/retrieval/F04-score-max-fusion.md`。

**构建侧**（已就绪）：`LayerAnnotator` 对长 content 产出 `unit.layers.l0`/`l1`，`VectorIndexBuilder`/`FulltextIndexBuilder` 对非空 layers 整段 embed 建独立分表（`vector_store.layers_l0/l1`、`fulltext_store.layers_l0/l1`）。向量 record id 为 `{unit_id}-layer-l0/l1`，全文 document id 为 `{unit_id}:l0/l1`。

**召回侧**：复用 `VectorRecaller`/`KeywordRecaller` 加 `layer` 参数（l2/l0/l1），注册 `vector_l0/l1`/`keyword_l0/l1` 具名实例，查对应分表 store。`layers_index_enabled` 开时（回退 globals）接入——store 为 None 时 recall 返空（该层未配，向后兼容）。同通道不同层级，Fuser 按 unit_id 聚合：**同通道多层命中一律取 MaxP（最高分）**（F01-memory-layer §6.3）。分层是同通道的多个索引入口，不是独立信号源——按多路处理会让分数偏向"有 layers 的 unit"，那是索引覆盖差异而非相关性差异。三层 store 必须使用同类后端、同一分词/度量配置，并统一满足「分越大越相关」。归并由 `fuser_impl/layered_merge.py` 统一前置，三个 Fuser 实现共用；未启用分层时为恒等变换。跨通道如何合并则取决于所选 Fuser：默认 `rrf` 按名次倒数累加，`weighted_rrf` 加通道权重，可选 `score_max` 取通道归一化分的最大值。

**披露侧**：`RetrievedItem` 三层一次性填充——`abstract`(L0) / `overview`(L1) / `content`(L2)，优先用 `unit.layers.l0/l1`（空则回退截断/取窗兜底）。调用方按需取用：紧预算用 abstract，中等用 overview，全文用 content。`level` 标本次披露主层级（ADAPTIVE 按 max_tokens 选）。

## 行为铁律

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

7. **Discloser 只做内容塑形**
   候选记忆单元已由 Retriever 经 UnitReader 点读、有效性过滤、（可选）重排后给定。Discloser 不再做点读/过滤/重排，只按 level 截/取内容产出结果。

## 与其他子目录的边界

**本模块管**：
- 查询理解（QueryParser）
- 多路召回编排（Recaller + Retriever）
- 融合与重排（Fuser）
- 渐进式披露（Discloser）
- 检索轨迹记录（TrajectoryStep）

**不管**：
- 鉴权（归 `api`）
- 记忆写入/演进（归 `construction`）
- 存储实现（通过注入的 Store 抽象间接调用）
- Tokenizer/Embedder/Reranker 实现（消费 `common` 注入的实例）

## 本地约束

1. 所有 Operator 必须实现 `operator_type()` 和 `health()`（继承自 `RetrievalOperator`）。
2. Recaller 实现必须声明 `channel()` 返回对应的 `RecallChannel`。
3. 算子实现通过 `@XxxProducer.register("name")` 自注册。
4. Retriever 内部 UnitReader 点读后必须做 lifecycle 过滤（排除 FORGOTTEN）。
5. `extensions` 字段透传配置：RetrievalQuery.extensions → ParsedQuery.extensions，供自定义 Recaller 按约定 key 读取，内核核心不解释。
