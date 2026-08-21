# S02 — 记忆接口层（Memory API Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/api/ |
| 最近一次修订日期 | 2026-08-20 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/api/F03-batch-write-api.md，docs/features/construction/F02-dynamic-extraction-consolidation.md，docs/features/construction/F04-cc-memory-compat.md，docs/features/construction/F05-construction-spec-multimodal-design.md，docs/features/common/F01-memory-layer.md，docs/features/common/F08-memory-tree.md，docs/features/common/F03-scope-space-isolation.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/control/F04-permission-context-routing.md，docs/features/control/F05-cloud-engine-design.md，docs/features/config/F01-config-source.md |
## Metadata 公共 API 契约

`add` / `add_async` / `batch_add` 以及 `BatchWriteItem` 分别接收
`system_metadata` 和 `user_metadata`，不再接收混合 `metadata`。`MemoryPatch` 对两个
dict 分别做 merge-update。用户过滤的规范路径为 `user_metadata.<key>`；裸自定义
字段仅在规范化边界作为该路径的兼容写法，`metadata.<key>` 拒绝。

## 范围 / 边界

**管什么**：
- 统一对外 Core API（形态无关）：所有接入形态（SDK/CLI/Skill/MCP/HTTP·gRPC）最终映射到 `MemoryAPI`
- 鉴权执行点（PEP）：调用 `PermissionManager.check(identity, scope, action)` 做入口鉴权
- 入口审计：写审计事件到 `AuditLogger`
- 参数装配：将调用侧参数装配为控制层可消费的内部结构
- 同步/异步桥接：为同步形态桥接引擎异步协程

**不管什么**：
- 不做编排逻辑（全部委托 `src/control`）
- 不直接操作存储
- 不调用 LLM / 构建 / 检索
- 不做 admin 策略存储（直达 PolicyManager）

## 不变量

1. **本层是薄封装 + PEP**：数据面委托 `MemoryEngine`，治理面委托 `Governor`，授权面委托 `PermissionManager`，调度面委托 `Scheduler`，策略面直达 `PolicyManager`。
2. **`identity` 不下沉**：鉴权通过后只透传已鉴权的 target `scope`，`identity` 不传入控制层。
3. **`identity` 为必填 keyword-only 参数**：与 target `scope` 同为 `Scope` 类型，强制具名传入防止位置传反。
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
15. **层级能力默认关闭（目标）**：普通 `add` 默认不建父树；只由显式 `evolve(..., mode=HIERARCHY, hierarchy_options=...)` 或启用的后台策略触发。显式层级请求在 `hierarchy.enabled=false` 时抛 `PolicyError`，不带层级参数的既有操作保持语义。
16. **三类遍历严格分离（目标）**：`trace` 只沿 `provenance`；树下钻由 `search(..., expand_depth>0)` 沿 `HierarchyRef` 完成；`get(as_of)` 只沿 `supersedes`/valid-time；L0/L1/L2 仅表示同一 unit 的披露层。


## 接口契约

### MemoryAPI（`memory_api.py`）

### search

```python
def search(
    query: str,
    context: Context,
    *,
    identity: Scope,
    filters: FilterExpr | list[FilterClause] | None = None,
    as_of: datetime | None = None,
    top_k: int = 10,
    disclosure: DisclosureLevel = DisclosureLevel.L0,
    with_trajectory: bool = False,
    hierarchy_kind: HierarchyKind | None = None,
    hierarchy_role: HierarchyRole | None = None,
    span_start: datetime | None = None,
    span_end: datetime | None = None,
    expand_depth: int = 0,
    rollup: bool = False,
) -> RetrievalResult: ...
```

`filters/as_of/top_k/disclosure/with_trajectory` 和 `context.extensions["max_tokens"]` 的既有处理不变。新增参数原样装配到 `RetrievalQuery`。`expand_depth=0`、`rollup=false` 保证默认只返回直接召回命中的节点，不展开后代、不把后代分数上卷；调用方通过 `hierarchy_role` 指定父侧角色时，即形成父节点优先召回。省略 role 时，同 kind 下所有活动角色均可参与召回。span 是 `HierarchyRef` 结构区间，不替代查询文本解析得到的 event-time。

`expand_depth>0` 时，Retriever 在同一次 search 内沿命中父节点展开子证据（单 kind）；**不另设公开 `MemoryAPI.expand`**。展开选子与父命中共用既有 `max_tokens` 上下文预算，不另设独立预算参数。

