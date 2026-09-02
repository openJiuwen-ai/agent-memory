# F01 — 控制层实现规约（jiuwen_memory/control/*_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-02 |
| 影响范围 | jiuwen_memory/control/{engine,governance,lifecycle,permission,policy,scheduler}_impl/，docs/specs/S03-control.md |
| 测试基线 | `pytest tests/unit/control` 全绿；控制层主链路同时被 `tests/unit/api/test_recall_context.py` 与 quickstart 类端到端用例间接覆盖 |
| Refs | — |

> 本文档归档**控制层第一版实现规约**：每个控制算子的当前实现、注册名、依赖、行为边界与取舍。接口契约本身归 [`docs/specs/S03-control.md`](../../specs/S03-control.md)；当前文件地图和本地铁律见 [`jiuwen_memory/control/AGENTS.md`](../../../jiuwen_memory/control/AGENTS.md)。本文聚焦「当前实现怎么落地、为什么这么做」。

---

## 背景

控制层（Control，C 层）是记忆系统的管理面。它不承担具体的摄入、抽取、索引、召回算法，而是把接口层语义编排到下游算子，并管理记忆生命周期、治理查询、权限、演进调度和运行时策略。

第一版实现需要解决三件事：

1. **让 `MemoryAPI` 有可跑的编排中枢**：`add/search/get/update/delete/evolve` 要能端到端串起 ingest、construction、retrieval、storage，形成最小闭环。
2. **保住治理语义**：更新默认非破坏式版本化，删除默认标记遗忘/归档/降权，合规硬删必须显式 `PURGE`，治理侧能 inspect/trace/audit。
3. **为真实后端留插拔点**：当前实现以纯内存/进程内为主，但顶层接口必须保持抽象，实现通过 Producer 自注册，后续可替换成异步队列、真实 ACL、持久化 policy 后端等。

控制层因此被拆成六个算子：`MemoryEngine`、`LifecycleManager`、`Governor`、`PermissionManager`、`Scheduler`、`PolicyManager`。所有算子继承 `ControlOperator`，实现 `operator_type()` 与 `health()`；顶层 `.py` 只放抽象接口，具体实现放到各自 `*_impl/` 子包。

---

## 决策

### 决策 1：Engine 只做跨层编排，能力全部注入

**选了什么**：当前 `InMemoryEngine` 注册为 `engine: in_memory`，构造时注入 `Ingestor`、`Classifier`、`IndexBuilder`、`Retriever`、`KVStore`、`Scheduler`、`Evolver`、`LifecycleManager`。它负责串路径，但不直接实现规约、分类、索引、检索、演进算法。

| 路径 | 第一版语义 |
|---|---|
| `write` | 构造含 assets 的 `RawPayload` → `Ingestor.ingest`（由 Ingestor 映射 assets）→ Engine 补 tags 等编排字段 → `Classifier.classify` → `IndexBuilder.build`（统一交付 Storage 并构建索引）→ 返回 units。`system_metadata["infer"]=="true"` 时改为同步走 `Evolver.evolve(units, EXTRACT)` 抽取派生记忆（原始不建索引），返回派生单元；默认路径**不再** `Scheduler.submit(EXTRACT, BACKGROUND)`（演进由调用方显式 `evolve()` 触发）。详见 [`F02-write-infer-extract`](../api/F02-write-infer-extract.md) |
| `recall` | 直接委托 `Retriever.retrieve(scope, query)` |
| `get` | 从 KV 真源加载；`as_of` 非空时沿 `supersedes` 版本族选 valid-time 命中的版本 |
| `update` | `SUPERSEDE` 新建新 id、旧版经 `LifecycleManager.supersede` 标记失效；`OVERWRITE` 原地覆写 |
| `delete` | 空 selector 抛 `ValidationError`；`FORGET/ARCHIVE` 走生命周期标记；`DOWNWEIGHT` 只更新 `metadata.importance`；`PURGE` 物理删除真源并递归删除 provenance 后代 |
| `evolve` | 委托 `Scheduler.submit`，返回 job id |
| `admin_*` | 不经 Engine；当前实现抛 `NotImplementedError`，API 层直达 `PolicyManager` |

**关键权衡**：

- Engine 是「接口语义编排器」，不是算法容器。这样 API 层薄、下游能力可替换，测试也能用小桩件隔离路径。
- `write` 里允许直接调用 KVStore，因为 Engine 需要完成接口语义的真源落盘；但具体后端由 `KVStore` 抽象承接，不把存储实现细节写进 Engine。
- `admin_*` 选择 API 层直达 PolicyManager，避免把管理面所有操作都塞进 Engine，也让策略读写不被数据面异步协程形态绑死。

### 决策 2：生命周期只做非破坏式状态流转，硬删留给 Engine.delete(PURGE)

