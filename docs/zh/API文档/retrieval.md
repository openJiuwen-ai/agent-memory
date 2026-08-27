# Retrieval 层 API

Retrieval 层将检索拆分为五类可插拔算子：

- `QueryParser`：把外部查询解析为结构化表示。
- `Recaller`：在单个逻辑通道内召回候选。
- `Fuser`：融合多个召回入口的候选并排序。
- `Discloser`：将已确定顺序的记忆塑形为 L0/L1/L2 内容。
- `Retriever`：面向调用方的统一检索入口。

本文是当前抽象接口和对外数据类型的 API 参考。以下源码是最终依据：

- [`base.py`](../../../jiuwen_memory/retrieval/base.py)
- [`query_parser.py`](../../../jiuwen_memory/retrieval/query_parser.py)
- [`recaller.py`](../../../jiuwen_memory/retrieval/recaller.py)
- [`fuser.py`](../../../jiuwen_memory/retrieval/fuser.py)
- [`discloser.py`](../../../jiuwen_memory/retrieval/discloser.py)
- [`retriever.py`](../../../jiuwen_memory/retrieval/retriever.py)
- [`types.py`](../../../jiuwen_memory/retrieval/types.py)

## 1. 公共调用入口

```python
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult

result: RetrievalResult = retriever.retrieve(
    scope,
    RetrievalQuery(text="用户之前提到的数据库是什么？", top_k=5),
)
```

`scope` 是显式的隔离轴，表示“在哪个范围内找”；`RetrievalQuery` 表示“找什么”。两者始终分开传递，不应把 Scope 维度放入 `filters`。

## 2. RetrievalOperator 基类

```python
from jiuwen_memory.retrieval.base import RetrievalOperator, RetrievalOperatorType
```

所有检索算子都继承 `RetrievalOperator`，并实现：

| API | 返回值 | 说明 |
|---|---|---|
| `operator_type()` | `RetrievalOperatorType` | 返回算子类型 |
| `health()` | `None` | 健康时返回 `None`，失败时抛异常 |

`RetrievalOperatorType` 包含 `QUERY_PARSER`、`RECALLER`、`FUSER`、`DISCLOSER`、`RETRIEVER`。

## 3. RetrievalQuery 请求类型

```python
@dataclass
class RetrievalQuery:
    text: str = ""
    filters: FilterExpr | None = None
    as_of: datetime | None = None
    top_k: int = 10
    disclosure: DisclosureLevel = DisclosureLevel.L0
    max_tokens: int | None = None
    with_trajectory: bool = False
    channels: list[RecallChannel] | None = None
    rerank: bool | None = None
    include_archived: bool = False
    extensions: dict[str, Any] = field(default_factory=dict)
```

| 字段 | 说明 |
|---|---|
| `text` | 原始自然语言查询 |
| `filters` | Scope 之外的用户元数据硬过滤谓词；进入对象时归一化为 `FilterExpr` |
| `as_of` | valid-time 回溯时间点；`None` 表示查询当前状态 |
| `top_k` | 最终返回条数，必须大于 `0` |
| `disclosure` | 主披露层级：`L0`、`L1`、`L2` 或 `ADAPTIVE` |
| `max_tokens` | 自适应披露的 token 预算；传入时必须大于 `0` |
| `with_trajectory` | 是否在结果中返回检索轨迹 |
| `channels` | 调用级召回通道覆盖；`None` 使用 parser 建议，parser 未建议时再交由 Storage 选择已配置入口；空列表非法 |
| `rerank` | 调用级精排开关；`None` 使用装配默认 |
| `include_archived` | 当前态查询是否允许 archived 记忆进入候选 |
| `extensions` | 透传给 `ParsedQuery.extensions` 的自定义选项；内核不解释其 key |

`as_of` 表示“在某个系统有效时间点看到什么”。`ParsedQuery.time_from/time_to` 则表示内容中事件发生的时间范围，两者是独立时间轴。

## 4. QueryParser API

