# memory_core.graph.graph_memory

`memory_core.graph.graph_memory` is the **Graph Memory** module in JiuwenMemory. It turns conversations, documents, or JSON strings into entities, relations, and source episodes, and supports hybrid retrieval over the graph structure.

> **Note**: Graph Memory is currently an independent module. It is not wired into the `LongTermMemory.add_messages` pipeline. To use it, create `GraphMemory` directly and register graph storage, an LLM, and an embedding model.

## When To Use It

Graph Memory is useful when memory needs relationship awareness, not just text similarity:

- Extract people, organizations, locations, projects, events, and their relationships from multi-turn conversations.
- Structure document knowledge into searchable entities and factual relations.
- Merge equivalent entities, deduplicate repeated relations, and keep source evidence.
- Retrieve entities, relations, and original source episodes, with optional graph expansion.

## Core Concepts

Graph Memory stores three types of graph objects:

- `Entity`: an entity node, such as a user, company, project, location, or concept. It contains `name`, `content`, `attributes`, linked relations, and source episodes.
- `Relation`: an edge between entities. It contains `lhs`, `rhs`, `name`, `content`, and time fields such as `valid_since` / `valid_until`.
- `Episode`: one input source, such as a conversation, document, or JSON string. It stores the original content and the entities mentioned by that content.

Each `add_memory` call returns `GraphMemUpdate`, which records objects added, updated, or removed:

- `added_episode` / `updated_episode`
- `added_entity` / `updated_entity` / `removed_entity`
- `added_relation` / `updated_relation` / `removed_relation`

`added_*` and `updated_*` fields contain graph objects. `removed_entity` and `removed_relation` are sets of UUID strings.

## Quick Start

The following example creates a Graph Memory instance, writes a document, and searches the graph. Replace the LLM endpoint, embedding endpoint, and embedding dimension with your real service values.

```python
import asyncio

from foundation.llm import Model
from foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from foundation.store.graph import GraphConfig, GraphStoreIndexConfig
from foundation.store.graph.index_field import MilvusAUTO
from memory_core.config.graph import AddMemStrategy, EpisodeType
from memory_core.graph.graph_memory.base import GraphMemory
from retrieval.common.config import EmbeddingConfig
from retrieval.embedding.api_embedding import APIEmbedding


async def main():
    llm = Model(
        model_config=ModelRequestConfig(model="your-llm-model"),
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_base="https://your-llm-endpoint/v1",
            api_key="your-api-key",
        ),
    )

    embedding = APIEmbedding(
        config=EmbeddingConfig(
            model_name="your-embedding-model",
            base_url="https://your-embedding-endpoint/v1/embeddings",
            api_key="your-api-key",
        )
    )

    graph_config = GraphConfig(
        uri="./graph_memory.db",
        name="agent_memory_graph",
        backend="milvus",
        embed_dim=1024,  # Replace with your actual embedding dimension.
        db_embed_config=GraphStoreIndexConfig(
            index_type=MilvusAUTO(),
            distance_metric="cosine",
        ),
    )

    memory = GraphMemory(
        db_config=graph_config,
        llm_client=llm,
        extraction_strategy=AddMemStrategy(chinese_entity=False),
        language="en",
    )
    memory.attach_embedder(embedding)

    update = await memory.add_memory(
        src_type=EpisodeType.DOCUMENT,
        user_id="user_001",
        content="Alice works on database products at Huawei Cloud and is evaluating a Milvus graph memory design.",
    )
    print(update.added_entity)

    result = await memory.search(
        query="Who is evaluating the graph memory design?",
        user_id="user_001",
        entity=True,
        relation=True,
        episode=True,
    )
    print(result)


asyncio.run(main())
```

## API Entry Point

### class GraphMemory

```python
class GraphMemory:
    def __init__(
        self,
        db_config: GraphConfig,
        llm_client: Model | None = None,
        llm_structured_output: bool = True,
        reranker: Reranker | None = None,
        extraction_strategy: AddMemStrategy = DEFAULT_STRATEGY,
        db_kwargs: dict | None = None,
        llm_extra_kwargs: dict | None = None,
        language: Literal["cn", "en"] = "cn",
        debug: bool = False,
    ):
        ...
```

`GraphMemory` is the main entry point for graph memory. It orchestrates LLM extraction, entity and relation merging, graph store writes, and graph retrieval. Import it from `memory_core.graph.graph_memory.base`; the package `memory_core.graph.graph_memory` does not currently re-export `GraphMemory`.

