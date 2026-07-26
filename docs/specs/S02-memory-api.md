# S02 — 记忆接口层（Memory API Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/api/ |
| 最近一次修订日期 | 2026-07-25 |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/construction/F02-dynamic-extraction-consolidation.md |
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
4. **recall 参数拆分**：`context: Context` 在本层边界拆开——`context.scope` 作独立轴穿透，`context.extensions` 写入调用级 options；约定 key `context.extensions["max_tokens"]` 由 API 边界解析为 int 后写入 `RetrievalQuery.max_tokens`，并从透传 extensions 中移除；`Context` 对象本身不进控制层。
5. **admin 不经 Engine**：admin_get/set/all 直达 PolicyManager。
6. **管理面闸门 = 根 scope**：无具体 target scope 的方法（admin_*、全局 audit）以根 scope `Scope()` 为鉴权目标——「能对根 scope 行权」即管理员闸门；租户数据/治理方法仍按各自 target scope 鉴权。
7. **`as_of` = valid-time 回溯点**：`recall`/`get` 的 `as_of` 沿系统相信时间轴回溯，返回「那时被认为有效」的版本（`get` 沿 `supersedes` 版本链定位）；`None` 表示当前态。
8. **target scope 兜底**：`delete` 等以 `selector.scope or 根 scope` 为鉴权目标——未限定 scope 的跨范围操作退到根闸门，要求更高权限。

## 接口契约

### MemoryAPI（`memory_api.py`）

#### 数据面（委托 MemoryEngine）

| 方法 | 签名 | 语义 |
|------|------|------|
| `write` | `(content, scope, source=TEXT, *, identity, assets, tags, metadata, occurred_at) -> list[MemoryUnit]` | 同步写入：鉴权 WRITE→委托 Engine→阻塞至 hot path 巩固完成。返回本次新增或更新的单元；NOOP 时可空 |
| `write_async` | `async (同签名) -> list[MemoryUnit]` | 异步写入：直通 Engine 协程，供事件循环形态使用 |
| `recall` | `(query, context: Context, *, identity, filters, as_of, top_k, disclosure, with_trajectory) -> RetrievalResult` | 混合检索：鉴权 READ→拆 Context→装配 RetrievalQuery→委托 Engine |
| `get` | `(unit_id, scope, *, identity, as_of=None) -> MemoryUnit` | 真源点读：鉴权 READ→委托 Engine |
| `update` | `(unit_id, scope, patch: MemoryPatch, *, identity) -> MemoryUnit` | 修正记忆：鉴权 UPDATE→委托 Engine |
| `delete` | `(selector: DeleteSelector, *, identity) -> list[str]` | 删除/归档/降权：鉴权 DELETE→委托 Engine |
| `evolve` | `(scope, mode: EvolveMode, channel=BACKGROUND, *, identity) -> str` | 触发演进：鉴权→委托 Engine→返回 job_id |
| `job_status` | `(job_id, *, identity) -> JobInfo` | 查询任务状态（委托 Scheduler） |
| `job_cancel` | `(job_id, *, identity) -> None` | 取消任务（委托 Scheduler） |
| `admin_get` | `(key, *, identity) -> str` | 读策略（直达 PolicyManager） |
| `admin_set` | `(key, value, *, identity) -> None` | 写策略（直达 PolicyManager） |
| `admin_all` | `(*, identity) -> dict[str, str]` | 列全部策略（直达 PolicyManager） |

#### infer 开关（write 的同步抽取语义）

`write` 的 `metadata["infer"]` 是调用级开关，控制写入时是否同步抽取派生记忆（对齐 mem0 `add(infer=True)`）：

- **真值判定**：`str(metadata.get("infer", "")).strip().lower() == "true"`——大小写/空白不敏感，仅字符串 `"true"` 触发，其余值（含 `"false"`/缺省/空）均走默认路径。
- **`infer="true"`**：原始记忆落 KV 真源但**不建索引**；hot path 同步走 `Engine → Evolver.evolve(units, EXTRACT)`——Extractor 抽取派生记忆 → Consolidator 判定+落盘+建索引（ADD/UPDATE/SUPERSEDE/NOOP）。返回新增或更新的派生单元；全部 NOOP 时合法返回空列表。
- **缺省 / 非 `"true"`（infer=false）**：原文经 `classifier.classify` 打 tier+tags，再统一交给 Consolidator 落盘。classifier 未注入时跳过。
- **evolver 缺失**：`infer="true"` 但装配未注入 `Evolver` 时 Engine 抛 `RuntimeError`——装配问题暴露而非静默降级。默认装配 `evolver: orchestrating` 总是注入。

