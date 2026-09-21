# S10 — 安全横切契约（Security）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `jiuwen_memory/common/security/`、`jiuwen_memory_entry/`、`jiuwen_memory/api/`、`jiuwen_memory/storage/` |
| 最近一次修订日期 | 2026-10-14 |
| 关联特性文档 | [F04 安全能力与设计决策](../features/common/F04-security-interfaces-and-encryption.md)，[F04-1 冻结接口备忘](../features/common/F04-1-security-api-contracts.md) |

## 范围 / 边界

本规约定义请求认证、主体凭据存储、资源保护（限流 / 并发预算 / 绑定策略）、静态加密配置、
授权判定与安全运行期装配不变量。PR1 已交付认证和加密；当前能力与设计边界统一见
[F04](../features/common/F04-security-interfaces-and-encryption.md)。后续安全能力更新
只维护 F04，不再新增安全 feature；逐次执行与验收记录不进入正式能力总纲。
既有接口的签名、值对象及安全语义保持固定；安全插件可按 S09 增量声明独立能力，
不得通过实现私有状态替代公开契约，也不得破坏旧接口与默认配置兼容。
审计完整性实现仍归 PR3。

安全能力统一归属 `jiuwen_memory/common/security/`，按能力域分子包：

| 子包 | 承载 |
|---|---|
| `authentication/` | `Authenticator`、`PrincipalKeyStore` 与内置实现（api_key / dev / trusted 三种模式）。进程内调用方经 `request_context.internal_context(authenticator)` 显式穿过认证边界——身份由传入的认证器产出，调用方不能自述身份 |
| `authorization/` | `Authorizer`、`GrantStore`、`DelegationStore`、Scope 覆盖规则与 PR2 实现 |
| `cryptography/` | `CryptographyProvider`、`KeyProvider`、ENC1 本地信封实现 |
| `protection/` | `RateLimiter`、`WorkloadGuard`、`BindingPolicy` |
| `types.py` | `AuthContext`、PR2 预置的 `RequestSecurityContext`、`CryptoContext`、`Role`、`Surface`、`Credentials` |
| `runtime.py` | `SecurityRuntime`：持有能力引用、启动期健康检查、统一生命周期 |
| `capabilities.py` | 兼容增量能力协议、装配声明校验与安全资源所有者；不改变旧 ABC 抽象方法集合 |

`authorization/` 的契约随接口分支固定，PR2 提供 `StandardAuthorizer`、
`RoutingAuthorizer`、`SpaceAwareAuthorizer`、memory / SQLite Store 与 PEP 接线，当前实现已
完成核心接线；SDK 凭据源与受控代理代写的设计决策见 F04。
`audit_integrity/`（PR3）本期仍只有固定契约，不实装或激活其 `*_impl`。

> 历史状态：这些能力此前平铺在 `common/authentication/`、`credential_store/`、
> `admission/`、`encryption/` 与 `type_def/auth.py`。那是迁移前的目录形态，不再作为
> 新代码的约束——新增安全能力一律落 `jiuwen_memory/common/security/<能力域>/`。

## 不变量

### 身份与认证

1. 请求身份只由认证中间件产生，客户端 payload 中的身份字段不是可信身份来源。
2. `Authenticator.authenticate` 成功返回 `AuthContext`，失败抛 `AuthenticationError`；不得返回默认身份。
3. `AuthContext` 是 frozen value object；其 `actor` 使用全局固定的可变 `Scope` 类型，内置
   Authenticator 必须为每次认证返回独立的 Scope 快照，不得复用模块级或 Store 内对象。
   `RequestSecurityContext` 的来源证明绑定 actor 全部维度；构造后原地改写 actor 必须导致
   PEP 校验失败。
4. `Authenticator.mode()` 返回**开放字符串**而非封闭枚举。核心不得按该值分支——需要
   分支的行为差异必须由 capability 方法显式声明，第三方实现无需改核心即可接入。
