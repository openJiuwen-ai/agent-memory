# F03 — Scope Space 隔离与目标定位设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | `jiuwen_memory/common/type_def/`、`jiuwen_memory/api/`、`jiuwen_memory/control/`、`jiuwen_memory/storage/`、`jiuwen_memory/retrieval/`、`jiuwen_memory/construction/`、`docs/design/architecture.md`、`docs/specs/S02-memory-api.md`、`docs/specs/S03-control.md`、`docs/specs/S05-construction.md`、`docs/specs/S06-storage.md`、`docs/specs/S07-common.md` |
| 测试基线 | `python3 -m compileall -q jiuwen_memory jiuwen_memory_entry/core tests`；12 个 Scope/Space 关键测试函数直接执行；`git diff --check`。当前环境未安装 pytest 与 ruff，未执行完整测试和 lint |

## 背景

变更前 `Scope` 为四维结构：

```python
Scope(org, user, agent, session)
```

它已经能表达组织、用户、Agent 与会话的归属关系，也支撑了现有 `Scope + Grant`
访问隔离。但在多租户 SaaS、企业内多工作区、项目隔离、环境隔离等场景中，`org`
往往是账务、合同与组织管理边界，不适合作为直接的数据隔离边界。

一个 `org` 内可能同时存在多个互不信任、需要独立治理或需要单独删除的数据池：

- 不同客户项目：`acme/proj-a` 与 `acme/proj-b`
- 不同环境：`prod`、`staging`、`dev`
- 不同产品线、团队、部门或知识域
- 需要独立 offboarding、export、retention policy、配额与审计的数据池

因此需要在 `Scope` 中新增 `space` 层，把它定义为**逻辑隔离单元**。`space`
是多租户隔离的主边界，覆盖 access 隔离与 storage 隔离；`org` 继续承担上级
组织、账务、管理员与聚合治理边界。

Space 字段初步落地后，生命周期、治理读取和索引删除仍有只接收裸
`MemoryUnit.id`、再扫描全部 Scope 或维护 `unit_id -> Scope` 缓存的路径。这隐含了 Memory
ID 全局唯一的前提，与 Store 的“完整 Scope 内唯一”契约冲突。list 若只按请求级过滤条件选择
权限 profile，也可能在未指定 `memory_types` 时跳过实际资源类型的二次鉴权。因此 Space 隔离
不仅是增加字段，还要求所有目标操作、权限上下文和 offboarding 链路使用同一 Scope 边界。

## 目标模型

### Scope 字段

目标 `Scope` 字段集合为：

```python
@dataclass
class Scope:
    org: str = ""
    space: str = field(default="", kw_only=True)
    user: str = ""
    agent: str = ""
    session: str = ""
```

字段语义：

- `org`：组织、账务、合同、平台管理边界。
- `space`：全局唯一的逻辑隔离单元标识，承载多租户数据边界、权限边界、存储分区边界；
  空字符串是未注册的本地兼容域。
- `user`：space 内的人类用户、业务对象、终端客户或 owner 主体。
- `agent`：space 内的 Agent、助手、自动化执行主体或 persona。
- `session`：space 内的短生命周期会话、run、thread、ticket。

`Scope` 的字段顺序不表达 `agent` 与 `user` 的固定父子关系。系统只把
`org > space` 作为全局硬层级；`space` 内部的主体归属顺序由 `principal_path`
配置决定。

`space` 使用 keyword-only 参数，旧位置参数继续保持
`Scope(org, user, agent, session)` 的顺序，避免新增字段把旧调用的 `user` 错绑定到 `space`。

### 主体路径

`space` 内支持两种主体路径：

| `principal_path` | 逻辑层级 | 适用场景 |
|---|---|---|
| `user_agent` | `org > space > user > agent > session` | 以用户为中心的个人助手、企业员工助手、面向用户画像的记忆 |
| `agent_user` | `org > space > agent > user > session` | 以 Agent/应用为中心的客服机器人、编码 Agent、产品内多用户服务 |

