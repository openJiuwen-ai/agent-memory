# F01 — 记忆接口层实现规约（jiuwen_memory/api/memory_api_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-01 |
| 影响范围 | jiuwen_memory/api/，jiuwen_memory/control/，jiuwen_memory/storage/，jiuwen_memory/common/type_def/，bootstrap/core/handler.py，docs/specs/S02-memory-api.md，docs/specs/S03-control.md，docs/specs/S06-storage.md，docs/specs/S07-common.md |
| 测试基线 | list 相关 API/handler/Engine/KV/common/retrieval 单测通过；ruff、compileall 与 `git diff --check` 通过；完整 `tests/unit` 仅两项因环境缺少 torch 失败 |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档**记忆接口层实现的设计与取舍**：`MemoryAPI` 的单进程实现 `LocalMemoryAPI`（鉴权/审计执行点）与装配落点 `assembly.py`（`build_kernel`/`assemble`/`Kernel`）。
> `MemoryAPI` 的**公开方法签名 / 参数语义 / 返回类型**以接口 `jiuwen_memory/api/memory_api.py` 与 [S02](../../specs/S02-memory-api.md) 为准，本文不重复罗列签名，只记录「为什么这样实现」。

---

## 背景

`MemoryAPI`（`jiuwen_memory/api/memory_api.py`）是内核的**唯一对外入口**，形态无关——SDK/CLI/Skill/MCP/HTTP·gRPC 各接入形态最终都映射到这同一组语义。调用层只依赖 `api` 这一个包即可触达全部能力与所需类型，无需 import 内核其他包：

```python
from api import (
    assemble, MemoryAPI,               # 入口 + 接口
    Scope, Context, Modality,          # 调用上下文
    MemoryPatch, UpdateMode,           # update
    DeleteSelector, DeleteMode,        # delete
    FilterClause, FilterOp,            # search 前置过滤
    DisclosureLevel, EvolveMode,       # search / evolve
    Grant, Action, Channel,            # 授权 / 演进通道
    SpaceSpec, SpaceInfo, SpacePolicy, # space 管理
)
```

`memory_api_impl/` 落两件事：

| 文件 | 角色 |
|---|---|
| `local_memory_api.py` · `LocalMemoryAPI` | 单进程下的 `MemoryAPI` 实现——**鉴权（PEP）+ 入口审计 + 委派**，自身不含编排逻辑 |
| `assembly.py` · `build_kernel`/`assemble`/`Kernel` | 把各层具体实现经 Producer 串成一个可直接调用的内核——「把整个项目串起来」的落点 |

接口层是控制层（`jiuwen_memory/control`）的**薄封装**：数据面（add/search/list/get/update/delete/evolve）委托 `MemoryEngine`，管理面（任务/治理/授权/admin/space 管理）直达对应控制算子；本层只做**参数装配 + 鉴权 + 入口审计**，编排逻辑全在控制层。

---

## 决策

### 1. PEP 在接口层：鉴权 + 审计 + 只透传已鉴权的 target

`LocalMemoryAPI` 是策略执行点（PEP）。每个涉及租户数据/治理的方法，统一走两个私有公共点：

- `_authorize(security.auth.actor, target, action)` → `PermissionManager.check(...)`，不通过抛 `PermissionDeniedError`；
- `_log(security.auth.actor, action, target_id)` → 落一条 `layer="api"` 的入口审计事件（带认证 actor）。

通过后才委托引擎/控制算子，且**只下沉已鉴权的 target `scope`，security 不下沉**（下游信任 target）。常见「本人操作自己」场景是 `security.auth.actor == scope`。

### 2. `security` 一律 keyword-only

每个方法的 `security` 都在 `*` 之后、必须具名传 `security=...`；调用方身份只能从
`security.auth.actor` 取得。这样不会把安全上下文误当成 target `Scope` 传入，也避免调用方
伪造 actor。`check_write` 为兼容历史调用保留第二个位置参数，但仍要求传入完整的
`RequestSecurityContext`。

### 3. Context 只活在接口层，边界处拆包

