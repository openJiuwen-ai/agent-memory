# S09 — 安全横切契约（Security）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `src/common/security/`、`bootstrap/`、`src/api/`、`src/storage/` |
| 最近一次修订日期 | 2026-08-07 |
| 关联特性文档 | `docs/features/common/F04-security-interfaces-and-encryption.md`，`docs/features/common/F07-authentication-kernel.md`，`docs/features/common/F08-authorization-context.md` |

## 范围 / 边界

本规约定义请求认证、主体凭据存储、授权判定、资源保护（限流 / 并发预算 / 绑定策略）、
静态加密配置与安全运行期装配的不变量。审计完整性由对应审计特性扩展。

安全能力统一归属 `src/common/security/`，按能力域分子包：

| 子包 | 承载 |
|---|---|
| `authentication/` | `Authenticator`、`PrincipalKeyStore` 与三个内置实现 |
| `authorization/` | `Authorizer`（PDP）、`GrantStore`、`DelegationStore`、`scope_rules` |
| `cryptography/` | `CryptographyProvider`、`KeyProvider`、ENC1 本地信封实现 |
| `protection/` | `RateLimiter`、`WorkloadGuard`、`BindingPolicy` |
| `types.py` | `AuthContext`、`RequestSecurityContext`、`CryptoContext`、`Role`、`Surface`、`Credentials`、`Action`、`ResourceDescriptor`、`AuthorizationEnvironment`、`DenyReason` |
| `request_context.py` | `RequestSecurityContext` 的受控构造入口：`new_request_context` / `internal_context` |
| `runtime.py` | `SecurityRuntime`：持有能力引用、启动期健康检查、统一生命周期 |
| `key_source.py` | 外部密钥源的兼容抽象接缝；当前不进入 `KeyProviderProducer` 注册装配链 |

`audit_integrity/` 由后续 PR 补齐。

> 历史状态：这些能力此前平铺在 `src/common/authentication/`、`credential_store/`、
> `admission/`、`encryption/` 与 `type_def/auth.py`。那是迁移前的目录形态，不再作为
> 新代码的约束——新增安全能力一律落 `src/common/security/<能力域>/`。

## 不变量

### 身份与认证

1. 请求身份只由认证中间件产生，客户端 payload 中的身份字段不是可信身份来源。
2. `Authenticator.authenticate` 成功返回 `AuthContext`，失败抛 `AuthenticationError`；不得返回默认身份。
3. `Scope` 与 `AuthContext` 是 frozen value object，认证后不得原地改写身份或隔离维度。
4. `Authenticator.mode()` 返回**开放字符串**而非封闭枚举。核心不得按该值分支——需要
   分支的行为差异必须由 capability 方法显式声明，第三方实现无需改核心即可接入。

- **凭据在线复核**：`PrincipalKeyStore.is_revoked` 是可撤销凭据的契约方法；
  `ApiKeyAuthenticator` 在认证期校验其已覆盖（第三方缺实现即时失败，不等到首个授权请求
  500）。PEP 持有 `CredentialStatusRegistry`，按 `(credential_type, credential_issuer)`
  复合键路由到发证 Store，在每次授权前复核 `AuthContext` 未撤销--撤销前缓存的上下文撤销后
  立即失效。内联认证器由 `Authenticator.bind_instance_name()` 接收 Runtime 派生的稳定 issuer；
  显式具名认证器保留其配置名。有 `credential_id` 但 issuer 未注册时必须 fail-closed；
  `AuthContext` 保持纯数据值对象，撤销复核不进值对象。

- **请求上下文受控构造**：`RequestSecurityContext` 的 `_origin` 是进程随机密钥签发的
  HMAC-SHA256 来源证明，Canonical JSON 绑定完整 `AuthContext` 及 request id、surface、peer、
  started_at、attributes。PEP 调 `has_valid_origin()` 校验；直接构造或经 `dataclasses.replace`
  改任一安全字段都会失配。该机制只防跨进程伪造，不承诺抵御可调用受控入口的恶意同进程代码；
  不可信插件必须用进程隔离或 capability 边界处理。

### 依据 capability 做安全决策

5. `Authenticator.requires_loopback_binding()` 默认返回 `True`。只有实现显式声明可远程
   暴露，surface 才能绑定非 loopback 地址。
6. `requires_concurrency_guard()` 默认返回 `True`；轻量实现必须显式返回 `False` 才能跳过
   并发预算。
7. 持久化、原子写、密钥轮换、分布式限流等能力必须由类型或 capability 显式声明。禁止通过
   `target == "sqlite"`、类名后缀或配置路径推测安全保证。`WorkloadGuard.supports_distributed_budget()`
   是这条的一个实例：进程内实现返回 `False`，多副本部署据此判断实际并发是 N 倍。

### 资源保护

8. 绑定约束由 `BindingPolicy.check(hosts, *, requires_loopback)` 在实际 socket 绑定前执行，
   不能只存在于某个 CLI `main()`。`requires_loopback` 是 keyword-only，位置传参会让放宽
   在调用点看不出来。