`principal_path` 是 space 级配置，默认值建议为 `user_agent`，用于兼容当前
owner-cover 规则：同一 `org` 与同一 `user` 下，actor 可覆盖更窄的 `agent/session`
子范围。需要 Agent 作为上级主体的 space 显式配置为 `agent_user`。

这样可以同时支持两类关系：

- `user -> agent`：一个用户拥有多个 Agent，每个 Agent 有自己的会话和私有记忆。
- `agent -> user`：一个 Agent 服务多个用户，Agent 拥有共享策略、工具经验或产品级记忆，
  用户是该 Agent 下的子主体。

## 决策

### 决策 1：`org + space` 是默认硬隔离边界

默认访问规则调整为：

1. `Scope()` 仍表示 platform admin。
2. `actor.org != target.org` 默认拒绝；当前实现不支持跨 org grant。
3. `actor.space != target.space` 默认拒绝，除非 platform admin 或显式 cross-space grant。
4. `actor` 与 `target` 在同一 `org + space` 内时，再按该 space 的
   `principal_path` 判断 owner-cover。
5. grant 必须记录完整 `Scope`，包括 `space`；省略 `space` 只匹配空 space 兼容域，不得被解释为跨所有 space。

`space` 为空仅用于兼容旧数据和本地单租户默认配置。生产多租户部署应开启
`scope.require_space=true`，要求租户数据面 API 的 target scope 同时包含 `org` 与 `space`。
非空 Space ID 在管理面全局唯一，`org` 表示该 Space 的归属组织；不同 org 不能重复创建
同一个 Space ID。

### 决策 2：owner-cover 由 `principal_path` 决定

owner-cover 的核心规则是“actor 的 scope 是 target scope 的前缀”。前缀字段顺序由
`principal_path` 决定：

```text
user_agent: org, space, user, agent, session
agent_user: org, space, agent, user, session
```

空字段只表示“没有继续收窄”，不能跳过中间层越级覆盖。例如：

| `principal_path` | actor | target | 结果 |
|---|---|---|---|
| `user_agent` | `org=o, space=s, user=u1` | `org=o, space=s, user=u1, agent=a1` | 允许 |
| `user_agent` | `org=o, space=s, agent=a1` | `org=o, space=s, user=u1, agent=a1` | 拒绝，缺少上级 `user` |
| `agent_user` | `org=o, space=s, agent=a1` | `org=o, space=s, agent=a1, user=u1` | 允许 |
| `agent_user` | `org=o, space=s, user=u1` | `org=o, space=s, agent=a1, user=u1` | 拒绝，缺少上级 `agent` |
| 任意 | `org=o, space=s1` | `org=o, space=s2` | 拒绝，跨 space |

这条规则避免把 `agent` 或 `user` 固化成全局父级，也避免仅凭字段是否为空推导长期角色。
长期角色模型应由 space member / role / policy 显式表达。

### 决策 3：Storage 隔离以 `org + space` 为分区键

Store 层必须把 `org + space` 当成隔离字段，所有读写删查都带完整 target scope。
`user/agent/session` 是 space 内的归属、过滤和 owner-cover 字段，不是跨租户分区键。

推荐定义三档实现策略：

| 策略 | 映射方式 | 适用场景 | 优点 | 风险 |
|---|---|---|---|---|
| `metadata_filter` | 单后端实例 + `org/space` metadata filter | 本地开发、小规模、低成本 | 实现简单、迁移成本低 | 依赖查询过滤，误漏过滤会串租户 |
| `namespace_per_space` | 每个 space 一个 namespace / tenant / partition | 标准多租户部署 | 隔离强、offboarding 简单、查询成本更可控 | namespace 管理和数量限制需治理 |
| `database_or_collection_per_space` | 每个 space 一个 database / collection / index | 强合规、大客户、schema 差异 | 最强隔离、可独立备份/迁移/冷热加载 | 成本高、资源碎片、运维复杂 |

各存储后端映射要求：

- KV：key prefix 或物理 namespace 必须包含 `org/space`。
- Vector：优先映射到 namespace/tenant/partition；不支持时必须把 `org/space`
  写入 metadata filter，并由 Store 层强制拼接过滤条件。
