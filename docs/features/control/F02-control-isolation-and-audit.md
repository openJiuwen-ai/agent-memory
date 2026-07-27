# F02 — 控制层隔离与审计（一阶段）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-02 |
| 影响范围 | `bootstrap/core/handler.py`、`src/api/`、`src/control/`、`src/storage/`、`src/common/audit/`、`docs/specs/S03-control.md`、`docs/specs/S06-storage.md`、`docs/specs/S07-common.md` |
| 测试基线 | `ruff check` 通过；相关单测通过 |
| Refs | `docs/design/mem0-control-layer-gap-analysis.md` |

> 本文档归档 **agent-memory 控制层隔离与审计一阶段** 的设计与落地结果：以 `Scope + Grant` 为核心，把原本偏 demo 的 `allow_all` 权限占位替换为可执行、可审计、可验证的逻辑隔离链路；同时补齐审计事件的可选 SQLite 落盘能力。当前阶段聚焦管理层访问隔离、存储层逻辑边界校验和审计可追责，不引入物理分库、完整认证系统或复杂 RBAC 管理后台。

---

## 背景

`agent-memory` 已经具备较好的多租户数据模型基础：`Scope(org, space, user, agent, session)` 是一等结构，存储层也明确把 scope 隔离作为原生职责。问题不在“有没有 scope”，而在“scope 是否真正形成了可执行的租户隔离边界”。

改造前主要有三个缺口：

1. 接入层默认把 `actor` 与 `target` 绑定为同一个 scope，无法表达“管理员访问某个用户 scope”或“被授权方跨 scope 读取”。
2. 管理层的权限实现仍然是 `AllowAllPermissionManager`，也就是有抽象、无门禁。
3. 存储层虽然天然按 scope 落库，但缺少一套显式的逻辑隔离校验约束，无法系统性验证“上层传错 scope 时，下层仍不会串租户”。

本特性选择的不是最轻的“只做管理层权限判断”，也不是最重的“立刻引入物理租户分库/分索引”，而是中间方案：**管理层访问隔离 + 存储层逻辑隔离校验**。它能先补上当前最关键的越权风险，同时不把仓库拖入一轮大规模基础设施改造。

---

## 决策

### 决策 1：采用管理层访问隔离 + 存储层逻辑边界校验

本次多租户隔离特性采用如下边界：

1. **管理层成为访问隔离的主控制面**
   由接入层解析调用者身份，形成 `actor scope`；由请求参数表达目标记忆范围，形成 `target scope`；再由 API 层统一调用 `PermissionManager.check(actor, target, action)` 做门禁。

2. **存储层继续承担原生 scope 隔离职责**
   不把“租户隔离”下沉成调用约定，而是继续把它视为后端实现必须满足的物理/逻辑约束。

3. **新增存储层逻辑隔离校验**
   不要求本轮实现物理分库、分 collection、分 namespace 的完整能力，但要求 Store 能被验证为“不会跨 scope 返回、修改或删除数据”。

真正的目标是让 `agent-memory` 形成完整隔离链路：

```text
surface actor -> API gate -> control decision -> storage scope boundary
```

### 决策 2：显式分离 actor 与 target

原先 `_scopes(payload)` 的语义是“从 `tenant_id + scope` 同时推导出 target 和 actor，并令二者相同”。这个假设只适用于单租户自服务，不适用于真正的多租户管理面。

新设计要求：

- `actor` 表示调用方身份。
- `target` 表示请求显式声明的目标 scope。
- 同一请求中，`actor != target` 是合法且常见的情况。

这样才能表达：

1. 管理员读取某租户下某用户的记忆。
2. 某用户经授权读取另一个 agent/session 的共享记忆。
3. 系统任务以平台身份对某个目标 scope 执行 sweep、audit、history、delete_all。

第一阶段不新增独立的 `RequestContext` / `Principal` 文件，也不修改 `dispatch(...)` 签名。入口层在 `bootstrap/core/handler.py` 中拆成：

