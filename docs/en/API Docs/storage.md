# Storage Layer API

The Storage layer provides two levels of storage abstraction to upper layers:

- `Storage`: the unified domain interface for `MemoryUnit`, also responsible for capability discovery, authorization, and retrieval adaptation.
- `BaseStore` and its subinterfaces: six standard ports for KV, vector, full-text, graph, fusion, and file backends, plus an independent entity reverse-index port.

This document is an API reference for the current abstract interfaces. It does not prescribe the internal implementation of any specific backend. The following source files are authoritative:

- [`storage.py`](../../../jiuwen_memory/storage/storage.py)
- [`base.py`](../../../jiuwen_memory/storage/base.py)
- [`security.py`](../../../jiuwen_memory/storage/security.py)
- [`kv.py`](../../../jiuwen_memory/storage/kv.py)
- [`vector.py`](../../../jiuwen_memory/storage/vector.py)
- [`fulltext.py`](../../../jiuwen_memory/storage/fulltext.py)
- [`graph.py`](../../../jiuwen_memory/storage/graph.py)
- [`fusion.py`](../../../jiuwen_memory/storage/fusion.py)
- [`fs.py`](../../../jiuwen_memory/storage/fs.py)
- [`entity_store.py`](../../../jiuwen_memory/storage/entity_store.py)
- [`types.py`](../../../jiuwen_memory/storage/types.py)
- [`common/type_def/entity.py`](../../../jiuwen_memory/common/type_def/entity.py)
- [`common/type_def/retrieval.py`](../../../jiuwen_memory/common/type_def/retrieval.py)
- [`common/errors.py`](../../../jiuwen_memory/common/errors.py)

## 1. Common Conventions

### 1.1 Scope Isolation

All data operations on `Storage` and the six standard Store interfaces explicitly accept `scope: Scope`. `Scope` consists of five dimensions: `org`, `space`, `user`, `agent`, and `session`. A storage implementation must constrain writes, queries, and deletions to that scope.

`scope` is an independent isolation axis. It must not be embedded in `metadata`, `filters`, or any Record/Query structure. The same ID may exist independently in different Scopes.

`EntityStore` is the only exception. It uses `space_id` routing together with `EntityStoreFilters.actor_id` for isolation and does not accept the five-part Scope as the first argument. See "EntityStore API" for details.

```python
from jiuwen_memory.common.type_def import Scope

scope = Scope(
    org="org-1",
    space="space-1",
    user="user-1",
    agent="agent-1",
    session="session-1",
)
```

### 1.2 CRUD Semantics

| Method | Semantics | When a record exists or is missing |
|---|---|---|
| `insert` | Create a new record | Raises `ConflictError` if it already exists |
| `update` | Replace or update an existing record | Raises `NotFoundError` if it does not exist |
| `delete` | Delete a record | Idempotent; a missing record is not an error |
| `get` | Read by ID/key | A missing single item raises `NotFoundError`; batch reads on retrieval-oriented Stores omit missing IDs |

The interface does not guarantee transactional atomicity for batch methods. Consult the specific backend's capabilities when atomic behavior is required.

### 1.3 Common Exceptions

| Exception | Meaning |
|---|---|
| `ConflictError` | The new ID/key already exists in the current Scope |
| `NotFoundError` | The ID/key to update or read does not exist |
| `ValidationError` | A parameter, Scope, or data structure is invalid |
| `PermissionDeniedError` | `StorageSecurity` denied the operation |
| `UnsupportedStorageCapabilityError` | The requested port was not declared by the Storage instance |
| `BackendError` | An unexpected storage connection, I/O, timeout, or remote-service failure |
| `HealthCheckError` | A `health()` check failed |
| `StorageRetrievalError` | Every selected retrieval source failed |

## 2. Unified Storage Interface

```python
from jiuwen_memory.storage.storage import Storage
```

`Storage` is the shared storage entry point for Engine, Construction, and Retrieval. It defines behavior at the interface level and does not mean "source-of-truth writes only." A concrete implementation may persist only the MemoryUnit body, or it may also write forward, vector, full-text, or graph indexes.

### 2.1 Capability Discovery and Port Access

`StorageCapability` defines six standard capabilities: `KV`, `VECTOR`, `FULLTEXT`, `GRAPH`, `FUSION`, and `FS`.

| API | Return value | Description |
|---|---|---|
| `capabilities()` | `frozenset[StorageCapability]` | Returns the capabilities declared by the current Storage instance |
| `has_kv()` / `has_vector()` / `has_fulltext()` | `bool` | Checks for the default KV/vector/full-text capability |
| `has_graph()` / `has_fusion()` / `has_fs()` | `bool` | Checks for the default graph/fusion/file capability |
| `has_*_port(name="default")` | `bool` | Checks whether a named port exists |
| `kv` / `vector` / `fulltext` / `graph` / `fusion` / `fs` | Corresponding Store | Accesses the default port |
| `*_port(name="default")` | Corresponding Store | Accesses a named port |

Check a capability with `has_*()` or `has_*_port()` before accessing its port. Accessing an undeclared port raises `UnsupportedStorageCapabilityError`.

```python
if storage.has_vector_port("default"):
    vector_store = storage.vector_port("default")
```

### 2.2 MemoryUnit Writes and Deletion

#### `add`

```python
storage.add(
    scope: Scope,
    units: list[MemoryUnit],
    *,
    mode: IndexWriteMode = IndexWriteMode.ALL,
    access: StorageAccessContext | None = None,
) -> None
```

Creates a batch of MemoryUnits in `scope`. Each `unit.scope` should match the explicitly supplied `scope`. Duplicate-ID handling follows the concrete Storage implementation's create semantics; standard storage ports represent conflicts with `ConflictError`.

#### `update`

