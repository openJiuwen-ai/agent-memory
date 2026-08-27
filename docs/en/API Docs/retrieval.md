# Retrieval Layer API

The Retrieval layer divides retrieval into five pluggable operator types:

- `QueryParser`: parses an external query into a structured representation.
- `Recaller`: recalls candidates within one logical channel.
- `Fuser`: merges and ranks candidates from multiple recall sources.
- `Discloser`: shapes ranked memories into L0/L1/L2 content.
- `Retriever`: provides the unified retrieval entry point for callers.

This document is an API reference for the current abstract interfaces and public data types. The following source files are authoritative:

- [`base.py`](../../../jiuwen_memory/retrieval/base.py)
- [`query_parser.py`](../../../jiuwen_memory/retrieval/query_parser.py)
- [`recaller.py`](../../../jiuwen_memory/retrieval/recaller.py)
- [`fuser.py`](../../../jiuwen_memory/retrieval/fuser.py)
- [`discloser.py`](../../../jiuwen_memory/retrieval/discloser.py)
- [`retriever.py`](../../../jiuwen_memory/retrieval/retriever.py)
- [`types.py`](../../../jiuwen_memory/retrieval/types.py)

## 1. Public Entry Point

```python
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult

result: RetrievalResult = retriever.retrieve(
    scope,
    RetrievalQuery(text="Which database did the user mention earlier?", top_k=5),
)
```

`scope` is the explicit isolation axis and specifies where to search. `RetrievalQuery` specifies what to search for. They are always passed separately; Scope dimensions must not be placed in `filters`.

## 2. RetrievalOperator Base Class

```python
from jiuwen_memory.retrieval.base import RetrievalOperator, RetrievalOperatorType
```

Every retrieval operator inherits `RetrievalOperator` and implements:

| API | Return value | Description |
|---|---|---|
| `operator_type()` | `RetrievalOperatorType` | Returns the operator type |
| `health()` | `None` | Returns `None` when healthy and raises an exception on failure |

`RetrievalOperatorType` contains `QUERY_PARSER`, `RECALLER`, `FUSER`, `DISCLOSER`, and `RETRIEVER`.

## 3. RetrievalQuery Request Type

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

| Field | Description |
|---|---|
| `text` | Original natural-language query |
| `filters` | Hard user-metadata predicate outside the Scope dimensions; normalized to `FilterExpr` when the object is created |
| `as_of` | A valid-time point for historical lookup; `None` means the current state |
| `top_k` | Maximum number of final results; must be greater than `0` |
| `disclosure` | Primary disclosure level: `L0`, `L1`, `L2`, or `ADAPTIVE` |
| `max_tokens` | Token budget for adaptive disclosure; if provided, it must be greater than `0` |
| `with_trajectory` | Whether to include the retrieval trajectory in the result |
| `channels` | Per-call recall-channel override. `None` uses parser recommendations; if the parser makes no recommendation, Storage selects from assembled sources. An empty list is invalid |
| `rerank` | Per-call reranking override; `None` uses the assembly default |
| `include_archived` | Whether an as-current query may include archived memories as candidates |
| `extensions` | Custom options passed through to `ParsedQuery.extensions`; the kernel does not interpret their keys |

`as_of` asks what was visible at a particular system valid-time point. `ParsedQuery.time_from/time_to` describe the time range in which an event in the content occurred. These are independent time axes.

## 4. QueryParser API

```python
from jiuwen_memory.retrieval.query_parser import QueryParser

parsed = parser.parse(query)
```

### `parse(query: RetrievalQuery) -> ParsedQuery`

Converts an external request into a structured query that recall channels can consume directly. An implementation may perform sanitization, rewriting, tokenization, keyword/entity extraction, vectorization, and time parsing.

The main `ParsedQuery` fields are:

| Field | Meaning |
|---|---|
| `raw` | Normalized query entering the retrieval pipeline |
| `rewritten` | Rewritten query |
| `intent` | Identified intent |
| `tokens` / `keywords` | Tokens and keywords |
| `entities` | Entities consumed by graph recall and similar channels |
| `vector` | Query vector |
| `scalar_filters` | Hard predicates pushed down to storage |
| `recheck_filters` | User predicates used for source-of-truth rechecking |
| `as_of` | Valid-time lookup point |
| `time_from` / `time_to` | Event-time window |
| `channels` | Logical recall channels recommended by the parser |
| `include_archived` | Whether archived memories are included |
| `extensions` | Custom pass-through options |