- Fulltext：索引名、index alias 或 filter 字段必须包含 `org/space`。
- Graph：graph id / graph namespace 必须包含 `org/space`；跨 space edge 默认禁止。
- Audit：审计事件记录 actor scope、target scope、target org、target space、action、decision。

### 决策 4：共享必须显式建模

`space` 是隔离单元，但系统仍需要共享能力。共享不通过隐式跨 space 检索实现，而通过两种
显式方式：

1. **Grant 型共享**：授予某 actor 对另一个 target scope 的 READ/WRITE/UPDATE/DELETE/SHARE
   权限，grant 必须包含明确 `space`。
2. **Shared Space 型共享**：把组织政策、公共知识库、跨 Agent 经验池建成独立 space，例如
   `Scope(org="acme", space="shared-policy")`，再授权业务 space 或主体读取。

默认 `add/search/get/update/delete/evolve` 都只作用于一个 target space。跨 space search
只能由 API 显式传入已授权的多个 scope，或通过 shared space 配置完成；不允许底层 retriever
自行扩大范围。

### 决策 5：Space 是 lifecycle / governance / offboarding 的最小租户单元

新增 space 后，治理面应具备：

- 创建、冻结、归档、删除 space。
- 查询与调整 space 级 policy，包括 `principal_path`、retention、配额、索引策略、演进策略。
- 管理 space 成员、角色与授权。
- 导出某个 space 的全部 memory、messages、indexes 与 audit。
- 删除某个 space 的全部真源和可重建索引，作为客户 offboarding 或项目关闭的主路径。
- 查询 space 级审计和用量统计。

删除 space 不应只做 metadata 标记。若后端支持物理 namespace/tenant/database 删除，应优先使用
物理删除；否则必须扫描完整 `org/space` 范围并删除真源、索引和派生记录。

### 决策 6：Space ID 全局唯一，Memory ID 仅在 Scope 内唯一

非空 Space ID 是全局唯一的资源标识。`KVSpaceManager` 在根 Scope 维护注册键，并以 KV
`insert` 冲突作为唯一性闸门；空字符串只表示未注册的本地兼容域。

`MemoryUnit.id` 只要求在完整 Scope 内唯一。Lifecycle、Governor、IndexBuilder、
provenance 扩展和 Space 清理必须携带完整 Scope 或带 Scope 的 MemoryUnit，不得用裸 ID
反向猜测 Scope。

### 决策 7：本地与云侧 Engine 的 Space 边界明确

`InMemoryEngine` 只处理 `space=""`；非空 Space 的数据面读写、生命周期和 offboarding 由
`CloudEngine` 处理。Space 管理仍由 API 与 SpaceManager 承担，Engine 只提供数据清理原语。

`MemoryAPI.list` 在请求级鉴权后，通过 `list_with_permission_contexts` 一次读取当前页、
分页前总数和每个实际 unit 的真源权限上下文并逐条鉴权。上下文、内容和 count 不得来自
独立查询，也不能把未指定 `memory_types` 解释为 fallback profile 已授权全部实际资源。

### 决策 8：迁移与 offboarding 顺序必须保留 Scope 语义

SQLite grant 旧表必须先增加 `grantor_space/grantee_space` 列，再创建引用这些列的索引。
`delete_space` 先通过 `MemoryEngine.purge_space` 清理目标 Space 下全部子 Scope 的真源与
索引，再由 SpaceManager 删除 messages、管理元数据和全局注册键。

## 当前落地范围

本次实现已覆盖核心多租户隔离链路：