```python
storage.update(
    scope: Scope,
    units: list[MemoryUnit],
    *,
    mode: IndexWriteMode = IndexWriteMode.ALL,
    access: StorageAccessContext | None = None,
) -> None
```

Updates a batch of existing MemoryUnits. If an implementation cannot separate body and retrieval-index writes, it must still ensure that the body is updated in `FORWARD_ONLY` mode.

#### `delete`

```python
storage.delete(
    scope: Scope,
    unit_ids: list[str],
    *,
    mode: IndexRemoveMode = IndexRemoveMode.HARD,
    access: StorageAccessContext | None = None,
) -> None
```

Deletes the specified memories within `scope`. Deletion is idempotent.

#### Write Scope: `IndexWriteMode`

| Enum value | Semantics |
|---|---|
| `ALL` | Requests writing the memory body and every retrieval index supported by the implementation |
| `FORWARD_ONLY` | Writes only the memory body and does not actively update retrieval indexes |
| `RETRIEVAL_ONLY` | Assumes the body already exists and writes only retrieval indexes; implementations without retrieval capability may treat this as a no-op |

`mode` expresses the logical write scope requested by the caller; it does not imply that the backend has two physical storage systems. For example, `CompositeStorage` does not perform index projection, while an integrated Storage implementation may complete multiple writes in a single `add` call.

#### Removal Scope: `IndexRemoveMode`

| Enum value | Semantics |
|---|---|
| `SOFT` | Removes only retrieval indexes; the memory body remains readable through `get`/`list`. Implementations without retrieval capability may treat this as a no-op |
| `HARD` | Physically deletes both retrieval indexes and the memory body |

### 2.3 MemoryUnit Reads and Listing

#### `get`

```python
storage.get(
    scope: Scope,
    unit_ids: list[str],
    *,
    access: StorageAccessContext | None = None,
) -> list[MemoryUnit]
```

Batch-reads MemoryUnits within `scope`. The result contains only memories that were actually found. If positional alignment with `unit_ids` is required, the caller should build a mapping by `unit.id`.

#### `list`

```python
storage.list(
    scope: Scope,
    *,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    filters: FilterExpr | None = None,
    extensions: dict[str, str] | None = None,
    access: StorageAccessContext | None = None,
) -> MemoryListResult
```

| Parameter | Description |
|---|---|
| `offset` | Pagination start offset |
| `limit` | Maximum number of items in the page |
| `memory_types` | Allowlist of memory types; `None` disables this filter |
| `filters` | A `FilterExpr` metadata predicate outside the Scope dimensions |
| `extensions` | Pass-through parameters interpreted by the concrete implementation or business convention |
| `access` | Optional authorization context |

`MemoryListResult.items` contains the current page. `MemoryListResult.count` is the total number of matching items before pagination. The standard semantics are filter first, then count and paginate.

### 2.4 Retrieval Adapter APIs

`Storage` exposes retrieval capability to the Retrieval layer at three levels:

| API | Return value | Responsibility boundary |
|---|---|---|
| `recall(scope, query, *, channels, recall_limit, access=None)` | `RecallResult[ScoredUnit]` | Returns only unmaterialized `unit_id` candidates, scores, and channel evidence |
| `recall_and_get(scope, query, *, channels, recall_limit, access=None)` | `RecallResult[ScoredMemoryUnit]` | Recalls candidates and loads complete `MemoryUnit` objects |
| `retrieve(scope, query, fuser, *, channels, recall_limit, rank_limit, access=None)` | `RankedStorageResult` | Performs recall, materialization, and Fuser ranking inside Storage |

Shared parameters:

- `query: ParsedQuery`: a structured query produced by the Retrieval layer.
- `channels`: the logical recall channels to use. When Storage is called directly, expansion of `None` depends on the concrete Storage and its assembled recall sources. Business code should prefer `Retriever.retrieve()`.
- `recall_limit`: the candidate limit for each physical recall source.
- `rank_limit`: the candidate limit after fusion inside Storage.
- `fuser: CandidateFuser`: any object that implements `fuse(query, candidates)`.

`preferred_retrieval_pipeline() -> RetrievalPipeline` returns the Storage instance's stable preferred path:

| Enum value | Retriever behavior |
|---|---|
| `RECALL_GET_RANK` | `recall` -> point reads -> `Fuser` |
| `RECALL_AND_GET_RANK` | `recall_and_get` -> `Fuser` |
| `RETRIEVE` | Calls `Storage.retrieve` directly |

`RecallResult` supports partial success: `batches` preserves candidates from each physical recall source, while `errors` records a `ChannelError` for every failed source.

### 2.5 Other Unified APIs

| API | Description |
|---|---|
| `security` | Returns the current Storage's `StorageSecurity` |
| `scopes() -> list[Scope]` | Enumerates Scopes containing MemoryUnit data; ordering is implementation-defined |
| `health() -> None` | Checks Storage, its security component, and its backends; returns `None` when healthy |

## 3. BaseStore Base Class

```python
from jiuwen_memory.storage.base import BaseStore, StoreType
```

Every low-level Store inherits `BaseStore` and provides the following APIs:

| API | Kind | Description |
|---|---|---|
| `security` | Property | Returns `StoreSecurity`; defaults to a passthrough implementation with protection disabled |
| `store_type()` | Abstract method | Returns `StoreType` |
| `health()` | Abstract method | Returns `None` when healthy; otherwise raises `HealthCheckError` |

`StoreType` contains `KV`, `FULLTEXT`, `VECTOR`, `GRAPH`, `FUSION`, and `FS`.

## 4. KVStore API

```python
from jiuwen_memory.storage.kv import KVStore
```