5. **actor 全局形态不变量（IMPL-01 §1.1）**：`AuthContext.actor` 恒为单主体--`org`
   必须非空（部署级凭据用 `org="system"`）；`user` 与 `agent` 必须且只能有一个非空；
   `session` 若非空必须挂在已确定的主体下。规则对**所有**认证模式统一生效（内置
   API Key / Trusted / DEV / Root Key 与第三方 `Authenticator`），由
   `security.types.validate_actor_form` 在 `authenticated()` 和受控上下文构造边界
   `new_request_context`（含 `internal_context`）统一执行，非法形态不得签发来源证明，
   surface 认证拒绝按入口策略留审计。`Scope(user=..., agent=...)` 同时非空仍是
   资源层 `principal_path` 的合法层级表达，只是不作为认证 actor。
6. **具名系统主体**：为兼容上游既有开发环境及本地数据，固定 DEV 产出
   `Scope(org="local", user="developer")`；Root API Key 产出
   `Scope(org="system", user="root")`。ROOT 权限只由 `role=Role.ROOT` 表达，不来自
   actor 形状。授权时只能由 `Authorizer` 根据显式 `AuthContext.role` 执行 ROOT 闸门；空
   actor、目标 Scope 形状或 ContextVar 均不得推导出特权。

- **凭据在线复核接缝**：`PrincipalKeyStore.is_revoked` 与 `CredentialStatusRegistry` 已在 PR1
  实现并有镜像单测；`ApiKeyAuthenticator` 在认证期校验 Store 覆盖了撤销查询。
  `AuthContext.credential_status_required` 显式声明是否需要在线复核，并通过
  `(credential_type, credential_issuer)` 路由到平行 Authenticator 各自的真源。ROOT key、
  trusted gateway 等身份可携带非空 `credential_id` 做审计，但 capability 为 false，不能由
  id 形状猜测撤销语义。声明需要复核却缺少 id 或注册 issuer 时 fail-closed。
  `AuthContext` 仍是纯数据值对象，不携带 Callable 或 Store 引用。唯一 PEP 必须通过与
  Authenticator 共用真源的 Registry 逐请求复核，使撤销前缓存的上下文立即失效。

- **PR2 显式上下文**：`MemoryAPI`、dispatch、HTTP、MCP、CLI、插件和进程内调用都必须显式
  传递受控构造的 `RequestSecurityContext`。Handler 不得保留 `identity` / `acting_user` 或
  payload actor 兼容旁路；ContextVar 只用于日志与 trace。

### 授权与资源隔离

23. `MemoryAPI` 是唯一业务 PEP，`Authorizer` 是唯一 PDP。旧 `PermissionManager` 可以为尚未
    迁移的历史代码保留，但不得处于生产请求判定路径，也不得成为第二套授权真源。
24. PEP 必须先验证上下文来源、actor 完整性、时效和需在线复核的凭据，再读取空间事实、访问
    业务 Store、创建 fallback space 或产生任何其他业务副作用。
25. `Authorizer` 只根据显式的 `AuthContext`、服务端构造的 `ResourceDescriptor` 与 `Environment`
    判定；不得解析传输协议、读取 ContextVar、读取业务 payload 或自行加载 MemoryUnit。
26. 判定顺序固定为管理面角色闸门、ROOT、org 硬边界、owner-cover、Delegation、Grant、默认拒绝；
    非 ROOT 不得跨 org，普通 Grant / Delegation 不得越过管理面角色闸门。
27. `GrantStore` 与 `DelegationStore` 是授权状态真源。`grant_id` 由服务端生成；撤销按 ID 幂等、
    单调且同 ID 重放不得复活。公共 grant/revoke 与实际判定必须访问同一具名 Store 实例。
    撤权须对 ID 对应的真实 grantor 判权，并在执行时原子绑定该目标；不得信任请求 grantor。
    同 ID 的活动记录更新全部值对象字段；已撤销记录保持原状，不接受重放复活。
    空间限制必须无损存取；旧格式有歧义或编码损坏时拒绝，不能解释为不限制空间。
    一个管理域仅允许一个共享 GrantStore；singular/plural 是同一真源的对象/列表视图，
    不代表扇出提交协议。多个不同 Store 在装配期拒绝，PEP 在管理写入前再次检查。