- `Scope` 增加 `space` 字段，`MemoryUnit` codec 升级到 `_v=3`，读取 `_v<3` 四段 scope 时把 `space` 补为空字符串。
- `space` 为 keyword-only 字段，保留旧的 `Scope(org, user, agent, session)` 位置参数语义。
- `scope_segments(scope)` 输出 `org/space/user/agent/session` 五段；KV、FS、Graph、Fusion 等命名空间型后端按五段精确隔离。
- `scope_dims(scope)` 在 `org` 已给出时固定下推 `space == ""`，防止空 space 查询跨到其他 space；Vector / Fulltext 后端记录并过滤 `space`。
- SQLite KV、SQLite Permission、SQLite Audit 支持旧表轻量迁移，旧数据进入空 space 兼容域。
- `PermissionManager.check` 先要求同 `org + space`，再按 `principal_path`（默认 `user_agent`，可通过 `PermissionContext.metadata["principal_path"]="agent_user"` 覆盖）判断 owner-cover；显式 grant 可跨 space 授权。
- `LocalMemoryAPI` 支持 `scope.require_space` 策略，开启后具体 target scope 缺少 `space` 会被拒绝并记录 deny audit。
- `SpaceManager` 已作为 control 算子落地，默认 `kv` 实现在根 Scope 维护全局 Space ID 注册键，
  并存储 space metadata、space policy、member/role、export 记录，提供 usage 与
  delete/offboarding。
- `LocalMemoryAPI` 已暴露 create/get/list/update/archive/delete/export/usage/policy/member space
  管理接口；`delete_space` 当前只支持 PURGE，先经 CloudEngine 清目标 Space 的全部子 Scope
  真源与索引，再由 SpaceManager 删除 KV/messages/metadata 和全局注册键。
- `InMemoryEngine` 只处理 `space=""` 本地兼容域；命名 Space 的数据面读写与清理由
  `CloudEngine` 负责。
- Lifecycle、Governor 和 IndexBuilder 的目标操作携带完整 Scope，允许不同 Scope 内复用同一
  `MemoryUnit.id` 而不串改、串查或串删。
- 已创建 space 的 `principal_path` 由 `SpaceManager.get_policy` 注入 `PermissionContext.metadata["principal_path"]`，调用级 metadata 不能临时覆盖 space policy。
- `AuditEvent` 增加 target scope，内存/SQLite 审计后端支持 `target_org` / `target_space` / `target_user` / `target_agent` / `target_session` 过滤。
- 非 HTTP 兼容 dispatch payload 支持 `space` / `space_id`、`actor_space` / `actor_space_id`、`grantee_space` / `grantee_space_id`；HTTP DTO 将 target 放入嵌套对象并拒绝 `actor_*`。

尚未落地：基于 `SpaceMember.role` 的默认权限矩阵、跨 space recall 的多 target API、index/audit 专用后端的精确 usage 计数、后端原生 namespace/tenant 物理删除适配。

## 需要增加的 API 接口

### 现有 API 的入参语义变化

这些接口不需要改名，但 target scope 必须支持 `space`，并在 `scope.require_space=true`
时拒绝缺少 `space` 的租户数据操作：

| 接口 | 需要变化 |
|---|---|
| `add` / `add_async` | 写入目标 scope 增加 `space`；落盘和索引必须记录 `org + space` |
| `search` | `context.scope` 增加 `space`；检索范围默认限定在单个 target space |
| `get` / `update` | 点读和修正必须按 `unit_id + org + space` 校验归属 |
| `delete` | `DeleteSelector.scope` 支持 space；无 scope 的跨范围删除继续退到根 scope 管理闸门 |
| `evolve` | 演进任务按 target space 提交和扫描 |
| `inspect` / `trace` | 治理读取按 target space 鉴权 |
| `audit` | filters 增加 `actor_space` 与 `target_org` / `target_space` / `target_user` / `target_agent` / `target_session` |
| `grant` / `revoke` | `Grant.grantor` 与 `Grant.grantee` 持久化 `space`，匹配逻辑不得跨 space 漏命中 |

### 新增 Space 管理 API

`MemoryAPI` 管理面已新增以下接口，并由 control 层的 `SpaceManager` 承接：

