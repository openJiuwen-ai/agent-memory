# S03 — 控制层（Control Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/control/ |
| 最近一次修订日期 | 2026-09-04 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 规划中的变更 | 群体记忆与空间治理（含契约与决策）见 [F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md)；本文描述当前形态 |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/api/F03-batch-write-api.md，docs/features/construction/F02-dynamic-extraction-consolidation.md，docs/features/construction/F04-cc-memory-compat.md，docs/features/construction/F07-memory-write-entry.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F03-control-pipeline-routing.md，docs/features/control/F04-permission-context-routing.md，docs/features/control/F05-cloud-engine-design.md，docs/features/common/F08-memory-tree.md，docs/features/common/F03-scope-space-isolation.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/config/F01-config-source.md，docs/features/ingest/F02-assets-ingestor-boundary.md，docs/features/storage/F07-storage-manager-domain-store-split.md（合并原 F07/F08/F09） |

## Metadata 编排契约

`MemoryEngine.write` 分别接收两个命名空。引擎控制流、pipeline 路由、权限上下文
和内部状态只读 `system_metadata`；`user_metadata` 只透传给领域对象、索引和返回链路。
`infer` / `procedural` / `middle` 不得从用户命名空读取。
## 范围 / 边界

**管什么**：
- 记忆引擎编排（接口层各语义的跨层委托中枢）
- 按记忆类型选择构建/查询 pipeline profile
- 记忆单元生命周期状态流转（active → superseded → archived → forgotten）
- 治理「看」侧（检视 / 血缘回溯 / 审计查询）
- 跨 scope 权限授权与校验
- 演进任务的 hot/background 双通道调度
- 长耗时摄入任务的队列、状态持久化和 Scope 内幂等
- 运行时可变策略的查询与调整
- space 生命周期、策略、成员、用量、导出与 offboarding 管理
- 应用端口（`control/application`）：已鉴权之后的数据面命令/查询、治理读取、Space 删除事务的 typed 入口；包装现有算子，不是新算子

**不管什么**：
- 不执行鉴权（PEP 在 `jiuwen_memory/api` 层，Engine 信任传入的 scope）
- 不绑定具体存储后端（只通过注入的 Store 抽象读写）
- 不实现抽取/升华/关联/冲突消解等演进逻辑（由构建层 `Evolver` 负责，控制层仅调度）
- 不生产记忆（由 `jiuwen_memory/ingest` + `jiuwen_memory/construction` 负责）
- 不执行检索（由 `jiuwen_memory/retrieval` 负责）。「执行」指调用该层的**算子**——有 Producer 注册、实现可替换、访问存储或模型的组件（`Retriever` / `Recaller` / `Fuser` 等）。两件事不在禁止之列：import 该层导出的类型与无状态纯函数（如 `retrieval/cross_space.py` 的取数上界、结果合并与失败编码），以及经调用方传入的 `recall` 回调调 `MemoryEngine` 门面。两者都不使本层持有检索算子实例，依赖方向仍是 control → retrieval，检索层不反向依赖控制层，无环。缺这条限定，`collective/cross_space_recall.py` 的召回扇出会被读成越界
- 不管不可变/重型配置（由 `jiuwen_memory/config` 在实例初始化时确定）

## 不变量

1. **引擎不实现具体算法能力**：`MemoryEngine` 只编排，Ingestor/构建算子/Retriever/Store 全部由装配注入；可通过 Store 抽象完成真源语义，但不得绑定具体后端或直接调用 LLM。
2. **引擎方法一律异步协程**：同步调用由 `jiuwen_memory/api` 层自行桥接（`asyncio.run`），engine 内不做同步阻塞。
3. **鉴权不在本层执行**：`PermissionManager.check` 由 `jiuwen_memory/api/MemoryAPI` 在入口调用，engine 信任传入的 scope 已鉴权。禁止在 engine 内部重复 check。
4. **LifecycleManager 只做非破坏式标记**：`transition` 标记状态（superseded/archived/forgotten），绝不物理删除。物理删除（purge）走 Engine 的 `delete` 路径 + `DeleteMode.PURGE`。
5. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 Producer 自注册后被外部装配消费，不被顶层接口引用。
6. **types.py 零依赖本层其他文件**：纯数据定义，被本层各接口和 `jiuwen_memory/api/` 共同依赖。
7. **DeleteSelector 各条件取「与」**：至少给出一项可命中条件；空 selector 抛 `ValidationError`。
8. **UpdateMode.SUPERSEDE 产生新 id**：旧 id 标记 superseded，`update` 返回的记忆 id 可能与传入的 `unit_id` 不同。
9. **admin_* 不经 Engine**：由 API 层直达 PolicyManager；Engine 不承载策略存储。
10. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `ControlOperator`，自描述 + 存活探测。
11. **自演进由控制层调度、构建层执行**：控制层只提交任务、管理通道和任务状态；抽取、升华、关联、冲突消解与索引维护逻辑归构建层。
12. **Pipeline 只做跨层 profile 选择**：`MemoryPipeline` 可以选择不同的构建/查询组件绑定，但不得实现抽取、索引、检索算法；construction/retrieval 不反向依赖 control。
13. **权限上下文由 API/Engine 解析，不信任调用方声明**：add/search/list 的请求条件可由 API 构造 `PermissionContext`；list 当前分页实际命中的 unit 以及 get/update/delete 这类已有 unit 操作必须由 Engine 从真源元数据解析 memory_type/tags 后再鉴权。
14. **权限路由与执行路由同源但职责独立**：两者对 recall 都使用
    extensions 优先、FilterExpr 强制唯一等值兜底的取值规则；PermissionManager 选择
    授权策略，MemoryPipeline 选择执行组件，互不代替。