28. 允许结果必须给出 rule，拒绝结果必须给出稳定 `DenyReason`；授权依赖故障必须与正常 403
    拒绝分开映射，不得吞错并降格为 deny。
29. RoutingAuthorizer 的路由字段只能来自服务端可信资源属性；未命中必须进入明确的安全
    fallback；任一可达的 test-only delegate 都使组合 Authorizer 为 test-only，生产装配须拒绝。
30. `SecurityRuntime.authorizer` 与 PEP 持有的 Authorizer 必须是同一实例；认证、凭据复核、授权
    判定和授权管理使用的具名能力也必须共享对应真源。
    此要求同时适用于独立 SDK 和 Server 装配。Server 的绑定和一致性校验由 API 包
    公开 assemble_server_runtime 完成，返回 S02 定义的 ServerRuntime；Access 不得
    依赖 MemoryRuntime/API 实现的私有字段或绑定方法。显式替换认证器时 Registry 只保留新认证器
    的真源，不得把旧 issuer 的凭据继续当作当前部署有效身份；绑定过程先健康检查再替换。
31. `AllowAllAuthorizer` 只允许显式测试装配。DEV 业务连续性由受控 `Role.ROOT` 通过真实
    Authorizer 实现，不得靠旧 permission fallback、空 Scope 或缺省放行。
32. 受控代理代写保持认证 actor 为真实单主体 agent。被代理用户只能从服务端绑定的
    Delegation 真源取得，不能从目标 Scope、请求 metadata 或 header 自述取得。
    凭据绑定与 PDP 共用同一 Store；认证后缓存的上下文仍须逐次复核委托有效期、撤销、
    动作、凭据/会话和空间限制。代理权限不得超过委托人当前内容权限，治理权不随委托转移。
    作者标记和检索坐标可以由有效委托派生，审计 actor 不得替换为委托人。
33. 治理读取 `inspect` / `trace` 的每个返回条目（含祖先）必须按其同一真源快照的
    Scope、作者和类型路由逐项 READ 判权；请求 Scope 的授权不替代条目级授权。
    `get` 同样必须检查最终返回快照；请求 ID 的权限检查不替代 `as_of` 所选版本的
    READ 判权，即使最终 ID 未变化，也不得沿用第二次读取前的权限快照。
34. 空间成员及授权的授予上界必须消费可信角色。ROOT 不受普通成员上界限制；ADMIN
    不因此自动获得内容权，普通成员的自提禁止和授予上界仍须执行。
35. 定时演进的注册、每 tick 和恢复均由同一 PEP/PDP 判权。注册只持久化已认证的
    身份与环境事实，不保存原始凭据或进程内来源证明；后台受控上下文保留原认证到期
    时间，不续期，继续在线复核凭据和委托。恢复只能读取服务端私有注册表，不能从
    业务目标、candidate 或 payload 推导身份、角色及凭据复核能力。旧记录缺少完整
    安全快照时跳过恢复；损坏快照隔离，不得降为不复核的上下文。

### 依据 capability 做安全决策

7. `Authenticator.requires_loopback_binding()` 默认返回 `True`。只有实现显式声明可远程
   暴露，surface 才能绑定非 loopback 地址。
8. `requires_concurrency_guard()` 默认返回 `True`；轻量实现必须显式返回 `False` 才能跳过
   并发预算。
9. 持久化、原子写、密钥轮换、分布式限流等能力必须由类型或 capability 显式声明。禁止通过
   `target == "sqlite"`、类名后缀或配置路径推测安全保证。`WorkloadGuard.supports_distributed_budget()`
   是这条的一个实例：进程内实现返回 `False`，多副本部署据此判断实际并发是 N 倍。

### 资源保护

