# Construction Layer API

The Construction layer accepts `MemoryUnit` objects and performs classification, information
extraction, abstraction, association, layer annotation, deduplication orchestration, and index
construction. Writes of both memory records and retrieval indexes enter the Storage interface
through `IndexBuilder`.

This document is an API reference for the current abstract interfaces, public types, built-in
implementations, and configuration targets. The following source files are authoritative:

- [`base.py`](../../../jiuwen_memory/construction/base.py)
- [`extractor.py`](../../../jiuwen_memory/construction/extractor.py)
- [`abstractor.py`](../../../jiuwen_memory/construction/abstractor.py)
- [`associator.py`](../../../jiuwen_memory/construction/associator.py)
- [`classifier.py`](../../../jiuwen_memory/construction/classifier.py)
- [`index_builder.py`](../../../jiuwen_memory/construction/index_builder.py)
- [`dedup.py`](../../../jiuwen_memory/construction/dedup.py)
- [`layer_annotator.py`](../../../jiuwen_memory/construction/layer_annotator.py)
- [`evolver.py`](../../../jiuwen_memory/construction/evolver.py)
- [`prompt_registry.py`](../../../jiuwen_memory/construction/prompt_registry.py)
- [`common/type_def/feature.py`](../../../jiuwen_memory/common/type_def/feature.py)
- [`common/errors.py`](../../../jiuwen_memory/common/errors.py)

## 1. Construction-Layer Call Flow

Construction operators are normally orchestrated by the Control layer's `MemoryEngine` and
`Evolver`. Application code should generally use `MemoryAPI` instead of wiring every operator
manually.

```text
MemoryEngine.write
  -> Ingestor produces MemoryUnit objects
  -> optional Classifier.classify
  -> IndexBuilder.build
       -> Storage.add / Store ports

MemoryEngine.evolve / background Job
  -> Evolver.evolve
       -> Extractor / Abstractor / Associator
       -> optional LayerAnnotator
       -> Dedup.recall
       -> IndexBuilder.build / update / remove
```

The Construction layer neither authorizes requests nor performs user-facing retrieval.
Authorization belongs at the API/Control boundary, and normal retrieval belongs to the Retrieval
layer. `Dedup` accesses indexes directly only to make evolution decisions; it does not go through a
Retrieval Recaller.

## 2. ConstructionOperator Base Class

```python
from jiuwen_memory.construction.base import ConstructionOperator, OperatorType
```

Every Construction operator inherits from `ConstructionOperator`:

| API | Return value | Description |
|---|---|---|
| `operator_type()` | `OperatorType` | Returns the operator's self-described type |
| `health()` | `None` | Returns `None` when healthy and raises an exception otherwise |

`OperatorType` currently contains:

- `EXTRACTOR`
- `ABSTRACTOR`
- `ASSOCIATOR`
- `CLASSIFIER`
- `INDEX_BUILDER`
- `EVOLVER`
- `LAYER_ANNOTATOR`

`Dedup` has its own Producer, but there is currently no separate `OperatorType.DEDUP`. Both
built-in Dedup implementations return `EVOLVER` from `operator_type()`.

## 3. Extractor API

```python
from jiuwen_memory.construction.extractor import Extractor

derived = extractor.extract(units, context=context)
```

### `extract(units, *, context=None) -> list[MemoryUnit]`

Extracts zero or more low-abstraction derived memories from the original `MemoryUnit` objects in
the current request. Derived units should refer back to their sources through `provenance`.

`context` has type `ExtractContext | None`:

```python
@dataclass
class ExtractContext:
    recent_originals: list[MemoryUnit]
    related_memories: list[MemoryUnit]
```

| Field | Purpose |
|---|---|
| `recent_originals` | Recent original infer input, used only for coreference resolution and contextual enrichment; it is neither deduplicated nor treated as an extraction source |
| `related_memories` | Recalled derived memories that expose existing facts and help deduplication |

Only `units` are extraction sources. Neither context collection should appear in the new unit's
`provenance`.

## 4. Abstractor API

