# Control Layer API

The Control layer is the orchestration and management plane of the memory system. It does not
implement extraction, indexing, or retrieval algorithms. Instead, it routes API requests to Ingest,
Construction, Retrieval, Storage, and governance components and manages permissions, lifecycle,
scheduling, policies, long-running jobs, and Spaces.

This document is an API reference for the current abstract interfaces, public types, built-in
implementations, and configuration targets. The following source files are authoritative:

- [`base.py`](../../../jiuwen_memory/control/base.py)
- [`engine.py`](../../../jiuwen_memory/control/engine.py)
- [`pipeline.py`](../../../jiuwen_memory/control/pipeline.py)
- [`lifecycle.py`](../../../jiuwen_memory/control/lifecycle.py)
- [`governance.py`](../../../jiuwen_memory/control/governance.py)
- [`permission.py`](../../../jiuwen_memory/control/permission.py)
- [`scheduler.py`](../../../jiuwen_memory/control/scheduler.py)
- [`jobs.py`](../../../jiuwen_memory/control/jobs.py)
- [`ingest_job.py`](../../../jiuwen_memory/control/ingest_job.py)
- [`policy.py`](../../../jiuwen_memory/control/policy.py)
- [`space.py`](../../../jiuwen_memory/control/space.py)
- [`types.py`](../../../jiuwen_memory/control/types.py)
- [`common/errors.py`](../../../jiuwen_memory/common/errors.py)

## 1. Control-Layer Call Flow

```text
MemoryAPI
  ├── PermissionManager.check      authorization at the API boundary
  ├── PolicyManager               admin runtime policies
  ├── Governor                    inspect / trace / audit
  ├── SpaceManager                Space management plane
  └── MemoryEngine                authorized data-plane orchestration
       ├── write -> RawPayload(assets) -> Ingestor(capability check/asset mapping) -> Pipeline/Classifier -> IndexBuilder
       ├── recall -> Pipeline -> Retriever
       ├── list/get -> Storage
       ├── update/delete -> LifecycleManager + IndexBuilder
       └── evolve -> JobFactory -> Scheduler -> Evolver

Long-running ingestion:
HTTP/API -> IngestJobController -> background task -> MemoryAPI.add
```

`MemoryEngine` trusts that the target Scope has already passed API authorization and does not call
`PermissionManager.check()` again. `PermissionManager`, `Governor`, `PolicyManager`, and
`SpaceManager` are peer control components injected into `LocalMemoryAPI` alongside Engine.

## 2. ControlOperator Base Class

```python
from jiuwen_memory.control.base import ControlOperator, ControlOperatorType
```

Every control operator implements:

| API | Return value | Description |
|---|---|---|
| `operator_type()` | `ControlOperatorType` | Returns the operator's self-described type |
| `health()` | `None` | Returns `None` when healthy and raises an exception otherwise |

`ControlOperatorType` contains `ENGINE`, `PIPELINE`, `LIFECYCLE`, `GOVERNOR`, `PERMISSION`,
`SCHEDULER`, `INGEST_JOB`, `POLICY`, and `SPACE`.

## 3. MemoryEngine API

```python
from jiuwen_memory.control.engine import MemoryEngine
```

Every `MemoryEngine` method is an asynchronous coroutine. `MemoryAPI` bridges synchronous calls at
the interface boundary; Engine does not create an event loop internally.

### 3.1 Data-Plane Methods

| API | Return value | Description |
|---|---|---|
| `await write(content, scope, source=TEXT, *, assets=None, tags=None, system_metadata=None, user_metadata=None, occurred_at=None)` | `list[MemoryUnit]` | Normalizes input and completes the hot-path write |
| `await batch_write(items, *, continue_on_error=True)` | `BatchWriteResult` | Reuses `write` in input order and returns a per-item success or error |
| `await recall(scope, query)` | `RetrievalResult` | Delegates full retrieval to the selected Retriever |
| `await list(scope, *, offset=0, limit=100, memory_types=None, extensions=None, filters=None)` | `MemoryListResult` | Lists `/memory/` records; excludes original infer input under `/messages/` |
| `await get(unit_id, scope, as_of=None)` | `MemoryUnit` | Performs a current point read or traverses the version chain for a valid-time read |
| `await update(unit_id, scope, patch)` | `MemoryUnit` | Creates a new version or overwrites in place according to `MemoryPatch.mode` |
| `await delete(selector)` | `list[str]` | Forgets, archives, downweights, or physically deletes selected units |
| `await purge_space(org, space)` | `list[str]` | Removes records and derived indexes from every child Scope in a Space |

`write` only makes a defensive copy of `assets` into `RawPayload.assets`. The Ingestor decides how
to map those references into one or more `MemoryUnit.segments`; the Engine no longer assumes and
backfills a “first Segment.” Before normalization, the Ingestor checks whether `source` belongs to
the active `Normalizer.modalities()`. An unsupported source raises `UnsupportedCapabilityError`
before any MemoryUnit, Storage write, or index write is produced.

### 3.2 Authorization-Context Helpers

These methods only return information needed for API authorization. The API itself still invokes
`PermissionManager`:

| API | Return value | Description |
|---|---|---|
| `await permission_context_for_unit(unit_id: str, scope: Scope)` | `PermissionContext` | Reads the existing memory's type, tags, routing fields, and actual Scope from the source of truth |
| `await list_with_permission_contexts(scope, *, offset=0, limit=100, memory_types=None, extensions=None, filters=None)` | `(MemoryListResult, list[PermissionContext])` | Returns the page and corresponding permission contexts in one query so pagination results and authorization evidence cannot drift |
| `await permission_contexts_for_delete(selector: DeleteSelector)` | `list[PermissionContext]` | Resolves resources matched by a selector so the API can authorize all of them before deletion |

### 3.3 Scheduling and Policy Methods

| API | Return value | Description |
|---|---|---|
| `await evolve(scope, mode, channel=BACKGROUND)` | `str` | Creates an Evolve Job, submits it to Scheduler, and returns its job ID |
| `await admin_get(key)` | `str` | Abstract policy-read entry point |
| `await admin_set(key, value)` | `None` | Abstract policy-update entry point |
| `await admin_all()` | `dict[str, str]` | Abstract policy-list entry point |

All three `admin_*` methods on the current `InMemoryEngine` and `CloudEngine` raise
`NotImplementedError`. `LocalMemoryAPI.admin_*` delegates directly to its injected `PolicyManager`,
so applications should call `MemoryAPI.admin_*` rather than built-in Engine `admin_*` methods.

### 3.4 Write-Path Switches

Engine reads system control fields only from `system_metadata`:

| Switch | Path |
|---|---|
| Default `infer=false` | Ingestor -> optional Classifier -> `IndexBuilder.build(ALL)`; original content enters `/memory/` directly |
| `infer=true` | Original content is written to `/messages/`; Evolver collects context, extracts derived candidates, and writes them to `/memory/` |
| `procedural=true` | Original content is not persisted independently; Extractor combines it into one PROCEDURAL memory before persistence |
| `infer=true, middle=true` | Original content enters `/memory/` as a medium-term WORKING memory, then a scheduled `MiddleToLongJob` converts it to long-term memory |

`middle=true` without `infer=true` is invalid. Control fields such as `infer`, `procedural`, and
`middle` must not fall back to `user_metadata`.

### 3.5 Update and Delete Semantics

- `UpdateMode.SUPERSEDE` creates a new ID by default, marks the old unit `SUPERSEDED`, and sets the
  new unit's `supersedes` field to the old ID.
- `UpdateMode.OVERWRITE` updates in place and retains the ID; old content remains only in audit
  records.
- `DeleteMode.FORGET` and `ARCHIVE` first write back lifecycle state and then call
  `IndexBuilder.remove(SOFT)` to remove the unit from retrieval.
- `DeleteMode.DOWNWEIGHT` keeps the unit ACTIVE and only lowers `system_metadata.importance`.
- `DeleteMode.PURGE` is the only physical deletion path and recursively deletes provenance
  descendants.

## 4. MemoryPipeline API

```python
from jiuwen_memory.control.pipeline import MemoryPipeline, PipelineBinding
```

`MemoryPipeline` only selects a group of already-assembled cross-layer components. It does not
implement Construction or Retrieval algorithms.

| API | Return value | Description |
|---|---|---|
| `select_for_write(units)` | `PipelineBinding` | Selects a profile from write-unit system metadata |
| `select_for_recall(query)` | `PipelineBinding` | Selects a profile from query extensions or system filters |

`PipelineBinding`:

```python
@dataclass(frozen=True)
class PipelineBinding:
    name: str
    index_builder: IndexBuilder
    retriever: Retriever
    evolver: Evolver
    classifier: Classifier | None = None
```

The default configuration does not declare `pipeline.default`. Pipeline routing is therefore
disabled by default, and Engine uses its injected single-profile components.

## 5. LifecycleManager API

```python
from jiuwen_memory.control.lifecycle import LifecycleManager
```

| API | Return value | Description |
|---|---|---|
| `transition(scope, unit_ids, target)` | `None` | Non-destructively changes lifecycle state within a full Scope |
| `supersede(scope, unit_id, invalid_at)` | `MemoryUnit` | Marks an old version SUPERSEDED and sets its valid-time invalidation boundary |
| `sweep()` | `list[str]` | Scans expired ACTIVE or SUPERSEDED memories and applies the runtime policy |

Allowed state transitions in the current `kv` implementation:

| Current state | Allowed targets |
|---|---|
| `ACTIVE` | ACTIVE, ARCHIVED, FORGOTTEN, SUPERSEDED |
| `ARCHIVED` | ARCHIVED, FORGOTTEN |
| `SUPERSEDED` | SUPERSEDED, FORGOTTEN |
| `FORGOTTEN` | FORGOTTEN |

LifecycleManager does not physically delete data. It calls `Storage.update(..., FORWARD_ONLY)` to
write back only record state. Engine/IndexBuilder separately orchestrates removal from retrieval.

## 6. Governor API

```python
from jiuwen_memory.control.governance import Governor
```

| API | Return value | Description |
|---|---|---|
| `inspect(unit_ids, scope)` | `list[MemoryUnit]` | Reads complete memories, including invalid versions, inside an authorized Scope |
| `trace(unit_id, scope)` | `list[MemoryUnit]` | Recursively follows `provenance` to its sources while preventing cycles |
| `audit(filters, limit=100)` | `list[AuditEvent]` | Delegates audit-event queries to AuditLogger |

