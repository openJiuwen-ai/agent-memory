# Configuration and Assembly Guide

Agent Memory configuration is more than a way to pass YAML fields to a constructor. It is a
lightweight assembly mechanism built around a default topology, Producer registries, named
instances, and dependency references. This guide explains how configuration is organized,
merged, resolved, and assembled, as well as which fields support runtime late binding.

This document reflects the current code. The following source files are authoritative:

- [`config.py`](../../../jiuwen_memory/config/config.py)
- [`context.py`](../../../jiuwen_memory/config/context.py)
- [`defaults.py`](../../../jiuwen_memory/config/defaults.py)
- [`config_source.py`](../../../jiuwen_memory/config/config_source.py)
- [`project.py`](../../../jiuwen_memory/config/project.py)
- [`active.py`](../../../jiuwen_memory/config/active.py)
- [`binding.py`](../../../jiuwen_memory/config/binding.py)
- [`routing.py`](../../../jiuwen_memory/config/routing.py)
- [`factory.py`](../../../jiuwen_memory/common/factory/factory.py)
- [`assembly.py`](../../../jiuwen_memory/api/memory_api_impl/assembly.py)
- [`config_loader.py`](../../../jiuwen_memory_entry/core/config_loader.py)
- [`profiles.py`](../../../jiuwen_memory_entry/core/profiles.py)
- [`server.py`](../../../jiuwen_memory_entry/core/server.py)

## 1. Configuration Model Overview

Configuration falls into four main categories:

| Category | Purpose | Primary activation time |
|---|---|---|
| Assembly topology | Select component implementations, declare instances, and connect dependencies | `assemble()` |
| Global parameters | Provide capability flags and shared parameters to multiple components | Primarily during `assemble()` |
| `ConfigSource` | Late-bind models, credentials, connection addresses, and Prompts | Runtime call path |
| `PolicyManager` | Manage lifecycle, Space, and other business policies | Runtime |

The core configuration structure is:

```text
Namespace
  └── Named instance
        ├── target
        ├── params
        └── new_instance
```

For example:

```yaml
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
```

In this example:

- `constructor` is `IndexBuilderProducer.TOP_NAME` and identifies the IndexBuilder configuration namespace.
- `default` is the named instance.
- `hybrid` is an implementation target registered with `IndexBuilderProducer`.
- `storage: default` references `storage.default`.
- `chunker: default` references `chunker.default`.
- `embedder: default` references `embedder.default`.

Therefore, `target` means "which implementation is selected for this abstract interface." It is
not a constructor parameter.

## 2. Complete Assembly Flow

Both SDK and service deployments eventually enter `assemble()`:

```text
SDK
  Config.from_dict / Config.from_yaml
                 ┬
                 ├─> assemble
                 │     ├─ Register every Producer target
HTTP / MCP       │     ├─ Clear the named-instance cache for this assembly
  load_layer     │     ├─ Merge default_context with user overrides
  -> load_config │     ├─ Assemble ConfigSource first
  -> memory_api ─┴     ├─ Assemble KV / Storage / IngestJob
                       ├─ Recursively assemble Engine and its dependencies from root references
                       └─ Return Kernel
```

`Kernel` currently contains:

| Field | Purpose |
|---|---|
| `api` | Surface-independent `MemoryAPI` entry point |
| `kv` | Source-of-truth KV handle assembled from `kv_store.default` |
| `storage` | Unified Storage used by upper layers; defaults to `CompositeStorage` |
| `ingest_jobs` | Controller for long-running ingestion jobs |
| `space` | SpaceManager |
| `config_source` | Runtime late-binding configuration source |

Components are not created in the order in which they appear in YAML. Assembly starts from the
root references in `ROOT_PARAMS` and follows each builder's `dep()` calls recursively. A named
instance that is not referenced by a root component or another component is not created
automatically.

## 3. Configuration Entry Points

### 3.1 SDK

The SDK can construct kernel configuration directly:

```python
from jiuwen_memory.api import assemble
from jiuwen_memory.config import Config

config = Config.from_dict(
    {
        "globals": {
            "vector_enabled": True,
            "graph_enabled": False,
        },
        "kv_store": {
            "default": {
                "target": "sqlite",
                "params": {
                    "db_path": "./data/memory.db",
                },
            }
        },
    }
)

api = assemble(config=config)
```

It can also read a YAML file that contains kernel configuration only:

```python
config = Config.from_yaml("./memory-config.yml")
api = assemble(config=config)
```

`Config.from_yaml()` only parses YAML; it does not expand `${ENV_VAR}`. In an SDK deployment, the
caller must read environment variables or construct the configuration dictionary in advance.

### 3.2 HTTP, MCP, and Deployment Configuration

Deployment configuration adds a service-level wrapper around the kernel configuration:

```yaml
profile: docker

policies:
  scope.require_space: "true"

memory_api:
  globals:
    vector_enabled: true
    graph_enabled: false

  kv_store:
    default:
      target: redis
      params:
        url: "${REDIS_URL:-redis://redis:6379/0}"
```

| Field | Purpose |
|---|---|
| `profile` | Service startup profile; not a kernel component namespace |
| `policies` | Convenience policy configuration passed to `PolicyManager` |
| `memory_api` | Kernel configuration actually passed to `Config.from_dict()` and `assemble()` |

HTTP/MCP startup performs the following steps:

1. Read YAML or JSON.
2. Expand `${VAR}` and `${VAR:-default}`.
3. Merge the service-level configuration layers.
4. Select only the `memory_api` section.
5. Construct the kernel `Config`.
6. Call `assemble()`.

Do not pass a complete deployment configuration containing `profile`, `policies`, and
`memory_api` directly to the kernel `Config`. Fields such as `profile` would be treated as unknown
Producer namespaces.

## 4. Basic Configuration Structure

### 4.1 `globals`

`globals` stores shared parameters used across components:

```yaml
globals:
  vector_enabled: true
  graph_enabled: false
  rerank_enabled: true
  layers_index_enabled: true

  embedder_dim: 1024
  chunk_size: 512

  llm_model: qwen-plus
  llm_base_url: https://example.com/v1
  llm_api_key: ${LLM_API_KEY}
```

Components read parameters through `config.get(key, default)` with the following precedence:

```text
Current instance params
  > globals
  > implementation default
```

For example:

```yaml
globals:
  embedder_dim: 1024

embedder:
  default:
    target: hashing
    params:
      tokenizer: default
      embedder_dim: 768
```

When the Hashing Embedder builder reads `embedder_dim`, the instance-level value `768` takes
precedence over the global value.

A parameter takes effect only if a builder explicitly reads it. `globals` is not a universal
configuration center that automatically injects arbitrary fields.

### 4.2 `prompts`

Prompts use a dedicated top-level section:

```yaml
prompts:
  extract:
    preference: "Extract user preferences and return the agreed JSON format."
  consolidate:
    preference: "Decide whether the candidate should be added, updated, superseded, or ignored."
  reflect:
    preference: "Review the candidate before persistence."
```

During assembly, `prompts` is placed in `AssemblyContext.globals["prompts"]` for use by
`PromptRegistry`.

Callers normally pass only a Prompt key:

```python
system_metadata = {
    "_extract_prompt_preference": "preference",
}
```

Business APIs should not pass complete Prompt text.

### 4.3 Component Namespaces

Each Producer uses a unique `TOP_NAME` as its configuration namespace:

```yaml
kv_store:
  default:
    target: redis
    params:
      url: redis://localhost:6379/0

vector_store:
  default:
    target: milvus
    params:
      uri: http://localhost:19530
      dim: 1024

constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
```

Common namespaces include:

| Category | Namespaces |
|---|---|
| Shared components | `tokenizer`, `chunker`, `embedder`, `llm`, `reranker` |
| Storage | `storage`, `kv_store`, `vector_store`, `fulltext_store`, `graph_store` |
| Construction | `extractor`, `classifier`, `constructor`, `dedup`, `evolver` |
| Retrieval | `query_parser`, `recaller`, `fuser`, `discloser`, `retriever` |
| Control | `engine`, `scheduler`, `permission`, `policy`, `lifecycle` |