15. **路由授权绑定数据范围**：路由型权限根据某字段授权后，API 必须把同一值回注为
    系统过滤谓词；routing fallback 必须是最小权限策略，不得使用 `allow_all`。
16. **目标操作使用完整 Scope**：MemoryUnit id 仅在 Scope 内唯一。LifecycleManager、Governor 与 IndexBuilder 的目标修改/读取/删除不得依赖全局 `id -> scope` 猜测，调用方必须显式提供 Scope 或携带 Scope 的 MemoryUnit。
17. **Engine 部署边界明确**：`InMemoryEngine` 只接受空 `space` 兼容域；具名非空 space 的数据面操作使用 `CloudEngine`。`CloudEngine` 仍兼容空 space，但生产多租户配置应开启 `scope.require_space=true`。
18. **普通 write 默认不建树（目标）**：`hierarchy.auto_derive` 是独立、默认关闭的后台策略；显式层级 recall/evolve/update 在 `hierarchy.enabled=false` 时抛 `PolicyError`，普通非层级行为兼容。
19. **树结构一致性（目标）**：同一 kind 的父子边必须同 `org+space`、无环、单父、双向一致且顺序稳定；`user`/`agent`/`session` 可按 compose profile 放宽（跨细粒度 scope 时边须可解析定位）；`HierarchyStatus` 只允许 ACTIVE/DISMISSED，且与 `LifecycleState` 分离。
20. **结构与生命周期事务（目标）**：`provenance`、`supersedes` 与 `hierarchy` 分别表示演进来源、版本替换和父子包含；FORGET/PURGE 不级联删除后代内容。
21. **重叠 span 串行化（目标）**：同一 `scope + kind` 下 span 相交的 HIERARCHY build/replace、层级 update、FORGET 和 PURGE 必须串行化，或以乐观版本条件在提交前检测冲突；replace 不得吸收未参与初始输入快照的并发叶写入。
22. **判权范围的裁剪可落本层，判权的执行不可**：`collective/write_targets.py` 决定哪些候选空间被送去判权（候选渲染、排序与上限截断），判权本身经 `can_write` 回调由 API 层执行；本层不持有 `PermissionManager`、不接收 `identity`、不抛权限异常——缺兜底落点时返回 `WriteTargets.fallback=None`，由 PEP 抛出。该裁剪可下沉的前提是失效方向为拒绝：未参与判权的空间不进候选，表现为写不进去而非越权写入。**检索侧的逐空间判权循环不适用本条**，其循环体就是 `PermissionManager.decide` 本身，移出等于把 PEP 分裂为两处；逐空间系统谓词的生成同样留 API 层，它按 `identity` 与空间事实取值。检索侧可下沉的是判权之后的部分：`collective/cross_space_recall.py` 收已判权的空间目标（含各自的谓词）与 `recall` 回调，做取数上界摊配、召回扇出与结果合并，全程不读 `identity`、不做任何裁决，与写入侧同一形态。它把空间级扇出失败与判权剔除分两路交回——并进 `merged.errors` 之后，扇出失败会与检索层的分通道错误混在同一个列表里，API 层要为「整个空间挂了」写审计就只能按 `channel is SPACE` 过滤，那是把审计判据绑在本层的 channel 编码上。
23. **应用端口不是算子**：`control/application` 的 `MemoryCommandService` / `MemoryQueryService` / `SpaceLifecycleService` / `GovernanceService` 无 Producer、不执行 PEP、不接收 `identity`。它们由已注入的 Engine / Governor / SpaceManager 组成，供 API 与单测复用同一语义，禁止 Service Locator。`delete_space` 的 purge → SpaceManager.delete → `deleted_counts` 汇总只允许出现在 `SpaceLifecycleService`。purge 成功而 metadata delete 失败必须抛 `PartialFailureError`，重试入口仍是 `delete_space`。路由谓词回注、逐空间判权和逐单元结果鉴权仍属 PEP（见不变量 22），不经这些端口下沉。
24. **Engine 不解释资产映射**：`write` 只把 `assets` 防御性复制到 `RawPayload.assets` 后交给 Ingestor；Ingestor 返回后，Engine 不得按“首个 Segment”或其他假设改写 `Segment.assets`。