`search` 收到的 `Context` 在边界即被拆开，**Context 对象本身不进内核**：

- `context.scope` → 照旧作独立轴下推（先鉴权、再作检索隔离轴）；
- `context.extensions["max_tokens"]` → API 边界解析为 int 后写入 `RetrievalQuery.max_tokens` 由披露阶段消费；
- 其余 `context.extensions` → 写入调用级 options，顺 parser 透传给（用户自定义的）检索模块按约定 key 取用。

`extensions["max_tokens"]` 是接口层解释的约定 key，解析后从透传 extensions 中移除；其他 extensions 仍保持内核不解释的不透明透传语义。

### 4. 异步内核 + 同步桥接

引擎是异步的。`add_async` 是**直通引擎的真协程**，供事件循环/高并发接入形态（HTTP/MCP）非阻塞调用；同步 `add`（及 search/list/get/update/delete/evolve）是它的**同步桥接**——内部 `asyncio.run(...)` 包一层，供 CLI/脚本直接调用。一套语义两个入口，避免同步/异步双实现漂移。

### 5. 数据面委托引擎，管理面直达控制算子

- **数据面** add/search/list/get/update/delete/evolve → `MemoryEngine`；
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
- **Kernel 暴露统一 Storage 与控制句柄**：`build_kernel` 返回
  `Kernel{api, storage, kv, space, config_source}`，其中 `storage` 是上层统一依赖，`kv` 是迁移期
  的真源兼容句柄；`assemble` 仍只返回 api。普通数据面能力应走 `MemoryAPI`。

```python
from api import assemble, build_kernel
from config import Config

api = assemble()                                  # 默认纯内存离线栈
api = assemble(config=Config(...), policies={"rerank_enabled": "true"})
api = assemble(kv=SQLiteKVStore("mem.db"))        # 注入落盘真源
kernel = build_kernel(config=Config(...))         # 另需真源 kv 句柄时
```

### 10. HTTP/多 surface 的结构化 dispatch 边界

HTTP、CLI、MCP 和进程内调用共享同一个 handler，但不共享未经约束的 payload 形状。HTTP
采用嵌套 `target` DTO，并在 adapter 中一次性完成字段白名单、类型和 Scope 校验，再构造
不可变 `DispatchRequest`；认证 actor 由 `RequestSecurityContext` 提供，绝不从请求体推断。
这样可以让 actor、target 和业务 payload 在进入 API 前保持明确分离，避免 handler 中存在多套
隐式兼容解析路径。

`DispatchRequest` 是 transport 与 API 的稳定边界：handler 只消费其中的结构化 actor、target、
grantee/member 及 batch item，不再读取 `__target`、`__actor` 等保留字段或从 flat 字段重新组装
Scope。CLI、MCP 和旧进程内调用若仍使用 flat 输入，必须显式经过
`bootstrap/core/legacy_request_adapter.py`；HTTP adapter 则只接受结构化 DTO。HTTP 的
`space_id` 兼容别名在 parser 内归一化为 `Scope.space`，后续授权、审计和存储只看到规范 Scope。

该边界同时保证认证、授权和审计使用同一组分离值：认证 actor 从
`RequestSecurityContext.auth.actor` 取得并以 `security=` 传入 API，嵌套
`target` 作为授权与审计目标，API 通过 `PermissionManager.check(security.auth.actor, target, action)`
后仅向 Control 下沉已鉴权 target。batch item target 采用完整替换而非按维度隐式合并；普通
batch 不支持逐 item actor，避免在单次写入中混淆身份来源。

### 11. list 升级为正式数据面接口

`bootstrap` 表面已有 `list` verb，但此前接口层没有正式 `MemoryAPI.list(...)`，
容易让不同接入形态各自实现枚举逻辑，甚至把“surface 直扫 KV”误读成长期架构。
参考 mem1.0 `list_memories(user_id, scope_id, offset, limit, mem_types)` 的核心语义，
本层把 list 收口到统一 API：