See the API reference for each layer and the registered Producer targets for the available target
values.

## 5. Named Instance Structure

### 5.1 Full Form

```yaml
kv_store:
  default:
    target: redis
    params:
      url: redis://localhost:6379/0
    new_instance: false
```

| Field | Required | Purpose |
|---|---:|---|
| `target` | Yes | Implementation name registered with the Producer |
| `params` | No | Implementation parameters and dependency references |
| `new_instance` | No | Whether to bypass the named-instance cache; defaults to `false` |

### 5.2 Shorthand Form

When an instance has no parameters, the target can be written directly:

```yaml
kv_store:
  default: memory

fuser:
  default: rrf
```

This is equivalent to:

```yaml
kv_store:
  default:
    target: memory

fuser:
  default:
    target: rrf
```

## 6. Dependency References

A component builder resolves dependencies through the corresponding Producer's `dep()` method.

### 6.1 Named References

A string references a named instance in another namespace:

```yaml
storage:
  default:
    target: composite
    params:
      kv_store: default
      vector_store: default
```

During assembly, these references resolve to:

```text
KvProducer.build_named("default")
VectorProducer.build_named("default")
```

References to the same name under the same Producer hit the same instance cache. This is the main
mechanism by which Construction, Retrieval, and Control share the same Storage, Embedder, and
Tokenizer instances.

### 6.2 Anonymous Inline Instances

A mapping creates an anonymous instance:

```yaml
query_parser:
  default:
    target: simple
    params:
      tokenizer:
        target: whitespace
      llm:
        target: echo
```

An anonymous instance:

- Does not need to be declared in the corresponding namespace in advance.
- Is not stored in the named-instance cache.
- Is created again every time the dependency is resolved.
- Is unsuitable for a Store, Embedder, or Tokenizer whose state must be shared across components.

### 6.3 Default Dependencies

Suppose a builder calls:

```python
EmbedderProducer.dep(config, default="hashing")
```

If `params` does not configure `embedder`, the system creates an anonymous `hashing` Embedder.

This keeps the default path runnable, but it may bypass a user-declared `embedder.default`.
Therefore, when overriding an instance with several dependencies, explicitly retain every named
dependency that should still be used.

### 6.4 A String Is Not Always a Dependency

A string in `params` represents a named reference only when a builder reads it through a
Producer's `dep()` method.

```yaml
params:
  storage: default
  collection: agent_memory_vectors
```

- `storage` is read by `StorageProducer.dep()` or `StorageProducer.resolve()` and is a named reference.
- `collection` is read by `config.get()` and is an ordinary string.

The configuration parser does not infer whether a string is a dependency from its contents.

## 7. Instance Sharing

Named instances are shared by default:

```yaml
embedder:
  default:
    target: openai
```

If both Construction and Retrieval reference:

```yaml
params:
  embedder: default
```

both receive the same `embedder.default` instance.

The default configuration uses the same sharing mechanism:

```text
Kernel
  ├── storage.default
  │    ├── kv_store.default
  │    ├── vector_store.default
  │    ├── fulltext_store.default
  │    └── graph_store.default
  │
  └── engine.default
       ├── constructor.default -> storage.default
       ├── retriever.default   -> storage.default
       ├── evolver.default     -> storage.default
       └── lifecycle.default   -> storage.default
```

By default:

- Kernel, Engine, and Retriever use the same `Storage` instance.
- Construction and Retrieval use the same underlying Embedder.
- Writes and recalls use the same set of Stores.
- Store ports exposed by `CompositeStorage` may be authorization proxies, but each proxy still
  delegates to the corresponding named Store.

### 7.1 `new_instance`

```yaml
llm:
  isolated:
    target: openai
    new_instance: true
    params:
      llm_model: qwen-plus
```

Every reference to `llm.isolated` creates a new instance and does not populate the named-instance
cache.

Use this setting only when a component explicitly requires instance isolation. Applying
`new_instance: true` to a stateful Store can cause writers and readers to connect to different
instances.

