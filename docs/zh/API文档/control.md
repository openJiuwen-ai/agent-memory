# Control 层 API

Control 层是记忆系统的编排与管理面。它不实现抽取、索引或检索算法，而是把 API 请求路由到 Ingest、Construction、Retrieval、Storage 及各治理组件，并管理权限、生命周期、调度、策略、长耗时任务和 Space。

本文是当前抽象接口、公共类型、内置实现与配置 target 的 API 参考。以下源码是最终依据：

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

## 1. Control 层调用关系

```text
MemoryAPI
  ├── PermissionManager.check      鉴权在 API 边界完成
  ├── PolicyManager               admin 运行时策略
  ├── Governor                    inspect / trace / audit
  ├── SpaceManager                space 管理面
  └── MemoryEngine                已鉴权的数据面编排
       ├── write -> RawPayload(assets) -> Ingestor(能力校验/资产映射) -> Pipeline/Classifier -> IndexBuilder
       ├── recall -> Pipeline -> Retriever
       ├── list/get -> Storage
       ├── update/delete -> LifecycleManager + IndexBuilder
       └── evolve -> JobFactory -> Scheduler -> Evolver

长耗时摄入：
HTTP/API -> IngestJobController -> 后台 task -> MemoryAPI.add
```

`MemoryEngine` 信任传入的 target Scope 已经通过 API 鉴权，不会重复调用 `PermissionManager.check()`。`PermissionManager`、`Governor`、`PolicyManager` 和 `SpaceManager` 是与 Engine 平级注入到 `LocalMemoryAPI` 的控制组件。

## 2. ControlOperator 基类

```python
from jiuwen_memory.control.base import ControlOperator, ControlOperatorType
```

所有控制算子实现：

| API | 返回值 | 说明 |
|---|---|---|
| `operator_type()` | `ControlOperatorType` | 返回算子自描述类型 |
| `health()` | `None` | 健康时返回 `None`，失败时抛异常 |

`ControlOperatorType` 包含 `ENGINE`、`PIPELINE`、`LIFECYCLE`、`GOVERNOR`、`PERMISSION`、`SCHEDULER`、`INGEST_JOB`、`POLICY` 和 `SPACE`。

## 3. MemoryEngine API

```python
from jiuwen_memory.control.engine import MemoryEngine
```

`MemoryEngine` 的方法全部是异步协程。同步调用由 `MemoryAPI` 在接口边界桥接，Engine 内部不负责创建事件循环。

### 3.1 数据面方法

| API | 返回值 | 说明 |
|---|---|---|
| `await write(content, scope, source=TEXT, *, assets=None, tags=None, system_metadata=None, user_metadata=None, occurred_at=None)` | `list[MemoryUnit]` | 规约输入并完成 hot path 写入 |
| `await batch_write(items, *, continue_on_error=True)` | `BatchWriteResult` | 按输入顺序复用 `write`，逐项返回成功或错误 |
| `await recall(scope, query)` | `RetrievalResult` | 委托已选择的 Retriever 执行完整检索 |
| `await list(scope, *, offset=0, limit=100, memory_types=None, extensions=None, filters=None)` | `MemoryListResult` | 列出 `/memory/` 记忆，不包含 `/messages/` infer 原文 |
| `await get(unit_id, scope, as_of=None)` | `MemoryUnit` | 当前点读或沿版本链执行 valid-time 回溯 |
| `await update(unit_id, scope, patch)` | `MemoryUnit` | 按 `MemoryPatch.mode` 新建版本或原地覆盖 |
| `await delete(selector)` | `list[str]` | 按选择器执行遗忘、归档、降权或物理删除 |
| `await purge_space(org, space)` | `list[str]` | 清理 Space 下全部子 Scope 的本体和派生索引 |

`write` 只把 `assets` 防御性复制到 `RawPayload.assets`。资产引用如何分配到一个或多个
`MemoryUnit.segments` 由 Ingestor 决定，Engine 不再假设“首个 Segment”并进行回填。
Ingestor 在规约前校验 `source` 是否属于当前 `Normalizer.modalities()`；不支持时抛出
`UnsupportedCapabilityError`，不会产生 MemoryUnit，也不会进入 Storage 或索引写入。

### 3.2 鉴权上下文辅助方法

这些方法只返回 API 鉴权需要的信息，鉴权动作本身仍由 API 调用 `PermissionManager`：

| API | 返回值 | 说明 |
|---|---|---|
| `await permission_context_for_unit(unit_id: str, scope: Scope)` | `PermissionContext` | 从真源读取已有记忆的类型、标签、路由字段和真实 Scope |
| `await list_with_permission_contexts(scope, *, offset=0, limit=100, memory_types=None, extensions=None, filters=None)` | `(MemoryListResult, list[PermissionContext])` | 一次查询返回当前页与对应权限上下文，避免分页结果和鉴权依据漂移 |
| `await permission_contexts_for_delete(selector: DeleteSelector)` | `list[PermissionContext]` | 解析 selector 将命中的候选资源，供 API 全部鉴权后再执行删除 |