```python
from jiuwen_memory.construction.abstractor import Abstractor

abstracted = abstractor.abstract(units)
```

### `abstract(units: list[MemoryUnit]) -> list[MemoryUnit]`

Summarizes low- or medium-abstraction memories into higher-abstraction memories such as profiles,
long-term preferences, patterns, or skills. Output must retain source `provenance` so it can be
rebuilt and traced.

## 5. Associator API

```python
from jiuwen_memory.construction.associator import Associator

relations = associator.associate(units)
```

### `associate(units: list[MemoryUnit]) -> list[Relation]`

Discovers entity coreference, topic associations, causal relationships, or references and returns a
list of `Relation` objects. A `Relation` primarily contains `source_id`, `target_id`, `relation`,
`score`, and `metadata`. The Evolver/index-construction path subsequently writes it to the graph
index.

## 6. Classifier API

```python
from jiuwen_memory.construction.classifier import Classifier

classified = classifier.classify(units)
```

### `classify(units: list[MemoryUnit]) -> list[MemoryUnit]`

Sets classification information such as `tier`, topic tags, and importance for a batch of memories,
then returns the updated units. The standard Engine currently calls Classifier only for the direct
`infer=false` write path. For `infer=true`, the Extractor produces already-classified derived units.

## 7. IndexBuilder API

```python
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
```

`IndexBuilder` is the Construction layer's unified write entry point. The standard
`InMemoryEngine` and `CloudEngine` do not call `Storage.add/update/delete` before invoking the
builder. An `IndexBuilder` implementation is therefore responsible for delivering the entire
operation to its configured Storage/Store targets.

### 7.1 Write Scope

| `IndexWriteMode` | Semantics |
|---|---|
| `ALL` | Writes the memory record and every enabled retrieval index |
| `FORWARD_ONLY` | Writes back only the memory record without changing retrieval indexes; used for lifecycle state updates |
| `RETRIEVAL_ONLY` | Maintains only retrieval indexes without changing the memory record; used for index backfills or migrations |

### 7.2 Removal Scope

| `IndexRemoveMode` | Semantics |
|---|---|
| `HARD` | Physically deletes the memory record and retrieval indexes |
| `SOFT` | Removes only retrieval indexes; the record remains available through `get/list` |

### 7.3 Methods

| API | Return value | Description |
|---|---|---|
| `build(units, *, mode=ALL)` | `None` | Creates a batch of memories and their indexes |
| `update(units, *, mode=ALL)` | `None` | Incrementally updates memories and their indexes |
| `remove(units, *, mode=HARD)` | `None` | Idempotently removes units using the Scope carried by each unit |
| `rebuild()` | `None` | Rebuilds derived indexes from the source of truth; an implementation may currently be a no-op |

`FORWARD_ONLY`, `RETRIEVAL_ONLY`, and `SOFT` are interface semantics. Whether they can actually be
executed independently depends on the specific `IndexBuilder` and Storage implementations.

### 7.4 Responsibilities of Built-in Builders

| `target` | Write responsibility |
|---|---|
| `forward` | Delivers only the memory record through Storage's KV forward-index port |
| `fulltext` | Builds only full-text and L0/L1 full-text indexes; does not deliver the memory record |
| `vector` | Chunks and embeds content and builds content plus L0/L1 vector indexes; does not deliver the memory record |
| `hybrid` | Default orchestrator; combines forward, fulltext, vector, and optional entity sub-builders in sequence |
| `unified` | Groups by Scope and delegates `build/update/remove` directly to `Storage.add/update/delete` |

When selected independently, `forward`, `fulltext`, and `vector` are responsible only for the side
listed in the table. A normal complete write should use `hybrid`, or a `unified + Storage`
combination in which Storage itself completely implements the write semantics.

`hybrid.rebuild()` and `unified.rebuild()` both currently return `None`; neither provides a real
full-scan rebuild flow yet.

## 8. Dedup API

```python
from jiuwen_memory.construction.dedup import Dedup

similar = dedup.recall(candidate)
```

### `recall(candidate: MemoryUnit) -> list[tuple[MemoryUnit, float]]`