The query-side Tokenizer, Embedder, and FeatureExtractor should use the same instances or compatible configuration as index construction. Otherwise, the vocabulary, vector space, or feature semantics may diverge.

## 5. Recaller API

```python
from jiuwen_memory.retrieval.recaller import Recaller
```

### `channel() -> RecallChannel`

Returns the logical recall channel represented by the current Recaller. `RecallChannel` contains:

- `DOCUMENT`: document lookup.
- `KEYWORD`: keyword/full-text recall.
- `VECTOR`: vector recall.
- `GRAPH`: graph-traversal recall.
- `TEMPORAL`: temporal recall or time constraints.

L0/L1/L2 are different physical index sources within the same logical channel. They do not introduce additional `RecallChannel` enum values.

### `recall(scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]`

Recalls the top-k candidates for this channel within the specified Scope. Each returned `ScoredUnit` contains:

| Field | Description |
|---|---|
| `unit_id` | MemoryUnit ID within the Scope |
| `score` | Recall score from this channel |
| `channel` | Logical channel that produced the hit |
| `evidence` | Optional list of channel evidence |

A Recaller uses `ParsedQuery` to assemble the low-level Store Query. It must pass `scope` as an independent Store method argument and use `query.scalar_filters` as the hard metadata predicate.

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

`candidates` is a two-dimensional structure with one list per physical recall source. `ScoredCandidate` may be an unmaterialized `ScoredUnit` or a materialized `ScoredMemoryUnit`. The current production Retriever completes source-of-truth point reads and rechecking before the Fuser, so it passes `ScoredMemoryUnit` objects.

The Fuser is responsible for candidate deduplication, evidence merging, score fusion, and ordering. The shared `Reranker` is a later, independent stage orchestrated by the Retriever and is not executed inside `Fuser.fuse()`.

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

| Parameter | Description |
|---|---|
| `query` | Supplies the rewritten query, keywords, and other content-selection signals |
| `candidates` | Final candidate order after reading, filtering, fusion, and optional reranking |
| `units` | `unit_id -> MemoryUnit` content lookup table |
| `level` | Primary disclosure level for this request |
| `max_tokens` | Budget input for `ADAPTIVE` mode |

`Discloser` performs content shaping only. It does not perform Store point reads, filtering, fusion, or reranking.

`DisclosureLevel` semantics:

| Enum value | Primary content |
|---|---|
| `L0` | Abstract |
| `L1` | Relevant excerpt/overview |
| `L2` | Full content |
| `ADAPTIVE` | Selects the actual level according to `max_tokens` |

## 8. Retriever API

```python
from jiuwen_memory.retrieval.retriever import Retriever
```

### `retrieve(scope: Scope, query: RetrievalQuery) -> RetrievalResult`

Executes complete retrieval within a Scope and returns the final result. The current standard `PipelineRetriever` follows this main sequence:

```text
RetrievalQuery
  -> QueryParser.parse
  -> Storage recall/materialization path
  -> source-of-truth recheck
  -> Fuser.fuse
  -> candidate-budget truncation
  -> optional Reranker
  -> relevance thresholds and top_k
  -> Discloser.disclose
  -> RetrievalResult
```

The Storage recall path is selected by `Storage.preferred_retrieval_pipeline()`:

- `RECALL_GET_RANK`: recalls IDs only; the Retriever then performs point reads and fusion.
- `RECALL_AND_GET_RANK`: Storage returns materialized candidates; the Retriever performs fusion.
- `RETRIEVE`: Storage performs recall, materialization, and Fuser ranking internally.

Regardless of the selected path, source-of-truth rechecking must be completed before the Fuser, including lifecycle, valid-time, event-time, and the complete `FilterExpr`.

### Boundary Behavior