Governor is the read-only side of governance. Editing, forgetting, and cleanup still go through
MemoryAPI/Engine.

## 7. PermissionManager API

```python
from jiuwen_memory.control.permission import PermissionManager
from jiuwen_memory.control.types import Action, Grant, PermissionContext
```

| API | Return value | Description |
|---|---|---|
| `grant(grant)` | `None` | Adds a cross-Scope grant |
| `revoke(grant)` | `None` | Idempotently revokes a grant; exact matching is implementation-defined |
| `check(actor, target, action, context=None)` | `bool` | Determines whether actor may perform an action on target |
| `routing_fields()` | `tuple[str, ...]` | Returns PermissionContext fields used for policy routing; empty by default |

`Action` contains `READ`, `WRITE`, `UPDATE`, `DELETE`, and `SHARE`. `check()` returns only a Boolean.
When it returns `False`, the API layer raises `PermissionDeniedError` and records an audit event.

`PermissionContext` adds resource type, memory type, pipeline, unit ID, actual Scope, tags, and
system metadata. A context for an existing memory must be built from the source of truth and must
not trust caller declarations.

## 8. Scheduler, Job, and JobFactory APIs

### 8.1 Scheduler

```python
from jiuwen_memory.control.scheduler import Scheduler
```

| API | Return value | Description |
|---|---|---|
| `await submit(job, channel)` | `str` | Submits a one-time or scheduled Job and returns its ID |
| `status(job_id)` | `JobInfo` | Returns job status; raises `NotFoundError` if absent |
| `cancel(job_id)` | `None` | Idempotently cancels a job that has not completed |

`Channel.HOT` and `Channel.BACKGROUND` are business execution labels; actual asynchronous behavior
depends on the Scheduler implementation. Default `in_process` awaits `job.run()` inside `submit()`,
even for BACKGROUND. Only `async_timer` enqueues work and returns the job ID immediately.

### 8.2 Job

```python
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
```

| API | Return value | Description |
|---|---|---|
| `await Job.run()` | `JobInfo` | Runs a job once and returns its result status |
| `JobFactory.register(job_type, builder)` | `None` | Registers a builder for a Job type during assembly |
| `JobFactory.get_job(job_type, scope, **kwargs)` | `Job` | Supplies runtime Scope and arguments and creates a Job |

`Job.interval=0` means one-time execution; `interval>0` declares a periodic job. Built-in `JobType`
contains `EVOLVE` and `MIDDLE_TO_LONG`.

## 9. IngestJobController API

`IngestJobController` manages long-running ingestion such as video processing. It is separate from
the Evolver `Scheduler` job system.

```python
from jiuwen_memory.control.ingest_job import IngestJobController
```

| API | Return value | Description |
|---|---|---|
| `submit(*, payload_id, source_ref, scope, task)` | `IngestSubmission` | Submits work or reuses a job by Scope + payload_id |
| `status(job_id, *, scope)` | `IngestJob` | Returns a job only when it belongs to the requested Scope |
| `close(*, wait=True)` | `None` | Closes worker resources and can wait for submitted work |

`IngestSubmission` contains `job` and `reused`. `IngestJob` contains `id`, `payload_id`,
`source_ref`, `scope`, `status`, timestamps, `unit_ids`, and `error`.

## 10. PolicyManager API

```python
from jiuwen_memory.control.policy import PolicyManager
```

| API | Return value | Description |
|---|---|---|
| `get(key)` | `str` | Reads a declared runtime policy |
| `set(key, value)` | `None` | Updates an existing mutable policy |
| `all()` | `dict[str, str]` | Returns a policy snapshot |

PolicyManager does not manage heavyweight static configuration such as backend addresses or Store
types. The built-in `dict` target does not permit adding an unknown key and raises `PolicyError` for
one.

## 11. SpaceManager API

```python
from jiuwen_memory.control.space import SpaceManager
```

| API | Return value | Description |
|---|---|---|
| `create(spec)` | `SpaceInfo` | Creates a globally unique, non-empty Space ID |
| `get(org, space)` | `SpaceInfo` | Reads Space metadata |
| `list(org, *, status=None, limit=100, cursor=None)` | `list[SpaceInfo]` | Lists Spaces by status; the implementation interprets cursor |
| `update(org, space, patch)` | `SpaceInfo` | Updates display name, status, principal path, policy, or metadata |
| `archive(org, space)` | `SpaceInfo` | Marks a Space ARCHIVED |
| `delete(org, space)` | `SpaceDeleteResult` | Deletes management-plane records and KV content |
| `export(org, space, *, include_audit=True)` | `str` | Creates an export record and returns its export ID |
| `usage(org, space)` | `SpaceUsage` | Returns usage that can be counted at the KV layer |
| `get_policy(org, space)` | `SpacePolicy` | Reads Space policy |
| `set_policy(org, space, policy)` | `SpacePolicy` | Replaces Space policy |
| `list_members(org, space)` | `list[SpaceMember]` | Lists members |
| `add_member(org, space, member)` | `None` | Adds a member or updates its role |
| `remove_member(org, space, member_scope)` | `None` | Removes a member |