9. 限流在 `authenticate` **之前**执行：认证本身就是要保护的资源。
10. 密码哈希、密钥派生与全量完整性验证等昂贵操作使用独立的全局并发预算。预算耗尽时快速
    拒绝（429），不得无界排队——排队只是把资源耗尽从 CPU/内存转移到线程和请求队列。

### 密码学

11. 密码学能力只能通过 `KeyProvider` 获取密钥，不能直接读取环境变量或配置文件中的根密钥。
12. 信封至少包含 magic、格式版本、algorithm id、**key id 与 key epoch**、nonce、ciphertext 与
    authentication tag。AAD 必须绑定规范化 Scope、存储用途、对象标识和格式版本。
13. 不提供隐式明文回退：要求加密的数据不是合法信封时拒绝读取；解密失败不得返回原始 bytes；
    是否允许未加密存储由上层显式选择不同的存储适配器表达，同一个加密适配器内部不存在
    `allow_plaintext` 降级开关。

### 装配

14. YAML 只选择已注册 target 并传递 params，不接受 Python import path 或任意类加载。
15. 任一安全顶层段存在多个具名实例时必须定义 `default`，否则拒绝启动。
16. 实现只依赖能力接口，不 import 其他实现目录；注册在装配前统一完成。
17. `SecurityRuntime` 只持有能力引用、执行启动期健康检查并暴露统一生命周期，不实现认证、
    授权或密码学算法。能力不健康必须在启动期拒绝，不能等第一个请求打进来才在 500 里暴露。
18. 运行期共享状态（并发预算、限流桶）通过**具名实例**显式共享，不靠模块级单例。
19. `SecurityRuntime` 不为后续 PR 预留恒为 `None` 的占位字段——那会诱导消费方写
    `if runtime.authorizer:` 的 fail-open 分支。
20. 健康检查不泄露 key、token 或主体存在性。

### 授权

21. ROOT 权限只由可信 `AuthContext.role` 判定，不能由空 actor 或请求参数隐式推导。空
    `Scope()` actor 是「上下文不完整」的信号，PDP 对它直接拒绝。
22. agent 代操作必须同时满足同 org、明确的委托目标与授权侧委托规则；不得覆盖其他 user
    或 agent 分支。委托关系只来自服务端 `DelegationStore`，请求里带的委托声明不可信。
23. 授权判定的调用形态演进必须保持 fail-closed 兼容，路由实现不得丢失角色上下文。
24. **PDP 输入封闭**：`Authorizer.authorize` 固定接收 `AuthContext + ResourceDescriptor +
    AuthorizationEnvironment` 三个 keyword-only 参数，**不读 ContextVar**，也不读存储真源。
    资源的安全 metadata 由 PEP 从真源解析后摊平成 `ResourceDescriptor`，请求 metadata
    不能覆盖它。
25. **PDP 不抛异常表达拒绝**：返回 `AuthorizationDecision`（`allowed` + 稳定 `reason` code
    + `rule`），allow 与 deny 两侧都必须标明是哪条规则做的判定。存储不可用等真实故障仍然
    抛——不能把故障静默成 deny，PEP 需要区分 403 与 503。
26. **唯一 PEP**：授权执行点只有 `api.MemoryAPI`，其所有公开 verb 经同一个 `_authorize`。
    不存在第二条能绕开它的授权入口，`SecurityRuntime` 也不代为转发。
27. **没有安全上下文就进不了 API**：`security: RequestSecurityContext` 是所有公开 verb 的
    必填参数。不存在 `auth=None` 分支，不存在空 `Scope()` 自动管理员，业务 payload 不接受
    `actor` / `role` / `acting_user`。
28. **默认拒绝**：未被 owner 覆盖、Delegation、Grant 或角色闸门覆盖的动作一律拒绝
    （`reason=NOT_COVERED`）。新增 `Action` 在写出对应规则前默认落在拒绝侧。
29. **委托是显式 allowlist**：可委托动作见 `common.security.types.DELEGATABLE_ACTIONS`，
    不含 `SHARE` 与管理动作——否则一次临时委托可升级成永久 Grant。
30. **管理面按角色分级**：`MANAGE_PRINCIPAL` / `MANAGE_SPACE` / `MANAGE_POLICY` /
    `READ_AUDIT` 要求 ADMIN 及以上，`VERIFY_AUDIT` / `ADMINISTER_SYSTEM` 要求 ROOT。
    ADMIN 的管辖止于本 org；无 org 归属的系统级资源（全局治理策略、跨 org 审计）只有
    ROOT 能碰，且其拒绝原因是 `ROLE_REQUIRED` 而非 `CROSS_ORG`——reason code 是审计与
    告警的匹配依据，指错方向会把排查引向配置而非权限。
31. **恒放行实现按 capability 拦截**：`Authorizer.is_test_only()` 为真的实现在生产装配被
    拒绝启动，要用必须显式打开 `globals.allow_test_only_security`。判据是 capability 而非
    `target == "allow_all"`（不变量 7）。