## 接口契约

### ControlOperator（基类，`base.py`）

```python
class ControlOperatorType(str, Enum):
    ENGINE / PIPELINE / LIFECYCLE / GOVERNOR / PERMISSION / SCHEDULER / INGEST_JOB /
    POLICY / SPACE

class ControlOperator(ABC):
    def operator_type(self) -> ControlOperatorType  # 自描述
    def health(self) -> None                        # 存活探测：健康返回 None，否则抛异常
```

### MemoryEngine（`engine.py`）

编排接口层各语义的中枢。注入依赖：Ingestor、Classifier、IndexBuilder、Evolver、Retriever、KVStore（真源端口，读经 `load_units`/`list_units` helper）、Scheduler、LifecycleManager；可选注入 MemoryPipeline 做按记忆类型或 message_type 的 profile 选择。Evolver 有两个平级注册实现：`OrchestratingEvolver`（注册名 `orchestrating`）与其子类 `DynamicEvolver`（注册名 `dynamic`，EXTRACT 走 extract→consolidate→reflect→落盘）；装配或 profile 选择决定实际路径。

| 方法 | 签名 | 语义 |
|------|------|------|
| `write` | `async (content, scope, source, *, assets, tags, system_metadata, user_metadata, occurred_at) -> list[MemoryUnit]` | 规约→可选抽取/分类→落盘+建索引；`infer=true` 时返回 `created_ids` 对应的派生结果，否则处理原始单元（直写不去重） |
| `batch_write` | `async (items: list[BatchWriteItem], *, continue_on_error=True) -> BatchWriteResult` | 只接收 API 已归一化并完成鉴权/space 前置校验的项；按输入顺序复用 `write`，归集领域异常及非领域异常（后者为 `InternalError`）；fail-fast 时填充 `Skipped` outcomes |
| `recall` | `async (scope, query: RetrievalQuery) -> RetrievalResult` | 委托 Retriever 完整检索链路（含目标 `expand_depth>0` 时的内部展开） |
| `list` | `async (scope, *, offset=0, limit=100, memory_types=None, extensions=None, filters=None) -> MemoryListResult` | 校验分页参数并完整委托 `KVStore.list`（经 `list_units` helper 反序列化）；返回当前页和分页前匹配总数 |
| `permission_context_for_unit` | `async (unit_id, scope) -> PermissionContext` | 读取已有记忆的权限上下文，只返回 memory_type/tags/metadata 等鉴权元数据，不返回 content/assets |
| `list_with_permission_contexts` | `async (同 list 参数) -> tuple[MemoryListResult, list[PermissionContext]]` | 从同一次 KV 查询的当前页构造逐项真源权限上下文，items/count/context 不做二次读取 |
| `permission_contexts_for_delete` | `async (selector: DeleteSelector) -> list[PermissionContext]` | 解析 delete selector 命中的候选 unit 权限上下文，供 API 层逐条鉴权 |
| `get` | `async (unit_id, scope, as_of=None) -> MemoryUnit` | 真源点读；`as_of` 非空沿 supersedes 链返回当时有效版本 |
| `update` | `async (unit_id, scope, patch: MemoryPatch) -> MemoryUnit` | SUPERSEDE 新 id 记版本链 / OVERWRITE 原地覆写；目标层级 patch 走结构事务 |
| `delete` | `async (selector: DeleteSelector) -> list[str]` | PURGE 物理删 / 其他委托 LifecycleManager 非破坏式流转；目标需维护受影响层级边 |
| `purge_space` | `async (org: str, space: str) -> list[str]` | 物理删除该 Space 全部 user/agent/session 子 Scope 的 MemoryUnit 真源与索引，供 offboarding 调用 |
| `evolve` | `async (scope, mode: EvolveMode, channel=BACKGROUND, *, hierarchy_options=None) -> str` | 提交演进任务到 Scheduler；执行逻辑由构建层 Evolver 完成，返回 job_id；仅目标 HIERARCHY 接受 options |
| `admin_get/set/all` | — | 管理面语义由 API 层直达 PolicyManager，Engine 不承载策略存储 |

**write 路径**：
```
Engine 组装 RawPayload（含 assets 的防御性副本）
→ Ingestor.ingest([RawPayload]) → list[MemoryUnit]
  # assets 如何映射到 Segment 由 Ingestor 实现决定
→ Engine 补充引擎管理的 metadata/tags，不改写 Segment.assets
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
      选中 profile 的 IndexBuilder.build(units)   # 记忆本体的交付含在其中
      返回 units
```


### 树结构目标扩展（尚未实现）