| API | Return value | Description |
|---|---|---|
| `insert(scope, key, value, ttl=0.0)` | `None` | Creates a binary value; `ttl` is measured in seconds and `0` means no expiration |
| `update(scope, key, value, ttl=0.0)` | `None` | Replaces an existing key |
| `delete(scope, key)` | `None` | Deletes a key idempotently |
| `get(scope, key)` | `bytes` | Reads one key; raises `NotFoundError` when missing |
| `mget(scope, keys)` | `list[bytes]` | Preserves positional correspondence with `keys` and does not deduplicate; any missing key raises `NotFoundError` |
| `exists(scope, key)` | `bool` | Checks whether a key exists |
| `scan(scope, prefix="")` | `list[tuple[str, bytes]]` | Scans unexpired raw key-value pairs within a Scope, optionally by prefix; ordering is undefined |
| `list(scope, *, offset=0, limit=100, memory_types=None, filters=None, extensions=None)` | `KVMemoryListResult` | Queries raw MemoryUnit entries in the `/memory/` namespace |
| `scopes()` | `list[Scope]` | Enumerates used Scopes; ordering is undefined |

`KVMemoryListResult.entries` contains `(key, value)` pairs for the current page, and `count` is the total before pagination.

## 5. VectorStore API

```python
from jiuwen_memory.storage.vector import VectorStore
from jiuwen_memory.storage.types import VectorQuery, VectorRecord
```

| API | Return value | Description |
|---|---|---|
| `insert(scope, records)` | `None` | Creates a list of `VectorRecord` rows |
| `update(scope, records)` | `None` | Replaces existing vector rows |
| `delete(scope, ids)` | `None` | Deletes vector rows idempotently |
| `get(scope, ids)` | `list[VectorRecord]` | Batch point-read; missing IDs are omitted |
| `search(scope, query)` | `list[ScoredID]` | Performs ANN search within the Scope |
| `recall(scope, query, output_fields=None)` | `list[ScoredHit]` | Optional ANN capability that returns payloads in one request; raises `NotImplementedError` by default |
| `score_higher_is_better()` | `bool` | Returns score direction semantics; defaults to `True` |

`VectorRecord` consists of `id`, `vector`, and `metadata`. `VectorQuery` consists of `vector`, `top_k`, `filters`, and `return_metadata`.

`recall()` is an optional optimization API. Currently, `output_fields` recognizes only `"metadata"`. If a backend does not override the method, the caller should fall back to `search()` + `get()`. A distance-based backend where lower scores are more relevant must override `score_higher_is_better()` and return `False`.

## 6. FulltextStore API

```python
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.types import Document, TextQuery
```

| API | Return value | Description |
|---|---|---|
| `insert(scope, docs)` | `None` | Creates a list of `Document` objects |
| `update(scope, docs)` | `None` | Rebuilds indexes for existing documents |
| `delete(scope, ids)` | `None` | Deletes documents idempotently |
| `get(scope, ids)` | `list[Document]` | Batch point-read; missing IDs are omitted |
| `search(scope, query)` | `list[ScoredID]` | Performs BM25 or similar keyword search and returns top-k results |

`Document` consists of `id`, `text`, and `metadata`. `TextQuery` consists of `text`, `top_k`, and `filters`.

## 7. GraphStore API

```python
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node
```

| API | Return value | Description |
|---|---|---|
| `seed_ids(scope, tokens)` | `list[str]` | Locates seed nodes for graph traversal from terms; matching semantics are backend-defined |
| `insert(scope, nodes=None, edges=None)` | `None` | Creates nodes and/or edges |
| `update(scope, nodes=None, edges=None)` | `None` | Updates existing nodes and/or edges |
| `delete(scope, node_ids=None, edge_ids=None)` | `None` | Deletes nodes and/or edges idempotently; deleting a node also removes its associated edges |
| `get(scope, node_ids)` | `list[Node]` | Batch point-read; missing node IDs are omitted |
| `search(scope, query)` | `list[Node]` | Performs multi-hop traversal starting from `start_id` |

`Node` consists of `id`, `label`, and `properties`. `Edge` consists of `id`, `source`, `target`, `relation`, and `properties`. `GraphQuery` provides `start_id`, `relation`, `depth`, and `limit`.

## 8. FusionStore API

```python
from jiuwen_memory.storage.fusion import FusionStore
from jiuwen_memory.storage.types import FusionQuery, FusionRecord
```

| API | Return value | Description |
|---|---|---|
| `insert(scope, records)` | `None` | Creates fusion rows |
| `update(scope, records)` | `None` | Replaces existing fusion rows |
| `delete(scope, ids)` | `None` | Deletes fusion rows idempotently |
| `get(scope, ids)` | `list[FusionRecord]` | Point-reads complete fusion rows from the forward store; missing IDs are omitted |
| `search(scope, query)` | `list[ScoredID]` | Combines vector, text, and scalar-predicate retrieval in one call |

`FusionRecord` may carry `vector`, `text`, `scalars`, and `value` together, and some fields may be `None`. `FusionQuery.vector_weight` controls the mixture of vector and text scores: `1.0` means vector only, while `0.0` means text only.

## 9. FSStore API

```python
from jiuwen_memory.storage.fs import FSStore
```

| API | Return value | Description |
|---|---|---|
| `insert(scope, key, data)` | `str` | Writes a new file and returns its canonical `ref` |
| `update(scope, ref, data)` | `str` | Replaces an existing file and returns the possibly updated `ref` |
| `delete(scope, ref)` | `None` | Deletes a file idempotently |
| `get(scope, ref)` | `BinaryIO` | Opens a file; the caller is responsible for closing the returned stream |
| `stat(scope, ref)` | `FileStat` | Returns the file reference, size, MIME type, and creation/update timestamps |

```python
with storage.fs.get(scope, ref) as stream:
    payload = stream.read()
```

## 10. EntityStore API

```python
from jiuwen_memory.storage.entity_store import EntityStore
```

