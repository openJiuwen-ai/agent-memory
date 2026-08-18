# S04 — 检索层（Retrieval Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/retrieval/ |
| 最近一次修订日期 | 2026-08-18 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md、docs/features/construction/F04-cc-memory-compat.md、docs/features/retrieval/F02-retrieval-threshold-topk-design.md、docs/features/retrieval/F03-metadata-filtering.md、docs/features/retrieval/F04-score-max-fusion.md、docs/features/retrieval/F05-storage-retrieval-pipelines.md |

## Metadata 检索契约

FilterExpr 以 `user_metadata.<key>` 表示用户字段，以 `system_metadata.<key>` 表示
内部系统谓词，两者不 fallback。`RetrievedItem` 返回 `user_metadata`，普通搜索结果
不暴露 `system_metadata`。

## 范围 / 边界

**管什么**：
- 混合检索的完整链路编排（查询理解 → 并行多路召回 → 融合 → 精排 → 相关性阈值 → 渐进式披露 → 返回 + 检索轨迹）
- 查询理解：去噪/改写/分词/实体/向量化/时间解析
- 多路召回：按配置启用的通道并行检索（向量/关键词/图/文档/时序）
- 融合：多路候选合并去重、归一化打分、取最大值/RRF/加权排序
- 精排（可选）：调用 Reranker 做 cross-encoder 精排
- 相关性阈值：绝对/相对阈值裁剪低相关候选（结果数可 < top_k），min_results 兜底回填
- 渐进式披露：L0 摘要/L1 片段/L2 全文 按需加载
- 检索轨迹：可观测的非黑盒调试信息

**不管什么**：
- 不做鉴权（由 `src/api` 层负责）
- 不做记忆写入/演进/落盘
- 不直接操作存储写入（只做存储读取/检索）
- 不实现 Embedder/Tokenizer/Reranker 等共享插件（消费 `src/common` 注入的实例）

## 不变量

1. **scope 是独立轴**：`scope: Scope` 作为 `Retriever.retrieve` / `Recaller.recall` 的显式第一入参贯穿全链路，不随 `RetrievalQuery` 携带、也不混进 `filters`。
2. **query 是「找什么」，scope 是「在谁的范围内找」**：两条轴分开传。
3. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
4. **通道到物理 Store 非 1:1**：一路可对应一个 Store，也可多路合到一个 Store（如 FusionStore），TEMPORAL 通常是叠加在其他通道上的时间过滤。
5. **读写同一套共享插件**：QueryParser 必须与构建侧使用同一套 Tokenizer/Embedder/FeatureExtractor，保证同词表/同向量空间。
6. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `RetrievalOperator`。
7. **scalar_filters 与软召回信号分离**：ParsedQuery 中 `scalar_filters`（硬前置过滤）与 `tokens/keywords/entities/vector`（软召回信号）不能互相折叠。
8. **双时间轴独立**：`as_of`（valid-time 回溯点）与 `time_from/time_to`（event-time 范围）是两条独立时间轴。
9. **召回分数高分优先**：chunk→unit MaxP、分层归并与融合排序统一按「分越大越相关」处理；向量 Recaller 不接受 L2 等 lower-is-better 度量。
10. **生产过滤先于 top-k**：Milvus / Elasticsearch / pgvector 必须在
    `limit/top_k` 前完整下推 `FilterExpr`；UnitReader 的真源复核只做纵深防御，
    不能补回已被截断的候选。
11. **系统谓词不可被用户逻辑稀释**：lifecycle / valid-time / event-time 谓词与用户
    `filters` 以外层 `AND` 合并，用户表达式内部的 `OR` / `NOT` 不能绕过系统约束。
    系统侧的事件窗以 `OR(AND(GTE from, LT to), EQ T_EVENT_UNKNOWN)` 子树表达
    「窗内命中 OR 未知放行」，整棵 OR 子树作为外层 AND 的一个 child 不摊平——
    安全谓词不被稀释，同时 `t_event=None` 的派生不被窗下推清空（见过滤表达式段）。
12. **rank 只包含 Fuser**：Fuser 在物化候选上做分层归并和跨通道融合；Reranker 保持后续
    独立阶段，不下沉到 Storage 的 retrieve 入口。
13. **部分失败显式返回**：部分召回入口失败时继续处理成功候选并返回 `ChannelError`；全部选中
    入口失败抛 `StorageRetrievalError`。显式空 channels 是无效输入。

## 接口契约

### RetrievalOperator（基类，`base.py`）

```python
class RetrievalOperatorType(str, Enum):
    QUERY_PARSER / RECALLER / FUSER / DISCLOSER / RETRIEVER

class RetrievalOperator(ABC):
    def operator_type(self) -> RetrievalOperatorType  # 自描述
    def health(self) -> None                          # 存活探测
```