当前默认实现的职责链是
`MemoryAPI.list -> MemoryEngine.list_with_permission_contexts -> KVStore.list`。
`KVStore.list` 在完整 `Scope` 内完成 `/memory/` MemoryUnit 的类型/FilterExpr 过滤、精确
计数、稳定排序和分页；Engine 只反序列化当前页并生成权限上下文。

```python
def list(
    self,
    scope: Scope,
    *,
    security: RequestSecurityContext,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    extensions: dict[str, str] | None = None,
    filters: FilterExpr | list[FilterClause] | dict | None = None,
) -> MemoryListResult:
    ...
```

设计目标：

- `MemoryAPI.list` 做 READ 鉴权、入口审计和参数委托。
- `MemoryEngine.list` 校验分页参数并把查询参数完整委托 `KVStore.list`。
- bootstrap `handler._list` 只解析 payload 并委托 `srv.api.list(...)`，不再直连 KV。
- 返回范围只包含 `/memory/` 前缀的已建索引 `MemoryUnit`，不返回 `/messages/` 下的 infer 原文缓存。
- list 是范围枚举，不走 `MemoryPipeline`；pipeline 仍只负责构建/查询组件绑定。

API 层权限上下文：

- 未传 `memory_types`：构造一次 `PermissionContext(resource_type="memory_list")`，按普通 READ 鉴权。
- 显式传一个或多个类型：按清洗去重后的类型列表逐个构造
  `PermissionContext(resource_type="memory_list", memory_type=<type>)`，全部通过才委托 Engine。
- 这样 `RoutingPermissionManager(route_key="memory_type")` 可以让不同记忆类型使用不同权限逻辑，
  且多类型请求不会绕过更严格的类型策略。

KV 层列表语义：

- 校验 `offset >= 0`、`limit > 0`。
- `KVStore.list` 只加载 `/memory/` 记录。
- `memory_types` 过滤优先读取 `unit.metadata["memory_type"]`，缺省退回 `unit.tier.value`。
- 按 `unit.temporal.t_ingest` 倒序返回，`unit.id` 作为稳定次级排序键。
- 返回当前页 `items`；响应中的 `count` 是过滤后的分页前总数。

bootstrap payload 兼容：

| 字段 | 语义 |
|---|---|
| `tenant_id` + `scope` | target scope |
| `actor_*` | HTTP 请求拒绝；仅保留为非 HTTP 旧 dispatch 调用的兼容输入，不能作为 HTTP 身份来源 |
| `offset` | 非负整数，默认 0 |
| `limit` | 正整数，默认 100 |
| `memory_types` / `mem_types` / `memory_type` | 记忆类型过滤；可为字符串列表，或逗号分隔字符串 |

拒绝的方案：

- **handler 继续直扫 KV**：绕开 API 鉴权/审计，也会让 SDK/MCP/HTTP 的 list 语义分裂。
- **复用 search 返回全量**：会混淆“相关性检索”和“范围枚举”，并受到召回通道、top_k、阈值和披露策略影响。
- **list 支持跨 scope**：枚举多个 scope 属于治理/管理能力，不放进普通数据面读接口。
- **只用 tier 命名过滤参数**：mem1.0 是 `mem_types`，本层选择更中性的 `memory_types`；
  handler 接受 `mem_types` 与 `memory_type` 作为兼容别名。

验证覆盖：

- `tests/unit/api/test_memory_api_list.py`：分页、`memory_types` 过滤、scope 隔离、`/messages/` 不外泄、
  非法 offset/limit、单一/多值 memory_type 权限路由。
- `tests/unit/api/test_dispatch_management_compat.py`：bootstrap `list` 委托 `MemoryAPI.list`，
  以及 handler 对 offset/limit/memory_types 的入参校验。

#### 11.1 List 自定义参数、过滤与结果总数增量设计（2026-07-30，已实现）

List 需要在“按 Scope 枚举”基础上支持调用方自定义参数和结构化过滤，同时返回过滤后的
结果总数。目标接口调整为：