#### procedural 开关（write 的过程记忆抽取）

`write` 的 `metadata["procedural"]` 是独立于 infer 的调用级开关（详见 F02 决策8）：

- **`procedural="true"`**：原文**不落 KV**；喂 `Evolver.evolve(units, EXTRACT)`。extractor 把本轮汇总成一条 PROCEDURAL 执行历史，再交给 Consolidator。
- procedural 与 infer 互斥：procedural=true 时原文不落 `/messages/`、不收集 context。语义是"把这轮做了什么记成一条可检索 how-to"。

#### 动态抽取与巩固 prompt

- metadata 是 `dict[str, str]`，调用方用 `metadata["_extract_prompt_<strategy>"] = prompt`
  或 `metadata.update(...)` 传值；不存在 `metadata.append()`。
- `_extract_prompt_<strategy>` 仅在 `infer=true` 时触发动态抽取；支持任意非空 strategy，
  每个策略调用一次。无动态 prompt 时回退旧 Extractor。
- `_consolidation_prompt_<strategy>` 为落盘前动态巩固 prompt。所有默认单 pipeline write
  都经过 `consolidation_2`；无 prompt 或输出不合法时回退 `consolidation_1`。
- 内核在自定义 prompt 后追加固定 JSON schema，调用方不能改变候选字段或
  ADD/UPDATE/SUPERSEDE/NOOP 决策集合。

#### infer 上下文增强与 KV 前缀分离（增量）

- **infer=true 时 evolver 内部收集上下文**（evolve 接口不变）：`recent_originals`（最近 10 条 infer 原文，做指代消解/语境，不参与去重）+ `related_memories`（`dedup.recall` 召回 10 条相关记忆，做去重提示）。两类参考项只拼进 extractor prompt，不进提取来源。去重靠 prompt 提示 + `_dedup_batch` 兜底（evolver 不再做产出后向量过滤）。详见 F02 决策7。
- **KV key 前缀分离**：真源 key 按「是否建索引」带前缀——`/memory/{id}`（建索引记忆）、`/messages/{id}`（未建索引 infer 原文）。前缀常量与 helper 在 `common.type_def.memory`/`raw`。详见 F02 决策6。
- **engine.write infer=false 调 classify**：默认路径调 `classifier.classify` 给原文打 tier+tags（纯 LLM 抽取 episodic/semantic/procedural + tags）；infer=true 不经 classifier（extractor 产派生时自定）。详见 F02 决策9。
- **`/v1/list` 收窄**：handler `_list` 用 `prefix="/memory/"` 直取，只返建索引的 Memory 记忆。详见 F02 决策10。

> 开关由来与"为何默认不同步、为何经 Evolver 而非独立 Extractor"的取舍见 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)；write 路径流程见 [`S03-memory-manage.md`](S03-memory-manage.md)。

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

## 数据结构

### Scope —— 目标范围 vs 调用方身份（`common/type_def`）

`org > user > agent > session` 四维归属，同时支撑隔离与共享。各维默认 `""`。API 里 `Scope` 出现在两个**不同语义**的位置（均为 `Scope` 类型，勿混淆）：

- **目标范围（target）**：操作作用于「谁的」记忆——`scope` 参数（或 `Context.scope` / `DeleteSelector.scope`）。
- **调用方身份（identity）**：「谁」在发起调用——`identity` 参数（必填 keyword-only）。

### Context（`common/type_def/context.py`）

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `scope` | Scope | 空 Scope | 检索目标范围（多租户隔离） |
| `extensions` | dict[str, str] | `{}` | 调用方自定义透传配置，值须为传输安全的 str；约定 key `"max_tokens"` 表示自适应披露 token 预算，由 API 边界解析为 `RetrievalQuery.max_tokens` |

> `extensions["max_tokens"]` 是 API 边界解释的约定 key，解析后从透传 extensions 中移除；无此 key 或空串时披露阶段使用默认策略。