Key parameters:

- `db_config`: graph store configuration. The built-in backend is currently `milvus`.
- `llm_client`: LLM used for entity extraction, relation extraction, merging, and deduplication. The constructor allows `None`, but `add_memory` requires a usable LLM.
- `llm_structured_output`: whether to request structured JSON output from the LLM.
- `reranker`: optional reranker used when search strategies enable reranking.
- `extraction_strategy`: write strategy controlling recall, merging, and prompt language. The default strategy has `chinese_entity=True`, so entity extraction uses Chinese prompts even if `GraphMemory.language="en"`; set `AddMemStrategy(chinese_entity=False)` for fully English entity extraction.
- `llm_extra_kwargs`: extra parameters passed to each LLM call.
- `language`: default prompt language, `"cn"` or `"en"`.
- `debug`: logs prompt template names, LLM input, and LLM output for debugging.

### attach_embedder

```python
def attach_embedder(self, embedder: Embedding) -> None:
    ...
```

Attach the embedding model used by graph storage. Both `add_memory` and `search` need an embedder; otherwise `MEMORY_GRAPH_EMBED_MODEL_NOT_FOUND` is raised.

`GraphConfig.embed_dim` must match `embedder.dimension`, or the Milvus backend will reject the embedder.

### attach_reranker

```python
def attach_reranker(self, reranker: Reranker) -> None:
    ...
```

Attach a reranker. It is used only when a `SearchConfig` has `rerank=True`.

### register_search_strategy

```python
def register_search_strategy(
    self,
    name: str,
    search_entity: SearchConfig | None = None,
    search_relation: SearchConfig | None = None,
    search_episode: SearchConfig | None = None,
    force: bool = False,
) -> None:
    ...
```

Register a named search strategy. Each strategy has separate search configs for entity, relation, and episode collections.

The default strategy is named `default`:

- entity: uses `WeightedRankConfig` and inherits `SearchConfig` defaults `top_k=3`, `min_score=0.3`.
- relation: `min_score=0.02`, with the default `RRFRankConfig`.
- episode: `min_score=0.025`, with the default `RRFRankConfig`.

### add_memory

```python
async def add_memory(
    self,
    src_type: EpisodeType,
    user_id: str,
    content: list[BaseMessage | dict] | str,
    content_fmt_kwargs: dict | None = None,
    reference_time: datetime | None = None,
) -> GraphMemUpdate:
    ...
```

Write one piece of content into graph memory.

Parameters:

- `src_type`: source type. See `EpisodeType`.
- `user_id`: user identifier. Writes and searches are isolated by user. With the default storage configuration, the length must not exceed 32. `add_memory` follows `GraphStoreStorageConfig.user_id`; `search` currently requires each id to be at most 32 characters.
- `content`: input content. Conversations may be a list of `BaseMessage` or OpenAI-style dictionaries; documents and JSON inputs should be strings.
- `content_fmt_kwargs`: conversation role replacement mapping, such as `{"user": "Alice (user)", "assistant": "Support Bot"}`.
- `reference_time`: reference time for the content. If omitted, the current time is used.

The returned `GraphMemUpdate` records added, updated, or removed episodes, entities, and relations.

### search

```python
async def search(
    self,
    query: str,
    user_id: str | list[str],
    search_strategy: str = "default",
    *,
    entity: bool = True,
    relation: bool = True,
    episode: bool = True,
    query_embedding: list[float] | None = None,
) -> dict[str, list[tuple[float, BaseGraphObject]]]:
    ...
```

Search graph memory with a natural-language query.

Parameters:

- `query`: search text.
- `user_id`: one user id or a list of user ids. With the default storage configuration, each id must not exceed 32 characters.
- `search_strategy`: registered strategy name.
- `entity` / `relation` / `episode`: whether to search the corresponding collection.
- `query_embedding`: optional precomputed query embedding. If omitted, the attached embedder is used.

Returns a dictionary grouped by collection. Possible keys are `ENTITY_COLLECTION`, `RELATION_COLLECTION`, and `EPISODE_COLLECTION`; only enabled collections are present. Values are lists of `(score, graph_object)` tuples.

## Configuration

### EpisodeType

```python
class EpisodeType(Enum):
    CONVERSATION = 0
    DOCUMENT = 1
    JSON = 2
```