```python
from jiuwen_memory.retrieval.query_parser import QueryParser

parsed = parser.parse(query)
```

### `parse(query: RetrievalQuery) -> ParsedQuery`

将外部请求转换为各召回通道可直接消费的结构化查询。实现可以进行去噪、改写、分词、关键词/实体抽取、向量化和时间解析。

`ParsedQuery` 的主要字段：

| 字段 | 含义 |
|---|---|
| `raw` | 进入检索链路的规范化查询 |
| `rewritten` | 改写后查询 |
| `intent` | 识别出的意图 |
| `tokens` / `keywords` | 分词结果和关键词 |
| `entities` | 图召回等通道消费的实体 |
| `vector` | 查询向量 |
| `scalar_filters` | 下推给存储的硬过滤谓词 |
| `recheck_filters` | 真源复核使用的用户过滤谓词 |
| `as_of` | valid-time 回溯点 |
| `time_from` / `time_to` | event-time 时间窗 |
| `channels` | parser 建议的逻辑召回通道 |
| `include_archived` | 是否包含 archived 记忆 |
| `extensions` | 自定义透传选项 |

查询侧的 Tokenizer、Embedder 和 FeatureExtractor 应与构建索引时使用同一套实例或兼容配置，否则可能出现词表、向量空间或特征口径不一致。

## 5. Recaller API

```python
from jiuwen_memory.retrieval.recaller import Recaller
```

### `channel() -> RecallChannel`

返回当前 Recaller 所属的逻辑召回通道。`RecallChannel` 包含：

- `DOCUMENT`：文档定位。
- `KEYWORD`：关键词/全文召回。
- `VECTOR`：向量召回。
- `GRAPH`：图遍历召回。
- `TEMPORAL`：时序召回或时间约束。

L0/L1/L2 是同一逻辑通道的不同物理索引入口，不会新增 `RecallChannel` 枚举值。

### `recall(scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]`

在指定 Scope 内召回本通道的 top-k 候选。返回的 `ScoredUnit` 包含：

| 字段 | 说明 |
|---|---|
| `unit_id` | Scope 内的 MemoryUnit ID |
| `score` | 本通道的召回分数 |
| `channel` | 命中的逻辑通道 |
| `evidence` | 可选通道证据列表 |

Recaller 负责用 `ParsedQuery` 组装底层 Store Query，必须把 `scope` 作为 Store 方法的独立参数，把 `query.scalar_filters` 作为元数据硬过滤。

## 6. Fuser API

```python
from jiuwen_memory.retrieval.fuser import Fuser
```

### `fuse(query, candidates) -> list[ScoredCandidate]`

```python
fuse(
    query: ParsedQuery,
    candidates: list[list[ScoredCandidate]],
) -> list[ScoredCandidate]
```

`candidates` 是“每个物理召回入口一个列表”的二维结构。`ScoredCandidate` 可为未物化的 `ScoredUnit` 或已物化的 `ScoredMemoryUnit`；当前生产 Retriever 在 Fuser 前会先完成真源点读和复核，因此传入的是 `ScoredMemoryUnit`。

Fuser 的职责是候选去重、证据合并、分数融合和排序。共享 `Reranker` 属于 Retriever 编排的后续独立阶段，不在 `Fuser.fuse()` 内执行。

## 7. Discloser API

```python
from jiuwen_memory.retrieval.discloser import Discloser
```

### `disclose(...) -> list[RetrievedItem]`

```python
disclose(
    query: ParsedQuery,
    candidates: list[ScoredCandidate],
    units: dict[str, MemoryUnit],
    level: DisclosureLevel,
    max_tokens: int | None = None,
) -> list[RetrievedItem]
```

| 参数 | 说明 |
|---|---|
| `query` | 提供改写后查询、关键词等内容选取信号 |
| `candidates` | 已完成读取、过滤、融合和可选精排的最终候选顺序 |
| `units` | `unit_id -> MemoryUnit` 内容查找表 |
| `level` | 本次披露的主层级 |
| `max_tokens` | `ADAPTIVE` 模式下的预算依据 |

