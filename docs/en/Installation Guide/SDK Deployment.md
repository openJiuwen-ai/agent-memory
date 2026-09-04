# SDK Deployment

Last revised: 2026-09-05

SDK deployment means installing and assembling `jiuwen_memory` in the consumer's Python process.
The process can call `MemoryAPI` directly or run a local HTTP service. Storage can use the default
in-process implementations or real backends started with Docker.

> `jiuwen_memory_entry/sdk/__init__.py` is currently empty, and the project does not yet provide a separately
> packaged SDK client. This document uses `jiuwen_memory.api.assemble()` and `MemoryAPI` as the
> current stable Python integration entry points.

## 1. Deployment Combinations

| Combination | Application location | Storage location | Persistent | Suitable scenarios |
|---|---|---|---|---|
| Memory + MemoryAPI | Python process | Same process | No | Unit tests, development, and minimal feature validation |
| Memory + local HTTP | Local HTTP process | Same process | No | HTTP protocol testing and quick cross-language integration |
| Docker backends + MemoryAPI | Python process | Docker containers | Yes | Embedded Python integration without an HTTP hop |
| Docker backends + local HTTP | Local HTTP process | Docker containers | Yes | A persistent local service for cross-process or cross-language access |

```text
Direct calls: Python application ──> MemoryAPI ──> Storage interface ──> memory or Docker backends

HTTP calls: client ──> local HTTP ──> MemoryAPI ──> Storage interface ──> memory or Docker backends
```

## 2. Installation

The project requires Python 3.11 or later. In the repository development environment, run:

```bash
uv sync
```

To connect to Redis, Milvus, Elasticsearch, or PostgreSQL/pgvector, install the `deploy` extra:

```bash
uv sync --extra deploy
```

To load local BGE embedding and reranking models in the current Python process, also install:

```bash
uv sync --extra embed
```

The equivalent pip commands are:

```bash
python -m pip install -e .
python -m pip install -e '.[deploy]'
python -m pip install -e '.[embed]'
```

## 3. Option One: In-Memory Storage + Direct MemoryAPI Calls

Without a configuration, `assemble()` uses the built-in offline assembly. `CompositeStorage`
combines the in-process KV, Vector, Fulltext, and Graph Store implementations. Embedding, the LLM,
and reranking also use default implementations with no external dependencies.

```python
from jiuwen_memory.api import Context, Scope, assemble_runtime, legacy_request_context

runtime = assemble_runtime()
api = runtime.api
scope = Scope(org="demo", user="alice")
security = legacy_request_context(scope)

try:
    units = api.add(
        "The user prefers to write code in Python",
        scope,
        security=security,
        tags=["preference"],
    )
    result = api.search(
        "Which language does the user prefer?",
        Context(scope),
        security=security,
        top_k=5,
    )
    print(units[0].id)
    print([item.content for item in result.items])
finally:
    runtime.close(wait=True)
```

All data in this mode lives in the current process and is lost when the process exits. Docker and
model services are not required.

The example's `legacy_request_context` is a transitional bridge for local testing; it does not
validate credentials. Production applications must obtain `RequestSecurityContext` from a trusted
authentication boundary instead of treating the business `scope` as an authenticated identity.
HTTP and CLI already use independent authentication boundaries and do not use this legacy bridge.

## 4. Option Two: In-Memory Storage + Local HTTP Launcher

Run the following command from the repository root:

```bash
uv run --no-sync -- ./scripts/run-server.sh --auth-mode dev --host 127.0.0.1 --port 8137
```

If you are not using uv, run the following in an environment where the dependencies are installed:

```bash
./scripts/run-server.sh --auth-mode dev --host 127.0.0.1 --port 8137
```

In another terminal, verify the health endpoint:

```bash
curl http://127.0.0.1:8137/healthz
```

`--auth-mode dev` enables a fixed identity authenticator for local functional testing only. It
ignores authentication headers, creates the server-side ROOT identity
`Scope(org="local", user="developer")`, and still runs `MemoryAPI` authorization. The examples use
the same Scope as the business target. This mode binds to loopback by default and must not be used
in production. The request body keeps the parameter structure of the same-named `MemoryAPI` method:

```bash

curl -X POST http://127.0.0.1:8137/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"content":"The user prefers to write code in Python","scope":{"org":"local","space":"","user":"developer","agent":"","session":""}}'

curl -X POST http://127.0.0.1:8137/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Which language does the user prefer","context":{"scope":{"org":"local","space":"","user":"developer","agent":"","session":""},"extensions":{}},"top_k":5}'
```

