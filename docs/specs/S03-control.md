# S03 — 控制层（Control Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/control/ |
| 最近一次修订日期 | 2026-08-05 |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/construction/F02-dynamic-extraction-consolidation.md，docs/features/construction/F04-cc-memory-compat.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F03-control-pipeline-routing.md，docs/features/control/F04-permission-context-routing.md，docs/features/control/F05-cloud-engine-design.md，docs/features/common/F03-scope-space-isolation.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/security/F02-role-aware-authorization.md |
## 范围 / 边界

**管什么**：
- 记忆引擎编排（接口层各语义的跨层委托中枢）
- 按记忆类型选择构建/查询 pipeline profile
- 记忆单元生命周期状态流转（active → superseded → archived → forgotten）
- 治理「看」侧（检视 / 血缘回溯 / 审计查询）
- 跨 scope 权限授权与校验
- 演进任务的 hot/background 双通道调度
- 运行时可变策略的查询与调整
- space 生命周期、策略、成员、用量、导出与 offboarding 管理

**不管什么**：
- 不执行鉴权（PEP 在 `src/api` 层，Engine 信任传入的 scope）
- 不绑定具体存储后端（只通过注入的 Store 抽象读写）
- 不实现抽取/升华/关联/冲突消解等演进逻辑（由构建层 `Evolver` 负责，控制层仅调度）
- 不生产记忆（由 `src/ingest` + `src/construction` 负责）
- 不执行检索（由 `src/retrieval` 负责）
- 不管不可变/重型配置（由 `src/config` 在实例初始化时确定）

## 不变量

1. **引擎不实现具体算法能力**：`MemoryEngine` 只编排，Ingestor/构建算子/Retriever/Store 全部由装配注入；可通过 Store 抽象完成真源语义，但不得绑定具体后端或直接调用 LLM。
2. **引擎方法一律异步协程**：同步调用由 `src/api` 层自行桥接（`asyncio.run`），engine 内不做同步阻塞。
3. **鉴权不在本层执行**：`PermissionManager.check` 由 `src/api/MemoryAPI` 在入口调用，engine 信任传入的 scope 已鉴权。禁止在 engine 内部重复 check。
4. **LifecycleManager 只做非破坏式标记**：`transition` 标记状态（superseded/archived/forgotten），绝不物理删除。物理删除（purge）走 Engine 的 `delete` 路径 + `DeleteMode.PURGE`。
5. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 Producer 自注册后被外部装配消费，不被顶层接口引用。
6. **types.py 零依赖本层其他文件**：纯数据定义，被本层各接口和 `src/api/` 共同依赖。
7. **DeleteSelector 各条件取「与」**：至少给出一项可命中条件；空 selector 抛 `ValidationError`。
8. **UpdateMode.SUPERSEDE 产生新 id**：旧 id 标记 superseded，`update` 返回的记忆 id 可能与传入的 `unit_id` 不同。
9. **admin_* 不经 Engine**：由 API 层直达 PolicyManager；Engine 不承载策略存储。
10. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `ControlOperator`，自描述 + 存活探测。
11. **自演进由控制层调度、构建层执行**：控制层只提交任务、管理通道和任务状态；抽取、升华、关联、冲突消解与索引维护逻辑归构建层。
12. **Pipeline 只做跨层 profile 选择**：`MemoryPipeline` 可以选择不同的构建/查询组件绑定，但不得实现抽取、索引、检索算法；construction/retrieval 不反向依赖 control。
13. **权限上下文由 API/Engine 解析，不信任调用方声明**：write/recall/list 的请求条件可由 API 构造 `PermissionContext`；list 当前分页实际命中的 unit 以及 get/update/delete 这类已有 unit 操作必须由 Engine 从真源元数据解析 memory_type/tags 后再鉴权。
14. **权限路由与执行路由同源但职责独立**：两者对 recall 都使用
    extensions 优先、FilterExpr 强制唯一等值兜底的取值规则；PermissionManager 选择
    授权策略，MemoryPipeline 选择执行组件，互不代替。