校验和闭区间相交语义以 [S04-retrieval.md](S04-retrieval.md) 为准。任一显式 hierarchy 参数、非零展开深度或 rollup 都构成层级请求；功能关闭时抛 `PolicyError`。普通 search 不因 hierarchy 关闭而失败。

### evolve

```python
def evolve(
    scope: Scope,
    mode: EvolveMode,
    channel: Channel = Channel.BACKGROUND,
    *,
    identity: Scope,
    hierarchy_options: HierarchyComposeOptions | None = None,
) -> str: ...
```

所有 evolve 模式要求 `Action.WRITE`。`EvolveMode.HIERARCHY` 是目标新增，必须提供
[S05-construction.md](S05-construction.md) 定义的 `HierarchyComposeOptions`。S05 是该
类型字段与默认值的唯一契约来源，API 层不复制定义。

仅 HIERARCHY 接受 `hierarchy_options`；其他模式提供 options 时抛 `ValidationError`。
HIERARCHY 缺 options 或 options 违反 S05 的 span、role 序列、`replace_existing`
约束时抛 `ValidationError`。功能关闭时抛 `PolicyError`。调用成功返回 job id，
不表示任务已经完成。

### update 与层级 patch（目标契约，尚未实现）

`MemoryPatch` 的既有非空字段为 `content/tier/tags/metadata/t_valid/t_invalid/mode`，目标增加：

```python
hierarchy: HierarchyPatch | None = None
```

`HierarchyPatch` 的精确类型由 S03 定义。它只允许修改指定 kind 的结构状态、span、
受控子边和稳定顺序；不得接受未校验的完整 `HierarchyRef`、裸 `parent_id` 或任意
`child_ids` 覆写。API 仅做形状校验，Engine 必须按 S03 验证同 org+space、无环、单父、
双向一致和稳定顺序后原子应用。

`SUPERSEDE` 若目标已挂树，Engine 必须把结构位置从旧 id 一致迁移到新版本 id，再把旧版本设为 `SUPERSEDED`；不得留下指向旧版本的活动结构边。`OVERWRITE` 保持 id，但层级 patch 仍须经过相同校验。

#### 数据面（委托 MemoryEngine）

| 方法 | 签名 | 语义 |
|------|------|------|
| `add` | `(content, scope, source=TEXT, *, identity, assets, tags, metadata, occurred_at) -> list[MemoryUnit]` | 同步写入：鉴权 WRITE→委托 Engine→阻塞至 hot path 完成。infer/procedural 触发时返回 `created_ids` 对应的派生单元（可空），否则返回原始单元 |
| `add_async` | `async (同签名) -> list[MemoryUnit]` | 异步写入：直通 Engine 协程，供事件循环形态使用 |
| `batch_add` | `(items: list[BatchWriteItem], scope=None, source=TEXT, *, identity, tags, metadata, occurred_at, stream_id="", continue_on_error=True) -> BatchWriteResult` | 同步桥接批量写入；逐项归一化、WRITE 鉴权、space 校验与审计，结果始终按输入索引对齐 |
| `batch_add_async` | `async (同签名) -> BatchWriteResult` | 串行保序批量写入；默认归集单项错误，`continue_on_error=False` 时后续项为 `Skipped` |
| `search` | 见上文完整签名 | 混合检索：鉴权 READ→拆 Context→装配 RetrievalQuery→委托 Engine；层级字段为目标、默认关闭；`expand_depth>0` 时同次召回内展开 |

| `list` | `(scope, *, identity, offset=0, limit=100, memory_types=None, extensions=None, filters=None) -> MemoryListResult` | 列出已建索引记忆：支持类型/FilterExpr 过滤、自定义参数透传和分页前精确总数；只返回 `/memory/` 真源记录 |
| `get` | `(unit_id, scope, *, identity, as_of=None) -> MemoryUnit` | 真源点读：鉴权 READ→委托 Engine |
| `update` | `(unit_id, scope, patch: MemoryPatch, *, identity) -> MemoryUnit` | 修正记忆：鉴权 UPDATE→委托 Engine |
| `delete` | `(selector: DeleteSelector, *, identity) -> list[str]` | 删除/归档/降权：鉴权 DELETE→委托 Engine |
| `evolve` | `(scope, mode: EvolveMode, channel=BACKGROUND, *, identity, hierarchy_options=None) -> str` | 触发演进：鉴权→委托 Engine→返回 job_id；仅目标 `HIERARCHY` 接受 options |
| `job_status` | `(job_id, *, identity, scope=None) -> JobInfo` | 查询 Scheduler 或长耗时 Ingest 任务；Ingest 任务要求 target scope，API 对任务真实 Scope 执行 READ 鉴权与审计 |
| `job_cancel` | `(job_id, *, identity) -> None` | 取消任务（委托 Scheduler） |
| `admin_get` | `(key, *, identity) -> str` | 读策略（直达 PolicyManager） |
| `admin_set` | `(key, value, *, identity) -> None` | 写策略（直达 PolicyManager） |
| `admin_all` | `(*, identity) -> dict[str, str]` | 列全部策略（直达 PolicyManager） |