Recalls existing memories similar to one candidate and returns `(MemoryUnit, score)` pairs in
descending score order. Built-in implementations:

1. construct a Vector or Fulltext Store query;
2. load the memory records;
3. remove the candidate itself and non-ACTIVE units;
4. aggregate by unit using the maximum score; and
5. apply `min_similarity`.

Dedup only recalls candidates; it does not decide `ADD/UPDATE/SUPERSEDE/NOOP`. The Evolver makes
that decision and persists it. Deduplication is best effort: built-in implementations return an
empty list instead of blocking evolution when an exception occurs.

## 9. LayerAnnotator API

```python
from jiuwen_memory.construction.layer_annotator import LayerAnnotator

annotated = annotator.annotate(units)
```

### `annotate(units: list[MemoryUnit]) -> list[MemoryUnit]`

Generates `unit.layers.l0` and `unit.layers.l1` for existing units without creating new memory
units. Only units with `len(content) > layers_threshold` are annotated. Short content keeps empty
layers and falls back to generated disclosure during Retrieval.

Built-in implementations are best effort. A single-unit or batch failure leaves layers empty and
does not block write, update, or evolve.

## 10. Evolver API

```python
from jiuwen_memory.construction.evolver import EvolveMode, Evolver, EvolveResult

result = evolver.evolve(units, EvolveMode.EXTRACT)
```

### `evolve(units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult`

Runs the selected memory-content evolution stage:

| `EvolveMode` | Purpose |
|---|---|
| `EXTRACT` | Extracts facts, events, preferences, or procedural memories from original input |
| `ASSOCIATE` | Discovers associations and maintains graph relationships |
| `CONSOLIDATE` | Abstracts, merges, or resolves conflicts |
| `FORGET` | Selects low-value or superseded memories, writes back lifecycle state, and removes them from retrieval |

Index maintenance is not a separate EvolveMode; indexes are maintained along with
`IndexBuilder.build/update/remove`.

`EvolveResult` fields:

| Field | Description |
|---|---|
| `created_ids` | IDs of newly created memories |
| `updated_ids` | IDs of memories updated in place |
| `superseded_ids` | IDs of old memories replaced by newer versions |
| `forgotten_ids` | IDs marked as forgotten |

`OrchestratingEvolver` and `DynamicEvolver` are peer targets. `DynamicEvolver` inherits the former
and only replaces `EXTRACT` with `extract -> consolidate (decision) -> reflect -> persist`; it uses
the parent implementation for the other three modes.

## 11. PromptRegistry and Dynamic Prompts

```python
from jiuwen_memory.construction.prompt_registry import PromptRegistry

registry = PromptRegistry.from_dict(prompts)
text = registry.get("extract", "preference")
```

| API | Return value | Description |
|---|---|---|
| `PromptRegistry.from_dict(data, *, config_source=None)` | `PromptRegistry` | Builds a registry from the configuration's `prompts` section and optionally injects a runtime ConfigSource |
| `get(phase, key)` | `str \| None` | Reads runtime `prompts.<phase>.<key>` first, then the construction-time snapshot |
| `has_phase(phase)` | `bool` | Checks only whether the construction-time snapshot contains the phase |

Supported phases are `extract`, `consolidate`, and `reflect`. Call-level metadata uses these keys:

| Key format | Purpose |
|---|---|
| `_extract_prompt_<strategy>` | Names a key under `prompts.extract` |
| `_consolidation_prompt_<strategy>` | Names a key under `prompts.consolidate` |
| `_reflect_prompt_<strategy>` | Names a key under `prompts.reflect` |
| `_extraction_strategy` | Records the extraction strategy actually used by a derived unit |

Metadata stores a prompt key, not the entire prompt text. When the registry does not contain the
referenced key, `DynamicLLMExtractor` treats the metadata value itself as prompt text for backward
compatibility.

## 12. Producers and Configuration Namespaces

