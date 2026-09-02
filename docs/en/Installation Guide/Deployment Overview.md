# Deployment Overview

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
> stable entry points are the `Kernel` and `MemoryAPI` returned by
> `jiuwen_memory.api.build_kernel()`.

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

When `build_kernel()` is called without a configuration, the system uses its built-in offline
configuration:

- `CompositeStorage` exposes the unified Storage interface;
- KV, Vector, Fulltext, and Graph Store all use in-process implementations;
- embedding uses the hashing implementation and requires no external model;
- the LLM uses echo, and reranking uses overlap;
- in-memory data is lost when the process exits.

This mode is suitable for feature development and testing. It should not be treated as a persistent
deployment.

### 3.2 HTTP and MemoryAPI Are Not Fully Equivalent

The local HTTP service is a JSON adapter over `MemoryAPI`. It covers common write, retrieval,
governance, permission, and space-management operations, but it is not a complete one-to-one
mapping. For example:

- HTTP does not expose `job_cancel` or `check_write` as standalone routes;
- `add_async` and `batch_add_async` do not have independent HTTP routes;
- HTTP `search` always uses L2 disclosure and does not expose every parameter, such as `as_of`;
- HTTP `update` and `delete` expose only commonly used subsets of the underlying API parameters;
- the target scope of ordinary HTTP requests primarily maps to `org + space + user`; the complete
  five-dimensional scope appears only in some request structures.

Use direct Python calls when you need the full `MemoryAPI` parameter set, asynchronous methods, or
fine-grained governance capabilities. HTTP is better suited to cross-language integration and common
feature testing.

### 3.3 Security Boundary of the Current HTTP Service

The repository provides a reference service based on Python's standard-library
`ThreadingHTTPServer`. It does not currently include transport authentication, TLS, rate limiting,
or multi-process management. Actor fields in requests are caller-provided claims and are not a
substitute for real identity authentication.

Bind the service to `127.0.0.1` in development. Before exposing it to a shared network or production,
add TLS, authentication, access control, rate limiting, and auditing at a gateway, and use a process
and availability management solution appropriate for production.

## 4. Access Methods That Are Not Separate Deployment Options

- **CLI**: can call the kernel in process or access an HTTP service; it is a client entry point.
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
