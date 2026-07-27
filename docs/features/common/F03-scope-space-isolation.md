# F03 — Scope 新增 Space 隔离层设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 影响范围 | `src/common/type_def/scope.py`、`src/api/`、`src/control/`、`src/storage/`、`src/retrieval/`、`src/construction/`、`docs/design/architecture.md`、`docs/specs/S02-memory-api.md`、`docs/specs/S03-memory-manage.md`、`docs/specs/S06-storage.md`、`docs/specs/S07-common.md` |
| 测试基线 | 设计文档阶段，未改代码，未运行测试 |

## 背景

当前 `Scope` 为四维结构：

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

## 目标模型

### Scope 字段

目标 `Scope` 字段集合为：

```python
@dataclass
class Scope:
    org: str = ""
    space: str = ""
    agent: str = ""
    user: str = ""
    session: str = ""
```

字段语义：

- `org`：组织、账务、合同、平台管理边界。
- `space`：逻辑隔离单元，承载多租户数据边界、权限边界、存储分区边界。
- `agent`：space 内的 Agent、助手、自动化执行主体或 persona。
- `user`：space 内的人类用户、业务对象、终端客户或 owner 主体。
- `session`：space 内的短生命周期会话、run、thread、ticket。

`Scope` 的字段顺序不表达 `agent` 与 `user` 的固定父子关系。系统只把
`org > space` 作为全局硬层级；`space` 内部的主体归属顺序由 `principal_path`
配置决定。

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
2. `actor.org != target.org` 默认拒绝，除非 platform admin 或显式跨 org grant。
3. `actor.space != target.space` 默认拒绝，除非 org admin、显式 cross-space grant，
   或治理接口正在执行 org 级 space 管理动作。
4. `actor` 与 `target` 在同一 `org + space` 内时，再按该 space 的
   `principal_path` 判断 owner-cover。
5. grant 必须记录完整 `Scope`，包括 `space`；省略 `space` 不得被解释为跨所有 space。

`space` 为空仅用于兼容旧数据和本地单租户默认配置。生产多租户部署应开启
`require_space=true`，要求租户数据面 API 的 target scope 同时包含 `org` 与 `space`。

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

默认 `write/recall/get/update/delete/evolve` 都只作用于一个 target space。跨 space recall
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

## 需要增加的 API 接口

### 现有 API 的入参语义变化

这些接口不需要改名，但 target scope 必须支持 `space`，并在 `require_space=true`
时拒绝缺少 `space` 的租户数据操作：

| 接口 | 需要变化 |
|---|---|
| `write` / `write_async` | 写入目标 scope 增加 `space`；落盘和索引必须记录 `org + space` |
| `recall` | `context.scope` 增加 `space`；检索范围默认限定在单个 target space |
| `get` / `update` | 点读和修正必须按 `unit_id + org + space` 校验归属 |
| `delete` | `DeleteSelector.scope` 支持 space；无 scope 的跨范围删除继续退到根 scope 管理闸门 |
| `evolve` | 演进任务按 target space 提交和扫描 |
| `inspect` / `trace` | 治理读取按 target space 鉴权 |
| `audit` | filters 增加 `target_org`、`target_space`、`actor_space` |
| `grant` / `revoke` | `Grant.grantor` 与 `Grant.grantee` 持久化 `space`，匹配逻辑不得跨 space 漏命中 |

### 新增 Space 管理 API

建议在 `MemoryAPI` 管理面新增以下接口，并由 control 层的 `SpaceManager`
或等价控制算子承接：