### 3.3 调度与策略方法

| API | 返回值 | 说明 |
|---|---|---|
| `await evolve(scope, mode, channel=BACKGROUND)` | `str` | 创建 Evolve Job，提交给 Scheduler 并返回 job id |
| `await admin_get(key)` | `str` | 抽象契约中的策略读取入口 |
| `await admin_set(key, value)` | `None` | 抽象契约中的策略修改入口 |
| `await admin_all()` | `dict[str, str]` | 抽象契约中的策略列表入口 |

当前 `InMemoryEngine` 与 `CloudEngine` 的三个 `admin_*` 方法均抛 `NotImplementedError`。实际 `LocalMemoryAPI.admin_*` 直接委托注入的 `PolicyManager`，因此应用侧应调用 `MemoryAPI.admin_*`，不要直接调用内置 Engine 的 `admin_*`。

### 3.4 write 路径开关

Engine 只从 `system_metadata` 读取系统控制字段：

| 开关 | 路径 |
|---|---|
| 缺省 `infer=false` | Ingestor -> 可选 Classifier -> `IndexBuilder.build(ALL)`，原文直接进入 `/memory/` |
| `infer=true` | 原文写入 `/messages/`，Evolver 收集上下文、抽取派生候选并落入 `/memory/` |
| `procedural=true` | 原文不单独落盘，Extractor 汇总为一条 PROCEDURAL 记忆后落盘 |
| `infer=true, middle=true` | 原文作为 WORKING 中期记忆进入 `/memory/`，再由定时 `MiddleToLongJob` 转为长期记忆 |

`middle=true` 但没有 `infer=true` 会报错。`infer`、`procedural`、`middle` 等控制字段不得从 `user_metadata` fallback。

### 3.5 update/delete 语义

- `UpdateMode.SUPERSEDE` 默认生成新 ID，旧单元标记 `SUPERSEDED`，新单元的 `supersedes` 指向旧 ID。
- `UpdateMode.OVERWRITE` 原地更新并沿用 ID，旧内容只留在审计记录中。
- `DeleteMode.FORGET` 与 `ARCHIVE` 先回写生命周期，再通过 `IndexBuilder.remove(SOFT)` 退出检索。
- `DeleteMode.DOWNWEIGHT` 保持 ACTIVE，只降低 `system_metadata.importance`。
- `DeleteMode.PURGE` 是唯一物理删除路径，并递归删除 provenance 后代。

## 4. MemoryPipeline API

```python
from jiuwen_memory.control.pipeline import MemoryPipeline, PipelineBinding
```

`MemoryPipeline` 只选择一组已经装配好的跨层组件，不实现 Construction 或 Retrieval 算法。

| API | 返回值 | 说明 |
|---|---|---|
| `select_for_write(units)` | `PipelineBinding` | 根据写入单元的系统元数据选择 profile |
| `select_for_recall(query)` | `PipelineBinding` | 根据查询 extensions 或系统过滤条件选择 profile |

`PipelineBinding`：

```python
@dataclass(frozen=True)
class PipelineBinding:
    name: str
    index_builder: IndexBuilder
    retriever: Retriever
    evolver: Evolver
    classifier: Classifier | None = None
```

默认配置没有声明 `pipeline.default`，因此 Pipeline 路由默认关闭，Engine 使用自身注入的单 profile 组件。

## 5. LifecycleManager API

```python
from jiuwen_memory.control.lifecycle import LifecycleManager
```

| API | 返回值 | 说明 |
|---|---|---|
| `transition(scope, unit_ids, target)` | `None` | 在完整 Scope 内非破坏式修改 lifecycle |
| `supersede(scope, unit_id, invalid_at)` | `MemoryUnit` | 标记旧版本为 SUPERSEDED，并设置 valid-time 失效边界 |
| `sweep()` | `list[str]` | 扫描已到期 ACTIVE 或 SUPERSEDED 记忆，按运行时策略处理 |

当前 `kv` 实现允许的状态流转：

| 当前状态 | 允许目标 |
|---|---|
| `ACTIVE` | ACTIVE、ARCHIVED、FORGOTTEN、SUPERSEDED |
| `ARCHIVED` | ARCHIVED、FORGOTTEN |
| `SUPERSEDED` | SUPERSEDED、FORGOTTEN |
| `FORGOTTEN` | FORGOTTEN |

LifecycleManager 不物理删除，并使用 `Storage.update(..., FORWARD_ONLY)` 只回写本体状态。是否移出检索由 Engine/IndexBuilder 另行编排。