10. 绑定约束由 `BindingPolicy.check(hosts, *, requires_loopback)` 在实际 socket 绑定前执行，
   不能只存在于某个 CLI `main()`。`requires_loopback` 是 keyword-only，位置传参会让放宽
   在调用点看不出来。
11. 限流在 `authenticate` **之前**执行：认证本身就是要保护的资源。
12. 密码哈希、密钥派生与全量完整性验证等昂贵操作使用独立的全局并发预算。预算耗尽时快速
    拒绝（429），不得无界排队——排队只是把资源耗尽从 CPU/内存转移到线程和请求队列。

### MCP 凭据载体

- stdio 从进程环境变量 `AGENT_MEMORY_API_KEY` 读取 API Key；空值仍交给 Authenticator
  决定，DEV 可用，API_KEY 模式拒绝。
- Streamable HTTP 逐请求读取 `Authorization: Bearer <key>`（兼容 `X-API-Key`）及 trusted
  gateway headers，并携带 socket peer；不得回退读取进程级 API Key。
- Streamable HTTP 的 peer 必须进入认证前 `RateLimiter` 与 `WorkloadGuard`；stdio 没有网络
  对端，不做地址限流。
- MCP surface 只构造 `Credentials`，不得直接构造 `AuthContext`；认证中间件把认证结果封装为
  `RequestSecurityContext` 并显式交给 PEP。

### 密码学

13. 密码学能力只能通过 `KeyProvider` 获取密钥，不能直接读取环境变量或配置文件中的根密钥。
14. 信封至少包含 magic、格式版本、algorithm id、**key id 与 key epoch**、nonce、ciphertext 与
    authentication tag。AAD 必须绑定规范化 Scope、存储用途、对象标识和格式版本。
15. 不提供隐式明文回退：要求加密的数据不是合法信封时拒绝读取；解密失败不得返回原始 bytes；
    是否允许未加密存储由上层显式选择不同的存储适配器表达，同一个加密适配器内部不存在
    `allow_plaintext` 降级开关。

### 装配

16. YAML 只选择已注册 target 并传递 params，不接受 Python import path 或任意类加载。
17. 任一安全顶层段存在多个具名实例时必须定义 `default`，否则拒绝启动。
18. 实现只依赖能力接口，不 import 其他实现目录；注册在装配前统一完成。
19. `SecurityRuntime` 只持有能力引用、执行启动期健康检查并暴露统一生命周期，不实现认证、
    授权或密码学算法。能力不健康必须在启动期拒绝，不能等第一个请求打进来才在 500 里暴露。
20. 运行期共享状态（密码学 provider、密钥 provider、并发预算、限流桶）通过**具名实例**
    显式共享，不靠模块级单例。同一具名密码学实例若同时被加密存储与
    `SecurityRuntime` 引用，二者必须得到同一个对象。
21. `SecurityRuntime` 不为后续 PR 预留恒为 `None` 的**必填**能力占位字段——那会诱导消费方写
    `if runtime.authorizer:` 的 fail-open 分支。F04-1 明确定义为可选装配位的字段
    （`cryptography_provider` / `audit_integrity_provider`）不在此列：消费方本就必须判空，
    未装配即该能力未启用。
22. 健康检查不泄露 key、token 或主体存在性。

## 增量公开能力与兼容

安全插件通过独立、可运行时查询的公开协议声明能力，不给既有 ABC 增加必填抽象方法。
能力在实例生命周期内稳定；使用者只依赖协议，不识别内置实现类或探测私有钩子。