`list` 的 `memory_types` 用于数据过滤，也参与权限路由：显式传一个或多个类型时，API 层为每个
类型分别构造 `PermissionContext(memory_type=<type>)` 并逐个执行 READ 鉴权；未传类型时先按
普通 `resource_type="memory_list"` 鉴权。两种路径随后都调用
`MemoryEngine.list_with_permission_contexts`，一次取得当前页、分页前总数及 MemoryUnit 真源
`memory_type/pipeline/tags/metadata` 权限上下文并逐条二次鉴权，避免未传过滤条件时落入
宽松 fallback，也避免权限上下文与内容来自两次分页读取。

`extensions` 为 `dict[str, str]`，API 防御性复制并把值规范为字符串；未知 key 原样透传。
`filters` 与 search 共用 FilterExpr/旧 list/dict DSL 规范化语义，`memory_types` 与 filters
取 AND。`org/space/user/agent/session` 属于 Scope 隔离轴，不得出现在 filters。

#### infer 开关（add 的同步抽取语义）

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

#### procedural 开关（add 的过程记忆抽取）

`add` 的 `system_metadata["procedural"]` 是独立于 infer 的调用级开关（详见 F02 决策8）：

- **`procedural="true"`**：原文**不落 KV**；喂 `Evolver.evolve(units, EXTRACT)`。extractor 把本轮汇总成一条 PROCEDURAL 执行历史，再由 Evolver 落盘（`DynamicEvolver` 也走父类 procedural 路径，不判定）。
- procedural 与 infer 同传时按 procedural 语义：原文不落 `/messages/`、不收集
  context、不去重。语义是"把这轮做了什么记成一条可检索 how-to"。

#### 动态抽取与巩固 prompt

- metadata 的公共类型是 `dict[str, Any]`；动态 prompt 控制项的值按
  `str(value).strip()` 解释为 prompt key。调用方使用
  `metadata["_extract_prompt_<strategy>"] = prompt_key` 或 `metadata.update(...)`
  传值；不存在 `metadata.append()`。
- 在 `add` 的普通同步抽取路径中，`_extract_prompt_<strategy>` 随 `infer=true`
  进入 EXTRACT；procedural 或显式 `evolve(EXTRACT)` 同样进入 Evolver 的 EXTRACT
  路径，Extractor 本身不校验 infer。支持任意非空 strategy，每个策略调用一次；
  无动态 prompt 时回退旧 Extractor。
- `_consolidation_prompt_<strategy>` 为落盘前动态巩固 prompt 的 **key**（引用 yml `prompts` 段的命名 prompt）。运行时由 `PromptRegistry` 按 `phase=consolidate + key` 查真实文本。`DynamicEvolver` 消费；无 prompt 或输出不合法时回退规则判定（高相似度 NOOP，否则 ADD）。
- `_reflect_prompt_<strategy>` 为反思步 prompt 的 key（同上，`phase=reflect`）。reflect 默认 no-op，子类可覆盖 `_reflect_step`。
- LLM 输出格式由 prompt 自身约定，内核不追加固定 schema。

#### infer 上下文增强与 KV 前缀分离（增量）