## 6. Governor API

```python
from jiuwen_memory.control.governance import Governor
```

| API | 返回值 | 说明 |
|---|---|---|
| `inspect(unit_ids, scope)` | `list[MemoryUnit]` | 在已鉴权 Scope 内读取完整记忆，包括失效版本 |
| `trace(unit_id, scope)` | `list[MemoryUnit]` | 沿 `provenance` 递归回溯来源链并防止循环 |
| `audit(filters, limit=100)` | `list[AuditEvent]` | 委托 AuditLogger 查询审计事件 |

Governor 是治理的只读侧；编辑、遗忘和清理仍通过 MemoryAPI/Engine 完成。

## 7. PermissionManager API

```python
from jiuwen_memory.control.permission import PermissionManager
from jiuwen_memory.control.types import Action, Grant, PermissionContext
```

| API | 返回值 | 说明 |
|---|---|---|
| `grant(grant)` | `None` | 新增跨 Scope 授权 |
| `revoke(grant)` | `None` | 幂等回收授权；精确匹配规则由实现定义 |
| `check(actor, target, action, context=None)` | `bool` | 判断 actor 是否可以对 target 执行动作 |
| `routing_fields()` | `tuple[str, ...]` | 返回本实现用于策略路由的 PermissionContext 字段；默认空 |

`Action` 包含 `READ`、`WRITE`、`UPDATE`、`DELETE` 和 `SHARE`。`check()` 只返回布尔值；API 层负责在 `False` 时抛 `PermissionDeniedError` 并记录审计。

`PermissionContext` 补充资源类型、memory_type、pipeline、unit_id、真实 Scope、tags 和系统 metadata。已有记忆的 context 必须从真源构造，不能信任调用方声明。

## 8. Scheduler、Job 与 JobFactory API

### 8.1 Scheduler

```python
from jiuwen_memory.control.scheduler import Scheduler
```

| API | 返回值 | 说明 |
|---|---|---|
| `await submit(job, channel)` | `str` | 提交一次性或定时 Job，返回 job id |
| `status(job_id)` | `JobInfo` | 查询任务状态；缺失抛 `NotFoundError` |
| `cancel(job_id)` | `None` | 幂等取消尚未完成的任务 |

`Channel.HOT` 与 `Channel.BACKGROUND` 是业务执行通道标签，是否真的异步取决于 Scheduler 实现。默认 `in_process` 会在 `submit()` 内 `await job.run()`，即使 channel 是 BACKGROUND 也会等待完成；`async_timer` 才会排入异步队列并立即返回 job id。

### 8.2 Job

```python
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
```

| API | 返回值 | 说明 |
|---|---|---|
| `await Job.run()` | `JobInfo` | 执行一次任务并返回结果状态 |
| `JobFactory.register(job_type, builder)` | `None` | 装配期注册某类 Job builder |
| `JobFactory.get_job(job_type, scope, **kwargs)` | `Job` | 运行时补充 Scope 和参数并生成 Job |

`Job.interval=0` 表示一次性任务；`interval>0` 表示定时声明。内置 `JobType` 包含 `EVOLVE` 和 `MIDDLE_TO_LONG`。

## 9. IngestJobController API

`IngestJobController` 管理视频等长耗时摄入任务，和 Evolver `Scheduler` 是两个不同的任务系统。

```python
from jiuwen_memory.control.ingest_job import IngestJobController
```

| API | 返回值 | 说明 |
|---|---|---|
| `submit(*, payload_id, source_ref, scope, task)` | `IngestSubmission` | 提交或按 Scope + payload_id 复用已有任务 |
| `status(job_id, *, scope)` | `IngestJob` | 仅在任务属于请求 Scope 时返回 |
| `close(*, wait=True)` | `None` | 关闭 worker 资源，可等待已提交任务完成 |

`IngestSubmission` 由 `job` 和 `reused` 组成。`IngestJob` 包含 `id`、`payload_id`、`source_ref`、`scope`、`status`、时间、`unit_ids` 和 `error`。

## 10. PolicyManager API

```python
from jiuwen_memory.control.policy import PolicyManager
```

| API | 返回值 | 说明 |
|---|---|---|
| `get(key)` | `str` | 读取已声明的运行时策略 |
| `set(key, value)` | `None` | 修改已存在的可变策略 |
| `all()` | `dict[str, str]` | 返回策略快照 |

PolicyManager 不管理后端地址、Store 类型等重型静态配置。内置 `dict` 实现不允许新增未知 key，未知 key 会抛 `PolicyError`。

## 11. SpaceManager API

```python
from jiuwen_memory.control.space import SpaceManager
```