`EntityStore` is an independent reverse-index port from entities to MemoryUnit IDs. It is not part of `StorageCapability` and cannot be accessed through `storage.entity` or `storage.*_port()`. It is assembled independently through `EntityStoreProducer`.

| API | Return value | Description |
|---|---|---|
| `ensure_index()` | `None` | Ensures that the entity index has been created and is ready; must be called before using the other APIs |
| `find_by_entity_text_hash(space_id, entity_text_hashes, *, filters, limit=500)` | `list[EntityRecord]` | Performs exact lookup by SHA-256 hash of entity text; vector nearest-neighbor search is not supported |
| `find_by_linked_memory_id(space_id, memory_id, *, filters)` | `list[EntityRecord]` | Finds entities linked to the specified MemoryUnit ID |
| `execute_operations(space_id, operations)` | `EntityBatchResult` | Executes a mixed batch of `INSERT` / `LINK` / `UNLINK_UPDATE` / `DELETE` operations |

`EntityStoreFilters.from_scope(scope)` derives `actor_id` from `scope.user`. This allows entities to be shared across agents and sessions for the same user while remaining constrained by `space_id + actor_id`.

`EntityBatchResult.successful_ids` and `failed_ids` report batch results per item, allowing partial failure. Although `EntityStore` inherits `BaseStore`, it does not participate in the six-value `StoreType` routing scheme. The current implementation returns `None` from `store_type()`.

## 11. Security APIs

### 11.1 StorageSecurity

```python
security.authorize(
    access: StorageAccessContext | None,
    scope: Scope,
    action: StorageAction,
    resource: str,
) -> None
```

Returns `None` when the operation is allowed and raises `PermissionDeniedError` when denied. `StorageAccessContext.actor` identifies the access subject, while `attributes` carries extension context required by the authorization implementation. `StorageAction` contains `ADD`, `UPDATE`, `DELETE`, `GET`, `LIST`, `SEARCH`, and `ADMIN`.

`health() -> None` checks the authorization component. The base implementation returns `None`; an implementation that depends on an external policy service may override it.

`AllowAllStorageSecurity` is the default allow-all implementation, so `access` may be omitted when custom authorization is not enabled.

### 11.2 StoreSecurity

`StoreSecurity` represents low-level data-protection capability, which is separate from the access-authorization responsibility of `StorageSecurity`.

| API | Description |
|---|---|
| `enabled() -> bool` | Returns whether real data protection is enabled for the backend |
| `health() -> None` | Checks the health of the protection component |

`BaseStore.security` returns `PassthroughStoreSecurity` by default, whose `enabled()` method returns `False`.

## 12. Producers and Implementation Registration

| Producer | `TOP_NAME` | Product |
|---|---|---|
| `StorageProducer` | `storage` | `Storage` |
| `KvProducer` | `kv_store` | `KVStore` |
| `VectorProducer` | `vector_store` | `VectorStore` |
| `FulltextProducer` | `fulltext_store` | `FulltextStore` |
| `GraphProducer` | `graph_store` | `GraphStore` |
| `FusionProducer` | `fusion_store` | `FusionStore` |
| `FsProducer` | `fs_store` | `FSStore` |
| `EntityStoreProducer` | `entity_store` | `EntityStore` |

Concrete implementations register with `@XxxProducer.register("name")`. `storage.bootstrap.register_backends()` imports the implementation modules and triggers registration. `StorageProducer.resolve(config)` resolves a shared Storage from component configuration and uses `composite` as its default target. Business code should normally use the `Storage` injected by the assembly layer instead of resolving and pinning low-level Stores itself.

## 13. Configurable Implementations

### 13.1 Configuration Structure

Configuration uses a two-level structure: "Producer namespace → named instance →
`target/params`":

```yaml
kv_store:                 # KvProducer.TOP_NAME
  default:                # Named instance that other components can reference
    target: sqlite        # Registered implementation name
    params:               # Implementation parameters and dependency references
      db_path: ./data/memory.db
    new_instance: false   # Optional; false means the named instance is shared
```

An implementation with no parameters can use the string shorthand:

```yaml
kv_store:
  default: memory
```

Dependency parameters support two forms:

- A string such as `storage: default` references a shared named instance in the corresponding
  Producer namespace.
- A mapping such as `raw_kv_store: {target: sqlite, params: {...}}` constructs an anonymous,
  non-shared instance inline.

An ordinary parameter not found in `params` falls back to `globals`. User configuration overrides
built-in defaults by namespace and instance name. When an existing instance is overridden, its
entire `params` mapping is replaced rather than deep-merged, so required dependency references must
not be omitted.

When passed directly to `Config.from_dict()` or `Config.from_yaml()`, these namespaces are top-level
sections. In deployment configuration, place them under `memory_api:`.

Without user configuration, the default Storage composition is
`composite + memory KV/vector/fulltext/graph`; the L0/L1 vector and full-text ports also use
`memory`. FusionStore, FSStore, and EntityStore are not connected to `CompositeStorage` by default.
External backend targets import clients and establish connections lazily. A valid configuration
therefore does not guarantee that the third-party client and service are ready; call `health()`
before deployment.

### 13.2 Storage Implementation

| `target` | Implementation class | Function | Main `params` |
|---|---|---|---|
| `composite` | `CompositeStorage` | Default unified Storage. It combines Store ports, persists the MemoryUnit body through the KV port, and exposes retrieval adapters through configured Recallers. It does not build vector, full-text, or graph projections. | `kv_store` (default `memory`); optional `vector_store`, `fulltext_store`, `graph_store`, `fusion_store`, and `fs_store`; `preferred_retrieval_pipeline` (default `recall_get_rank`) |

`preferred_retrieval_pipeline` accepts `recall_get_rank`, `recall_and_get_rank`, or `retrieve`.
`vector_store.layers_l0/layers_l1` and `fulltext_store.layers_l0/layers_l1` are automatically exposed
by `CompositeStorage` as ports with the same names.