| 能力 | 方法 | 契约 |
|---|---|---|
| GrantManagementCapability | create_grant / get_grant / revoke_if_grantor | 原子创建、真实记录读取及原子绑定撤权 |
| DelegationSourcesCapability | delegation_stores | 实际委托真源的稳定 tuple；组合策略去重并完整透传 |
| CredentialSourcesCapability | credential_sources | 稳定 tuple，元素为 credential_type、issuer、PrincipalKeyStore；同一 issuer 不得绑定不同 Store |
| SpaceFactsCapability | requires_space_facts | 稳定 bool；声明需要时 PEP 从真源读取空间事实 |
| EntryRoutingCapability | routing_fields_for_entry | 特定入口实际使用的稳定路由字段 tuple；外层路由不能因委托策略需要空间事实而被关闭 |
| SecurityDependenciesCapability | security_dependencies | 由本装配根拥有的依赖 tuple；借用资源不列入，禁止循环拓扑 |
| CloseableSecurityCapability | close | 幂等关闭自身资源，不级联关闭已交给装配根的共享依赖 |
| SecurityLifecycleCapability | security_resource_owner | 暴露生命周期根供装配阶段显式共享；不将嵌套生命周期根作为普通依赖级联关闭 |

### 原子授权管理

- create_grant 接收带服务端 ID 的 Grant；相同 ID/同记录重投幂等，不覆盖另一个记录，
  不复活已撤销 ID。抛异常必须保证此次创建未留下有效权限；不具备该保证的后端不得
  声明管理能力，需先提供事务或明确的恢复机制。不得用 best-effort 补偿撤销冒充原子提交。
  事务清理包括取消/中断等非正常退出；回滚后保留原异常语义，不将取消转换为成功。
- get_grant 返回含过期/撤销状态的独立快照，未知 ID 返回 None；PEP 先对真实 grantor
  执行 REVOKE_SHARE 判权，再由 revoke_if_grantor 原子比较并软撤销。目标变化报
  BackendError 要求重新读取/鉴权重试，未知或重复撤销幂等，不刷新撤销时间。
- 原 GrantStore.add 的全字段更新与撤销单调语义保留；管理创建不用 add/upsert 覆盖
  既有授权。Store 级幂等以 grant_id 为键，不等于新的网络 grant 请求自动去重。
- 未声明增量能力的旧 GrantStore 仍可服务只读 PDP；调用管理能力统一抛
  UnsupportedCapabilityError，不返回假成功。globals.require_grant_management 是
  服务端布尔约束，默认 false；显式 true 的部署必须在装配期验证完整管理能力。
  非 bool、矛盾的真源声明和多 Store 管理拓扑拒绝启动。
  部署要求必须保存到 PEP 装配边界，并在显式 Runtime/PDP 替换时复核最终生效对象，
  不能只校验被替换的默认实例。

### 生命周期所有权

装配根按公开依赖声明创建固定资源清单，不依赖解释器退出。同一图中共享对象按 identity
去重关闭；使用者先停，依赖后释放。SecurityRuntime 与其外层 MemoryRuntime 不重复拥有
同一授权图；未装配认证 Runtime 的 SDK 仍须拥有并关闭授权依赖。外部借用资源由其原
所有者关闭，不进入借用者的清单；多个 surface 共享同一 Runtime 而不是各建关闭根。
停止后台任务的异常不得跳过安全清理。关闭故障须可观测，继续清理其他资源，失败项
保留重试机会；重复 close 不再次释放已成功关闭的对象。
停止接收任务与任务全部结束必须分开记录；MemoryRuntime/ServerRuntime 的
`close(wait=False)` 不承诺整体立即返回。首次摄入停机请求可不等待，但统一关闭根
在释放依赖前仍以 `wait=True` 等待全部已提交摄入任务及其状态持久化完成，随后停止
Scheduler、释放 dreaming 锁，再关闭安全资源。文档后台使用者也遵守相同顺序：
停止文件事件准入并等待已进入数据面的同步工作，shadow 连接与安全依赖按对象身份
去重拥有；同步关闭不能依赖调用方 loop 恢复协程后才能完成等待。重复或共享关闭
不得跳过此等待。
显式注入或替换 Runtime 时，通过 share_security_lifecycle 在装配阶段合并资源所有者，
不是按包装对象数量重复拥有底层 Store。根开始关闭后禁止再挂接；不同独立生命周期
若共享依赖，也须显式共享所有者或声明借用，不能依靠全局隐式缓存去重。
assemble 返回的 API 通过独立 CloseableSecurityCapability 委托关闭其拥有的
MemoryRuntime；不向冻结的 MemoryAPI ABC 增加抽象方法。已关闭的 SecurityRuntime
health 必须拒绝使用，新的配置辅助装配不复用缓存中的关闭句柄及其依赖。
装配根在解析依赖前观察本次新建对象，启动失败（含取消）先停止本次后台使用者，
再清理新建资源及其缓存，不关闭缓存命中的既有借用对象。插件自己的构造尚未成功
返回时，内部未发布资源仍由插件负责局部清理。
按 S07 显式绑定同装配纪元的生命周期根，关闭任一同纪元视图前先停止其共享后台
使用者；只失效该纪元缓存，旧根关闭不能清空较新纪元。健康故障直接报告，不盲目
重建或切换后端。关闭通知与资源清理遇取消/中断时仍处理其余清单，再传播原中断；
失败项保留重试机会。

