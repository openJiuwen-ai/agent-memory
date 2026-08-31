# agent-memory FAQ: Troubleshooting Guide

A **problem-locating guide** for users and operators: when something goes wrong, follow the path "confirm log location → triage by symptom → enter the matching scenario and work through the steps". For installation, Docker deployment, and general configuration questions, see the [deployment instructions](../../../deploy/docker/README.md).

## Contents

- [1. Problem Location](#1-problem-location)
  - [1.1 Confirm Where the Logs Are](#11-confirm-where-the-logs-are)
  - [1.2 Triage by Symptom](#12-triage-by-symptom)
  - [1.3 Scenario 1: Service Unreachable / Won't Start](#13-scenario-1-service-unreachable--wont-start)
  - [1.4 Scenario 2: Requests Fail Outright](#14-scenario-2-requests-fail-outright)
  - [1.5 Scenario 3: Write Succeeds but Recall Is Empty (Most Frequent)](#15-scenario-3-write-succeeds-but-recall-is-empty-most-frequent)
  - [1.6 Scenario 4: Recall Results Not as Expected](#16-scenario-4-recall-results-not-as-expected)
  - [1.7 Scenario 5: Slow Requests](#17-scenario-5-slow-requests)
- [Appendix A: Error Code Reference](#appendix-a-error-code-reference)

---

## 1. Problem Location

### 1.1 Confirm Where the Logs Are

**Where to find engine logs**

Engine logs are written **to the terminal only** by default; to persist them to disk, configure the `memory_api.globals` section in `config.yml`:

```yaml
memory_api:
  globals:
    log_file: /var/log/agent-memory/engine.log   # file path; omit to log to terminal only
    log_level: INFO                                # default: INFO
```

Locate the logs for your runtime form:

| Runtime form | Where to find engine logs |
|---|---|
| Docker | `docker compose logs -f agent-memory` (terminal stream); the `log_file` path inside the container (if configured) |
| Local HTTP server | Startup terminal; the `log_file` path (if configured) |
| CLI | Console; the `log_file` path (if configured) |
| SDK (in-process) | Engine logs do not propagate to the host root logger; the host must read the `agent_memory.*` subtree directly or call `setup_logging` itself |

Two expectations to set:

- **The HTTP / CLI entry points log only a few lines** (startup/shutdown etc.), with no per-request logging. All request success/failure information lives in the HTTP response body—don't look for request clues in entry-point logs.
- Log names are self-identified by module prefix (e.g., `agent_memory.construction.index_builder_impl.vector_index_builder`); filter by prefix to distinguish layers.

**How to locate "my request" in a flood of logs**

agent-memory logs **have no request id spanning the whole chain**; correlation relies on two business identifiers:

1. **scope** (`org/space/user/session`) — lines such as `Engine.write ...` / `Recaller ...` all carry the scope; filter logs by it to narrow down to that tenant/session's log segment;
2. **first 8 characters of the unit id** — `VectorIndexBuilder` WARN lines carry it, letting you map the `item_id` from an add response directly to the specific memory unit.

### 1.2 Triage by Symptom

The first triage basis is the **HTTP response body** — most problems are half-answered after reading the response. Read the response first, then decide whether to dig into logs.

| Field | Meaning |
|------|------|
| `ok` | `true` means the request pipeline itself completed |
| `error` / `message` | Exception class name and reason on failure; map the class name via [Appendix A](#appendix-a-error-code-reference) |
| `item_id` | Memory id returned on successful add; `null` with `skipped`, see below |
| `skipped` | add only: with `infer=true`, all derived memories were deduplicated as update/noop — **this is normal semantics, not a failure** |
| `trajectory` | Retrieval trace returned when search carries `"trace": true`; see [Scenario 3](#15-scenario-3-write-succeeds-but-recall-is-empty-most-frequent) step ② |

Decision priority: a non-empty `error` means failure (enter Scenario 2); `ok=true` only means the request pipeline succeeded — it **does not guarantee the memory is retrievable** (index build failures degrade silently; enter Scenario 3).

Errors use the uniform format `{"error": "<exception class name>", "message": "<reason>"}`. Note: `message` is the raw exception text and is **not guaranteed to be redacted** — redaction only happens on some internal paths such as recall channel errors (see A.3); do not pass plaintext credentials in request parameters.

Once you have the symptom, find your seat:

```text
Problem occurs
│
├─ Service won't start / requests can't get through ───→ Scenario 1
├─ Request gets a response, but returns an error ──────→ Scenario 2
├─ Write returns ok=true, but search recall is empty ──→ Scenario 3 (most frequent)
├─ Recall works, but results are wrong (missing/biased/extra/stale) → Scenario 4
└─ Requests go through, but slowly ────────────────────→ Scenario 5
```

### 1.3 Scenario 1: Service Unreachable / Won't Start

**① Liveness probe**: `curl http://localhost:8137/healthz` only confirms that **the service process itself** is alive (a healthy response is 200 `{"status": "ok", "profile": ...}`); it **does not probe backends** — healthz still returns 200 when Redis/ES/Milvus are down. Backend unavailability manifests as real requests failing with `BackendError` (enter [Scenario 2](#14-scenario-2-requests-fail-outright)) or unhealthy containers (go to ②).

**② Check container status**: `docker compose ps`; look for unhealthy / restart-looping containers.

**③ Check logs per service**: `docker compose logs <service>`. For the app container, check startup-phase logs for startup exceptions; for backend containers (Redis/ES/Milvus), check their own errors (port conflicts, mount paths, and out-of-memory are common causes).

**④ Service is up but requests return 404**: `{"error": "UnknownVerb"}` → the verb is misspelled; check it against the routing table (add / batch_add / search / list / get / update / delete / evolve / ...).

### 1.4 Scenario 2: Requests Fail Outright

**① Read the status code and error class name, then triage via this table**

| Error class | Layer at fault | Next step |
|---|---|---|
| `ValidationError` | Request parameter validation | Self-check parameters against `message` (metadata is split into system/user segments, `k` not a positive integer, missing parameters); no need to check logs |
| `NotFoundError` / `ConflictError` / `PermissionDeniedError` | API-layer semantic validation | id doesn't exist / duplicate creation / scope not authorized; act on `message` |
| `InvalidExtractionJSONError` | LLM extraction | Search logs for `Extractor` (including the `LLM response is not valid JSON` WARN); confirm the LLM endpoint is reachable and responses aren't truncated |
| `LockError` / `LockTimeoutError` / `LockLostError` | Distributed lock | Lock contention from multiple instances concurrently writing the same scope; check Redis lock configuration and instance count |
| `BackendError` | Storage layer | Backend network/IO failure; go to [Scenario 1](#13-scenario-1-service-unreachable--wont-start) ②③ to inspect per-service container logs |
| `StorageRetrievalError` | Recall layer | Keyword + vector channels failed **simultaneously**; troubleshoot Milvus / ES separately |
| `UnsupportedStorageCapabilityError` | Assembly configuration | A backend was removed from config but an operator still references its capability; check config-operator consistency |
| `InternalError` | Internal bug | `message` contains the original exception text; use it as a keyword to search logs for the full stack trace |

For the complete status-code mapping, see [Appendix A](#appendix-a-error-code-reference).

**② Filter engine logs by scope** (see [1.1 Confirm Where the Logs Are](#11-confirm-where-the-logs-are)) to locate that request's log segment.

**③ Drill down along the call chain** (each layer's log prefix and meaning):

```text
POST /v1/<verb>
→ HTTP entry (verb dispatch + parameter validation, no logging; ValidationError is raised here)
→ API layer (auth and parameter assembly, no logging)
→ Engine (log prefix Engine.; write dispatches by infer/procedural/middle)
   ├─ Write → extraction and indexing
   │    ├─ Extractor (prefix Extractor:, LLM extraction and retries)
   │    └─ IndexBuilder (prefix Forward/Fulltext/Vector/HybridIndexBuilder:,
   │         embedding and vector-write failures degrade silently to WARN here)
   └─ Recall
        ├─ Recaller (prefix KeywordRecaller: / VectorRecaller:, with hits=/units= counts)
        └─ Retriever / Fuser (multi-channel fusion; partial channel failures return ChannelError)
→ Storage layer (backend IO failures raise BackendError; scope isolation is enforced here)
```

### 1.5 Scenario 3: Write Succeeds but Recall Is Empty (Most Frequent)

`ok=true` ≠ the memory is retrievable. On the write path, embedding and vector-write failures are **logged as WARN only, then skipped** — HTTP still returns `ok=true`. Troubleshoot in this order:

**① Check engine logs at write time to confirm the index was actually built**

| Log line | Meaning |
|---|---|
| `Engine.write infer=True: N originals, M derived added, scope=...` | Infer extraction path completed; M is the derived count |
| `ForwardIndexBuilder: building forward index for N units` | Memory body (forward KV) written; without it, get/list find nothing |
| `VectorIndexBuilder: building index for N units` | Vector index build started |
| `FulltextIndexBuilder: building index for N units` | Full-text index build |
| **WARN** `VectorIndexBuilder: Embedder.embed failed for unit ...` | **Embedding failed; that unit's vector index is missing** — the direct root cause of "keyword recall works, vector recall doesn't" |
| **WARN** `VectorIndexBuilder: VectorStore.insert failed for scope ...` | **Milvus write failed**; the vector index is missing entirely |
| `Extractor: received N units, M accepted after preprocessing` | LLM extraction entry; M=0 means preprocessing rejected everything |

Handling WARN hits:

- `Embedder.embed failed` / `VectorStore.insert failed` → silent degradation has occurred; fix the embedding endpoint / Milvus, then re-write that batch of memories. The body (forward index) is unaffected — `get`/`list` still find them
- `Extractor` retry-type WARNs → retries happen internally and an error is only raised after the threshold is reached; only the final failure means the write failed

**② Bisect by channel with search + `"trace": true`**

The response's `trajectory` array carries `stage` / `channel` / `candidate_count` / `cost_ms` per step — which channel zeroes out at which step is plain to see:

- Keyword channel `candidate_count>0` while Vector channel is 0 → embedding problem; go back to ① and check the degradation WARNs
- Both channels 0 → scope mismatch or backend failure; go to ③ / [Scenario 1](#13-scenario-1-service-unreachable--wont-start)
- You can also search logs for `KeywordRecaller:` / `VectorRecaller:` `hits=N units=N` lines to verify per-channel hit counts

**③ Verify scope consistency**

The scope (`org` / `space` / `user` / `session`) used for writes and queries must match exactly — **scope is the isolation axis; cross-scope queries returning nothing is by design**, not data loss.

**④ As-of time-travel queries**: confirm the unit's `t_valid` is properly set; units missing `t_valid` get skipped by as-of queries or sort incorrectly.

### 1.6 Scenario 4: Recall Results Not as Expected

**① Recall metadata is empty**: when the SDK `recall()` needs metadata output, it must explicitly pass `output_fields=["metadata"]`; otherwise it comes back empty.

**② Metadata filtering doesn't take effect**: filter keys must carry the `user_metadata.` prefix; bare key names match nothing.

**③ "Old" memories reappear that shouldn't**: under the bitemporal model, as-of queries rewind historical versions by `t_valid` — that's normal semantics; if units are missing `t_valid`, erroneous rewinding occurs; go to [Scenario 3](#15-scenario-3-write-succeeds-but-recall-is-empty-most-frequent) ④.

**④ Results biased/missing**: add `"trace": true` to search and check each channel's `candidate_count` (see [Scenario 3](#15-scenario-3-write-succeeds-but-recall-is-empty-most-frequent) ②) to tell single-channel quality issues from fusion issues.

### 1.7 Scenario 5: Slow Requests

| Symptom | Diagnosis |
|---|---|
| First request slow (seconds) | **Normal**: local-mode bge embedding model loading; afterwards it stays in memory |
| Slow writes (infer=true) | LLM extraction path is the cost; search logs for `Extractor:` to check LLM endpoint latency and retry counts |
| Slow recall | The per-step `cost_ms` in the `trace=true` `trajectory` pinpoints the costly stage; for slow vector channels, check Milvus load first |
| Intermittent slowness | Backend resource contention; use `docker stats` to check container resource levels |

---

## Appendix A: Error Code Reference

agent-memory's error system is a **two-layer structure** (as opposed to numeric error codes):

1. **Exception layer**: 12 exception classes (base class `AgentMemoryError` + 11 subclasses; plus the lock exception family and extraction exception family, see A.2) — SDK callers can use one uniform exception hierarchy across backends and layers;
2. **HTTP layer**: the HTTP entry uniformly converts to status codes; the response body is fixed at `{"error": "<exception class name>", "message": "<reason>"}`.

### A.1 Exception Class ↔ HTTP Status Code Reference

Mapping rule: **only the first 5 classes in the table below have dedicated status codes**; other `AgentMemoryError` subclasses all map to 400; unexpected non-`AgentMemoryError` exceptions map to 500.

| Exception class | HTTP | Semantics | Typical cause |
|---|---|---|---|
| `NotFoundError` | 404 | Target entity/record/key doesn't exist | id of get/update/inspect/trace doesn't exist; scope is empty |
| `PermissionDeniedError` | 403 | actor not allowed to perform the action on the target scope | policy doesn't authorize that actor/action/scope combination |
| `ConflictError` | 409 | Conflict with an existing record | id already exists on insert; duplicate space name |
| `ValidationError` | 400 | Invalid or out-of-range input | metadata contains non-scalar values (dict/list); `k` not a positive integer; missing parameters |
| `PolicyError` | 400 | Runtime policy operation rejected | unknown policy key; attempt to modify immutable config |
| `AuthenticationError` | 400* | Credentials missing/malformed/failed validation | wrong or missing API key (*no dedicated status code, maps to 400) |
| `RateLimitedError` | 400* | Rate limit exceeded (occurs before authentication) | rate-limit bucket drained (*maps to 400 likewise) |
| `HealthCheckError` | 400* | Component health check failed | health() probe failure of Redis/ES/Milvus/embedder components (raised on SDK or internal paths; if raised through an HTTP request path it maps to 400 — the `/healthz` endpoint itself never raises it and returns 200 directly) |
| `BackendError` | 400* | Unexpected underlying storage failure | backend network/IO failure; ES/Milvus/Redis unreachable (*no dedicated status code, maps to 400 — **don't look for it under status 500**) |
| `UnsupportedStorageCapabilityError` | 400* | Storage doesn't declare the requested port capability | a backend was removed from config but an operator still references its capability |
| `StorageRetrievalError` | 400* | All selected recall entry points failed | keyword + vector channels failed simultaneously |
| Unexpected exception | 500 | `{"error": "InternalError"}` | internal bug; message contains the original exception |

Unknown verb: 404 `{"error": "UnknownVerb"}`.

### A.2 Errors Specific to the Write Path

| Exception | Trigger point | Meaning |
|---|---|---|
| `InvalidExtractionJSONError` / `InvalidExtractionCandidateError` | LLM extraction | LLM extraction output isn't valid JSON / candidate structure is invalid; retried internally, only raised after final failure (maps to 500 over HTTP) |
| `LockError` / `LockTimeoutError` / `LockLostError` | Distributed lock | lock contention / lease expiry when multiple instances concurrently write the same scope |

### A.3 Error Message Redaction

`safe_error_message` replaces `password / passwd / pwd / token / api_key / secret` key-values, `Authorization: Bearer/Basic ...` headers, and URL-embedded credentials (`//user:pass@`) in exception text with `<redacted>`, and truncates to 200 characters. **Currently it is only applied on internal paths such as recall channel error messages (ChannelError's message)** — the `message` field of HTTP error responses is the raw exception text and is not guaranteed to be redacted; also, don't try to reverse-lookup plaintext keys from logs while troubleshooting.