```python
@dataclass
class MemoryListResult:
    items: list[MemoryUnit] = field(default_factory=list)
    count: int = 0


def list(
    self,
    scope: Scope,
    *,
    security: RequestSecurityContext,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    extensions: dict[str, str] | None = None,
    filters: FilterExpr | list[FilterClause] | dict | None = None,
) -> MemoryListResult:
    ...
```

Python API 使用 `filters` 作为规范参数名，与 `search` 保持一致；bootstrap payload 以
`filters` 为规范字段，同时兼容调用方使用单数 `filter`。两者同时出现时拒绝请求，避免
静默选择其中一份条件。

`extensions` 的契约与 `RetrievalQuery.extensions` 一致：

- 类型为 `dict[str, str]`；API 边界复制并把传输层值规范为字符串，避免调用方后续修改
  原字典影响正在执行的请求。
- 内核默认实现不解释业务 key，必须沿
  `MemoryAPI -> MemoryEngine -> KVStore.list` 完整透传；自定义 Engine 或 KV
  后端可按约定消费，未知 key 不报错。
- `extensions` 不得改变 `scope`、绕过权限或覆盖系统过滤谓词。若某个扩展值参与权限路由，
  API 必须像 search 一样把对应路由值回注为系统等值过滤条件，并与用户 filters 做外层
  `AND`，确保“按什么条件授权，就只列出什么范围的数据”。
- `None` 与空字典都表示没有自定义参数。非字典输入在 API/handler 边界抛
  `ValidationError`，不静默丢弃。

`filters` 复用 retrieval 的完整过滤契约：

- 接受单个 `FilterClause`、`FilterExpr` 树、旧式 `list[FilterClause]` 和 dict DSL；
  在 API 边界统一通过 `normalize` 转成 `FilterExpr | None`。
- 支持 `EQ/NE/IN/NOT_IN/GT/GTE/LT/LTE/CONTAINS` 以及 `AND/OR/NOT`。
- Scope 是独立的强隔离轴，`org/space/user/agent/session` 不允许放入 filters；
  用户过滤只能进一步收窄已鉴权的 target Scope，不能扩大查询范围。
- `memory_types` 保留为兼容快捷参数，与 filters 是 `AND` 关系；类型匹配仍优先读取
  `unit.metadata["memory_type"]`，缺省退回 `unit.tier.value`。
- 规范化后的 filters、`memory_types`、`extensions`、`offset` 和 `limit` 必须由 Engine
  原样下推 KV 查询契约；过滤必须在存储适配器内部先于排序、`offset` 和 `limit` 执行，
  禁止 Engine 先取一页再做后置过滤。

通用 `KVStore.scan(scope, prefix)` 是无业务语义的字节扫描能力，供 lifecycle、
offboarding 和兼容代码使用；不能直接在该方法上堆叠 MemoryUnit 过滤参数。KV 契约新增
面向 `/memory/` 真源的结构化查询：

```python
@dataclass
class KVMemoryListResult:
    entries: list[tuple[str, bytes]] = field(default_factory=list)
    count: int = 0


class KVStore:
    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        ...
```

参数传递规则：

- API 对 `extensions` 做防御性复制和字符串值规范化，对 filters 做 AST 规范化。
- Engine 校验分页参数后，以同名 keyword 参数直接调用 `KVStore.list`。
- KV 实现将 `memory_types/filters/extensions` 视为只读参数；需要异步保存或跨请求缓存时
  自行复制。
- `KVMemoryListResult` 由同一次存储查询返回当前页 entries 和分页前 count，保证两者基于
  相同的数据状态。

`KVStore.list` 只查询 `MEMORY_KEY_PREFIX` 下由 `memory_codec.dumps` 写入的 MemoryUnit，
不包含 `/messages/` 或其他普通 KV 记录。各 KV 实现必须保证：

- 在完整 Scope 内执行查询，不能跨 Scope/Space。
- 返回的 `entries` 已完成 filters、memory_types、稳定排序和分页。
- `count` 是同一查询条件下的分页前精确总数。
- `extensions` 完整到达后端；默认后端可以不解释未知 key，但不能在 Engine 层提前丢弃。
- 原生支持 JSON/metadata 查询和 count 的后端可直接下推；不支持的内存、加密或简单
  key-value 后端在自身适配器内执行“枚举、解码、过滤、计数、排序、分页”兼容回退。