```yaml
storage:
  default:
    target: composite
    params:
      kv_store: default
      vector_store: default
      fulltext_store: default
      graph_store: default
      preferred_retrieval_pipeline: recall_get_rank
```

The only built-in Storage target is currently `composite`. `RoutingStorage` and the routing Stores
are product-level routing components that can be injected manually; they are not currently
registered as a YAML-compatible `target: routing`.

### 13.3 KVStore Implementations

| `target` | Implementation class | Function | Required parameters | Main optional parameters |
|---|---|---|---|---|
| `memory` | `InMemoryKVStore` | In-process KV with TTL support; suitable for default development and tests. Data is lost when the process exits. | None | None |
| `sqlite` | `SQLiteKVStore` | SQLite single-file persistence with the five Scope dimensions stored as isolation columns. | None | `db_path` (default `agent_memory.db`; `":memory:"` selects in-process SQLite) |
| `redis` | `RedisKVStore` | Remote Redis KV that encodes Scope into the key namespace and supports late-bound connection parameters. | `url` | `ssl_verify`, `ssl_ca_cert`; the code still accepts fallback `host`, `port`, `db`, and `password` fields, but the current builder requires `url`, and the URL path takes precedence |
| `postgres` | `PostgresKVStore` | PostgreSQL-backed KV with five-column Scope isolation, TTL, and connection pooling. | `dsn` | `schema`, `table`, `pool_min_size`, `pool_max_size`, `connect_timeout`, `application_name`, `auto_create_schema`, `ssl_verify`, `ssl_ca_cert` |
| `encrypted` | `EncryptedKVStore` | Encryption decorator that transparently encrypts and decrypts data over any raw KV implementation; it does not implement cryptographic algorithms itself. | `raw_kv_store`, `security` | None; `raw_kv_store` cannot reference the encrypted instance itself |

```yaml
kv_store:
  raw:
    target: sqlite
    params:
      db_path: ./data/memory.db
  default:
    target: encrypted
    params:
      raw_kv_store: raw
      security: default

security:
  default:
    target: local
    params:
      key_env: AGENT_MEMORY_ENCRYPTION_ROOT_KEY
      allow_plaintext: false
```

When `ssl_verify=true` for Redis, `url` must use `rediss://` and `ssl_ca_cert` must also be set. For
PostgreSQL, enabling SSL verification internally selects `sslmode=verify-full`.

### 13.4 VectorStore Implementations

| `target` | Implementation class | Function | Required parameters | Main optional parameters |
|---|---|---|---|---|
| `memory` | `InMemoryVectorStore` | In-process exhaustive vector search for tests and small datasets. | None | None |
| `milvus` | `MilvusVectorStore` | Milvus ANN with metadata filtering and metadata returned during recall. | `uri` and a positive `dim` (which may fall back to `globals.embedder_dim`) | `token`, `collection`, `metric_type`, `consistency_level`, `scope_field_max_length`, `id_max_length`, `ssl_verify`, `ssl_ca_cert` |
| `pgvector` | `PgVectorStore` | PostgreSQL + pgvector with HNSW, pushed-down Scope/metadata filtering, and connection pooling. | `dsn` and a positive `dim` (which may fall back to `globals.embedder_dim`) | `schema`, `table`, `metric_type`, `index_type`, `hnsw_m`, `hnsw_ef_construction`, `ef_search`, `max_scan_tuples`, `create_metadata_index`, `pool_min_size`, `pool_max_size`, `connect_timeout`, `application_name`, `auto_create_schema`, `create_extension`, `ssl_verify`, `ssl_ca_cert` |

```yaml
globals:
  embedder_dim: 1024

vector_store:
  default:
    target: milvus
    params:
      uri: http://localhost:19530
      collection: agent_memory_vectors
      dim: 1024
      metric_type: COSINE
  layers_l0:
    target: milvus
    params:
      uri: http://localhost:19530
      collection: agent_memory_vectors_l0
      dim: 1024
      metric_type: COSINE
  layers_l1:
    target: milvus
    params:
      uri: http://localhost:19530
      collection: agent_memory_vectors_l1
      dim: 1024
      metric_type: COSINE
```

The vector dimension must match the Embedder output. Production Retriever MaxP and descending
fusion require higher scores to mean greater relevance. Milvus reports `L2` as a distance, so
`VectorRecaller` rejects it during assembly; use `COSINE` or `IP` in the retrieval pipeline.
`pgvector` converts `L2` distance to a higher-is-better score and therefore supports `COSINE`, `IP`,
and `L2`.

With `ssl_verify=true`, `milvus` sets `secure=True` and `server_pem_path`. The `pgvector` SSL behavior
matches the PostgreSQL KV implementation.

### 13.5 FulltextStore Implementations

| `target` | Implementation class | Function | Required parameters | Main optional parameters |
|---|---|---|---|---|
| `memory` | `InMemoryFulltextStore` | In-process term-hit scoring based on a Tokenizer. | None | `tokenizer` (named reference; default is an anonymous `whitespace` tokenizer) |
| `elasticsearch` | `ElasticsearchFulltextStore` | Elasticsearch document CRUD plus `match`/BM25 search, with pushed-down Scope and FilterExpr. | `hosts` | `index`, `username`, `password`, `api_key`, `text_field`, `text_analyzer`, `refresh`, `ssl_verify`, `ssl_ca_cert` |

```yaml
fulltext_store:
  default:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: agent_memory_fulltext
      text_analyzer: english
  layers_l0:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: agent_memory_fulltext_l0
      text_analyzer: english
  layers_l1:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: agent_memory_fulltext_l1
      text_analyzer: english
```

`text_analyzer` takes effect only when the index is created; changing it requires rebuilding the
index. With `ssl_verify=true`, `hosts` must use `https://` and `ssl_ca_cert` must be configured.

