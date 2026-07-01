# Agent Memory Retrieval（检索层）

**规约文档**：[S04-retrieval.md](../../docs/specs/S04-retrieval.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

混合检索的完整链路编排：查询理解 → 并行多路召回 → 融合 + 重排 → 渐进式披露 → 返回 + 检索轨迹。记忆接口层的 `recall` 映射到本层 `Retriever`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | RetrievalOperator 基类：所有检索层算子的自描述契约 |
| `types.py` | 检索层数据类型：RetrievalQuery / ParsedQuery / ScoredUnit / RetrievedItem / RetrievalResult 等 |
| `query_parser.py` | QueryParser 接口：查询理解（去噪/改写/分词/实体/向量化/时间解析） |
| `recaller.py` | Recaller 接口：单路召回（向量/关键词/图/文档/时序） |
| `fuser.py` | Fuser 接口：多路融合 + 重排 |
| `discloser.py` | Discloser 接口：渐进式披露（L0 摘要/L1 片段/L2 全文） |
| `retriever.py` | Retriever 接口：检索层入口，编排完整链路 |
| `query_parser_impl/` | QueryParser 实现目录（simple_query_parser / sanitize / time_parse） |
| `recaller_impl/` | Recaller 实现目录（keyword / vector / graph） |
| `fuser_impl/` | Fuser 实现目录（rrf / weighted_rrf） |
| `discloser_impl/` | Discloser 实现目录（structured / truncating） |
| `retriever_impl/` | Retriever 实现目录 |
| `bootstrap.py` | 统一触发所有检索算子注册 |

## 检索链路七步

```
1. QueryParser.parse(query) → ParsedQuery
     ↓ 结构化查询（tokens/keywords/entities/vector/scalar_filters/channels）
2. ParsedQuery.raw 为空时短路返回空结果
     ↓
3. 并行 Recaller[i].recall(scope, parsed_query, top_k) → list[list[ScoredUnit]]
     ↓ 多路召回
4. Fuser.fuse(parsed_query, candidates) → list[ScoredUnit]
     ↓ 跨通道融合排序
5. UnitReader 点读 MemoryUnit → 有效性过滤（lifecycle/as_of/event-time/filters）
     ↓
6. 可选 Reranker 精排 → 截断 top_k
     ↓
7. Discloser.disclose(...) → list[RetrievedItem]
     ↓ 按层级加载内容（L0/L1/L2）
→ RetrievalResult（items + trajectory）
```

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