| 接口 | 语义 |
|---|---|
| `create_space(spec, *, identity) -> SpaceInfo` | 创建 space，写入 `principal_path`、状态、metadata 与初始 policy |
| `get_space(org, space, *, identity) -> SpaceInfo` | 读取单个 space 的基础信息与状态 |
| `list_spaces(org, *, identity, status=None, limit=100, cursor=None) -> list[SpaceInfo]` | 列出 org 下 spaces |
| `update_space(org, space, patch, *, identity) -> SpaceInfo` | 修改 display name、metadata、policy 等非破坏字段 |
| `archive_space(org, space, *, identity) -> SpaceInfo` | 归档 space，默认停止写入但保留读取与导出能力 |
| `delete_space(org, space, *, identity, mode=PURGE) -> SpaceDeleteResult` | 删除 space 真源与派生索引；当前只支持 PURGE |
| `export_space(org, space, *, identity, include_audit=True) -> str` | 提交 space 导出任务，返回 export id |
| `space_usage(org, space, *, identity) -> SpaceUsage` | 查询 memory/message/KV bytes 用量 |
| `get_space_policy(org, space, *, identity) -> SpacePolicy` | 查询 space 级 policy |
| `set_space_policy(org, space, policy, *, identity) -> SpacePolicy` | 设置 space 级 policy，包括 `principal_path`、retention、配额、索引和演进策略 |
| `list_space_members(org, space, *, identity) -> list[SpaceMember]` | 查询 space 成员与角色 |
| `add_space_member(org, space, member, *, identity) -> None` | 添加或更新 space 成员角色 |
| `remove_space_member(org, space, member, *, identity) -> None` | 移除 space 成员 |

### 新增或扩展的数据类型

| 类型 | 关键字段 |
|---|---|
| `PrincipalPath` | `USER_AGENT` / `AGENT_USER` |
| `SpaceStatus` | `ACTIVE` / `FROZEN` / `ARCHIVED` / `DELETING` / `DELETED` |
| `SpaceInfo` | `org` / `space` / `display_name` / `status` / `principal_path` / `metadata` / `created_at` / `archived_at` |
| `SpaceSpec` | `org` / `space` / `display_name` / `principal_path` / `policy` / `metadata` |
| `SpacePatch` | `display_name` / `status` / `policy` / `metadata` |
| `SpacePolicy` | `require_space` / `principal_path` / `storage_isolation_strategy` / `retention` / `quotas` / `index_profiles` / `pipeline_profiles` |
| `SpaceMember` | `scope` / `role` / `created_at` / `expires_at` |
| `SpaceUsage` | `memory_count` / `message_count` / `index_count` / `storage_bytes` / `audit_count` |
| `SpaceDeleteResult` | `org` / `space` / `deleted_counts` / `status` / `audit_event_id` |

## 当前已实现配置

```yaml
policy:
  default:
    target: dict
    params:
      policies:
        rerank.enabled: "true"
        lifecycle.expired_active.target: forgotten
        lifecycle.superseded.target: forgotten
        scope.require_space: "true"

space:
  default:
    target: kv
    params:
      kv_store: default
```

非 HTTP 兼容 dispatch 接入 payload 支持 `space` / `space_id`，actor override 支持
`actor_space` / `actor_space_id`，授权 grantee 支持 `grantee_space` /
`grantee_space_id`。HTTP 使用 `target.space` / `target.space_id`，并拒绝 actor override；
未传 space 时是否进入空 space 兼容域由各非 HTTP adapter 决定。

## 后续配置草案

```yaml
memory_api:
  globals:
    require_space: true
    default_principal_path: user_agent
    storage_isolation_strategy: namespace_per_space

  spaces:
    default:
      principal_path: user_agent
      retention:
        memory_days: 365
      quotas:
        max_memory_units: 1000000
      indexes:
        vector: true
        fulltext: true
        graph: false
      pipelines:
        construction: default
        retrieval: default

  vector_store:
    default:
      target: default_vector
      params:
        isolation_strategy: partition_key
        partition_key_fields: ["org", "space"]
```

配置优先级建议为：

```text
全局默认
  -> 部署 profile
  -> org 级策略
  -> space 级策略
  -> 调用级 options
```