**选了什么**：当前 `KVLifecycleManager` 注册为 `lifecycle: kv`。它扫描 KV 真源，反序列化 `MemoryUnit`，修改 `lifecycle` 与必要的 `temporal.t_invalid` 后写回。

| 方法 | 第一版语义 |
|---|---|
| `transition(unit_ids, target)` | 跨 scope 扫描匹配 id；校验状态机；只改 `lifecycle` |
| `supersede(unit_id, invalid_at)` | 找到旧版，标记 `SUPERSEDED`，设置 `t_invalid=invalid_at`，返回旧版 |
| `sweep()` | 扫描 `SUPERSEDED` 旧版和 `t_invalid < now` 的 active 单元，按策略转为 `FORGOTTEN` 或 `ARCHIVED` |

状态流转只允许向更冷/更不可见的方向移动：`ACTIVE` 可到 `ARCHIVED/FORGOTTEN/SUPERSEDED`，`ARCHIVED` 可到 `FORGOTTEN`，`SUPERSEDED` 可到 `FORGOTTEN`，`FORGOTTEN` 只能保持自身。非法回流抛 `ValidationError`。

**关键权衡**：

- 非破坏式删除是默认治理语义：召回可移除，审计和版本链仍可追。
- `PURGE` 不放进 LifecycleManager，是为了让「物理删除真源 + 删除派生索引 + 删除 provenance 后代」作为一个显式危险路径集中在 Engine 的 delete 语义里。
- `sweep` 的目标态通过 PolicyManager 读取，第一版支持两个策略键：`lifecycle.expired_active.target` 与 `lifecycle.superseded.target`，取值仅允许 `forgotten` 或 `archived`。

### 决策 3：治理算子只负责「看」，不负责改

**选了什么**：当前 `InMemoryGovernor` 注册为 `governor: in_memory`。它依赖同一份 KV 真源和内存审计事件列表，提供 inspect / trace / audit。

| 方法 | 第一版语义 |
|---|---|
| `inspect(unit_ids)` | 跨 scope 查找 id，返回能找到的 `MemoryUnit`，包括已失效版本 |
| `trace(unit_id)` | 沿 `provenance` 递归向上回溯来源链，循环用 `seen` 防止重复 |
| `audit(filters, limit)` | 当前支持按 `action`、`layer`、`decision`、`target_id`、actor scope、target scope 与时间区间过滤审计事件，按插入顺序取前 `limit` 条 |

**关键权衡**：

- 治理层只做可检视、可回溯、可审计；编辑和遗忘仍走 API 的 update/delete，以便统一鉴权和审计。
- `trace` 只沿 `provenance`，不沿 `supersedes`。前者表示「这条记忆从哪些来源演进而来」，后者表示版本替换链，服务 `get(as_of)`。
- 第一版跨 scope 查找依赖 KVStore 的 `scopes()` + `scan()` 能力，适合治理后台；生产环境若需要更细粒度授权，应在 API 层和真实 Governor 后端补访问控制。

### 决策 4：权限第一版用 allow-all 占位，保留 Grant 记录形状

**选了什么**：当前 `AllowAllPermissionManager` 注册为 `permission: allow_all`。`check` 恒返回 `True`，`grant` 把授权对象追加到内存列表，`revoke` 按 `grantor + grantee` 移除。

**关键权衡**：

- 本地 demo 和单租户测试需要默认可跑，不能被未完成的 ACL 规则拦住主链路。
- `Grant` / `Action` / `check(actor, target, action)` 的接口形状先稳定下来，API 层已经把 PEP 鉴权点放在入口，后续替换真实权限实现不需要改 Engine。
- 这是刻意的最小实现，不代表生产权限语义。真实部署应实现 scope 包含关系、授权过期、逐 action 撤销和审计。

### 决策 5：Scheduler 第一版进程内同步执行，但保留任务状态流

**选了什么**：当前 `InProcessScheduler` 注册为 `scheduler: in_process`。`submit` 创建 `JobInfo` 后立即在当前进程调用 `_execute_task`；若注入了 KV 和 Evolver，则加载目标 scope 下的所有 `MemoryUnit` 并调用 `Evolver.evolve(units, mode)`，把结果 id 写入 `job.detail`。

| 状态 | 第一版触发 |
|---|---|
| `PENDING` | job 创建后进入任务表 |
| `RUNNING` | `_execute_task` 前设置 |
| `SUCCEEDED` | 执行无异常 |
| `FAILED` | 捕获异常并记录 `error_type/error` |
| `CANCELLED` | 仅 pending 任务可被取消；已完成任务取消为幂等 no-op |

**关键权衡**：