15. **路由授权绑定数据范围**：路由型权限根据某字段授权后，API 必须把同一值回注为
    系统过滤谓词；routing fallback 必须是最小权限策略，不得使用 `allow_all`。
16. **目标操作使用完整 Scope**：MemoryUnit id 仅在 Scope 内唯一。LifecycleManager、Governor 与 IndexBuilder 的目标修改/读取/删除不得依赖全局 `id -> scope` 猜测，调用方必须显式提供 Scope 或携带 Scope 的 MemoryUnit。
17. **Engine 部署边界明确**：`InMemoryEngine` 只接受空 `space` 兼容域；具名非空 space 的数据面操作使用 `CloudEngine`。`CloudEngine` 仍兼容空 space，但生产多租户配置应开启 `scope.require_space=true`。

## 接口契约

### ControlOperator（基类，`base.py`）

```python
class ControlOperatorType(str, Enum):
    ENGINE / PIPELINE / LIFECYCLE / GOVERNOR / PERMISSION / SCHEDULER / POLICY / SPACE

class ControlOperator(ABC):
    def operator_type(self) -> ControlOperatorType  # 自描述
    def health(self) -> None                        # 存活探测：健康返回 None，否则抛异常
```

### MemoryEngine（`engine.py`）

编排接口层各语义的中枢。注入依赖：Ingestor、Classifier、IndexBuilder、Evolver、Retriever、KVStore、Scheduler、LifecycleManager；可选注入 MemoryPipeline 做按记忆类型或 message_type 的 profile 选择。Evolver 有两个平级注册实现：`OrchestratingEvolver`（注册名 `orchestrating`）与其子类 `DynamicEvolver`（注册名 `dynamic`，EXTRACT 走 extract→consolidate→reflect→落盘）；装配或 profile 选择决定实际路径。

| 方法 | 签名 | 语义 |
|------|------|------|
| `write` | `async (content, scope, source, *, assets, tags, metadata: dict[str, Any] \| None, occurred_at) -> list[MemoryUnit]` | 规约→可选抽取/分类→落盘+建索引；`infer=true` 时返回 `created_ids` 对应的派生结果，否则处理原始单元（直写不去重） |
| `recall` | `async (scope, query: RetrievalQuery) -> RetrievalResult` | 委托 Retriever 完整检索链路 |
| `list` | `async (scope, *, offset=0, limit=100, memory_types=None, extensions=None, filters=None) -> MemoryListResult` | 校验分页参数并完整委托 `KVStore.list`；返回当前页和分页前匹配总数 |
| `permission_context_for_unit` | `async (unit_id, scope) -> PermissionContext` | 读取已有记忆的权限上下文，只返回 memory_type/tags/metadata 等鉴权元数据，不返回 content/assets |
| `list_with_permission_contexts` | `async (同 list 参数) -> tuple[MemoryListResult, list[PermissionContext]]` | 从同一次 KV 查询的当前页构造逐项真源权限上下文，items/count/context 不做二次读取 |
| `permission_contexts_for_delete` | `async (selector: DeleteSelector) -> list[PermissionContext]` | 解析 delete selector 命中的候选 unit 权限上下文，供 API 层逐条鉴权 |
| `get` | `async (unit_id, scope, as_of=None) -> MemoryUnit` | 真源点读；`as_of` 非空沿 supersedes 链返回当时有效版本 |
| `update` | `async (unit_id, scope, patch: MemoryPatch) -> MemoryUnit` | SUPERSEDE 新 id 记版本链 / OVERWRITE 原地覆写 |
| `delete` | `async (selector: DeleteSelector) -> list[str]` | PURGE 物理删 / 其他委托 LifecycleManager 非破坏式流转 |
| `purge_space` | `async (org: str, space: str) -> list[str]` | 物理删除该 Space 全部 user/agent/session 子 Scope 的 MemoryUnit 真源与索引，供 offboarding 调用 |
| `evolve` | `async (scope, mode: EvolveMode, channel=BACKGROUND) -> str` | 提交演进任务到 Scheduler；执行逻辑由构建层 Evolver 完成，返回 job_id |
| `admin_get/set/all` | — | 管理面语义由 API 层直达 PolicyManager，Engine 不承载策略存储 |