## 8. Default Configuration and User Overrides

`assemble()` first creates the built-in default configuration and then merges user
configuration over it.

The default configuration is an in-process stack that can run offline:

```text
kv_store.default       -> memory
vector_store.default   -> memory
fulltext_store.default -> memory
graph_store.default    -> memory
storage.default        -> composite

embedder.default       -> hashing
llm.default            -> echo
constructor.default    -> hybrid
retriever.default      -> pipeline
engine.default         -> in_memory
```

Users normally declare only the parts that differ from the defaults:

```yaml
kv_store:
  default:
    target: redis
    params:
      url: redis://redis:6379/0

vector_store:
  default:
    target: milvus
    params:
      uri: http://milvus:19530
      dim: 1024
```

The default `storage.default` already references `kv_store.default` and `vector_store.default`.
Replacing the Store targets is therefore sufficient; the complete Storage configuration does not
need to be declared again.

## 9. Merge Rules

### 9.1 Kernel Configuration Merge

Built-in and user configuration are merged as follows:

- `globals`: overridden by field.
- Namespace: named instances are overridden or added by instance name.
- Same named instance: the complete `RawSpec` is replaced.
- `params` inside the same named instance: not deep-merged.

For example, the default configuration contains:

```yaml
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
```

The user configuration contains:

```yaml
constructor:
  default:
    target: hybrid
    params:
      layers_index_enabled: false
```

After the merge, the three default dependency references are gone and only
`layers_index_enabled` remains. The Hybrid builder may fall back to anonymous default
Chunker/Embedder instances, bypassing user-configured named instances.

The recommended form is:

```yaml
constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
      layers_index_enabled: false
```

If a parameter is a shared capability flag, placing it in `globals` is preferable:

```yaml
globals:
  layers_index_enabled: false
```

This avoids replacing `constructor.default` entirely.

### 9.2 Merging Multiple Deployment Files

HTTP/MCP can load several configuration files in sequence, but the service-level configuration
uses only a shallow top-level merge:

```text
base.yml
  memory_api: {...}

prod.yml
  memory_api: {...}
```

The later `prod.yml.memory_api` replaces `base.yml.memory_api` as a whole. Fields inside the two
`memory_api` sections are not merged.

Therefore:

- Do not split one kernel configuration across several deployment files.
- Multiple files are better suited to overriding service-level fields such as `profile` and
  `policies`.
- Keep kernel-specific differences together in a single `memory_api` section.

## 10. Backend Configuration Example

The following configuration uses Redis, Milvus, and Elasticsearch while disabling graph and
layered indexes:

```yaml
profile: docker

memory_api:
  globals:
    vector_enabled: true
    graph_enabled: false
    rerank_enabled: true
    layers_index_enabled: false
    embedder_dim: 1024
    chunk_size: 512

    llm_model: qwen-plus
    llm_base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    llm_api_key: "${LLM_API_KEY}"

    embedder_model: bge-m3
    embedder_base_url: "${EMBEDDER_BASE_URL}"
    embedder_api_key: "${MODEL_API_TOKEN}"

  llm:
    default:
      target: dashscope

  embedder:
    default:
      target: openai

  tokenizer:
    default:
      target: jieba

  kv_store:
    default:
      target: redis
      params:
        url: "${REDIS_URL:-redis://redis:6379/0}"

  vector_store:
    default:
      target: milvus
      params:
        uri: "${MILVUS_URI:-http://milvus:19530}"
        collection: agent_memory_vectors
        dim: 1024
        metric_type: COSINE

  fulltext_store:
    default:
      target: elasticsearch
      params:
        hosts: "${ES_HOSTS:-http://elasticsearch:9200}"
        index: agent_memory_fulltext
        text_analyzer: english
```

The following components are not explicitly overridden and continue to use the default topology:

- `storage.default=composite`
- `constructor.default=hybrid`
- `retriever.default=pipeline`
- `engine.default=in_memory`
- `scheduler.default=in_process`

Through named references, these components automatically use the overridden Redis, Milvus,
Elasticsearch, and model instances.