| API | 返回值 | 说明 |
|---|---|---|
| `create(spec)` | `SpaceInfo` | 创建全局唯一的非空 Space ID |
| `get(org, space)` | `SpaceInfo` | 读取 Space 元数据 |
| `list(org, *, status=None, limit=100, cursor=None)` | `list[SpaceInfo]` | 按状态列出 Space；cursor 由实现解释 |
| `update(org, space, patch)` | `SpaceInfo` | 更新显示名、状态、主体路径、策略或 metadata |
| `archive(org, space)` | `SpaceInfo` | 把 Space 标记为 ARCHIVED |
| `delete(org, space)` | `SpaceDeleteResult` | 删除管理面记录和 KV 内容 |
| `export(org, space, *, include_audit=True)` | `str` | 创建导出记录并返回 export id |
| `usage(org, space)` | `SpaceUsage` | 返回 KV 层可统计的用量 |
| `get_policy(org, space)` | `SpacePolicy` | 读取 Space policy |
| `set_policy(org, space, policy)` | `SpacePolicy` | 替换 Space policy |
| `list_members(org, space)` | `list[SpaceMember]` | 列出成员 |
| `add_member(org, space, member)` | `None` | 新增成员或更新角色 |
| `remove_member(org, space, member_scope)` | `None` | 移除成员 |

直接调用 `SpaceManager.delete()` 只承担管理面/KV 清理，不应被理解为所有派生索引都已清除。标准 API 删除 Space 时由 Engine 的 `purge_space()` 与 SpaceManager 共同完成数据面和管理面清理。

## 12. 公共数据类型

### 12.1 权限与调度枚举

| 类型 | 枚举值 |
|---|---|
| `Action` | `READ`、`WRITE`、`UPDATE`、`DELETE`、`SHARE` |
| `PrincipalPath` | `USER_AGENT`、`AGENT_USER` |
| `Channel` | `HOT`、`BACKGROUND` |
| `JobStatus` | `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED` |
| `UpdateMode` | `SUPERSEDE`、`OVERWRITE` |
| `DeleteMode` | `FORGET`、`ARCHIVE`、`DOWNWEIGHT`、`PURGE` |
| `SpaceStatus` | `ACTIVE`、`FROZEN`、`ARCHIVED`、`DELETING`、`DELETED` |

### 12.2 写入、更新与删除类型

| 类型 | 主要字段 | 说明 |
|---|---|---|
| `BatchWriteItem` | content、scope、source、assets、tags、system_metadata、user_metadata、occurred_at、stream_id、sequence、idempotency_key | API 归一化后的单条批量写入输入 |
| `BatchWriteOutcome` | index、item、units、error、error_type | 与原输入位置对齐的逐项结果 |
| `BatchWriteResult` | outcomes | 保持输入顺序的完整批量结果 |
| `MemoryListResult` | items、count | 当前页与分页前匹配总数 |
| `MemoryPatch` | content、tier、tags、两个 metadata、t_valid、t_invalid、mode | 仅非 `None` 字段生效；metadata 为 merge-update |
| `DeleteSelector` | unit_ids、scope、tags、before、mode | 各筛选条件取“与”；至少提供一项 |

### 12.3 权限类型

| 类型 | 主要字段 | 说明 |
|---|---|---|
| `Grant` | grantor、grantee、actions、expires_at | 一条跨 Scope 授权 |
| `PermissionContext` | resource_type、memory_type、pipeline、unit_id、scope、tags、metadata | 权限策略的资源上下文 |

### 12.4 Space 类型

| 类型 | 作用 |
|---|---|
| `SpacePolicy` | require_space、principal_path、隔离策略、保留期、配额、索引/pipeline profile |
| `SpaceSpec` | 创建输入：org、space、display_name、principal_path、policy、metadata |
| `SpaceInfo` | Space 当前元数据与创建/归档时间 |
| `SpacePatch` | 仅非 None 字段生效的更新输入 |
| `SpaceMember` | 成员 Scope、role、创建与到期时间 |
| `SpaceUsage` | memory/message/index 数量、存储字节与审计数量 |
| `SpaceDeleteResult` | 删除计数、最终状态和审计事件 ID |

## 13. Producer 与配置命名空间

| Producer | `TOP_NAME` | 实现目录 |
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

`JobFactory` 不是 `ControlOperator`，但同样通过 Producer 装配。控制实现由 `control.bootstrap.register_controllers()` 统一导入并触发注册；`jobs_impl` 在 Engine 装配 JobFactory 时按需导入。

配置使用相同的两级命名空间：

```yaml
permission:
  default:
    target: sqlite
    params:
      db_path: ./data/permissions.db
```

覆盖同名实例时 `params` 整体替换。特别是 `engine.default`、`pipeline.default` 这类依赖较多的实例，覆盖时必须把仍需要的具名依赖一并声明。

## 14. 可配置实现

