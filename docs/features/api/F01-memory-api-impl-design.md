# F01 — 记忆接口层实现规约（src/api/memory_api_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-06-24 |
| 影响范围 | src/api/memory_api.py，src/api/memory_api_impl/{local_memory_api.py,assembly.py}，src/control/engine.py，bootstrap/core/handler.py，docs/specs/S02-memory-api.md（如有） |
| 测试基线 | `pytest tests/unit/api` 全绿（exit 0；含 `test_build_kernel_config` / `test_recall_context`）；list 增量用 `compileall` + 直接 smoke 验证，当前环境缺少 `pytest` / `ruff` 模块 |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档**记忆接口层实现的设计与取舍**：`MemoryAPI` 的单进程实现 `LocalMemoryAPI`（鉴权/审计执行点）与装配落点 `assembly.py`（`build_kernel`/`assemble`/`Kernel`）。
> `MemoryAPI` 的**公开方法签名 / 参数语义 / 返回类型**以接口 `src/api/memory_api.py` 为准（归 spec/接口源码），本文不重复罗列签名，只记录「为什么这样实现」。

---

## 背景

`MemoryAPI`（`src/api/memory_api.py`）是内核的**唯一对外入口**，形态无关——SDK/CLI/Skill/MCP/HTTP·gRPC 各接入形态最终都映射到这同一组语义。调用层只依赖 `api` 这一个包即可触达全部能力与所需类型，无需 import 内核其他包：

```python
from api import (
    assemble, MemoryAPI,               # 入口 + 接口
    Scope, Context, Modality,          # 调用上下文
    MemoryPatch, UpdateMode,           # update
    DeleteSelector, DeleteMode,        # delete
    FilterClause, FilterOp,            # recall 前置过滤
    DisclosureLevel, EvolveMode,       # recall / evolve
    Grant, Action, Channel,            # 授权 / 演进通道
    SpaceSpec, SpaceInfo, SpacePolicy, # space 管理
)
```

`memory_api_impl/` 落两件事：

| 文件 | 角色 |
|---|---|
| `local_memory_api.py` · `LocalMemoryAPI` | 单进程下的 `MemoryAPI` 实现——**鉴权（PEP）+ 入口审计 + 委派**，自身不含编排逻辑 |
| `assembly.py` · `build_kernel`/`assemble`/`Kernel` | 把各层具体实现经 Producer 串成一个可直接调用的内核——「把整个项目串起来」的落点 |

接口层是控制层（`src/control`）的**薄封装**：数据面（write/recall/list/get/update/delete/evolve）委托 `MemoryEngine`，管理面（任务/治理/授权/admin/space 管理）直达对应控制算子；本层只做**参数装配 + 鉴权 + 入口审计**，编排逻辑全在控制层。

---

## 决策

### 1. PEP 在接口层：鉴权 + 审计 + 只透传已鉴权的 target

`LocalMemoryAPI` 是策略执行点（PEP）。每个涉及租户数据/治理的方法，统一走两个私有公共点：

- `_authorize(identity, target, action)` → `PermissionManager.check(...)`，不通过抛 `PermissionDeniedError`；
- `_log(identity, action, target_id)` → 落一条 `layer="api"` 的入口审计事件（带 identity）。

通过后才委托引擎/控制算子，且**只下沉已鉴权的 target `scope`，identity 不下沉**（下游信任 target）。常见「本人操作自己」场景 `identity == scope`。

### 2. `identity` 一律 keyword-only

每个方法的 `identity` 都在 `*` 之后、必须具名传 `identity=...`。两者同为 `Scope` 类型（target scope 与 caller identity），强制具名可杜绝位置传反导致的**越权**。这是刻意设计，不是风格偏好。

### 3. Context 只活在接口层，边界处拆包

`recall` 收到的 `Context` 在边界即被拆开，**Context 对象本身不进内核**：

