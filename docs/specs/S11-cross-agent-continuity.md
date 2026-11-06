# S11 — 跨 Agent 接入、关系扩展与任务接续契约

## 元信息

| 项 | 值 |
| --- | --- |
| 状态 | 2026-11-05 设计修订已实现：两档模式、接续参数并入 `add`/`batch_add`/`search`、关系真源改存 Registry KV、`list` 过滤下推、接口改名与删减；剩余实现差距见 [F10 12.2](../features/construction/F10-role-memory.md#122-实现差距)。真实产品 G0 联调、外部 Agent 记忆统一导入与真实存量验收待完成 |
| 关联模块 | jiuwen_memory_adapter/、jiuwen_memory_entry/、jiuwen_memory/api/、jiuwen_memory/control/、jiuwen_memory/common/、jiuwen_memory/storage/ |
| 最近一次修订日期 | 2026-11-06 |
| 关联特性文档 | [F10-role-memory](../features/construction/F10-role-memory.md) |
| 关联规约 | [S02](S02-memory-api.md)、[S03](S03-control.md)、[S04](S04-retrieval.md)、[S06](S06-storage.md)、[S07](S07-common.md)、[S09](S09-plugin-development.md) |

## 范围 / 边界

规定三方接入需要提供的认证、事件、任务绑定、历史导入与回写语义，以及 agent-memory 负责的任务登记、职责关系扩展和结构化视图。本规约定义跨模块边界；接续能力以**可选参数**扩展 `add` / `batch_add`（`continuity`）与 `search`（`duty` / `task_id` / `anchor` / `view_budget`），参数缺省时两者行为与改动前完全一致；`get` 签名不变。专用入口只保留任务登记（`continuity_task`）、关系登记（`continuity_relation`）、核对恢复（`continuity_recover`）与治理（`quarantine_source` / `release_source`）。不规定产品内部任务系统，不提供通用工作流或自动执行锁，不提供跨空间发布与增量同步游标。

首版从已保存记录恢复任务背景，前任可以离线；不要求交接单或接收确认。多个授权参与者可贡献记录，本规约不定义执行所有权或外部任务锁。

## 不变量

1. 调用身份来自认证链，`product`、职责名称、会话 ID 和业务字段均不能替代认证。每个连接配置映射到可信主体及连接标识；绑定标识不是访问令牌。
2. 全局任务属于一个组织内的项目空间。任务绑定和来源引用不授予原记忆访问权限，所有读取仍按真实 Scope 判权。
3. 本地会话是上下文，不与全局任务简单等同。一任务可关联多个会话；同一会话涉及多任务时必须显式选择绑定或事件范围，不按标题或时钟自动合并。
4. TaskRegistry 是任务 ID、绑定及背景引用的唯一真源；客户端缓存、记忆摘要和产品侧任务列表都不能覆盖登记结果。
5. 职责只选择关系扩展策略，不授予权限；图路径不自动证明结论，关系类型/方向必须明确，共现不能冒充因果。
6. 旧 MemoryUnit 只要有合法 Scope、ID 且可读，就可被精确引用；职责标签、task_id 和向量索引不是引用资格的前提。
7. 视图中的未知、缺证和部分失败必须结构化表达；空列表不等于完成检查。受限记录的身份、数量或 Scope 不得借缺口说明泄露。
8. 登记状态更新和回执要先持久化再返回成功。普通聊天的 best-effort 写入不能作为可靠写入确认。
9. 角色入口与其他公开方法一样向 PEP 传递完整 `RequestSecurityContext`，先验证来源、时效和凭据撤销状态；不能只传 actor。项目空间成员授权由 `space_aware` Authorizer 执行。
10. 角色入口沿用同步 MemoryAPI 契约；异步宿主通过工作线程调用（MCP 已如此接线）。关系修订和恢复判权使用已读取的关系记录与端点 MemoryUnit 的同一快照，不为判权额外读取另一版本。
11. 接续能力分两档：`continuity`（接续模式，不需要图存储）与 `role`（角色模式，需要图存储）。档位由配置显式声明，运行期不自动升降档；接续模式下不得伪装具备关系扩展（见[模式与能力矩阵](#模式与能力矩阵)）。

## 模式与能力矩阵

配置键 `continuity.default.params.mode` 取 `continuity` 或 `role`，**必填**；出现 `continuity` 命名空间而缺 `mode` 时启动失败。

- **接续模式（continuity）**：任务登记、可靠写入（含历史导入）、任务背景检索、来源治理、可选派生构建。不需要图存储。若同时配置了 `graph_store`、`relation_index_builder`、`role_view`、`relation_associator`、`conflict_resolver`、`relation_types*`、`duty_policies*`，启动时忽略并输出一次 warning，列出被忽略的键。
- **角色模式（role）**：接续模式全部能力 ＋ 关系登记、职责扩展、证据图投影、关系覆盖清单、可选候选关系发现与冲突消解。缺 `graph_store`、后端不支持有向邻接查询或条件投影、或缺 `role_view` 时启动失败。

| 能力 | continuity × 普通写入 | continuity × 文档模式 | role × 普通写入 | role × 文档模式 |
| --- | --- | --- | --- | --- |
| 任务登记 / 绑定（`continuity_task`） | ✓ | ✓ | ✓ | ✓ |
| 可靠写入（`add` / `batch_add` 的 `continuity` 参数） | ✓ | ✓ | ✓ | ✓ |
| 任务背景检索（`search` + `task_id`） | ✓ | ✓ | ✓ | ✓ |
| 来源隔离 / 解除 | ✓ | ✓ | ✓ | ✓ |
| 派生构建 | 可选 | ✗（无精确元数据查询，配置即启动失败） | 可选 | ✗（同左） |
| 关系登记（`continuity_relation`） | ✗ `UnsupportedCapabilityError` | ✗ | ✓ | ✓（关系真源在 KV，不受文档模式影响） |
| 职责扩展（`search` + 非 general `duty` / `anchor`） | 降级：普通结果 ＋ 非阻断 gap | 同左 | ✓ | ✓ |
| 候选关系发现 / 冲突消解 | ✗ | ✗ | 可选 | 可选 |

接续模式下 `search` 带非 general `duty` 或带 `anchor` 时不抛错：返回正常的任务背景与语义检索结果，`gaps` 追加非阻断 `relation_expansion_disabled`，`view_meta.mode="continuity"`。仅关系的入口（`continuity_relation`、`continuity_recover(kind=relation)`）返回 `UnsupportedCapabilityError`。MCP、JiuwenSwarm provider 与远程桥在接续模式下不注册 `continuity_relation` 工具；服务端能力发现返回 `continuity_mode`。

## 数据结构

本节给出字段级定义，含义、用法和示例见 [F10 第 5 节](../features/construction/F10-role-memory.md#5-数据结构)，两处小标题同名。公开 DTO 定义在 `jiuwen_memory/common/type_def/continuity.py`，经 `jiuwen_memory.api` 重导出；标为"内部"的结构是持久控制记录，不是公开 DTO，KV 路径为 `/continuity/{tasks,events,relations}/<key>` 与 `/continuity/coverage/relations`。

### `MemoryRef`

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `scope` | Scope（org / space / user / agent / session 五个字符串） | 必填 | 原记忆的真实五维 Scope |
| `unit_id` | string | 必填 | 实际 MemoryUnit ID，非空；不把 `/messages/` 原始来源 ID 冒充 unit ID |
| `content_digest` | string | 必填 | 64 位小写十六进制 SHA-256 |

摘要输入为 `segments` 的有序列表，每段仅含 content、assets（保留顺序）和 source 的枚举值；封装成键为 segments 的 JSON 对象，按键排序、无额外空白、保留 Unicode 字符而不做规范化，以 UTF-8 编码后计算 SHA-256。层级摘要、向量、生命周期和授权信息不入摘要，这些状态在每次读取时另行核验。旧记录不需先增加字段，首次关联时由服务端读取并计算摘要。读取时摘要不匹配报 `ConflictError`；被引用单元非 active 或不在有效时间内同样拒绝。

### `TaskRequest`

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `operation` | string | 必填 | create / get / find / bind / unbind / attach / close |
| `request_id` | string | `""` | 除 get、find 外必填；同键重试须内容一致 |
| `task_id` | string | `""` | create 由服务端生成；其他定点操作必填 |
| `expected_revision` | int | `0` | 非负；create 用 0 |
| `connector_id` | string | `""` | bind、find 使用；须是已配置连接 |
| `context_id` | string | `""` | bind、find 使用 |
| `context_source` | string | `"native"` | native / adapter |
| `binding_id` | string | `""` | unbind 指定 |
| `background` | MemoryRef[] | `[]` | 随任一变更登记的背景引用，提交前逐个读取校验 |
| `required` | bool | `true` | 本次 background 是否为必需证据 |
| `reason` | string | `""` | 解绑原因 |

### TaskRecord 与 Binding（内部）

TaskRecord（`/continuity/tasks/<task_id>`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 由负责人摘要与 create 的 `request_id` 计算 |
| `owner` | string | 创建者身份摘要；首版只有 owner 可修改任务，未实现委托表 |
| `revision` | int | 每次变更加一；事件提交关联背景时也递增 |
| `state` | string | open / closed；closed 后拒绝变更和新事件 |
| `bindings` | Binding[] | 见下 |
| `background` | `{ref: MemoryRef, required: bool}[]` | 按值去重 |
| `receipts` | `{request_id: {digest, revision}}` | 最多 256 条，超出按修订号淘汰最旧；淘汰后的旧修订请求仍因修订号不符被拒绝 |

Binding：`binding_id`（由任务与 bind 的 `request_id` 计算）、`connector_id`、`context_id`、`context_source`、`created_by`、`active`；解绑后增加 `revoked_by`、`revocation_reason`，记录保留。

### `ContinuityWrite`

`add` 与 `BatchWriteItem` 的可选参数 `continuity`，表示"这是一条要可靠登记到任务的事件"。缺省时写入行为与改动前一致。

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `task_id` | string | 必填 | 非空，任务须 open |
| `binding_id` | string | 必填 | 当前有效的绑定 |
| `event_id` | string | 必填 | 在该连接和绑定内稳定，重试复用；幂等键 = 连接、绑定、事件 ID 的摘要（即 `operation_id`） |
| `kind` | string | `"unclassified"` | observation / statement / judgment / result / experience / correction / unclassified |
| `classification_confidence` | string | `"unknown"` | low / high / unknown；low 时 `kind` 强制改为 unclassified |
| `evidence_refs` | MemoryRef[] | `[]` | 写入和重试时逐个核验 |
| `claimed_author` | string 或 null | `null` | 原文声称的作者，不是已认证作者 |
| `context_id` | string 或 null | `null` | 产品会话或上下文 ID |
| `source_locator` | string 或 null | `null` | 原始消息、工具输出或导入文件位置；写入 `MemoryUnit.source_ref` |
| `schema_version` | string | `"continuity-write/1"` | 其他值拒绝 |

字段映射：正文取 `add` 的 `content`；原始时间取 `add` 的 `event_time`（写 `temporal.t_event`），`occurred_at` 保持消息时间（t_message）语义；职责背景取 `tags`（`duty:<id>`），不产生权限；`kind` 写 `continuity_kind`，原始事件统一 `tier=episodic`（不经 classifier 改写）；其余字段由内部写路径注入 `continuity_*` 系统元数据（`continuity_task`、`continuity_binding`、`continuity_event`、`continuity_connector`、`continuity_confidence`、`continuity_evidence_refs`、`continuity_claimed_author`、`continuity_envelope_digest`）。公开调用方不得直接设置 `continuity_*`。

带 `continuity` 的写入：scope 必须是项目空间（user、agent、session 为空）；连接由认证链解析并核对属于该项目；只有这类写入使用幂等键，普通 `add` 不变。

### `ContinuityEvent`（交换格式）

`ContinuityEvent` 只作为 Adapter 的历史导入交换格式（JSONL 一行一个事件），由适配器或导入工具映射为 `add` / `batch_add` 的参数：`content` → `content`，`occurred_at` → `event_time`，`duty_ids` → `tags`，其余字段 → `ContinuityWrite`。

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `task_id` / `binding_id` / `event_id` | string | 必填 | 同 `ContinuityWrite` |
| `content` | string | 必填 | 非空，不含凭据 |
| `kind` / `classification_confidence` / `evidence_refs` / `claimed_author` / `source_locator` | — | 同 `ContinuityWrite` | 同 `ContinuityWrite` |
| `duty_ids` | string[] | `[]` | 职责背景，不产生权限 |
| `occurred_at` | string 或 null | `null` | RFC 3339，必须带时区；不用于决定全局顺序 |
| `schema_version` | string | `"continuity-event/1"` | 其他值拒绝 |

首版不提供来源内位置（`source_position` / `previous_position`）与增量同步游标，见 F10 12.3。

### 事件回执（内部）

`/continuity/events/<幂等键>`，幂等键为连接、绑定、事件 ID 的摘要（即返回的 `operation_id`）。

| 字段 | 说明 |
| --- | --- |
| `digest` | 事件全部字段的摘要；同键不同摘要返回冲突 |
| `status` | unknown / partial / committed |
| `refs` | 已保存的正文 MemoryRef 列表 |
| `connector`、`task_id` | 提交时的可信连接与任务 |
| `association_status` | 启用关系发现时：committed / partial |
| `relation_candidates` | 启用关系发现时：已记录的候选关系，重试复用，不重新发现 |
| `build_id`、`build_plan` | 启用派生构建时：构建批次标识和冻结的构建计划 |
| `derived`、`derivation_status`、`derivation_conflicts` | 启用派生构建时：实际输出、派生状态和冲突项 |

返回语义：`add` 返回类型仍为 `list[MemoryUnit]`。committed 时返回本次（或同键重放时已保存的）正文单元；同键异内容返回 `ConflictError`；回执为 unknown 或 partial 时抛 `WriteIncompleteError`（`PartialFailureError` 子类，HTTP 409），携带 `operation_id`、`status`（unknown / partial）、`refs`（已确认保存的 MemoryRef）与 `retry_action`（`retry_original_write` / `recover_then_retry`）。

提交顺序：先保存 unknown 回执 → 写正文 → 保存引用并转 partial → 把引用加入任务背景并递增修订 → 转 committed。这不是跨记录事务：背景已保存而最终回执未保存时，同键重试复用引用，按值去重补齐背景，不重写正文；任务修订号可能多递增一次。未保存可靠引用时保持 unknown，必须通过恢复接口核对。关系发现失败不撤销已提交事件，只把 `association_status` 置为 partial 并记录不含原文和异常消息的错误类别。

派生构建：原始事件持久化之后，抽取、去重、巩固和关系发现作为第二阶段。回执另记 `build_id`（事件幂等键 ＋ 构建器与模型版本摘要）、`derivation_status`（pending / committed / partial / failed）、冻结的构建计划，以及 `derived` 列表（每项 `{output_key, ref, action}`，`action` 为 created / reused）。计划持久化前禁止任何派生写入；已有计划则复用、不重新调用模型，没有计划时允许重新生成；同一 `build_id` 只能以"尚无计划"为条件写入一份计划（由单写者 Registry 串行保证）；新构建器或模型版本发起的重建产生新的 `build_id`。created 新建输出在系统元数据带 `continuity_build`、`continuity_output`；reused 保留目标既有标识。核对入口对 created 校验批次、输出标识与计划摘要，对 reused 校验冻结目标引用及其摘要，不接受其他内容版本。恢复规则：created 以 `system_metadata.continuity_output` 在真源上做精确等值查询（不依赖向量或全文索引），查到已存在且摘要一致则采用，否则新建，摘要不一致标冲突；reused 的来源关联以"事件 ＋ 被复用记忆"为键，集合语义。前置能力：写入链须保证系统元数据等值查询写后可读，缺失时不得启用派生构建。首版不提供 updated（原地更新已有记忆），也不依赖 KV CAS 或带期望摘要的条件更新，见 F10 12.3。`continuity_recover` 另支持 `kind=derivation`（只核对、不写新内容）。零条派生视为 committed；派生记忆来源指向原始事件引用；派生失败不影响原始事件已提交的结论。

### `RelationRecord` 与 `RelationStatus`

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `relation_id` | string | 必填 | 非空，不得以 `continuity:` 开头 |
| `source` / `target` | MemoryRef | 必填 | 不允许自环；修订不能更换 |
| `relation_type` | string | 必填 | 关系目录中已注册的类型；修订不能更换 |
| `revision` | int | `1` | 正整数，须等于 `expected_revision + 1` |
| `status` | RelationStatus | `candidate` | candidate / confirmed / needs_review / revoked；读取时 proposed→candidate、retracted→revoked |
| `evidence_refs` | MemoryRef[] | `[]` | confirmed 时必须非空 |
| `conditions` | string[] | `[]` | verifies 时必须非空 |
| `schema_version` | string | `"memory-relation/1"` | 其他值拒绝 |
| `model_version` | string | `"memory-relations/1"` | 其他值拒绝 |
| `batch_id` | string | `""` | 补建批次，供按批撤回核对 |
| `conflict_kind` | string | `""` | factual / temporal / advisory / 空 |
| `resolution` | string | `""` | prefer_a / prefer_b / both_valid / escalate / defer；仅 contradicts 可填且须有 `resolution_reason`；advisory 不得为 prefer_a / prefer_b |
| `resolution_reason` | string | `""` | 裁决依据说明 |
| `resolution_origin` | string | `""` | 由服务端设置：显式提交裁决为 human，自动策略为 automatic，模型候选为 model_candidate；客户端填写的值不作为信任依据 |
| `resolution_refs` | MemoryRef[] | `[]` | 自动选胜者时依据的正向支持关系（同条件、已确认、带证据的 `supports`）引用；每次使用回查其当前状态，被修订或撤回后裁决失效。`verifies` 只说明做过验证、不编码结果，不能单独作为选胜依据 |

写入主体、时间及关系真实归属由服务端保存。端点的 `continuity_kind` 须在关系类型允许的端点类别内。后续自动修订未提供裁决时，保留此前的人工裁决。

### 关系头与修订历史（内部）

关系真源存于 Registry KV，**不是** MemoryUnit（F10 4.1 的例外，理由见 F10 4.1 与第 10 节）。项目 Scope 隔离：

| KV 路径 | 内容 |
| --- | --- |
| `/continuity/relations/<relation_id>` | 关系头：`revision`、`digest`、`payload_digest`、`status`、`connector`、`current` |
| `/continuity/relations/<relation_id>/rev/<n>` | 第 n 修订的完整 RelationRecord，写入后不可变 |

| 关系头字段 | 说明 |
| --- | --- |
| `revision` | 当前修订号 |
| `digest` | 本次请求关系的摘要；同修订同内容重试只补投影 |
| `payload_digest` | 实际落盘的关系内容（含服务端补充的裁决字段）的摘要，供投影核对 |
| `status` | projecting（真源已提交、投影未完成）/ committed |
| `connector` | 提交该修订的可信连接；连接停用或隔离时该关系不可用 |
| `current` | 当前修订完整 RelationRecord（含服务端裁决字段与写入主体、时间） |

写入顺序：先写不可变修订记录，再原子替换关系头（单记录原子替换即真源提交点，不再有 unknown 状态），然后条件投影，投影完成后置 committed。并发沿用单写者 Registry（`claim_single_writer`）与 `expected_revision` 校验，不依赖 KV CAS。

读取判权：调用方对项目 Scope 有 READ，关系两端及证据均可读，提交连接未停用、未隔离。关系不进入普通 `search` / `list` / `get` 的候选集，也不进入 Evolve。

图投影：节点 ID 为 `continuity:memory:` 加 Scope 与 unit ID 的摘要，边 ID 为 `continuity:relation:<relation_id>`，边属性保存 `relation_id` 与 `revision`（不再保存关系 MemoryRef），视图回查 KV 关系头核验当前状态。投影由 `evidence_graph` IndexBuilder 以 RETRIEVAL_ONLY 模式完成；撤回时删除对应边。旧的无前缀投影不再作为角色关系使用，需要重建；存量关系 MemoryUnit 由一次性迁移搬入 KV 并重建投影（F10 12.2）。

### `EvidenceRelation`

内置六类：supports、contradicts、possible_cause_of、verification_method_for、verifies、requires，语义与端点约束见"关系记录与职责扩展契约 / 关系模型"。

### `RelationCatalog`、`RelationTypeSpec`、`TraversalRule`

`RelationTypeSpec`：

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `name` | string | 必填 | 非空，全局唯一 |
| `display_name` | string | 必填 | 展示名 |
| `description` | string | 必填 | 给模型和人工审阅的说明 |
| `direction` | string | `"forward"` | forward / backward / both |
| `symmetric` | bool | `false` | 为 true 时 direction 必须为 both，反之亦然 |
| `transitive` | bool | `false` | 首版有界展开，不做传递闭包 |
| `valid_source_kinds` / `valid_target_kinds` | string[] | 全部类别 | 非空，取自 observation / statement / judgment / result / experience / correction / unclassified / method / plan / action |

`TraversalRule`：

| 字段 | 类型 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `edge_type` | string | 必填 | 已注册的关系类型 |
| `direction` | string | `"both"` | forward / backward / both；forward 对应出边，backward 对应入边 |
| `priority` | int | `1` | 非负，越小越先展开 |
| `max_depth` | int | `2` | 1–4 |

`RelationCatalog` 在装配 `continuity.local` 时由 `relation_types` 与 `duty_policies`（内联字典）或 `relation_types_path` 与 `duty_policies_path`（YAML 文件）构造，同一项不能同时用两种方式。内置六类关系只能覆盖 `display_name`、`description`；职责策略为 `{名称: {traversal: [TraversalRule...], budget: ViewBudget}}`，每个策略强制追加 `TraversalRule("contradicts", "both", 0, 4)` 与 `TraversalRule("requires", "forward", 0, 4)`，配置无法删除或削弱。未知关系名、未知端点类别或策略引用未注册类型时启动失败；运行时不动态注册。`version` 为配置内容摘要，作为视图的 `policy_version`。构建侧与查询侧注入同一实例。

### `Duty`

内置 general、diagnosis、testing、execution，默认展开规则：

| 策略 | 规则 |
| --- | --- |
| general | 所有已注册类型，both |
| diagnosis | possible_cause_of both；supports backward |
| testing | verification_method_for backward；verifies backward |
| execution | requires forward；verifies backward |

查询 `duty` 为一个策略名，或去重后 1–4 个策略名的列表，可取配置新增的策略名；任一未注册返回 `ValidationError`，不静默忽略。业务 `duty_ids` 与经验适用职责可多值，查询 duty 只选择遍历策略，不是业务身份，也不授予权限。配置可用同名职责覆盖内置策略的展开规则，强制检查仍会追加。

多职责规则：每个职责保留自己的 TraversalRule，不合并成一张表；第 k 跳的边被职责 d 允许，当且仅当 d 的某条规则匹配其类型与实际方向且 k 小于该规则的 `max_depth`；被至少一个职责允许即展开，`step.matched_duties` 按此逐职责计算。遍历单位是路径状态（节点，以及每个仍有效职责的剩余深度）；同一节点上，若 S 的有效职责集合包含 T 的且对 T 的每个职责剩余深度不小于 T，则 S 支配 T，被支配状态不展开、可被替代，互不支配的状态都保留；有效职责集合为空后，后续每跳沿任一职责在当前跳数下仍允许的边前进，路径有效职责保持为空，跨职责状态每节点只保留剩余深度最大的一个；单节点至多保留 k＋1 个互不支配状态，这是主动截断上限，达到时须报告 `unchecked_dependencies` 并把受影响职责标为 truncated；正文按 MemoryRef 去重为一个 视图条目，后续到达追加 `path_ids` 与 `valid_duties`；边的真源回查与判权结果缓存复用，不用全局已见边截断路径。`path.valid_duties` 为各步允许职责的交集，可为空（跨职责路径照常返回）；视图条目 的 `valid_duties` 为到达路径之并。预算共享：生效预算 = 请求预算与各职责策略预算中最大者取较小值；总预算优先：边预算 B ≥ k 时每个职责先获得 ⌊B/(2k)⌋（至少 1 条）的最低配额，B < k 时按传入顺序轮转每职责至多 1 条直至用完，未分到预算的职责为 not_started；剩余预算按合并优先级轮转分配；共享边的回查、判权和预算只计一次，但计入所有允许它的职责的覆盖统计；`meta.duty_coverage` 给出每个职责的 `status`（complete / truncated / not_started）、展开边数与截断原因；complete 只表示本次有界展开未被截断，不表示证据已完整覆盖。任务背景、anchor、语义候选、强制检查产生的反证与风险标记 `common=true`，与全部缺口一起始终保留；按单职责还原时保留 `common` 条目、全部缺口和 `valid_duties` 含该职责的路径，结果不保证与单独查询完全相同。

### `search` 的接续参数与 `ViewBudget`

`search(query, context, ..., duty: str | list[str] | None = None, task_id: str | None = None, anchor: MemoryRef | None = None, view_budget: ViewBudget | None = None)`。`duty`、`task_id`、`anchor` 均为空时与改动前行为完全一致；任一非空即进入任务 / 职责视图路径，此时 `query`（去空白后）、`anchor`、`task_id` 至少一项非空，仅给 `task_id` 或 `anchor` 时 `query` 可为空串，`duty` 缺省解析为 general。首版不支持跨空间职责视图：`context.extensions` 带 `spaces` 键且同时给出 `duty` / `task_id` / `anchor` 时返回 `ValidationError`。

`ViewBudget`：

| 字段 | 默认值 | 上限 |
| --- | --- | --- |
| `max_depth` | 2 | 4 |
| `max_nodes` | 30 | 200 |
| `max_edges` | 60 | 500 |
| `max_paths` | 60 | 500 |
| `timeout_ms` | 1000 | 10000 |

均为正整数；生效预算取请求值与职责策略预算的较小值。内置职责策略未单独配置预算时取默认值 2 / 30 / 60 / 1000，此时请求更大的值不会生效；需要放宽须在职责策略中配置 `budget`。`timeout_ms` 只限制继续展开，不强行中断已进入的同步存储调用，部署还须配置底层 I/O 超时。

### 职责视图结果（`role-view/2`，`RetrievalResult` 可选字段）

职责视图不再是独立返回类型，而是 `RetrievalResult` 与 `RetrievedItem` 上的可选字段，仅当 `duty` / `task_id` / `anchor` 给出时填充；普通检索这些字段为 null。MCP/HTTP 传输适配必须保留其语义，Markdown 只是附加呈现。

| `RetrievalResult` 新增字段 | 类型 | 约束 |
| --- | --- | --- |
| `paths` | RelationPath[] 或 null | 有依据且已授权的扩展路径；接续模式为空列表 |
| `gaps` | Gap[] 或 null | 视图路径下必填（可为空列表）；不能省略 gaps 来表示"不知道" |
| `view_meta` | ViewMeta 或 null | 视图路径下必填，见下 |

`RetrievedItem.view: ViewAnnotation | None`：视图路径下每个条目必填。原 role-view/1 的 facts / judgments / experiences / open_issues / risks 分组改由 `view.kind` 与 `view.status` 表达，客户端按需分组。

`ViewAnnotation`：`kind`（observation / statement / inference / suggestion / decision / experience / open_issue / risk）、`status`（observed / reported / candidate / supported_in_conditions / needs_review / refuted / unknown）、`classification_confidence`、`evidence_refs`、`conditions`、`duty_ids`（经验适用职责，非作者身份）、`discovery`（background / anchor / relation / semantic_candidate）、`path_ids`、`valid_duties: string[]`、`common: bool`。条目身份为 `RetrievedItem.unit_id`。`reported` 仅表示来源陈述；经验条目默认 `candidate`；无证据时不得标为 supported_in_conditions，该状态的 conditions 非空。

排序：background → anchor → relation → semantic_candidate，组内按原检索得分；`top_k` 截断 items 时追加非阻断 `budget_exhausted` gap，且必需背景不被语义候选挤出。

supported_in_conditions 由已确认 `supports` 关系提升其语义 target 的 inference 或 experience 条目：关系、两端及证据均可读，且 evidence_refs 与 conditions 非空。反向遍历不颠倒被支持者；条目携带该关系的条件、证据与路径。多条支持路径保留各自条件，不拼成无条件结论。`verifies` 本身不编码结果是支持还是否定，不能仅凭该关系提升状态。待复核反证及未解决冲突优先降级为 needs_review，明确反驳保留 refuted；关系撤回后重新计算。

`RelationPath`：`path_id`（视图内唯一）、`anchor`（MemoryRef）、`valid_duties: string[]`、`steps`（有序列表，每步含 `relation_id`、`revision`、`relation_type`、`direction`（in / out，遍历方向，不改变关系固有方向）、`source`、`target`、`status`、`evidence_refs`、`conditions`、`matched_duties: string[]`）。关系真源在 KV，步内不再携带关系 MemoryRef。路径可共享节点；正文按引用去重，不从摘要重新抽取。

`Gap`：`code`、`message`（经脱敏）、`blocking`、`affected_item_ids`（取 `unit_id`）。code 取 missing_input / reference_unavailable / unchecked_dependencies / budget_exhausted / retrieval_partial / environment_unconfirmed / review_pending / unresolved_conflict / conflict_escalated / relation_coverage_unknown / graph_unavailable / relation_expansion_disabled；reference_unavailable 不对无权者区分不存在和受限；relation_expansion_disabled 仅在接续模式出现，非阻断。

| ViewMeta 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | string | `role-view/2`；未知主版本拒绝解析 |
| `mode` | string | continuity / role，本服务的接续档位 |
| `task_id` | string 或 null | 无任务的记忆扩展返回 null |
| `anchors` | MemoryRef[] | 实际使用的可读起点 |
| `based_on_handoff` | MemoryRef 或 null | 基于哪个明确交接；null 不等于整个历史已检查 |
| `generated_at` | RFC 3339 string | 生成时间，不是三方同步水位 |
| `registry_revision` | integer 或 null | 本次任务登记修订；无任务时 null |
| `duty` | string | 实际解析的职责；多职责时为列表第一个 |
| `duties` | string[] | 实际解析的全部职责 |
| `duty_coverage` | object[] | 每个职责的 `duty`、`status`（complete / truncated / not_started）、`expanded_edges`、`reason`；接续模式为空列表 |
| `policy_version` / `model_version` | string | 所选策略（组合）及关系模型版本；接续模式为空串 |
| `effective_budget` | object | 生效的深度、节点、边和耗时上限 |
| `relation_coverage` | covered_for_requested_scope / partial / unknown | 仅描述已登记关系的构建与投影范围；无登记关系或接续模式为 unknown，登记关系全部投影追平为 covered_for_requested_scope，否则 partial |
| `completeness` | complete_for_requested_scope / partial | 首版固定 partial：遍历结束不能证明摄入覆盖或环境已核实，不依据 gaps 为空升级 |
| `continuation_ready` | boolean | 必需证据及已声明环境条件是否满足；首版不提供环境核验，固定 false。仅为信息就绪提示，不授权执行 |
| `unchecked_scope` | string[] | 语义范围，例如 additional_history / relation_coverage / environment；不是未授权空间 ID 列表 |

首版不提供 `ViewMeta.review` 复核聚合与影响传播（见 F10 12.3）。

### `Resolution` 与 `ConflictResolution`

`ConflictResolver.resolve(memory_a, memory_b, edge, context) -> Resolution`，定义在 `jiuwen_memory/construction/conflict_resolver.py`。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `decision` | ConflictResolution | prefer_a（source 胜）/ prefer_b（target 胜）/ both_valid / escalate / defer |
| `reason` | string | 裁决理由 |
| `candidate` | bool | 默认 false；true 表示模型候选，落库时 `resolution` 记为 escalate、`resolution_origin` 记为 model_candidate |

`context` 由构建侧提供已核验的事实：两端来源连接与事件时间、是否显式纠正、同条件下是否存在已确认的正向支持（`supports`）证据、证据检查是否完整、是否存在多方冲突。`verifies` 只说明做过验证，不编码验证结果，不能单独作为选胜依据；失败或不确定的验证结果不算正向支持，失败应以 `contradicts` 或撤销 `supports` 表达。胜负只对当前条件下的视图生效：不全局删除原记忆，也不删除反证边；被裁定失败的一方在该条件的视图中标为 refuted。advisory 差异一律 defer；冲突类型未分类、证据检查不完整或存在多方冲突时 escalate；模型结果只能是 both_valid 或 escalate。

### `ContinuityRecovery`

`continuity_recover` 的入参，覆盖未知写入核对与投影修复（MemoryAPI 没有公开的索引重建入口，关系投影修复也经此）。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `kind` | string | event / relation / derivation；relation 仅角色模式可用，接续模式返回 `UnsupportedCapabilityError` |
| `operation_id` | string | event、derivation 为回执的 `operation_id`；relation 为 `relation_id` |
| `refs` | MemoryRef[] | event 时非空，必须位于当前项目 Scope；relation、derivation 可为空 |

事件恢复核对每个引用的 `continuity_event`、信封摘要和拼接后的正文，一致时把回执置为 partial，返回 `next_action=retry_original_write`；调用方用原写入请求重试完成背景关联。关系恢复从 KV 读取关系头与当前修订，核对 `payload_digest` 后补建投影并置 committed（关系真源单记录提交，不需要客户端提供引用）。派生恢复只核对已保存计划和输出标识，不写业务正文。不匹配返回 `ConflictError`；恢复不创建第二份内容，也不自动认定写入失败。

### `continuity_connectors`

服务端配置 `globals.continuity_connectors`，类型为 `{连接名: {actor, projects, enabled}}`：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `actor` | 五维身份 | 必须与该连接的认证主体完全一致 |
| `projects` | string[] | 允许绑定和写入的 space |
| `enabled` | bool | 缺省视为启用；false 时拒绝该连接的新事件，并在普通读取、搜索、列表和视图中隐藏其来源内容（规则见[来源治理与列表过滤](#来源治理与列表过滤)） |

它是服务端身份映射，不是认证器，也不授予空间权限；不是 dict 时装配失败。

### 导入清单（manifest）

| 字段 | 说明 |
| --- | --- |
| `batch_id` | 导入批次 ID |
| `product` / `version` | 来源产品及版本 |
| `method` | user_supplied / native_export |
| `context_id` / `context_source` | 来源上下文 ID 及来源（导入时为 adapter） |
| `file_sha256` | 源文件摘要 |
| `record_count` | 事件数 |
| `missing_fields` | 缺失的可选字段（如 occurred_at、claimed_author） |
| `coverage` | 当前固定 selected_events_only |

## Adapter 接入契约

### 认证与能力声明

| 项 | 必须提供 | 不满足时 |
| --- | --- | --- |
| 认证 | 远程连接使用 TLS 和服务端可验证的 Bearer/API 凭据；本地桥接从受控凭据存储取得同等身份上下文。凭据不写入事件/提示词 | 拒绝业务调用；不得切为匿名或 dev 身份 |
| 连接身份 | 服务端登记的 `connector_id` 及其所属主体；产品本地账号映射由可信配置确认 | 不以用户提供的产品名推导主体 |
| 工具调用 | 能以接续参数调用写入与检索（`memory_add` / `memory_search`）、任务登记/绑定查询，角色模式下还能登记关系，并接收结构化结果 | 不能标记为具备接续能力 |
| 上下文绑定 | 原生上下文 ID，或由适配器生成并持久化的上下文别名；同时标明 ID 来源 | 不声称别名是产品原生会话 ID |
| 记录可靠性 | 稳定事件/重试键、写入结果及失败状态可取得；无法自动采集时由显式工具提交 | 不把未回写的执行结果列为已交接 |
| 历史能力 | 声明 `native_export`、`user_supplied` 或 `unsupported`；记录实际覆盖范围 | 不承诺完整历史；不阻断从新任务开始的在线接续 |
| 生命周期钩子 | 声明是否有前置读取、轮次回写和结束钩子 | 无钩子时使用显式工具调用，不虚构自动捕获 |

上述能力在连接配置/版本确定后声明；临时认证或网络故障作为操作失败，不通过修改能力声明掩盖。Agent 主动工具模式需要用户或接入指引明确触发；未发生工具调用时，不能认为事件已被捕获或交接已完成。

### 事件与回写信封

事件体即 `add` / `BatchWriteItem` 的正文与 [`ContinuityWrite`](#continuitywrite)（历史导入时由 [`ContinuityEvent`](#continuityevent交换格式) 映射而来），认证上下文与事件体分开。

服务端从认证链和绑定解析可信连接、提交者、任务与落点；若请求显式携带这些字段，须核对一致，不能覆盖可信值。稳定事件键的作用域包含组织、目标空间、可信连接与绑定；同键同内容重试不得增加独立证据计数，同键异内容返回冲突。

回写结果至少区分 `committed` / `partial` / `unknown` / `rejected`：`add` 以返回值（committed）、`WriteIncompleteError`（partial / unknown）与普通错误（rejected）表达，`batch_add` 在每个 `BatchWriteOutcome` 的 `continuity_status` 上表达；均返回已确认保存的 MemoryRef 与结构化错误，原始来源和派生单元引用分开。`unknown` 先用 `continuity_recover` 核对再重试。不能仅返回 Markdown、“已记住”或一个后台 job ID 充当持久化确认。

### 批量可靠写入（`batch_add`）

外部 Agent 历史导入与高频回写使用 `batch_add`，`BatchWriteItem.continuity: ContinuityWrite | None` 逐项声明。

- 批级 `scope` 与逐项 scope 必须是同一项目空间；同一批可包含不同任务。
- 已有 `BatchWriteItem.idempotency_key` 与 `continuity` 同时出现时，两者须一致，否则该项拒绝；以 `continuity` 幂等键为准。
- 分阶段处理：① 批量写入全部项的 unknown 回执 → ② 正文走现有批量写入链（批量向量化与索引）→ ③ 批量转 partial 并保存引用 → ④ 按任务分组，每个任务**只追加一次**背景并递增一次修订 → ⑤ 批量转 committed。派生构建与关系发现按项排入后续阶段，不阻塞批写返回。
- `BatchWriteOutcome` 新增 `operation_id` 与 `continuity_status`（committed / partial / unknown / rejected）；单项失败按 `continue_on_error` 处理，不中断其他项。
- 整批重放安全：已 committed 的项返回已保存单元，partial 的项补齐背景，unknown 的项须先核对。不同项的提交不构成事务，任务修订号可能比项数少（按任务合并），也可能因重试多递增一次。

### 历史导入交换格式

Adapter 交付 UTF-8 JSONL：每行一个 [`ContinuityEvent`](#continuityevent交换格式)，导入工具按批映射为 `batch_add`；附批次清单，包含来源产品和版本、导出/采集方式、上下文 ID 与来源、事件范围、批次 ID、文件摘要、记录数及缺失字段。原生导出为每会话一行时由适配器拆成事件并保留原文件及行内定位。

本地无稳定事件 ID 时，使用固定导入批次标识＋源文件摘要＋行/事件序号生成可重试键，不能用文本相同推断是同一事件。用户手工提供片段标为 `user_supplied`，不能宣称完整会话或可信工具执行反馈。源文件变化作为新批次，跨批去重需来源映射，不保证自动识别所有重复。

导入只增加记忆，不改变任务管理权或恢复验证状态。覆盖缺口、冲突和失败清单随结果返回。

### 外部记忆导入契约

外部 Agent（OpenClaw、Hermes、WorkBuddy、DeepSeek Harness 等）自有记忆读入 agent-memory 时遵守本契约，流程与取舍见 [F10 第 8 节](../features/construction/F10-role-memory.md#8-外部-agent-记忆迁移)。

- **适配器输出**：一份[导入清单](#导入清单manifest) ＋ 一份 `ContinuityEvent` JSONL（UTF-8，每行一个事件），由导入工具分批调用 `batch_add` 写入。
- **字段映射**：原始位置写入 `source_locator`（产品、批次、文件摘要、行号或事件序号）；原文作者写入 `claimed_author`；原始时间写入 `occurred_at` 并带时区，未知则为 null 并列入清单的 `missing_fields`；能判断的类型写入 `kind`，判断依据不足时为 unclassified 或 `classification_confidence=low`。
- **事件键**：产品有稳定事件 ID 时使用之；没有时用批次 ID、源文件摘要、行号或事件序号计算，不能用文本相同推断同一事件。
- **服务端要求**：原文作为来源保存；派生内容进入统一的抽取、去重、巩固、关系发现与冲突消解；独立来源计数在去重后保留，同一来源的多次导出或转述不计为多个来源；导入者为认证主体，`claimed_author` 不升级为认证身份；每批可按批次 ID 追溯与回滚，回滚不删除批次之后他人新增或修订的数据。
- **实现状态**：配置派生构建后，导入事件进入统一抽取与精确去重；未配置时只保存原文，语义巩固未接通，产品原生格式转换器尚未提供。实现细节与差距见 F10 8.3、12.2。

## TaskRegistry：任务登记与上下文绑定

### 对外操作语义

| 操作 | 行为 | 权限与错误 |
| --- | --- | --- |
| 注册任务 | 创建稳定 task_id，保存组织、项目空间、负责人及目标引用；初始为 open，可同时登记背景引用；绑定需另行 bind | 需要项目 WRITE；背景引用逐个读取校验 |
| 绑定上下文 | 将可信连接＋本地上下文及 ID 来源关联任务，返回 binding_id | 首版仅创建负责人可建立；须验证连接属于本组织且允许该项目 |
| 查询任务/绑定 | 按 task_id 或连接＋上下文读取有权查看的映射及当前修订 | 多个匹配不得默选；背景引用按源权限裁剪，不暴露其他项目绑定或私有来源 |
| 解除绑定 | 软撤销映射，记录操作者、原因和修订，历史关联仍可审计 | 解除不等于删除历史或撤销源记录授权 |
| 关联已有记忆 | 将有权引用的 MemoryRef 挂到任务的背景清单，标注用途和范围 | 校验任务管理权和来源读取权；不复制正文、不改源 Scope，不授予他人读取权 |
| 关闭任务 | 标记 closed，保留历史；新事件不能隐式重开 | 首版仅创建负责人，带 expected_revision |

任务标识由 agent-memory 生成，用户或脚本负责显式绑定；Adapter 负责解析并回传绑定，产品无需实现“跨产品任务等价”API。一会话多任务默认使用不同绑定，未提供绑定而无法唯一解析时拒绝。解除后重新关联创建新绑定，不能让迟到事件被静默写入另一个任务。

背景清单在控制真源中区分必需与可选引用。对外查询按源授权裁剪，不意味着内部删除引用：视图和可选背景快照的受信任核验仍检查必需集合。任何必需引用因无权或缺失不可用时，返回不含引用身份的通用 blocking gap，continuation_ready=false；不得通过先过滤、再对剩余集合宣布完整来绕过条件。可选引用未能读取时仅显示不泄露来源身份的覆盖限制。

TaskRecord 与 Binding 的字段见[数据结构](#taskrecord-与-binding内部)。首版绑定归入任务控制记录一并持久化；按本地上下文反查的索引（若建立）仅是可重建投影，缺失或滞后时回查控制真源，不能据旧索引接受已经解除的绑定。记录不得包含第三方登录凭据。

### 首版存储和部署约束

这是 **control 管理的数据域**，通过公开 TaskRegistry 端口访问；不把登记记录当普通 MemoryUnit 交给抽取、向量检索或 dreaming。数据经 StoreManager 的显式具名持久端口保存，不能在 Adapter 私建数据库。管理域必须执行同组织/项目授权、审计和保留策略，生命周期由装配根管理；这些是 S09 统一门面例外所需的明确边界。

首版采用**一个有权写入的 Registry 服务实例**，按 task 串行处理修订检查及持久写入；持久端口需保证单任务控制记录原子替换和故障后可读。现有通用 KV 接口不声明 CAS，不能把“先 get 再 update”包装成跨实例原子保证。首版禁止多个写实例；无法保证单写部署或持久能力时拒绝启用。多写实例支持需另增条件写/租约等契约，本期不承担。

三方接续调用必须指向**同一个 agent-memory 服务中的 Registry 写入口**，不是各产品各自 assemble 一个 SDK 内核再共享同一个 KV。独立 SDK 实例只可维持原有非接续模式；不得成为第二个 Registry 写者或在远端失败后本地接管。单进程内嵌测试可共享同一具名 Registry 实例，但不替代三方真实集成的统一服务部署验证。

修改已有任务的请求须携带 expected_revision 和稳定 request_id；创建请求以调用身份、项目和 request_id 形成稳定创建键，避免重试创建多个任务。同修订并发更新只有一个成功，其余返回 conflict；在声明的回执保留窗口内，同一已提交请求返回既有结果，同 request_id 不同操作或内容返回冲突。回执过期后旧修订请求仍拒绝，不能当成新操作重新执行；调用方改为查询当前状态核对。重试也须重新认证授权，不回显已失去权限的内容。状态及操作回执作为同一任务控制记录提交，不能先报告成功再异步落盘。审计记录请求和实际结果，不把失败请求记为完成。

具名持久 KV 须声明单记录原子替换（`persistent_atomic_records`），并支持 `claim_single_writer(namespace)`，持有到 Store 关闭。SQLite 用独立文件锁拒绝第二写者，仅限单机文件系统，不支持跨主机网络文件系统多写；`allow_ephemeral` 仅用于测试。

装配与生命周期：角色模式下 Continuity 查询使用的 GraphStore 与关系投影写入的 GraphStore 必须是同一对象：两者由同一装配上下文按具名依赖解析，Continuity 装配时校验投影器实际持有的图与视图使用的图是同一对象（对象身份，端口名只用于声明依赖、不能仅靠字符串比较）；不一致即启动失败。IndexBuilder 以公开能力声明支持的写入模式（契约见 S05 `supported_write_modes`），只声明 RETRIEVAL_ONLY 的构建器（如关系投影器）配到主写入链位置时启动期拒绝，判断依据是能力声明而非 target 名或具体类型（S09 §9；能力声明已落地，主写入链的装配期拒绝检查仍未接入 Engine 装配）。Registry KV（含单写锁）、证据图及其 KV 由装配根持有，`runtime.close()` 按"停止接续准入 → 关闭 Continuity → 关闭图存储 → 关闭底层 KV"释放，共享 KV 只关闭一次（按对象身份去重），单写锁随 KV 关闭释放，关闭后同进程可重新装配；借用资源不由当前装配根关闭；装配中途失败只释放本次创建的资源与本次获取的单写锁。显式配置的可选组件（关系发现、LLM 冲突策略）启动期 health 失败即启动失败，运行期失败显式降级（`association_status=partial` 或 escalate）。重新装配与同一图对象校验已落地并经验证（`runtime.close()` 后重装成功、投影器/视图图不一致启动失败）；装配中途失败回滚与借用资源区分仍待验证。

图存储后端（仅角色模式）：角色图要求 GraphStore 支持有向邻接查询、同一对端点多条边、五维 Scope 隔离和单次写操作原子提交；首版须提供至少一个满足这些要求的真实图数据库后端用于集成测试和部署（Neo4j），不能只以 KV 投影或内存实现验收。不同后端须通过同一套 GraphStore 契约测试。关系投影采用按修订的条件投影（GraphStore 可选能力，见 S06）：单事务内按确定性边 ID 比较修订，新于当前则创建或原地更新，相等视为完成，旧于当前拒绝覆盖；撤回仅删除修订不新于撤回修订的边，不删除节点；普通 insert 保持重复 ID 报冲突。真实后端验收须覆盖连接中断、提交确认丢失后的条件重放与重建、事务回滚和关闭后重新装配。

## 无交接接续与并发语义

新产品取得授权并绑定任务即可查询，不需要前任在线、prepare 或 accept。可选背景快照是普通有引用的记录，不转移执行权。任务背景缺失时返回 gaps，不能把未知当作未做或已完成。

Registry 的修订检查只保护映射和背景清单。多个绑定可并发写观察，事件按稳定键幂等；互相冲突的结果分别保留并标注，不按最后写入覆盖任务事实。已撤销绑定拒绝新事件；关闭任务后仅允许授权的历史查询和显式管理操作，不因迟到结果自动重开。正文保存与关联登记非跨库事务，部分失败返回已保存引用和未完成关联，按回执核对后重试。

## 关系记录与职责扩展契约

### 关系模型

下列类型是新增的证据关系语义，不声明与已有 RelationType 同名值兼容；适配必须核对方向及端点含义，不能直接混用。关系类型先定义、版本化；具体关系后续增量生成。首版端点使用 MemoryRef，实体索引作为定位入口，其规范实体标识需解析成受权记忆引用，不能直接冒充记忆 ID。

| relation_type | source → target 含义 | 端点约束 |
| --- | --- | --- |
| supports | 证据 → 判断 | 不能由相似度自动判定支持 |
| contradicts | 证据 → 判断 | 双向等价；保留条件，按下述消解契约处理 |
| possible_cause_of | 原因假设 → 观察到的问题 | 始终保留假设性质，确认关系不等于确认因果 |
| verification_method_for | 方法/用例 → 问题观察、判断或方案 | 描述如何验证，不表示执行过 |
| verifies | 结果 → 问题观察、判断、方案或用例 | 需注明验证条件及结果依据 |
| requires | 方案/行动 → 前置条件 | 是执行约束，不表示条件已满足 |

RelationRecord 字段与状态见[数据结构](#relationrecord-与-relationstatus)。确认表示关系经可定位依据或有权责任方确认，不把端点推断升级为外部事实；模型抽取默认 candidate，缺证不得自动 confirmed，candidate 只在显式未核实区呈现，不作为已验证路径。自定义关系名为字符串，启动目录校验未知类型、端点类别及策略引用；内置语义不可覆盖，只允许改展示名和说明。冲突消解：advisory 差异不自动合并；无法证明条件可比、存在多方冲突或证据检查不完整时不自动选胜者；模型结果是候选，即使高置信也不能代替独立依据；模型发现与冲突分类属于可选装配，不开启时可显式登记与复核。

未知类型、无效方向/端点、模型版本不兼容拒绝写入；端点未能解析保留为待处理项，不生成虚构边。真源关系记录先持久化到 Registry KV，再以 relation_id/revision 幂等更新图投影。关系修订保留历史，撤回和源隔离立即通过真源核验阻止使用；投影滞后只能降低完整性，不能恢复旧状态。关系与两端及证据均须可读；无权节点不可作为中间桥梁，响应不得泄露隐藏路径或节点数量。

涉及本次新内容的关系采用两步写入：先以带 `continuity` 的 `add` 取得服务端返回的正文单元并构造 MemoryRef，再通过 `continuity_relation` 提交 RelationRecord（签名见 S02，仅角色模式）。首版不接受客户端临时端点。第二步失败不回滚第一步；返回正文引用、关系提交状态与幂等回执，按 relation_id/revision 核对后重试，图投影失败另行修复，不重复抽取正文。关系登记与修订需校验真实归属的操作权和端点读取权，expected_revision 冲突拒绝。

### 查询输入与策略

下列为 `search` 的接续参数（`view_budget` 即下表 budget）：

| 字段 | 约束 |
| --- | --- |
| query | 非空文本，可选；用于向量/全文/实体定位起点 |
| anchor | MemoryRef，可选；存在时直接点读作为起点，不强制再做语义搜索 |
| task_id | 可选；指定时必须读取获授权的任务共同背景 |
| duty | 服务端注册的策略名，或 1–4 个策略名的列表；内置 general / diagnosis / testing / execution，其中 general 为缺省解析值且可显式传入，枚举之外取值报错 |
| view_budget | 可选 max_depth / max_nodes / max_edges / max_paths / timeout_ms；正整数，不得超过服务端上限 |

query、anchor、task_id 至少提供一个。仅 task_id 时以登记背景为起点；同时给 query 与 anchor（或任务背景）时，anchor 与背景固定起点，query 只用于发现与起点没有连边的职责经验候选，不参与排序。anchor 与 task_id 同传不自动创建关联。响应报告实际解析到的职责、策略版本和生效预算；配置默认职责与显式值的优先级为“显式值 → 连接默认值 → general”，均不影响授权。

业务 duty_ids 与经验适用职责可多值，查询 duty 只选择遍历策略，不是业务身份。团队自定义职责通过启动配置新增遍历策略；多职责查询按"数据结构 / `Duty`"中的合并规则执行一次展开。

职责策略映射为“关系类型＋遍历方向＋深度/数量限制”，不要求每条边写 duty。diagnosis 从问题沿 possible_cause_of 入边找假设，再沿 supports/contradicts 入边找证据；testing 沿 verification_method_for/verifies 入边找方法和结果，再找方法的 verifies 入边；execution 从已选方案沿 requires 出边找条件，并沿 verifies 入边找执行验证。问题没有明确方案关系时报告缺口，可另做语义候选检索，不能虚构问题到方案的连接。

需要跨任务经验且没有已知关系时，可从已授权任务目标/query/anchor 正文做有界语义候选发现。返回条目的 `view.discovery` 为 semantic_candidate，path_ids 为空；不得编造与原起点的关系，可将候选作为附加起点继续展开。无可用文本报 missing_input；候选与图共用总预算，额外候选不挤掉必需背景。

general 使用同一关系集合的有界通用展开。所有策略对返回判断/方案的直接反证、必要条件执行共同检查，共用总预算；不足时明确 unchecked_dependencies，不宣称完整。按 Scope/ID/摘要去重节点，按关系 ID/版本去重边，访问去重防环。固定模型和数据下，相同 anchor 不同 duty 应体现路径选择差异，而不是只重写摘要。

声明支持该能力的查询实现须支持端点解析、类型/方向筛选、逐跳授权、路径返回和预算限制，不可用私有 extensions 隐藏必需契约。图通道临时失败可返回起点及明确背景的 partial 结果；未装配能力返回 unsupported，不能伪装无结果。无图覆盖与确实无匹配边通过 coverage/gaps 区分。

覆盖清单由构建/查询流程维护（`ContinuityService.relation_coverage`），记录 `model_version`、已登记关系数量与投影数量、关系真源摘要的 `source_digest`、图投影水位 `projection_watermark` 与状态；清单持久于 Registry 的 `/continuity/coverage/relations`。只有已登记关系全部有真源引用且投影状态 committed（投影追平）才返回 covered_for_requested_scope；存在部分失败/未投影为 partial；无任何登记关系为 unknown（无可证明清单）。该状态只证明登记范围被处理并投影，不证明自动抽取无遗漏或外部世界无其他关系。

### Agent 能力发现与工具暴露

`memory_search` 工具 schema 描述 query/anchor/task_id/duty/view_budget 及错误语义；接续模式下 duty 说明标注"当前服务未启用关系扩展"；工具说明明确“定位记忆并按职责扩展关系”，暴露支持的 duty 与缺省行为。系统提示词或 Skill 仅补充何时调用：恢复任务先读取背景、缺证时展开、形成结果后记录。不得仅靠一段提示词宣称已实现工具能力。

工具命名：内核 MCP 沿用 `memory_<方法名>`（`memory_add`、`memory_search`、`memory_continuity_task`、`memory_continuity_recover`，角色模式另有 `memory_continuity_relation`）；JiuwenSwarm 适配器为 `agent_memory_<方法名>`。服务端无法报告 `continuity_mode` 时适配器按接续模式注册（少注册优于误注册）。

Provider/MCP Adapter 只负责注册、可信上下文、参数转换及结构化回写，不执行图算法。已固定职责可经连接配置提供默认值；多职责 Agent 可显式选择。宿主不能表达所需 schema 时由适配器显式转换并测试，不静默丢弃职责或路径。Agent 不需掌握全部边类型，关系策略由服务端维护。

## 结构化职责视图（role-view/2）

结果字段、条目状态规则、缺口码和元信息见[数据结构「职责视图结果」](#职责视图结果role-view2retrievalresult-可选字段)。关系覆盖没有可信全量水位时返回 unknown，不虚构 covered；遍历队列耗尽只表示本次有界展开结束，不能证明摄入覆盖或环境已核实。

所有引用和内容在返回前判权；无权信息不得以 item ID、计数、Scope 或缺口说明回显。必需交接引用不可用时 continuation_ready=false；可选经验通道失败可返回 partial，但必须说明实际可继续的边界。

## 来源治理与列表过滤

来源治理入口为 `quarantine_source(scope, connector, reason)` 与 `release_source(scope, connector)`，归入治理组（管理面暴露，不进入 Agent 工具清单）。不可用来源集合 = 配置 `enabled=false` 的连接 ∪ 当前项目被隔离的连接。

- `search`（含视图路径）：不可用来源的条目在候选阶段过滤，关系的提交连接不可用时该关系不参与展开。
- `list`：不可用来源集合作为系统过滤谓词 `NOT system_metadata.continuity_connector IN {不可用连接}` 下推到 Engine，在分页与 `count` 之前生效，保证 `count`、`offset` / `limit` 一致；不得在取页后逐条抛 `PermissionDeniedError` 导致整页失败。
- `get`：单条命中不可用来源时返回 `PermissionDeniedError`，不区分不存在与受限。
- 隔离与解除即时生效，不改写正文，不删除关系；解除后恢复可见。

## 验证基线

- 每种产品独立认证和工具注册；伪造 duty、actor 或 binding 不得扩大权限。
- 前任离线且无交接单时，新产品能恢复已保存背景，未采集历史明确为缺口。
- 相同 query/anchor 不同 duty 返回正确关系类型与方向；对照纯向量、标签重排和无职责图展开，检查路径正确率及成本。
- candidate（兼容旧 proposed）、反证、环、超预算、失效端点、撤回边、图滞后、缺失通道、权限中间节点均有可验证行为。
- 任务映射重启可读；并发修订、事件重试、绑定撤销及任务关闭不误关联或重开。
- 实际存量无需职责标签即可点读；关系补建幂等、可撤回，不改原正文，不误删后续关系。
- 结构化响应包含 view_meta.anchors / paths / gaps 和策略版本；工具 schema、默认职责与显式职责一致。
- `search` 不带接续参数时结果与改动前一致；接续模式下带 duty 返回普通结果与 relation_expansion_disabled。
- `batch_add` 导入：每任务一次背景追加；整批重放不产生重复正文；单项失败不影响其他项。
- `list` 在存在隔离来源时分页与 count 一致，不整页失败。
- 关系真源在 KV：普通 search / list / get 不返回关系，撤回与隔离立即生效。

## 与其它 spec 的关系

S02 是公开 API 和鉴权入口，登记 `add` / `batch_add` 的 `continuity` 参数、`search` 的接续参数、`continuity_task` / `continuity_relation` / `continuity_recover`、治理 `quarantine_source` / `release_source` 及 surface 覆盖。S03 承载 TaskRegistry 编排，S04 承载职责关系扩展与 `RetrievalResult` 可选字段，S06 明确具名管理端口及持久能力，S07 承载最终 DTO/编解码。S09 约束装配、能力、错误、health 和关闭。本规约不自动赋予现有插件这些能力。

逻辑 DTO 经统一 HTTP/CLI 契约解析；MCP 保留嵌套 schema。远程产品桥只转发到同一服务，不在产品内装配第二个 Registry。配置与可执行接入步骤见[示例](../../examples/role_memory/README.md)；正式产品联调和真实历史样本不由协议替身测试代替。

### 派生构建能力与依赖判权

写入链通过 `supports_exact_metadata_lookup` 声明真源系统元数据等值查询的写后可读能力。派生构建启用前必须验证；不支持的实现（含文档模式）默认 false，此时配置派生构建即启动失败。派生输出以 `continuity_source_refs` 保存原始事件 MemoryRef 的 JSON 列表，读取正文前重新验证源摘要与读取权限；普通搜索不能验证依赖时不得返回该输出。

`ContinuityRecovery.kind=derivation` 核对已保存计划和输出标识，不写业务正文；回执置 partial 并要求重试原写入完成后续关联。已有计划的普通重试不因当前模型版本变化而重跑；显式新批次重建仍是独立能力，且只产生 created / reused。

### 时间映射与字段信任边界

接续时间映射：带 `continuity` 的写入，`event_time` 写入原始事件所有分片的 `temporal.t_event`，优先于正文日期推断；缺省时沿用摄入时间抽取规则。`occurred_at` 仍写入 t_message。

接续字段信任边界：`continuity_*` 仅由接续内部写路径注入，普通公开写入/更新不得设置；`add` 的 `continuity` 参数经 DTO 验证后由内部写路径注入，经 Command/Engine/RawPayload 传递；Ingestor 不解析信封，信封摘要仅作持久核对依据。

派生时间边界：`event_time` 只设置原始事件 t_event；created 派生输出不自动继承来源时间，按自身正文日期推断，无法推断则为空。reused 保留目标既有时间。来源时间检索不保证召回派生结果，查询侧不自动沿来源引用扩展时间谓词；同事件声明驱动的继承与来源时间联查均为后续能力。

### 缺口码

| 码 | 含义 | 阻塞 |
| --- | --- | --- |
| `relation_expansion_disabled` | 接续模式不展开关系（仅当调用方要求非 general 职责） | 否 |
| `budget_exhausted` | 视图结果超过 `top_k` 被截断 | 否 |
| `task_not_found` | 给定 `task_id` 未登记 | 否 |
| `background_unreadable` | 任务背景引用无法解析（不披露条目标识） | 按存储的 `required` |
| `background_unavailable` | 任务背景条目不可读（不披露条目标识） | 按存储的 `required` |
