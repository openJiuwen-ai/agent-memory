# Deployment Overview

Last revised: 2026-09-05

This document helps users choose how to run agent-memory before proceeding to detailed installation
instructions. The project currently supports two broad deployment categories: **containerized
deployment** and **SDK deployment**.

```text
agent-memory
├── Containerized deployment
│   ├── Remote embedding/reranking + Redis / Milvus / Elasticsearch
│   ├── Local embedding/reranking + Redis / Milvus / Elasticsearch
│   └── Remote embedding/reranking + PostgreSQL / pgvector / Elasticsearch
└── SDK deployment (the kernel runs in a Python process)
    ├── Storage: in-process memory / Docker-hosted backends
    └── Access: direct MemoryAPI calls / local HTTP service
```

## 1. Deployment Categories

### 1.1 Containerized Deployment

Containerized deployment uses Docker Compose to run the agent-memory HTTP service and its required
storage backends together. The application, connection configuration, and backend dependencies are
organized in one Compose profile. This option is suitable when you want a complete service quickly
and want to minimize differences between host Python environments.

The repository currently provides three complete Compose profiles:

| Profile | Model mode | Source-of-truth and index backends | External interface |
|---|---|---|---|
| `online` | LLM, embedding, and reranking all use remote services | Redis + Milvus + Elasticsearch | HTTP on port `8137` by default |
| `local` | Embedding and reranking load local BGE models in the application container; the LLM still defaults to remote DashScope | Redis + Milvus + Elasticsearch | HTTP on port `8137` by default |
| `postgres` | LLM, embedding, and reranking all use remote services | PostgreSQL KV + pgvector + Elasticsearch | HTTP on port `8137` by default |

In this documentation, "local models" refers only to local embedding and reranking. The current
`local` profile **does not include a local LLM**. PostgreSQL is a storage choice and is independent
of the model mode. The repository does not currently provide a ready-made "local embedding and
reranking + PostgreSQL" profile.

See [Containerized Deployment](<Containerized Deployment.md>) for detailed instructions.

### 1.2 SDK Deployment

SDK deployment means installing and assembling `jiuwen_memory` in the consumer's Python process.
The application can call `MemoryAPI` directly or start the repository's local HTTP service in that
process.

> `jiuwen_memory_entry/sdk` does not yet provide a separately packaged SDK client or launcher. In this
> documentation, "SDK deployment" is the general term for direct Python package integration. The
> stable entry point is the `MemoryAPI` returned by `jiuwen_memory.api.assemble()`.

This mode has two independent selection dimensions:

| Storage mode | Direct MemoryAPI calls | Local HTTP service |
|---|---|---|
| In-process memory | Minimal development, testing, and feature validation | HTTP protocol testing and quick cross-language integration |
| Docker-hosted backends | Embed memory capabilities in a Python application with persistent data | Run a local persistent service for cross-process access |

See [SDK Deployment](<SDK Deployment.md>) for detailed instructions.

## 2. Selection Guide

| Requirement | Recommended option | Reason |
|---|---|---|
| Validate add/search as quickly as possible | SDK + memory + direct MemoryAPI | No external services and the lowest startup cost |
| Test the HTTP request format | SDK + memory + local HTTP | Validates HTTP without external storage |
| Embed in a Python application with persistence | SDK + Docker PostgreSQL/pgvector/ES + MemoryAPI | Avoids an additional HTTP hop while persisting backend data |
| Run a persistent cross-process HTTP service on the local machine | SDK + Docker PostgreSQL/pgvector/ES + local HTTP | The application runs on the host and storage runs in containers, which is convenient for development |
| Start a complete service with one command | Containerized deployment | Compose manages the application and backend dependencies together |
| Embedding and reranking data must not be sent to remote model services | Containerized `local` profile | BGE embedding and reranking run in the application container |
| Reduce the number of Milvus, etcd, and MinIO components | Containerized `postgres` profile | pgvector replaces Milvus and its dependencies |

## 3. Capabilities and Boundaries

### 3.1 Default In-Memory Stack

When `assemble()` is called without a configuration, the system uses its built-in offline
configuration:

- `CompositeStorage` exposes the unified Storage interface;
- KV, Vector, Fulltext, and Graph Store all use in-process implementations;
- embedding uses the hashing implementation and requires no external model;
- the LLM uses echo, and reranking uses overlap;
- in-memory data is lost when the process exits.

This mode is suitable for feature development and testing. It should not be treated as a persistent
deployment.

### 3.2 HTTP and MemoryAPI Are Aligned One to One

The local HTTP service is a JSON adapter over `MemoryAPI` and exposes all 36 methods. Each method
uses `POST /v1/<method_name>`. Request field names, nesting, required fields, and defaults match the
same-named `MemoryAPI` method. A successful response directly serializes the original return value
without introducing another business envelope or task model.

The HTTP boundary performs only the conversions required by JSON:

- Python objects such as `Scope`, `Context`, and `MemoryPatch` use equivalent JSON objects, while
  enum and datetime values use strings and ISO 8601 strings respectively;
- `security` cannot be supplied in the request body. The authentication boundary constructs and
  injects it from the runtime's authentication result. Dev mode uses a minimal
  `DevHttpSecurityRuntime`, not a fully configured production security runtime.

Synchronous methods run directly in the request thread. For asynchronous methods, the HTTP entry
point waits for the same-named async method and returns its original result instead of creating a
background job. Direct Python calls are generally chosen for in-process latency, static typing, and
Python objects, not missing ordinary HTTP methods. One current exception is the write-side
`system_metadata.coords` routing extension: HTTP/CLI decoding rejects its object value, so use the
Python API directly for routed writes. See [API F05 limitations](../../features/api/F05-http-memory-api-alignment.md#已知遗留).

### 3.3 Security Boundary of the Current HTTP Service

The repository provides a reference service based on Python's standard-library
`ThreadingHTTPServer`. It does not include TLS or multi-process management. Business requests must
first establish a trusted `RequestSecurityContext`, and the service rejects `security`, `actor_*`,
`identity`, and similar identity claims in the request body. In the default `required` mode, a
missing production runtime makes business endpoints return 503 instead of trusting a payload
identity. Local functional tests may explicitly enable `dev`, which creates the fixed
`local/developer` ROOT identity on the server. Dev mode ignores authentication headers but still
runs MemoryAPI authorization.

The standard `HttpServer.serve()` entry point checks the binding before creating the socket.
Dev mode allows only loopback hosts by default, such as `127.0.0.1`, `::1`, and `localhost`.
Reusing `handler_cls()` in a separately constructed server does not perform this check;
the embedding application must enforce its own binding policy.
Listening on `0.0.0.0` inside Docker also requires
`JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK=true`. The supplied Compose files set this flag
and publish the host port only on `127.0.0.1` by default. Before exposing the service to a shared network or production,
return to `required`, provide a production-grade authentication runtime, add TLS, access control,
and traffic protection at a gateway, and use appropriate process and availability management.

> **Dangerous override:** `JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK=true` relaxes the
> dev binding restriction; it does not add credential verification. Anyone who can reach the
> service uses the same test identity, subject to API authorization. Restrict access through
> published ports, container networks, and proxies. A startup warning is not an access control.

## 4. Access Methods That Are Not Separate Deployment Options

- **CLI**: exposes all 36 API methods as same-named commands with matching parameter names and JSON
  structures. Local mode calls the API directly; `--server` sends the same payload over HTTP without
  legacy request/response conversion. Local tests require explicit `--auth-mode dev`; remote
  authentication is controlled by the server. See the [CLI guide](../../../jiuwen_memory_entry/cli/DESIGN.md).
- **MCP**: can expose tools over stdio or streamable HTTP; it is a protocol surface whose underlying
  deployment must still choose between memory and real storage.
- **`jiuwen_memory_entry/sdk`**: currently contains only a package skeleton and is not a separate SDK product
  layer from `jiuwen_memory.api`.
- **Kubernetes / Helm / systemd**: the repository does not currently provide ready-made deployment
  manifests for these systems.

## 5. Related Documentation

- [Containerized Deployment](<Containerized Deployment.md>)
- [SDK Deployment](<SDK Deployment.md>)
- [Storage API documentation](<../API Docs/storage.md>)
- [Retrieval API documentation](<../API Docs/retrieval.md>)
- [Original Docker Compose instructions](../../../deploy/docker/README.md)