### 14.1 Engine 与 Pipeline

| 命名空间 | `target` | 实现类 | 功能 | 依赖与主要参数 |
|---|---|---|---|---|
| `engine` | `in_memory` | `InMemoryEngine` | 本地兼容编排，只接受 `scope.space == ""`；支持 write/recall/list/update/delete/evolve | ingestor、index_builder、retriever、storage、scheduler、evolver、lifecycle；可选 classifier、pipeline、job_factory |
| `engine` | `cloud` | `CloudEngine` | 支持非空 Space 与云侧 message_type/profile 编排，校验 unit Scope 一致性 | 同上；`message_type_key`、`default_message_type`、`default_pipeline_name` |
| `pipeline` | `metadata` | `MetadataPipeline` | 按 system_metadata、query.extensions 或强制等值 FilterExpr 选择 `PipelineBinding` | `profiles`、`routes`、`fallback`、`route_key` |

`MetadataPipeline` 写侧读取首个非空 `unit.system_metadata[route_key]`；查询侧优先读 `query.extensions[route_key]`，其次从 `system_metadata.<route_key>` 的逻辑必然等值过滤中提取。route 值可映射到 profile，也可直接使用同名 profile；不存在时回退 fallback。

### 14.2 生命周期、治理与权限

| 命名空间 | `target` | 实现类 | 功能 | 参数与边界 |
|---|---|---|---|---|
| `lifecycle` | `kv` | `KVLifecycleManager` | 通过 Storage 点读和 `FORWARD_ONLY` 回写状态；按 Policy sweep | `storage`、`policy` |
| `governor` | `in_memory` | `InMemoryGovernor` | Scope 内 inspect/trace，并查询共享 AuditLogger | `storage`、`audit` |
| `permission` | `allow_all` | `AllowAllPermissionManager` | `check()` 恒为 True，只适合测试/demo | 无；不得作为 routing fallback |
| `permission` | `sqlite` | `SQLitePermissionManager` | 持久化 Grant，支持 root、owner-cover、Space 边界和过期授权 | `db_path`，默认 `:memory:` |
| `permission` | `routing` | `RoutingPermissionManager` | 按 PermissionContext 字段选择具名 PermissionManager delegate | `route_key`、`routes`、必填 `fallback` |

`routing` 的 fallback 必须是最小权限策略，装配期拒绝 `allow_all`。路由值只接受 `routes` 中显式声明的键，不允许调用方直接点名底层 policy。API 会把 `routing_fields()` 返回的值回注为系统过滤谓词，使“使用哪条权限策略”和“能够访问哪些数据”保持绑定。

### 14.3 调度、摄入任务、策略与 Space

| 命名空间 | `target` | 实现类 | 功能 | 主要参数 |
|---|---|---|---|---|
| `scheduler` | `in_process` | `InProcessScheduler` | 在 `submit()` 中立即执行并等待 Job | 无 |
| `scheduler` | `async_timer` | `AsyncTimerScheduler` | 同 Scope FIFO 串行、跨 Scope 并行，支持周期 TimerWheel | `tick_interval`，默认 `10` 秒 |
| `ingest_job` | `in_process` | `InProcessIngestJobController` | ThreadPoolExecutor 后台摄入、KV 状态持久化、payload 幂等 | `ingest_max_workers` 默认 `1`、`ingest_max_pending_jobs` 默认 `2`、`kv_store` |
| `policy` | `dict` | `DictPolicyManager` | 进程内可变策略表，只允许更新已知 key | `policies` |
| `space` | `kv` | `KVSpaceManager` | 用 Storage KV 维护 Space 元数据、策略、成员、用量和导出记录 | `storage` |
| `job_factory` | `default` | `JobFactory` | 注册 `EvolveJob` 与 `MiddleToLongJob` builder | storage、evolver、lifecycle、index_builder、llm；middle 调参和可选 lock |

`async_timer` 要求定时 Job 的 `interval >= tick_interval`。定时精度上限为一个 tick；同 Scope 同类任务不会并发堆积。

`InProcessIngestJobController` 的 payload 幂等键包含完整 Scope。相同 Scope + payload_id 的 pending/running/succeeded 任务会复用；相同 payload_id 指向不同 source 会冲突；failed 任务可以重试。服务重启后，持久化为 pending/running 的任务会被标记为失败，不会自动续跑。

## 15. 默认装配

无用户配置时，Control 相关默认实例为：

| 命名空间 | 默认 target | 备注 |
|---|---|---|
| `engine.default` | `in_memory` | 本地空 Space 兼容域 |
| `pipeline` | 未默认声明 | 单 profile 模式 |
| `lifecycle.default` | `kv` | Storage + Policy |
| `governor.default` | `in_memory` | Storage + SQLite AuditLogger |
| `permission.default` | `sqlite` | `db_path=:memory:` |
| `scheduler.default` | `in_process` | submit 时同步等待 Job |
| `ingest_job.default` | `in_process` | 共享 `kv_store.default` |
| `policy.default` | `dict` | 内置四项策略 |
| `space.default` | `kv` | 共享 Storage |
| `job_factory.default` | `default` | Evolve 与 MiddleToLong Job |