## 11. Multiple Named Instances

A namespace can declare several named instances:

```yaml
llm:
  default:
    target: dashscope
    params:
      llm_model: qwen-plus

  reasoning:
    target: openai
    params:
      llm_model: reasoning-model

extractor:
  default:
    target: llm
    params:
      llm: reasoning

classifier:
  default:
    target: llm
    params:
      llm: default
```

The assembled references are:

```text
Extractor  -> llm.reasoning
Classifier -> llm.default
```

Adding a named instance does not activate it automatically. Another component must reference it
explicitly. Likewise, registering a target does not mean that the implementation has been
assembled.

## 12. Runtime ConfigSource

`Config` and `AssemblyContext` determine the assembly topology. After that topology has been
constructed, selected configuration values can be late-bound through `ConfigSource`.

### 12.1 Default `yaml_defaults`

```yaml
config_source:
  default:
    target: yaml_defaults
```

This implementation projects the merged assembly configuration into a read-only `key -> str`
snapshot.

Common keys include:

```text
globals.vector_enabled
prompts.extract.preference
llm.model
llm.api_key
llm.base_url
embedder.model
embedder.api_key
embedder.base_url
kv_store.url
vector_store.uri
fulltext_store.hosts
```

It does not watch the YAML file for changes. After modifying the file, call `assemble()` again.

### 12.2 Mutable `dict`

```yaml
config_source:
  default:
    target: dict
    params:
      values:
        llm.model: qwen-plus
        llm.api_key: initial-key
        llm.base_url: https://example.com/v1
```

After assembly, product-side management code can update wired late-bound fields.
Public `assemble()` / `assemble_runtime()` do not return a `config_source` handle.
With the `dict` target, the deployer holds the same `DictConfigSource` instance and
calls `put`, instead of taking a port off Kernel.

```python
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource

source.put("llm.model", "qwen-max")
source.put("llm.api_key", "new-key")
```

The next supported LLM call reads the new values.

`put()` is a configuration-center or product-management capability. It is not a business argument
to `MemoryAPI.add/search/evolve`.

### 12.3 `overlay`

`overlay` checks the primary source first and uses the fallback when the key is missing:

```yaml
config_source:
  mutable:
    target: dict
    params:
      values:
        llm.model: qwen-max

  snapshot:
    target: yaml_defaults

  default:
    target: overlay
    params:
      primary: mutable
      fallback: snapshot
```

This structure can overlay dynamic values from a configuration center on top of a YAML/defaults
snapshot.

The built-in `dict` target also loads the current `AssemblyContext` projection when it is created,
then applies `params.values` as overrides. The primary source in the example therefore already
contains most projected YAML keys. The fallback mainly handles keys that are genuinely absent
from the primary; it does not turn `dict` into a sparse source containing only `values`.

### 12.4 Current Late-Binding Scope

The main configuration fields currently connected to runtime late binding are:

- `model`, `api_key`, and `base_url` for LLM, Embedder, and Reranker.
- Connection fields for Redis, PostgreSQL, Milvus, Elasticsearch, FS, and related backends.
- Dynamic Prompts.

The following remain primarily assembly-time configuration:

- `target`
- The number of named instances
- Component dependency relationships
- Pipeline topology flags such as `vector_enabled`, `graph_enabled`, and `rerank_enabled`
- The internal port composition of `CompositeStorage`

Even if `globals.vector_enabled` exists in `ConfigSource`, changing it does not automatically add
or remove an already assembled Recaller/IndexBuilder. Such changes require reassembly.

### 12.5 `*.active`

The code can resolve keys such as `embedder.active`, `kv_store.active`, and `storage.active` to
select dynamically among preassembled instances.

However, the default YAML assembly does not register a generic `target: routing`. `*.active`
actually switches instances only when product code explicitly constructs and injects wrappers such
as `RoutingEmbedder` or `RoutingStorage`.

To switch an implementation in ordinary configuration:

1. Change the corresponding `default.target`.
2. Run `assemble()` again.