### RequestSecurityContext 的构造

32. `RequestSecurityContext` 只能由 `common.security.request_context` 的两个入口构造：
    `request_id` 由服务端生成（不接受调用方传入）、`started_at` 取服务端时钟、`surface`
    无默认值必须由适配层显式写入、`peer` 经可信代理规则规范化（无可信代理白名单时只采信
    传输层地址，不读 `X-Forwarded-For`）、`attributes` 只由系统组件写入。
33. **进程内调用与外部请求使用同一契约**：进程内直连调用方走
    `internal_context(authenticator)`，authenticator 必填且身份仍由它产出，调用方不能自行
    声明身份。不允许把传入的 `Scope` 直接包装成已认证 actor，也不允许无参领取 ROOT。
34. ContextVar（`common.security.types` 的 `set_current` / `get_current` / `reset_current`）
    降级为日志与 trace 的辅助传播：`Authorizer` 与 PEP 均不得依赖其存在，缺失它不改变任何
    授权结论。

## 注册与配置

每个能力目录的顶层 `.py` 定义抽象接口和 Producer，`*_impl/` 中的实现通过
`@Producer.register("target")` 注册，`common.bootstrap.register_plugins()` 在配置解析前统一触发。

实现模块必须在配置解析前由 `common.bootstrap.register_plugins()` 或应用自己的注册入口
import，注册装饰器才会生效。当前核心不自动发现任意外部 Python 包；外部插件应由宿主应用
在 `Server.build` / `build_kernel` 前显式加载。

顶层段名：`security`、`authenticator`、`key_store`、`authorizer`、`grant_store`、
`delegation_store`、`rate_limiter`、`workload_guard`、`binding_policy`、`cryptography`、
`key_provider`。

```yaml
security:
  default:
    target: standard
    params:
      authenticator: default          # 必填，无默认实现
      authorizer: default             # 省略时引用具名实例 authorizer.default
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
      grant_store: default            # 两个 Store 都无默认实现
      delegation_store: default
grant_store:
  default:
    target: memory
delegation_store:
  default:
    target: memory
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
配置。`authorizer` 的默认是**具名实例** `authorizer.default`（不是匿名新建）——内核装配
`api.memory_api_impl.build_kernel` 已经建过它并注入了 PEP，Factory 的具名缓存类级共享，
故 `SecurityRuntime` 命中的是同一个实例。健康检查若检查的是另一份持有另一套
Grant/DelegationStore 的 authorizer，给出的是虚假保证。代价是**装配顺序**：
`SecurityRuntime` 必须在 `build_kernel` 之后建；独立装配（如单测）须在 `security.params`
里显式给出 `authorizer`。

其余能力的默认取保守侧，且默认值本身由 capability 决定而非 target 名——认证声明
`requires_loopback_binding()` 时限流默认 `unlimited`（无远端攻击面），否则默认 `token_bucket`。

未配置 `security` 段时回落 DEV 并告警。回落到 DEV 而非拒绝启动是刻意的：它把「无认证」
从隐式且不可改，变成显式、可切换、且非 loopback 时由 `BindingPolicy` 拒绝启动。

## 当前扩展边界

- `Authenticator`、`PrincipalKeyStore`、`Authorizer`、`GrantStore`、`DelegationStore`、
  `RateLimiter`、`WorkloadGuard`、`BindingPolicy`、
  `CryptographyProvider`、`KeyProvider` 均可通过 Producer 注册扩展。
- `GrantStore` 与 `DelegationStore` 是两个独立 Producer：授权记录与委托记录的生命周期、
  撤销语义与保留期都不同，共用一个后端会让「撤销一次委托」和「回收一条永久授权」走同一
  条代码路径。
- `KeyProvider` 是独立 Producer：换 KMS / Vault 不必改加密实现。
- Server 按 capability 决策绑定和并发保护，不按封闭枚举分支。
- 认证根装配消费一个最终实例；需要多认证串联时，应注册组合 target，由该 target 通过
  `Producer.dep()` 引用多个具名实例，而不是让 YAML 隐式并行执行。
- EncryptedKVStore / EncryptedFSStore 只负责存储边界接线，密码学实现归 `common/security/cryptography`。
- `FsProducer` 已能独立装配 FSStore，但当前 `build_kernel` 主业务链路没有 FSStore 消费点；
  仅写 YAML 不会自动让记忆资产经过 FS 加密，接入前须先定义资产写入/读取消费者和 API 契约。
- 内置 `LocalKeyProvider` 支持多代轮换：`rotate()` 生成新随机根密钥并推进 epoch，旧 epoch
  根密钥保留在进程内字典供 `unwrap` 解开历史信封（写出一律 v2 信封，v1 只读兼容）。新根
  密钥**不持久化**--进程重启回到配置声明的初始密钥，轮换后写入的信封在重启后不可读；
  需要跨重启保留轮换状态应换 KMS/Vault，由其管理历史 epoch 验证材料。