HTTP authentication mode is selected in this order: `--auth-mode`, the
`JIUWEN_MEMORY_HTTP_AUTH_MODE` environment variable, then `required`. Setting the environment variable
to `dev` enables development authentication even without the command-line option.
In `required` mode, the launcher has no production `SecurityRuntimeProducer`, so business endpoints
fail closed with 503. Integrating applications should inject a trusted security runtime through
`HttpServer.build(..., security_runtime=security_runtime)`. This security runtime is not the
memory-kernel runtime returned by `assemble_runtime()`.
The development launcher uses a minimal `DevHttpSecurityRuntime` with a fixed-identity authenticator
and no rate limiter, workload guard, or surface audit component. API authorization and business
auditing still run.

The HTTP process assembles one Kernel, so requests share state while the service is running. The
default in-memory data is lost after the service stops.

## 5. Option Three: Docker-Hosted Backends + Direct MemoryAPI Calls

Reusing the PostgreSQL profile backends is recommended because it requires only the
PostgreSQL/pgvector and Elasticsearch containers. First, start storage without starting the
agent-memory application container:

```bash
cd deploy/docker/postgres
cp .env.example .env
# Update POSTGRES_PASSWORD and other database settings in .env.
docker compose up -d postgres elasticsearch
docker compose ps
```

When the data volume is created for the first time, Compose executes
[scripts/pg_schema.sql](../../../scripts/pg_schema.sql) to create the KV and pgvector tables.

Next, create a host-side configuration such as `local-real-storage.yml`:

```yaml
profile: local-real-storage

memory_api:
  globals:
    vector_enabled: true
    graph_enabled: false
    embedder_dim: 1024

  kv_store:
    default:
      target: postgres
      params:
        dsn: "${PG_DSN}"
        schema: public
        table: agent_memory_kv
        auto_create_schema: false

  vector_store:
    default:
      target: pgvector
      params: &pgvector_params
        dsn: "${PG_DSN}"
        schema: public
        table: agent_memory_vectors
        dim: 1024
        metric_type: COSINE
        auto_create_schema: false
        create_extension: false
    layers_l0:
      target: pgvector
      params:
        <<: *pgvector_params
        table: agent_memory_vectors_l0
    layers_l1:
      target: pgvector
      params:
        <<: *pgvector_params
        table: agent_memory_vectors_l1

  fulltext_store:
    default:
      target: elasticsearch
      params: &es_params
        hosts: "${ES_HOSTS:-http://127.0.0.1:9200}"
        index: agent_memory_fulltext
        text_analyzer: cjk
    layers_l0:
      target: elasticsearch
      params:
        <<: *es_params
        index: agent_memory_fulltext_l0
    layers_l1:
      target: elasticsearch
      params:
        <<: *es_params
        index: agent_memory_fulltext_l1
```

This example continues to use the built-in hashing embedder, echo LLM, and overlap reranker. Only
storage is replaced with real backends, so no model service is required. `embedder_dim: 1024` must
match the pgvector dimension in the initialization SQL.

Set the host-side connection addresses:

```bash
export PG_DSN='postgresql://agent_memory:replace-with-password@127.0.0.1:5432/agent_memory'
export ES_HOSTS='http://127.0.0.1:9200'
```

For direct calls, `assemble()` accepts only the two-level namespace inside `memory_api`. To reuse
both the HTTP configuration format and its environment-variable expansion, load the configuration as
follows:

```python
from jiuwen_memory.api import Context, Scope, assemble_runtime, legacy_request_context
from jiuwen_memory_entry.core.config_loader import load_layer

layer = load_layer("local-real-storage.yml")
runtime = assemble_runtime(config=layer["memory_api"])
api = runtime.api

scope = Scope(org="demo", user="alice")
security = legacy_request_context(scope)
try:
    api.add("A memory that must be persisted", scope, security=security)
    result = api.search("persisted memory", Context(scope), security=security, top_k=5)
    print([item.content for item in result.items])
finally:
    runtime.close(wait=True)
```

Do not call `Config.from_yaml()` directly on this file with the `profile` and `memory_api` wrapper.
Doing so treats the service-layer keys as kernel component namespaces. Another difference is that
`Config.from_yaml()` parses YAML but does not expand `${VAR}`. The `load_layer()` function used above
performs the environment-variable expansion.

## 6. Option Four: Docker-Hosted Backends + Local HTTP Service

Keep the PostgreSQL and Elasticsearch containers from the previous section running, and then start
the local HTTP service:

```bash
export PG_DSN='postgresql://agent_memory:replace-with-password@127.0.0.1:5432/agent_memory'
export ES_HOSTS='http://127.0.0.1:9200'

uv run --no-sync -- ./scripts/run-server.sh \
  --auth-mode dev \
  --host 127.0.0.1 \
  --port 8137 \
  local-real-storage.yml
```

The HTTP launcher reads the top-level `profile`, passes only `memory_api` to the kernel assembly, and
expands environment variables in the configuration. Exiting the application process does not delete
data in PostgreSQL or Elasticsearch. The backend containers and their data volumes must be managed
separately.