普通 write 默认不建父树。`hierarchy.auto_derive=false` 时不提交任何层级任务。启用后，write 在不可变构建配置已有 compose profile 且本批叶可确定有界 span 时，必须在成功返回后向 BACKGROUND 通道提交 HIERARCHY 任务，并固定组装 `replace_existing=true`；条件不足时不提交，并记录跳过原因。提交失败只记录任务/审计错误，不回滚已经成功的权威叶写入。auto derive 不得改成阻塞 hot path，也不得推断未配置的 kind、role 或无界 span。

#### ensure_hierarchy

策略名统一为 `hierarchy.ensure_on_recall`，默认 `false`。只有同时满足以下条件才允许 ensure：

1. `hierarchy.enabled=true`；
2. `hierarchy.ensure_on_recall=true`；
3. recall 显式提供一个 `hierarchy_kind`；
4. `span_start` 与 `span_end` 成对、有效且有界。

每个可 ensure 的 kind 还必须在不可变构建配置中存在 S05 定义的 `HierarchyComposeProfile`。profile 按 kind 唯一查找，提供 `leaf_role/parent_roles` 和 stage options；请求提供 kind+span，Engine 据 profile 组装完整 `HierarchyComposeOptions`，并固定 `replace_existing=true`。缺少 profile 时抛 `PolicyError`。

本规约选择**阻塞式 ensure**：Engine 在父层召回前同步提交对应 kind+span 的 HIERARCHY 构建，并等待任务进入终态。SUCCEEDED 且 `complete=true` 后才执行 recall；FAILED、CANCELLED、修复未完成或超过调度器配置的等待期限均抛 `BackendError`。功能关闭时显式层级请求抛 `PolicyError`，不得悄悄退化为无层级结果。该行为只针对明确的有界层级 recall；普通 recall 与无 span 的层级 recall 从不触发 ensure。

当 `ensure_on_recall=false` 时，显式层级 recall 只查询当前已有结构，不自动构建；合法的空结果仍返回空结果。

#### HierarchyPatch

```python
class HierarchyEdgeOp(str, Enum):
    ATTACH = "attach"
    DETACH = "detach"

@dataclass
class HierarchyEdgeChange:
    op: HierarchyEdgeOp
    child_id: str
    expected_parent_id: str = ""

@dataclass
class HierarchySpanPatch:
    span_start: datetime | None
    span_end: datetime | None

@dataclass
class HierarchyPatch:
    kind: HierarchyKind
    status: HierarchyStatus | None = None
    span: HierarchySpanPatch | None = None
    edge_changes: list[HierarchyEdgeChange] = field(default_factory=list)
    child_order: list[str] | None = None
```

patch 以被 update 的 `unit_id` 为父节点。`ATTACH` 把 child 挂到该父；若 child 已有父，`expected_parent_id` 必须精确匹配旧父，Engine 才能在同一事务中从旧父移除并改挂。`DETACH` 要求 child 当前父为该 unit；`expected_parent_id` 为空或等于该 unit，否则 `ConflictError`。

`child_order` 若提供，必须恰好是应用全部 edge_changes 后的完整直接子集合，无重复、无缺失。未提供时保留未变子节点的相对顺序，DETACH 删除原位置，ATTACH 按请求顺序追加。TIME 结构最终按 span 起点、事件时间和稳定次序校验；其他 kind 按 `ordinal` 和领域稳定顺序校验。

`span=None` 表示不修改区间；`HierarchySpanPatch(None, None)` 表示清除区间，仅非 TIME kind 允许；其他组合必须同时给出起止且起点不晚于终点。

Engine 在写入前必须一次性加载所有受影响 unit 并验证：同 org+space、同 kind、单父、无环、双向一致、span 有效且 TIME 必填；跨细粒度 scope 时 `child_scopes`/`parent_scope` 可解析。调用方不得通过 `metadata`、完整 `HierarchyRef`、裸 `parent_id` 或裸 `child_ids` 绕过该接口。校验失败不写任何 unit。

`UpdateMode.SUPERSEDE` 对已挂树 unit 生成新 id 后，必须在同一结构事务中把父列表中的旧 id 替换为新 id，并把直接子的 `parent_id` 改为新 id，位置不变；随后旧 unit 进入 SUPERSEDED。`OVERWRITE` 保持 id。

#### delete 与层级边

删除选择器先解析完整命中集，再按稳定 id 顺序处理：

| DeleteMode | 生命周期/存储 | 层级边 |
|---|---|---|
| DOWNWEIGHT | lifecycle 保持 ACTIVE，仅降权 | 不改边 |
| ARCHIVE | lifecycle 设 ARCHIVED | 保留边；默认召回和展开按 lifecycle 排除，显式 include_archived 可见 |
| FORGET | lifecycle 设 FORGOTTEN | 提交前双向断开所有直接父边和子边 |
| PURGE | 物理删除真源与索引 | 先双向断边，再删除目标 |

删除父节点时，直接子仅清空指向该父的 `parent_id`，成为未挂接节点；不删除、归档或遗忘子。删除子节点时，从父 `child_ids` 移除并保持其余顺序。一个 selector 同时命中父子时先计算最终存活边，再一次提交。空父默认保留；任何 PURGE 都不得沿 hierarchy 级联删除权威叶。

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