- `CONVERSATION`: conversation content, usually a list of `BaseMessage` objects or `{"role": ..., "content": ...}` dictionaries.
- `DOCUMENT`: document text, suitable for long descriptions, knowledge base content, or web page text.
- `JSON`: structured JSON content. `add_memory` still requires `content` to be a string, so pass a serialized JSON string rather than a Python dict or list.

### GraphConfig

`GraphConfig` describes graph store connection and index parameters:

- `uri`: Milvus connection URI or a local Milvus Lite file path.
- `name`: Milvus database name. Default: `""`.
- `token`: remote Milvus auth token. Default: `""`.
- `backend`: backend name. The built-in backend is currently `"milvus"`. Default: `"milvus"`.
- `timeout`: connection and operation timeout. Default: `15.0`.
- `extras`: extra backend client arguments, such as a Milvus alias.
- `max_concurrent`: internal concurrency limit. Default: `10`.
- `embed_dim`: vector dimension. It must match the embedding dimension. Default: `512`.
- `embed_batch_size`: batch size used when embedding graph objects for writes. Default: `10`.
- `embedding_model`: optional initial embedding model; it can also be attached later with `attach_embedder`.
- `db_storage_config`: graph object field length and array size limits.
- `db_embed_config`: vector index and distance metric configuration. It must provide `index_type` and `distance_metric`.
- `request_max_retries`: retry count for internal LLM calls and pre-write batch embedding. Default: `5`.

### GraphStoreIndexConfig

`GraphStoreIndexConfig` controls ANN indexing in graph storage:

- `index_type`: index type, such as `MilvusAUTO()`, `MilvusHNSW()`, or `MilvusFLAT()`.
- `distance_metric`: `"cosine"`, `"euclidean"`, or `"dot"`.
- `extra_configs`: extra index build parameters.
- `bm25_config`: sparse BM25 retrieval parameters.
- `bm25_analyzer_settings`: BM25 analyzer settings.

### AddMemStrategy

`AddMemStrategy` controls extraction, recall, and merging during writes:

- `chinese_entity`: whether entity extraction always uses Chinese prompts. Default: `True`.
- `chinese_entity_dedupe`: whether entity deduplication always uses Chinese prompts. Default: `False`.
- `chinese_relation`: whether relation extraction always uses Chinese prompts. Default: `False`.
- `skip_uuid_dedupe`: whether to skip UUID conflict checks. Default: `False`.
- `recall_episode`: strategy for retrieving related historical episodes before extraction. Defaults include `top_k=3`, `min_score=0.025`, `same_kind=False`, and `exclude_future_results=True`.
- `recall_entity`: strategy for retrieving existing entities after entity extraction. Defaults include `top_k=3`, `min_score=0.1`, and `WeightedRankConfig(name_dense=0.7, content_dense=0.1, content_sparse=0.2)`.
- `recall_relation`: strategy for retrieving existing relations during relation deduplication. Defaults include `top_k=3`, `min_score=0.02`, and `RRFRankConfig`.
- `summary_target`: target length for entity summaries. Default: `250`.
- `merge_entities`: whether to merge entities. Default: `True`.
- `merge_relations`: whether to merge relations. Default: `True`.
- `merge_filter`: whether to filter relations after entity merging. Default: `True`.

### SearchConfig

`SearchConfig` controls retrieval:

- `top_k`: number of returned results.
- `min_score`: minimum score threshold.
- `rank_config`: hybrid ranking config, such as `RRFRankConfig` or `WeightedRankConfig`.
- `bfs_k`: number of graph expansion candidates per layer.
- `bfs_depth`: graph expansion depth. It currently applies only to entity and relation collections; episode search does not perform graph expansion.
- `filter_expr`: extra filter expression.
- `output_fields`: returned fields.
- `rerank`: whether to use the attached reranker.
- `language`: language parameter passed to the backend and reranker during retrieval. Default: `"en"`. This is separate from `GraphMemory.language`.

## Write Flow

`GraphMemory.add_memory` roughly follows these steps:

1. Validate input and format content according to `EpisodeType`.
2. Retrieve related historical episodes as extraction context.
3. Create the current episode with source content and reference time.
4. Start timezone prediction concurrently and call the LLM to extract entity declarations.
5. Embed entity names and retrieve existing entities.
6. Ask the LLM to judge whether new and existing entities are duplicates, then merge if needed.
7. Use timezone prediction to help parse relation validity times, and call the LLM to extract relations.
8. Generate entity summaries and attributes.
9. Filter, classify, and semantically deduplicate relations.
10. Post-process bidirectional references among entities, relations, and episodes.
11. Batch-generate embeddings and write graph objects to storage.
12. Refresh the backend and return `GraphMemUpdate`.

