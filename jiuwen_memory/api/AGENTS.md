# Agent Memory API（接口层）

**规约文档**：[S02-memory-api.md](../../docs/specs/S02-memory-api.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。
>
> **文档分工**：`docs/design/architecture.md` §6 = 已实现接口清单；本目录 = 已实现代码；S02 = 详细用法与方法总览（含尚未实现标注）；`docs/features/api/`（F01–F07）= 特性决策。尚未实现接口上库后须同步 S02 与 §6。

统一对外 Core API，所有接入形态（SDK/CLI/MCP/HTTP）最终映射到 `MemoryAPI`。本层是控制层的薄封装：做参数装配与鉴权，编排逻辑全部在 `jiuwen_memory/control`。

审计完整性接线保留返回快照判权、完整工具参数、授权插件能力和 Runtime 生命周期；
当前能力及部署边界见 [F04](../../docs/features/common/F04-security-interfaces-and-encryption.md)。

## 模块地图

| 文件 | 职责 |
|---|---|
| `__init__.py` | Access 唯一公共导入面：重导出 Core API 所需类型、异常、装配函数及日志脱敏辅助函数 |
| `memory_api.py` | MemoryAPI 抽象接口：统一语义定义（add/batch_add/check_write/submit_ingest/search/list/get/update/delete/evolve/admin/inspect/trace/audit/grant/revoke/space 管理） |
| `memory_api_impl/` | 具体实现目录 |
| `memory_api_impl/assembly.py` | 公开装配：assemble 返回 MemoryAPI；assemble_runtime 返回 api/authenticate/start/start_background/close 句柄；assemble_server_runtime 增量提供安全组合与共享 audit。私有 Kernel 持有原始端口。close 先停止准入，再等待 watcher/摄入/调度任务结束；wait=False 由单个收尾线程完成屏障与资源释放，wait=True 等待并报告异常；共享所有者统一关闭 shadow 和安全依赖 |
| `memory_api_impl/local_memory_api.py` | LocalMemoryAPI facade：构造、属性，公开方法由 mixin 提供 |
| `memory_api_impl/local_support.py` | 入口校验、过滤/谓词、空间投影等无状态辅助函数，以及私有可信预检目标值对象；不扩充公共接口 |
| `memory_api_impl/pep_ops.py` | PepOpsMixin：空间事实、`_authorize`、审计、`check_write` |
| `memory_api_impl/write_ops.py` | WriteOpsMixin：add/batch 与落点解析，鉴权后走 CommandService |
| `memory_api_impl/query_ops.py` | QueryOpsMixin：search/list/get/update/delete/evolve，鉴权后走 Query/Command；Schema 更新对 Command 返回的计划逐项鉴权后提交；evolve 的 dreaming 三态分发委托 `DreamingCoordinator` |
| `memory_api_impl/md_ops.py` | MdOpsMixin（F08）：`read_md_file` / `edit_md_file`，经 Engine 窄端口委托（非经 `_commands.write`/`_queries.get`）。鉴权对齐 `get`（READ 单段）/ `add`（WRITE 单段，edit 走继承式 SUPERSEDE 本质是写新记忆，D5）；project 解析在 Engine 层 `project_from_context`，本层不拆 Context。返回前剥瞬态键（coords 等是路径解析依据，不是条目属性）。`edit_md_file` 经 `_ensure_space_writable`（与 `add` 同，装配空间级判定时由 `ENTRY_RULES` 登记 `edit_md_file` 触发状态校验） |
| `memory_api_impl/dreaming.py` | DreamingCoordinator + DreamingDriverJob：dreaming 编排（PEP 边界）——leader-only 注册、`CANCEL_REQUESTED` 持久化幂等注销、generation 隔离旧代 Driver、生命周期操作串行与短状态锁分离、按 leader 任期缓存恢复结果、每 tick 持续授权（`created_by` 复验）、fan-out 逐桶裁决；Coordinator 只持鉴权、提交、调度、注册表、Engine 候选 scope 枚举窄端口，不反向持有完整 API；驱动 Job 每 tick 经 `commands.evolve` 提交一次性 EvolveJob |
| `memory_api_impl/admin_ops.py` | AdminOpsMixin：`submit_ingest`、任务、admin、治理、verify_audit、grant/revoke |
| `memory_api_impl/dreaming_security.py` | 私有注册身份快照编码；只消费受控上下文和服务端注册表，不接业务 payload、不保存原始凭据、不续期认证 |
| `memory_api_impl/space_ops.py` | SpaceOpsMixin：Space CRUD；`delete_space` 经 SpaceLifecycleService |
| `access_security.py` | Access 安全装配辅助：提供 dev Authenticator；私有 `_build_security_runtime` 从同一装配上下文选择、健康检查 Runtime，公开 mapping helper 复用它；不暴露 common 实现路径 |

## 行为铁律

- **静态加密装配边界**：构建组件前核查部署声明，ConfigSource 安装位置变更门禁后
  回填当前上下文；公共 manager 解析执行保护清单准入，替代插件不得绕过统一开关。
  包装接线遵循同一装配作用域和上游资源所有权/失败回滚生命周期。

- **关闭失败必须可观察**：Runtime 在变更状态前校验 Scheduler 关闭上下文，逐步隔离收尾错误。无法确认后台组件停止时不释放 leader 锁或存储；确认停止后，锁释放失败仍继续存储清理。等待关闭汇报单个异常或 ExceptionGroup，影子索引清理失败不得静默吞掉。

0. **写入 metadata 明确分区**
   add/batch/update 入口只接收 `system_metadata` 和 `user_metadata`。两者分别校验、
   分别合并并原样委托 Engine；用户过滤规范路径为 `user_metadata.<key>`。

1. **本层不做编排**  
   `MemoryAPI` 只做三件事：鉴权（PEP）、参数装配、委托 typed Control application ports。
   数据面经 `MemoryCommandService` / `MemoryQueryService`，治理经 `GovernanceService`，
   Space 删除事务经 `SpaceLifecycleService`。编排逻辑（write 路径、search/list 取数、
   evolve 调度、purge+delete）全部在 `control`，禁止在本层堆业务逻辑。

周期 HIERARCHY 的准入回调由 API 注入并绑定 home；每轮读取及每批写入前重新走 PEP、
刷新空间事实并检查可写状态，不沿用注册时许可。执行层只调用回调，不持有身份。

2. **调用方身份不下沉**
   身份取自 `security.auth.actor`；鉴权通过后只透传已鉴权的 target `scope`，`security` 及其中的 actor 不传入控制层/检索层/构建层/存储层。

3. **search 参数拆分在本层边界**
   `search(query, context, *, security, ...)` 中的 `context: Context` 在本层拆开：
   - `context.scope` 作独立轴穿透到 Engine
   - `context.extensions["max_tokens"]` 由 API 边界解析为 `RetrievalQuery.max_tokens`
   - 其余 `context.extensions` 写入 `RetrievalQuery.extensions`
   - `context.extensions["query_context"]` 校验为字符串序列、转为 tuple 后写入 `RetrievalQuery.query_context`；该 key 移除后不参与鉴权、硬过滤或插件透传

4. **admin_* 不经 Engine**  
   `admin_get/set/all` 直达 `PolicyManager`，不经过 `MemoryEngine`（Engine 中对应方法抛 NotImplementedError）。
   统一按根 Scope 的 ADMINISTER_SYSTEM 判权，需 ROOT，不是 MANAGE_POLICY。`audit` 的
   READ_AUDIT 也以根 Scope 判权，ADMIN 不足；`create_space` 仅本 org 可由 ADMIN 执行。
   空间成员/策略仍按既有空间治理轴和授予上界判断，不能用组织 ADMIN 取代空间事实。

5. **写入同步/异步桥接**
   `add` / `batch_add` 分别桥接对应协程入口；batch 在本层逐项归一化、鉴权、space 校验和审计后委托 Engine，默认按输入顺序返回 partial-success outcomes。

6. **space 必须在 API 边界执行策略校验**
   `scope.require_space=true` 时，具体 target scope 缺少 `space` 的数据面/治理面操作必须在 `LocalMemoryAPI._authorize` 拒绝并记录 deny audit；`Scope()` 根管理面与 org 级 `list_spaces/create_space` 鉴权目标不受此策略影响。

7. **space policy 在 API 边界注入可信资源事实**
   已创建 space 的 `principal_path` 由 `SpaceManager.get_policy` 提供，`LocalMemoryAPI` 在调用
   `Authorizer` 前把它写入服务端构造的授权资源描述；调用侧 metadata 不得覆盖 space policy。

8. **list 对实际返回资源逐条鉴权**
   请求级 `memory_types` 鉴权通过后，API 必须调用 Engine 的
   `list_with_permission_contexts` 一次取得当前分页及其真源权限上下文，再逐条 READ 鉴权；
   不得把未指定类型解释为可绕过类型路由，也不得用两次分页分别读取上下文和内容。
   API 在委托前复制 `extensions`、规范化 `filters`，并把权限路由值作为系统过滤条件
   与用户过滤做外层 AND；返回 `MemoryListResult` 的 count 为分页前匹配总数。
   `inspect` / `trace` 同样逐条 READ 判权（包含每个祖先），权限上下文必须由本次返回
   MemoryUnit 的同一快照构造，不能另读一次上下文后返回旧内容。
   `get` 保留请求 ID 的前置鉴权，并在引擎选定版本后对实际返回快照再次 READ 判权；
   `as_of` 选中另一版本或同 ID 内容变化时，不能沿用预读的作者、Scope 或类型路由。

9. **Space 删除覆盖全部子 Scope**
   `delete_space` 鉴权后调用 `SpaceLifecycleService`：先 `MemoryEngine.purge_space`
   清理同一 `org + space` 下所有 user/agent/session 子 Scope 的真源和索引，再委托
   `SpaceManager` 清理 messages 与管理元数据，并汇总 `deleted_counts`。本层只做
   membership 缓存失效与入口审计，不得内联 purge+delete。

## PEP 鉴权流程

```
MemoryAPI.method(scope=target, security=RequestSecurityContext)
  → identity = security.auth.actor（actor 只能来自这里，不来自业务 payload）
  → 校验上下文来源、完整性、时效与需在线复核的凭据（必须先于任何业务端口访问）
  → 构造 PermissionContext（add/search/list 请求条件来自入参；list 实际 unit 与 get/update/delete 来自 Engine 真源元数据）
  → Authorizer.authorize(auth=security.auth, resource=<服务端事实>, action=<对应动作>, env=...)
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

- dreaming 驱动提交派生任务后须立即调用 `link_child`，两步之间不得让出调度循环，以便执行前绑定父任务取消生命周期。注销先持久化 `CANCEL_REQUESTED`，Driver 在 tick 开头和提交前复核 `state + generation + job_id`；阻塞式 Scheduler 调用不得发生在协调器短状态锁内。
- dreaming 的 `created_by` 保留五维 actor，并由 API 写入私有 `_request_context_v1` 字符串快照。
  恢复只从服务端注册表读快照，保留原角色、到期时间、凭据复核能力及委托 ID；每 tick
  生成新的受控请求上下文，再走唯一 PEP。只有 actor 的旧记录跳过恢复，不能补猜身份。
1. `security` 为必填参数，类型 `common.security.types.RequestSecurityContext`；只能来自
   `auth_middleware.authenticated()` 或 `request_context.internal_context()`。不得从 payload
   或目标 Scope 自述身份（legacy 自述入口已删除，进程内调用方一律走
   `internal_context(authenticator)`——身份由认证器产出，`scope` 只作操作目标）。除
   `check_write(scope, security, *, ...)`
   为兼容旧第二位置参数外，其余公开方法均要求 keyword-only。
2. 授权面（`grant`/`revoke`）的公共类型是 `common.security.types.Grant`/`Action`；`control.types`
   只兼容再导出同一对象，不得定义第二套类型或结构转换。`grant_id` 由服务端生成，`revoke`
   按 ID 精确、幂等、单调撤销；管理入口与实际 Authorizer 判定必须使用同一具名 GrantStore，
   不得双写旧 `PermissionManager`。
3. 所有数据面方法（add/batch_add/search/list/get/update/delete/evolve）都需要鉴权，治理面（inspect/trace/audit）也需要鉴权。`LocalMemoryAPI._record_audit()` 先剥除 detail 中的 decision/request_id，再写入服务端真实判定与受控请求 ID；系统字段不占普通 detail 键数预算。
4. `verify_audit`（审计完整性验证，可选 provider 实现）是独立于 `audit` 的管理面入口：新入口按 `VERIFY_AUDIT` 对根 scope 判权；既有 `audit` 亦按目标动作 `READ_AUDIT` 鉴权（授权记录按 action 精确匹配，不与普通 READ 互认）。验证只收服务端参数（不接受调用方传入 digest/key/proof）；provider 与专用 `audit_verify_guard` 必须成对注入，全量验证占一个独立并发槽，耗尽抛 `RateLimitedError`。guard 耗尽发生在授权通过后，审计事件保持 `decision=allow`，并沿用 `workload_guard=exhausted` 表达容量准入失败，不得混入 `decision=deny` 的鉴权拒绝事件。guard 准入后先落验证尝试审计、再调用 provider，provider 抛完整性异常时仍须能追溯发起者与发生时间；成功或异常路径不重复写完成事件。`page_size` / `max_samples` 截到服务端 `globals.audit_verify_max_page_size` / `globals.audit_verify_max_samples` 装配出的可信 `AuditVerificationLimits`；装配边界只接受真正的整数（拒绝 `bool` 和字符串），并把非法类型或范围统一翻译成 `ValidationError`。provider 返回 samples 由 PEP 再截到有效上限。未装配 provider 时返回 `unsupported`，不降级成 clean。HTTP 在认证中间件产出可信上下文后通过同名 `/v1/verify_audit` 暴露原返回值，容量限流映射为 429；CLI 通过同名 `verify_audit` 命令和同一 JSON 契约暴露；MCP 经 `memory_verify_audit` 工具暴露同一契约（`after_sequence`/`page_size`/`max_samples`/`anchor_policy` 透传）；legacy handler 暂无一等入口。进程内调用必须显式传安全上下文。
5. 装配由 `assembly.assemble` / `assembly.assemble_runtime` / `assembly.assemble_server_runtime`
   完成，内部经 `_build_kernel`
   与各 Producer 的 `dep/build_named/build` 组装；Retriever 的 `domain_store()` 与内部
   `_Kernel.storage` 引用同一个全局 manager（`globals.store_manager` 指名，默认
   `store_manager.default`）。顺序铁律：
   ConfigSource → `store_manager.default`（composite 再 dep 各 Store，含 `kv_store.default`；
   若 default 为 encrypted target 则其 raw 指向具名实例）→ `_Kernel.kv = manager.kv(端口名)`。
   统一静态加密开关开启时，resolve 验证 manager 完整保护清单与 KV/FS 保护健康，
   替代 manager 缺少保护契约必须拒绝启动。`RoutingKVStore` 须作为 raw 落在加密层内；同实现换 Redis
   用 `kv_store.url` 晚绑定，不要为换 URL 预装多套 Routing 槽位（F01 §2.1.5 / S08）。
6. 实现类（LocalMemoryAPI）不对外暴露，外部只依赖 `MemoryAPI` 抽象接口。
7. `job_status` 统一查询 Scheduler 和长耗时 Ingest 任务；Ingest 任务必须显式传入
   target `scope`，由 API 对任务真实 Scope 执行 READ 鉴权并记录审计。
8. Access（`jiuwen_memory_entry/`、`jiuwen_memory_adapter/`）只 `import jiuwen_memory.api`，
   且不得 import `jiuwen_memory.api.memory_api_impl`。本包重导出协议转换所需的 DTO /
   枚举 / 异常 / `Credentials`，以及 Access 日志脱敏所需的
   `install_privacy_filter` / `metadata_for_log` / `redact_for_log` / `scope_for_log`。
   公开装配是
   `assemble` / `assemble_runtime` / `assemble_server_runtime`（接受 `dict | Config | None`），Access composition
   root 不 import `jiuwen_memory.config`。`Kernel` / `build_kernel` / `LocalMemoryAPI`
   不是公开包导出；`assemble_runtime` 不带 `kv` / `storage` / `space`。
9. `LocalMemoryAPI` 类体内禁止同名方法重复定义（后定义会静默覆盖先定义）。
   `add` / `add_async`（以及 `batch_add` / `batch_add_async`）是不同方法名的同步/异步入口，
   不属重复。静态检查见 `tests/unit/api/test_local_memory_api_methods.py`。
10. 数据面写经 `MemoryCommandService`，查询经 `MemoryQueryService`，治理经 `GovernanceService`，
    Space 删除事务经 `SpaceLifecycleService`。PEP、路由谓词回注、逐条鉴权仍在本层。
    不得把 `_purge_space_memories` 或内联 purge+delete 收回本类。
11. `build_dev_authenticator(identities=...)` 构造固定或预设映射开发身份，不是生产 runtime。HTTP / CLI / MCP
    的显式 DEV 入口通过 `with_local_dev_security()` 补齐完整配置；
    固定身份默认 USER，ROOT 必须在 dev 认证器 params.role 显式配置；适配器不注入
    `allow_all` PermissionManager。固定角色与 identities 映射互斥，公开 helper 签名不变。
    `build_configured_security_runtime()` 从普通 mapping 装配并健康检查
    Runtime，每次调用建立独立上下文；SDK 直接使用 Runtime.authenticate。缺失 `security`
    返回 `None`、多实例无 `default` 时拒绝歧义选择。Access 不得
    直接 import `common.security.*_impl` 或 Factory。DEV 身份经真实 Authorizer 放行；显式
    API Key / Trusted 部署仍按其 AuthContext 和授权事实判定。
12. 配置了 `security` 的 SDK 与 Server 装配均在 `_build_kernel` 内绑定实际认证器的
    issuer/KeyStore，并校验 Runtime 与 PEP 的 PDP 对象一致。Server AUTO 复用内核已建
    Runtime，其 close 由 MemoryRuntime 管理；显式注入另一个 Runtime 时回绑 PDP、替换
    Registry，禁止残留旧 issuer 或关闭已有 router 的空间治理。没有 security 配置时不猜测
    认证器或 KeyStore，未知 issuer 仍拒绝。MemoryRuntime 提供
    api/authenticate/start/start_background/start_background_jobs/close；authenticate 返回
    SDK 上下文并使用同次装配的认证器与预算，不要求再次构建配置。
    start_background_jobs 为固定树驻留范围（tree_home_scope，简称 home）注册后台建树。
    `assemble_server_runtime` 返回增量 ServerRuntime，另公开 security_runtime/audit；
    同源校验、回绑及共享关闭根由本包装配器完成，Server 不访问内核私有成员。旧手工构造
    MemoryRuntime-only 的 Server 不探测私有 audit，额外安全组合按公开 close 能力清理。
13. 代理代写不得改写 `AuthContext.actor` 或从目标 Scope 推导用户。PEP 通过私有
    `_resource_principal` 回委托真源推导作者与资源坐标，真实 actor 用于审计；
    `_delegator.` 前缀属性仅允许 PEP 从空间事实投影，调用方同名 metadata 必须剥离。
    委托人的当前内容权由唯一 PDP 复核，治理轴不借用委托，委托不自动创建 fallback 空间。
14. `grant/revoke` 通过公开 `GrantManagementCapability` 的 `create_grant/get_grant/
    revoke_if_grantor` 创建或原子绑定真实目标撤销；管理域只有一个 GrantStore，不扇出写入。
    旧只读插件未声明管理能力时返回 UnsupportedCapabilityError，显式完整管理部署在启动期
    拒绝缺能力。未知 ID 幂等，ID 不是授权凭据；已有 MemoryAPI 签名不变，
    Grant.for_revocation 可构造 ID 引用，其余请求字段不参与定位或归属鉴权。
    空间/入口路由/委托真源通过公开增量能力传递，不探测插件私有状态。
    无 SecurityRuntime 的 SDK 也由 MemoryRuntime 持有安全资源所有者；停止异常不跳过其余
    安全停止动作，未确认停止时保留依赖。与外层同一 SecurityRuntime 不重复关闭资源。assemble 返回的
    API 通过独立关闭能力委托其 Runtime；显式 Runtime 替换在装配期共享安全生命周期根，
    已关闭 Runtime 不再用于健康启动，配置辅助的新装配不得复用关闭句柄。
    PEP 保存本部署完整管理能力要求，显式替换 PDP 也复核；_build_kernel 在解析依赖前
    建立创建回滚作用域。失败清理不关闭借用实例；后台停止与共享关闭保持幂等和先后顺序。
    close(wait=False) 封闭准入并启动单个收尾线程，先请求 Scheduler 与摄入停机，再等待
    在飞任务结束；`_background_drained` 只在全部使用者及自有 loop 确认停止后置位。
    close(wait=True) 等待同一结果并报告异常；共享关闭通过独立屏障等待使用者，不 join
    可能正在等待资源根锁的收尾线程。额外注入的仅 close 组合也纳入同一资源根。
    文档模式同样先停止 watcher 准入与数据面工作；所有具名 shadow 端口列入共同所有者，
    后台线程不再访问连接后才释放 shadow。同步启动将 Observer 故障传回调用方，关闭后拒绝重启。
15. 成员/Grant 授予上界接收完整可信上下文，只有 ROOT 免除普通成员上界；不得从 actor
    的 Scope 形状推导特权。路由写入和跨空间查询必须区分后端故障与正常权限拒绝。


### 显式建树边界

原 `evolve` 追加 hierarchy_options/reuse_existing，普通模式保留原 candidate/dreaming/
interval 与授权链。HIERARCHY 保留 WRITE+UPDATE 可信授权及空间可写检查；开启时
校验树参数、权限路由限制与父 metadata，关闭时忽略树选项并提交成功的跳过任务。
提交前执行与普通演进相同的审计完整性健康预检，故障不得先产生任务或建树副作用。
基础校验与树语义校验分离，缺少 hierarchy_options 不阻止关闭状态的成功跳过。
HTTP/CLI 从 MemoryAPI 反射原方法，MCP 只提供原 memory_evolve。

### 查询参数与树总开关

- 原 search 末尾追加树参数，签名直接声明 TIME/time_span/expand_depth=1/rollup=True。
  `_search` 使用内部 SearchOptions 参数载体，不再补默认值；显式 0/False 原样生效。
- 在可信来源校验后读取 hierarchy.enabled；关闭或缺失时先清除六个树参数再验证树语义，
  普通 filters/as_of/top_k/Scope/权限保持不变，普通召回仍排除生成的 TIME 父。
- Context 与参数快照不污染调用者；extensions 标准容器复制，不透明插件保留身份。
- 单/跨空间查询共用已归一化参数，不在后续分支重复读取树开关。
- 不再提供公开 search_v2/evolve_v2；HTTP/CLI/MCP 使用原接口和新增可选参数。
- 无有效 TimeSpan 时严格返回空结果，不回退到未挂树叶；搜索不触发建树。


### Runtime 周期注册

assembly 的 MemoryRuntime / ServerRuntime 提供异步 start_background_jobs，关闭屏障前后
检查注册状态，不在持锁时 await 调度器。装配期绑定 QueryOps._start_background_jobs
私有回调，Runtime 才公开同名注册能力；具体 API 不保留公开注册方法。内部入口只做固定 home 的
可信来源、WRITE/UPDATE、空间可写、审计完整性与权限路由校验，随后委托 Command；不增加 MemoryAPI
抽象方法或 Access 路由。周期由现有 Scheduler 自有循环承担，不改 watchdog 宿主循环。
注册完成时若已关闭，逐个尝试取消返回的任务；取消异常只记录日志，仍报告注册期间关闭，
资源释放继续受既有后台停止屏障保护。

Markdown 编辑同样在委托 Command 前执行审计完整性健康预检；健康故障或已锁存的
degraded 不得产生文件编辑或版本写入副作用。读取仍沿用既有 READ 判权与审计路径。