`Discloser` 只负责内容塑形，不执行 Store 点读、过滤、融合或精排。

`DisclosureLevel` 语义：

| 枚举值 | 主要内容 |
|---|---|
| `L0` | 摘要 |
| `L1` | 相关片段/概览 |
| `L2` | 全文 |
| `ADAPTIVE` | 根据 `max_tokens` 选择实际层级 |

## 8. Retriever API

```python
from jiuwen_memory.retrieval.retriever import Retriever
```

### `retrieve(scope: Scope, query: RetrievalQuery) -> RetrievalResult`

在 Scope 内执行完整检索并返回最终结果。当前标准 `PipelineRetriever` 的主要顺序是：

```text
RetrievalQuery
  -> QueryParser.parse
  -> Storage 召回/物化路径
  -> 真源复核
  -> Fuser.fuse
  -> 候选预算截断
  -> 可选 Reranker
  -> 相关性阈值与 top_k
  -> Discloser.disclose
  -> RetrievalResult
```

Storage 召回路径由 `Storage.preferred_retrieval_pipeline()` 决定：

- `RECALL_GET_RANK`：只召回 ID，Retriever 再点读和融合。
- `RECALL_AND_GET_RANK`：Storage 返回已物化候选，Retriever 融合。
- `RETRIEVE`：Storage 内完成召回、物化和 Fuser 排序。

不论选择哪条路径，都应在 Fuser 前完成真源复核，包括 lifecycle、valid-time、event-time 和完整 `FilterExpr`。

### 边界行为

- `query.text` 为空白，或 parser 清洗后 `ParsedQuery.raw` 为空白时，返回空结果，不调用后端召回。
- `top_k <= 0`、`max_tokens <= 0` 或 `channels=[]` 时，标准 `PipelineRetriever` 抛 `ValidationError`。
- 部分召回入口失败时，返回成功的 `items` 和结构化 `errors`。
- 所有选中入口都失败时抛 `StorageRetrievalError`。
- `with_trajectory=False` 只会省略 `trajectory`，不会省略 `errors`。

## 9. RetrievalResult 返回类型

```python
RetrievalResult(
    items: list[RetrievedItem],
    trajectory: list[TrajectoryStep],
    errors: list[ChannelError],
)
```

### RetrievedItem

| 字段 | 说明 |
|---|---|
| `unit_id` | MemoryUnit ID |
| `score` | 融合/精排后得分 |
| `abstract` | L0 摘要 |
| `overview` | L1 片段/概览 |
| `content` | L2 全文 |
| `user_metadata` | 返回给调用方的用户元数据 |
| `level` | 本次披露的主层级 |

`abstract`、`overview`、`content` 会在返回项中一次性填充，`level` 用来表示调用方本次主要请求的披露层级。

### TrajectoryStep

| 字段 | 说明 |
|---|---|
| `stage` | 阶段名，如 `parse`、`recall`、`fuse`、`rerank`、`disclose` |
| `channel` | 召回通道；非召回阶段可为 `None` |
| `candidate_count` | 该步产出的候选数 |
| `cost_ms` | 该步耗时，单位毫秒 |
| `detail` | 参数、截断原因或降级信息 |

### ChannelError

| 字段 | 说明 |
|---|---|
| `channel` | 失败入口所属的逻辑通道 |
| `source` | 物理召回入口名称 |
| `error_type` | 异常类型名 |
| `message` | 已安全处理的错误摘要 |

## 10. 候选与证据类型

| 类型 | 说明 |
|---|---|
| `ScoredUnit` | 未物化候选：`unit_id + score + channel + evidence` |
| `ScoredMemoryUnit` | 已物化候选：`unit + score + channel + evidence`，并提供 `unit_id` 属性 |
| `ChannelEvidence` | 单条结果在某通道的 `rank`、`score`、`weight`、`contribution` |
| `RecallBatch` | 单个物理召回入口的候选列表；`source` 可区分同通道下的 L0/L1/L2 索引 |
| `RecallResult` | `batches + errors`，表达多入口的部分成功 |

