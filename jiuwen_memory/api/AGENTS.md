# Agent Memory API（接口层）

**规约文档**：[S02-memory-api.md](../../docs/specs/S02-memory-api.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

统一对外 Core API，所有接入形态（SDK/CLI/MCP/HTTP）最终映射到 `MemoryAPI`。本层是控制层的薄封装：做参数装配与鉴权，编排逻辑全部在 `src/control`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `memory_api.py` | MemoryAPI 抽象接口：统一语义定义（add/batch_add/search/list/get/update/delete/evolve/admin/inspect/trace/audit/grant/revoke/space 管理） |
| `memory_api_impl/` | 具体实现目录 |
| `memory_api_impl/assembly.py` | 装配入口：`build_kernel(config)` 构建并暴露 MemoryAPI、Storage、兼容 KV 与控制面句柄 |
| `memory_api_impl/local_memory_api.py` | LocalMemoryAPI：委托 Control 算子并执行 PEP 鉴权与审计 |

## 行为铁律

0. **写入 metadata 明确分区**
   add/batch/update 入口只接收 `system_metadata` 和 `user_metadata`。两者分别校验、
   分别合并并原样委托 Engine；用户过滤规范路径为 `user_metadata.<key>`。

1. **本层不做编排**  
   `MemoryAPI` 只做三件事：鉴权（PEP）、参数装配、委托。编排逻辑（add 路径、search/list 路径、evolve 调度）全部在 `control/MemoryEngine`，禁止在本层堆业务逻辑。

2. **identity 不下沉**  
   鉴权通过后只透传已鉴权的 target `scope`，`identity` 参数不传入控制层/检索层/构建层/存储层。

3. **search 参数拆分在本层边界**
   `search(query, context, *, identity, ...)` 中的 `context: Context` 在本层拆开：
   - `context.scope` 作独立轴穿透到 Engine
   - `context.extensions["max_tokens"]` 由 API 边界解析为 `RetrievalQuery.max_tokens`
   - 其余 `context.extensions` 写入 `RetrievalQuery.extensions`

4. **admin_* 不经 Engine**  
   `admin_get/set/all` 直达 `PolicyManager`，不经过 `MemoryEngine`（Engine 中对应方法抛 NotImplementedError）。

5. **写入同步/异步桥接**
   `add` / `batch_add` 分别桥接对应协程入口；batch 在本层逐项归一化、鉴权、space 校验和审计后委托 Engine，默认按输入顺序返回 partial-success outcomes。

6. **space 必须在 API 边界执行策略校验**
   `scope.require_space=true` 时，具体 target scope 缺少 `space` 的数据面/治理面操作必须在 `LocalMemoryAPI._authorize` 拒绝并记录 deny audit；`Scope()` 根管理面与 org 级 `list_spaces/create_space` 鉴权目标不受此策略影响。

7. **space policy 在 API 边界注入权限上下文**
   已创建 space 的 `principal_path` 由 `SpaceManager.get_policy` 提供，`LocalMemoryAPI._authorize` 在调用 `PermissionManager.check` 前写入 `PermissionContext.metadata["principal_path"]`；调用侧 metadata 不覆盖 space policy。

8. **list 对实际返回资源逐条鉴权**
   请求级 `memory_types` 鉴权通过后，API 必须调用 Engine 的
   `list_with_permission_contexts` 一次取得当前分页及其真源权限上下文，再逐条 READ 鉴权；
   不得把未指定类型解释为可绕过类型路由，也不得用两次分页分别读取上下文和内容。
   API 在委托前复制 `extensions`、规范化 `filters`，并把权限路由值作为系统过滤条件
   与用户过滤做外层 AND；返回 `MemoryListResult` 的 count 为分页前匹配总数。

9. **Space 删除覆盖全部子 Scope**
   `delete_space` 通过 `MemoryEngine.purge_space` 清理同一 `org + space` 下所有 user/agent/session 子 Scope 的真源和索引，再委托 `SpaceManager` 清理 messages 与管理元数据。

## PEP 鉴权流程

```
MemoryAPI.method(scope=target, identity=caller)
  → 构造 PermissionContext（add/search/list 请求条件来自入参；list 实际 unit 与 get/update/delete 来自 Engine 真源元数据）
  → PermissionManager.check(actor=identity, target=scope, action=<对应动作>, context=...)
    → 通过 → 委托 Engine/Governor/PolicyManager（仅传 scope，不传 identity）
    → 拒绝 → 抛 PermissionDeniedError
  → 落审计事件（含 identity + action + target_id + 时间）
```

## 与其他子目录的边界

**本模块管**：
- 统一对外接口定义（语义一致性）
- 鉴权执行（PEP）与入口审计
- 参数装配（context 拆分、RetrievalQuery 组装）
- 同步/异步桥接

**不管**：
- 编排逻辑（归 `control/MemoryEngine`）
- 记忆写入/落盘（归 `construction`）
- 检索链路（归 `retrieval`）
- 存储操作（归 `storage`）
- 策略存储（PolicyManager 实现在 `control`）

## 本地约束

1. `identity` 为必填 keyword-only 参数，与 `scope` 同为 Scope 类型，强制具名传入防止位置传反。
2. 所有数据面方法（add/batch_add/search/list/get/update/delete/evolve）都需要鉴权，治理面（inspect/trace/audit）也需要鉴权。
3. 装配由 `assembly.build_kernel(config)` 完成，经各 Producer 的 `dep/build_named/build` 组装；
   `Kernel.storage` 与 Retriever 引用同一个 `storage.default` 实例。顺序铁律：
   ConfigSource → `kv_store.default`（非已加密则外包 `EncryptedKVStore`）→ `storage.default`
   （composite 再 dep 各 Store）。`RoutingKVStore` 须作为 raw 落在加密层内；同实现换 Redis
   用 `kv_store.url` 晚绑定，不要为换 URL 预装多套 Routing 槽位（F01 §2.1.5 / S08）。
4. 实现类（LocalMemoryAPI）不对外暴露，外部只依赖 `MemoryAPI` 抽象接口。
5. `job_status` 统一查询 Scheduler 和长耗时 Ingest 任务；Ingest 任务必须显式传入
   target `scope`，由 API 对任务真实 Scope 执行 READ 鉴权并记录审计。
