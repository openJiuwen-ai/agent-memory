# S02 — 记忆接口层（Memory API Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/api/ |
| 最近一次修订日期 | 2026-08-29 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/api/F01-memory-api-impl-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/api/F03-batch-write-api.md，docs/features/api/F04-memory-metadata-separation.md，docs/features/F01-system-spec-design.md，docs/features/construction/F02-dynamic-extraction-consolidation.md，docs/features/construction/F04-cc-memory-compat.md，docs/features/construction/F05-construction-spec-multimodal-design.md，docs/features/common/F01-memory-layer.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F05-security-api-contracts.md，docs/features/common/F08-memory-tree.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/control/F04-permission-context-routing.md，docs/features/control/F05-cloud-engine-design.md，docs/features/config/F01-config-source.md，docs/features/control/F07-collective-memory-design.md |

## 文档分工

对外接口相关内容分四处维护，互不替代。

| 位置 | 记什么 |
|---|---|
| [architecture.md §6](../design/architecture.md) | **已实现**接口的清单（方法 / 语义 / 入参 / 出参） |
| `jiuwen_memory/api/` | **已实现**接口的代码（`memory_api.py` 契约，`memory_api_impl/` 实现） |
| 本文（S02） | 对外接口的详细介绍与用法：含已实现，以及已设计但尚未实现的增量；尚未实现处用 **状态：已设计、尚未实现** 标明。方法总览同时指向对应特性文档 |
| `docs/features/api/`（F01–F04） | 特性决策（为什么这样改）；不重复罗列全部方法签名。各 F 覆盖哪些方法见下文方法总览与特性文档对照 |

已设计但尚未实现的接口完成代码开发上库时：去掉本文（及受影响 F 文档）中的尚未实现标注，并把该方法（或增量入参）补进 architecture.md §6。

## Metadata 公共 API 契约

`add` / `add_async` / `batch_add` 以及 `BatchWriteItem` 分别接收
`system_metadata` 和 `user_metadata`，不再接收混合 `metadata`。`MemoryPatch` 对两个
dict 分别做 merge-update。用户过滤的规范路径为 `user_metadata.<key>`；裸自定义
字段仅在规范化边界作为该路径的兼容写法，`metadata.<key>` 拒绝。

群体记忆的内核字段（作者标记、判定命中的类别名、判定标签键）全部落 `system_metadata`：
它们由内核写入、内核解释并参与判定与检索谓词，谓词路径为 `system_metadata.<key>`。
`system_metadata` 同时是对外入参，因此写入与改写入口拒绝调用方占用这些键
（`KERNEL_SYSTEM_METADATA_KEYS` 与判定表解析出的标签键集合），见 F07「写入边界校验」。

## 范围 / 边界

**管什么**：
- 统一对外 Core API（形态无关）：所有接入形态（SDK/CLI/Skill/MCP/HTTP·gRPC）最终映射到 `MemoryAPI`
- 鉴权执行点（PEP）：从请求安全上下文取 actor，调用 `PermissionManager.check(identity, scope, action)` 做入口鉴权
- 入口审计：写审计事件到 `AuditLogger`
- 参数装配：将调用侧参数装配为控制层可消费的内部结构
- 鉴权驱动的编排：按 `identity` 决定资源可见范围、生成并回注系统谓词、按判权结果决定取数范围。不变量 9（授权路由值回注查询）与 12（分页命中后逐条 READ 鉴权）是这一职责的两处既有形态；跨空间检索的候选空间枚举、逐空间判权与逐空间系统谓词的生成同属此列。**范围以「判权本身及其直接输入输出」为限**，其余属机械计算或 I/O 编排，落控制层 `control/collective/`，本层只提供回调：

  | 机械部分 | 落点 | 本层提供 |
  |---|---|---|
  | 写入候选空间的渲染、排序与上限截断 | `collective/write_targets.py` | `can_write` 判权回调 |
  | 跨空间的取数上界摊配、召回扇出与结果合并 | `collective/cross_space_recall.py` | `recall` 回调、已判权的空间目标与逐空间谓词 |
- 同步/异步桥接：为同步形态桥接引擎异步协程

**不管什么**：
- 不做业务编排逻辑（全部委托 `jiuwen_memory/control`）——指内容如何抽取、演进、索引这类加工编排；上条所述的鉴权驱动编排不在此列，它是 PEP 职责的延伸。判据是「移出本层是否还能按 `identity` 裁决」：逐空间判权循环移出即 PEP 分裂为两处，故留本层；候选集合的计算与跨空间的召回扇出都不读 `identity`——前者在判权之前、后者在候选集与谓词都已定妥之后，故不留。逐空间构造 `RetrievalQuery` 的循环本身就是取数编排，随扇出一并移出
- 不直接操作存储
- 不调用 LLM / 构建 / 检索。此处「调用」指调用该层的**算子**——有 Producer 注册、实现可替换、访问模型或存储的组件（`Router` / `Evolver` / `Classifier` / `IndexBuilder` 等）。引用该层导出的类型与无状态纯函数不在此列（见 `construction/router.py` 模块文档：「API 层引用构建层类型是既有形态，依赖方向为 API → 构建，无环」）。归属判定算子的调用落在控制层的 `control/collective/routing.py`，本层调控制层
- 不做 admin 策略存储（直达 PolicyManager）

## 不变量

1. **本层是薄封装 + PEP**：数据面委托 `MemoryEngine`，治理面委托 `Governor`，授权面委托 `PermissionManager`，调度面委托 `Scheduler`，策略面直达 `PolicyManager`。API 不实现跨层业务编排，真实处理逻辑由 control 层算子及其下游 construction/retrieval/storage 完成。
2. **调用方身份不下沉**：鉴权通过后只透传已鉴权的 target `scope`，`security` 及其中的 actor 不传入控制层。
3. **`security` 为必填安全输入**：类型为 `common.security.types.RequestSecurityContext`，由接入层的 `authenticated()` 产出，调用方不得自行拼装；actor 从 `security.auth.actor` 取。除 `check_write` 为兼容原第二位置参数而保留 `(scope, security, *, ...)` 外，其余方法均为 keyword-only。
4. **search 参数拆分**：`context: Context` 在本层边界拆开——`context.scope` 作独立轴穿透，`context.extensions` 写入调用级 options；约定 key `context.extensions["max_tokens"]` 由 API 边界解析为 int 后写入 `RetrievalQuery.max_tokens`，并从透传 extensions 中移除；`Context` 对象本身不进控制层。
5. **admin 不经 Engine**：admin_get/set/all 直达 PolicyManager。
6. **管理面闸门 = 根 scope**：无具体 target scope 的方法（admin_*、全局 audit）以根 scope `Scope()` 为鉴权目标——「能对根 scope 行权」即管理员闸门；租户数据/治理方法仍按各自 target scope 鉴权。
7. **`as_of` = valid-time 回溯点**：`search`/`get` 的 `as_of` 沿系统相信时间轴回溯，返回「那时被认为有效」的版本（`get` 沿 `supersedes` 版本链定位）；`None` 表示当前态。
8. **target scope 兜底**：`delete` 等以 `selector.scope or 根 scope` 为鉴权目标——未限定 scope 的跨范围操作退到根闸门，要求更高权限。
9. **路由鉴权绑定数据范围**：路由型 PermissionManager 依据请求中的字段选择策略时，
   API 必须把同一个授权路由值作为系统过滤谓词回注查询，避免「按 A 类型授权、读取
   B 类型数据」。系统谓词与用户 `filters` 以外层 `AND` 合并。