| Producer | `TOP_NAME` | Implementation directory |
|---|---|---|
| `ExtractorProducer` | `extractor` | `extractor_impl/` |
| `AbstractorProducer` | `abstractor` | `abstractor_impl/` |
| `AssociatorProducer` | `associator` | `associator_impl/` |
| `ClassifierProducer` | `classifier` | `classifier_impl/` |
| `IndexBuilderProducer` | `constructor` | `index_builder_impl/` |
| `DedupProducer` | `dedup` | `dedup_impl/` |
| `LayerAnnotatorProducer` | `layer_annotator` | `layer_annotator_impl/` |
| `EvolverProducer` | `evolver` | `evolver_impl/` |

A new implementation registers through `@XxxProducer.register("target")`.
`construction.bootstrap.register_constructors()` imports implementation modules and triggers their
registration.

Configuration uses a two-level namespace:

```yaml
constructor:             # Producer.TOP_NAME
  default:               # named instance
    target: hybrid       # registered name
    params:
      storage: default   # reference to a named instance in another namespace
      chunker: default
      embedder: default
```

User configuration replaces a built-in instance with the same name. Instance `params` are replaced
as a whole rather than deep-merged field by field. When overriding instances such as
`constructor.default` or `evolver.default`, repeat every dependency reference that is still
required. In HTTP/deployment configuration, these sections are nested under `memory_api:`.

## 13. Configurable Implementations

### 13.1 Extractor Implementations

| `target` | Implementation | Function | Dependencies and primary parameters |
|---|---|---|---|
| `keyword` | `KeywordExtractor` | Splits source text with a Chunker and produces SEMANTIC units with lineage; combines procedural content into one procedural memory | `chunker`, default `fixed_window` |
| `llm` | `ExtractorImpl` | Performs structured LLM extraction and validates source, confidence, tier, and tags | `llm`; `extractor_min_confidence`, `extractor_retry_max`, `extractor_retry_backoff`, `extract_batch_size` |
| `dynamic_llm` | `DynamicLLMExtractor` | Extracts once per `_extract_prompt_<strategy>`; delegates to fallback when no strategy is present | `llm`, `fallback`, `prompts`; same parameters as `llm` |
| `video_memory` | `VideoMemoryExtractor` | Converts video normalization results into CLM/ELM multimodal MemoryUnit objects | No configuration dependency; input must contain the expected video metadata |

### 13.2 Abstractor, Associator, and Classifier Implementations

| Namespace | `target` | Implementation | Function | Dependencies and primary parameters |
|---|---|---|---|---|
| `abstractor` | `concat` | `ConcatAbstractor` | Concatenates at least two ACTIVE memories into one CORE profile | None |
| `abstractor` | `llm` | `LLMAbstractor` | Groups input and asks an LLM to produce high-abstraction candidates such as summaries, patterns, and portraits | `llm`, `feature_extractor`; confidence, group-size, batch-size, context-budget, and retry parameters |
| `associator` | `keyword` | `KeywordAssociator` | Produces a `related` relation when the number of shared keywords reaches a threshold | `feature_extractor`; the current builder uses default `min_overlap=2` |
| `associator` | `llm` | `LLMAssociator` | Uses vector, keyword, and entity discovery with optional deep LLM verification | `llm`, `feature_extractor`, `embedder`; similarity, confirmation-range, batch-size, and retry parameters |
| `classifier` | `keyword` | `KeywordClassifier` | Sets tier and topic tags using keyword heuristics | None |
| `classifier` | `llm` | `LLMClassifier` | Produces tier and tags for a batch in one LLM call | `llm`; `classifier_retry_max`, `classifier_retry_backoff` |

The pure offline default LLM is `echo`, which does not perform genuine structured reasoning. To
obtain meaningful results from the `llm` Classifier, Extractor, Abstractor, or Associator, configure
a real LLM that can satisfy the corresponding JSON contract.

### 13.3 IndexBuilder Implementations