## 7. Why the Example Prefers the PostgreSQL Profile

`deploy/docker/postgres/docker-compose.yml` already maps PostgreSQL `5432` and Elasticsearch `9200`
to the host, so they can be accessed directly by a host-side Python process.

Although the Redis/Milvus profiles expose Milvus and Elasticsearch, their current Compose files do
not map Redis `6379` to the host. Therefore, a "host application + Redis/Milvus/ES" setup cannot reuse
the existing Compose files unchanged. It requires a Compose override or a storage-only Compose file
that adds a controlled Redis port mapping. Do not put container-only addresses such as
`redis://redis:6379` or `http://milvus:19530` in a host-side configuration.

## 8. Configuration Shape Comparison

### Starting Local HTTP

The HTTP launcher accepts a service-layer configuration:

```yaml
profile: local-profile
memory_api:
  globals: {}
  kv_store: {}
```

The startup command can accept one or more YAML/JSON files:

```bash
./scripts/run-server.sh --auth-mode dev config.yml
```

### Calling MemoryAPI Directly

`jiuwen_memory.config.Config` accepts the kernel configuration itself:

```yaml
globals: {}
kv_store: {}
```

To read this form without a `memory_api` wrapper directly, use:

```python
from jiuwen_memory.api import assemble
from jiuwen_memory.config import Config

api = assemble(config=Config.from_yaml("memory-api.yml"))
```

This form does not expand environment variables automatically. Write already-resolved values or let
the application inject environment variables first.

## 9. Choosing Between HTTP and MemoryAPI

The HTTP service exposes all 36 `MemoryAPI` methods one to one through
`POST /v1/<method_name>`. Request names, nesting, required fields, and defaults match the same-named
method. A successful response is the JSON representation of the original return value, without an
additional envelope. `security` is the only parameter not accepted from the request body; the
authentication runtime constructs and injects it.

All ordinary methods are exposed, so the choice usually depends on how the application runs:

- call `MemoryAPI` directly for in-process latency, Python type checking, and original Python
  objects;
- use HTTP for cross-language or cross-process access;
- an asynchronous HTTP route is still a normal request-response operation: the service waits for
  the same-named async method and does not create another job.

Current exception: HTTP/CLI type decoding rejects the object-valued write-side
`system_metadata.coords` routing extension. Use the Python API directly for that feature; ordinary
Scope-based writes are unaffected. See [API F05 limitations](../../features/api/F05-http-memory-api-alignment.md#已知遗留).

### CLI Uses the Same Parameters

CLI exposes all 36 API methods as same-named commands, preserves parameter names, and accepts
complex objects as JSON. For example:

```bash
uv run --no-sync -- ./scripts/run-cli.sh --auth-mode dev add \
  --content 'CLI write test' --scope '{"org":"local","user":"developer"}'
```

Local mode calls the API directly. `--server http://127.0.0.1:8137` sends the same JSON request over
HTTP and returns the original response value. Without dev mode or an injected authenticator, local
business calls return 503. Remote authentication is controlled by the server; CLI sends
`AGENT_MEMORY_API_KEY` as a Bearer credential and rejects combining `--server` with local
`--auth-mode dev`. The default in-memory backend does not persist between CLI processes; use a
`batch` session, persistent storage, or a long-running HTTP service for repeated calls.
See the [CLI guide](../../../jiuwen_memory_entry/cli/DESIGN.md) for the full parameter contract.

## 10. Runtime and Security Considerations

- The default in-memory stack is lost when the process exits. The lifecycle of real backend data is
  determined by Docker volumes.
- Local scripts do not read `.env` automatically. Use `export` in the shell or inject environment
  variables through another process manager.
- Bind the reference HTTP service to `127.0.0.1` during development instead of exposing it directly
  to the public internet.
- HTTP actors come only from the authentication context; `actor_*`, `identity`, and other identity
  claims in the request body are rejected.
- In the default `required` mode, a launcher without a production authentication runtime returns
  503 and never falls back to an empty or payload-provided identity.
- `dev` mode fixes the actor to the `local/developer` ROOT identity, ignores authentication headers,
  but still runs authorization; use it only for functional tests bound to loopback.
- Production environments should provide a trusted authentication runtime together with TLS, rate
  limiting, timeouts, monitoring, backups, and reliable process management.
- Before the application exits, call `runtime.close(wait=True)` to wait for and release
  the in-process ingestion worker pool.

## 11. Related Documentation

- [Deployment Overview](<Deployment Overview.md>)
- [Containerized Deployment](<Containerized Deployment.md>)
- [Storage API documentation](<../API Docs/storage.md>)
- [Retrieval API documentation](<../API Docs/retrieval.md>)
- [HTTP startup script](../../../scripts/run-server.sh)