- `_target_scope(payload)`
- `_actor_scope(payload)`

当前 `actor_*` 字段仍属于过渡态的 **claimed identity**，不等同于最终鉴权后的 authenticated identity。后续真正接入鉴权时，只替换 `_actor_scope(...)` 的来源，不重做 `MemoryAPI(identity=...)` 主链路。

### 决策 3：权限模型以 Scope/Grant 为核心

`mem0` 的 OSS/server 在身份入口上有可参考之处，但它的主模型更偏向“按 `user_id/agent_id/run_id` 过滤访问”，不是 `agent-memory` 的显式 `Scope + Grant` 授权模型。

因此本次设计不把多租户隔离简化为：

- 请求里传 `tenant_id`
- 查询时附带 `tenant_id/user_id`
- 后端只做过滤

而是维持 `agent-memory` 已有的控制层方向：

- owner scope 直接访问自己的 target；
- scope 包含关系可形成上级访问下级的默认授权；
- 跨 scope 访问必须依赖显式 `Grant`；
- 管理接口与普通数据接口使用不同的权限要求。

### 决策 4：第一阶段只做 platform admin，不做 tenant admin

第一阶段不引入完整的分层管理员模型，不做 `tenant admin`。权限主体先收敛为三类：

1. `platform admin`
2. 普通 owner scope
3. 通过 grant 获得跨 scope 权限的调用者

当前采用强管理员语义：

- `Scope()` 被视为 `platform admin`，可访问管理面接口，也可跨 scope 行权。
- owner 访问自己的 target scope 默认放行。
- 同租户跨 scope 默认拒绝，除非存在匹配 Grant。
- 跨 `org` 默认拒绝，除非 actor 为 `platform admin`。
- `admin_*`、全局 `audit` 等管理面接口不再对普通租户 scope 开放。

这是偏实现优先的阶段性决策。后续若要收紧成 break-glass 或审批式数据访问，再单独演进。

### 决策 5：Grant 第一阶段即持久化

第一阶段不接受内存态 grant。原因是内存授权无法支撑：

- 服务重启后的稳定权限状态；
- 管理层追责；
- 后续鉴权接入后的完整授权链。

当前落地方向：

- 新增真实 `SQLitePermissionManager`；
- grant 持久化到 SQLite；
- `revoke` 使用软撤销，而不是物理删除；
- `check(actor, target, action)` 支持 scope 覆盖匹配，而不是只做全等。

---

## 当前实现

### 权限语义

1. 默认权限实现已从 `AllowAllPermissionManager` 切换到 `SQLitePermissionManager(db_path=":memory:")`。
2. `Scope()` 被视为 `platform admin`，全局放行。
3. owner 访问自己的 target scope 默认放行。
4. 同租户跨 scope 默认拒绝，除非存在匹配 Grant。
5. 跨 `org` 默认拒绝，除非 actor 为 `platform admin`。
6. `grant` / `revoke` 支持 SQLite 持久化、过期校验与软撤销。

### 接入与 API

1. `bootstrap/core/handler.py` 已拆分 `_target_scope(payload)` 与 `_actor_scope(payload)`。
2. 对外请求形状保持兼容，`tenant_id + scope` 仍代表 target scope。
3. dispatch surface 新增可选 claimed actor 字段：`actor_tenant_id`、`actor_space` / `actor_space_id`、`actor_scope`、`actor_agent`、`actor_session`。
4. 若未传任何 `actor_*` 字段，则 actor 默认继承当前请求的 `tenant_id + scope`。
5. 若完全未传身份字段，actor 回落为 `Scope(org="default", user="")`，不再通过空 payload 表达 `platform admin`。
6. 只要显式传入任一 `actor_*` 字段，也不允许通过空值构造 `Scope()`；空 `actor_tenant_id` 会回落到当前请求的 `tenant_id`，避免 claimed identity 升级为 platform admin。
7. `Scope()` 仍可作为 API 内部可信调用的 platform admin 身份，但 dispatch surface 不从业务 payload 推导它；后续应由真实认证层注入。
8. dispatch 路由已补齐 `revoke`。
9. dispatch `list` 已恢复为正式数据面入口：`handler._list` 先解析 target/actor 与分页参数，再委托 `MemoryAPI.list(...)`；鉴权与审计仍在 API 层完成。