**write 路径**：
```
Ingestor.ingest([RawPayload]) → list[MemoryUnit]
→ 将 metadata/assets/tags 入参补入接入层产出的 MemoryUnit
→ MemoryPipeline.select_for_write(units)  # 可选；未注入时使用 Engine 默认组件
→ if str((metadata or {}).get("procedural", "")).strip().lower() == "true"
     or str((metadata or {}).get("infer", "")).strip().lower() == "true":
      选中 profile 的 Evolver.evolve(units, EXTRACT)
      # Evolver 实现决定 EXTRACT 路径：
      #   OrchestratingEvolver → _evolve_extract: extract→annotate→_dedup_batch(判定+落盘)
      #   DynamicEvolver       → _evolve_extract: extract→consolidate(判定)→reflect→落盘
      返回 EvolveResult.created_ids 对应的新建派生单元；UPDATE/NOOP 可返回空
  else:
      选中 profile 的 Classifier.classify(units)  # 可选
      KVStore.insert(scope, memory_key(unit.id), dumps(unit))
      选中 profile 的 IndexBuilder.build(units)
      返回 units
```

### MemoryPipeline（`pipeline.py`）

控制层的跨构建/查询 profile 编排抽象。Pipeline 不实现具体能力，只返回一组已装配的组件绑定。

```python
@dataclass(frozen=True)
class PipelineBinding:
    name: str
    index_builder: IndexBuilder
    retriever: Retriever
    evolver: Evolver
    classifier: Classifier | None = None

class MemoryPipeline(ControlOperator):
    def select_for_write(units: list[MemoryUnit]) -> PipelineBinding
    def select_for_recall(query: RetrievalQuery) -> PipelineBinding
```

默认 `metadata` 实现的配置形态：

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

路由规则：

- 写入侧读取 `MemoryUnit.metadata[route_key]`。
- 查询侧优先读取 `RetrievalQuery.extensions[route_key]`，其次从规范化
  `FilterExpr` 提取逻辑上强制成立的 `metadata.<route_key>` 唯一等值；
  `memory_type` 裸字段仅作为 `metadata.memory_type` 的旧输入别名。
- `routes` 把路由值映射到 profile 名；未命中时若路由值本身是 profile 名则直接使用，否则退回 `fallback`。
- 未配置 `pipeline.default` 时不启用 pipeline，行为等价旧单 pipeline；用户通过 YAML 显式声明后启用。

> `infer` 开关详见 [`S02-memory-api.md`](S02-memory-api.md)「infer 开关」与 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)。默认路径不再自动提交 background EXTRACT——`InProcessScheduler` 同步执行下"自动提交"实为同步阻塞，与双通道时延初衷相悖；真异步 Scheduler 落地后是否恢复可选自动提交另行决策。

**delete 路径**：
```
按完整 Scope 分组遍历 selector 命中的 MemoryUnit:
  → PURGE: KVStore.delete(scope, memory_key(unit.id)) + IndexBuilder.remove([unit])
  → 其他: LifecycleManager.transition(scope, [unit.id], 对应状态)
           + IndexBuilder.remove([unit])
→ 返回命中 id 列表
```

### LifecycleManager（`lifecycle.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `transition` | `(scope: Scope, unit_ids: list[str], target: LifecycleState) -> None` | 在指定 Scope 内批量非破坏式状态标记 |
| `supersede` | `(scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit` | 在指定 Scope 内将旧版本标记 SUPERSEDED，并把 valid-time 失效边界设为 `invalid_at` |
| `sweep` | `() -> list[str]` | 扫描到期（`t_invalid` 已过）的 active 单元，标记 FORGOTTEN |