| 接口 | 语义 |
|---|---|
| `create_space(spec, *, actor) -> SpaceInfo` | 创建 space，写入 `principal_path`、状态、metadata 与初始 policy |
| `get_space(org, space, *, actor) -> SpaceInfo` | 读取单个 space 的基础信息与状态 |
| `list_spaces(org, *, actor, status=None, limit=100, cursor=None) -> SpaceList` | 列出 org 下可见 spaces |
| `update_space(org, space, patch, *, actor) -> SpaceInfo` | 修改 display name、metadata、policy 等非破坏字段 |
| `archive_space(org, space, *, actor) -> SpaceInfo` | 归档 space，默认停止写入但保留读取与导出能力 |
| `delete_space(org, space, *, actor, mode=PURGE) -> SpaceDeleteResult` | 删除 space 真源与派生索引；物理删除失败必须返回可审计错误 |
| `export_space(org, space, *, actor, include_audit=True) -> str` | 提交 space 导出任务，返回 job id 或 export id |
| `space_usage(org, space, *, actor) -> SpaceUsage` | 查询 memory/message/index/audit 容量与调用统计 |
| `get_space_policy(org, space, *, actor) -> SpacePolicy` | 查询 space 级 policy |
| `set_space_policy(org, space, policy, *, actor) -> SpacePolicy` | 设置 space 级 policy，包括 `principal_path`、retention、配额、索引和演进策略 |
| `list_space_members(org, space, *, actor) -> list[SpaceMember]` | 查询 space 成员与角色 |
| `add_space_member(org, space, member, role, *, actor) -> None` | 添加或更新 space 成员角色 |
| `remove_space_member(org, space, member, *, actor) -> None` | 移除 space 成员 |

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

## 配置草案

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
- 增加 `require_space` 策略：生产配置打开后，target scope 缺少 `space` 直接拒绝写入/检索。
- 增加 space member / role / policy 的管理模型。

### 阶段 3：Storage 隔离

- 所有 Store record 增加或下推 `space` 字段。
- Vector/Fulltext/Graph/KV 后端补 `org + space` filter 或 namespace 映射。
- `IndexBuilder` 构建索引时把 `unit.scope.org` 与 `unit.scope.space` 写入索引记录。
- `Retriever`、`Dedup`、`Lifecycle`、`Governor` 跨 scope 扫描都必须包含 space。

### 阶段 4：Space 治理

- 增加 space create/get/list/update/archive/delete/export/usage/policy/member 接口。
- 增加 space 级审计过滤。
- 增加 offboarding 流程：冻结写入 -> 导出 -> 删除真源与索引 -> 审计记录。

## 验证计划

### 单测

- `Scope` 新字段默认兼容：旧构造不传 `space` 仍可用。
- `require_space=true`：缺少 space 的 write/recall 拒绝。
- owner-cover：`user_agent` 下 user 覆盖 agent/session；agent 不反向覆盖 user。
- owner-cover：`agent_user` 下 agent 覆盖 user/session；user 不反向覆盖 agent。
- Grant：只授权指定 space；不同 space 不命中。
- Space policy：`principal_path` 只能在 space policy 上生效，不被调用级 options 覆盖。

### 集成测试

- 同 org 两个 space 写入同样内容，recall 只返回目标 space 的结果。
- delete space A 不影响 space B。
- vector/fulltext/graph 三类索引均不跨 space 召回。
- lifecycle sweep 只处理目标 space，或按 space 分组扫描。
- audit 可按 `target_space` 查询。
- `agent_user` 与 `user_agent` 两类 space 在同一部署中共存。

### 回归测试

- 旧单租户默认配置仍可用，`space=""` 兼容。
- 旧数据 loads 不失败。
- CLI/HTTP/MCP 未传 space 时在 `require_space=false` 下行为不变。

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
默认 recall 永远只在 target space 内执行。

## 已知遗留

- `org admin`、`space admin`、`space member` 的角色枚举与默认权限矩阵需要在 API spec 中固化。
- 跨 space recall 的最终 API 形态需要独立设计，建议显式传 authorized scopes。
- 各后端的物理隔离能力差异较大，需要在 `docs/specs/S06-storage.md` 中补矩阵。
- 迁移旧数据时是否把 `space` 填为 `default` 还是空串，需要根据部署方式决定。
- 若保留 `Scope` 的位置参数兼容，需要审计所有非 keyword 构造调用，避免新增字段后位置错位。