Writes for the same `user_id` hold a per-user thread lock to avoid overlapping graph updates for the same user.

## Search Flow

`GraphMemory.search` can search three collections:

- `ENTITY_COLLECTION`: entity nodes.
- `RELATION_COLLECTION`: entity relations.
- `EPISODE_COLLECTION`: source episodes.

Search first obtains the query embedding, then searches enabled collections concurrently according to the selected `search_strategy`. Each collection can have its own `SearchConfig`, so `top_k`, `min_score`, and ranking can differ by collection. Graph expansion depth currently applies only to entity and relation collections.

If `bfs_depth > 0`, the underlying `GraphStore` can expand along graph relations for entity and relation collections. If `rerank=True` and `GraphMemory` has an attached reranker, candidates are reranked after recall.

## Graph Store Backend

Graph storage abstractions are defined in `foundation.store.graph`:

- `GraphStore`: graph store protocol for add, query, delete, search, refresh, and close operations.
- `GraphStoreFactory`: creates backend instances from `GraphConfig.backend`.
- `GraphConfig`: backend connection, indexing, and concurrency configuration.
- `Entity` / `Relation` / `Episode`: graph object models.

The built-in backend is `MilvusGraphStore`. It maintains three collections:

- `ENTITY_COLLECTION`
- `RELATION_COLLECTION`
- `EPISODE_COLLECTION`

The Milvus backend supports dense vectors, BM25 sparse vectors, and hybrid ranking. `GraphStoreFactory.from_config` auto-registers Milvus support when the backend is `"milvus"`.

## Prompts And Languages

Graph Memory extraction prompts are located in:

- `memory_core/graph/extraction/prompts/cn/`
- `memory_core/graph/extraction/prompts/en/`

They cover:

- entity extraction
- entity deduplication
- entity merging
- relation extraction
- relation filtering
- relation deduplication
- timezone prediction
- entity summary generation

Structured output models are defined in `memory_core/graph/extraction/extraction_models.py` and use `MultilingualBaseModel` to generate Chinese and English JSON schemas.

## FAQ

### How Is GraphMemory Related To LongTermMemory?

They are parallel modules for now. `LongTermMemory` manages flat memory units such as user profiles, semantic memories, episodic memories, variables, and summaries. `GraphMemory` manages a knowledge graph made of entities, relations, and episodes. `LongTermMemory.add_messages` does not currently call `GraphMemory.add_memory`.

### Why Does add_memory Report A Missing Embedder?

Both `add_memory` and `search` require an embedding model. Attach one before use:

```python
memory.attach_embedder(embedding)
```

Alternatively, pass it through `GraphConfig(embedding_model=embedding, embed_dim=...)`. `embed_dim` must still match the actual embedding dimension.

### Why Does MilvusGraphStore Report An embed_dim Mismatch?

`GraphConfig.embed_dim` must equal `embedding.dimension`. If they differ, the vector fields for entities, relations, and episodes cannot match the backend schema.

### When Should I Customize SearchConfig?

Register a custom strategy if you want stricter entity recall, looser relation recall, BFS only for entity/relation collections, or rerank only for selected result types:

```python
from foundation.store.graph.result_ranking import WeightedRankConfig
from memory_core.config.graph import SearchConfig

memory.register_search_strategy(
    "entity_heavy",
    search_entity=SearchConfig(
        top_k=10,
        min_score=0.1,
        rank_config=WeightedRankConfig(name_dense=0.7, content_dense=0.2, content_sparse=0.1),
    ),
    search_relation=SearchConfig(top_k=5, min_score=0.02),
    search_episode=SearchConfig(top_k=3, min_score=0.025),
)
```

### How Do I Debug LLM Extraction Failures?

Set `debug=True` when creating `GraphMemory` to log prompt template names, LLM inputs, and LLM outputs. Also check whether the LLM reliably returns content compatible with the expected JSON schema.

`GraphConfig.request_max_retries` controls retries for internal LLM calls and pre-write batch embedding. It does not mean Milvus operations or all external service calls are automatically retried.

## Related Source

- `memory_core/graph/graph_memory/base.py`
- `memory_core/graph/graph_memory/states.py`
- `memory_core/graph/graph_memory/postprocess_graph_objects.py`
- `memory_core/graph/extraction/`
- `memory_core/config/graph.py`
- `foundation/store/graph/`
- `foundation/store/graph/milvus/`