- 写入侧读取 `MemoryUnit.system_metadata[route_key]`。
- 查询侧优先读取 `RetrievalQuery.extensions[route_key]`，其次从规范化
  `FilterExpr` 提取逻辑上强制成立的 `system_metadata.<route_key>` 唯一等值；
  `memory_type` 裸字段仅作为 `system_metadata.memory_type` 的输入别名。
- `routes` 把路由值映射到 profile 名；未命中时若路由值本身是 profile 名则直接使用，否则退回 `fallback`。
- 未配置 `pipeline.default` 时不启用 pipeline，行为等价旧单 pipeline；用户通过 YAML 显式声明后启用。

> `infer` 开关详见 [`S02-memory-api.md`](S02-memory-api.md)「infer 开关」与 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)。默认路径不再自动提交 background EXTRACT——`InProcessScheduler` 同步执行下"自动提交"实为同步阻塞，与双通道时延初衷相悖；真异步 Scheduler 落地后是否恢复可选自动提交另行决策。

**delete 路径**：
```
按完整 Scope 分组遍历 selector 命中的 MemoryUnit:
  → PURGE: IndexBuilder.remove([unit])                        # 本体与派生索引一并删除（HARD）
  → 其他: LifecycleManager.transition(scope, [unit.id], 对应状态)   # 本体保留新状态
           + IndexBuilder.remove([unit], mode=SOFT)                  # 仅退出检索
→ 返回命中 id 列表
```

### LifecycleManager（`lifecycle.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `transition` | `(scope: Scope, unit_ids: list[str], target: LifecycleState) -> None` | 在指定 Scope 内批量非破坏式状态标记 |
| `supersede` | `(scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit` | 在指定 Scope 内将旧版本标记 SUPERSEDED，并把 valid-time 失效边界设为 `invalid_at` |
| `sweep` | `() -> list[str]` | 扫描到期（`t_invalid` 已过）的 active 单元，标记 FORGOTTEN；目标挂树 unit 必须走与 FORGET 相同的断边编排 |

LifecycleManager 不修改 `HierarchyStatus`。结构 status 的 ACTIVE 不会覆盖 ARCHIVED/FORGOTTEN 等生命周期过滤。

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
| `grant` | `(grant: Grant) -> None` | 新增跨 scope 授权 |
| `revoke` | `(grant: Grant) -> None` | 回收授权（幂等） |
| `check` | `(actor: Scope, target: Scope, action: Action, context: PermissionContext \| None = None) -> bool` | 校验 actor 对 target 是否可执行 action；context 为资源类型、memory_type、pipeline、unit_id、tags 等可选上下文 |
| `routing_fields` | `() -> tuple[str, ...]` | 继承安全域共享的 `RoutingFieldsProvider`；返回本实现鉴权路由所依据的 metadata 字段，非路由实现返回空元组 |

**check 规则**：
1. `actor == Scope()`（platform admin）→ 全局通过
2. actor owner-cover target → 通过：先要求同 `org + space`，再按 `PermissionContext.metadata["principal_path"]`（`user_agent` / `agent_user`，默认 `user_agent`）判断 actor scope 是否为 target scope 的合法前缀；空字段不能跳过中间层
3. `actor.org != target.org` 且 actor 非 root → 拒绝；跨 org grant 不属于默认授权契约
4. 存在匹配 Grant（未过期 + action 在授权集合内 + grantee 覆盖 actor + grantor 覆盖 target）→ 通过；grantor/grantee 都持久化 `space`，显式 grant 可跨 space
5. 否则 → 拒绝

权限后端由配置选择；无具体 target scope 的管理面方法（`admin_get` / `admin_set` /
`admin_all` / 全局 `audit`）统一以根 scope `Scope()` 作为鉴权目标，普通租户
scope 不默认具备管理面访问权；`grant` / `revoke` 则以 grantor scope 为 target
做 `Action.SHARE` 校验。

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
| `submit` | `(scope: Scope, mode: EvolveMode, channel: Channel, *, hierarchy_options: HierarchyComposeOptions | None = None) -> str` | 提交演进任务，返回 job_id；仅 HIERARCHY 接受 options（目标） |
| `status` | `(job_id: str) -> JobInfo` | 查询任务状态 |
| `cancel` | `(job_id: str) -> None` | 取消尚未完成的任务（幂等） |

**双通道**：HOT（在线低时延：write 返回前完成的轻量索引）；BACKGROUND（离线异步：重的抽取/升华/重索引）。

目标 HIERARCHY 任务必须在 `JobInfo.detail` 中提供稳定字符串键：`hierarchy_kind`、`span_start`/`span_end`、`trigger`（`explicit`/`ensure_on_recall`/`auto_derive`）、`created_parent_count`、`updated_child_count`、`replaced_parent_count`、`repair_required_count`、`complete`、`error`。`detail["repair_required_count"]` 等于 `HierarchyComposeResult.repair_required` 的元素数量。目标 `JobInfo` 增加 `result: EvolveResult | None = None`（结构见 S05）；`complete=false` 或非空 `repair_required` 不得标记 SUCCEEDED。ensure 等待终态；auto derive 不等待。