- If `query.text` is blank, or `ParsedQuery.raw` is blank after parser sanitization, an empty result is returned without calling backend recall.
- The standard `PipelineRetriever` raises `ValidationError` when `top_k <= 0`, `max_tokens <= 0`, or `channels=[]`.
- If some recall sources fail, successful `items` and structured `errors` are returned together.
- If every selected source fails, `StorageRetrievalError` is raised.
- `with_trajectory=False` omits only `trajectory`; it does not omit `errors`.

## 9. RetrievalResult Return Type

```python
RetrievalResult(
    items: list[RetrievedItem],
    trajectory: list[TrajectoryStep],
    errors: list[ChannelError],
)
```

### RetrievedItem

| Field | Description |
|---|---|
| `unit_id` | MemoryUnit ID |
| `score` | Score after fusion/reranking |
| `abstract` | L0 abstract |
| `overview` | L1 excerpt/overview |
| `content` | L2 full content |
| `user_metadata` | User metadata returned to the caller |
| `level` | Primary disclosure level for this request |

`abstract`, `overview`, and `content` are populated together in the returned item. `level` indicates the disclosure level primarily requested by the caller.

### TrajectoryStep

| Field | Description |
|---|---|
| `stage` | Stage name, such as `parse`, `recall`, `fuse`, `rerank`, or `disclose` |
| `channel` | Recall channel; may be `None` for non-recall stages |
| `candidate_count` | Number of candidates produced by the step |
| `cost_ms` | Step duration in milliseconds |
| `detail` | Parameters, truncation reasons, or degradation information |

### ChannelError

| Field | Description |
|---|---|
| `channel` | Logical channel of the failed source |
| `source` | Name of the physical recall source |
| `error_type` | Exception type name |
| `message` | Safely processed error summary |

## 10. Candidate and Evidence Types

| Type | Description |
|---|---|
| `ScoredUnit` | Unmaterialized candidate: `unit_id + score + channel + evidence` |
| `ScoredMemoryUnit` | Materialized candidate: `unit + score + channel + evidence`, with a `unit_id` property |
| `ChannelEvidence` | A result's `rank`, `score`, `weight`, and `contribution` within one channel |
| `RecallBatch` | Candidate list from one physical recall source; `source` distinguishes L0/L1/L2 indexes within the same channel |
| `RecallResult` | `batches + errors`, representing partial success across multiple sources |

## 11. Producers and Custom Implementations

| Producer | `TOP_NAME` | Implementation directory |
|---|---|---|
| `QueryParserProducer` | `query_parser` | `query_parser_impl/` |
| `RecallerProducer` | `recaller` | `recaller_impl/` |
| `FuserProducer` | `fuser` | `fuser_impl/` |
| `DiscloserProducer` | `discloser` | `discloser_impl/` |
| `RetrieverProducer` | `retriever` | `retriever_impl/` |

A new implementation registers with `@XxxProducer.register("name")`. `retrieval.bootstrap.register_operators()` imports the implementation modules and triggers registration.

An abstract operator implementation must, at minimum:

1. Implement the operator-specific abstract business methods.
2. Implement `operator_type()` and return the enum value corresponding to the interface.
3. Implement `health()`.
4. For a Recaller, also implement `channel()`.
5. Use shared exception types for expected failures instead of leaking backend-specific exceptions upward.

## 12. Configurable Implementations

### 12.1 Configuration

Retrieval and Storage use the same two-level namespace configuration:

```yaml
fuser:                   # FuserProducer.TOP_NAME
  default:               # Named instance
    target: rrf          # Registered implementation
    params:
      k: 60
```

A string dependency in `params` references a named instance in the corresponding Producer
namespace. A mapping such as `{target: ..., params: ...}` constructs an anonymous instance inline.
Ordinary parameters are read from the current instance's `params` first and fall back to `globals`
when absent.

User configuration overrides a same-named built-in instance, and the instance's `params` mapping is
replaced as a whole rather than deep-merged field by field. When overriding instances such as
`retriever.default` or `query_parser.default`, include every named dependency that is still needed.
In deployment configuration, place these sections under `memory_api:`.

