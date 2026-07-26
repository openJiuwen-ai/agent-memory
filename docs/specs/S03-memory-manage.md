# S03 — 控制层（Control Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/control/ |
| 最近一次修订日期 | 2026-07-25 |

| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/construction/F02-dynamic-extraction-consolidation.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F03-control-pipeline-routing.md，docs/features/control/F04-permission-context-routing.md |
## 范围 / 边界

**管什么**：
- 记忆引擎编排（接口层各语义的跨层委托中枢）
- 按记忆类型选择构建/查询 pipeline profile
- 记忆单元生命周期状态流转（active → superseded → archived → forgotten）
- 治理「看」侧（检视 / 血缘回溯 / 审计查询）
- 跨 scope 权限授权与校验
- 演进任务的 hot/background 双通道调度
- 运行时可变策略的查询与调整

**不管什么**：
- 不执行鉴权（PEP 在 `src/api` 层，Engine 信任传入的 scope）
- 不直接操作存储（通过注入的 Store 抽象间接调用）
- 不实现抽取/升华/关联/冲突消解等演进逻辑（由构建层 `Evolver` 负责，控制层仅调度）
- 不生产记忆（由 `src/ingest` + `src/construction` 负责）
- 不执行检索（由 `src/retrieval` 负责）
- 不管不可变/重型配置（由 `src/config` 在实例初始化时确定）

## 不变量

1. **引擎不实现具体能力**：`MemoryEngine` 只编排，Ingestor/构建算子/Retriever/Store 全部由装配注入。禁止在 engine 内直接操作存储或调用 LLM。
2. **引擎方法一律异步协程**：同步调用由 `src/api` 层自行桥接（`asyncio.run`），engine 内不做同步阻塞。
3. **鉴权不在本层执行**：`PermissionManager.check` 由 `src/api/MemoryAPI` 在入口调用，engine 信任传入的 scope 已鉴权。禁止在 engine 内部重复 check。
4. **LifecycleManager 只做非破坏式标记**：`transition` 标记状态（superseded/archived/forgotten），绝不物理删除。物理删除（purge）走 Engine 的 `delete` 路径 + `DeleteMode.PURGE`。
5. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 Producer 自注册后被外部装配消费，不被顶层接口引用。
6. **types.py 零依赖本层其他文件**：纯数据定义，被本层各接口和 `src/api/` 共同依赖。
7. **DeleteSelector 各条件取「与」**：至少给出一项可命中条件；空 selector 抛 `ValidationError`。
8. **UpdateMode.SUPERSEDE 产生新 id**：旧 id 标记 superseded，`update` 返回的记忆 id 可能与传入的 `unit_id` 不同。
9. **admin_* 不经 Engine**：由 API 层直达 PolicyManager；Engine 中对应方法抛 NotImplementedError。
10. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `ControlOperator`，自描述 + 存活探测。
11. **自演进由控制层调度、构建层执行**：控制层只提交任务、管理通道和任务状态；抽取、升华、关联、冲突消解与索引维护逻辑归构建层。
12. **Pipeline 只做跨层 profile 选择**：`MemoryPipeline` 可以选择不同的构建/查询组件绑定，但不得实现抽取、索引、检索算法；construction/retrieval 不反向依赖 control。
13. **权限上下文由 API/Engine 解析，不信任调用方声明**：write/recall 可直接从请求参数构造 `PermissionContext`；get/update/delete 这类已有 unit 操作必须由 Engine 从真源元数据解析 memory_type/tags 后再鉴权。

## 接口契约

### ControlOperator（基类，`base.py`）

```python
class ControlOperatorType(str, Enum):
    ENGINE / PIPELINE / LIFECYCLE / GOVERNOR / PERMISSION / SCHEDULER / POLICY

class ControlOperator(ABC):
    def operator_type(self) -> ControlOperatorType  # 自描述
    def health(self) -> None                        # 存活探测：健康返回 None，否则抛异常
```

### MemoryEngine（`engine.py`）

编排接口层各语义的中枢。注入依赖：Ingestor、Classifier、Consolidator、IndexBuilder、Evolver、Retriever、KVStore、Scheduler、LifecycleManager；可选注入 MemoryPipeline 做按记忆类型的 profile 选择。