### IngestJobController（`ingest_job.py`）

长耗时摄入任务使用独立 Control 算子管理，具体线程池实现位于 `job_impl/`。`status`
是无缓存副作用的只读查询；payload 幂等映射只在 Scope 一致后维护。接口层通过
`MemoryAPI.job_status()` 对任务真实 Scope 执行 READ 鉴权与审计。

### PolicyManager（`policy.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `get` | `(key: str) -> str` | 读取一项运行时策略 |
| `set` | `(key: str, value: str) -> None` | 调整策略（未知键/不可变配置抛 `PolicyError`） |
| `all` | `() -> dict[str, str]` | 列出全部运行时策略及当前值 |

> **与 ConfigSource 的边界（S08）**：PolicyManager 只管理少量**已知策略键**（如 lifecycle 清扫目标、`scope.require_space`、既有 `rerank.enabled` 占位键）。能力开关/prompt 全文/模型凭证/Store 端点与 `*.active` 等六类动态配置走 `ConfigSource.fetch`，不通过 `admin_set` 扩展为任意配置树。

### SpaceManager（`space.py`）

space 是 `org` 下的逻辑隔离单元。API 层负责鉴权与审计，`SpaceManager` 负责
space 元数据、space policy、成员、用量与 offboarding 状态管理。

| 方法 | 签名 | 语义 |
|------|------|------|
| `create` | `(spec: SpaceSpec) -> SpaceInfo` | 创建 space，写入 display name、主体路径、policy、metadata 与状态 |
| `get` | `(org: str, space: str) -> SpaceInfo` | 读取单个 space |
| `list` | `(org: str, *, status=None, limit=100, cursor=None) -> list[SpaceInfo]` | 列出 org 下 spaces；`cursor` 是由实现解释的分页游标，调用方不得解析其内部格式 |
| `update` | `(org: str, space: str, patch: SpacePatch) -> SpaceInfo` | 修改 display name、status、principal_path、policy 或 metadata |
| `archive` | `(org: str, space: str) -> SpaceInfo` | 归档 space；API 层会拒绝已归档 space 的 add/update/evolve |
| `delete` | `(org: str, space: str) -> SpaceDeleteResult` | 删除该 `org + space` 下 KV 真源、messages、space metadata；API 层在调用前先经 Engine purge memory 并清理索引 |
| `export` | `(org: str, space: str, *, include_audit=True) -> str` | 创建导出记录并返回 export id |
| `usage` | `(org: str, space: str) -> SpaceUsage` | 统计 memory/message 数量与 KV bytes；index/audit 计数由后续专用后端补齐 |
| `get_policy` | `(org: str, space: str) -> SpacePolicy` | 读取 space policy |
| `set_policy` | `(org: str, space: str, policy: SpacePolicy) -> SpacePolicy` | 替换 space policy，并同步 `principal_path` |
| `list_members` | `(org: str, space: str) -> list[SpaceMember]` | 列出成员与角色 |
| `add_member` | `(org: str, space: str, member: SpaceMember) -> None` | 添加或更新成员角色；成员 scope 的 org/space 归一为目标 space |
| `remove_member` | `(org: str, space: str, member: Scope) -> None` | 移除成员 |

目标层级策略键：

| 键 | 类型与默认 | 语义 |
|---|---|---|
| `hierarchy.enabled` | bool，`false` | 层级总开关 |
| `hierarchy.auto_derive` | bool，`false` | write 后是否后台派生 |
| `hierarchy.ensure_on_recall` | bool，`false` | 是否对显式有界层级 recall 阻塞确保结构 |
| `hierarchy.score_propagation` | str，默认 `maxp` | rollup 算法；当前仅接受 `maxp` |
| `hierarchy.expand_default_depth` | int，`1` | 仅供未显式给 `expand_depth` 的内部/接入形态默认值；公开 recall 默认仍为 0 |
| `hierarchy.expand_top_m` | int \| None，`None` | 每个父最多保留的直接子数；必须 > 0；`None` 表示不额外裁剪（仍受 depth 与 `max_tokens` 约束） |

`enabled=false` 优先于其他层级键。修改策略不回写已有 unit，不触发隐式重建。未知值或越界值抛 `PolicyError`。

#### 群体记忆带来的控制层变更（F07）