**状态机**：
```
active → superseded → archived → forgotten
active → archived → forgotten
```

非法流转：forgotten → *；superseded → active；archived → active。

`supersede` 是 `update` SUPERSEDE 路径的专用入口，必须同步写入旧版本的 `t_invalid`。

### Governor（`governance.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `inspect` | `(unit_ids: list[str], scope: Scope) -> list[MemoryUnit]` | 在已鉴权目标 Scope 内检视完整内容与治理字段（含已失效版本） |
| `trace` | `(unit_id: str, scope: Scope) -> list[MemoryUnit]` | 在已鉴权目标 Scope 内沿 `provenance` 血缘向上追溯；`supersedes` 仅用于版本链与 `get(as_of)` |
| `audit` | `(filters: dict[str, str], limit=100) -> list[AuditEvent]` | 治理层唯一对外审计查询入口，按 `action` / `layer` / `decision` / `target_id` / `actor_org` / `actor_space` / `actor_user` / `actor_agent` / `actor_session` / `target_org` / `target_space` / `target_user` / `target_agent` / `target_session` / `occurred_after` / `occurred_before` 过滤审计事件；底层 `AuditLogger` 负责记录事件，并通过 `query(filters, limit)` 向治理层提供后端内查询能力 |

### PermissionManager（`permission.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `grant` | `(grant: Grant) -> None` | 新增跨 scope 授权**记录** |
| `revoke` | `(grant: Grant) -> None` | 回收授权记录（幂等） |
| `check` | `(actor: Scope, target: Scope, action: Action, context: PermissionContext \| None = None, *, auth: AuthContext \| None = None) -> bool` | **已不在请求路径上**：授权判定归 `common.security.authorization.Authorizer`（见 S08），本方法只剩历史实现与既有回归覆盖 |
| `routing_fields` | `() -> tuple[str, ...]` | 返回本实现鉴权路由所依据的 metadata 字段；非路由实现返回空元组 |

**授权判定不在本层**。`LocalMemoryAPI._authorize` 这个唯一 PEP 调的是
`Authorizer.authorize(auth=..., resource=..., environment=...)`，输入固定为
`AuthContext + ResourceDescriptor + AuthorizationEnvironment`，**不读 ContextVar**，
也不存在 `auth=None` 退回纯 ACL、空 `Scope()` 即 platform admin 这两条旧兼容线——
它们在 PR2 已删除。判定顺序与 truth table 见 [S08](S08-security.md)。本层的
`PermissionManager` 在当前主干已不再是 grant/revoke 的写入通道--API 的 grant/revoke
改写 Authorizer 读取的 `GrantStore`；`PermissionManager` 仅作后续 PR 待删除的遗留。

权限/授权后端由配置选择；无具体 target scope 的管理面方法（`admin_get` / `admin_set` /
`admin_all` / 全局 `audit`）统一以根 scope `Scope()` 作为鉴权目标并携带
`resource_type`（`admin` / `audit`），普通租户 scope 不默认具备管理面访问权；
`grant` / `revoke` 则以 grantor scope 为 target 做 `Action.SHARE` 校验。管理面资源
中无 org 归属的（全局治理策略、跨 org 审计）要求 ROOT，带 org 的（space、主体）
ADMIN 可管但止于本 org。

`routing` 权限后端按 `PermissionContext` 分派到不同具名 permission policy。示例：

```yaml
permission:
  default:
    target: routing
    params:
      route_key: memory_type
      fallback: standard
      routes:
        coding: strict
  standard: sqlite
  strict: sqlite
```

`routing` 不改变授权语义，只选择 delegate；`grant` / `revoke` 会广播给全部
delegate，避免调用方理解授权记录应落在哪个后端。路由值只接受 `routes` 中显式
声明的业务值，未知值和直接 policy 名都落 `fallback`；`fallback` 在装配期禁止指向
`allow_all`。