| `target` | Implementation | Dependencies and parameters | Description |
|---|---|---|---|
| `forward` | `ForwardIndexBuilder` | `storage` | Forward memory records only |
| `fulltext` | `FulltextIndexBuilder` | `storage`; `layers_index_enabled` | Full-text and optional L0/L1 full-text indexes |
| `vector` | `VectorIndexBuilder` | `storage`, `chunker`, `embedder`; `layers_index_enabled` | Content-chunk vectors and optional L0/L1 vector indexes |
| `hybrid` | `HybridIndexBuilder` | `storage`, `chunker`, `embedder`; `layers_index_enabled`, `entity_enabled`, optional `entity_store` | Default complete orchestrator |
| `unified` | `UnifiedIndexBuilder` | `storage` | Delegates every CRUD operation and mode unchanged to Storage |

`EntityIndexBuilder` is an internal sub-builder of `hybrid`; it is not registered as an independent
`constructor` target. It is enabled only when `entity_enabled=true` and an `EntityStore` is
assembled successfully. Assembly failure disables the entity path while full-text and vector paths
continue to operate.

### 13.4 Dedup, LayerAnnotator, and Evolver Implementations

| Namespace | `target` | Implementation | Function | Dependencies and primary parameters |
|---|---|---|---|---|
| `dedup` | `vector` | `VectorDedup` | Similarity recall through Embedder + Vector Store | `storage`, `embedder`; `dedup_min_similarity`, `dedup_top_k`, `dedup_tier_filter`, `dedup_scope_filter` |
| `dedup` | `keyword` | `KeywordDedup` | Fulltext Store recall scored by token overlap | `storage`; same parameters as vector |
| `layer_annotator` | `keyword` | `KeywordLayerAnnotator` | Rule-based L0/L1 generation | `layer_annotator_threshold`, `layer_annotator_l1_chars` |
| `layer_annotator` | `llm` | `LLMLayerAnnotator` | Batch LLM generation with strict L0/L1 validation | `llm`; threshold and retry parameters |
| `evolver` | `orchestrating` | `OrchestratingEvolver` | Legacy four-mode flow; EXTRACT couples dedup decisions with persistence | extractor, abstractor, associator, index_builder, storage, message_store, dedup, llm; `params.layer_annotator` can select or disable annotation |
| `evolver` | `dynamic` | `DynamicEvolver` | Dynamic-prompt four-step EXTRACT; inherits other modes from orchestrating | Same dependencies plus `PromptRegistry`; automatically injects `layer_annotator.default` when present |

Both Evolvers use `dedup_medium_similarity` (default `0.7`) and `dedup_high_similarity` (default
`0.9`). When `vector_enabled=false`, unspecified IndexBuilder and Dedup targets switch to `fulltext`
and `keyword`, respectively.

## 14. Default Assembly

Without user configuration, Construction uses these default instances:

| Namespace | Default target | Notes |
|---|---|---|
| `extractor.default` | `dynamic_llm` | Falls back to `extractor.legacy=keyword` when no call-level strategy is supplied |
| `abstractor.default` | `concat` | Rule-based profile combination |
| `associator.default` | `keyword` | Keyword association |
| `classifier.default` | `llm` | Uses shared `llm.default`; offline default is echo |
| `constructor.default` | `hybrid` | storage + chunker + embedder |
| `dedup.default` | `vector` | storage + embedder |
| `evolver.default` | `orchestrating` | Default legacy EXTRACT |
| `evolver.dynamic` | `dynamic` | Declared named instance, but does not automatically replace default |
| `layer_annotator` | Not declared by default | Evolver does not annotate when no named default exists |

## 15. Dynamic Evolution Configuration Example

```yaml
prompts:
  extract:
    preference: "Extract user preferences and return the required JSON"
  consolidate:
    preference: "Decide whether to add, update, supersede, or ignore the candidate"
  reflect:
    preference: "Review and correct the candidate before persistence"

extractor:
  default:
    target: dynamic_llm
    params:
      llm: default
      fallback: legacy
  legacy:
    target: keyword
    params:
      chunker: default

layer_annotator:
  default:
    target: llm
    params:
      llm: default
      layer_annotator_threshold: 512

evolver:
  default:
    target: dynamic
    params:
      extractor: default
      abstractor: default
      associator: default
      index_builder: default
      storage: default
      message_store: default
      dedup: default
      llm: default
      dedup_medium_similarity: 0.7
      dedup_high_similarity: 0.9
```