| 方法 | 签名 | 语义 |
|------|------|------|
| `write` | `async (content, scope, source, *, assets, tags, metadata, occurred_at) -> list[MemoryUnit]` | 规约→可选抽取/分类→Consolidator 巩固落盘；`infer=true` 时返回派生结果，否则处理原始单元 |
| `recall` | `async (scope, query: RetrievalQuery) -> RetrievalResult` | 委托 Retriever 完整检索链路 |
| `permission_context_for_unit` | `async (unit_id, scope) -> PermissionContext` | 读取已有记忆的权限上下文，只返回 memory_type/tags/metadata 等鉴权元数据，不返回 content/assets |
| `permission_contexts_for_delete` | `async (selector: DeleteSelector) -> list[PermissionContext]` | 解析 delete selector 命中的候选 unit 权限上下文，供 API 层逐条鉴权 |
| `get` | `async (unit_id, scope, as_of=None) -> MemoryUnit` | 真源点读；`as_of` 非空沿 supersedes 链返回当时有效版本 |
| `update` | `async (unit_id, scope, patch: MemoryPatch) -> MemoryUnit` | SUPERSEDE 新 id 记版本链 / OVERWRITE 原地覆写 |
| `delete` | `async (selector: DeleteSelector) -> list[str]` | PURGE 物理删 / 其他委托 LifecycleManager 非破坏式流转 |
| `evolve` | `async (scope, mode: EvolveMode, channel=BACKGROUND) -> str` | 提交演进任务到 Scheduler；执行逻辑由构建层 Evolver 完成，返回 job_id |
| `admin_get/set/all` | — | 由 API 层直达 PolicyManager，Engine 抛 NotImplementedError |

**write 路径**：
```
Ingestor.ingest([RawPayload]) → list[MemoryUnit]
→ 将 content/assets 入参补入接入层产出的 MemoryUnit.segments，并补齐 tags
→ MemoryPipeline.select_for_write(units)  # 可选；未注入时使用 Engine 默认组件
→ Classifier.classify(units)
→ if metadata["infer"] == "true":
      选中 profile 的 Evolver.evolve(units, EXTRACT)
      Extractor → LayerAnnotator → Consolidator
  else:
      选中 profile 的 Consolidator.consolidate(units)
→ 返回本次 created_ids + updated_ids 对应单元；NOOP 可返回空
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
    consolidator: Consolidator | None = None

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
- 查询侧优先读取 `RetrievalQuery.extensions[route_key]`，其次读取等值 filter 的 `route_key` 或 `metadata.<route_key>`。
- `routes` 把路由值映射到 profile 名；未命中时若路由值本身是 profile 名则直接使用，否则退回 `fallback`。
- 未配置 `pipeline.default` 时不启用 pipeline，行为等价旧单 pipeline；用户通过 YAML 显式声明后启用。

> `infer` 开关详见 [`S02-memory-api.md`](S02-memory-api.md)「infer 开关」与 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)。默认路径不再自动提交 background EXTRACT——`InProcessScheduler` 同步执行下"自动提交"实为同步阻塞，与双通道时延初衷相悖；真异步 Scheduler 落地后是否恢复可选自动提交另行决策。

**delete 路径**：
```
遍历 selector.unit_ids:
  → PURGE: KVStore.delete(scope, unit_id) + IndexBuilder.remove([unit_id])
  → 其他: LifecycleManager.transition([unit_id], 对应状态) + IndexBuilder.remove([unit_id])