| 项 | 内容 | 状态 |
|---|---|---|
| 新增算子 `MembershipResolver`（`membership.py`） | 一次读取空间授权事实（元数据 + 已滤除过期记录的成员表）并缓存，向鉴权点提供同一份快照；另提供主体到空间的反查与缓存失效 | 已落地，消费方是空间感知判定实现 |
| 新增子包 `collective/` | 三个非算子模块，均不含判据、不读 `identity`。`routing.py`：结论直写路径的归属判定调用点，判定算子在构建层、判定输入由 API 层的鉴权点构造，二者之间的调用按 S02 的分层边界落在本层；不接判权回调——`RouteContext.candidates` 是 API 层判权后给出的成品集合。`write_targets.py`：写入候选空间集合的计算，接判权回调（`identity` 由 API 层闭包捕获，不出现在本层签名内），不抛权限异常，见不变量 22。`cross_space_recall.py`：跨空间召回的取数上界摊配、扇出与合并，接 `recall` 回调与已判权的空间目标（含逐空间谓词），只 import `retrieval/cross_space.py` 的三个纯函数、不持有引擎；空间级扇出失败单独返回，不并进 `merged.errors`。带实现的模块收在子包而非顶层，以保持「顶层只定义抽象接口」 | 已落地 |
| `SpaceManager` 新增 `spaces_for` | 主体到空间的反查，取代 `list` 的全 keyspace 遍历。与 `list` 是同一批成员关系的两个查询方向：`list` 按 org 枚举空间，`spaces_for` 按主体反查。KV 没有二级索引，实现须另建一份按主体组织的派生索引并在成员与归属的增删处同步维护，超集语义（允许多给、不允许遗漏）。与 `list` 同为裸算子，不含鉴权 | 已落地 |
| `SpaceManager` 改造 | 创建时按 `SpaceSpec.owner` 登记归属主体；成员表由逐成员键改单键（破坏性，须回填）；`update` 增状态机校验；拒绝主体两维同时非空的成员记录；四处索引维护 | 已落地 |
| `types.py` 加字段 | `SpaceMember` 增两轴角色（枚举类型自安全层导入）、`SpaceInfo` 增归属登记、`SpaceSpec` 增创建者身份，另新增空间授权事实快照类型 | 已落地 |
| `Scheduler` 两个实现 | `JobInfo.mode` 优先取 `Job.mode` 声明的演进模式，无声明才回落任务类名；一次性任务、定时注册、到点生成的实例三条路径同口径 | 已落地，取值域变更见 S02 契约变更表 |
| `PermissionManager` | 在上游安全模块合入后退出请求授权路径，只服务兼容测试。授权判定迁至安全层的 `Authorizer` 实现，本层不再承载判定 | 规划中，随上游合入 |

**两轴角色枚举、三张动作矩阵与身份推导比较函数不落本层**，落 `common/security/`：其元素类型取上游 12 值 `Action`，而本层 `types.py` 已有五值 `Action` 且 `Grant.actions` 仍在使用，同一模块内两者无法共存；更根本的是这些常量与函数的消费方跨安全层、API 层与控制层三侧，落本层即安全层反向依赖控制层。本层只消费它们作为成员记录的字段类型。

#### 应用端口（B-03）

| 项 | 内容 | 状态 |
|---|---|---|
| 新增子包 `application/` | 四个非算子应用端口，由已注入的 Engine/Governor/SpaceManager 组成，不执行 PEP、不接收 `identity`。`MemoryCommandService`：write/batch_write/update/delete/evolve；`MemoryQueryService`：recall/list_with_permission_contexts/get 与鉴权元数据读取；`SpaceLifecycleService`：purge + SpaceManager.delete + `deleted_counts` 汇总；`GovernanceService`：inspect/trace/audit。SDK/HTTP/MCP 经 `LocalMemoryAPI` 共用同一组端口。带实现的模块收在子包而非顶层 | 已落地 |

## 数据结构

### 控制层数据类型（`types.py`）