The current `dynamic` builder looks up `layer_annotator.default` directly and does not read
`evolver.default.params.layer_annotator`. In contrast, `orchestrating` supports selecting a named
instance through that parameter or explicitly disabling annotation with an empty value.

Put prompt keys in system metadata when invoking the flow:

```python
system_metadata = {
    "infer": "true",
    "_extract_prompt_preference": "preference",
    "_consolidation_prompt_preference": "preference",
    "_reflect_prompt_preference": "preference",
}
```

## 16. Requirements for Custom Implementations

A new Construction operator must at least:

1. inherit the corresponding abstract interface and implement its business method;
2. implement `operator_type()` and `health()`;
3. register with the corresponding Producer through `register("target")`;
4. use injected Storage, LLM, Chunker, Embedder, and other dependencies instead of constructing
   backends internally;
5. set the correct `scope` and `provenance` on derived memories;
6. implement `build/update/remove/rebuild` for an IndexBuilder and honor write/removal modes; and
7. preserve best-effort semantics for Dedup and LayerAnnotator so degradable failures do not block
   the primary write flow.

## 17. Method-Level Contracts

This section specifies inputs, outputs, and side effects beyond merely making the methods callable.
Abstract interfaces do not define transactions across operators. When persistence atomicity is
required, it must be provided by the selected IndexBuilder/Storage implementation and backend.

### 17.1 Extractor, Abstractor, Associator, and Classifier

| API | Empty input | Input/output relationship | Persistence side effects |
|---|---|---|---|
| `extract(units, *, context=None)` | Returns an empty list | Returns new derived units; `context` enriches context only and is not a provenance source | None; Evolver/IndexBuilder persists the result |
| `abstract(units)` | Returns an empty list | Returns higher-abstraction derived units; built-in `concat` uses only ACTIVE input and produces nothing for fewer than two units | None |
| `associate(units)` | Returns an empty list | Only discovers `Relation` objects; scores are not guaranteed to share a `[0, 1]` scale because each implementation defines its scale | None; Evolver/IndexBuilder orchestrates graph-index writes |
| `classify(units)` | Returns an empty list | Built-in implementations mutate input units and return those units in the same order while preserving IDs and Scope | None; Engine invokes IndexBuilder afterwards |

`Relation` is a shared data structure:

| Field | Type | Default | Semantics |
|---|---|---|---|
| `source_id` | `str` | `""` | ID of the source memory or entity |
| `target_id` | `str` | `""` | ID of the target memory or entity |
| `relation` | `str` | `""` | Relationship name, such as `related`, `caused_by`, or `refers_to` |
| `score` | `float` | `0.0` | Implementation-defined relevance or confidence score |
| `metadata` | `dict[str, Any]` | `{}` | Relationship evidence and additional attributes |

`FeatureSet` is a shared feature container used by Associator and Extractor. Its fields are
`keywords: list[str]=[]`, `entities: list[Entity]=[]`, and `labels: dict[str, str]={}`. `Entity`
contains `text: str=""`, `type: str=""`, and `score: float=0.0`.

Every unit emitted by a custom Extractor or Abstractor must have a non-empty ID and the correct Scope
and `provenance`. For multi-source output, `user_metadata` may inherit only the intersection of
key-value pairs that are equal across every source.

### 17.2 IndexBuilder

```python
build(units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None
update(units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None
remove(units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD) -> None
rebuild() -> None
```

| Contract | Description |
|---|---|
| Empty batch | Must be a no-op with no side effects |
| Scope | Scope comes from each `MemoryUnit`; built-in builders accept multiple Scopes in one batch and write each unit using its own Scope |
| Order | `HybridIndexBuilder.build` writes the forward record before derived indexes; `remove(HARD)` removes derived indexes before the forward record |
| Atomicity | Built-in hybrid invokes multiple sub-builders sequentially and provides no cross-Store atomicity; a mid-flow failure may leave the source record or partial indexes |
| Retry | `build` preserves create semantics and may raise `ConflictError` for a duplicate ID; `update` may raise `NotFoundError` when the target is absent; `remove` is idempotent |
| Mode support | An implementation must understand all three write modes and both removal modes; an unsupported capability must not silently reverse the requested semantics |
| `rebuild()` | Built-in builders currently have no full-scan implementation; returning `None` does not mean indexes were rebuilt |