内置 Policy key：

```yaml
rerank.enabled: "true"
lifecycle.expired_active.target: "forgotten"
lifecycle.superseded.target: "forgotten"
scope.require_space: "false"
```

## 16. 配置示例

### 16.1 持久化权限与异步调度

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

覆盖 `policy.default.params.policies` 时要写全仍会被其他组件读取的 key；未知 key 不能在运行期通过 `set()` 新增。

### 16.2 权限路由

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

此例允许 `episodic` 显式走宽松策略，但路由值缺失或未知时必须落到 `strict`。

### 16.3 Metadata Pipeline

下面的 profile 中引用的 `constructor`、`retriever`、`evolver` 和 `classifier` 具名实例必须在各自命名空间预先声明：

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

声明 `pipeline.default` 后，`InMemoryEngine` 和 `CloudEngine` 会自动探测并注入它；无需仅为启用 pipeline 覆盖整个 `engine.default`。

## 17. 自定义实现要求

新增 Control 算子时至少需要：

1. 顶层接口文件只保留抽象契约，具体实现放在对应 `*_impl/`；
2. 实现业务方法、`operator_type()` 和 `health()`；
3. 使用对应 Producer 的 `register("target")` 注册；
4. Engine 只编排注入组件，不直接绑定具体后端、调用 LLM 或重复鉴权；
5. Engine 方法保持 async，阻塞型同步组件通过线程桥接；
6. LifecycleManager 只做 Scope 内非破坏式标记；
7. PermissionManager 不信任调用方声明的已有资源属性；
8. Pipeline 只返回 `PipelineBinding`，不实现具体 Construction/Retrieval 算法；
9. Scheduler 只决定何时执行，不决定 Job 内容；
10. Space、任务和授权状态必须保持 Scope 隔离。

## 18. 完整方法契约

### 18.1 同步与异步边界

| 组件 | 调用方式 | 约定 |
|---|---|---|
| `MemoryEngine` | 全部为 `async` | API 层负责同步桥接；Engine 内不创建新事件循环 |
| `Scheduler.submit` / `Job.run` | `async` | `submit` 是否等待 Job 完成由 Scheduler target 决定 |
| Pipeline/Lifecycle/Governor/Permission/Policy/Space | 同步 | 若实现包含阻塞 I/O，Engine/API 编排侧负责线程桥接 |
| `IngestJobController` | 同步提交与查询 | 内置 `in_process` 用线程池执行 `task`，`submit` 返回时任务通常仍在 pending/running |

### 18.2 非 Engine 接口的完整签名

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

`SpaceManager` 的完整签名：

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

## 19. 公共数据类型详情

### 19.1 批量写入与列表

| 类型.字段 | 类型 | 默认值/必填 | 语义 |
|---|---|---|---|
| `MemoryListResult.items` | `list[MemoryUnit]` | `[]` | 当前页 |
| `MemoryListResult.count` | `int` | `0` | 分页前的匹配总数 |
| `BatchWriteItem.content` | `str` | 必填 | 写入内容 |
| `BatchWriteItem.scope` | `Scope \| None` | `None` | Engine 层调用前应已由 API 层归一化为具体 Scope |
| `BatchWriteItem.source` | `Modality \| None` | `None` | 输入模态；API 层可补默认值 |
| `BatchWriteItem.assets` | `list[str] \| None` | `None` | 关联资产引用 |
| `BatchWriteItem.tags` | `list[str] \| None` | `None` | 记忆标签 |
| `BatchWriteItem.system_metadata` | `dict[str, MetadataValueType] \| None` | `None` | 系统控制元数据 |
| `BatchWriteItem.user_metadata` | `dict[str, MetadataValueType] \| None` | `None` | 用户元数据 |
| `BatchWriteItem.occurred_at` | `datetime \| None` | `None` | 事件发生时间 |
| `BatchWriteItem.stream_id` | `str` | `""` | 调用方数据流标识 |
| `BatchWriteItem.sequence` | `int \| None` | `None` | 数据流内序号 |
| `BatchWriteItem.idempotency_key` | `str` | `""` | 调用方幂等标识 |
| `BatchWriteOutcome.index` | `int` | 必填 | 原输入下标 |
| `BatchWriteOutcome.item` | `BatchWriteItem` | 必填 | 原输入项 |
| `BatchWriteOutcome.units` | `list[MemoryUnit]` | `[]` | 该项成功产出的单元 |
| `BatchWriteOutcome.error` | `str` | `""` | 失败摘要；成功时为空 |
| `BatchWriteOutcome.error_type` | `str` | `""` | 领域异常类名；未预期异常记为 `InternalError` |
| `BatchWriteResult.outcomes` | `list[BatchWriteOutcome]` | `[]` | 与原输入顺序一致 |