10. **space 是租户隔离单元**：`Scope.space` 参与鉴权、存储命名空间、索引过滤和审计 actor/target 过滤；`scope.require_space=true` 时，具体 target scope 缺少 `space` 的数据/治理操作在 API 层拒绝。org 级 `create_space/list_spaces` 使用 `Scope(org=...)` 做管理面鉴权，不受该策略拦截。
11. **space policy 在 API 边界生效**：已创建 space 的 `principal_path` 由 `SpaceManager.get_policy` 提供，API 在调用 `PermissionManager.check` 前写入 `PermissionContext.metadata["principal_path"]`；调用级 metadata 不能覆盖 space policy。
12. **list 按实际资源二次鉴权**：请求显式给出的 `memory_types` 先做类型级鉴权；Engine 再以当前分页实际命中的 MemoryUnit 真源元数据返回权限上下文，API 逐条 READ 鉴权，全部通过后才返回内容。参与权限路由的 extensions 值必须作为系统过滤条件回注。
13. **list 过滤和计数在 KV 内完成**：API 复制 `extensions`、规范化 `filters` 后完整下推；返回 `MemoryListResult.items` 当前页和分页前精确 `count`，不以 `len(items)` 代替总数。
14. **六类动态配置不走业务入参**：能力开关、prompt 全文、LLM/Embedder/Reranker 的 model/api_key/url、Store 连接或 `*.active` 等由 `ConfigSource.fetch` 提供（见 S08）；`add`/`search`/`evolve`/`list` 不得把上述值解释为配置写入。调用侧可传 prompt **key**、`memory_type`/pipeline 等业务选择子。
15. **安全输入唯一且不可自造**：`security` 只能来自受控构造入口——接入形态经 `bootstrap.core.auth_middleware.authenticated()`，进程内直连经 `common.security.request_context.internal_context(authenticator)`。请求 payload 不得声明 actor / request_id / surface。过渡期 `common.security.legacy.legacy_request_context()` 是唯一例外（见 F05 §PR2），随实装 PR 一并删除。
16. **授权面使用安全域授权类型**：`grant`/`revoke` 的公共类型是 `common.security.types.Grant` / `Action`；目标形态下 `grant_id` 由服务端生成、`revoke` 按 `grant_id` 精确定位。接口先行过渡期只固定签名，`GrantStore` 未实装前不生成 ID、不据 ID 判定，撤销语义与 `mem2.0` 一致（见 F05 §5.4）。
17. **层级能力默认关闭（目标）**：普通 `add` 默认不建父树；只由显式 `evolve(..., mode=HIERARCHY, hierarchy_options=...)` 或启用的后台策略触发。显式层级请求在 `hierarchy.enabled=false` 时抛 `PolicyError`，不带层级参数的既有操作保持语义。
18. **三类遍历严格分离（目标）**：`trace` 只沿 `provenance`；树下钻由 `search(..., expand_depth>0)` 沿 `HierarchyRef` 完成；`get(as_of)` 只沿 `supersedes`/valid-time；L0/L1/L2 仅表示同一 unit 的披露层。
19. **API 与 Control 的职责边界**：API 只负责协议边界工作——输入形状和兼容参数校验、请求对象装配、`security.auth.actor`/target `scope` 的 PEP 鉴权、权限路由过滤回注、入口审计以及同步/异步桥接。API 不得调用 LLM、Extractor、Classifier、IndexBuilder、Retriever 或 Store，也不得实现写入、去重、版本、生命周期、检索排序和后台任务编排。
20. **委托对象按职责分流**：数据面 add/search/list/get/update/delete/evolve 委托 `MemoryEngine`；治理操作委托 `Governor`；任务状态和取消委托 `Scheduler`/`IngestJobController`；跨 scope 授权在过渡期委托 `PermissionManager`，目标切到 `Authorizer` / `GrantStore`；策略读写委托 `PolicyManager`；space 管理委托 `SpaceManager`。这些是控制算子的直接委托，不属于 API 自行实现业务逻辑。
21. **允许的 API 协调例外**：`delete_space` 可以在鉴权后先调用 `MemoryEngine.purge_space` 清理记忆，再调用 `SpaceManager.delete` 清理 space 元数据；该方法只负责跨控制算子的事务顺序和结果汇总，不得实现 purge、索引删除或存储遍历本身。
22. **业务逻辑下沉可验证**：新增数据面语义时，API 侧只增加契约校验/参数装配/授权映射，具体行为必须在 `MemoryEngine` 或对应 Control/Construction/Retrieval 算子中实现。API 单测应使用 spy/mock 验证委托，Control 单测应覆盖真实行为，禁止只在 API 单测中覆盖业务分支。

## 接口契约

`MemoryAPI` 定义在 `jiuwen_memory/api/memory_api.py`。下列各节按委托面划分；方法总览标明状态与详细介绍所在小节。文档明确**不对外暴露**的能力（`link`、独立 `MemoryAPI.expand`）不列入本契约。

### MemoryAPI 方法总览