`UnifiedIndexBuilder` preserves input order while grouping by the five-part Scope and calls
`Storage.add/update/delete` once per group. `HybridIndexBuilder` orders operations to preserve the
rebuildable memory record first, but does not wrap writes to multiple backends in a transaction.

### 17.3 Dedup, LayerAnnotator, and Evolver

| API | Read/write behavior | Return contract | Failure semantics |
|---|---|---|---|
| `Dedup.recall(candidate)` | Read-only recall | Descending `(MemoryUnit, float)` pairs after filtering the candidate itself and non-ACTIVE units | Built-in implementations swallow backend exceptions and return an empty list |
| `LayerAnnotator.annotate(units)` | Mutates `unit.layers` in place | Returns processed units; short text may retain empty layers | Built-in implementations annotate per unit/batch on a best-effort basis and do not block the main write flow |
| `Evolver.evolve(units, mode)` | May read originals, recall for deduplication, and persist through IndexBuilder | Returns ID categories for completed work | Non-best-effort extraction/write failures propagate; completed multi-Store side effects are not rolled back automatically |

`EvolveResult` field types and defaults:

| Field | Type | Default | Semantics |
|---|---|---|---|
| `created_ids` | `list[str]` | `[]` | IDs successfully created by this call |
| `updated_ids` | `list[str]` | `[]` | IDs successfully updated in place |
| `superseded_ids` | `list[str]` | `[]` | IDs of old memories replaced by newer versions |
| `forgotten_ids` | `list[str]` | `[]` | IDs marked forgotten and removed from retrieval |

`EvolveResult` is a completion result, not a transaction rollback log. If the call raises, the
absence of a result must not be interpreted as proof that no backend write occurred.

## 18. Built-in Parameter Reference

The table lists common parameters read directly by current builders that affect runtime behavior.
Dependency references such as `llm`, `storage`, and `embedder` are resolved according to the
Producer rules above.