- 第一版不引入线程池、队列或外部 worker，避免调度基础设施压过记忆主链路。
- 即便同步执行，也保留 job id、状态、时间戳、执行结果详情，API 和 UI 可以先依赖稳定的任务查询契约。
- 缺少 KV/Evolver 时执行体空转并成功返回，用于极简装配；完整装配中通过 Producer 依赖默认注入 `KvProducer.dep(..., default="memory")` 与 `EvolverProducer.dep(..., default="orchestrating")`。

> **增量（2026-07，[`F06`](F06-middle-term-memory.md)）**：`Scheduler.submit` 改为 `async def submit(self, job: Job, channel: Channel) -> str`——task 内容由 `Job` 封装，Scheduler 不再决定 mode；InProcessScheduler 不再持 KV/Evolver，签名 `def __init__(self) -> None`，直接 `await job.run()` 执行。原 `_execute_task` 逻辑外提为 `EvolveJob`（`jobs_impl/evolve_job.py`）。`AsyncTimerScheduler` 作为真异步调度实现注册为 `async_timer`，见 F06 决策 2。

### 决策 6：Policy 第一版是已知键内存表

**选了什么**：当前 `DictPolicyManager` 注册为 `policy: dict`。策略存内存 dict，`get/set` 仅允许访问已存在 key，未知 key 抛 `PolicyError`。默认策略：

| key | 默认值 | 消费方 |
|---|---|---|
| `rerank.enabled` | `true` | 检索/装配侧策略开关 |
| `lifecycle.expired_active.target` | `forgotten` | `KVLifecycleManager.sweep` |
| `lifecycle.superseded.target` | `forgotten` | `KVLifecycleManager.sweep` |
| `scope.require_space` | `false` | `LocalMemoryAPI` target scope 校验 |

**关键权衡**：

- 策略是运行时可变配置，不是后端选型、连接串、索引结构这类初始化期重型配置。
- 不允许新增未知 key，是为了避免拼写错误静默生效，也避免 admin 接口把不可变配置伪装成运行时策略。
- 第一版未做持久化和审计落点，后续可替换为 DB/配置中心实现。

---

## 当前实现矩阵

| 算子 | Producer TOP_NAME | target | 实现类 | 主要依赖 | 语义摘要 |
|---|---|---|---|---|---|
| MemoryEngine | `engine` | `in_memory` | `InMemoryEngine` | ingestor / classifier / index_builder / retriever / kv / scheduler / evolver / lifecycle / job_factory | 编排 API 数据面主语义 |
| LifecycleManager | `lifecycle` | `kv` | `KVLifecycleManager` | kv / policy | KV 真源上的非破坏式状态流转与清扫 |
| Governor | `governor` | `in_memory` | `InMemoryGovernor` | kv / audit logger events | 治理检视、血缘回溯、审计过滤 |
| PermissionManager | `permission` | `allow_all` | `AllowAllPermissionManager` | — | 本地放行占位，记录 Grant |
| Scheduler | `scheduler` | `in_process` | `InProcessScheduler` | — | 进程内同步执行 Job（`await job.run()`），维护 JobInfo |
| Scheduler | `scheduler` | `async_timer` | `AsyncTimerScheduler` | — | 异步 + 定时调度：per scope FIFO 队列 + 单 drain Task + per scope TimerWheel（见 [`F06`](F06-middle-term-memory.md)） |
| PolicyManager | `policy` | `dict` | `DictPolicyManager` | policies 参数 | 已知键内存策略表 |

### 注册与装配

每个实现文件尾部通过 `@XProducer.register("<target>")` 自注册；`control.bootstrap.register_controllers()` 统一 import 六个实现包，触发注册，且幂等。装配层按两级命名空间构建实例：

```yaml
engine:
  default:
    target: in_memory
    params:
      scheduler: default
scheduler:
  default:
    target: in_process
```

未显式配置时，各实现 builder 使用 `Producer.dep(..., default="<target>")` 选择当前最小实现，并通过具名实例缓存共享同一 KV、Evolver、AuditLogger 等依赖。

---

## 拒绝的方案

### 方案 A：把鉴权逻辑下沉到 Engine

**描述**：`MemoryEngine.write/recall/get/update/delete` 内部调用 `PermissionManager.check`。

**拒绝原因**：

- API 层已承担 PEP（Policy Enforcement Point）：它同时拥有 `identity` 与 target scope，能做入口审计；Engine 收到的是已鉴权 scope。
- 下沉会导致同步/异步 API、治理方法、接入 surface 重复鉴权，且容易产生一部分路径漏查、一部分路径双查。
- Engine 应只编排数据面，权限算子作为独立管理面能力由 API 调用。

### 方案 B：LifecycleManager 同时负责 PURGE 物理删除

**描述**：把 `DeleteMode.PURGE` 也映射成 LifecycleManager 方法，由生命周期算子删 KV。

**拒绝原因**：