## 11. Producer 与自定义实现

| Producer | `TOP_NAME` | 实现目录 |
|---|---|---|
| `QueryParserProducer` | `query_parser` | `query_parser_impl/` |
| `RecallerProducer` | `recaller` | `recaller_impl/` |
| `FuserProducer` | `fuser` | `fuser_impl/` |
| `DiscloserProducer` | `discloser` | `discloser_impl/` |
| `RetrieverProducer` | `retriever` | `retriever_impl/` |

新实现使用 `@XxxProducer.register("name")` 注册，由 `retrieval.bootstrap.register_operators()` 触发实现模块导入。

实现抽象算子时至少需要：

1. 实现本算子的业务抽象方法。
2. 实现 `operator_type()` 并返回与接口一致的枚举值。
3. 实现 `health()`。
4. Recaller 额外实现 `channel()`。
5. 对可预期错误使用共享异常类型，不向上泄漏具体后端异常。

## 12. 可配置实现

### 12.1 配置方式

Retrieval 与 Storage 使用相同的两级命名空间配置：

```yaml
fuser:                   # FuserProducer.TOP_NAME
  default:               # 具名实例
    target: rrf          # 已注册实现
    params:
      k: 60
```

`params` 中的字符串依赖是对相应 Producer 命名空间下具名实例的引用，映射 `{target: ..., params: ...}` 则就地构造匿名实例。普通参数优先读当前实例 `params`，缺失时回退到 `globals`。

用户配置会覆盖内置默认中的同名实例，且实例 `params` 是整体替换而不是逐字段深合并。因此覆盖 `retriever.default`、`query_parser.default` 等实例时，应把仍需要的具名依赖一并写回。部署配置中这些段放在 `memory_api:` 下。

不传用户配置时，默认组合为 `simple` QueryParser、keyword/vector/graph 及其 L0/L1 分层 Recaller、`rrf` Fuser、`truncating` Discloser 和 `pipeline` Retriever。是否实际纳入向量、图、分层与精排阶段，由 `vector_enabled`、`graph_enabled`、`layers_index_enabled`、`rerank_enabled` 决定。

### 12.2 QueryParser 实现

| `target` | 实现类 | 功能 | 依赖与参数 |
|---|---|---|---|
| `simple` | `SimpleQueryParser` | 当前唯一内置 parser；可完成保守去噪、LLM 改写、Tokenizer 分词、FeatureExtractor 关键词/实体抽取、Embedder 向量化和规则时间解析 | 依赖 `tokenizer`、`embedder`、`llm`、`feature_extractor`；`sanitize_enabled`（默认 `true`），`sanitize_strip_code`（默认 `false`）；`vector_enabled=false` 时不装配 Embedder |

```yaml
query_parser:
  default:
    target: simple
    params:
      tokenizer: default
      embedder: default
      # 内联匿名 echo：查询原样透传，不调用对话模型改写
      llm: {target: echo}
      feature_extractor: default
      sanitize_enabled: true
      sanitize_strip_code: false
```

内置默认 target 为 `simple`。其默认 LLM target 为 `echo`，因此不改写查询；如果引用其他 LLM，`simple` 会直接使用 `llm.generate(text)` 的结果作为 `rewritten`。

### 12.3 Recaller 实现