- `EncryptedKVStore` 不能把 MemoryUnit filters 直接下推到密文 raw KV；它必须先解密再执行
  兼容回退，或者依赖不泄露敏感值的独立可查询 metadata sidecar。无 sidecar 时不能用密文
  条数近似过滤后 count。

新增 `KVStore.list` 是抽象接口变更，当前所有 KV 实现都必须同步修改，不能只在
默认 `memory` 后端实现：

| KV 实现 | `list` 首版策略 | 约束 |
|---|---|---|
| `InMemoryKVStore` | 在目标 Scope 的 `/memory/` entries 上解码并使用公共 MemoryUnit filter evaluator，再精确计数、排序、分页 | 不得依赖 dict 遍历顺序 |
| `SQLiteKVStore` | 当前 bytes schema 使用公共 evaluator 回退；先按 Scope 和 `/memory/` 前缀取值，再解码、过滤、计数、排序、分页 | 不拼接 FilterExpr SQL；后续 schema 支持 JSON 查询后再单独增加原生下推 |
| `RedisKVStore` | 普通 Redis bytes 模式没有通用 metadata query，首版按 namespaced `/memory/` key 扫描并使用公共 evaluator；将来启用 RedisJSON/搜索索引时可增加方言编译器 | 不得把不同 Scope 的 key 混入候选 |
| `EncryptedKVStore` | 先取得并解密目标 Scope 的 `/memory/` entries，再使用公共 evaluator；若未来有安全 metadata sidecar 才允许原生下推 | filters/extensions 不能直接委托给看不到明文的 raw KV |

`extensions` 必须传到以上每个实现。默认实现忽略未知 key；某实现声明并消费自定义 key 时，
必须在该实现的配置/文档中定义语义，不允许不同后端对同名 key 给出相反含义。

FilterExpr 到“KV 可执行条件”的转换不能是一份固定查询字符串，因为各后端没有共同查询
语言。可复用边界分成三层：

1. **公共 AST 与校验**：继续由 `common.type_def.filter.normalize` 负责，把单 clause、旧 list
   和 dict DSL 收口成合法的 `FilterExpr | None`。
2. **公共树遍历**：复用 `common.type_def.filter.evaluate` 递归处理 `AND/OR/NOT`。
3. **公共 MemoryUnit 求值**：`matches_memory_unit(unit, expr)` 统一字段投影和叶子比较；
   当前 memory/sqlite/redis/encrypted 四个实现都用该兼容路径。后续后端如需原生下推，
   由各自方言编译 FilterExpr，不改变 `KVStore.list` 契约。

公共 `matches_memory_unit` 已从 retrieval UnitReader 的字段投影和叶子比较逻辑中提取，
统一 `tags/tier/source/lifecycle/unit_id/t_event/t_valid/t_invalid/metadata.*` 的取值、缺值、
集合和类型比较语义。Retrieval 真源复核与 KV 扫描回退都调用它，禁止各自复制一套比较逻辑。
“树形结构遍历”和“MemoryUnit 过滤语义”可以通用；“如何表达为 SQL、Milvus 字符串或
Elasticsearch DSL”必须由方言适配，不能伪装成跨后端通用查询语句。

`CloudEngine` 与 `InMemoryEngine` 的 List 能力必须对齐：

- `list`、`list_with_permission_contexts` 接受完全相同的
  `offset/limit/memory_types/extensions/filters`，返回相同的 items/count 语义。
- 两个 Engine 都直接调用同一个 `KVStore.list` 契约，不允许一个下推 KV、另一个
  在 Engine 内全量扫描。
- 两者都只反序列化 KV 返回的当前页 entries，并从同一批 MemoryUnit 构造逐项
  PermissionContext，避免 items 与鉴权上下文来自两次查询。