### 13.6 GraphStore Implementations

| `target` | Implementation class | Function | Required parameters | Main optional parameters |
|---|---|---|---|---|
| `memory` | `InMemoryGraphStore` | In-process property graph supporting term-based seed discovery and multi-hop traversal. | None | None |
| `nano_graphrag` | `NanoGraphRAGGraphStore` | Uses nano-graphrag `NetworkXStorage`, with one GraphML namespace per Scope and optional disk persistence. | `working_dir` | `namespace_prefix` (default `agent_memory_graph`), `create_root` (default `true`) |

```yaml
graph_store:
  default:
    target: nano_graphrag
    params:
      working_dir: ./data/graph
      namespace_prefix: agent_memory_graph
```

### 13.7 FusionStore Implementations

| `target` | Implementation class | Function | Required parameters | Main optional parameters |
|---|---|---|---|---|
| `memory` | `InMemoryFusionStore` | In-process combined storage for vectors, terms, scalar filters, and forward values. | None | `tokenizer` (default is an anonymous `whitespace` tokenizer) |
| `milvus_graph` | `MilvusGraphFusionStore` | Stores vectors, scalars, and forward values in Milvus and adjacency data in nano-graphrag; retrieval performs ANN first and then expands through graph relationships. | `uri`, `working_dir`, and a positive `dim` (which may fall back to `globals.embedder_dim`) | `collection`, `metric_type`, `namespace_prefix`, `link_field`, `neighbor_depth`, `neighbor_decay`, `neighbor_relation` |

```yaml
fusion_store:
  default:
    target: milvus_graph
    params:
      uri: http://localhost:19530
      working_dir: ./data/fusion-graph
      dim: 1024
      collection: agent_memory_fusion
      link_field: links
      neighbor_depth: 1
      neighbor_decay: 0.5
```

`milvus_graph` currently focuses on "vector seed → graph-neighbor expansion." It does not use
`FusionQuery.text` or `vector_weight`, so it does not provide BM25 fusion.

### 13.8 FSStore Implementations

| `target` | Implementation class | Function | Required parameters | Main optional parameters |
|---|---|---|---|---|
| `memory` | `InMemoryFSStore` | In-process binary file storage. | None | None |
| `local` | `LocalFSStore` | Local file-system storage under `root/<five Scope segments>/`, with directory-traversal prevention. | `root` | `create_root` (default `true`) |

```yaml
fs_store:
  default:
    target: local
    params:
      root: ./data/assets
      create_root: true
```

### 13.9 EntityStore Implementation

| `target` | Implementation class | Function | Enabling parameter | Main optional parameters |
|---|---|---|---|---|
| `elasticsearch` | `ElasticsearchEntityStore` | Exact entity-hash lookup, reverse MemoryUnit association lookup, and batch mutation. | `hosts` or `endpoint`; if neither is set, the builder returns `None` and silently disables the entity path | `index`, `username`, `password`, `timeout`, `list_limit`, `number_of_shards`, `number_of_replicas`, `ssl_verify`, `ssl_ca_cert` |

```yaml
globals:
  entity_enabled: true

entity_store:
  default:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: memory_entities

# The write path and recall path must reference the same named instance
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
      entity_store: default
recaller:
  keyword:
    target: keyword
    params:
      storage: default
      entity_store: default
```

`entity_enabled=true` is only the global switch. The write-side `constructor.default` and the
recall-side `recaller.keyword` must each reference the same named `entity_store` instance.

### 13.10 How Security Interfaces Are Implemented

`StorageSecurity` currently has no independent Producer namespace. A configuration-built
`CompositeStorage` uses `AllowAllStorageSecurity` by default. A custom authorization implementation
must be supplied in code through `CompositeStorage(..., security=...)` or injected by the product
assembly layer; it cannot be selected directly as a YAML target.

`StoreSecurity` also has no separately selectable target. Ordinary Stores use
`PassthroughStoreSecurity` by default. Selecting `kv_store.target=encrypted` makes
`EncryptedKVStore.security` report enabled, while the actual cryptographic implementation is
provided by the `SecurityProvider` referenced by `params.security`.

## 14. Minimal Usage Examples

```python
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.storage.storage import Storage
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode


def save_and_load(storage: Storage, scope: Scope, unit: MemoryUnit) -> MemoryUnit | None:
    storage.add(scope, [unit], mode=IndexWriteMode.ALL)
    loaded = storage.get(scope, [unit.id])
    return loaded[0] if loaded else None


def remove_from_retrieval(storage: Storage, scope: Scope, unit_id: str) -> None:
    storage.delete(scope, [unit_id], mode=IndexRemoveMode.SOFT)
```

These examples express only the interface semantics. The physical data affected by `ALL` or `SOFT` depends on the injected `Storage` implementation and its capabilities.

## 15. Storage Data Types

The types in this section do not carry Scope. Scope is the explicit first argument of Store methods
and should not be duplicated in `metadata`, `filters`, or record bodies.

### 15.1 Common Result Types

| Type.field | Type | Default/required | Semantics |
|---|---|---|---|
| `ScoredID.id` | `str` | Required | Logical ID within a Scope |
| `ScoredID.score` | `float` | Required | Relevance score; the current retrieval path expects higher scores first |
| `ScoredID.metadata` | `dict[str, Any] \| None` | `None` | Optional hit metadata |
| `ScoredHit.id` | `str` | Required | Hit ID |
| `ScoredHit.score` | `float` | Required | Relevance score |
| `ScoredHit.metadata` | `dict[str, Any]` | `{}` | Payload optionally returned by `VectorStore.recall` |
| `KVMemoryListResult.entries` | `list[tuple[str, bytes]]` | `[]` | Raw KV entries on the current page |
| `KVMemoryListResult.count` | `int` | `0` | Total matches before pagination |
| `MemoryListResult.items` | `list[MemoryUnit]` | `[]` | Domain objects on the current page |
| `MemoryListResult.count` | `int` | `0` | Total matches before pagination |