| Parameter | Type | Default | Applicable implementation | Purpose/constraint |
|---|---|---:|---|---|
| `extractor_min_confidence` | `float` | `0.5` | `llm` / `dynamic_llm` | Filters low-confidence extraction candidates |
| `extractor_retry_max` | `int` | `3` | `llm` / `dynamic_llm` | Maximum LLM attempts; must be at least `1` |
| `extractor_retry_backoff` | `int` | `1000` | `llm` / `dynamic_llm` | Retry backoff in milliseconds |
| `extract_batch_size` | `int` | `10` | `llm` / `dynamic_llm` | Maximum source units per LLM extraction call |
| `abstractor_min_confidence` | `float` | `0.5` | `abstractor.llm` | Minimum confidence |
| `abstractor_min_group_size_summary` | `int` | `1` | `abstractor.llm` | Minimum summary group size |
| `abstractor_min_group_size_pattern` | `int` | `3` | `abstractor.llm` | Minimum pattern group size |
| `abstractor_min_group_size_portrait` | `int` | `5` | `abstractor.llm` | Minimum portrait group size |
| `abstractor_max_groups_per_batch` | `int` | `4` | `abstractor.llm` | Maximum groups per LLM call |
| `abstractor_max_context_tokens` | `int` | `180000` | `abstractor.llm` | Context-token budget |
| `abstractor_retry_max` | `int` | `3` | `abstractor.llm` | Maximum LLM attempts |
| `abstractor_retry_backoff` | `int` | `1000` | `abstractor.llm` | Retry backoff in milliseconds |
| `associator_similarity_threshold` | `float` | `0.7` | `associator.llm` | Vector-candidate similarity threshold |
| `associator_keyword_jaccard_threshold` | `float` | `0.3` | `associator.llm` | Keyword Jaccard threshold |
| `associator_entity_match_threshold` | `float` | `0.8` | `associator.llm` | Entity-match threshold |
| `associator_min_auto_confirm` | `float` | `0.5` | `associator.llm` | Lower bound of automatic-confirmation range |
| `associator_max_auto_confirm` | `float` | `0.85` | `associator.llm` | Upper bound of automatic-confirmation range |
| `associator_min_final_score` | `float` | `0.5` | `associator.llm` | Minimum final relationship score |
| `associator_deep_discovery` | `bool` | `true` | `associator.llm` | Enables deep LLM discovery |
| `associator_max_pairs_per_llm_call` | `int` | `10` | `associator.llm` | Maximum candidate pairs per LLM call |
| `associator_ann_threshold` | `int` | `50` | `associator.llm` | Unit-count threshold for switching to ANN discovery |
| `associator_max_units_per_associate` | `int` | `200` | `associator.llm` | Maximum units per association call |
| `associator_retry_max` | `int` | `3` | `associator.llm` | Maximum LLM attempts |
| `associator_retry_backoff` | `int` | `1000` | `associator.llm` | Retry backoff in milliseconds |
| `classifier_retry_max` | `int` | `3` | `classifier.llm` | Maximum LLM attempts |
| `classifier_retry_backoff` | `int` | `1000` | `classifier.llm` | Retry backoff in milliseconds |
| `dedup_min_similarity` | `float` | `0.5` | Both Dedup implementations | Minimum similarity |
| `dedup_top_k` | `int` | `5` | Both Dedup implementations | Candidate limit |
| `dedup_tier_filter` | `bool` | `false` | Both Dedup implementations | Restricts recall to the same tier |
| `dedup_scope_filter` | `bool` | `true` | Both Dedup implementations | Restricts recall to the candidate Scope |
| `dedup_medium_similarity` | `float` | `0.7` | Both Evolvers | Medium-similarity decision threshold |
| `dedup_high_similarity` | `float` | `0.9` | Both Evolvers | High-similarity threshold; must not be below medium |
| `layer_annotator_threshold` | `int` | `512` | Both LayerAnnotators | Annotates only units whose content length exceeds this threshold |
| `layer_annotator_l1_chars` | `int` | `200` | `layer_annotator.keyword` | Number of characters retained for L1 |
| `layer_annotator_retry_max` | `int` | `3` | `layer_annotator.llm` | Maximum LLM annotation attempts |
| `layer_annotator_retry_backoff` | `int` | `1000` | `layer_annotator.llm` | Retry backoff in milliseconds |
| `layers_index_enabled` | `bool` | `true` | fulltext/vector/hybrid | Writes independent L0/L1 indexes when enabled |
| `entity_enabled` | `bool` | `false` | `hybrid` | Attempts to assemble EntityStore when enabled |
| `vector_enabled` | `bool` | `true` | Evolver builder | Selects the default IndexBuilder/Dedup combination when targets are not explicit |

### 18.1 `extract_batch_size` vs `middle_batch_size`

| Parameter | Config location | Default | Role |
|---|---|---:|---|
| `middle_batch_size` | `job_factory.default.params` | `10` | Job batching cap |
| `extract_batch_size` | `extractor` assembly params (same as `extractor_min_confidence`) | `10` | Extractor LLM batch cap |

`middle_batch_size` is in the `defaults.py` snapshot; `extract_batch_size` defaults from Extractor `_build` and is overridden in `extractor.*.params` when needed. When tuning, keep `extract_batch_size` ≥ `middle_batch_size`.

## 19. Minimal Operator Example

```python
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.classifier import Classifier
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.storage.types import IndexWriteMode


def classify_and_build(
    classifier: Classifier,
    index_builder: IndexBuilder,
    units: list[MemoryUnit],
) -> list[str]:
    classified = classifier.classify(units)
    index_builder.build(classified, mode=IndexWriteMode.ALL)
    return [unit.id for unit in classified]
```

This is the smallest Construction-operator combination used in the direct `infer=false` write path.
Real applications should let `MemoryEngine` perform cross-layer orchestration instead of copying the
full write flow into business code.