recall 完成权限检查后，API 读取 `PermissionManager.routing_fields()`，把授权所依据
的路由值作为等值系统谓词回注查询，并与用户 `FilterExpr` 做外层 `AND`。这保证
“选择哪条权限策略”和“实际能读取哪类数据”使用同一个值，用户表达式中的 `OR`
不能绕过该约束。

### Scheduler（`scheduler.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `submit` | `(scope: Scope, mode: EvolveMode, channel: Channel) -> str` | 提交演进任务，返回 job_id |
| `status` | `(job_id: str) -> JobInfo` | 查询任务状态 |
| `cancel` | `(job_id: str) -> None` | 取消尚未完成的任务（幂等） |

**双通道**：HOT（在线低时延：write 返回前完成的轻量索引）；BACKGROUND（离线异步：重的抽取/升华/重索引）。

### PolicyManager（`policy.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `get` | `(key: str) -> str` | 读取一项运行时策略 |
| `set` | `(key: str, value: str) -> None` | 调整策略（未知键/不可变配置抛 `PolicyError`） |
| `all` | `() -> dict[str, str]` | 列出全部运行时策略及当前值 |

### SpaceManager（`space.py`）

space 是 `org` 下的逻辑隔离单元。API 层负责鉴权与审计，`SpaceManager` 负责
space 元数据、space policy、成员、用量与 offboarding 状态管理。

| 方法 | 签名 | 语义 |
|------|------|------|
| `create` | `(spec: SpaceSpec) -> SpaceInfo` | 创建 space，写入 display name、主体路径、policy、metadata 与状态 |
| `get` | `(org: str, space: str) -> SpaceInfo` | 读取单个 space |
| `list` | `(org: str, *, status=None, limit=100, cursor=None) -> list[SpaceInfo]` | 列出 org 下 spaces；`cursor` 是由实现解释的分页游标，调用方不得解析其内部格式 |
| `update` | `(org: str, space: str, patch: SpacePatch) -> SpaceInfo` | 修改 display name、status、principal_path、policy 或 metadata |
| `archive` | `(org: str, space: str) -> SpaceInfo` | 归档 space；API 层会拒绝已归档 space 的 write/update/evolve |
| `delete` | `(org: str, space: str) -> SpaceDeleteResult` | 删除该 `org + space` 下 KV 真源、messages、space metadata；API 层在调用前先经 Engine purge memory 并清理索引 |
| `export` | `(org: str, space: str, *, include_audit=True) -> str` | 创建导出记录并返回 export id |
| `usage` | `(org: str, space: str) -> SpaceUsage` | 统计 memory/message 数量与 KV bytes；index/audit 计数由后续专用后端补齐 |
| `get_policy` | `(org: str, space: str) -> SpacePolicy` | 读取 space policy |
| `set_policy` | `(org: str, space: str, policy: SpacePolicy) -> SpacePolicy` | 替换 space policy，并同步 `principal_path` |
| `list_members` | `(org: str, space: str) -> list[SpaceMember]` | 列出成员与角色 |
| `add_member` | `(org: str, space: str, member: SpaceMember) -> None` | 添加或更新成员角色；成员 scope 的 org/space 归一为目标 space |
| `remove_member` | `(org: str, space: str, member: Scope) -> None` | 移除成员 |

## 数据结构

### 控制层数据类型（`types.py`）