`batch_write(..., continue_on_error=False)` 不把第一个错误重新抛出；它会返回该错误 outcome，
并将后续未执行项标记为 `error_type="Skipped"`。

### 19.2 更新、删除与权限

| 类型.字段 | 类型 | 默认值 | 语义 |
|---|---|---|---|
| `MemoryPatch.content` | `str \| None` | `None` | 替换内容；`None` 表示不修改 |
| `MemoryPatch.tier` | `MemoryTier \| None` | `None` | 替换 tier |
| `MemoryPatch.tags` | `list[str] \| None` | `None` | 替换 tags |
| `MemoryPatch.system_metadata` | `dict[str, MetadataValueType] \| None` | `None` | 与原系统 metadata 做 merge-update |
| `MemoryPatch.user_metadata` | `dict[str, MetadataValueType] \| None` | `None` | 与原用户 metadata 做 merge-update |
| `MemoryPatch.t_valid` / `t_invalid` | `datetime \| None` | `None` | valid-time 边界 |
| `MemoryPatch.mode` | `UpdateMode` | `SUPERSEDE` | 新建版本或原地覆盖 |
| `DeleteSelector.unit_ids` | `list[str]` | `[]` | ID 选择器 |
| `DeleteSelector.scope` | `Scope \| None` | `None` | 限制完整 Scope；空值由 Engine 实现解释扫描范围 |
| `DeleteSelector.tags` | `list[str]` | `[]` | tag 选择器 |
| `DeleteSelector.before` | `datetime \| None` | `None` | 时间上界 |
| `DeleteSelector.mode` | `DeleteMode` | `FORGET` | 遗忘、归档、降权或物理删除 |
| `Grant.grantor` / `grantee` | `Scope` | `Scope()` | 授权方和被授权方 |
| `Grant.actions` | `list[Action]` | `[]` | 允许动作 |
| `Grant.expires_at` | `datetime \| None` | `None` | 过期时间；`None` 表示不限时 |
| `PermissionContext.resource_type` | `str` | `""` | 资源类型 |
| `PermissionContext.memory_type` | `str` | `""` | 记忆类型/路由值 |
| `PermissionContext.pipeline` | `str` | `""` | pipeline 名 |
| `PermissionContext.unit_id` | `str` | `""` | 已有记忆 ID |
| `PermissionContext.scope` | `Scope` | `Scope()` | 资源真实 Scope |
| `PermissionContext.tags` | `tuple[str, ...]` | `()` | 不可变标签快照 |
| `PermissionContext.metadata` | `dict[str, str]` | `{}` | 权限路由使用的系统字段 |

`DeleteSelector` 的 `unit_ids` / `tags` / `before` 至少要有一项非空，多个条件之间取 AND。

### 19.3 Job 与长耗时摄入

| 类型.字段 | 类型 | 默认值/必填 | 语义 |
|---|---|---|---|
| `Job.scope` | `Scope` | `Scope()` | 任务隔离边界 |
| `Job.interval` | `int` | `0` | `0` 一次性，正数表示定时周期 |
| `JobInfo.id` | `str` | `""` | Scheduler 任务 ID |
| `JobInfo.channel` | `Channel` | `BACKGROUND` | 业务通道标签 |
| `JobInfo.mode` | `str` | `""` | Job 实现类型或模式 |
| `JobInfo.scope` | `Scope` | `Scope()` | 任务 Scope |
| `JobInfo.status` | `JobStatus` | `PENDING` | 当前状态 |
| `JobInfo.detail` | `dict[str, str]` | `{}` | 开始/结束时间、错误和业务结果 |
| `IngestJob.id` | `str` | 必填 | 长耗时摄入任务 ID |
| `IngestJob.payload_id` / `source_ref` | `str` | 必填 | 幂等 payload 与来源引用 |
| `IngestJob.scope` | `Scope` | 必填 | 任务真实 Scope |
| `IngestJob.status` | `str` | 必填 | `pending` / `running` / `succeeded` / `failed` |
| `IngestJob.created_at` / `updated_at` | `datetime` | 必填 | 创建和最后更新时间 |
| `IngestJob.unit_ids` | `tuple[str, ...]` | `()` | 成功产出的单元 ID |
| `IngestJob.error` | `str` | `""` | 失败摘要 |
| `IngestSubmission.job` | `IngestJob` | 必填 | 新建或复用的任务 |
| `IngestSubmission.reused` | `bool` | 必填 | 是否命中已有幂等任务 |