`principal_path` 不建议作为调用级 options 临时覆盖。它影响权限、检索、存储命名空间与治理语义，
应固定在 space policy 上。

## 迁移计划

### 阶段 0：文档与约束确认

- 明确 `space` 是 `Scope` 的一级字段。
- 明确 `org + space` 是默认硬隔离边界。
- 明确 `agent/user` 不再有全局固定父子关系，而是由 space 的 `principal_path` 决定。
- 明确生产多租户环境要求 `org + space` 非空。

### 阶段 1：兼容字段落地

- 修改 `Scope` dataclass，增加 `space: str = ""`。
- 更新 scope 序列化/反序列化、CLI/HTTP/MCP payload 解析。
- 老数据缺少 `space` 时读为 `""`，行为保持兼容。
- 更新 `Scope` 字符串化、审计 detail、配置示例。

### 阶段 2：Access 隔离

- 修改 owner-cover 规则，先校验 `org + space`，再按 `principal_path` 判断前缀覆盖。
- 修改 `Grant` 持久化，增加 grantor/grantee 的 `space` 字段。
- 增加 migration：旧 grants 的 `space` 填空串。
- 增加 `scope.require_space` 策略：生产配置打开后，target scope 缺少 `space` 直接拒绝写入/检索。
- 增加 space member / role / policy 的管理模型。

### 阶段 3：Storage 隔离

- 所有 Store record 增加或下推 `space` 字段。
- Vector/Fulltext/Graph/KV 后端补 `org + space` filter 或 namespace 映射。
- `IndexBuilder` 构建索引时把 `unit.scope.org` 与 `unit.scope.space` 写入索引记录。
- `Retriever`、`Dedup` 的查询必须包含 space；Lifecycle、Governor 的目标操作必须显式接收
  完整 Scope，全局 sweep 逐 Scope 分组执行。

### 阶段 4：Space 治理

- 增加 space create/get/list/update/archive/delete/export/usage/policy/member 接口。
- 增加 space 级审计过滤。
- 增加 offboarding 流程：冻结写入 -> 导出 -> 删除真源与索引 -> 审计记录。

## 验证

### 单测

- `Scope` 新字段默认兼容：旧构造不传 `space` 仍可用。
- `Scope(org, user, agent, session)` 四个位置参数的绑定顺序保持不变，`space` 只能按关键字传入。
- 不同 org 创建相同非空 Space ID 时返回冲突。
- `scope.require_space=true`：缺少 space 的 add/search 拒绝。
- owner-cover：`user_agent` 下 user 覆盖 agent/session；agent 不反向覆盖 user。
- owner-cover：`agent_user` 下 agent 覆盖 user/session；user 不反向覆盖 agent。
- Grant：只授权指定 space；不同 space 不命中。
- Space policy：`principal_path` 只能在 space policy 上生效，不被调用级 options 覆盖。
- Lifecycle、Governor、Fulltext/Vector IndexBuilder：跨 Space 同 Memory ID 时只处理目标
  Scope。
- InMemoryEngine：非空 Space 请求被拒绝；CloudEngine 承担命名 Space 数据面操作。
- list：对当前分页实际资源类型逐条鉴权，未指定 `memory_types` 也不能绕过类型路由。
- SQLite PermissionManager：缺少 Space 列的旧表先完成迁移，再创建索引。

### 集成测试

- 同 org 两个 space 写入同样内容，search 只返回目标 space 的结果。
- delete space A 不影响 space B。
- 不同 space 使用相同 `MemoryUnit.id` 时，生命周期、治理读取和索引删除只影响目标 Scope。
- vector/fulltext/graph 三类索引均不跨 space 召回。
- Fusion 后端允许跨 Scope 使用相同逻辑 id，检索与正排仍严格隔离。
- lifecycle sweep 只处理目标 space，或按 space 分组扫描。
- audit 可按 `actor_space` 与 `target_space` 查询。
- `agent_user` 与 `user_agent` 两类 space 在同一部署中共存。

### 回归测试