- `PURGE` 不只是状态流转，还要删除真源、派生索引，以及 provenance 后代；这需要 Engine 统筹多依赖。
- LifecycleManager 的语义是非破坏式管理，混入硬删会模糊危险边界。
- 非破坏式 delete 与硬删 delete 的审计/恢复语义不同，必须由显式 mode 分支隔离。

### 方案 C：第一版引入真实异步队列

**描述**：Scheduler 直接接 Celery/RQ/线程池/外部 worker，background 真异步运行。

**拒绝原因**：

- 第一版目标是跑通契约和端到端路径，引入外部调度基础设施会增加部署与测试复杂度。
- 任务状态契约比执行介质更稳定；先固化 `JobInfo` 和 `submit/status/cancel`，后续替换实现即可。
- 当前构建层 Evolver 仍以最小实现为主，真实异步队列的收益有限。

### 方案 D：PolicyManager 允许动态新增任意 key

**描述**：`admin_set("some.new.key", "value")` 若 key 不存在就创建。

**拒绝原因**：

- 拼写错误会静默制造无效策略，难以排查。
- 运行时策略与初始化期配置边界会被打破，用户可能试图通过 admin 改后端类型、连接串等不可变配置。
- 已知键白名单让策略消费方和 admin 面共享同一组明确契约。

---

## 验证

### 单元测试覆盖

| 测试文件 | 覆盖重点 |
|---|---|
| `tests/unit/control/test_engine_delete_selector.py` | selector 的 AND 语义、空 selector 校验、FORGET/ARCHIVE/DOWNWEIGHT/PURGE、purge provenance 后代删除 |
| `tests/unit/control/test_engine_update_versioning.py` | `SUPERSEDE` 新 id 与旧版失效、`OVERWRITE` 同 id 覆写 |
| `tests/unit/control/test_engine_get_as_of.py` | `get(as_of)` 沿版本链按 valid-time 选择版本 |
| `tests/unit/control/test_engine_evolve_scheduler.py` | Engine.evolve 委托 Scheduler 并返回 job id |
| `tests/unit/control/test_lifecycle_manager.py` | lifecycle 状态机、supersede、sweep 策略目标 |
| `tests/unit/control/test_governance.py` | inspect / trace / audit |
| `tests/unit/control/test_scheduler_in_process.py` | submit 状态流、失败记录、Evolver 调用、cancel 幂等 |

### 端到端覆盖

- `tests/unit/api/test_recall_context.py` 间接覆盖 API → Engine → Retriever 的 search 边界。
- `examples/quickstart.py` 类示例覆盖 add → search → get/update/delete → evolve → admin/audit 主链路。

---

## 已知遗留

1. **Permission 仍是 allow-all**：当前只适合本地 demo/单租户测试。真实多租户部署必须实现 scope 包含、Grant 匹配、过期校验、逐 action revoke 与审计。

2. **Scheduler 不是真异步**（**部分解决，仍有遗留**，见 [`F06`](F06-middle-term-memory.md)）：原 `InProcessScheduler.submit` 在当前进程同步执行 Evolver，add 后提交的 background EXTRACT 仍可能拖慢调用链。`F06` 新增 `AsyncTimerScheduler`（target=`async_timer`）作为异步 + 定时调度器，per scope FIFO 队列 + 单 drain Task + per scope TimerWheel，Job 提交后不再阻塞 add 路径。**但有两处遗留**：(a) `defaults.py` 默认装配仍配 `in_process`——需用户显式覆盖为 `async_timer` 才走异步；(b) `AsyncTimerScheduler` 依赖长生命周期事件循环，与同步 `LocalMemoryAPI.add` 内的 `asyncio.run` 桥接不兼容（同步 API 返回后临时循环关闭、Timer 协程被取消）——生产部署需配独立 Scheduler Runtime 或改用 `add_async` 全链路 await。详见 F06 已知遗留。

3. **Policy 未持久化、未审计**：`DictPolicyManager` 只存在于进程内，重启丢失；`set` 也未直接产生日志/审计事件。后续应接持久化后端并补 admin 审计。

4. **Governor.audit 过滤能力仍是结构化精确匹配**：当前已支持 action/layer/decision/target_id、actor scope、target scope 与时间区间；更复杂的模糊查询、聚合统计和长期审计保留策略仍需后续补齐。

5. **Lifecycle.transition 按 id 跨 scope 扫描**：便于治理/测试，但大规模后端成本高；生产实现应利用索引或把 scope 纳入调用条件。

6. **Engine.delete 的 DOWNWEIGHT 只改 importance**：当前把 `metadata.importance` 乘 0.5，未通知检索层访问频次/重要度模型，也没有独立降权索引字段。

7. **admin_* 在 Engine 抛 NotImplementedError**：这是当前架构选择（API 直达 PolicyManager），但如果未来所有管理面都要统一走 Engine，需要重新修订 S03 与 API 文档。