- `offset/limit` 校验、空 filters/extensions、稳定排序、count 和异常语义完全一致；公共
  逻辑提取到 Engine 共享 helper，两个实现不复制 `_list_page`。
- 唯一允许的能力差异是既有部署边界：`InMemoryEngine` 继续拒绝非空 `scope.space`，
  `CloudEngine` 支持命名 Space。该差异不改变 List 查询、过滤和返回协议。

默认执行顺序固定为：

```text
MemoryAPI
  -> 规范化 extensions/filters，完成请求级 READ 鉴权
  -> MemoryEngine 校验分页参数，不解释、复制或丢弃查询参数
  -> KVStore.list(
       scope,
       offset=offset,
       limit=limit,
       memory_types=memory_types,
       filters=filters,
       extensions=extensions,
     )
       -> memory_types AND filters
       -> count = len(全部匹配结果)
       -> t_ingest DESC, id DESC 稳定排序
       -> entries = matches[offset:offset + limit]
  -> Engine 只反序列化当前页 entries，生成 MemoryUnit + PermissionContext
  -> API 对当前页实际 MemoryUnit 二次 READ 鉴权
  -> MemoryListResult(items, count)
```

`MemoryListResult.count` 表示同一 Scope 下同时满足 `memory_types` 和 `filters` 的**分页前
总数**，不受 `offset/limit` 影响；本页数量由 `len(result.items)` 得到。bootstrap 响应中的
`count` 同步改为该总数，`items` 仍只包含当前页。请求级鉴权未通过时不得执行枚举或返回
count；实际资源二次鉴权未通过时整个请求失败，不返回部分 items 或可推断未授权数据规模的
count。

这一阶段继续以 KV 真源查询保证语义正确，不复用 search：list 是确定性范围枚举，不应受
相关性、召回通道、阈值、top-k 或披露策略影响。简单 KV 后端的兼容回退仍需扫描 Scope
下全部 `/memory/` 记录，复杂度为 O(N)，但扫描职责封装在 KV 适配器内，Engine 始终使用
同一个 `list` 契约。生产后端应在分页前完整下推 FilterExpr 并使用原生 count；
不能用“先取 limit 条再过滤/计数”的近似实现替代精确语义。

拒绝的方案：

- **继续返回裸 `list[MemoryUnit]`，另加 count 接口**：同一过滤条件会执行两次，数据在两次
  调用间变化时 items 与 count 不一致。
- **让 `count = len(items)`**：这是本页大小，不是调用方要求的符合条件总数。
- **先分页再过滤**：会造成空页、漏项和错误总数。
- **直接扩展通用 `KVStore.scan`**：该方法还承担 lifecycle/offboarding 的无业务语义字节
  枚举；强塞 MemoryUnit filters 会污染通用契约。新增专用 `list`，但参数和
  执行职责仍完整下推 KV 层。
- **Engine 全量读取后自行过滤**：能实现功能但 extensions 到不了自定义 KV，生产后端也
  无法使用原生 metadata filter/count，且所有 Engine 都会重复扫描逻辑。
- **复用 search 实现过滤 List**：会把确定性枚举错误地绑定到相关性检索语义。

验证覆盖：

- `extensions` 逐层透传、自定义 Engine/KV 可见、非字典输入拒绝且输入字典不被修改。
- dict DSL 与完整 FilterExpr 的各算子/逻辑组合，以及非法 Scope filter 拒绝。
- `memory_types AND filters`、过滤后再分页、稳定排序和 `/messages/` 不外泄。
- `count` 为分页前精确总数，空结果为 0，超出 offset 时 items 为空但 count 保持不变。
- filters/extensions 权限路由值与系统过滤绑定，当前页逐条二次鉴权仍然生效。
- 相同 unit id 在不同 Scope/Space 下的过滤和计数互不影响。
- Engine/KV 记录调用断言 offset/limit/memory_types/filters/extensions 完整透传。
- `memory/sqlite/redis/encrypted` 四个实现运行同一组 `list` 契约测试，覆盖 Scope 隔离和
  encrypted 解密后过滤。