- **infer=true 时 evolver 内部收集上下文**（evolve 接口不变）：`recent_originals`（最近 10 条 infer 原文，做指代消解/语境，不参与去重）+ `related_memories`（`dedup.recall` 召回 10 条相关记忆，做去重提示）。两类参考项只拼进 extractor prompt，不进提取来源；最终判定由所选 Evolver 完成：`OrchestratingEvolver` 走 `_dedup_batch`，`DynamicEvolver` 走 consolidate → reflect → 落盘。详见 F02 决策7。
- **KV key 前缀分离**：真源 key 按「是否建索引」带前缀——`/memory/{id}`（建索引记忆）、`/messages/{id}`（未建索引 infer 原文）。前缀常量与 helper 在 `common.type_def.memory`/`raw`。详见 F02 决策6。
- **engine.write infer=false 调 classify**：默认路径调 `classifier.classify` 给原文打 tier+tags（纯 LLM 抽取 episodic/semantic/procedural + tags）；infer=true 不经 classifier（extractor 产派生时自定）。详见 F02 决策9。
- **`/v1/list` 收窄并上收为 API 契约**：handler `_list` 委托
  `MemoryAPI.list(scope, identity=..., offset, limit, memory_types, extensions, filters)`；
  `KVStore.list` 只查询 `/memory/` 记忆并返回当前页与分页前总数。详见 F02 决策10与
  F01 的 list 决策。

> 开关由来与"为何默认不同步、为何经 Evolver 而非独立 Extractor"的取舍见 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)；add 路径流程见 [`S03-control.md`](S03-control.md)。

#### 治理面（委托 Governor）

| 方法 | 签名 | 语义 |
|------|------|------|
| `inspect` | `(unit_ids, scope, *, identity) -> list[MemoryUnit]` | 检视完整内容与治理字段（含已失效版本） |
| `trace` | `(unit_id, scope, *, identity) -> list[MemoryUnit]` | 沿 provenance 追溯演进来源链 |
| `audit` | `(filters: dict[str, str], *, identity, limit=100) -> list[AuditEvent]` | 按条件检索审计留痕 |

#### 授权面（委托 PermissionManager）

| 方法 | 签名 | 语义 |
|------|------|------|
| `grant` | `(grant: Grant, *, identity) -> None` | 新增跨 scope 授权 |
| `revoke` | `(grant: Grant, *, identity) -> None` | 回收授权（幂等） |

#### Space 管理面（委托 SpaceManager）

| 方法 | 签名 | 语义 |
|------|------|------|
| `create_space` | `(spec: SpaceSpec, *, identity) -> SpaceInfo` | 创建全局唯一 space id；以 `Scope(org=spec.org)` 做 WRITE 鉴权，成功后记录目标 space 审计 |
| `get_space` | `(org, space, *, identity) -> SpaceInfo` | 读取单个 space 的基础信息与策略 |
| `list_spaces` | `(org, *, identity, status=None, limit=100, cursor=None) -> list[SpaceInfo]` | 列出 org 下 spaces；以 `Scope(org=org)` 做 READ 鉴权 |
| `update_space` | `(org, space, patch: SpacePatch, *, identity) -> SpaceInfo` | 修改 display name、status、principal_path、policy 或 metadata |
| `archive_space` | `(org, space, *, identity) -> SpaceInfo` | 归档 space；已归档 space 的 `add/update/evolve` 会被拒绝 |
| `delete_space` | `(org, space, *, identity, mode=PURGE) -> SpaceDeleteResult` | 删除 space；当前只支持 PURGE，API 先经 Engine 清该 `org + space` 下全部 user/agent/session 子 Scope 的 `/memory/` 真源与索引，再委托 SpaceManager 清 KV/messages/metadata |
| `export_space` | `(org, space, *, identity, include_audit=True) -> str` | 创建导出记录并返回 export id |
| `space_usage` | `(org, space, *, identity) -> SpaceUsage` | 查询 space 级 memory/message/KV bytes 用量 |
| `get_space_policy` | `(org, space, *, identity) -> SpacePolicy` | 读取 space policy |
| `set_space_policy` | `(org, space, policy: SpacePolicy, *, identity) -> SpacePolicy` | 替换 space policy，并同步主体路径 |
| `list_space_members` | `(org, space, *, identity) -> list[SpaceMember]` | 列出 space 成员与角色 |
| `add_space_member` | `(org, space, member: SpaceMember, *, identity) -> None` | 添加或更新成员角色 |
| `remove_space_member` | `(org, space, member: Scope, *, identity) -> None` | 移除成员 |

## 数据结构

### Scope —— 目标范围 vs 调用方身份（`common/type_def`）

`org > space > user/agent > session` 五维归属，同时支撑隔离与共享。各维默认 `""`。API 里 `Scope` 出现在两个**不同语义**的位置（均为 `Scope` 类型，勿混淆）：

- **目标范围（target）**：操作作用于「谁的」记忆——`scope` 参数（或 `Context.scope` / `DeleteSelector.scope`）。
- **调用方身份（identity）**：「谁」在发起调用——`identity` 参数（必填 keyword-only）。