### Retriever（`retriever.py`）

检索层入口，编排完整链路。

| 方法 | 签名 | 语义 |
|------|------|------|
| `retrieve` | `(scope: Scope, query: RetrievalQuery) -> RetrievalResult` | 在 scope 范围内执行完整检索链路，返回结果项与轨迹 |

**retrieve 路径**：
```
QueryParser.parse(query) → ParsedQuery
→ 若 ParsedQuery.raw 为空则短路返回空结果
→ 按 Storage.preferred_retrieval_pipeline 选择 recall→get、recall_and_get 或 retrieve
→ Fuser 前物化候选并完成 lifecycle/valid-time/event-time/filters 真源复核
→ Fuser.fuse(parsed_query, candidates) → list[ScoredMemoryUnit]
→ 截断精排预算
→ 可选 Reranker 精排 → 相关性阈值过滤（结果数可 < top_k）→ 截断 top_k
→ Discloser.disclose(parsed_query, candidates, units, level, max_tokens) → list[RetrievedItem]
→ 组装 RetrievalResult（items + trajectory + errors）
```

### QueryParser（`query_parser.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `parse` | `(query: RetrievalQuery) -> ParsedQuery` | 将检索请求解析为结构化查询表示 |

**产出**：raw/rewritten/intent/tokens/keywords/entities/vector/scalar_filters/as_of/time_from/time_to/channels/extensions。

`raw` 表示进入检索链路的规范化 query 文本，不要求逐字等于调用方传入的
`RetrievalQuery.text`。默认 `simple` 实现会先剥除上游包装噪声（如 UTC 时间戳、
`Sender (untrusted metadata)` 元数据行），再基于清洗后的文本产生分词、向量和时间窗。

### 过滤表达式

`RetrievalQuery.filters` 的内核类型是 `FilterExpr | None`：

- `FilterClause(field, op, value)` 表示叶子谓词；
- `FilterGroup(logic, children)` 表示可嵌套的 `AND` / `OR` / `NOT`；
- 旧 `list[FilterClause]` 在查询对象边界规范化为 `AND`；
- dict DSL 仅作为 API / SDK 兼容输入，在进入检索内核前转换为 `FilterExpr`；
- scope 字段不得进入 filters，隔离仍由 `scope: Scope` 专用入参保证。

metadata 比较保留 JSON 原生类型。查询侧不做 string / number / boolean 隐式互转；
范围算子只接受有限 `int` / `float`，同一业务 key 的类型稳定性由调用方负责。
字段形态同样属于比较语义：`EQ` / `IN` 的正向匹配只命中标量，`CONTAINS` 只命中
数组成员；`NE` / `NOT_IN` 分别按对应正向谓词取反。标量 `CONTAINS` 不退化为等值或
字符串子串，数组 `EQ` / `IN` 也不退化为成员匹配。

历史 `as_of` 查询追加 `lifecycle != forgotten`、`t_valid <= as_of`、
`t_invalid > as_of`。开放有效期在索引中投影为 `T_INVALID_OPEN`，真源仍保持
`t_invalid=None`；UnitReader 按真源 `[t_valid, t_invalid)` 区间复核。

事件时间窗 `[time_from, time_to)` 下推为 `OR(AND(GTE from, LT to), EQ 0)` 子树：
`AND` 子组放行窗内已知事件时间 unit，`EQ 0` 分支放行 `t_event=None` 的派生
（F07 净化后此类派生常见）。真源 `t_event=None` 在索引投影与 `memory_filter`
后置复核两侧都投影为哨兵 `T_EVENT_UNKNOWN=0`，使下推与复核语义不分叉。
`in_event_window` 后置仍读真源 datetime、对 None / naive 放行，与下推的
「窗内 OR 未知放行」意图对齐。半开边界与原扁平 GTE+LT 一致，不引入 LTE。

属性问（多大/几岁/爱好/是谁/住址/名字/生日/年龄…）即便含时间词也清空
`time_from/to` 不下推——属性问不是事件时间检索，误下推会放大 `t_event=None`
派生的误伤。`time_parse` 入口对此类 query 直接返回 `(None, None)`。

### Recaller（`recaller.py`）

单路召回算子。一个 Recaller 对应一条召回通道。

| 方法 | 签名 | 语义 |
|------|------|------|
| `channel` | `() -> RecallChannel` | 返回本召回路对应的通道 |
| `recall` | `(scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]` | 在 scope 范围内本通道内召回 top-k 候选 |

### Fuser（`fuser.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `fuse` | `(query: ParsedQuery, candidates: list[list[ScoredMemoryUnit]]) -> list[ScoredMemoryUnit]` | 融合已物化的分入口候选，保持 MemoryUnit 与 evidence |