- `context.scope` → 照旧作独立轴下推（先鉴权、再作检索隔离轴）；
- `context.extensions["max_tokens"]` → API 边界解析为 int 后写入 `RetrievalQuery.max_tokens` 由披露阶段消费；
- 其余 `context.extensions` → 写入调用级 options，顺 parser 透传给（用户自定义的）检索模块按约定 key 取用。

`extensions["max_tokens"]` 是接口层解释的约定 key，解析后从透传 extensions 中移除；其他 extensions 仍保持内核不解释的不透明透传语义。

### 4. 异步内核 + 同步桥接

引擎是异步的。`write_async` 是**直通引擎的真协程**，供事件循环/高并发接入形态（HTTP/MCP）非阻塞调用；同步 `write`（及 recall/list/get/update/delete/evolve）是它的**同步桥接**——内部 `asyncio.run(...)` 包一层，供 CLI/脚本直接调用。一套语义两个入口，避免同步/异步双实现漂移。

### 5. 数据面委托引擎，管理面直达控制算子

- **数据面** write/recall/list/get/update/delete/evolve → `MemoryEngine`；
- **管理面** job_status/job_cancel → `Scheduler`，admin_* → `PolicyManager`，inspect/trace/audit → `Governor`，grant/revoke → `PermissionManager`，create/get/list/update/archive/delete/export/usage/policy/member space 管理 → `SpaceManager`。

接口层不替管理面做编排，直达对应控制算子，职责清晰。

### 6. 管理面闸门 = 根 scope 鉴权

admin（运行时策略）与全局 audit 查询**没有具体 target scope**，统一以「根 scope」`_ROOT = Scope()` 为鉴权目标：真实 RBAC 后端下「能对全局根 scope 行权」即等价于管理员闸门，`allow_all` 装配下为 no-op。租户数据/治理方法仍按各自 target scope 鉴权；org 级 `create_space/list_spaces` 以 `Scope(org=...)` 做管理面鉴权，不受 `scope.require_space` 拦截。

### 7. 任务鉴权：先取任务、再按其 scope 判权

`job_status`/`job_cancel` 先 `Scheduler.status(job_id)` 取到任务（含其 scope），再据 identity 对该 scope 判权（status 为只读查询，先取后判权不产生副作用；cancel 按 WRITE，与 evolve 触发一致）——保证只能查/操作自身或已授权范围的任务。

### 8. delete 的 scope 兜底：未限定 scope 退根闸门

`delete` 按 `selector.scope` 鉴权 DELETE；未限定 scope（纯按 id/标签的跨范围删除）则退到 `_ROOT` 闸门，要求更高权限——避免「不指定范围」绕过隔离。

### 9. 装配（assembly.py）：Producer.dep + 默认上下文 + 合并覆盖 + kv 注入

`build_kernel` 的串接策略：

- **取依赖统一走 `dep`**：`XProducer.dep(root, default=...)` 按配置值分派（引用名→`build_named` 共享 / 内联 dict→`build` 匿名 / 缺省→按 `default` 匿名新建）；字段名默认取各 Producer 的 `TOP_NAME`。根组件经 `ROOT_PARAMS` 引用各命名空间下的 `default` 实例。
- **默认装配**：无 config 时用内置默认上下文（`config.defaults`）——纯内存离线栈，用显式具名 + 引用复刻共享拓扑；用户 config 经 `AssemblyContext.merged` **合并覆盖**到其上（只写要改动的部分）。
- **policies 便捷覆盖**：`policies` 折进 `globals["policies"]`。
- **真源 kv 注入**：`kv` 入参经 `KvProducer.put(KV_DEFAULT_NAME, kv)` 预置进缓存，**覆盖配置的 kv_store 选择并被各处共享**（如传 `SQLiteKVStore` 即落盘）。
- **多次装配隔离**：组装前 `_register_all()`（各层 bootstrap 幂等自注册）+ `Factory.reset_all()` 清空具名实例缓存。
- **Kernel 多暴露 kv/space 句柄**：`build_kernel` 返回 `Kernel{api, kv, space}`，比 `assemble`（只返回 api）多给真源 kv 与 SpaceManager 句柄，供测试、特殊装配或调试场景观测真源；接入 surface 的普通数据面能力应走 `MemoryAPI`，例如 list 已由 `MemoryAPI.list` 承接。