- 旧单租户默认配置仍可用，`space=""` 兼容。
- 旧数据 loads 不失败。
- CLI/HTTP/MCP 未传 space 时在 `require_space=false` 下行为不变。

当前环境未安装 pytest 和 ruff，因此实际执行了 Python 编译、12 个关键测试函数直接调用、
新增行长检查与 `git diff --check`；完整测试和 lint 仍需在项目开发环境运行。

## 拒绝的方案

### 拒绝方案 1：复用 `org` 表示租户隔离

`org` 更适合作为上级组织、账务、合同和管理边界。把所有项目、环境、团队都塞进 `org`
会导致：

- org 数量膨胀，成员关系和账务语义混乱。
- 同一客户下多个隔离项目无法自然表达。
- 无法在 org 内做 space 级 offboarding、retention、配额和审计。

### 拒绝方案 2：把 `space` 放进 metadata 而不是 Scope

metadata filter 只能作为低层实现策略，不能作为核心模型。若 `space` 只是 metadata：

- API 鉴权无法把它当作一等边界。
- Store 抽象无法强制所有后端带上 `space`。
- 审计、grant、delete_space、export_space 等治理能力会变成约定而非契约。
- 开发者容易漏传过滤条件，造成串租户。

### 拒绝方案 3：把 `user -> agent` 或 `agent -> user` 固定为全局唯一顺序

两类业务关系都合理。把其中一种写死为全局规则，会让另一类场景被迫伪造字段：

- 用户中心场景需要 `user -> agent`。
- Agent/应用中心场景需要 `agent -> user`。

因此 `Scope` 只承载字段，`principal_path` 承载 space 内主体路径。

### 拒绝方案 4：一开始强制“一 space 一物理数据库”

物理数据库隔离强，但不适合所有规模。大量小 space 会造成资源碎片和运维成本。更合理的是让
`space` 作为统一抽象，由后端配置选择 namespace / partition / database / metadata filter。

### 拒绝方案 5：默认跨 space 聚合检索

跨 space 检索容易造成数据泄露和审计困难。需要共享时，应显式授权或显式查询 shared space。
默认 search 永远只在 target space 内执行。

### 拒绝方案 6：把 Memory ID 改为全局唯一

Store 接口已经以 Scope 作为命名空间，强制 Memory ID 全局唯一会扩大迁移成本，也不能替代
调用链显式传递租户边界。

### 拒绝方案 7：保留 `unit_id -> Scope` 单值缓存

缓存键会在跨 Scope 同 ID 时碰撞，并引入失效和重建问题。目标操作应直接接收 Scope 或
带 Scope 的 MemoryUnit。

### 拒绝方案 8：让 API 直接扫描 KV 完成 Space 清理

API 不应依赖具体存储实现。跨真源与索引的清理编排属于 Engine 契约，API 只负责鉴权、委托和
审计。

### 拒绝方案 9：信任 list 请求过滤条件代表实际资源类型

请求未指定 `memory_types` 时仍可能返回多种资源。权限路由必须来自真源中的实际 unit
metadata，并与返回内容来自同一次分页读取。

### 拒绝方案 10：让 InMemoryEngine 部分支持命名 Space

两个 Engine 对非空 Space 的责任边界必须明确，避免本地最小实现逐步复制云侧隔离、治理和
offboarding 逻辑。

## 已知遗留

- `org admin`、`space admin`、`space member` 的角色枚举与默认权限矩阵需要在 API spec 中固化。
- 跨 space search 的最终 API 形态需要独立设计，建议显式传 authorized scopes。
- 各后端的物理隔离能力差异较大，需要在 `docs/specs/S06-storage.md` 中补矩阵。
- 全局 Space 注册键与 Space metadata 分两次 KV 写入，进程在两次写入之间崩溃时需要后台
  reconciliation 清理孤立注册键。
- CloudEngine 开启 EncryptedKVStore 后的跨 Space recall/offboarding 仍缺完整集成测试。
- `purge_space` 当前按 KV Scope 枚举并逐 Scope 清理；具备原生 namespace/tenant 删除能力的
  云 Store 后端后续应提供物理清理适配。