`space` 是全局唯一的逻辑隔离标识，`org` 表示其归属组织并继续参与权限边界；不同 org
不能创建相同的非空 space id。空 `space` 只表示兼容旧数据/单租户默认域，不参与 Space
资源注册，也不表示跨全部 space。`space` 为 keyword-only 字段，旧四段位置参数顺序仍是
`org/user/agent/session`。

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
| `metadata` | dict[str, Any] | 业务元数据；保留 JSON 标量原生类型，也可使用字符串数组 |
| `lifecycle` | LifecycleState | 生命周期状态 |
| `hierarchy` | HierarchyRef | 目标树结构；为空时不启用层级 |

`Segment`：`content`（可治理文本/结构投影，索引与检索对象）、`assets`（本段原模态资产引用）、`source`（本段来源 Modality）。便捷只读折叠属性：`unit.content`（各段换行连接）、`unit.assets`（各段扁平合并）、`unit.source`（首段模态）——返回新对象，勿就地 `append`。

`ContentLayers` 的 L0/L1 是同一 unit 的披露层，不是多模态粒度，也不是 TIME 父子结构。

写入和更新边界对 metadata 执行以下约束：

- 接受 JSON 标量（string / number / boolean / null）和字符串数组；
- 不把业务值统一转换为字符串，真源与索引保留原生类型；
- 拒绝 `unit_id`、`tier`、`lifecycle`、`tags`、时间字段等系统保留 key；
- 不接受嵌套 object 或非字符串数组。

### MemoryPatch / UpdateMode（update，`control/types.py`）

`MemoryPatch` 仅**非 None** 字段生效：`content` / `tier` / `tags`（整体替换）/
`metadata`（合并）/ `t_valid` / `t_invalid` / `mode`。目标增加
`hierarchy: HierarchyPatch | None = None`；其精确类型及结构事务校验见 S03。

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
        {"metadata.project": {"in": ["alpha", "beta"]}},
        {"metadata.priority": {"gte": 8}},
        {"NOT": {"metadata.archived": True}},
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

- `BatchWriteItem` 表达单项内容与可选 scope/source/tags/metadata/occurred_at 覆盖；`stream_id`、`sequence`、`idempotency_key` 首版仅用于调度和回显，不写入真源。
- `BatchWriteOutcome` 包含输入索引、归一化 item、该项产生的 `units` 与可归集的 `error` / `error_type`；成功且 units 为空仍是成功。Engine 的非领域异常也必须归集为 `InternalError`，不能使整批 HTTP 请求退化为 500。
- `BatchWriteResult.outcomes` 与输入严格一一对应。相同 `(Scope, stream_id)` 的非空 `sequence` 不得重复；接口不自动重排。

### DisclosureLevel / RetrievalResult（search 返回，`retrieval/types.py`）

`DisclosureLevel`：`L0`（摘要）/ `L1`（片段）/ `L2`（全文）/ `ADAPTIVE`（按 `max_tokens` 预算自动选层级）。

`RetrievalResult`：
- `items: list[RetrievedItem]` —— 每项 `unit_id` / `score`（融合/重排后最终分）/ `content`（按层级加载）/ `level`（实际披露层级）。
- `trajectory: list[TrajectoryStep]` —— 仅 `with_trajectory=True` 返回。每步 `stage`（parse/recall/fuse/recheck/rerank/threshold/disclose）/ `channel`（召回通道，非召回步为 None）/ `candidate_count` / `cost_ms` / `detail`。

### Channel / EvolveMode / JobInfo（evolve / 任务，`control/types.py`）

- `Channel`：`HOT`（在线低时延）/ `BACKGROUND`（离线异步，默认）。
- `EvolveMode`：`EXTRACT`（抽取低抽象事实）/ `ASSOCIATE`（关联分析）/ `CONSOLIDATE`（升华画像）/ `FORGET`（遗忘/清理）。
- `JobInfo`（`job_status` 返回）：`id` / `channel` / `mode` / `scope` / `status`（`JobStatus`：PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED）/ `detail`。

### Modality / MemoryTier / LifecycleState（`common/type_def/memory.py`）

- `Modality`：`TEXT`/`IMAGE`/`AUDIO`/`VIDEO`/`CODE`/`DOCUMENT`（多模态来源在接入层规约为 content 文本投影 + assets 原模态资产）。
- `MemoryTier`：`WORKING`/`CORE`/`EPISODIC`/`SEMANTIC`/`PROCEDURAL`/`ARCHIVAL`。
- `LifecycleState`：`ACTIVE`（默认可召回）/ `SUPERSEDED`（被新版取代）/ `ARCHIVED`（归档转冷，不默认召回）/ `FORGOTTEN`（遗忘，无继任者）。