### MemoryUnit / Segment（读取类方法返回，`common/type_def/memory.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | 全局唯一 id（每条记忆/每个版本各一） |
| `scope` | Scope | 归属（unit 级单一 owner，隔离/存储命名空间键） |
| `tier` | MemoryTier | 认知角色分类 |
| `segments` | list[Segment] | 多段内容投影（每段 = content + assets + source） |
| `source_ref` | str | 来源引用（RawPayload id / 会话 id，可溯源） |
| `temporal` | Temporal | 双时间：`t_event`/`t_ingest`/`t_valid`/`t_invalid` |
| `provenance` | list[str] | 演进血缘（多→一合成）：由哪些 unit 提取/升华/合并而来 |
| `supersedes` | str | 版本链（一→一更替）：本版本取代的上一版 id；空表示首版 |
| `tags` | list[str] | 标签（检索前置过滤用） |
| `metadata` | dict[str, str] | 其他元数据 |
| `lifecycle` | LifecycleState | 生命周期状态 |

`Segment`：`content`（可治理文本/结构投影，索引与检索对象）、`assets`（本段原模态资产引用）、`source`（本段来源 Modality）。便捷只读折叠属性：`unit.content`（各段换行连接）、`unit.assets`（各段扁平合并）、`unit.source`（首段模态）——返回新对象，勿就地 `append`。

### MemoryPatch / UpdateMode（update，`control/types.py`）

`MemoryPatch` 仅**非 None** 字段生效：`content` / `tier` / `tags`（整体替换）/ `metadata`（合并）/ `t_valid` / `t_invalid` / `mode`。

`UpdateMode`：`SUPERSEDE`（默认、非破坏式，新 id 新版本 + 旧版标 superseded）/ `OVERWRITE`（原地覆写、同 id，旧内容仅留审计）。

### DeleteSelector / DeleteMode（delete，`control/types.py`）

`DeleteSelector` 各条件取「与」，至少给一项：`unit_ids` / `scope`（鉴权依据）/ `tags`（命中任一）/ `before`（`t_event` 早于此）/ `mode`。

`DeleteMode`：`FORGET`（标记遗忘，可恢复，默认）/ `ARCHIVE`（归档转冷）/ `DOWNWEIGHT`（保持 active 仅降权）/ `PURGE`（物理删真源与索引，合规硬删，不可恢复）。

### FilterClause / FilterOp（recall 前置过滤，`common/type_def`）

单条 `FilterClause` = 一个谓词：`field`（标签维度 / 元数据 key / 标量字段名）、`op`、`value`。多条放进列表语义为 **AND**（取交），字段内「或」用 `IN`。`FilterOp`：`EQ`/`NE`/`IN`（value 为 list）/`NOT_IN`/`GT`/`GTE`/`LT`/`LTE`/`CONTAINS`（列表型字段含某元素）。scope 不走 filters——它是记录/查询上的专用隔离字段。

### DisclosureLevel / RetrievalResult（recall 返回，`retrieval/types.py`）

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

### AuditEvent（audit 返回，`common/type_def/audit.py`）

`id` / `actor`（操作者 Scope）/ `action` / `target_id` / `layer`（产生事件的层）/ `occurred_at` / `detail`。`detail` 常见约定包括 `permission_check`、`permission_reason`、`job_id`、`before_unit_id` / `after_unit_id`、`before_unit_ids` / `after_unit_ids`；其中 `before_unit_*` / `after_unit_*` 仅表示记忆单元 id，不用于调度任务 id。

## 错误语义

均继承自 `common.errors.AgentMemoryError`：

| 异常 | 触发场景 |
|------|----------|
| `PermissionDeniedError` | 鉴权不通过（identity 对 target scope 无相应 Action 权限） |
| `NotFoundError` | `get` 等按 id 读取但记忆不存在 |
| `ValidationError` | 入参非法（如 `recall` 的 `top_k <= 0`） |
| `PolicyError` | `admin_set` 的键未知或为不可变配置 |
| `ConflictError` | 写入冲突（如 id 重复） |
| `BackendError` / `HealthCheckError` | 后端故障 / 健康探测失败 |

## 鉴权流程

```
调用方 → MemoryAPI.method(scope=target, identity=caller)
  → PermissionManager.check(actor=identity, target=scope, action=<对应动作>)
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
| S01-ingest_access | write 路径中 Engine 内部调用 Ingestor |
| S03-memory_manage | 数据面委托 MemoryEngine，治理/授权/调度面委托对应算子 |
| S04-retrieval | recall 路径中 Engine 委托 Retriever |
| architecture.md §9 | 记忆接口层语义定义 |