Calling `SpaceManager.delete()` directly cleans only management-plane/KV data and must not be
interpreted as proof that all derived indexes were removed. When the standard API deletes a Space,
Engine `purge_space()` and SpaceManager jointly clean the data and management planes.

## 12. Public Data Types

### 12.1 Permission and Scheduling Enums

| Type | Values |
|---|---|
| `Action` | `READ`, `WRITE`, `UPDATE`, `DELETE`, `SHARE` |
| `PrincipalPath` | `USER_AGENT`, `AGENT_USER` |
| `Channel` | `HOT`, `BACKGROUND` |
| `JobStatus` | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `UpdateMode` | `SUPERSEDE`, `OVERWRITE` |
| `DeleteMode` | `FORGET`, `ARCHIVE`, `DOWNWEIGHT`, `PURGE` |
| `SpaceStatus` | `ACTIVE`, `FROZEN`, `ARCHIVED`, `DELETING`, `DELETED` |

### 12.2 Write, Update, and Delete Types

| Type | Primary fields | Description |
|---|---|---|
| `BatchWriteItem` | content, scope, source, assets, tags, system_metadata, user_metadata, occurred_at, stream_id, sequence, idempotency_key | One normalized batch-write input item |
| `BatchWriteOutcome` | index, item, units, error, error_type | Per-item result aligned with the original input position |
| `BatchWriteResult` | outcomes | Complete batch result in input order |
| `MemoryListResult` | items, count | Current page and total matches before pagination |
| `MemoryPatch` | content, tier, tags, both metadata fields, t_valid, t_invalid, mode | Only non-`None` fields apply; metadata is merge-updated |
| `DeleteSelector` | unit_ids, scope, tags, before, mode | Selection fields are ANDed; at least one selector is required |

### 12.3 Permission Types

| Type | Primary fields | Description |
|---|---|---|
| `Grant` | grantor, grantee, actions, expires_at | One cross-Scope grant |
| `PermissionContext` | resource_type, memory_type, pipeline, unit_id, scope, tags, metadata | Resource context for permission policy evaluation |

### 12.4 Space Types

| Type | Purpose |
|---|---|
| `SpacePolicy` | require_space, principal_path, isolation strategy, retention, quotas, and index/pipeline profiles |
| `SpaceSpec` | Creation input: org, space, display_name, principal_path, policy, metadata |
| `SpaceInfo` | Current Space metadata plus creation/archive timestamps |
| `SpacePatch` | Update input in which only non-None fields apply |
| `SpaceMember` | Member Scope, role, creation time, and expiration time |
| `SpaceUsage` | Memory/message/index counts, storage bytes, and audit count |
| `SpaceDeleteResult` | Deletion counts, final status, and audit-event ID |

## 13. Producers and Configuration Namespaces

| Producer | `TOP_NAME` | Implementation directory |
|---|---|---|
| `EngineProducer` | `engine` | `engine_impl/` |
| `PipelineProducer` | `pipeline` | `pipeline_impl/` |
| `LifecycleProducer` | `lifecycle` | `lifecycle_impl/` |
| `GovernorProducer` | `governor` | `governance_impl/` |
| `PermissionProducer` | `permission` | `permission_impl/` |
| `SchedulerProducer` | `scheduler` | `scheduler_impl/` |
| `IngestJobProducer` | `ingest_job` | `job_impl/` |
| `PolicyProducer` | `policy` | `policy_impl/` |
| `SpaceProducer` | `space` | `space_impl/` |
| `JobFactoryProducer` | `job_factory` | `jobs_impl/` |

`JobFactory` is not a `ControlOperator`, but it is also assembled through a Producer.
`control.bootstrap.register_controllers()` imports control implementations and triggers
registration. `jobs_impl` is imported on demand when Engine assembles JobFactory.

Configuration uses the same two-level namespace:

```yaml
permission:
  default:
    target: sqlite
    params:
      db_path: ./data/permissions.db
```

Overriding an instance replaces its `params` as a whole. When overriding dependency-heavy
instances such as `engine.default` or `pipeline.default`, repeat every named dependency that is
still required.

## 14. Configurable Implementations

### 14.1 Engine and Pipeline

| Namespace | `target` | Implementation | Function | Dependencies and primary parameters |
|---|---|---|---|---|
| `engine` | `in_memory` | `InMemoryEngine` | Local compatibility orchestration; accepts only `scope.space == ""`; supports write/recall/list/update/delete/evolve | ingestor, index_builder, retriever, storage, scheduler, evolver, lifecycle; optional classifier, pipeline, job_factory |
| `engine` | `cloud` | `CloudEngine` | Supports non-empty Space and cloud message_type/profile orchestration and validates unit Scope consistency | Same dependencies; `message_type_key`, `default_message_type`, `default_pipeline_name` |
| `pipeline` | `metadata` | `MetadataPipeline` | Selects a `PipelineBinding` from system_metadata, query.extensions, or an equality FilterExpr that is logically required | `profiles`, `routes`, `fallback`, `route_key` |

On writes, `MetadataPipeline` reads the first non-empty `unit.system_metadata[route_key]`. On
queries, it reads `query.extensions[route_key]` first, then extracts a logically required equality
filter for `system_metadata.<route_key>`. A route value may map to a profile or directly name a
profile. Missing values fall back to `fallback`.