## 13. Boundary Between PolicyManager and ConfigSource

The following values are business policies and belong in `PolicyManager`:

```yaml
policies:
  rerank.enabled: "true"
  lifecycle.expired_active.target: "forgotten"
  lifecycle.superseded.target: "forgotten"
  scope.require_space: "true"
```

They can also be placed in kernel `globals.policies`:

```yaml
memory_api:
  globals:
    policies:
      rerank.enabled: "true"
      lifecycle.expired_active.target: "forgotten"
      lifecycle.superseded.target: "forgotten"
      scope.require_space: "true"
```

The following are technical component configuration and are suitable for `ConfigSource`:

- Models and credentials
- Store addresses
- Prompts
- Runtime endpoints
- Active selection among assembled instances

PolicyManager and ConfigSource are independent mechanisms and should not be mixed.

`rerank.enabled` and `globals.rerank_enabled` are also different settings. The former is a known
key in `PolicyManager`; the latter determines whether `PipelineRetriever` injects a Reranker during
assembly. In the current implementation, changing `rerank.enabled` at runtime does not rebuild the
retrieval pipeline automatically.

## 14. Configuration Validation and Error Timing

| Error | Detection time |
|---|---|
| Unknown top-level namespace | When `Config` is converted to `AssemblyContext` |
| Named instance without `target` | During configuration parsing |
| Reference to a missing named instance | While recursively assembling that dependency |
| Unregistered target | When the Producer builds that instance |
| Missing required parameter for Redis, Milvus, or another backend | When the corresponding builder is assembled |
| External service cannot be reached | On first connection or during `health()` |
| `*.active` references an instance that was not preassembled | When a Routing component resolves the active instance |

Assembly follows dependencies recursively from root components. A named instance that is not
referenced by any component is normally not built, so an invalid target or a missing backend
dependency in that instance may not surface during startup.

Deployment validation should explicitly call `health()` on assembled components that expose
health checks. A successful `assemble()` call alone does not prove that external services are
available.

## 15. Recommendations and Common Issues

1. Override `*.default` unless multiple named instances are genuinely required.
2. Use named references, not anonymous inline instances, for stateful Stores, Embedders, and
   Tokenizers.
3. When overriding a named instance, include every `params` dependency that must be preserved.
4. Put cross-component capability flags in `globals`.
5. Pass only the `memory_api` section of deployment configuration to the kernel.
6. Remember that SDK `Config.from_yaml()` does not expand environment variables.
7. Do not split `memory_api` across multiple deployment files.
8. Prefer `ConfigSource` for runtime credential or connection-address changes.
9. Reassemble after changing a target, dependency topology, or instance count.
10. Do not use the string `"false"` as the Boolean value `false`.

The last point is particularly important. The configuration system does not convert every string
to a Boolean uniformly, and some builders use Python truth-value testing directly:

```yaml
globals:
  vector_enabled: "false"  # A non-empty string may be treated as True
```

Write this instead:

```yaml
globals:
  vector_enabled: false
```

When an environment variable controls a Boolean flag, the caller must convert it explicitly or
confirm that the corresponding builder uses a shared Boolean parser.

## 16. Summary of the Current Design

The current configuration approach can be summarized as:

```text
Default object topology
  + User overrides by named instance
  + Producer target registration
  + String dependency references
  + Named-instance cache sharing
  + ConfigSource runtime late binding
```

Its main characteristics are:

- The default configuration runs offline.
- A new implementation only needs to register a target.
- Backend and algorithm implementations can be replaced independently.
- Named references ensure that Construction and Retrieval share components.
- No central factory containing every implementation is required.

Users must understand that:

- Configuration contains implementation selection, dependency injection, and ordinary parameters.
- `params` for the same named instance are not deep-merged.
- A string may be either an ordinary value or an instance reference.
- Some errors surface only during recursive assembly or the first connection.
- `ConfigSource` can dynamically change only fields that are already wired for late binding; it
  cannot rebuild the object topology automatically.
- Factory named-instance caches are process-level class variables. Do not run multiple
  `assemble()` calls concurrently in the same process.