```python
from api import assemble, build_kernel
from config import Config

api = assemble()                                  # 默认纯内存离线栈
api = assemble(config=Config(...), policies={"rerank_enabled": "true"})
api = assemble(kv=SQLiteKVStore("mem.db"))        # 注入落盘真源
kernel = build_kernel(config=Config(...))         # 另需真源 kv 句柄时
```

### 10. list 升级为正式数据面接口

`bootstrap` 表面已有 `list` verb，但此前接口层没有正式 `MemoryAPI.list(...)`，
容易让不同接入形态各自实现枚举逻辑，甚至把“surface 直扫 KV”误读成长期架构。
参考 mem1.0 `list_memories(user_id, scope_id, offset, limit, mem_types)` 的核心语义，
本层把 list 收口到统一 API：

```python
def list(
    self,
    scope: Scope,
    *,
    identity: Scope,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
) -> list[MemoryUnit]:
    ...
```

设计目标：

- `MemoryAPI.list` 做 READ 鉴权、入口审计和参数委托。
- `MemoryEngine.list` 做 scope 内真源枚举，支持 offset/limit 分页和 `memory_types` 过滤。
- bootstrap `handler._list` 只解析 payload 并委托 `srv.api.list(...)`，不再直连 KV。
- 返回范围只包含 `/memory/` 前缀的已建索引 `MemoryUnit`，不返回 `/messages/` 下的 infer 原文缓存。
- list 是范围枚举，不走 `MemoryPipeline`；pipeline 仍只负责构建/查询组件绑定。

API 层权限上下文：

- 未传 `memory_types`：构造一次 `PermissionContext(resource_type="memory_list")`，按普通 READ 鉴权。
- 显式传一个或多个类型：按清洗去重后的类型列表逐个构造
  `PermissionContext(resource_type="memory_list", memory_type=<type>)`，全部通过才委托 Engine。
- 这样 `RoutingPermissionManager(route_key="memory_type")` 可以让不同记忆类型使用不同权限逻辑，
  且多类型请求不会绕过更严格的类型策略。

Engine 层枚举语义：

- 校验 `offset >= 0`、`limit > 0`。
- 调用 `KVStore.list(scope, prefix=MEMORY_KEY_PREFIX)`，只加载 `/memory/` 记录。
- `memory_types` 过滤优先读取 `unit.metadata["memory_type"]`，缺省退回 `unit.tier.value`。
- 按 `unit.temporal.t_ingest` 倒序返回，`unit.id` 作为稳定次级排序键。
- 返回 `units[offset:offset + limit]`；响应中的 `count` 是本页数量，不是过滤后的总数。

bootstrap payload 兼容：

| 字段 | 语义 |
|---|---|
| `tenant_id` + `scope` | target scope |
| `actor_*` | 可选调用方身份覆盖，沿用现有 dispatch 身份拆分 |
| `offset` | 非负整数，默认 0 |
| `limit` | 正整数，默认 100 |
| `memory_types` / `mem_types` / `memory_type` | 记忆类型过滤；可为字符串列表，或逗号分隔字符串 |

拒绝的方案：

- **handler 继续直扫 KV**：绕开 API 鉴权/审计，也会让 SDK/MCP/HTTP 的 list 语义分裂。
- **复用 recall 返回全量**：会混淆“相关性检索”和“范围枚举”，并受到召回通道、top_k、阈值和披露策略影响。
- **list 支持跨 scope**：枚举多个 scope 属于治理/管理能力，不放进普通数据面读接口。
- **只用 tier 命名过滤参数**：mem1.0 是 `mem_types`，本层选择更中性的 `memory_types`；
  handler 接受 `mem_types` 与 `memory_type` 作为兼容别名。