Without user configuration, the default composition is the `simple` QueryParser; keyword, vector,
and graph Recallers including their L0/L1 variants; the `rrf` Fuser; the `truncating` Discloser; and
the `pipeline` Retriever. Whether vector, graph, layered-index, and reranking stages are actually
included is controlled by `vector_enabled`, `graph_enabled`, `layers_index_enabled`, and
`rerank_enabled`.

### 12.2 QueryParser Implementation

| `target` | Implementation class | Function | Dependencies and parameters |
|---|---|---|---|
| `simple` | `SimpleQueryParser` | The only built-in parser. It can perform conservative sanitization, LLM rewriting, Tokenizer tokenization, FeatureExtractor keyword/entity extraction, Embedder vectorization, and rule-based time parsing. | Depends on `tokenizer`, `embedder`, `llm`, and `feature_extractor`; `sanitize_enabled` (default `true`), `sanitize_strip_code` (default `false`); when `vector_enabled=false`, no Embedder is assembled |

```yaml
query_parser:
  default:
    target: simple
    params:
      tokenizer: default
      embedder: default
      # Anonymous inline echo: pass the query through without conversational-model rewriting
      llm: {target: echo}
      feature_extractor: default
      sanitize_enabled: true
      sanitize_strip_code: false
```

The built-in default target is `simple`. Its default LLM target is `echo`, so it does not rewrite
the query. If another LLM is referenced, `simple` uses the result of `llm.generate(text)` directly
as `rewritten`.

### 12.3 Recaller Implementations

| `target` | Implementation class | Channel/layer | Function | Main dependencies and parameters |
|---|---|---|---|---|
| `keyword` | `KeywordRecaller` | `KEYWORD` / L2 | Queries the `storage.fulltext` index and aggregates index records into MemoryUnits; entity-association expansion is available when the entity path is enabled. | `storage`; optional `entity_store`; `entity_enabled` (default `false`) |
| `keyword_l0` | `KeywordRecaller` | `KEYWORD` / L0 | Queries the summary index in `storage.fulltext_port("layers_l0")`. | `storage`; controlled by the global `layers_index_enabled` switch |
| `keyword_l1` | `KeywordRecaller` | `KEYWORD` / L1 | Queries the fragment index in `storage.fulltext_port("layers_l1")`. | `storage`; controlled by the global `layers_index_enabled` switch |
| `vector` | `VectorRecaller` | `VECTOR` / L2 | Runs ANN over content chunks, aggregates them back into MemoryUnits through metadata, and applies MaxP across chunks belonging to the same unit. | `storage`; `min_similarity` (default `0.0`, which disables the pre-recall threshold) |
| `vector_l0` | `VectorRecaller` | `VECTOR` / L0 | Queries summary vectors in `storage.vector_port("layers_l0")`. | `storage`; controlled by the global `layers_index_enabled` switch |
| `vector_l1` | `VectorRecaller` | `VECTOR` / L1 | Queries fragment vectors in `storage.vector_port("layers_l1")`. | `storage`; controlled by the global `layers_index_enabled` switch |
| `graph` | `GraphRecaller` | `GRAPH` / L2 | Finds seed nodes from keyword and entity text, then performs multi-hop graph traversal. | `storage`; `depth` (default `1`) |

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

If `keyword_l0/l1` or `vector_l0/l1` cannot find the corresponding named Store port, it returns an
empty recall result without affecting other channels. `RecallChannel.DOCUMENT` and
`RecallChannel.TEMPORAL` are defined in the public enum, but no corresponding built-in
`RecallerProducer` targets currently exist.

### 12.4 Fuser Implementations

| `target` | Implementation class | Function | Main parameters |
|---|---|---|---|
| `rrf` | `RRFFuser` | Default Reciprocal Rank Fusion. Each route contributes `1/(k+rank+1)` by rank, and contributions are accumulated across routes without depending on raw score scales. | `k` (default `60`) |
| `weighted_rrf` | `WeightedRRFFuser` | Multiplies each RRF contribution by the logical channel weight. | `fusion_rrf_k` (default `60`), `fusion_channel_weights` (default `{}`, giving each channel weight `1.0`) |
| `score_max` | `ScoreMaxFuser` | Normalizes scores by the highest score within each channel, then takes the cross-channel maximum for each unit (CombMAX). | `fusion_channel_weights` (default `{}`) |