- 公共 FilterExpr truth table 继续由 common/retrieval 单测覆盖；KV list 直接复用同一
  `matches_memory_unit`。
- `CloudEngine` 与默认 `InMemoryEngine` 分别覆盖过滤、extensions 透传、分页和 count；
  命名 Space 仍只由 CloudEngine 支持。

---

## 拒绝的方案

- **`security` 作为位置参数**：被拒。安全上下文必须与 target `scope` 保持不同的类型和
  参数角色；除 `check_write` 的历史兼容位置外统一强制 keyword-only，避免调用处误传或丢失
  认证信息。
- **Context 对象下沉进内核**：被拒。Context 是接口层的打包容器，下沉会让内核耦合「调用形态」；改为边界拆包，scope 独立下推，extensions 中的约定 key 由 API 边界解释，其余 extensions 透传。
- **接口层承担编排**：被拒。api 层只做 PEP + 参数装配 + 审计，所有编排（add 的规约/索引、search 的多路召回/融合/披露、evolve 的阶段调度）留在 `jiuwen_memory/control` 与各算子层，保证入口薄、可替换接入形态。
- **管理面也走 MemoryEngine**：被拒。engine 聚焦数据面；任务/策略/治理/授权直达对应控制算子，避免 engine 变成「什么都转发」的上帝对象。
- **同步实现整套内核**：被拒。内核选异步（适配 HTTP/MCP 高并发），同步入口用 `asyncio.run` 桥接；避免维护同步/异步两份实现导致语义漂移。
- **admin/全局 audit 按调用方自身 scope 鉴权**：被拒。这类操作无具体 target scope，按自身 scope 判权会让任意用户都「对自己有权」从而绕过管理面；改用根 scope 闸门统一表达管理员权限。
- **隐式按字段名共享装配依赖**：被拒（随 config/factory 重构）。共享改为「显式具名 + 引用」，`build_kernel` 用默认上下文复刻共享拓扑，用户只覆盖差异。

---

## 验证

- 历史基线：`pytest tests/unit/api` 全绿（exit 0）。
  - `test_build_kernel_config`：装配路径——默认上下文、用户 config 合并覆盖、`build_named` 具名共享、顶层名校验、kv 注入。
  - `test_search_context`：Context 边界拆包（scope、extensions 约定 key、其余 extensions）与 search 端到端。
- 鉴权/审计语义随控制层 `tests/unit/control/` 一并回归（PEP 在接口层，闸门行为在 `allow_all` 与真实 PermissionManager 下分别覆盖）。
- list 增量：API、handler、CloudEngine、四个 KV 实现、公共过滤求值相关单测通过；
  ruff、compileall 与 `git diff --check` 通过。
- HTTP/结构化 dispatch 增量：`tests/unit/bootstrap/test_http_dto.py`、
  `tests/unit/bootstrap/test_http_server_security.py` 及 HTTP-03 定向回归通过；覆盖
  `target` 字段映射、未知/保留身份字段拒绝、认证 actor 注入、space 别名冲突、batch item
  target 和 handler 仅接收 `DispatchRequest`。

---

## 已知遗留

- **同步方法不可在运行中的事件循环内调用**：`add`/`search`/… 内部 `asyncio.run`，在已有 event loop 的环境（如 async 框架内）须改用 `add_async` 等协程入口，否则 `asyncio.run` 报错。
- **security 不下沉 = 下游信任 target**：鉴权只在接口层做一次，下游算子信任传入的 target scope；若未来出现「下游需二次校验」的场景，需要显式传递鉴权上下文。
- **默认装配是本地 SQLite owner-only ACL**：`assemble()` 默认 `permission=sqlite(db_path=":memory:")`，owner 访问自己的 target scope 默认放行，同租户跨 scope 默认拒绝；测试或开发若要完全放行需显式装配 `allow_all`。
- **管理面闸门粒度粗**：admin/全局 audit 统一走根 scope，尚无更细的「按策略键/按 layer」分权；待真实 RBAC 后端细化。