### 审计

审计事件当前新增以下顶层字段：

| 字段 | 语义 |
|---|---|
| `decision` | `allow` / `deny` |
| `target` | 操作目标 `Scope`；无具体目标时为空 scope |

其余可见信息统一写入 `detail`，且不输出敏感 scope 明细。当前约定包括：

| 字段 | 语义 |
|---|---|
| `permission_check` | `enabled` / `disabled` |
| `permission_reason` | 放行或拒绝原因 |
| `job_id` | 调度任务 id（如 `evolve` 返回的 Scheduler job） |
| `before_unit_id` / `after_unit_id` | 单条记忆变更前后 id |
| `before_unit_ids` / `after_unit_ids` | 批量记忆变更前后 id 列表（JSON 字符串） |

dispatch `audit` 返回体透出 `actor`、`target`、`action`、`target_id`、`layer`、`decision`、`detail`。

审计查询入口仍然是治理层的 `Governor.audit(filters, limit)`，并由 API 层通过 `MemoryAPI.audit(...)` 暴露；`AuditLogger.query(filters, limit)` 只作为治理层消费审计后端的内部接口，不直接暴露为用户 API。这样可以保持“审计查询属于治理面”的边界。

当前 `filters` 支持 `action`、`layer`、`decision`、`target_id`、`actor_org`、`actor_space`、`actor_user`、`actor_agent`、`actor_session`、`target_org`、`target_space`、`target_user`、`target_agent`、`target_session`，以及闭区间时间边界 `occurred_after` / `occurred_before`（ISO datetime 字符串）。

当前审计后端有两类：

| 后端 | 配置 target | 语义 |
|---|---|---|
| SQLite 审计 | `sqlite` | 默认后端；默认 `db_path=":memory:"`，可改为 SQLite 文件路径以支持重启后继续通过 `Governor.audit(...)` 查询 |
| 内存审计 | `in_memory` | 事件保存在进程内存列表中，仅适合小规模本地开发和单测 |

SQLite 审计通过 `audit.default` 配置启用：

```python
{
    "audit": {
        "default": {
            "target": "sqlite",
            "params": {"db_path": ".agent-memory/audit.sqlite3"},
        }
    }
}
```

当前全局默认为 `sqlite` + `db_path=":memory:"`，避免默认生成持久文件；生产或长期运行场景应显式配置稳定的 SQLite `db_path`。

---

## 拒绝的方案

### 方案 A：只做管理层访问隔离，不做存储层逻辑校验

**描述**：只在 API / PermissionManager 增加权限判断，认为这样已经足够。

**拒绝原因**：

- 会让隔离正确性过度依赖上层调用纪律。
- 一旦某条内部路径漏掉校验，存储层不会提供第二道边界。
- 与仓库里“scope 隔离是存储层原生职责”的既有设计相冲突。

### 方案 B：立即推进物理租户隔离

**描述**：本轮直接把不同租户拆到独立数据库、独立向量 collection、独立图 namespace，甚至独立 worker。

**拒绝原因**：

- 改动面会迅速扩大到装配、配置、部署和运维层。
- 当前仓库的最主要缺口仍是“访问控制未落地”，不是“物理隔离编排能力不足”。
- 如果在 ACL、history、audit 都还没稳住之前就引入物理隔离，复杂度会上升得过快。

### 方案 C：退化为 mem0 式 identity 过滤模型

**描述**：不落地 Scope/Grant，只在请求里传 `tenant_id/user_id/agent_id`，靠查询过滤实现隔离。