### 14.2 Lifecycle, Governance, and Permission

| Namespace | `target` | Implementation | Function | Parameters and boundaries |
|---|---|---|---|---|
| `lifecycle` | `kv` | `KVLifecycleManager` | Point-reads through Storage, writes state with `FORWARD_ONLY`, and sweeps according to Policy | `storage`, `policy` |
| `governor` | `in_memory` | `InMemoryGovernor` | Scope-local inspect/trace and shared AuditLogger queries | `storage`, `audit` |
| `permission` | `allow_all` | `AllowAllPermissionManager` | `check()` always returns True; intended only for tests/demos | None; must not be used as a routing fallback |
| `permission` | `sqlite` | `SQLitePermissionManager` | Persists Grants and supports root, owner-cover, Space boundaries, and expiration | `db_path`, default `:memory:` |
| `permission` | `routing` | `RoutingPermissionManager` | Selects a named PermissionManager delegate from PermissionContext fields | `route_key`, `routes`, required `fallback` |

The `routing` fallback must be a least-privilege policy; assembly rejects `allow_all`. Route values
must be keys explicitly declared in `routes` and cannot directly name an underlying policy. The API
feeds fields returned by `routing_fields()` back into system filter predicates, binding policy
selection to the data that policy can access.

### 14.3 Scheduling, Ingestion Jobs, Policy, and Space

| Namespace | `target` | Implementation | Function | Primary parameters |
|---|---|---|---|---|
| `scheduler` | `in_process` | `InProcessScheduler` | Runs and awaits the Job immediately inside `submit()` | None |
| `scheduler` | `async_timer` | `AsyncTimerScheduler` | FIFO within one Scope, parallel across Scopes, with periodic TimerWheel support | `tick_interval`, default `10` seconds |
| `ingest_job` | `in_process` | `InProcessIngestJobController` | Background ingestion through ThreadPoolExecutor, persistent KV status, payload idempotency | `ingest_max_workers` default `1`, `ingest_max_pending_jobs` default `2`, `kv_store` |
| `policy` | `dict` | `DictPolicyManager` | In-process mutable policy map; only known keys may be updated | `policies` |
| `space` | `kv` | `KVSpaceManager` | Maintains Space metadata, policy, members, usage, and export records through Storage KV | `storage` |
| `job_factory` | `default` | `JobFactory` | Registers builders for `EvolveJob` and `MiddleToLongJob` | storage, evolver, lifecycle, index_builder, llm; middle parameters and optional lock |

For periodic jobs, `async_timer` requires `interval >= tick_interval`. Timing precision is bounded by
one tick, and jobs of the same type in the same Scope do not accumulate concurrently.

The payload idempotency key of `InProcessIngestJobController` includes the complete Scope. A
pending/running/succeeded job with the same Scope + payload_id is reused. The same payload_id
pointing to a different source conflicts, while failed jobs may be retried. After a service restart,
persisted pending/running jobs are marked failed and do not resume automatically.

## 15. Default Assembly

Without user configuration, Control uses these default instances:

| Namespace | Default target | Notes |
|---|---|---|
| `engine.default` | `in_memory` | Local compatibility domain with an empty Space |
| `pipeline` | Not declared by default | Single-profile mode |
| `lifecycle.default` | `kv` | Storage + Policy |
| `governor.default` | `in_memory` | Storage + SQLite AuditLogger |
| `permission.default` | `sqlite` | `db_path=:memory:` |
| `scheduler.default` | `in_process` | Awaits the Job during submit |
| `ingest_job.default` | `in_process` | Shared `kv_store.default` |
| `policy.default` | `dict` | Four built-in policies |
| `space.default` | `kv` | Shared Storage |
| `job_factory.default` | `default` | Evolve and MiddleToLong Jobs |

Built-in Policy keys:

```yaml
rerank.enabled: "true"
lifecycle.expired_active.target: "forgotten"
lifecycle.superseded.target: "forgotten"
scope.require_space: "false"
```

## 16. Configuration Examples

### 16.1 Persistent Permissions and Asynchronous Scheduling

```yaml
permission:
  default:
    target: sqlite
    params:
      db_path: ./data/permissions.db

scheduler:
  default:
    target: async_timer
    params:
      tick_interval: 10

ingest_job:
  default:
    target: in_process
    params:
      kv_store: default
      ingest_max_workers: 2
      ingest_max_pending_jobs: 8

policy:
  default:
    target: dict
    params:
      policies:
        rerank.enabled: "true"
        lifecycle.expired_active.target: "archived"
        lifecycle.superseded.target: "forgotten"
        scope.require_space: "true"
```

When overriding `policy.default.params.policies`, include every key still read by another component.
Unknown keys cannot be added later through `set()`.

### 16.2 Permission Routing

```yaml
permission:
  default:
    target: routing
    params:
      route_key: memory_type
      fallback: strict
      routes:
        coding: strict
        episodic: standard
  strict:
    target: sqlite
    params:
      db_path: ./data/strict-permissions.db
  standard:
    target: allow_all
```

This example explicitly allows `episodic` to use a permissive policy, while missing or unknown
route values must fall back to `strict`.