## 注册与配置

每个能力目录的顶层 `.py` 定义抽象接口和 Producer，`*_impl/` 中的实现通过
`@Producer.register("target")` 注册，`common.bootstrap.register_plugins()` 在配置解析前统一触发。

实现模块必须在配置解析前由 `common.bootstrap.register_plugins()` 或应用自己的注册入口
import，注册装饰器才会生效。当前核心不自动发现任意外部 Python 包；外部插件应由宿主应用
在 `Server.build` / `assemble` / `assemble_runtime` / `assemble_server_runtime` 前显式加载。

顶层段名：`security`、`authenticator`、`key_store`、`authorizer`、`grant_store`、
`delegation_store`、`rate_limiter`、`workload_guard`、`binding_policy`、`cryptography`、
`key_provider`。

```yaml
security:
  default:
    target: standard
    params:
      authenticator: default          # 必填，无默认实现
      authorizer: default              # 必填，与 MemoryAPI PEP 使用同一具名实例
      rate_limiter: default
      workload_guard: shared_budget   # 具名引用 = 跨 surface 共享同一份预算
      binding_policy: loopback        # 省略时按 target 名取默认实现
      cryptography: default           # 可选；不配则 SecurityRuntime 不持有密码学能力
authenticator:
  default:
    target: api_key
    params:
      key_store: default
      root_api_key: ${ROOT_API_KEY}
key_store:
  default:
    target: memory
authorizer:
  default:
    target: standard
    params:
      grant_store: default
      delegation_store: default
grant_store:
  default:
    target: sqlite
delegation_store:
  default:
    target: sqlite
rate_limiter:
  default:
    target: token_bucket
    params:
      capacity: 30
      refill_per_sec: 10
workload_guard:
  shared_budget:
    target: semaphore
    params:
      max_concurrent: 4
cryptography:
  default:
    target: local
    params:
      key_provider: default
key_provider:
  default:
    target: local
    params:
      key_env: AGENT_MEMORY_ENCRYPTION_ROOT_KEY
      key_file: ~/.agent-memory/security/master.key
      key_epoch: 1
```

`security.params.authenticator` 无默认：给认证一个默认会让「忘了配认证」静默变成某种可用
配置。其余能力的默认取保守侧，且默认值本身由 capability 决定而非 target 名——认证声明
`requires_loopback_binding()` 时限流默认 `unlimited`（无远端攻击面），否则默认 `token_bucket`。

未配置 `security` 段时，通用 `Server.build()` 与 HTTP/CLI 的 `required` 模式都不隐式创建
认证器：Runtime 为 `None`，业务请求 fail-closed。只有 composition root **显式选择**本地
DEV 时，才用配置适配器补齐完整 SecurityRuntime；MCP（含 stdio）同样默认 required，
须显式 JIUWEN_MEMORY_MCP_AUTH_MODE=dev 才启用该适配器。未配置认证时均失闭。

### DEV 业务连续性

