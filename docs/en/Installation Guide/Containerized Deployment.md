# Containerized Deployment

Containerized deployment uses Docker Compose to run the agent-memory HTTP service and backend
storage together. The repository provides three profiles that can be configured and started
directly: `online`, `local`, and `postgres`.

```text
Caller
  │ HTTP :8137
  ▼
agent-memory application container
  ├── MemoryAPI / construction / retrieval
  ├── Models: remote calls or BGE loaded inside the container
  └── Storage interface
       ├── Redis or PostgreSQL KV
       ├── Milvus or pgvector
       └── Elasticsearch
```

## 1. Prerequisites

- The Python application does not need to be installed on the host, but Docker and Docker Compose
  v2 are required.
- For the Redis/Milvus/Elasticsearch profiles, allocate at least `20 GB` of memory to Docker. The
  complete stack typically uses approximately `13–18 GB` while running.
- The host must be able to pull the required images. When remote models are used, the containers
  must also be able to reach the corresponding model endpoints.
- The default HTTP port is `8137`, and the Elasticsearch port is `9200`. The Milvus profiles also
  expose `19530/9091`, while the PostgreSQL profile exposes `5432` by default.

For the original Compose parameters, see
[deploy/docker/README.md](../../../deploy/docker/README.md).

## 2. Choosing a Profile

| Profile | Models inside the application | External models | Storage backends | Suitable scenarios |
|---|---|---|---|---|
| `online` | None | LLM, embedding, reranking | Redis + Milvus + ES | Remote model services already exist and a lighter application image is preferred |
| `local` | BGE embedding and BGE reranking | LLM | Redis + Milvus + ES | Memory content must not be sent to remote embedding/reranking services |
| `postgres` | None | LLM, embedding, reranking | PostgreSQL KV + pgvector + ES | Fewer Milvus, etcd, and MinIO components are preferred |

All profiles expose HTTP through `POST /v1/<verb>` and provide a health check through
`GET /healthz`.

## 3. Online Model Mode

In this mode, the LLM, embedding, and reranking capabilities are provided by remote services. Redis
stores the source-of-truth records, Milvus stores vector indexes, and Elasticsearch stores full-text
indexes.

```bash
cd deploy/docker/online
cp .env.example .env
# Edit .env and configure at least LLM_API_KEY, MODEL_API_TOKEN,
# EMBEDDER_BASE_URL, and RERANKER_BASE_URL.
docker compose config
docker compose up -d --build
```

Pay particular attention to two model URL rules:

- `EMBEDDER_BASE_URL` must include `/v1`; the OpenAI client appends `/embeddings`.
- `RERANKER_BASE_URL` must not include `/rerank`; the adapter appends that path.

The default LLM target is DashScope. When switching to a generic OpenAI-compatible service, update
the target, model name, URL, and vendor-specific parameters in `config.yml` together. Replacing only
the URL is not sufficient.

## 4. Local Model Mode

This mode loads `bge-m3` and `bge-reranker-v2-m3` in the agent-memory application container. The LLM
still defaults to DashScope. Model files are not downloaded automatically during container startup
and must be prepared on the host first.

```bash
cd deploy/docker/local
./download-models.sh
cp .env.example .env
# Edit .env and configure at least LLM_API_KEY.
docker compose up -d --build
```

After the download completes, `deploy/docker/models/` should contain `bge-m3/` and
`bge-reranker-v2-m3/`. Compose mounts them into `/models-local` in the application container.

The models are loaded into memory when the application receives its first relevant request, so the
first add/search request may be noticeably slower. Without a GPU, inference runs on the CPU. The
features remain available, but batch-write and concurrent throughput will be lower.

## 5. PostgreSQL Mode

This mode uses one PostgreSQL instance for the KV source of truth and three pgvector tables, while
Elasticsearch continues to store the full-text indexes. It does not start Redis, Milvus, etcd, or
MinIO.

```bash
cd deploy/docker/postgres
cp .env.example .env
# Edit .env: configure model credentials and endpoints, and replace the default database password.
docker compose config
docker compose up -d --build
```

When the PostgreSQL data volume is created for the first time,
[scripts/pg_schema.sql](../../../scripts/pg_schema.sql) automatically:

- installs the `vector` extension;
- creates `agent_memory_kv`;
- creates `agent_memory_vectors`, `agent_memory_vectors_l0`, and
  `agent_memory_vectors_l1`;
- creates the scope, metadata, and HNSW-related indexes.

The initialization script runs only when an empty data volume is first created. The default DDL uses
the `public` schema, 1024-dimensional vectors, and COSINE/HNSW. If you change the schema, table names,
or embedding dimensions, update `.env`, `config.yml`, and the initialization SQL before creating the
volume. Existing volumes are not migrated automatically.