验证覆盖：

- `tests/unit/api/test_memory_api_list.py`：分页、`memory_types` 过滤、scope 隔离、`/messages/` 不外泄、
  非法 offset/limit、单一/多值 memory_type 权限路由。
- `tests/unit/api/test_dispatch_management_compat.py`：bootstrap `list` 委托 `MemoryAPI.list`，
  以及 handler 对 offset/limit/memory_types 的入参校验。

---

## 拒绝的方案

- **`identity` 作为位置参数**：被拒。与 target `scope` 同为 `Scope`，位置传反即静默越权；强制 keyword-only 把错误挡在调用处。
- **Context 对象下沉进内核**：被拒。Context 是接口层的打包容器，下沉会让内核耦合「调用形态」；改为边界拆包，scope 独立下推，extensions 中的约定 key 由 API 边界解释，其余 extensions 透传。
- **接口层承担编排**：被拒。api 层只做 PEP + 参数装配 + 审计，所有编排（write 的规约/索引、recall 的多路召回/融合/披露、evolve 的阶段调度）留在 `src/control` 与各算子层，保证入口薄、可替换接入形态。
- **管理面也走 MemoryEngine**：被拒。engine 聚焦数据面；任务/策略/治理/授权直达对应控制算子，避免 engine 变成「什么都转发」的上帝对象。
- **同步实现整套内核**：被拒。内核选异步（适配 HTTP/MCP 高并发），同步入口用 `asyncio.run` 桥接；避免维护同步/异步两份实现导致语义漂移。
- **admin/全局 audit 按调用方自身 scope 鉴权**：被拒。这类操作无具体 target scope，按自身 scope 判权会让任意用户都「对自己有权」从而绕过管理面；改用根 scope 闸门统一表达管理员权限。
- **隐式按字段名共享装配依赖**：被拒（随 config/factory 重构）。共享改为「显式具名 + 引用」，`build_kernel` 用默认上下文复刻共享拓扑，用户只覆盖差异。

---

## 验证

- 历史基线：`pytest tests/unit/api` 全绿（exit 0）。
  - `test_build_kernel_config`：装配路径——默认上下文、用户 config 合并覆盖、`build_named` 具名共享、顶层名校验、kv 注入。
  - `test_recall_context`：Context 边界拆包（scope、extensions 约定 key、其余 extensions）与 recall 端到端。
- 鉴权/审计语义随控制层 `tests/unit/control/` 一并回归（PEP 在接口层，闸门行为在 `allow_all` 与真实 PermissionManager 下分别覆盖）。
- list 增量：`compileall` 与直接 smoke 已覆盖分页、类型过滤、`/messages/` 不外泄、权限路由和 handler 委托；当前环境缺少 `pytest` / `ruff` 模块，未能运行标准命令。

---

## 已知遗留

- **同步方法不可在运行中的事件循环内调用**：`write`/`recall`/… 内部 `asyncio.run`，在已有 event loop 的环境（如 async 框架内）须改用 `write_async` 等协程入口，否则 `asyncio.run` 报错。
- **identity 不下沉 = 下游信任 target**：鉴权只在接口层做一次，下游算子信任传入的 target scope；若未来出现「下游需二次校验」的场景，需要显式传递鉴权上下文。
- **默认装配是本地 SQLite owner-only ACL**：`assemble()` 默认 `permission=sqlite(db_path=":memory:")`，owner 访问自己的 target scope 默认放行，同租户跨 scope 默认拒绝；测试或开发若要完全放行需显式装配 `allow_all`。
- **管理面闸门粒度粗**：admin/全局 audit 统一走根 scope，尚无更细的「按策略键/按 layer」分权；待真实 RBAC 后端细化。