### 15.2 Vector and Fulltext

| Type | Fields (type; default) | Constraints |
|---|---|---|
| `VectorRecord` | `id: str`; `vector: list[float]`; `metadata: dict[str, Any]={}` | Vector dimension must match the backend index; ID is unique within Scope |
| `VectorQuery` | `vector: list[float]`; `top_k: int=10`; `filters: FilterExpr \| None=None`; `return_metadata: bool=false` | `filters` is normalized at construction; `top_k` should be positive |
| `Document` | `id: str`; `text: str`; `metadata: dict[str, Any]={}` | ID is unique within Scope |
| `TextQuery` | `text: str`; `top_k: int=10`; `filters: FilterExpr \| None=None` | `filters` is normalized at construction; `top_k` should be positive |

`VectorStore.search()` returns `ScoredID`. Optional `VectorStore.recall()` can return
`ScoredHit.metadata` in the same ANN request. When the backend does not implement it, the base class
raises `NotImplementedError` and `VectorRecaller` falls back to `search + get`.

### 15.3 Graph, Fusion, and FS

| Type | Fields (type; default) | Constraints |
|---|---|---|
| `Node` | `id: str`; `label: str=""`; `properties: dict[str, Any]={}` | ID is unique within Scope |
| `Edge` | `id: str`; `source: str`; `target: str`; `relation: str=""`; `properties: dict[str, Any]={}` | source/target are node IDs |
| `GraphQuery` | `start_id: str`; `relation: str \| None=None`; `depth: int=1`; `limit: int=100` | `relation=None` does not restrict relationship type |
| `FusionRecord` | `id: str`; `vector: list[float] \| None=None`; `text: str \| None=None`; `scalars: dict[str, Any]={}`; `value: bytes \| None=None` | May provide only a subset of modality fields |
| `FusionQuery` | `vector: list[float] \| None=None`; `text: str \| None=None`; `scalar_filters: FilterExpr \| None=None`; `top_k: int=10`; `vector_weight: float=0.5` | `vector_weight` is the vector-score weight; a backend may support only a subset |
| `FileStat` | `ref: str`; `size: int`; `content_type: str=""`; `created_at/updated_at: float=0.0` | Times are Unix seconds |

### 15.4 EntityStore Types

| Type | Fields | Semantics |
|---|---|---|
| `EntityStoreFilters` | `actor_id: str \| None=None` | User-isolation condition in addition to `space_id` |
| `EntityMention` | `entity_type: str`, `display_name: str`, `normalized_name: str` | Normalized entity mention extracted from memory or query text |
| `EntityRecord` | `id`, `space_id`, `entity_text`, `entity_type`, `linked_memory_ids: tuple[str, ...]`, `filters`, `entity_text_hash=""` | Reverse index from an entity to MemoryUnit IDs |
| `EntityLinkResult` | `extracted_count/inserted_count/updated_count/deleted_count/failed_count/skipped_count: int=0` | Write statistics from the entity linker, not per-item results from the EntityStore batch API |
| `EntityOperation` | `type: EntityOpType`, `record: EntityRecord \| None=None`, `record_id: str \| None=None`, `link_memory_ids: tuple[str, ...]=()` | `INSERT/LINK/UNLINK_UPDATE/DELETE` batch command |
| `EntityBatchResult` | `successful_ids: list[str]`, `failed_ids: list[str]` | Per-item partial-success result; one item failure need not fail the whole batch |

## 16. Complete Store Method Signatures

### 16.1 KVStore

```python
insert(scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None
update(scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None
delete(scope: Scope, key: str) -> None
get(scope: Scope, key: str) -> bytes
mget(scope: Scope, keys: list[str]) -> list[bytes]
exists(scope: Scope, key: str) -> bool
scan(scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]
list(
    scope: Scope,
    *,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    filters: FilterExpr | None = None,
    extensions: dict[str, str] | None = None,
) -> KVMemoryListResult
scopes() -> list[Scope]
```

`ttl` is measured in seconds; `0` means no expiration. `mget` preserves input order and duplicate
keys and returns one value per key; any missing key raises `NotFoundError`. Ordering from `scan` and
`scopes` is implementation-defined and callers must not depend on it.

### 16.2 VectorStore, FulltextStore, and FusionStore

```python
# VectorStore
insert(scope: Scope, records: list[VectorRecord]) -> None
update(scope: Scope, records: list[VectorRecord]) -> None
delete(scope: Scope, ids: list[str]) -> None
get(scope: Scope, ids: list[str]) -> list[VectorRecord]
search(scope: Scope, query: VectorQuery) -> list[ScoredID]
recall(
    scope: Scope,
    query: VectorQuery,
    output_fields: list[str] | None = None,
) -> list[ScoredHit]
score_higher_is_better() -> bool

# FulltextStore
insert(scope: Scope, docs: list[Document]) -> None
update(scope: Scope, docs: list[Document]) -> None
delete(scope: Scope, ids: list[str]) -> None
get(scope: Scope, ids: list[str]) -> list[Document]
search(scope: Scope, query: TextQuery) -> list[ScoredID]

# FusionStore
insert(scope: Scope, records: list[FusionRecord]) -> None
update(scope: Scope, records: list[FusionRecord]) -> None
delete(scope: Scope, ids: list[str]) -> None
get(scope: Scope, ids: list[str]) -> list[FusionRecord]
search(scope: Scope, query: FusionQuery) -> list[ScoredID]
```

Batch `get` on all three Stores returns only records that actually exist and is not positionally
aligned with input IDs. Built-in `search` methods return higher scores first. A distance-based
VectorStore must report score direction through `score_higher_is_better()`; current
`VectorRecaller` rejects direct assembly with a lower-is-better Store.