### 16.3 Metadata Pipeline

The named `constructor`, `retriever`, `evolver`, and `classifier` instances referenced by these
profiles must be declared in their respective namespaces first:

```yaml
pipeline:
  default:
    target: metadata
    params:
      route_key: memory_type
      fallback: default
      routes:
        coding: coding
      profiles:
        default:
          index_builder: default
          retriever: default
          evolver: default
          classifier: default
        coding:
          index_builder: coding
          retriever: coding
          evolver: coding
          classifier: coding
```

After `pipeline.default` is declared, `InMemoryEngine` and `CloudEngine` detect and inject it
automatically; enabling the pipeline alone does not require overriding all of `engine.default`.

## 17. Requirements for Custom Implementations

A new Control operator must at least:

1. keep only the abstract contract in the top-level interface file and place the implementation in
   the corresponding `*_impl/` directory;
2. implement its business methods, `operator_type()`, and `health()`;
3. register with the corresponding Producer through `register("target")`;
4. keep Engine limited to orchestration of injected components, without binding a concrete backend,
   invoking an LLM directly, or authorizing a request again;
5. keep Engine methods async and bridge blocking synchronous components through threads;
6. keep LifecycleManager limited to non-destructive marking within one Scope;
7. never trust caller-declared properties for an existing resource in PermissionManager;
8. have Pipeline return only `PipelineBinding`, without implementing Construction/Retrieval
   algorithms;
9. have Scheduler decide when to run work, not what the Job does; and
10. preserve Scope isolation for Space, job, and grant state.

## 18. Complete Method Contracts

### 18.1 Synchronous and Asynchronous Boundaries

| Component | Invocation | Contract |
|---|---|---|
| `MemoryEngine` | All methods are `async` | The API layer bridges synchronous calls; Engine does not create a new event loop |
| `Scheduler.submit` / `Job.run` | `async` | The Scheduler target decides whether `submit` waits for completion |
| Pipeline/Lifecycle/Governor/Permission/Policy/Space | Synchronous | Engine/API orchestration is responsible for a thread bridge when an implementation uses blocking I/O |
| `IngestJobController` | Synchronous submit and query | Built-in `in_process` runs `task` in a thread pool; work is normally pending/running when `submit` returns |

### 18.2 Complete Signatures for Non-Engine Interfaces

```python
# Pipeline
select_for_write(units: list[MemoryUnit]) -> PipelineBinding
select_for_recall(query: RetrievalQuery) -> PipelineBinding

# Lifecycle
transition(scope: Scope, unit_ids: list[str], target: LifecycleState) -> None
supersede(scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit
sweep() -> list[str]

# Governor
inspect(unit_ids: list[str], scope: Scope) -> list[MemoryUnit]
trace(unit_id: str, scope: Scope) -> list[MemoryUnit]
audit(filters: dict[str, str], limit: int = 100) -> list[AuditEvent]

# Permission
grant(grant: Grant) -> None
revoke(grant: Grant) -> None
check(
    actor: Scope,
    target: Scope,
    action: Action,
    context: PermissionContext | None = None,
) -> bool
routing_fields() -> tuple[str, ...]

# Scheduler / Job
async submit(job: Job, channel: Channel) -> str
status(job_id: str) -> JobInfo
cancel(job_id: str) -> None
async Job.run() -> JobInfo

# Ingest job
submit(
    *, payload_id: str, source_ref: str, scope: Scope, task: IngestTask
) -> IngestSubmission
status(job_id: str, *, scope: Scope) -> IngestJob
close(*, wait: bool = True) -> None

# Policy
get(key: str) -> str
set(key: str, value: str) -> None
all() -> dict[str, str]
```

Complete `SpaceManager` signatures:

```python
create(spec: SpaceSpec) -> SpaceInfo
get(org: str, space: str) -> SpaceInfo
list(
    org: str,
    *,
    status: SpaceStatus | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> list[SpaceInfo]
update(org: str, space: str, patch: SpacePatch) -> SpaceInfo
archive(org: str, space: str) -> SpaceInfo
delete(org: str, space: str) -> SpaceDeleteResult
export(org: str, space: str, *, include_audit: bool = True) -> str
usage(org: str, space: str) -> SpaceUsage
get_policy(org: str, space: str) -> SpacePolicy
set_policy(org: str, space: str, policy: SpacePolicy) -> SpacePolicy
list_members(org: str, space: str) -> list[SpaceMember]
add_member(org: str, space: str, member: SpaceMember) -> None
remove_member(org: str, space: str, member: Scope) -> None
```

## 19. Public Data Type Details

### 19.1 Batch Writes and Lists