### Grant / Action（授权，`control/types.py`）

`Grant`：`grantor`（授权方 scope）/ `grantee`（被授权方 scope）/ `actions: list[Action]` / `expires_at`（None 为长期）。`Action`：`READ`/`WRITE`/`UPDATE`/`DELETE`/`SHARE`。

### Space 数据结构（`control/types.py`）

- `PrincipalPath`：`USER_AGENT` / `AGENT_USER`，决定 space 内 owner-cover 字段顺序。
- `SpaceStatus`：`ACTIVE` / `FROZEN` / `ARCHIVED` / `DELETING` / `DELETED`。
- `SpacePolicy`：`require_space` / `principal_path` / `storage_isolation_strategy` / `retention` / `quotas` / `index_profiles` / `pipeline_profiles`。
- `SpaceSpec`：`org` / `space` / `display_name` / `principal_path` / `policy` / `metadata`。
- `SpaceInfo`：`org` / `space` / `display_name` / `status` / `principal_path` / `policy` / `metadata` / `created_at` / `archived_at`。
- `SpacePatch`：`display_name` / `status` / `principal_path` / `policy` / `metadata`。
- `SpaceMember`：`scope` / `role` / `created_at` / `expires_at`。
- `SpaceUsage`：`org` / `space` / `memory_count` / `message_count` / `index_count` / `storage_bytes` / `audit_count`。
- `SpaceDeleteResult`：`org` / `space` / `deleted_counts` / `status` / `audit_event_id`。

### AuditEvent（audit 返回，`common/type_def/audit.py`）

`id` / `actor`（操作者 Scope）/ `target`（目标 Scope）/ `action` / `target_id` / `layer`（产生事件的层）/ `occurred_at` / `detail`。`detail` 常见约定包括 `permission_check`、`permission_reason`、`job_id`、`before_unit_id` / `after_unit_id`、`before_unit_ids` / `after_unit_ids`；其中 `before_unit_*` / `after_unit_*` 仅表示记忆单元 id，不用于调度任务 id。审计查询支持 `actor_*` 与 `target_*` scope 字段过滤。

`search/get/inspect/trace` 使用 READ，`add/evolve` 使用 WRITE，`update` 使用 UPDATE，`delete` 使用 DELETE，`grant/revoke` 使用 SHARE。未限定 target scope 的 delete 和全局管理操作退到根 scope 闸门。

## 错误语义

| 异常 | 触发场景 |
|------|----------|
| `PermissionDeniedError` | 鉴权不通过（identity 对 target scope 无相应 Action 权限） |
| `NotFoundError` | `get`、`update` 目标在已鉴权 scope 内不可见 |
| `ValidationError` | 入参非法（如 `search` 的 `top_k <= 0`；`add`/`batch_add` 的 `content` 非 `str`、空串或纯空白；目标层级的 span、深度、预算、options 或 patch 形状非法） |
| `PolicyError` | `admin_set` 的键未知或为不可变配置；层级功能关闭时发起显式目标层级操作 |
| `ConflictError` | 写入冲突（如 id 重复）或目标结构前置条件与当前状态冲突 |

| `BackendError` / `HealthCheckError` | 后端故障 / 健康探测失败 |

## 鉴权流程

```
调用方 → MemoryAPI.method(scope=target, identity=caller)
  → PermissionManager.check(actor=identity, target=scope, action=<对应动作>, context=...)
    # list/get/update/delete/inspect/trace 的已有资源上下文来自真源和已鉴权 target scope
    → 通过 → 委托 Engine/Governor/PolicyManager（仅传 scope，不传 identity）
    → 拒绝 → 抛 PermissionDeniedError
  → 落审计事件（含 identity + action + target_id + 时间）
```

## 实现注册机制

```
src/api/memory_api_impl/
    __init__.py             # 重导出实现类
```

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | add 路径中 Engine 内部调用 Ingestor |
| S03-control | 数据面委托 MemoryEngine，治理/授权/调度面委托对应算子 |
| S04-retrieval | search 路径中 Engine 委托 Retriever |
| S08-config | 六类动态配置经 ConfigSource；不经本层业务入参写入 |
| architecture.md §9 | 记忆接口层语义定义 |