→ 返回命中 id 列表
```

### LifecycleManager（`lifecycle.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `transition` | `(unit_ids: list[str], target: LifecycleState) -> None` | 批量非破坏式状态标记 |
| `supersede` | `(unit_id: str, invalid_at: datetime) -> MemoryUnit` | 将旧版本标记 SUPERSEDED，并把 valid-time 失效边界设为 `invalid_at` |
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
| `inspect` | `(unit_ids: list[str]) -> list[MemoryUnit]` | 跨 scope 检视完整内容与治理字段（含已失效版本） |
| `trace` | `(unit_id: str) -> list[MemoryUnit]` | 沿 `provenance` 血缘向上追溯演进来源链；`supersedes` 仅用于版本链与 `get(as_of)` |
| `audit` | `(filters: dict[str, str], limit=100) -> list[AuditEvent]` | 治理层唯一对外审计查询入口，按 `action` / `layer` / `decision` / `target_id` / `actor_org` / `actor_user` / `actor_agent` / `actor_session` / `occurred_after` / `occurred_before` 过滤审计事件；底层 `AuditLogger` 负责记录事件，并通过 `query(filters, limit)` 向治理层提供后端内查询能力；审计后端默认使用 SQLite `:memory:`，可通过 `audit.default = {"target": "sqlite", "params": {"db_path": "..."}}` 切换为 SQLite 落盘 |

### PermissionManager（`permission.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `grant` | `(grant: Grant) -> None` | 新增跨 scope 授权 |
| `revoke` | `(grant: Grant) -> None` | 回收授权（幂等） |
| `check` | `(actor: Scope, target: Scope, action: Action, context: PermissionContext \| None = None) -> bool` | 校验 actor 对 target 是否可执行 action；context 为资源类型、memory_type、pipeline、unit_id、tags 等可选上下文 |

**check 规则**：
1. `actor == Scope()`（platform admin）→ 全局通过
2. actor owner-cover target（当前实现要求同 `org`、同 `user`，并允许 agent/session 向下覆盖）→ 通过
3. `actor.org != target.org` 且 actor 非 root → 拒绝
4. 存在匹配 Grant（未过期 + action 在授权集合内 + grantee 覆盖 actor + grantor 覆盖 target）→ 通过
5. 否则 → 拒绝

默认权限后端为 `sqlite`，配置为 `{"target": "sqlite", "params": {"db_path": ":memory:"}}`；`allow_all` 仅保留为显式 dev-only 配置。无具体 target scope 的管理面方法（`admin_get` / `admin_set` / `admin_all` / 全局 `audit`）统一以根 scope `Scope()` 作为鉴权目标，普通租户 scope 不默认具备管理面访问权；`grant` / `revoke` 则以 grantor scope 为 target 做 `Action.SHARE` 校验。

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
  standard: allow_all
  strict: sqlite
```

`routing` 不改变授权语义，只选择 delegate；`grant` / `revoke` 会广播给全部 delegate，避免调用方理解授权记录应落在哪个后端。

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
| `UpdateMode` | 枚举 | SUPERSEDE（默认，新 id）/ OVERWRITE（同 id） |
| `MemoryPatch` | dataclass | content（修正后的文本投影，应用时更新对应 Segment 内容） / tier / tags / metadata / t_valid / t_invalid / mode(UpdateMode) |
| `DeleteMode` | 枚举 | FORGET / ARCHIVE / DOWNWEIGHT / PURGE |
| `DeleteSelector` | dataclass | unit_ids / scope / tags / before / mode(DeleteMode) |

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
| S02-memory_api | 数据面委托 MemoryEngine；治理/授权/调度/策略面直达控制算子 |
| S05-construction | Engine/Scheduler 驱动构建层 IndexBuilder/Evolver；演进逻辑由构建层执行 |
| S06-storage | 控制层通过 KVStore 读写真源；LifecycleManager/Governor 依赖 `kv.scopes()` + `kv.list()` 做跨 scope 枚举 |
| S07-common | 控制层消费 `MemoryUnit`、`AuditEvent`、错误类型等公共结构 |
| architecture.md §3.1 | MemoryUnit 数据模型（lifecycle / temporal / supersedes / provenance）由 `common/type_def` 定义，控制层消费 |
| architecture.md §8 | 演进调度（EvolveMode / Channel）映射到 Scheduler 双通道 + Evolver 四阶段 |
| architecture.md §9 | `src/api/MemoryAPI` 是控制层的薄封装 + PEP；数据面委托 Engine，管理面直达各算子 |
| architecture.md §12 | 横切可观测/治理——Governor.audit 消费 `common/audit/AuditLogger` 记录的审计事件 |
| architecture.md §13.4 | PolicyManager 是运行时可变策略的 admin 落点 |