| Type.field | Type | Default/required | Semantics |
|---|---|---|---|
| `MemoryListResult.items` | `list[MemoryUnit]` | `[]` | Current page |
| `MemoryListResult.count` | `int` | `0` | Total matches before pagination |
| `BatchWriteItem.content` | `str` | Required | Content to write |
| `BatchWriteItem.scope` | `Scope \| None` | `None` | Must be normalized to a concrete Scope by the API layer before Engine invocation |
| `BatchWriteItem.source` | `Modality \| None` | `None` | Input modality; the API may supply a default |
| `BatchWriteItem.assets` | `list[str] \| None` | `None` | Related asset references |
| `BatchWriteItem.tags` | `list[str] \| None` | `None` | Memory tags |
| `BatchWriteItem.system_metadata` | `dict[str, MetadataValueType] \| None` | `None` | System control metadata |
| `BatchWriteItem.user_metadata` | `dict[str, MetadataValueType] \| None` | `None` | User metadata |
| `BatchWriteItem.occurred_at` | `datetime \| None` | `None` | Event occurrence time |
| `BatchWriteItem.stream_id` | `str` | `""` | Caller data-stream identifier |
| `BatchWriteItem.sequence` | `int \| None` | `None` | Sequence number within the stream |
| `BatchWriteItem.idempotency_key` | `str` | `""` | Caller-provided idempotency identifier |
| `BatchWriteOutcome.index` | `int` | Required | Original input index |
| `BatchWriteOutcome.item` | `BatchWriteItem` | Required | Original input item |
| `BatchWriteOutcome.units` | `list[MemoryUnit]` | `[]` | Units produced successfully for this item |
| `BatchWriteOutcome.error` | `str` | `""` | Failure summary; empty on success |
| `BatchWriteOutcome.error_type` | `str` | `""` | Domain exception class name; unexpected errors use `InternalError` |
| `BatchWriteResult.outcomes` | `list[BatchWriteOutcome]` | `[]` | Results in original input order |

`batch_write(..., continue_on_error=False)` does not re-raise the first error. It returns an outcome
for that error and marks subsequent unexecuted items with `error_type="Skipped"`.

### 19.2 Updates, Deletes, and Permissions

| Type.field | Type | Default | Semantics |
|---|---|---|---|
| `MemoryPatch.content` | `str \| None` | `None` | Replacement content; `None` means unchanged |
| `MemoryPatch.tier` | `MemoryTier \| None` | `None` | Replacement tier |
| `MemoryPatch.tags` | `list[str] \| None` | `None` | Replacement tags |
| `MemoryPatch.system_metadata` | `dict[str, MetadataValueType] \| None` | `None` | Merge-updated into existing system metadata |
| `MemoryPatch.user_metadata` | `dict[str, MetadataValueType] \| None` | `None` | Merge-updated into existing user metadata |
| `MemoryPatch.t_valid` / `t_invalid` | `datetime \| None` | `None` | Valid-time boundaries |
| `MemoryPatch.mode` | `UpdateMode` | `SUPERSEDE` | Creates a new version or overwrites in place |
| `DeleteSelector.unit_ids` | `list[str]` | `[]` | ID selector |
| `DeleteSelector.scope` | `Scope \| None` | `None` | Restricts full Scope; Engine defines the scan boundary when absent |
| `DeleteSelector.tags` | `list[str]` | `[]` | Tag selector |
| `DeleteSelector.before` | `datetime \| None` | `None` | Time upper bound |
| `DeleteSelector.mode` | `DeleteMode` | `FORGET` | Forget, archive, downweight, or physically delete |
| `Grant.grantor` / `grantee` | `Scope` | `Scope()` | Granting and receiving principals |
| `Grant.actions` | `list[Action]` | `[]` | Allowed actions |
| `Grant.expires_at` | `datetime \| None` | `None` | Expiration; `None` means no time limit |
| `PermissionContext.resource_type` | `str` | `""` | Resource type |
| `PermissionContext.memory_type` | `str` | `""` | Memory type/routing value |
| `PermissionContext.pipeline` | `str` | `""` | Pipeline name |
| `PermissionContext.unit_id` | `str` | `""` | Existing memory ID |
| `PermissionContext.scope` | `Scope` | `Scope()` | Actual resource Scope |
| `PermissionContext.tags` | `tuple[str, ...]` | `()` | Immutable tag snapshot |
| `PermissionContext.metadata` | `dict[str, str]` | `{}` | System fields used for permission routing |

At least one of `DeleteSelector.unit_ids`, `tags`, or `before` must be non-empty. Multiple selectors
are combined with AND.

### 19.3 Jobs and Long-Running Ingestion

| Type.field | Type | Default/required | Semantics |
|---|---|---|---|
| `Job.scope` | `Scope` | `Scope()` | Job isolation boundary |
| `Job.interval` | `int` | `0` | `0` means one-time; a positive value is the periodic interval |
| `JobInfo.id` | `str` | `""` | Scheduler job ID |
| `JobInfo.channel` | `Channel` | `BACKGROUND` | Business channel label |
| `JobInfo.mode` | `str` | `""` | Job implementation type or mode |
| `JobInfo.scope` | `Scope` | `Scope()` | Job Scope |
| `JobInfo.status` | `JobStatus` | `PENDING` | Current state |
| `JobInfo.detail` | `dict[str, str]` | `{}` | Start/end times, error, and business result |
| `IngestJob.id` | `str` | Required | Long-running ingestion job ID |
| `IngestJob.payload_id` / `source_ref` | `str` | Required | Idempotency payload and source reference |
| `IngestJob.scope` | `Scope` | Required | Actual job Scope |
| `IngestJob.status` | `str` | Required | `pending` / `running` / `succeeded` / `failed` |
| `IngestJob.created_at` / `updated_at` | `datetime` | Required | Creation and last-update timestamps |
| `IngestJob.unit_ids` | `tuple[str, ...]` | `()` | IDs successfully produced |
| `IngestJob.error` | `str` | `""` | Failure summary |
| `IngestSubmission.job` | `IngestJob` | Required | Newly created or reused job |
| `IngestSubmission.reused` | `bool` | Required | Whether an existing idempotent job was reused |