| `target` | 实现类 | 通道/层级 | 功能 | 主要依赖与参数 |
|---|---|---|---|---|
| `keyword` | `KeywordRecaller` | `KEYWORD` / L2 | 查询 `storage.fulltext` 全文索引，将索引记录聚合到 MemoryUnit；开启实体链路时可做实体关联扩展 | `storage`；可选 `entity_store`；`entity_enabled`（默认 `false`） |
| `keyword_l0` | `KeywordRecaller` | `KEYWORD` / L0 | 查询 `storage.fulltext_port("layers_l0")` 中的概要索引 | `storage`；受 `layers_index_enabled` 总开关控制 |
| `keyword_l1` | `KeywordRecaller` | `KEYWORD` / L1 | 查询 `storage.fulltext_port("layers_l1")` 中的片段索引 | `storage`；受 `layers_index_enabled` 总开关控制 |
| `vector` | `VectorRecaller` | `VECTOR` / L2 | 对 content chunk 执行 ANN，通过 metadata 聚合回 MemoryUnit，同 unit 多 chunk 取 MaxP | `storage`；`min_similarity`（默认 `0.0`，表示关闭召回前阈值） |
| `vector_l0` | `VectorRecaller` | `VECTOR` / L0 | 查询 `storage.vector_port("layers_l0")` 概要向量 | `storage`；受 `layers_index_enabled` 总开关控制 |
| `vector_l1` | `VectorRecaller` | `VECTOR` / L1 | 查询 `storage.vector_port("layers_l1")` 片段向量 | `storage`；受 `layers_index_enabled` 总开关控制 |
| `graph` | `GraphRecaller` | `GRAPH` / L2 | 以关键词和实体文本定位种子节点，再执行多跳图遍历 | `storage`；`depth`（默认 `1`） |

```yaml
globals:
  vector_enabled: true
  graph_enabled: true
  layers_index_enabled: true
  entity_enabled: false

recaller:
  keyword:
    target: keyword
    params:
      storage: default
  keyword_l0: {target: keyword_l0, params: {storage: default}}
  keyword_l1: {target: keyword_l1, params: {storage: default}}
  vector:
    target: vector
    params:
      storage: default
      min_similarity: 0.0
  vector_l0: {target: vector_l0, params: {storage: default}}
  vector_l1: {target: vector_l1, params: {storage: default}}
  graph:
    target: graph
    params:
      storage: default
      depth: 1
```

`keyword_l0/l1` 或 `vector_l0/l1` 找不到对应具名 Store 端口时会返回空召回，不影响其他通道。`RecallChannel.DOCUMENT` 和 `RecallChannel.TEMPORAL` 已定义在公共枚举中，但当前没有对应的内置 `RecallerProducer` target。

### 12.4 Fuser 实现

| `target` | 实现类 | 功能 | 主要参数 |
|---|---|---|---|
| `rrf` | `RRFFuser` | 默认 Reciprocal Rank Fusion；各路按名次贡献 `1/(k+rank+1)` 并跨路累加，不依赖原始得分量纲 | `k`（默认 `60`） |
| `weighted_rrf` | `WeightedRRFFuser` | 在 RRF 贡献上乘以逻辑通道权重 | `fusion_rrf_k`（默认 `60`）、`fusion_channel_weights`（默认 `{}`，各通道权重 `1.0`） |
| `score_max` | `ScoreMaxFuser` | 每个通道内按当次最高分归一化，再对同一 unit 取跨通道最大值（CombMAX） | `fusion_channel_weights`（默认 `{}`） |

三种 Fuser 都会先将同一逻辑通道的 L0/L1/L2 入口按 unit_id 做 MaxP 归并，再执行跨通道融合。

```yaml
fuser:
  default:
    target: weighted_rrf
    params:
      fusion_rrf_k: 60
      fusion_channel_weights:
        keyword: 1.0
        vector: 2.0
        graph: 0.5
```

切换为无权重 RRF 时要使用 `params.k`，不是 `fusion_rrf_k`：

```yaml
fuser:
  default: {target: rrf, params: {k: 80}}
```

### 12.5 Discloser 实现

| `target` | 实现类 | 功能 | 配置 |
|---|---|---|---|
| `truncating` | `TruncatingDiscloser` | 默认无状态实现；优先使用预生成 `unit.layers.l0/l1`，缺失时 L0 截断到 80 字符、L1 围绕首个关键词取 240 字符窗口 | 无 `params`；`ADAPTIVE` 当前固定以 L0 作为主层级，不消费 `max_tokens` |
| `structured` | `StructuredDiscloser` | 面向 Agent 生成稳定行格式的 L0 记忆卡、L1 证据片段和 L2 全文；优先使用预生成 layers | 无 `params`；`ADAPTIVE` 会根据 `max_tokens` 逐条升级披露层级 |