| 类型 | 性质 | 关键字段 |
|------|------|----------|
| `Action` | 安全域兼容再导出 | 与 `common.security.types.Action` 为同一对象；旧 PermissionManager 运行路径仅处理 READ / WRITE / UPDATE / DELETE / SHARE |
| `PermissionContext` | dataclass | resource_type / memory_type / pipeline / unit_id / scope / tags / metadata |
| `Grant` | 安全域兼容再导出 | 与 `common.security.types.Grant` 为同一对象，不另建四字段选择子 |
| `Channel` | 枚举 | HOT / BACKGROUND |
| `WriteTargets` | frozen dataclass | candidates(tuple[Scope]) / fallback(Scope \| None，为 None 即兜底落点不在候选集内，由 PEP 拒绝) |
| `JobStatus` | 枚举 | PENDING / RUNNING / SUCCEEDED / FAILED / CANCELLED |
| `JobInfo` | dataclass | id / channel / mode / scope / status / detail；目标增加 result |
| `MemoryListResult` | dataclass | items: list[MemoryUnit] / count: int（分页前匹配总数） |
| `UpdateMode` | 枚举 | SUPERSEDE（默认，新 id）/ OVERWRITE（同 id） |
| `MemoryPatch` | dataclass | content（修正后的文本投影，应用时更新对应 Segment 内容） / tier / tags / metadata / t_valid / t_invalid / mode(UpdateMode)；目标增加 hierarchy |
| `DeleteMode` | 枚举 | FORGET / ARCHIVE / DOWNWEIGHT / PURGE |
| `DeleteSelector` | dataclass | unit_ids / scope / tags / before / mode(DeleteMode) |
| `PrincipalPath` | 枚举 | USER_AGENT / AGENT_USER |
| `SpaceStatus` | 枚举 | ACTIVE / FROZEN / ARCHIVED / DELETING / DELETED |
| `SpacePolicy` | dataclass | require_space / principal_path / storage_isolation_strategy / retention / quotas / index_profiles / pipeline_profiles |
| `SpaceSpec` | dataclass | org / space / display_name / principal_path / policy / metadata |
| `SpaceInfo` | dataclass | org / space / display_name / status / principal_path / policy / metadata / created_at / archived_at |
| `SpacePatch` | dataclass | display_name / status / principal_path / policy / metadata |
| `SpaceMember` | dataclass | scope / role / content_role / governance_role / created_at / expires_at（两轴角色为判定依据，`role` 保留但判定不再读取） |
| `SpaceUsage` | dataclass | org / space / memory_count / message_count / index_count / storage_bytes / audit_count |
| `SpaceDeleteResult` | dataclass | org / space / deleted_counts / status / audit_event_id |

`DeleteSelector` 条件取 AND 且至少一项非空。`MemoryPatch` 仅非 `None` 字段生效；目标 `HierarchyPatch.edge_changes` 的空列表表示无边变更。

### 三种状态/引用边界（目标）

| 结构 | 允许值或作用 |
|---|---|
| `HierarchyStatus` | 结构节点修正状态；精确枚举见 S07 |
| `LifecycleState` | unit 生命周期；精确枚举见 S07 |
| `provenance` | 演进来源 |
| `supersedes` | 版本替换 |
| `HierarchyRef.parent_id/child_ids` | 直接父子包含 |

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
jiuwen_memory/control/<算子>_impl/
    __init__.py             # import 实现模块，触发自注册
    <impl_class_snake>.py   # 具体实现 + 尾部 @Producer.register("name")
```

自注册模式：Producer 定义在对应顶层接口文件中；实现文件尾部 `@XxxProducer.register("name")` 绑定构建函数，`__init__.py` 导入实现文件触发注册，`control.bootstrap.register_controllers()` 统一 import 各 `*_impl` 包。装配层通过 Producer 按配置选取实现。

## 错误语义（层级目标扩展）

| 异常 | 场景 |
|---|---|
| `ValidationError` | 空 selector、非法 options/patch/span/顺序、跨 kind 组合 |
| `ConflictError` | expected_parent_id 不匹配、并发版本变化或单父冲突 |
| `NotFoundError` | 已鉴权 scope 内的目标或边端点不存在 |
| `PolicyError` | 层级功能关闭、策略非法、ensure 前提不满足 |
| `BackendError` | 层级事务、调度或阻塞 ensure 失败 |

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | 数据面委托 MemoryEngine；治理/授权/调度/策略/space 管理面直达控制算子 |
| S04-retrieval | Retriever 检索链路 |
| S05-construction | Engine/Scheduler 驱动构建层 IndexBuilder/Evolver；演进逻辑由构建层执行 |
| S06-storage | 控制层经 IndexBuilder 写正排与派生索引，真源读写直连注入的 KVStore 端口（点读 `load_units` / 列表 `list_units` / 枚举 `kv.scopes()`，Governor 同为 KVStore 点读）；LifecycleManager/Governor 的目标操作按显式 Scope 点查或枚举，只有 sweep/offboarding 这类全局管理任务跨 Scope 枚举 |
| S07-common | 控制层消费 `MemoryUnit`、`AuditEvent`、错误类型等公共结构 |
| F07-collective-memory | 两轴角色、归属登记、空间授权事实快照落本层类型；`MembershipResolver` 是本层新增算子，`SpaceManager` 增 `spaces_for` 反查；授权判定迁出本层 |
| architecture.md §3.1 | MemoryUnit 数据模型（lifecycle / temporal / supersedes / provenance）由 `common/type_def` 定义，控制层消费 |
| architecture.md §8 | 演进调度（EvolveMode / Channel）映射到 Scheduler 双通道 + Evolver 四阶段 |
| architecture.md §9 | `jiuwen_memory/api/MemoryAPI` 是控制层的薄封装 + PEP；数据面委托 Engine，管理面直达各算子 |
| architecture.md §12 | 横切可观测/治理——Governor.audit 消费 `common/audit/AuditLogger` 记录的审计事件 |
| architecture.md §13.4 | PolicyManager 是少量已知策略键的 admin 落点；六类动态配置见 S08 ConfigSource |
| S08-config | ConfigSource 与 PolicyManager 分工 |
