# Agent Memory API（接口层）

**规约文档**：[S02-memory-api.md](../../docs/specs/S02-memory-api.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

统一对外 Core API，所有接入形态（SDK/CLI/MCP/HTTP）最终映射到 `MemoryAPI`。本层是控制层的薄封装：做参数装配与鉴权，编排逻辑全部在 `src/control`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `memory_api.py` | MemoryAPI 抽象接口：统一语义定义（write/batch_write/recall/list/get/update/delete/evolve/admin/inspect/trace/audit/grant/revoke/space 管理） |
| `memory_api_impl/` | 具体实现目录 |
| `memory_api_impl/assembly.py` | 装配入口：`build_kernel(config)` 递归构建 MemoryAPI 实例 |
| `memory_api_impl/local_memory_api.py` | LocalMemoryAPI：委托 Engine/Governor/Scheduler/SpaceManager + PEP 鉴权（调 `common.security.authorization` 的 Authorizer 作 PDP） |

## 行为铁律

1. **本层不做编排**  
   `MemoryAPI` 只做三件事：鉴权（PEP）、参数装配、委托。编排逻辑（write 路径、recall/list 路径、evolve 调度）全部在 `control/MemoryEngine`，禁止在本层堆业务逻辑。

2. **security 不下沉**  
   鉴权通过后只透传已鉴权的 target `scope`，`security`（`RequestSecurityContext`）不传入控制层/检索层/构建层/存储层。

3. **recall 参数拆分在本层边界**  
   `recall(query, context, *, security, ...)` 中的 `context: Context` 在本层拆开：
   - `context.scope` 作独立轴穿透到 Engine
   - `context.extensions["max_tokens"]` 由 API 边界解析为 `RetrievalQuery.max_tokens`
   - 其余 `context.extensions` 写入 `RetrievalQuery.extensions`

4. **admin_* 不经 Engine**  
   `admin_get/set/all` 直达 `PolicyManager`，不经过 `MemoryEngine`（Engine 中对应方法抛 NotImplementedError）。

5. **write/write_async 分离**  
   `write` 是同步桥接（内部 `asyncio.run(write_async)`），供 CLI/脚本使用；`write_async` 直通 Engine 协程，供事件循环形态使用。

   `batch_write` / `batch_write_async` 同样只保留一套异步实现；每个归一化 item 独立经过
   PEP，随后只把已鉴权的 `BatchWriteItem` 交给 Engine，`security` 不下沉。

6. **space 必须在 API 边界执行策略校验**
   `scope.require_space=true` 时，具体 target scope 缺少 `space` 的数据面/治理面操作必须在 `LocalMemoryAPI._authorize` 拒绝并记录 deny audit；`Scope()` 根管理面与 org 级 `list_spaces/create_space` 鉴权目标不受此策略影响。

7. **space policy 在 API 边界注入权限上下文**
   已创建 space 的 `principal_path` 由 `SpaceManager.get_policy` 提供，`LocalMemoryAPI._authorize` 在构造 `ResourceDescriptor` 前写入 `PermissionContext.metadata["principal_path"]`，随后摊平为 descriptor 属性交给 Authorizer；调用侧 metadata 不覆盖 space policy。

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
MemoryAPI.method(scope=target, security=RequestSecurityContext)
  → 构造 PermissionContext（write/recall/list 请求条件来自入参；list 实际 unit 与 get/update/delete 来自 Engine 真源元数据）
  → 摊平成 ResourceDescriptor（action + resource_type + scope + resource_id + attributes）
  → 由 security 派生 AuthorizationEnvironment.from_request(security, now=<服务端时钟>)
  → Authorizer.authorize(auth=security.auth, resource=..., environment=...)
    → allow → 委托 Engine/Governor/PolicyManager（仅传 scope，不传 security）
    → deny  → 抛 PermissionDeniedError，并落 deny audit（含 DenyReason code + rule）
  → 落审计事件（含 security.actor + action + target_id + 时间）
```

`security` 是本层**唯一**的安全输入，由调用方（surface 适配层或进程内受控入口）显式
传入——`Authorizer` 与本层都不读 ContextVar。ContextVar 里仍有一份 `AuthContext`，
但只供日志/trace 关联，缺失它不影响授权结论、存在它也不能替代 `security`
（F05 §RequestSecurityContext）。

`security.auth` 携带 `role` 这个 Scope 推不出来的东西（§3.1）。actor 由认证层产出，
业务 payload 不接受 `actor` / `role` / `acting_user`——`Authorizer` 决策第 2 步会校验
actor 一致性，空 `Scope()` 直接拒（它是「上下文不完整」的信号，不是任何一种权限）。
代操作不在请求里表达：委托关系来自服务端的 `DelegationStore`，由
`common.security.authorization` 的 Authorizer 按 `delegation_id` 复核。

管理面方法（`admin_*` / 全局 `audit`）除以根 scope 为 target 外，还须携带
`resource_type`（`admin` / `audit`），使「这是系统级操作」显式可读，而不是从
「target 恰好是空 scope」反推。ROOT 权限同样只由 `role` 表达。

### 没有 `security` 就进不了 API

`security: RequestSecurityContext` 是所有公开 verb 的**必填 keyword-only 参数**，没有
`auth=None` 分支、没有「空 `Scope()` 即 platform admin」的旁路——两者都在 PR2 删除。
非请求场景（`build_kernel` 直连、评测 harness、示例脚本、单测）与外部请求使用**同一
契约**，通过 `common.security.request_context` 的受控入口取得上下文：

- `new_request_context(auth, *, surface, peer, attributes)`——给已完成认证的 surface；
- `internal_context(authenticator)`——给进程内直连调用方，authenticator 必填，身份仍由
  它产出；调用方**不能自行声明身份**，也不存在无参领取 ROOT 的隐式默认。

构造规则收在这一处：`request_id` 由服务端生成、`started_at` 取服务端时钟、
`attributes` 只由系统组件写入（业务 payload 一律不得注入）、`surface` 无默认值必须由
适配层写入。HTTP / MCP / CLI 三个 surface 经
`bootstrap.core.auth_middleware.authenticated` 调它，各自传入自己的 `Surface`。新增
surface 必须沿用同一中间件——它同时承载凭据归一、限流、并发预算与入口审计，绕开它
等于把请求降级成无认证。

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

1. `security` 为必填 keyword-only 参数，类型是 `RequestSecurityContext`（不是 `Scope`）——与 target `scope` 类型不同，位置传反会在类型层暴露。
2. 所有数据面方法（write/batch_write/recall/list/get/update/delete/evolve）都需要鉴权，治理面（inspect/trace/audit）也需要鉴权。
3. 装配由 `assembly.build_kernel(config)` 完成，递归调用各 Producer.create_from(spec)。
4. 实现类（LocalMemoryAPI）不对外暴露，外部只依赖 `MemoryAPI` 抽象接口。