```yaml
discloser:
  default: structured
```

### 12.6 Retriever 实现

| `target` | 实现类 | 功能 | 主要参数 |
|---|---|---|---|
| `pipeline` | `PipelineRetriever` | 默认检索编排；根据 Storage 首选路径执行查询解析、多路召回/物化、真源复核、Fuser、可选 Reranker、阈值和 Discloser | 依赖 `storage`、`query_parser`、`fuser`、`discloser`、`reranker`与各 `*_recaller`；开关 `vector_enabled`、`graph_enabled`、`layers_index_enabled`、`rerank_enabled`；调参见下表 |
| `multimodal` | `MultimodalRetriever` | 在基础 Retriever 上并行执行 native、CLM 和 ELM 三个过滤分支，最后用 RRF 合并 | `base_retriever`（默认匿名 `pipeline`）、`clip_top_k`（默认 `10`）、`event_top_k`（默认 `10`）、`rrf_k`（默认 `60`） |

`pipeline` 的数值调参：

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `over_fetch_factor` | `4` | 每路召回数相对 `top_k` 的放大倍数 |
| `over_fetch_floor` | `60` | 每路召回数下限 |
| `recall_max` | `100` | 每路召回数硬上限；`<=0` 表示不封顶 |
| `rerank_max` | `60` | 复核/精排候选预算，实际不小于 `top_k` |
| `min_score` | `0.0` | 校准分数路径的绝对阈值；`0` 关闭 |
| `min_score_ratio` | `0.0` | 校准分数相对最高分的比例阈值；`0` 关闭 |
| `min_score_ratio_uncalibrated` | `0.0` | 未校准融合分数的比例阈值；`0` 关闭 |
| `min_results` | `0` | 阈值裁剪后从正分候选回填的最小结果数；`0` 关闭 |

```yaml
globals:
  vector_enabled: true
  graph_enabled: false
  layers_index_enabled: true
  rerank_enabled: true

retriever:
  default:
    target: pipeline
    params:
      storage: default
      query_parser: default
      fuser: default
      discloser: default
      reranker: default
      keyword_recaller: keyword
      keyword_l0_recaller: keyword_l0
      keyword_l1_recaller: keyword_l1
      vector_recaller: vector
      vector_l0_recaller: vector_l0
      vector_l1_recaller: vector_l1
      graph_recaller: graph
      over_fetch_factor: 4
      over_fetch_floor: 60
      recall_max: 100
      rerank_max: 60
      min_score: 0.0
      min_score_ratio: 0.0
      min_score_ratio_uncalibrated: 0.0
      min_results: 0
```

`multimodal` 不会自行完成视频理解或构建 CLM/ELM，它只根据 `system_metadata.modal_type` 和 `system_metadata.memory_level` 分支调用基础 Retriever。使用时需要先有对应的多模态写入链路和元数据。

```yaml
retriever:
  base:
    target: pipeline
    params:
      storage: default
      query_parser: default
      fuser: default
      discloser: default
  default:
    target: multimodal
    params:
      base_retriever: base
      clip_top_k: 10
      event_top_k: 10
      rrf_k: 60
```

## 13. 完整调用示例

```python
from jiuwen_memory.common.type_def import RecallChannel, Scope
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery


def search_memory(retriever: Retriever, scope: Scope) -> list[str]:
    result = retriever.retrieve(
        scope,
        RetrievalQuery(
            text="上次讨论的检索方案是什么？",
            top_k=5,
            channels=[RecallChannel.KEYWORD, RecallChannel.VECTOR],
            disclosure=DisclosureLevel.L1,
            with_trajectory=True,
        ),
    )
    return [item.overview for item in result.items]
```

应用侧优先调用 `Retriever.retrieve()`，而不是自行组合 QueryParser、Recaller、Fuser 和 Discloser。后者主要用于实现新的检索管线、替换算法或进行组件级测试。