### 19.4 Space Types

| Type | Fields (type; default) |
|---|---|
| `SpacePolicy` | `require_space: bool=false`; `principal_path: PrincipalPath=USER_AGENT`; `storage_isolation_strategy: str="metadata_filter"`; `retention/quotas/index_profiles/pipeline_profiles: dict[str, str]={}` |
| `SpaceSpec` | `org/space/display_name: str=""`; `principal_path=USER_AGENT`; `policy=SpacePolicy()`; `metadata: dict[str, str]={}` |
| `SpaceInfo` | Main `SpaceSpec` fields plus `status: SpaceStatus=ACTIVE` and `created_at/archived_at: datetime \| None=None` |
| `SpacePatch` | Optional `display_name/status/principal_path/policy/metadata`; `None` means unchanged |
| `SpaceMember` | `scope: Scope=Scope()`; `role: str="member"`; `created_at/expires_at: datetime \| None=None` |
| `SpaceUsage` | `org/space: str=""`; `memory_count/message_count/index_count/storage_bytes/audit_count: int=0` |
| `SpaceDeleteResult` | `org/space: str=""`; `deleted_counts: dict[str, int]={}`; `status: SpaceStatus=DELETED`; `audit_event_id: str=""` |

## 20. Exceptions, Partial Success, and Job State

### 20.1 Common Exceptions from Built-in Implementations

| Scenario | Interface | Exception/result |
|---|---|---|
| `offset < 0` or `limit <= 0` | Engine `list` | `ValidationError` |
| Unit absent or no valid version at `as_of` | Engine `get/update` | `NotFoundError` |
| DeleteSelector has no ID/tag/before | Engine `delete` | `ValidationError` |
| `InMemoryEngine` receives a non-empty Space | Any data-plane method | `ValidationError` |
| `source` is outside the active Normalizer capability set | Engine `write` / `batch_write` | `UnsupportedCapabilityError` |
| Invalid lifecycle transition | `transition` / `supersede` | `ValidationError` or `PolicyError` |
| Job absent | Scheduler `status` | `NotFoundError` |
| Controller closed or queue full | Ingest `submit` | `BackendError` |
| Same Scope + payload_id points to another source | Ingest `submit` | `ConflictError` |
| Duplicate Space | `SpaceManager.create` | `ConflictError` |
| Space absent | Space read/write methods | `NotFoundError` |
| Empty `org`/`space`, `limit <= 0`, or invalid cursor | Space methods | `ValidationError` |
| Unknown Policy key | `PolicyManager.get/set` | `PolicyError` |

This table describes built-in targets. It intentionally does not attribute API-layer
`PermissionDeniedError` to Engine because Engine trusts that the incoming Scope is authorized.

### 20.2 Partial-Success Boundaries for Writes and Updates

- `batch_write` isolates each call to `write`; it provides no transaction for the entire batch.
- After Ingestor/Classifier completes, `write(infer=false)` invokes IndexBuilder. If a multi-Store
  write fails midway, the source record or partial indexes may remain.
- `update(SUPERSEDE)` creates the new version before marking the old one SUPERSEDED, so a failure to
  create the new version leaves the old one readable. The full operation is still not a
  cross-backend transaction.
- `delete(FORGET/ARCHIVE)` writes lifecycle state before soft-removing retrieval indexes. If index
  removal fails, source state may already have changed.
- `purge_space` enumerates child Scopes and removes them through IndexBuilder. Its return value is the
  selected unit IDs, not a per-backend deletion report.

### 20.3 Scheduler State Semantics

```text
PENDING -> RUNNING -> SUCCEEDED
                   -> FAILED
PENDING -> CANCELLED
```

- `in_process.submit()` awaits `job.run()` in the current coroutine, so the job is normally already
  SUCCEEDED or FAILED when the job ID is returned.
- `async_timer.submit()` enqueues a one-time job and returns immediately. Jobs are FIFO within a
  Scope and may run in parallel across Scopes.
- Built-in `cancel()` is idempotent best-effort cancellation. A one-time job can normally move to
  CANCELLED only while PENDING; running work is not interrupted.
- A periodic Job's `interval` must be at least `tick_interval`; no later tick fires after
  cancellation.

## 21. Minimal Usage Example

```python
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.types import DeleteMode, DeleteSelector, MemoryPatch


async def update_then_forget(
    engine: MemoryEngine,
    scope: Scope,
    unit_id: str,
) -> list[str]:
    updated = await engine.update(
        unit_id,
        scope,
        MemoryPatch(content="Updated memory content"),
    )
    return await engine.delete(
        DeleteSelector(
            unit_ids=[updated.id],
            scope=scope,
            mode=DeleteMode.FORGET,
        )
    )
```

`MemoryEngine` is an authorized internal orchestration interface. Applications should still prefer
`MemoryAPI`; this example is not a reason to bypass API-layer authorization and audit.