### Discloser（`discloser.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `disclose` | `(query, candidates, units, level, max_tokens=None) -> list[RetrievedItem]` | 按披露层级为候选塑形内容 |

**参数说明**：
- `query: ParsedQuery` — 提供改写后查询与关键词（L1 据此挑最相关片段）
- `candidates: list[ScoredUnit]` — 最终顺序的候选列表（已融合/重排）
- `units: dict[str, MemoryUnit]` — unit_id → MemoryUnit 的内容查找表
- `level: DisclosureLevel` — L0/L1/L2/ADAPTIVE
- `max_tokens: int | None` — 自适应披露预算

## 数据结构

### RetrievalQuery（`types.py`）

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `text` | str | "" | 自然语言查询 |
| `filters` | FilterExpr \| None | None | 标签/元数据硬过滤；支持 AND / OR / NOT 树 |
| `as_of` | datetime \| None | None | valid-time 回溯点 |
| `top_k` | int | 10 | 返回条数上限（经相关性阈值后实际可少于此数） |
| `disclosure` | DisclosureLevel | L0 | 结果披露层级 |
| `max_tokens` | int \| None | None | 自适应披露预算 |
| `with_trajectory` | bool | False | 是否返回检索轨迹 |
| `channels` | list[RecallChannel] \| None | None | 覆盖启用的召回通道 |
| `rerank` | bool \| None | None | 覆盖重排开关 |
| `include_archived` | bool | False | 是否纳入 archived 记忆 |
| `extensions` | dict[str, str] | {} | 调用方自定义透传配置 |

### ParsedQuery（`types.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `raw` | str | 进入检索链路的规范化 query；默认实现会先做保守去噪 |
| `rewritten` | str | LLM 改写后的 query |
| `intent` | str | 意图标签 |
| `tokens` | list[str] | 分词结果 |
| `keywords` | list[str] | 抽取的关键词 |
| `entities` | list[Entity] | 实体（FeatureExtractor NER 抽取，graph 通道召回读本字段做实体扩展；实体反向索引召回**不读本字段**——它读 fulltext L2 文档 `metadata['entities']` 明文，见 [F06](../features/retrieval/F06-entity-recall-channel.md)） |
| `vector` | list[float] | query 向量 |
| `scalar_filters` | FilterExpr \| None | 已规范化的硬前置过滤谓词 |
| `recheck_filters` | FilterExpr \| None | 用户原始硬过滤谓词，供物化后的真源复核 |
| `as_of` | datetime \| None | valid-time 回溯 |
| `time_from` | datetime \| None | event-time 下界 |
| `time_to` | datetime \| None | event-time 上界 |
| `channels` | list[RecallChannel] | 建议启用的通道 |
| `include_archived` | bool | 当前态真源复核是否允许 archived |
| `extensions` | dict[str, str] | 透传配置 |

### 结果结构

| 类型 | 关键字段 |
|------|----------|
| `ScoredUnit` | unit_id / score / channel / evidence: list[ChannelEvidence] |
| `ChannelEvidence` | channel / rank / score / weight / contribution |
| `RetrievedItem` | unit_id / score / content / level: DisclosureLevel |
| `TrajectoryStep` | stage / channel / candidate_count / cost_ms / detail |
| `ScoredMemoryUnit` | unit: MemoryUnit / score / channel / evidence |
| `ChannelError` | channel / source / error_type / message |
| `RetrievalResult` | items / trajectory / errors: list[ChannelError] |

### 枚举

| 枚举 | 值 |
|------|------|
| `DisclosureLevel` | L0 / L1 / L2 / ADAPTIVE |
| `RecallChannel` | DOCUMENT / KEYWORD / VECTOR / GRAPH / TEMPORAL |

## 实现注册机制

```
src/retrieval/<算子>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`QueryParserProducer` / `RecallerProducer` / `FuserProducer` / `DiscloserProducer` / `RetrieverProducer`。
注册由 `retrieval.bootstrap.register_operators` 统一触发。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | MemoryAPI.search → Engine → 本层 Retriever |
| S03-control | Engine.recall 委托本层 Retriever |
| S05-construction | 本层消费构建层产出的索引（向量/全文/图） |
| S06-storage | Retriever 经 StorageProducer 获取统一 Storage；现有 Recaller 作为 CompositeStorage 的兼容检索适配器 |
| S07-common | 复用 Tokenizer/Embedder/FeatureExtractor/LLM/Reranker |
| S08-config | 能力开关与 rerank/embedder 晚绑定经 ConfigSource |
| architecture.md §8 | 检索链路设计 |