公共 Core 装配入口是 `api.assemble()` / `api.assemble_runtime()`；Server 使用
`api.assemble_server_runtime()` 获取已调和认证、授权、审计和生命周期的公开句柄。
MemoryRuntime 的上游文档生命周期与 ServerRuntime 增量契约见 S02；`build_kernel` 不再公开。
默认授权经 StandardAuthorizer 及其具名 Grant/Delegation 真源，不调用旧 PermissionManager。
未配置认证时不得从资源 Scope 推导身份，也不得因 DEV 配置缺失而全放行。

HTTP/CLI/MCP 的显式 DEV 入口调用 `with_local_dev_security()`，仅在用户没有
声明 `security` 时向配置副本补齐完整 DEV Runtime；不修改原始 `config.settings`，也不注入
`permission.default=allow_all`。PR2 的 `Authorizer` 已接管 `role=ROOT`，DEV 跨组织 add/get
由真实 PDP 放行；API Key / Trusted 部署仍按 AuthContext 和授权事实判定。DEV 的
`BindingPolicy` 继续限制实际监听地址为 loopback。

### 兼容周期

- 上一版公开导入 `SecurityProvider` / `SecurityProducer` / `SecurityContext` / `KeySource`
  保留一个发布周期；前三者适配到 `Cryptography*` / `CryptoContext`，`KeySource` 仅保留旧
  抽象导入，不进入新装配链。
- 上一版 `security.<name>.target=local` 与加密 KV 的 `params.security` 在配置解析期自动迁移为
  `cryptography` / `key_provider` 与 `params.cryptography`，并发出 `DeprecationWarning`；同段
  已有的 `SecurityRuntime` 实例会原样保留，无法无损迁移的参数一律拒绝。
- 旧 `allow_plaintext` 不迁移为可用开关；加密存储仍严格 fail-closed。

## 当前扩展边界

- `Authenticator`、`PrincipalKeyStore`、`Authorizer`、`GrantStore`、`DelegationStore`、
  `RateLimiter`、`WorkloadGuard`、`BindingPolicy`、`CryptographyProvider`、`KeyProvider` 均可
  通过 Producer 注册扩展。
- `KeyProvider` 是独立 Producer：换 KMS / Vault 不必改加密实现。
- Server 按 capability 决策绑定和并发保护，不按封闭枚举分支。
- 认证根装配消费一个最终实例；需要多认证串联时，应注册组合 target，由该 target 通过
  `Producer.dep()` 引用多个具名实例，而不是让 YAML 隐式并行执行。
- `EncryptedKVStore` 只负责 KV 存储边界接线，密码学实现归
  `common/security/cryptography`。FS 加密装饰器已从 PR1 拆出，待资产链路与 API 契约明确后
  单独合入。
- 内置 `LocalKeyProvider` 支持多代轮换：`rotate()` 生成新随机根密钥并推进 epoch，旧 epoch
  根密钥保留在进程内字典供 `unwrap` 解开历史信封（写出一律 v2 信封，v1 只读兼容）。新根
  密钥**不持久化**--进程重启回到配置声明的初始密钥，轮换后写入的信封在重启后不可读；
  需要跨重启保留轮换状态应换 KMS/Vault，由其管理历史 epoch 验证材料。

### 上游 HTTP 多身份兼容与 PR2 迁移

HTTP 显式 DEV 可从 http.dev_identities 读取服务端身份映射，或由已配置的安全 Runtime
提供身份；配置中的 ADMIN 必须具名（如 org=local,user=ops）。缺失/未知 selector 返回 401。
映射模式不自动注入 allow_all。当前空间、成员、读写隔离均经 MemoryAPI/Authorizer；
具名 ADMIN/ROOT 的组织权限与 agent 代表 user 的受控委托使用同一链路，内容轴、治理轴、
作者标记和路由/检索过滤共同接受验证。旧 PermissionManager 仅为历史兼容类，不参与生产判定。
PR1 阶段曾暂用旧权限链，那是迁移历史而非当前要求；不得恢复双 PDP。
