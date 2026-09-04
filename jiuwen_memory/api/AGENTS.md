# Agent Memory API（接口层）

**规约文档**：[S02-memory-api.md](../../docs/specs/S02-memory-api.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。
>
> **文档分工**：`docs/design/architecture.md` §6 = 已实现接口清单；本目录 = 已实现代码；S02 = 详细用法与方法总览（含尚未实现标注）；`docs/features/api/`（F01–F04）= 特性决策。尚未实现接口上库后须同步 S02 与 §6。

统一对外 Core API，所有接入形态（SDK/CLI/MCP/HTTP）最终映射到 `MemoryAPI`。本层是控制层的薄封装：做参数装配与鉴权，编排逻辑全部在 `jiuwen_memory/control`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `memory_api.py` | MemoryAPI 抽象接口：统一语义定义（add/batch_add/check_write/submit_ingest/search/list/get/update/delete/evolve/admin/inspect/trace/audit/grant/revoke/space 管理） |
| `memory_api_impl/` | 具体实现目录 |
| `memory_api_impl/assembly.py` | 公开装配：`assemble(config) -> MemoryAPI`、`assemble_runtime(config) -> MemoryRuntime`（仅 api+close）；内部 `_build_kernel` 才持有 KV/Storage/ingest |
| `memory_api_impl/local_memory_api.py` | LocalMemoryAPI facade：构造、属性，公开方法由 mixin 提供 |
| `memory_api_impl/local_support.py` | 入口校验、过滤/谓词、空间投影等无状态辅助函数 |
| `memory_api_impl/pep_ops.py` | PepOpsMixin：空间事实、`_authorize`、审计、`check_write` |
| `memory_api_impl/write_ops.py` | WriteOpsMixin：add/batch 与落点解析，鉴权后走 CommandService |
| `memory_api_impl/query_ops.py` | QueryOpsMixin：search/list/get/update/delete/evolve，鉴权后走 Query/Command |
| `memory_api_impl/admin_ops.py` | AdminOpsMixin：`submit_ingest`、任务、admin、治理、verify_audit、grant/revoke |
| `memory_api_impl/space_ops.py` | SpaceOpsMixin：Space CRUD；`delete_space` 经 SpaceLifecycleService |

## 行为铁律

0. **写入 metadata 明确分区**
   add/batch/update 入口只接收 `system_metadata` 和 `user_metadata`。两者分别校验、
   分别合并并原样委托 Engine；用户过滤规范路径为 `user_metadata.<key>`。

1. **本层不做编排**  
   `MemoryAPI` 只做三件事：鉴权（PEP）、参数装配、委托 typed Control application ports。
   数据面经 `MemoryCommandService` / `MemoryQueryService`，治理经 `GovernanceService`，
   Space 删除事务经 `SpaceLifecycleService`。编排逻辑（write 路径、search/list 取数、
   evolve 调度、purge+delete）全部在 `control`，禁止在本层堆业务逻辑。

2. **调用方身份不下沉**
   身份取自 `security.auth.actor`；鉴权通过后只透传已鉴权的 target `scope`，`security` 及其中的 actor 不传入控制层/检索层/构建层/存储层。

3. **search 参数拆分在本层边界**
   `search(query, context, *, security, ...)` 中的 `context: Context` 在本层拆开：
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
   `delete_space` 鉴权后调用 `SpaceLifecycleService`：先 `MemoryEngine.purge_space`
   清理同一 `org + space` 下所有 user/agent/session 子 Scope 的真源和索引，再委托
   `SpaceManager` 清理 messages 与管理元数据，并汇总 `deleted_counts`。本层只做
   membership 缓存失效与入口审计，不得内联 purge+delete。

## PEP 鉴权流程

```
MemoryAPI.method(scope=target, security=RequestSecurityContext)
  → identity = security.auth.actor（actor 只能来自这里，不来自业务 payload）
  → 构造 PermissionContext（add/search/list 请求条件来自入参；list 实际 unit 与 get/update/delete 来自 Engine 真源元数据）
  → PermissionManager.check(actor=identity, target=scope, action=<对应动作>, context=...)
    → 通过 → 委托 Command/Query/Governance/SpaceLifecycle 端口或 PolicyManager/Scheduler（仅传已鉴权 scope，不传 identity）
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

1. `security` 为必填参数，类型 `common.security.types.RequestSecurityContext`；只能来自 `auth_middleware.authenticated()` 或 `request_context.internal_context()`（过渡期另有 `legacy_request_context()`，实装 PR 删除）。除 `check_write(scope, security, *, ...)` 为兼容旧第二位置参数外，其余公开方法均要求 keyword-only。
2. 授权面（`grant`/`revoke`）的公共类型是 `common.security.types.Grant`/`Action`；`control.types` 只兼容再导出同一对象，不得定义第二套类型或结构转换。`Grant` 必须兼容旧 `grantor`/`grantee`/`actions` 构造形状，`grant_id` 构造时默认留空，actions 在值对象边界冻结并校验。目标形态下 `grant_id` 服务端生成、`revoke` 按 ID 精确定位；接口先行过渡期撤销语义不变，安全域独有动作在委托旧 `PermissionManager` 前 fail-closed。
3. 所有数据面方法（add/batch_add/search/list/get/update/delete/evolve）都需要鉴权，治理面（inspect/trace/audit）也需要鉴权。`LocalMemoryAPI._record_audit()` 在存在受控请求上下文时以 `setdefault` 写入 `AuditEvent.detail["request_id"]`，用于与入口响应和日志关联，不覆盖调用方已经传入的可信 detail 值。
4. `verify_audit`（审计完整性验证，PR3 接口先行）是独立于 `audit` 的管理面入口：新入口按 `VERIFY_AUDIT` 对根 scope 判权；既有 `audit` 为兼容存量按 action 精确匹配的授权记录，仍使用 legacy `READ`，迁移到目标动作 `READ_AUDIT` 不属于本 PR。验证只收服务端参数（不接受调用方传入 digest/key/proof）；provider 与专用 `audit_verify_guard` 必须成对注入，全量验证占一个独立并发槽，耗尽抛 `RateLimitedError`。guard 耗尽发生在授权通过后，审计事件保持 `decision=allow`，并沿用 `workload_guard=exhausted` 表达容量准入失败，不得混入 `decision=deny` 的鉴权拒绝事件。guard 准入后先落验证尝试审计、再调用 provider，provider 抛完整性异常时仍须能追溯发起者与发生时间；成功或异常路径不重复写完成事件。`page_size` / `max_samples` 截到服务端 `globals.audit_verify_max_page_size` / `globals.audit_verify_max_samples` 装配出的可信 `AuditVerificationLimits`；装配边界只接受真正的整数（拒绝 `bool` 和字符串），并把非法类型或范围统一翻译成 `ValidationError`。provider 返回 samples 由 PEP 再截到有效上限。未装配 provider 时返回 `unsupported`，不降级成 clean。真实认证接入前 generic handler 不注册该 verb，HTTP/MCP/CLI 均无一等入口；进程内调用必须显式传安全上下文。
5. 装配由 `assembly.assemble` / `assembly.assemble_runtime` 完成，内部经 `_build_kernel`
   与各 Producer 的 `dep/build_named/build` 组装；Retriever 与内部 `_Kernel.storage`
   引用同一个 `storage.default` 实例。顺序铁律：
   ConfigSource → `kv_store.default`（非已加密则外包 `EncryptedKVStore`）→ `storage.default`
   （composite 再 dep 各 Store）。`RoutingKVStore` 须作为 raw 落在加密层内；同实现换 Redis
   用 `kv_store.url` 晚绑定，不要为换 URL 预装多套 Routing 槽位（F01 §2.1.5 / S08）。
6. 实现类（LocalMemoryAPI）不对外暴露，外部只依赖 `MemoryAPI` 抽象接口。
7. `job_status` 统一查询 Scheduler 和长耗时 Ingest 任务；Ingest 任务必须显式传入
   target `scope`，由 API 对任务真实 Scope 执行 READ 鉴权并记录审计。
8. Access（`jiuwen_memory_entry/`、`jiuwen_memory_adapter/`）只 `import jiuwen_memory.api`，
   且不得 import `jiuwen_memory.api.memory_api_impl`。本包重导出协议转换所需的 DTO /
   枚举 / 异常 / `legacy_request_context` / `Credentials`。公开装配是
   `assemble` / `assemble_runtime`（接受 `dict | Config | None`），Access composition
   root 不 import `jiuwen_memory.config`。`Kernel` / `build_kernel` / `LocalMemoryAPI`
   不是公开包导出；`assemble_runtime` 不带 `kv` / `storage` / `space`。
9. `LocalMemoryAPI` 类体内禁止同名方法重复定义（后定义会静默覆盖先定义）。
   `add` / `add_async`（以及 `batch_add` / `batch_add_async`）是不同方法名的同步/异步入口，
   不属重复。静态检查见 `tests/unit/api/test_local_memory_api_methods.py`。
10. 数据面写经 `MemoryCommandService`，查询经 `MemoryQueryService`，治理经 `GovernanceService`，
    Space 删除事务经 `SpaceLifecycleService`。PEP、路由谓词回注、逐条鉴权仍在本层。
    不得把 `_purge_space_memories` 或内联 purge+delete 收回本类。