**拒绝原因**：

- 无法承接 `agent-memory` 已有的层级 scope 模型。
- 很难表达“谁在访问谁”的授权语义，只能表达“当前请求过滤哪个集合”。
- 对 audit、history、grant、admin 等管理面能力支撑不足。

---

## 验证

### 访问控制

- actor 访问自己的 target scope 成功。
- 未授权 actor 访问其他 scope 被拒绝。
- grant 生效后，跨 scope 指定 action 可访问。
- grant 过期后自动失效。
- revoke 后授权失效。
- platform admin 可访问管理面接口。

### 租户边界

- 跨 `org` 默认拒绝。
- 同一逻辑 id 在不同 scope 下不会串读、串写、串删。
- `list/search/get` 均不会跨 scope 返回数据。
- dispatch `list` 不再绕过 API 鉴权直读 truth-source。

### 审计追责

- permission deny 会形成结构化审计事件。
- audit 返回体能看出 `actor` 与 `target`，并通过 `decision/detail` 解释这次访问的结果。
- grant/revoke 形成审计留痕。
- 启用 SQLite audit 后端后，审计事件会落盘，并可在重新装配内核后继续通过 `api.audit(...)` 查询。

### 已执行验证

| 验证项 | 结果 |
|---|---|
| `uv run ruff check ...` | 通过 |
| 相关 `pytest` 单测 | 通过 |
| `uv run python scripts\verify_control_engine_phase1.py` | 通过 |

---

## 代码审查结论

实现完成后，额外做过一轮针对遗漏点的代码自查，重点确认了以下问题：

1. dispatch 是否同时覆盖 `grant` 与 `revoke`：已补齐。
2. 审计扩展字段是否仅写入、不对外返回：已在 dispatch `audit` 返回体中透出。
3. 默认权限切换后，空 `DeleteSelector()` 是否仍先报 `ValidationError`：已修正。
4. 验证脚本是否依赖旧的内存态 `grant` 列表：已改为黑盒行为验证。
5. dispatch `list` 是否仍存在绕过 API 鉴权直读 truth-source 的旁路：已修正。
6. 审计持久化是否引入新的 `AuditLogger.query/audit` 读取接口：未引入；查询边界仍收敛在 `Governor.audit(...)`。

---

## 已知遗留

1. **物理隔离仍是后续阶段能力**
   本文只保留逻辑隔离与存储层边界校验，不覆盖分库分索引编排。

2. **权限模型与管理员层级仍需进一步规格化**
   比如租户管理员、平台管理员、系统任务身份的精确定义，后续需要写入 spec。

3. **grant 持久化后仍需与 history/audit 对齐**
   多租户隔离落地后，grant/revoke 本身会成为管理层的重要追责对象。

4. **审计落盘路径仍需按部署场景决策**
   当前默认使用 SQLite `:memory:`，生产部署应显式配置 SQLite audit 文件路径；迁移策略和清理策略仍需后续补齐。

5. **跨租户共享策略本轮不开放**
   如果未来要支持跨 org 共享，需要独立设计，不应在本轮默认放开。

6. **claimed identity 只是过渡方案**
   当前 `actor_*` 仍由请求方声明。后续接入真实认证后，应由鉴权结果生成 actor，而不是信任业务 payload。

## 最终判断

这次多租户隔离设计的核心，不是“把请求多带一个 tenant_id”，而是把 `agent-memory` 现有的 `Scope` 抽象真正落成一套可执行、可验证、可追责的控制边界。

采用“管理层访问隔离 + 存储层逻辑隔离校验”的方案，有三个直接好处：

1. 能先补上当前最关键的越权风险。
2. 能保住仓库原有的 scope-first 架构方向。
3. 能为后续的 history、audit、delete_all、policy persistence 和物理隔离预留稳定边界。

它不是终局方案，但很适合作为 `agent-memory` 多租户能力的第一版正式落点。