## 6. Post-Startup Verification

The following commands apply to all three profiles:

```bash
docker compose ps
docker compose logs -f agent-memory
```

In another terminal, run:

```bash
curl http://localhost:8137/healthz

curl -X POST http://localhost:8137/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","scope":"alice","content":"The user prefers to write code in Python"}'

curl -X POST http://localhost:8137/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","scope":"alice","query":"Which language does the user prefer","k":5}'
```

A `status: ok` health response only confirms that the HTTP process can respond. Complete availability
should be verified with a real add/search round trip, because model connectivity, vector dimensions,
and index configuration errors often appear only during business requests.

## 7. Configuration Loading Rules

The Compose file for each profile mounts the local `config.yml` as `/config/config.yml` inside the
container, and the application startup command reads that path. The mount source in Compose is
therefore what determines which configuration is used.

```yaml
volumes:
  - ./config.yml:/config/config.yml:ro
```

The HTTP service configuration has two layers:

```yaml
profile: docker-profile-name

memory_api:
  globals: {}
  kv_store: {}
  vector_store: {}
  fulltext_store: {}
```

- `profile` belongs to the service surface and identifies the active profile.
- The content under `memory_api` is the two-level component namespace passed to `build_kernel()`.
- User configuration is merged over the built-in default assembly, so only implementations that
  need to be replaced must be declared.
- `${VAR}` and `${VAR:-default}` are expanded recursively by the HTTP startup configuration loader.
- Docker Compose reads `.env` from the same directory, so secrets do not need to be stored in the
  configuration file.

The HTTP launcher accepts multiple configuration files, but files are combined with a shallow merge
of top-level keys. If a later file declares `memory_api`, it replaces the entire `memory_api` section
from the earlier file instead of deep-merging its contents. Keep the kernel differences for one
profile in a single `memory_api` section whenever possible.

## 8. Container Networking and Host Addresses

Within Compose, backends are reached through service names:

- `redis:6379`
- `milvus:19530`
- `elasticsearch:9200`
- `postgres:5432`

These names resolve only on the Compose network. If the application runs on the host and only storage
runs in Docker, the configuration must use `127.0.0.1` and the ports mapped to the host. Do not reuse
the container DNS names above. See [SDK Deployment](<SDK Deployment.md>) for detailed instructions.

## 9. Data and Lifecycle

```bash
docker compose down
```

This stops the services and preserves named volumes. Backend data remains available after the stack
is started again.

```bash
docker compose down -v
```

This stops the services and deletes the volumes declared by the current profile. It removes the
source-of-truth and index data and is generally not recoverable. Confirm that backups exist before
running it.

## 10. Capabilities Required Before Production Use

The current HTTP service is a reference implementation. It does not include TLS, transport-level
identity authentication, rate limiting, or multi-process management. Elasticsearch security is also
disabled by default in Compose, and the default database credentials are suitable only for
development.

Before deployment on a shared network or in production, at least:

- terminate TLS at a reverse proxy or API gateway;
- derive the actor from a trusted identity system instead of trusting actor fields in the request
  body;
- replace all default passwords and configure backend accounts with least privilege;
- restrict exposure of PostgreSQL, Elasticsearch, Milvus, and other backend ports;
- configure TLS for PostgreSQL, Redis, Elasticsearch, Milvus, and model endpoints;
- add rate limiting, timeouts, retries, log collection, monitoring, backup, and recovery policies;
- add process orchestration and replication appropriate for the availability target.

## 11. Troubleshooting

### The Health Check Passes but Search Returns No Results

Check the application logs for embedding errors, confirm connectivity to model endpoints, verify that
vector dimensions match, and confirm that index data was actually generated in Elasticsearch/Milvus
or pgvector. An incomplete local model download can also allow a write to succeed while index
construction degrades.

### Chinese Keyword Recall Is Poor

The default Elasticsearch analyzer may not be suitable for Chinese. Use the built-in `cjk` analyzer,
or use an Elasticsearch image with the IK plugin and configure `ik_max_word`. Analyzers take effect
when an index is created; existing indexes must be rebuilt after this setting changes.

### Configuration Changes Do Not Take Effect

If you changed a Compose mount or service definition, run `docker compose up -d` to recreate the
affected containers. A plain `restart` does not apply Compose file changes. Then use
`docker compose config` and the container logs to confirm the effective configuration.

## 12. Related Documentation

- [Deployment Overview](<Deployment Overview.md>)
- [SDK Deployment](<SDK Deployment.md>)
- [Storage API documentation](<../API Docs/storage.md>)
- [Retrieval API documentation](<../API Docs/retrieval.md>)