All three Fusers first merge L0/L1/L2 sources within the same logical channel by unit_id using MaxP,
then perform cross-channel fusion.

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

When switching to unweighted RRF, use `params.k`, not `fusion_rrf_k`:

```yaml
fuser:
  default: {target: rrf, params: {k: 80}}
```

### 12.5 Discloser Implementations

| `target` | Implementation class | Function | Configuration |
|---|---|---|---|
| `truncating` | `TruncatingDiscloser` | Default stateless implementation. It prefers pre-generated `unit.layers.l0/l1`; when absent, L0 truncates to 80 characters and L1 takes a 240-character window around the first keyword. | No `params`; `ADAPTIVE` currently fixes L0 as the primary level and does not consume `max_tokens` |
| `structured` | `StructuredDiscloser` | Produces stable line-oriented L0 memory cards, L1 evidence fragments, and L2 full text for agents, preferring pre-generated layers. | No `params`; `ADAPTIVE` upgrades the disclosure level item by item according to `max_tokens` |

```yaml
discloser:
  default: structured
```

### 12.6 Retriever Implementations

| `target` | Implementation class | Function | Main parameters |
|---|---|---|---|
| `pipeline` | `PipelineRetriever` | Default retrieval orchestration. According to the Storage-preferred path, it performs query parsing, multi-route recall/materialization, source-of-truth verification, Fuser processing, optional Reranker processing, thresholding, and disclosure. | Depends on `storage`, `query_parser`, `fuser`, `discloser`, `reranker`, and the `*_recaller` instances; switches: `vector_enabled`, `graph_enabled`, `layers_index_enabled`, and `rerank_enabled`; see the tuning table below |
| `multimodal` | `MultimodalRetriever` | Runs native, CLM, and ELM filtering branches in parallel over a base Retriever, then merges them with RRF. | `base_retriever` (default is an anonymous `pipeline`), `clip_top_k` (default `10`), `event_top_k` (default `10`), `rrf_k` (default `60`) |

Numeric tuning parameters for `pipeline`:

| Parameter | Default | Function |
|---|---:|---|
| `over_fetch_factor` | `4` | Multiplier from `top_k` to the recall count for each route |
| `over_fetch_floor` | `60` | Lower bound for each route's recall count |
| `recall_max` | `100` | Hard upper bound for each route's recall count; `<=0` means unlimited |
| `rerank_max` | `60` | Verification/reranking candidate budget, never lower than `top_k` |
| `min_score` | `0.0` | Absolute threshold for calibrated-score paths; `0` disables it |
| `min_score_ratio` | `0.0` | Ratio threshold relative to the highest calibrated score; `0` disables it |
| `min_score_ratio_uncalibrated` | `0.0` | Ratio threshold for uncalibrated fusion scores; `0` disables it |
| `min_results` | `0` | Minimum result count refilled from positive-score candidates after thresholding; `0` disables it |

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

`multimodal` does not perform video understanding or build CLM/ELM data itself. It only dispatches
to the base Retriever according to `system_metadata.modal_type` and
`system_metadata.memory_level`. A corresponding multimodal write path and metadata must already
exist.

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

## 13. Complete Usage Example

```python
from jiuwen_memory.common.type_def import RecallChannel, Scope
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import DisclosureLevel, RetrievalQuery


def search_memory(retriever: Retriever, scope: Scope) -> list[str]:
    result = retriever.retrieve(
        scope,
        RetrievalQuery(
            text="What retrieval approach did we discuss last time?",
            top_k=5,
            channels=[RecallChannel.KEYWORD, RecallChannel.VECTOR],
            disclosure=DisclosureLevel.L1,
            with_trajectory=True,
        ),
    )
    return [item.overview for item in result.items]
```

Application code should prefer `Retriever.retrieve()` rather than assembling QueryParser, Recaller, Fuser, and Discloser directly. Direct composition is intended primarily for implementing a new retrieval pipeline, replacing an algorithm, or performing component-level tests.