| 分面 | 方法 | 状态 | 详细介绍 | 特性文档 |
|---|---|---|---|---|
| 数据面（委托 MemoryEngine） | `add` | 已实现 | [add / add_async](#add--add_async) | F01、F02、F04 |
| 数据面 | `add_async` | 已实现 | [add / add_async](#add--add_async) | F01、F02、F04 |
| 数据面 | `batch_add` | 已实现 | [batch_add / batch_add_async](#batch_add--batch_add_async) | F03、F04 |
| 数据面 | `batch_add_async` | 已实现 | [batch_add / batch_add_async](#batch_add--batch_add_async) | F03、F04 |
| 数据面 | `check_write` | 已实现 | [check_write](#check_write) | 无独立 F；实现见 F01 PEP |
| 数据面 | `search` | 已实现；层级增量参数尚未实现 | [search](#search) | F01、F04；层级见 F08 |
| 数据面 | `list` | 已实现 | [list](#list) | F01、F04 |
| 数据面 | `get` | 已实现 | [get](#get) | 无独立 F；实现见 F01 |
| 数据面 | `update` | 已实现；`MemoryPatch.hierarchy` 尚未实现 | [update](#update) | F01、F04；层级见 F08 |
| 数据面 | `delete` | 已实现 | [delete](#delete) | 无独立 F；实现见 F01 |
| 数据面 | `evolve` | 已实现 EXTRACT/ASSOCIATE/CONSOLIDATE/FORGET；`HIERARCHY` 尚未实现 | [evolve](#evolve) | F01、F02；层级见 F08 |
| 任务面（委托 Scheduler） | `job_status` | 已实现 | [job_status](#job_status) | F01 |
| 任务面 | `job_cancel` | 已实现 | [job_cancel](#job_cancel) | F01 |
| 治理面（委托 Governor） | `inspect` | 已实现 | [inspect](#inspect) | F01 |
| 治理面 | `trace` | 已实现 | [trace](#trace) | F01 |
| 治理面 | `audit` | 已实现 | [audit](#audit) | F01 |
| 授权面（委托 PermissionManager） | `grant` | 已实现 | [grant](#grant) | F01 |
| 授权面 | `revoke` | 已实现 | [revoke](#revoke) | F01 |
| Space 管理面（委托 SpaceManager） | `create_space` | 已实现 | [create_space](#create_space) | F01 |
| Space 管理面 | `get_space` | 已实现 | [get_space](#get_space) | F01 |
| Space 管理面 | `list_spaces` | 已实现 | [list_spaces](#list_spaces) | F01 |
| Space 管理面 | `update_space` | 已实现 | [update_space](#update_space) | F01 |
| Space 管理面 | `archive_space` | 已实现 | [archive_space](#archive_space) | F01 |
| Space 管理面 | `delete_space` | 已实现；仅 `PURGE`。`FORGET`/`ARCHIVE`/`DOWNWEIGHT` 尚未实现 | [delete_space](#delete_space) | F01 |
| Space 管理面 | `export_space` | 已实现 | [export_space](#export_space) | F01 |
| Space 管理面 | `space_usage` | 已实现；`index_count` / `audit_count` 尚未实现 | [space_usage](#space_usage) | F01；缺口见 S03 |
| Space 管理面 | `get_space_policy` | 已实现 | [get_space_policy](#get_space_policy) | F01 |
| Space 管理面 | `set_space_policy` | 已实现 | [set_space_policy](#set_space_policy) | F01 |
| Space 管理面 | `list_space_members` | 已实现 | [list_space_members](#list_space_members) | F01 |
| Space 管理面 | `add_space_member` | 已实现 | [add_space_member](#add_space_member) | F01 |
| Space 管理面 | `remove_space_member` | 已实现 | [remove_space_member](#remove_space_member) | F01 |
| 运行时策略面（直达 PolicyManager） | `admin_get` | 已实现 | [admin_get](#admin_get) | F01 |
| 运行时策略面 | `admin_set` | 已实现 | [admin_set](#admin_set) | F01 |
| 运行时策略面 | `admin_all` | 已实现 | [admin_all](#admin_all) | F01 |

---

### 群体记忆带来的契约变更（F07）

契约细节、判定规则与决策取舍见 [F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md)。「已落地」指内核已实现；接入层改造另计。

| 入口 | 变更 | 兼容性 | 状态 |
|---|---|---|---|
| `add` / `add_async` | 签名不变。`scope` 仍必填、语义不变；`system_metadata["coords"]` 在参数袋里即表示落点交由判定 | 向后兼容，不带该键的调用方行为不变 | 已落地 |
| `batch_add` / `batch_add_async` | 签名不变，批级 `scope` 仍为缺省值（逐项自带即可整批省略）。批级参数袋带 `coords` 键即转入归属判定；坐标取自批级 `system_metadata["coords"]`，逐项携带即拒绝 | 向后兼容 | 已落地 |
| `search` | 签名不变。归属坐标经 `Context.extensions["coords"]` 传入，供收窄维谓词取值；`Context.extensions["spaces"]` 在参数袋里即转为跨空间检索 | 向后兼容，不带这两个键的调用方行为不变 | 已落地 |
| `RecallChannel` | 新增取值 `space`，标记跨空间检索里某个空间整体召回失败 | 向后兼容；穷举该枚举的调用方须容纳新取值 | 已落地 |
| `delete` | 入参 `DeleteSelector` 新增 `filters`，空选择器判据随之纳入该字段；实体删除后的跨空间清理由接入方经本入口逐个执行 | 向后兼容 | 已落地 |
| `list` | 不读归属坐标，但注入第一族系统谓词 | 行为变更：个体空间内不再返回不可见条目 | 已落地 |
| `list_spaces` | `cursor` 标记废弃；`limit` 由每页条数改为返回条数上限 | 签名不变，翻页语义变更 | 已落地 |
| `create_space` | 签名不变；入参 `SpaceSpec` 新增 `owner` 字段，供开通服务声明归属主体或显式不登记 | 向后兼容 | 已落地 |
| `job_status` / `job_cancel` | 签名不变；返回值 `JobInfo.mode` 的取值域由任务类名改为演进模式（`EvolveMode` 的值），无演进模式的任务仍回落类名 | 行为变更，不受两个开关约束：鉴权点按该取值决定任务入口取哪个动作，类名区分不了遗忘与抽取 | 已落地 |

`scope` 语义不变：它就是落点，且保持必填。落点也可以不由调用方指定、改由归属判定选择，这时在写入侧参数袋里放 `system_metadata["coords"]` 请求判定，候选空间集合由归属坐标推出、落点由判定算子在集内选择。分流判据是该键在不在，与 `scope` 的取值形态无关——键由调用方主动放入，而 `space` 为空是调用方什么都没表达时的取值，拿它触发另一条路径等于由内核解读缺省状态。`space` 非空也不否决判定请求：上游网关按自己的租户或应用标识填 `space` 是常见形态，那个取值不是本系统的空间标识，以它否决则这类接入方无路可走（直写要求空间已登记，未登记即判权拒绝）。给了 `coords` 即交出落点决定权，`scope.space` 在这条路径上不参与落点计算，真实落点由返回的记忆单元携带。两处都没有声明时拒绝，替换的是同一形态下原本含义模糊的判权拒绝。走判定路径时入参 `scope` 的主体维与 `org` 仍须与身份一致，不静默丢弃。条目的可读范围由所在空间的权限决定，写入侧不设条目级的可见性声明入参（F07）。

**归属坐标不占形参，经参数袋传入**：写入侧 `system_metadata["coords"]`，检索侧 `Context.extensions["coords"]`，取值为 `dict[str, str]`。该键在 API 层入口即被取出，不落盘、不进鉴权入参、不随 options 透传给自定义检索模块。检索侧的取出以装配了判定表为条件：未装配的部署里本特性整体不可达，该键不产生任何收窄谓词，取出它只会让自定义检索模块少收到一个调用方明确传入的字段。取值不受 `MetadataValueType` 约束（该联合类型不含 `dict`），与 `route_ctx` 承载判定上下文对象同例；判型改在运行期做。由此，六个入口的形参列表与本特性之前逐字一致。身份入参不新增：随上游安全模块交付的 `security` 是唯一可信身份来源。

**跨空间检索不另设入口，同样经参数袋分流**：`Context.extensions["spaces"]` 键在即跨空间，取值 `list[str]`，空列表表示「调用方可读的全部空间」。跨空间不是新的检索算法，是在单空间召回之上套的一层编排（候选空间 → 逐空间判权 → 按上界分配取数 → 逐空间召回 → 轮转合并），两族谓词与召回复用同一份实现；拆成两个入口即同一件事有两处契约，接入方须先判断部署形态才知道该调哪个，且两处一旦分叉就出现「一个入口挂了谓词、另一个没挂」的绕过通道。判据同样取键的有无：取值判空则「查我能读的全部空间」这层意图只能靠缺省状态表达，与「没打算跨空间」不可区分。取值为 `None` 按非法拒绝，不当作空列表——网关把未填字段序列化成 `null` 时若按空列表处置，一次本意为单空间的检索会静默扩到全部可读空间。

跨空间形态下 `search` 有三处差异，其余与单空间路径一致：

| 项 | 差异 |
|---|---|
| `context.scope` | 只取 `org` 维定组织边界，空间维由候选集给出、传了不生效 |
| 无权的候选空间 | 逐个剔除并记入 `RetrievalResult.errors`（`ChannelError`，`channel=space`、`error_type=PermissionDeniedError`）；候选集非空而一个都读不到时抛 `PermissionDeniedError`，与单空间路径同一处置。候选集为空是合法空结果，不抛 |
| 时延 | 随候选空间数线性增长，上界由 `space.fanout_limit` 约束 |

未装配判定算子（未声明 `router` 配置命名空间）时，上述写入侧变更全部不可达：判定表为空、`coords` 键不产生落点、`space` 为空照旧直写，全链路行为与改造前一致。装配条件是分流判据的一半，缺它则未装配部署里以空 `space` 为落点的既有调用会撞上「写入落点未声明」。这是可灰度上线的前提。

---

### 数据面（委托 MemoryEngine）

#### add / add_async

**状态：已实现**

```python
def add(
    content: str,
    scope: Scope,
    source: Modality = Modality.TEXT,
    *,
    security: RequestSecurityContext,
    assets: list[str] | None = None,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
) -> list[MemoryUnit]: ...

async def add_async(...) -> list[MemoryUnit]: ...  # 签名同 add
```

同步写入：鉴权 WRITE→委托 Engine→阻塞至 hot path 完成。infer/procedural 触发时返回 `created_ids` 对应的派生单元（可空），否则返回原始单元。`add_async` 为异步写入：直通 Engine 协程，供事件循环形态使用。

##### infer 开关（add 的同步抽取语义）

`add` 的 `system_metadata["infer"]` 是调用级开关，控制写入时是否同步抽取派生记忆（对齐 mem0 `add(infer=True)`）：

- **真值判定**：`str(system_metadata.get("infer", "")).strip().lower() == "true"`——大小写/
  空白不敏感，字符串 `"true"` 和布尔值 `True` 均会触发；`"false"`/`False`/缺省/
  空值走默认路径。
- **`infer="true"`**：原始记忆落 `/messages/{id}` 真源但**不建索引**；hot path
  同步走 `Engine → Evolver.evolve(units, EXTRACT)`。`OrchestratingEvolver`
  以 `_dedup_batch` 完成判定与落盘，`DynamicEvolver` 走
  extract → consolidate（只判定）→ reflect → 落盘；**不提交** background EXTRACT。
  Engine 从 `EvolveResult.created_ids` 反查并返回派生单元，因此 ADD/SUPERSEDE
  返回新派生单元，只有 UPDATE/NOOP 时合法返回空列表。
- **缺省 / 非 `"true"`（infer=false）**：原文经 `classifier.classify` 打 tier+tags（纯 LLM 抽取 episodic/semantic/procedural + 1-3 个 tags）→ 落盘 `/memory/{id}` + 建索引。classifier 未注入时跳过（tier 保持 EPISODIC 默认，向后兼容）。返回原始单元列表。
- **evolver 缺失**：`infer="true"` 但装配未注入 `Evolver` 时 Engine 抛 `RuntimeError`——装配问题暴露而非静默降级。默认装配 `evolver: orchestrating` 总是注入。

##### procedural 开关（add 的过程记忆抽取）

`add` 的 `system_metadata["procedural"]` 是独立于 infer 的调用级开关（详见 F02 决策8）：

- **`procedural="true"`**：原文**不落 KV**；喂 `Evolver.evolve(units, EXTRACT)`。extractor 把本轮汇总成一条 PROCEDURAL 执行历史，再由 Evolver 落盘（`DynamicEvolver` 也走父类 procedural 路径，不判定）。
- procedural 与 infer 同传时按 procedural 语义：原文不落 `/messages/`、不收集
  context、不去重。语义是"把这轮做了什么记成一条可检索 how-to"。

##### 动态抽取与巩固 prompt

- metadata 的公共类型是 `dict[str, Any]`；动态 prompt 控制项的值按
  `str(value).strip()` 解释为 prompt key。调用方使用
  `system_metadata["_extract_prompt_<strategy>"] = prompt_key` 或 `system_metadata.update(...)`
  传值；不存在 `metadata.append()`。
- 在 `add` 的普通同步抽取路径中，`_extract_prompt_<strategy>` 随 `infer=true`
  进入 EXTRACT；procedural 或显式 `evolve(EXTRACT)` 同样进入 Evolver 的 EXTRACT
  路径，Extractor 本身不校验 infer。支持任意非空 strategy，每个策略调用一次；
  无动态 prompt 时回退旧 Extractor。
- `_consolidation_prompt_<strategy>` 为落盘前动态巩固 prompt 的 **key**（引用 yml `prompts` 段的命名 prompt）。运行时由 `PromptRegistry` 按 `phase=consolidate + key` 查真实文本。`DynamicEvolver` 消费；无 prompt 或输出不合法时回退规则判定（高相似度 NOOP，否则 ADD）。
- `_reflect_prompt_<strategy>` 为反思步 prompt 的 key（同上，`phase=reflect`）。reflect 默认 no-op，子类可覆盖 `_reflect_step`。
- LLM 输出格式由 prompt 自身约定，内核不追加固定 schema。

##### infer 上下文增强与 KV 前缀分离（增量）

- **infer=true 时 evolver 内部收集上下文**（evolve 接口不变）：`recent_originals`（最近 10 条 infer 原文，做指代消解/语境，不参与去重）+ `related_memories`（`dedup.recall` 召回 10 条相关记忆，做去重提示）。两类参考项只拼进 extractor prompt，不进提取来源；最终判定由所选 Evolver 完成：`OrchestratingEvolver` 走 `_dedup_batch`，`DynamicEvolver` 走 consolidate → reflect → 落盘。详见 F02 决策7。
- **KV key 前缀分离**：真源 key 按「是否建索引」带前缀——`/memory/{id}`（建索引记忆）、`/messages/{id}`（未建索引 infer 原文）。前缀常量与 helper 在 `common.type_def.memory`/`raw`。详见 F02 决策6。
- **engine.write infer=false 调 classify**：默认路径调 `classifier.classify` 给原文打 tier+tags（纯 LLM 抽取 episodic/semantic/procedural + tags）；infer=true 不经 classifier（extractor 产派生时自定）。详见 F02 决策9。
- **`/v1/list` 收窄并上收为 API 契约**：handler `_list` 委托
  `MemoryAPI.list(scope, security=..., offset, limit, memory_types, extensions, filters)`；
  `KVStore.list` 只查询 `/memory/` 记忆并返回当前页与分页前总数。详见 F02 决策10与
  F01 的 list 决策。

> 开关由来与"为何默认不同步、为何经 Evolver 而非独立 Extractor"的取舍见 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)；add 路径流程见 [`S03-control.md`](S03-control.md)。

#### batch_add / batch_add_async

**状态：已实现**

```python
def batch_add(
    items: list[BatchWriteItem],
    scope: Scope | None = None,
    source: Modality = Modality.TEXT,
    *,
    security: RequestSecurityContext,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
    stream_id: str = "",
    continue_on_error: bool = True,
) -> BatchWriteResult: ...

async def batch_add_async(...) -> BatchWriteResult: ...  # 签名同 batch_add
```

同步桥接批量写入；逐项归一化、WRITE 鉴权、space 校验与审计，结果始终按输入索引对齐。`batch_add_async` 串行保序批量写入；默认归集单项错误，`continue_on_error=False` 时后续项为 `Skipped`。顶层 `system_metadata` / `user_metadata` 作批次默认值，单项可覆盖。设计取舍见 [`F03-batch-write-api.md`](../features/api/F03-batch-write-api.md)。

#### check_write

**状态：已实现**

```python
def check_write(
    scope: Scope,
    security: RequestSecurityContext,
    *,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
) -> None: ...
```

Pre-flight WRITE 鉴权，不落盘。用于长耗时摄入任务入队前拒绝无权限请求，避免 DoS（队列被无权限请求占满）。镜像 `add` 的鉴权路径与 space 可写校验，但不调 `engine.write`。后台实际写入仍保留一次鉴权作防御层。

#### search

**状态：已实现**（下列签名为当前代码）。层级过滤 / 展开 / rollup 为已设计增量，见本节末。
#### 薄封装职责

API 方法的实现顺序应保持为：

```text
transport input
  → 类型/形状校验与兼容归一化
  → 构造 PermissionContext / 内部请求对象
  → PermissionManager.check + 审计
  → 委托 Control 算子
  → 结果适配与审计
```

API 可以复制可变参数、规范化过滤表达式、把 `Context` 拆为 `scope` 和 `RetrievalQuery`，也可以将授权路由值合并为系统过滤条件；这些属于边界适配和安全约束，不属于检索或写入算法。

API 不得在上述流程中自行执行以下逻辑：

- 记忆抽取、分类、去重、巩固和索引构建；
- 检索通道选择、召回、融合、重排和披露预算计算；
- MemoryUnit 版本、生命周期或 Storage CRUD；
- 后台任务拆分、并发策略、重试和调度实现。

管理面直接委托 `PolicyManager`、`PermissionManager`、`SpaceManager` 等 Control 算子是允许的；API 仍只承担鉴权、参数装配、调用顺序和结果适配。

### search

```python
def search(
    query: str,
    context: Context,
    *,
    security: RequestSecurityContext,
    filters: FilterExpr | list[FilterClause] | dict | None = None,
    as_of: datetime | None = None,
    top_k: int = 10,
    disclosure: DisclosureLevel = DisclosureLevel.L0,
    with_trajectory: bool = False,
) -> RetrievalResult: ...
```

混合检索：鉴权 READ→拆 Context→装配 RetrievalQuery→委托 Engine。`filters/as_of/top_k/disclosure/with_trajectory` 和 `context.extensions["max_tokens"]` 的既有处理不变。

**状态：已设计、尚未实现**（层级增量参数；不另设公开 `MemoryAPI.expand`）

```python
# 目标增量，尚未出现在 MemoryAPI.search 签名中
hierarchy_kind: HierarchyKind | None = None
hierarchy_role: HierarchyRole | None = None
span_start: datetime | None = None
span_end: datetime | None = None
expand_depth: int = 0
rollup: bool = False
```

新增参数原样装配到 `RetrievalQuery`。`expand_depth=0`、`rollup=false` 保证默认只返回直接召回命中的节点，不展开后代、不把后代分数上卷；调用方通过 `hierarchy_role` 指定父侧角色时，即形成父节点优先召回。省略 role 时，同 kind 下所有活动角色均可参与召回。span 是 `HierarchyRef` 结构区间，不替代查询文本解析得到的 event-time。

`expand_depth>0` 时，Retriever 在同一次 search 内沿命中父节点展开子证据（单 kind）；**不另设公开 `MemoryAPI.expand`**。展开选子与父命中共用既有 `max_tokens` 上下文预算，不另设独立预算参数。

校验和闭区间相交语义以 [S04-retrieval.md](S04-retrieval.md) 为准。任一显式 hierarchy 参数、非零展开深度或 rollup 都构成层级请求；功能关闭时抛 `PolicyError`。普通 search 不因 hierarchy 关闭而失败。

#### list

**状态：已实现**

```python
def list(
    scope: Scope,
    *,
    security: RequestSecurityContext,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    extensions: dict[str, Any] | None = None,
    filters: FilterExpr | list[FilterClause] | dict | None = None,
) -> MemoryListResult: ...
```

列出已建索引记忆：支持类型/FilterExpr 过滤、自定义参数透传和分页前精确总数；只返回 `/memory/` 真源记录。

`list` 的 `memory_types` 用于数据过滤，也参与权限路由：显式传一个或多个类型时，API 层为每个
类型分别构造 `PermissionContext(memory_type=<type>)` 并逐个执行 READ 鉴权；未传类型时先按
普通 `resource_type="memory_list"` 鉴权。两种路径随后都调用
`MemoryEngine.list_with_permission_contexts`，一次取得当前页、分页前总数及 MemoryUnit 真源
`memory_type/pipeline/tags/metadata` 权限上下文并逐条二次鉴权，避免未传过滤条件时落入
宽松 fallback，也避免权限上下文与内容来自两次分页读取。

`extensions` 为 `dict[str, Any]`，API 只复制外层字典并原样透传值；内核不得隐式调用
`str()`。只有明确声明需要落盘或跨进程传输的扩展，才由对应边界 adapter 使用显式 codec
序列化；未知 key 原样透传。
`filters` 与 search 共用 FilterExpr/旧 list/dict DSL 规范化语义，`memory_types` 与 filters
取 AND。`org/space/user/agent/session` 属于 Scope 隔离轴，不得出现在 filters。

#### get

**状态：已实现**

```python
def get(
    unit_id: str,
    scope: Scope,
    *,
    security: RequestSecurityContext,
    as_of: datetime | None = None,
) -> MemoryUnit: ...
```

真源点读：鉴权 READ→委托 Engine。`as_of` 为空时返回该 id 对应的那一条；非空时沿 `supersedes` 版本链回溯，返回 valid 区间含 `as_of` 的那一版。不存在时抛 `NotFoundError`。

#### update

**状态：已实现**（下列 `MemoryPatch` 字段为当前代码）。`hierarchy` 为已设计增量。

```python
def update(
    unit_id: str,
    scope: Scope,
    patch: MemoryPatch,
    *,
    security: RequestSecurityContext,
) -> MemoryUnit: ...
```

修正记忆：鉴权 UPDATE→委托 Engine。`MemoryPatch` 仅非 None 字段生效：`content` / `tier` / `tags`（整体替换）/ `system_metadata`（合并）/ `user_metadata`（合并）/ `t_valid` / `t_invalid` / `mode`。

**状态：已设计、尚未实现**（`MemoryPatch.hierarchy`）

`MemoryPatch` 的既有非空字段为 `content/tier/tags/system_metadata/user_metadata/t_valid/t_invalid/mode`，目标增加：

```python
hierarchy: HierarchyPatch | None = None
```

`HierarchyPatch` 的精确类型由 S03 定义。它只允许修改指定 kind 的结构状态、span、
受控子边和稳定顺序；不得接受未校验的完整 `HierarchyRef`、裸 `parent_id` 或任意
`child_ids` 覆写。API 仅做形状校验，Engine 必须按 S03 验证同 org+space、无环、单父、
双向一致和稳定顺序后原子应用。

`SUPERSEDE` 若目标已挂树，Engine 必须把结构位置从旧 id 一致迁移到新版本 id，再把旧版本设为 `SUPERSEDED`；不得留下指向旧版本的活动结构边。`OVERWRITE` 保持 id，但层级 patch 仍须经过相同校验。

#### delete

**状态：已实现**

```python
def delete(selector: DeleteSelector, *, security: RequestSecurityContext) -> list[str]: ...
```

删除/归档/降权：鉴权 DELETE→委托 Engine。按选择器删除，条件取「与」，至少一项：`unit_ids` / `scope` / `tags` / `before` / `mode`。返回命中 id。未给 `selector.scope` 时鉴权退到根 scope。

#### evolve

**状态：已实现**（`EXTRACT` / `ASSOCIATE` / `CONSOLIDATE` / `FORGET`）。`HIERARCHY` 与 `hierarchy_options` 为已设计增量。

```python
def evolve(
    scope: Scope,
    mode: EvolveMode,
    channel: Channel = Channel.BACKGROUND,
    *,
    security: RequestSecurityContext,
) -> str: ...
```

触发演进：鉴权 WRITE→委托 Engine→返回 job_id。当前代码只接受 EXTRACT/ASSOCIATE/CONSOLIDATE/FORGET。调用成功返回 job id，不表示任务已经完成。索引维护不在此（随数据面自动跟进）。

**状态：已设计、尚未实现**（`EvolveMode.HIERARCHY` 与 `hierarchy_options`）

```python
def evolve(
    scope: Scope,
    mode: EvolveMode,
    channel: Channel = Channel.BACKGROUND,
    *,
    security: RequestSecurityContext,
    hierarchy_options: HierarchyComposeOptions | None = None,
) -> str: ...
```

所有 evolve 模式要求 `Action.WRITE`。`EvolveMode.HIERARCHY` 是目标新增，必须提供
[S05-construction.md](S05-construction.md) 定义的 `HierarchyComposeOptions`。S05 是该
类型字段与默认值的唯一契约来源，API 层不复制定义。

仅 HIERARCHY 接受 `hierarchy_options`；其他模式提供 options 时抛 `ValidationError`。
HIERARCHY 缺 options 或 options 违反 S05 的 span、role 序列、`replace_existing`
约束时抛 `ValidationError`。功能关闭时抛 `PolicyError`。建树任务仍走已实现的 `job_status` / `job_cancel`，不另开 MemoryAPI。

---

### 任务面（委托 Scheduler）

本面不增加新的尚未实现对外方法。`evolve(HIERARCHY)` 落地后，建树任务仍走本面已实现的 `job_status` / `job_cancel`（S03 `Scheduler.submit(..., hierarchy_options=...)` 为控制层目标，不另开 MemoryAPI）。

#### job_status

**状态：已实现**

```python
def job_status(
    job_id: str,
    *,
    security: RequestSecurityContext,
    scope: Scope | None = None,
) -> JobInfo: ...
```

查询 Scheduler 或长耗时 Ingest 任务；Ingest 任务要求 target scope，API 对任务真实 Scope 执行 READ 鉴权与审计。先取任务（含其 scope），再据 `security.auth.actor` 对该 scope 判权。`JobInfo`：`id` / `channel` / `mode` / `scope` / `status`（`PENDING`/`RUNNING`/`SUCCEEDED`/`FAILED`/`CANCELLED`）/ `detail`。

#### job_cancel

**状态：已实现**

```python
def job_cancel(job_id: str, *, security: RequestSecurityContext) -> None: ...
```

取消尚未完成的演进任务（幂等，委托 Scheduler）。按其任务 scope 鉴权 WRITE（与 evolve 触发一致）。

---

### 治理面（委托 Governor）

本面不增加新的尚未实现对外方法。不变量 16 要求三类遍历分离：树下钻由 `search(..., expand_depth>0)` 完成（见数据面 search 的尚未实现增量），`trace` 只沿 `provenance`，`get(as_of)` 只沿 `supersedes`；不另设 `MemoryAPI.expand`。

#### inspect

**状态：已实现**

```python
def inspect(
    unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
) -> list[MemoryUnit]: ...
```

检视完整内容与治理字段（含已失效版本）。`scope` 为目标范围，本层据 `security.auth.actor` 与目标范围鉴权 READ。

#### trace

**状态：已实现**

```python
def trace(
    unit_id: str, scope: Scope, *, security: RequestSecurityContext
) -> list[MemoryUnit]: ...
```

沿 provenance 追溯演进来源链（不沿层级树、不沿 `supersedes`）。

#### audit

**状态：已实现**

```python
def audit(
    filters: dict[str, str],
    *,
    security: RequestSecurityContext,
    limit: int = 100,
) -> list[AuditEvent]: ...
```

按条件（actor/action/layer/时间段等）检索审计留痕。无具体 target scope 时以根 `Scope()` 为鉴权闸门。

---

### 授权面（委托 PermissionManager）

本面对外方法与下列契约一致，无额外尚未实现的 MemoryAPI。`PermissionManager.check` / `routing_fields` 是控制层内部接口，不对外。

#### grant

**状态：已实现**

```python
def grant(grant: Grant, *, security: RequestSecurityContext) -> Grant: ...
```

新增跨 scope 授权。`security.auth.actor` 须有权再授权 `grant.grantor` 范围（当前过渡态鉴权 SHARE）。返回同一个安全域 `Grant`；当前 `PermissionManager` 尚不生成 `grant_id`，目标 `GrantStore` 实装后由服务端生成稳定 ID。

#### revoke

**状态：已实现**

```python
def revoke(grant: Grant, *, security: RequestSecurityContext) -> None: ...
```

回收授权（幂等）。当前过渡态按 grantor+grantee+action 条件撤销且鉴权 SHARE；目标形态鉴权 `REVOKE_SHARE` 并按 `grant_id` 精确定位。公共签名保持不变。

---

### Space 管理面（委托 SpaceManager）

#### create_space

**状态：已实现**

```python
def create_space(spec: SpaceSpec, *, security: RequestSecurityContext) -> SpaceInfo: ...
```

创建全局唯一 space id；以 `Scope(org=spec.org)` 做 WRITE 鉴权，成功后记录目标 space 审计。`SpaceSpec`：`org` / `space` / `display_name` / `principal_path` / `policy` / `metadata`。

#### get_space

**状态：已实现**

```python
def get_space(org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo: ...
```

读取单个 space 的基础信息与策略。

#### list_spaces

**状态：已实现**

```python
def list_spaces(
    org: str,
    *,
    security: RequestSecurityContext,
    status: SpaceStatus | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> list[SpaceInfo]: ...
```

列出 org 下 spaces；以 `Scope(org=org)` 做 READ 鉴权。

#### update_space

**状态：已实现**

```python
def update_space(
    org: str, space: str, patch: SpacePatch, *, security: RequestSecurityContext
) -> SpaceInfo: ...
```

修改 display name、status、principal_path、policy 或 metadata。`SpacePatch` 仅非 None 生效。

#### archive_space

**状态：已实现**

```python
def archive_space(org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo: ...
```

归档 space；已归档 space 的 `add/update/evolve` 会被拒绝。读取与导出保留。

#### delete_space

**状态：已实现**（当前只支持 `PURGE`）

```python
def delete_space(
    org: str,
    space: str,
    *,
    security: RequestSecurityContext,
    mode: DeleteMode = DeleteMode.PURGE,
) -> SpaceDeleteResult: ...
```

删除 space；当前只支持 PURGE。API 先经 Engine 清该 `org + space` 下全部 user/agent/session 子 Scope 的 `/memory/` 真源与索引，再委托 SpaceManager 清 KV/messages/metadata。

**状态：已设计、尚未实现**（`mode` 取 `FORGET` / `ARCHIVE` / `DOWNWEIGHT`）。方法本身已落地，其它 `DeleteMode` 尚未实现。

#### export_space

**状态：已实现**

```python
def export_space(
    org: str,
    space: str,
    *,
    security: RequestSecurityContext,
    include_audit: bool = True,
) -> str: ...
```

创建导出记录并返回 export id。

#### space_usage

**状态：已实现**（当前统计侧重 memory/message 数量与 KV bytes）

```python
def space_usage(org: str, space: str, *, security: RequestSecurityContext) -> SpaceUsage: ...
```

查询 space 级 memory/message/KV bytes 用量。

**状态：已设计、尚未实现**（返回字段未补齐）：`SpaceUsage.index_count` / `audit_count` 待专用后端补齐（见 S03）。

#### get_space_policy

**状态：已实现**

```python
def get_space_policy(org: str, space: str, *, security: RequestSecurityContext) -> SpacePolicy: ...
```

读取 space policy。

#### set_space_policy

**状态：已实现**

```python
def set_space_policy(
    org: str, space: str, policy: SpacePolicy, *, security: RequestSecurityContext
) -> SpacePolicy: ...
```

替换 space policy，并同步主体路径。

#### list_space_members

**状态：已实现**

```python
def list_space_members(
    org: str, space: str, *, security: RequestSecurityContext
) -> list[SpaceMember]: ...
```

列出 space 成员与角色。

#### add_space_member

**状态：已实现**

```python
def add_space_member(
    org: str, space: str, member: SpaceMember, *, security: RequestSecurityContext
) -> None: ...
```

添加或更新成员角色。

#### remove_space_member

**状态：已实现**

```python
def remove_space_member(
    org: str, space: str, member: Scope, *, security: RequestSecurityContext
) -> None: ...
```

移除成员。

---

### 运行时策略面（直达 PolicyManager，不经 Engine）

本面不增加新的尚未实现对外方法。S03 目标层级策略键（`hierarchy.enabled` / `hierarchy.auto_derive` / `hierarchy.ensure_on_recall` / `hierarchy.score_propagation` / `hierarchy.expand_default_depth`）尚未作为可 `admin_set` 的运行时键落地；S08 规定能力开关等动态配置走 `ConfigSource`，不经 `admin_set` 扩展为任意配置树。层级请求关闭时的 `PolicyError` 见数据面尚未实现的 `search`/`evolve`。

#### admin_get

**状态：已实现**

```python
def admin_get(key: str, *, security: RequestSecurityContext) -> str: ...
```

读策略（直达 PolicyManager）。管理面以根 `Scope()` 鉴权。

#### admin_set

**状态：已实现**

```python
def admin_set(key: str, value: str, *, security: RequestSecurityContext) -> None: ...
```

写策略（直达 PolicyManager）；键未知或不可变抛 `PolicyError`。

#### admin_all

**状态：已实现**

```python
def admin_all(*, security: RequestSecurityContext) -> dict[str, str]: ...
```

列全部策略（直达 PolicyManager）。

## 数据结构

### Scope —— 目标范围 vs 调用方身份（`common/type_def`）

`org > space > user/agent > session` 五维归属，同时支撑隔离与共享。各维默认 `""`。API 里目标范围与调用方身份是**两个不同语义**的位置（勿混淆）：

- **目标范围（target）**：操作作用于「谁的」记忆——`scope` 参数（或 `Context.scope` / `DeleteSelector.scope`），类型 `Scope`。
- **调用方身份（actor）**：「谁」在发起调用——不再由调用方以 `Scope` 直传，而是取自必填 `security: RequestSecurityContext` 的 `security.auth.actor`；除 `check_write` 保留第二位置参数兼容外，其余方法要求具名传入。

`space` 是全局唯一的逻辑隔离标识，`org` 表示其归属组织并继续参与权限边界；不同 org
不能创建相同的非空 space id。空 `space` 只表示兼容旧数据/单租户默认域，不参与 Space
资源注册，也不表示跨全部 space。`space` 为 keyword-only 字段，旧四段位置参数顺序仍是
`org/user/agent/session`。

### RequestSecurityContext（安全输入，`common/security/types.py`）

一次请求的可信身份绑定，是 `MemoryAPI` 所有公开方法的唯一安全输入（keyword-only 必填）。

| 字段 | 类型 | 语义 |
|------|------|------|
| `auth` | AuthContext | 认证产出；`auth.actor` 即调用方身份 Scope |
| `request_id` | str | 服务端生成，进审计与授权环境，调用方不可指定 |
| `peer` | str | 传输层对端地址（不采信 `X-Forwarded-For` 一类自述 header） |
| `surface` | Surface | 接入形态（HTTP/MCP/CLI/SDK/INTERNAL），由适配层写入 |
| `started_at` | datetime | 服务端时钟（带时区），授权时效判定的 now 由它派生 |
| `attributes` | Mapping[str, str] | 只读；只由系统组件写入，业务 payload 不得注入 |

便捷属性 `security.actor` 等价于 `security.auth.actor`。构造收在
`common.security.request_context`（见不变量 14），实例携带来源绑定，
`has_valid_origin()` 可校验其是否出自受控入口。

### Context（`common/type_def/context.py`）

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `scope` | Scope | 空 Scope | 检索目标范围（多租户隔离） |
| `extensions` | dict[str, str] | `{}` | 调用方自定义透传配置，值须为传输安全的 str；约定 key `"max_tokens"` 表示自适应披露 token 预算，由 API 边界解析为 `RetrievalQuery.max_tokens` |

> `extensions["max_tokens"]` 是 API 边界解释的约定 key，解析后从透传 extensions 中移除；无此 key 或空串时披露阶段使用默认策略。

### MemoryUnit / Segment（读取类方法返回，`common/type_def/memory.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | Scope 内唯一 id（每条记忆/每个版本各一） |
| `scope` | Scope | 归属（unit 级单一 owner，隔离/存储命名空间键） |
| `tier` | MemoryTier | 认知角色分类 |
| `segments` | list[Segment] | 多段内容投影（每段 = content + assets + source） |
| `source_ref` | str | 来源引用（RawPayload id / 会话 id，可溯源） |
| `temporal` | Temporal | 双时间：`t_event`/`t_ingest`/`t_valid`/`t_invalid` |
| `provenance` | list[str] | 演进血缘（多→一合成）：由哪些 unit 提取/升华/合并而来 |
| `supersedes` | str | 版本链（一→一更替）：本版本取代的上一版 id；空表示首版 |
| `tags` | list[str] | 标签（检索前置过滤用） |
| `system_metadata` | dict[str, MetadataValueType] | 系统扩展字段（infer / procedural / pipeline / prompt key 等） |
| `user_metadata` | dict[str, MetadataValueType] | 用户业务元数据；保留 JSON 标量原生类型，也可使用字符串数组 |
| `lifecycle` | LifecycleState | 生命周期状态 |
| `hierarchy` | HierarchyRef | **状态：已设计、尚未实现**。目标树结构；为空时不启用层级 |

`Segment`：`content`（可治理文本/结构投影，索引与检索对象）、`assets`（本段原模态资产引用）、`source`（本段来源 Modality）。便捷只读折叠属性：`unit.content`（各段换行连接）、`unit.assets`（各段扁平合并）、`unit.source`（首段模态）——返回新对象，勿就地 `append`。

`ContentLayers` 的 L0/L1 是同一 unit 的披露层，不是多模态粒度，也不是 TIME 父子结构。

`MetadataValueType = str | int | float | bool | None | list[str]`。写入和更新边界对 metadata 执行以下约束：

- 接受 JSON 标量（string / number / boolean / null）和字符串数组；
- 不把业务值统一转换为字符串，真源与索引保留原生类型；
- 拒绝 `unit_id`、`tier`、`lifecycle`、`tags`、时间字段等系统保留 key；
- 不接受嵌套 object 或非字符串数组。

### MemoryPatch / UpdateMode（update，`control/types.py`）

`MemoryPatch` 仅**非 None** 字段生效：`content` / `tier` / `tags`（整体替换）/
`system_metadata`（合并）/ `user_metadata`（合并）/ `t_valid` / `t_invalid` / `mode`。目标增加
`hierarchy: HierarchyPatch | None = None`（**状态：已设计、尚未实现**）；其精确类型及结构事务校验见 S03。

`UpdateMode`：`SUPERSEDE`（默认、非破坏式，新 id 新版本 + 旧版标 superseded）/
`OVERWRITE`（原地覆写、同 id，旧内容仅留审计）。

### DeleteSelector / DeleteMode（delete，`control/types.py`）

`DeleteSelector` 各条件取「与」，至少给一项：`unit_ids` / `scope`（鉴权依据）/ `tags`（命中任一）/ `before`（`t_event` 早于此）/ `mode`。

`DeleteMode`：`FORGET`（标记遗忘，可恢复，默认）/ `ARCHIVE`（归档转冷）/ `DOWNWEIGHT`（保持 active 仅降权）/ `PURGE`（物理删真源与索引，合规硬删，不可恢复）。

### FilterExpr / FilterClause / FilterGroup（search 前置过滤，`common/type_def`）

`FilterExpr = FilterClause | FilterGroup`。单条 `FilterClause` 表示字段比较；
`FilterGroup` 用 `AND` / `OR` / `NOT` 递归组合子表达式。旧
`list[FilterClause]` 兼容为隐式 `AND`，dict DSL 可在 API / SDK 边界写成：

```python
{
    "AND": [
        {"user_metadata.project": {"in": ["alpha", "beta"]}},
        {"user_metadata.priority": {"gte": 8}},
        {"NOT": {"user_metadata.archived": True}},
    ]
}
```

`FilterOp`：`EQ`/`NE`/`IN`/`NOT_IN`/`GT`/`GTE`/`LT`/`LTE`/`CONTAINS`。
scope 不走 filters。metadata 比较严格保留类型：number、string、boolean 不互相转换；
`int` / 有限 `float` 属于同一数值类别。当前不维护中央 metadata schema，同一业务 key
的写入和查询类型应由调用方保持一致。

### MemoryListResult（list 返回，`control/types.py`）

- `items: list[MemoryUnit]`：当前分页结果。
- `count: int`：同一 Scope 和过滤条件下的分页前精确总数，不受 offset/limit 影响。

### BatchWriteItem / BatchWriteOutcome / BatchWriteResult（batch_add，`control/types.py`）

- `BatchWriteItem` 表达单项内容与可选 scope/source/tags/`system_metadata`/`user_metadata`/occurred_at 覆盖；`stream_id`、`sequence`、`idempotency_key` 首版仅用于调度和回显，不写入真源。
- `BatchWriteOutcome` 包含输入索引、归一化 item、该项产生的 `units` 与可归集的 `error` / `error_type`；成功且 units 为空仍是成功。Engine 的非领域异常也必须归集为 `InternalError`，不能使整批 HTTP 请求退化为 500。
- `BatchWriteResult.outcomes` 与输入严格一一对应。相同 `(Scope, stream_id)` 的非空 `sequence` 不得重复；接口不自动重排。

### DisclosureLevel / RetrievalResult（search 返回，`retrieval/types.py`）

`DisclosureLevel`：`L0`（摘要）/ `L1`（片段）/ `L2`（全文）/ `ADAPTIVE`（按 `max_tokens` 预算自动选层级）。

`RetrievalResult`：
- `items: list[RetrievedItem]` —— 每项 `unit_id` / `score`（融合/重排后最终分）/ `content`（按层级加载）/ `level`（实际披露层级）。
- `trajectory: list[TrajectoryStep]` —— 仅 `with_trajectory=True` 返回。每步 `stage`（parse/recall/fuse/recheck/rerank/threshold/disclose）/ `channel`（召回通道，非召回步为 None）/ `candidate_count` / `cost_ms` / `detail`。

### Channel / EvolveMode / JobInfo（evolve / 任务，`control/types.py`）

- `Channel`：`HOT`（在线低时延）/ `BACKGROUND`（离线异步，默认）。
- `EvolveMode`：`EXTRACT`（抽取低抽象事实）/ `ASSOCIATE`（关联分析）/ `CONSOLIDATE`（升华画像）/ `FORGET`（遗忘/清理）。**状态：已设计、尚未实现**：`HIERARCHY`（显式建树/区间替换）。
- `JobInfo`（`job_status` 返回）：`id` / `channel` / `mode` / `scope` / `status`（`JobStatus`：PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED）/ `detail`。

### Modality / MemoryTier / LifecycleState（`common/type_def/memory.py`）

- `Modality`：`TEXT`/`IMAGE`/`AUDIO`/`VIDEO`/`CODE`/`DOCUMENT`（多模态来源在接入层规约为 content 文本投影 + assets 原模态资产）。
- `MemoryTier`：`WORKING`/`CORE`/`EPISODIC`/`SEMANTIC`/`PROCEDURAL`/`ARCHIVAL`。
- `LifecycleState`：`ACTIVE`（默认可召回）/ `SUPERSEDED`（被新版取代）/ `ARCHIVED`（归档转冷，不默认召回）/ `FORGOTTEN`（遗忘，无继任者）。

### Grant / Action（授权，`common/security/types.py`）

`Grant`（frozen）：`grantor`（授权方 scope）/ `grantee`（被授权方 scope）/
`actions: frozenset[Action]` / `expires_at`（None 为长期）/ `grant_id`（授权的稳定标识，
构造时默认为空，目标形态由服务端生成）/ `revoked`；`is_active(*, now)` 判定时效。
为兼容既有 `jiuwen_memory.api.Grant` 调用方，构造器继续接受不含 `grant_id` 的旧参数形状，
并在构造边界把 `list[Action]` 等动作迭代归一为 `frozenset[Action]`；非 `Action` 成员立即
抛 `TypeError`。`Action`：`READ`/`WRITE`/`UPDATE`/`DELETE`/`SHARE`/
`REVOKE_SHARE`/`MANAGE_PRINCIPAL`/`MANAGE_SPACE`/`MANAGE_POLICY`/`READ_AUDIT`/
`VERIFY_AUDIT`/`ADMINISTER_SYSTEM`。

`control/types.py` 为兼容既有内部与仓外导入路径，再导出同一个安全域 `Grant`/`Action`
对象，不再维护第二套字段或枚举；API 把同一 `Grant` 实例直接交给过渡期
`PermissionManager`，不会裁掉 `grant_id` / `revoked`。旧权限实现尚无安全域管理动作的
角色闸门，因此 API 对五个旧动作之外的 Grant 显式抛 `ValueError`（fail-closed），待
`Authorizer` 实装后由其完整策略判定。

### Space 数据结构（`control/types.py`）

- `PrincipalPath`：`USER_AGENT` / `AGENT_USER`，决定 space 内 owner-cover 字段顺序。
- `SpaceStatus`：`ACTIVE` / `FROZEN` / `ARCHIVED` / `DELETING` / `DELETED`。
- `SpacePolicy`：`require_space` / `principal_path` / `storage_isolation_strategy` / `retention` / `quotas` / `index_profiles` / `pipeline_profiles`。
- `SpaceSpec`：`org` / `space` / `display_name` / `principal_path` / `policy` / `metadata`。
- `SpaceInfo`：`org` / `space` / `display_name` / `status` / `principal_path` / `policy` / `metadata` / `created_at` / `archived_at`。
- `SpacePatch`：`display_name` / `status` / `principal_path` / `policy` / `metadata`。
- `SpaceMember`：`scope` / `role` / `created_at` / `expires_at`。
- `SpaceUsage`：`org` / `space` / `memory_count` / `message_count` / `index_count` / `storage_bytes` / `audit_count`。当前实现填充 memory/message 数量与 KV bytes；`index_count` / `audit_count` **状态：已设计、尚未实现**。
- `SpaceDeleteResult`：`org` / `space` / `deleted_counts` / `status` / `audit_event_id`。

> 规划中：`SpaceMember` 增内容轴与治理轴两个角色字段、`SpaceInfo` 增归属主体登记、`SpaceSpec` 增创建者身份，另新增空间授权事实快照类型。见 [F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md) 「空间数据结构变更」。

### AuditEvent（audit 返回，`common/type_def/audit.py`）

`id` / `actor`（操作者 Scope）/ `target`（目标 Scope）/ `action` / `target_id` / `layer`（产生事件的层）/ `occurred_at` / `detail`。`detail` 常见约定包括 `permission_check`、`permission_reason`、`job_id`、`before_unit_id` / `after_unit_id`、`before_unit_ids` / `after_unit_ids`；其中 `before_unit_*` / `after_unit_*` 仅表示记忆单元 id，不用于调度任务 id。审计查询支持 `actor_*` 与 `target_*` scope 字段过滤。

`search/get/inspect/trace` 使用 READ，`add/evolve` 使用 WRITE，`update` 使用 UPDATE，`delete` 使用 DELETE，`grant/revoke` 使用 SHARE。未限定 target scope 的 delete 和全局管理操作退到根 scope 闸门。

## 错误语义

| 异常 | 触发场景 |
|------|----------|
| `PermissionDeniedError` | 鉴权不通过（`security.auth.actor` 对 target scope 无相应 Action 权限） |
| `NotFoundError` | `get`、`update` 目标在已鉴权 scope 内不可见 |
| `ValidationError` | 入参非法（如 `search` 的 `top_k <= 0`；`add`/`batch_add` 的 `content` 非 `str`、空串或纯空白；目标层级的 span、深度、预算、options 或 patch 形状非法） |
| `PolicyError` | `admin_set` 的键未知或为不可变配置；层级功能关闭时发起显式目标层级操作 |
| `ConflictError` | 写入冲突（如 id 重复）或目标结构前置条件与当前状态冲突 |
| `BackendError` / `HealthCheckError` | 后端故障 / 健康探测失败 |

## 鉴权流程

```
接入层 → authenticated(...) / internal_context(...) → security: RequestSecurityContext
调用方 → MemoryAPI.method(scope=target, security=security)
  → identity = security.auth.actor
  → PermissionManager.check(actor=identity, target=scope, action=<对应动作>, context=...)
    # list/get/update/delete/inspect/trace 的已有资源上下文来自真源和已鉴权 target scope
    → 通过 → 委托 Engine/Governor/PolicyManager（仅传 scope，不传 identity）
    → 拒绝 → 抛 PermissionDeniedError
  → 落审计事件（含 identity + action + target_id + 时间）
```

> 目标形态下这一步由安全域 `Authorizer` 承担（策略判定、`AuthorizationEnvironment`、
> 决策审计）；本期只固定接口，鉴权仍走既有 `PermissionManager`，运行行为不变。
> 详见 [F05 安全域接口契约](../features/common/F05-security-api-contracts.md)。

## 实现注册机制

```
jiuwen_memory/api/memory_api_impl/
    __init__.py             # 重导出实现类
    local_memory_api.py     # LocalMemoryAPI：PEP + 委托
    assembly.py             # build_kernel / assemble
```

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | add 路径中 Engine 内部调用 Ingestor |
| S03-control | 数据面委托 MemoryEngine，治理/授权/调度/space 面委托对应算子 |
| S04-retrieval | search 路径中 Engine 委托 Retriever |
| S05-construction | 目标 `HierarchyComposeOptions` / `EvolveMode.HIERARCHY` 的字段契约 |
| S08-config | 六类动态配置经 ConfigSource；不经本层业务入参写入 |
| F07-collective-memory | 本层是空间治理的鉴权点：入口到轴与动作的映射、空间事实一次读取、检索两族谓词的生成与注入均落在本层。多空间读写编排按「是否读 `identity`」拆开——判权与谓词生成留本层，写入候选集的计算与跨空间的召回扇出落控制层 `control/collective/` |
| architecture.md §6 | 已实现 MemoryAPI 清单 |