| 类型 | 性质 | 关键字段 |
|------|------|----------|
| `Action` | 枚举 | READ / WRITE / UPDATE / DELETE / SHARE |
| `PermissionContext` | dataclass | resource_type / memory_type / pipeline / unit_id / scope / tags / metadata |
| `Grant` | dataclass | grantor(Scope) / grantee(Scope) / actions(list[Action]) / expires_at |
| `Channel` | 枚举 | HOT / BACKGROUND |
| `JobStatus` | 枚举 | PENDING / RUNNING / SUCCEEDED / FAILED / CANCELLED |
| `JobInfo` | dataclass | id / channel / mode / scope / status / detail |
| `MemoryListResult` | dataclass | items: list[MemoryUnit] / count: int（分页前匹配总数） |
| `UpdateMode` | 枚举 | SUPERSEDE（默认，新 id）/ OVERWRITE（同 id） |
| `MemoryPatch` | dataclass | content（修正后的文本投影，应用时更新对应 Segment 内容） / tier / tags / metadata / t_valid / t_invalid / mode(UpdateMode) |
| `DeleteMode` | 枚举 | FORGET / ARCHIVE / DOWNWEIGHT / PURGE |
| `DeleteSelector` | dataclass | unit_ids / scope / tags / before / mode(DeleteMode) |
| `PrincipalPath` | 枚举 | USER_AGENT / AGENT_USER |
| `SpaceStatus` | 枚举 | ACTIVE / FROZEN / ARCHIVED / DELETING / DELETED |
| `SpacePolicy` | dataclass | require_space / principal_path / storage_isolation_strategy / retention / quotas / index_profiles / pipeline_profiles |
| `SpaceSpec` | dataclass | org / space / display_name / principal_path / policy / metadata |
| `SpaceInfo` | dataclass | org / space / display_name / status / principal_path / policy / metadata / created_at / archived_at |
| `SpacePatch` | dataclass | display_name / status / principal_path / policy / metadata |
| `SpaceMember` | dataclass | scope / role / created_at / expires_at |
| `SpaceUsage` | dataclass | org / space / memory_count / message_count / index_count / storage_bytes / audit_count |
| `SpaceDeleteResult` | dataclass | org / space / deleted_counts / status / audit_event_id |

### 生命周期状态映射（delete 路径）

| DeleteMode | 目标 LifecycleState |
|------------|---------------------|
| FORGET | FORGOTTEN |
| ARCHIVE | ARCHIVED |
| DOWNWEIGHT | ACTIVE（仅降权，不变状态） |
| PURGE | 无状态——物理删除 |

### 实现注册机制

每个算子对应一个 `*_impl/` 子目录：

```
src/control/<算子>_impl/
    __init__.py             # import 实现模块，触发自注册
    <impl_class_snake>.py   # 具体实现 + 尾部 @Producer.register("name")
```

自注册模式：Producer 定义在对应顶层接口文件中；实现文件尾部 `@XxxProducer.register("name")` 绑定构建函数，`__init__.py` 导入实现文件触发注册，`control.bootstrap.register_controllers()` 统一 import 各 `*_impl` 包。装配层通过 Producer 按配置选取实现。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | 数据面委托 MemoryEngine；治理/授权/调度/策略/space 管理面直达控制算子 |
| S05-construction | Engine/Scheduler 驱动构建层 IndexBuilder/Evolver；演进逻辑由构建层执行 |
| S06-storage | 控制层通过 KVStore 读写真源；LifecycleManager/Governor 的目标操作按显式 Scope 点查或枚举，只有 sweep/offboarding 这类全局管理任务使用 `kv.scopes()` |
| S07-common | 控制层消费 `MemoryUnit`、`AuditEvent`、错误类型等公共结构 |
| architecture.md §3.1 | MemoryUnit 数据模型（lifecycle / temporal / supersedes / provenance）由 `common/type_def` 定义，控制层消费 |
| architecture.md §8 | 演进调度（EvolveMode / Channel）映射到 Scheduler 双通道 + Evolver 四阶段 |
| architecture.md §9 | `src/api/MemoryAPI` 是控制层的薄封装 + PEP；数据面委托 Engine，管理面直达各算子 |
| architecture.md §12 | 横切可观测/治理——Governor.audit 消费 `common/audit/AuditLogger` 记录的审计事件 |
| architecture.md §13.4 | PolicyManager 是运行时可变策略的 admin 落点 |