### 19.4 Space 类型

| 类型 | 字段（类型；默认值） |
|---|---|
| `SpacePolicy` | `require_space: bool=false`；`principal_path: PrincipalPath=USER_AGENT`；`storage_isolation_strategy: str="metadata_filter"`；`retention/quotas/index_profiles/pipeline_profiles: dict[str, str]={}` |
| `SpaceSpec` | `org/space/display_name: str=""`；`principal_path=USER_AGENT`；`policy=SpacePolicy()`；`metadata: dict[str, str]={}` |
| `SpaceInfo` | `SpaceSpec` 的主要字段 + `status: SpaceStatus=ACTIVE`、`created_at/archived_at: datetime \| None=None` |
| `SpacePatch` | `display_name/status/principal_path/policy/metadata` 均为可选字段；`None` 表示不修改 |
| `SpaceMember` | `scope: Scope=Scope()`；`role: str="member"`；`created_at/expires_at: datetime \| None=None` |
| `SpaceUsage` | `org/space: str=""`；`memory_count/message_count/index_count/storage_bytes/audit_count: int=0` |
| `SpaceDeleteResult` | `org/space: str=""`；`deleted_counts: dict[str, int]={}`；`status: SpaceStatus=DELETED`；`audit_event_id: str=""` |

## 20. 异常、部分成功与任务状态

### 20.1 内置实现的常见异常

| 场景 | 接口 | 异常/结果 |
|---|---|---|
| `offset < 0` 或 `limit <= 0` | Engine `list` | `ValidationError` |
| 单元不存在或 `as_of` 无有效版本 | Engine `get/update` | `NotFoundError` |
| DeleteSelector 无 ID/tag/before | Engine `delete` | `ValidationError` |
| `InMemoryEngine` 收到非空 Space | 任意数据面方法 | `ValidationError` |
| `source` 不在当前 Normalizer 的能力集合内 | Engine `write` / `batch_write` | `UnsupportedCapabilityError` |
| 非法 lifecycle 转换 | `transition` / `supersede` | `ValidationError` 或 `PolicyError` |
| job 不存在 | Scheduler `status` | `NotFoundError` |
| 已关闭或队列已满 | Ingest `submit` | `BackendError` |
| 同 Scope + payload_id 指向不同 source | Ingest `submit` | `ConflictError` |
| Space 重复 | `SpaceManager.create` | `ConflictError` |
| Space 不存在 | Space 读写方法 | `NotFoundError` |
| `org`/`space` 为空、`limit <= 0`、cursor 非法 | Space 方法 | `ValidationError` |
| 未知 Policy key | `PolicyManager.get/set` | `PolicyError` |

上表描述当前内置 target，不会把 API 层的 `PermissionDeniedError` 错误归到 Engine；
Engine 信任传入 Scope 已通过鉴权。

### 20.2 写入和更新的部分成功边界

- `batch_write` 以单条 `write` 为隔离单元，不提供整批事务。
- `write(infer=false)` 在 Ingestor/Classifier 完成后调用 IndexBuilder；若多 Store 写入中途失败，可能已留下真源或部分索引。
- `update(SUPERSEDE)` 先创建新版本，再将旧版本标记为 SUPERSEDED，保证新版本创建失败时旧版本仍可读；整个过程仍非跨后端事务。
- `delete(FORGET/ARCHIVE)` 先回写 lifecycle，再软删检索索引；索引删除失败时真源状态已可能改变。
- `purge_space` 先枚举 Space 下各子 Scope，再通过 IndexBuilder 删除；返回值是已选中的 unit ID，不是逐后端删除报告。

### 20.3 Scheduler 状态语义

```text
PENDING -> RUNNING -> SUCCEEDED
                   -> FAILED
PENDING -> CANCELLED
```

- `in_process.submit()` 在当前协程内等待 `job.run()`，返回 job id 时通常已是 SUCCEEDED/FAILED。
- `async_timer.submit()` 将一次性任务入队后立即返回；同 Scope FIFO，不同 Scope 可并行。
- 内置 `cancel()` 是幂等的最尽力取消；一次性任务通常只在 PENDING 时能转为 CANCELLED，不中断已运行任务。
- 定时 Job 的 `interval` 必须不小于 `tick_interval`；取消后不再触发后续 tick。

## 21. 最小调用示例

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
        MemoryPatch(content="更新后的记忆内容"),
    )
    return await engine.delete(
        DeleteSelector(
            unit_ids=[updated.id],
            scope=scope,
            mode=DeleteMode.FORGET,
        )
    )
```

`MemoryEngine` 是已鉴权的内部编排接口。应用侧依然应优先调用 `MemoryAPI`，不应因为本示例
绕过 API 层的鉴权和审计。