### 16.3 GraphStore, FSStore, and EntityStore

```python
# GraphStore
seed_ids(scope: Scope, tokens: set[str]) -> list[str]
insert(
    scope: Scope,
    nodes: list[Node] | None = None,
    edges: list[Edge] | None = None,
) -> None
update(
    scope: Scope,
    nodes: list[Node] | None = None,
    edges: list[Edge] | None = None,
) -> None
delete(
    scope: Scope,
    node_ids: list[str] | None = None,
    edge_ids: list[str] | None = None,
) -> None
get(scope: Scope, node_ids: list[str]) -> list[Node]
search(scope: Scope, query: GraphQuery) -> list[Node]

# FSStore
insert(scope: Scope, key: str, data: BinaryIO) -> str
update(scope: Scope, ref: str, data: BinaryIO) -> str
delete(scope: Scope, ref: str) -> None
get(scope: Scope, ref: str) -> BinaryIO
stat(scope: Scope, ref: str) -> FileStat

# EntityStore (routes by space_id and is not one of the six Storage capabilities)
ensure_index() -> None
find_by_entity_text_hash(
    space_id: str,
    entity_text_hashes: tuple[str, ...],
    *,
    filters: EntityStoreFilters,
    limit: int = 500,
) -> list[EntityRecord]
find_by_linked_memory_id(
    space_id: str,
    memory_id: str,
    *,
    filters: EntityStoreFilters,
) -> list[EntityRecord]
execute_operations(
    space_id: str,
    operations: list[EntityOperation],
) -> EntityBatchResult
```

An FS `ref` is the canonical reference returned by the Store; callers must not construct a physical
path themselves. GraphStore `seed_ids` only locates traversal entry points; the backend defines its
token-matching strategy.

## 17. CRUD, Batch, and Exception Contracts

| Scenario | Standard result |
|---|---|
| `(scope, id/key)` already exists for `insert` | `ConflictError` |
| `(scope, id/key)` is absent for `update` | `NotFoundError` |
| Deletion target is absent | Idempotent success; no missing-item exception |
| KV/FS single-item `get` target is absent | `NotFoundError` |
| Any key in KV `mget` is absent | `NotFoundError`; no partial list is returned |
| Some IDs in a retrieval Store batch `get` are absent | Missing items are omitted and existing records are returned |
| `MemoryUnit.scope` differs from the explicit Scope passed to `Storage.add/update` | Built-in `CompositeStorage` raises `ValidationError` |
| Filter operator, vector dimension, query, or configuration is invalid | `ValidationError` |
| An undeclared capability or named port is accessed | `UnsupportedStorageCapabilityError` |
| `StorageSecurity.authorize` denies access | `PermissionDeniedError` |
| External backend connection fails, times out, or is unavailable | `BackendError` |
| Some of multiple recall sources fail | Successful batches plus `ChannelError` |
| Every selected recall source fails | `StorageRetrievalError` |

The standard interfaces do not require global atomicity across batch items or multiple Stores. If
an implementation relies on a backend batch API, its own documentation and tests should state
whether the operation is all-or-nothing or may partially succeed. A caller must not infer a
cross-backend transaction from a `None` return value.

## 18. Storage Modes and Implementation Capability Matrix

`IndexWriteMode` and `IndexRemoveMode` are logical requirements on the Storage interface. They do
not imply that every Storage implementation has built-in index-projection capability.

| Combination | `ALL` | `FORWARD_ONLY` | `RETRIEVAL_ONLY` | `SOFT` | `HARD` |
|---|---|---|---|---|---|
| Direct CRUD through `CompositeStorage` | Writes/updates the KV memory record | Writes/updates the KV memory record | No-op | No-op; record remains | Deletes the KV memory record |
| `HybridIndexBuilder + CompositeStorage` | forward + fulltext + vector + optional entity | Runs forward only | Runs retrieval sub-builders only | Removes derived retrieval indexes only | Removes derived indexes first, then forward |
| `UnifiedIndexBuilder + CompositeStorage` | KV memory record only | KV memory record only | No-op | No-op | Deletes the KV memory record |
| `UnifiedIndexBuilder + custom unified Storage` | Storage implements the complete write | Must preserve at least the source record | Implementation decides whether indexes can be backfilled independently | Implementation must preserve the source record | Implementation deletes the record and derived indexes |

Therefore, the abstract `Storage` methods alone do not prove that a selected target manages vector,
full-text, and graph indexes together. Index-projection capability must be evaluated from both the
Storage implementation and its paired IndexBuilder.

## 19. Backend Selection Summary

| Store | In-process target | Persistent/remote target | Key constraints |
|---|---|---|---|
| KV | `memory` | `sqlite`, `redis`, `postgres`; `encrypted` wraps any raw KV | Redis builder requires `url`; Postgres requires `dsn`; TTL is in seconds |
| Vector | `memory` | `milvus`, `pgvector` | `dim` must match Embedder; retrieval requires higher scores first |
| Fulltext | `memory` | `elasticsearch` | Rebuild indexes after changing the analyzer |
| Graph | `memory` | `nano_graphrag` | External implementation creates a separate GraphML namespace per Scope |
| Fusion | `memory` | `milvus_graph` | Current `milvus_graph` is vector seeding + graph expansion and does not implement BM25 text fusion |
| FS | `memory` | `local` | LocalFS stores under `root/<scope>/` and prevents directory traversal |
| Entity | None | `elasticsearch` | Independent Producer; not one of the six StorageCapability ports |

Connection-backed implementations usually establish a real connection only on first access or
`health()`. Successful configuration construction does not prove that the remote service,
schema/index, or TLS path is ready; deployment acceptance should call `health()` explicitly.
