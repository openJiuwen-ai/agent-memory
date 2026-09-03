# S09 — 安全横切契约（Security）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | `jiuwen_memory/common/security/`、`jiuwen_memory_entry/`、`jiuwen_memory/api/`、`jiuwen_memory/storage/` |
| 最近一次修订日期 | 2026-09-03 |
| 关联特性文档 | `docs/features/common/F04-security-interfaces-and-encryption.md`，`docs/features/common/F09-authentication-kernel.md` |

## 范围 / 边界

本规约定义 PR1 已落地的请求认证、主体凭据存储、资源保护（限流 / 并发预算 / 绑定策略）、
静态加密配置与安全运行期装配不变量。授权判定与唯一 PEP 由 PR2 补齐，审计完整性由 PR3 补齐。

安全能力统一归属 `jiuwen_memory/common/security/`，按能力域分子包：

| 子包 | 承载 |
|---|---|
| `authentication/` | `Authenticator`、`PrincipalKeyStore` 与三个内置实现 |
| `cryptography/` | `CryptographyProvider`、`KeyProvider`、ENC1 本地信封实现 |
| `protection/` | `RateLimiter`、`WorkloadGuard`、`BindingPolicy` |
| `types.py` | `AuthContext`、PR2 预置的 `RequestSecurityContext`、`CryptoContext`、`Role`、`Surface`、`Credentials` |
| `runtime.py` | `SecurityRuntime`：持有能力引用、启动期健康检查、统一生命周期 |

`authorization/`（PR2）与 `audit_integrity/`（PR3）的**契约**随上游接口分支已在库中，但 PR1 不实装其 `*_impl`，也不创建 F08 授权特性文档。`request_context.py` 的受控构造入口 PR1 已由认证中间件使用（PEP 的 `has_valid_origin()` 校验仍归 PR2 启用）。Runtime 的必填 `authorizer` 字段 PR1 装 `allow_all` 占位（`is_test_only()` 为真，不做任何判定；做判定的 `StandardAuthorizer` 随 PR2 合入）；`audit_integrity_provider` 是 F05 定义的可选装配位，PR1 固定字段与健康检查位、PR3 填实现。

> 历史状态：这些能力此前平铺在 `common/authentication/`、`credential_store/`、
> `admission/`、`encryption/` 与 `type_def/auth.py`。那是迁移前的目录形态，不再作为
> 新代码的约束——新增安全能力一律落 `jiuwen_memory/common/security/<能力域>/`。

## 不变量

### 身份与认证

1. 请求身份只由认证中间件产生，客户端 payload 中的身份字段不是可信身份来源。
2. `Authenticator.authenticate` 成功返回 `AuthContext`，失败抛 `AuthenticationError`；不得返回默认身份。
3. `AuthContext` 是 frozen value object；其 `actor` 使用全局固定的可变 `Scope` 类型，内置
   Authenticator 必须为每次认证返回独立的 Scope 快照，不得复用模块级或 Store 内对象。
   `RequestSecurityContext` 的来源证明绑定 actor 全部维度，PR2 PEP 启用来源校验后，构造后
   原地改写 actor 必须导致校验失败。
4. `Authenticator.mode()` 返回**开放字符串**而非封闭枚举。核心不得按该值分支——需要
   分支的行为差异必须由 capability 方法显式声明，第三方实现无需改核心即可接入。
5. **actor 全局形态不变量（IMPL-01 §1.1）**：`AuthContext.actor` 恒为单主体--`org`
   必须非空（部署级凭据用 `org="system"`）；`user` 与 `agent` 必须且只能有一个非空；
   `session` 若非空必须挂在已确定的主体下。规则对**所有**认证模式统一生效（内置
   API Key / Trusted / DEV / Root Key 与第三方 `Authenticator`），由
   `security.types.validate_actor_form` 在 `authenticated()` 认证边界统一执行，非法
   形态 fail-closed 并落入口拒绝审计。`Scope(user=..., agent=...)` 同时非空仍是
   资源层 `principal_path` 的合法层级表达，只是不作为认证 actor。
6. **具名系统主体**：DEV 与 Root API Key 产出 `Scope(org="system", user="dev")` /
   `Scope(org="system", user="root")`，ROOT 权限只由 `role=Role.ROOT` 表达，不来自
   actor 形状。**PR1 无 role 执行点**：认证层产出的 role 传递到 `AuthContext` 后，
   过渡期判定仍走 `PermissionManager.decide`（无 `auth` 参数，退回纯 ACL），
   `role=Role.ROOT` 在 PR1 无任何放行判定、不具特权——这是已知过渡缺口，随 PR2 由
   `Authorizer` 接管 role 闸门（见页面末尾「已知过渡缺口」）。`LocalMemoryAPI._authorize`
   只取 `security.auth.actor` 走原有的 PermissionManager 路径，不读 ContextVar 透传
   role（该接缝已在 PR1 撤回）。

- **凭据在线复核接缝**：`PrincipalKeyStore.is_revoked` 与 `CredentialStatusRegistry` 已在 PR1
  实现并有镜像单测；`ApiKeyAuthenticator` 在认证期校验 Store 覆盖了撤销查询。
  `AuthContext.credential_status_required` 显式声明是否需要在线复核，并通过
  `(credential_type, credential_issuer)` 路由到平行 Authenticator 各自的真源。ROOT key、
  trusted gateway 等身份可携带非空 `credential_id` 做审计，但 capability 为 false，不能由
  id 形状猜测撤销语义。声明需要复核却缺少 id 或注册 issuer 时 fail-closed。
  `AuthContext` 仍是纯数据值对象，不携带 Callable 或 Store 引用。PR1 尚无 Authorizer / PEP
  消费这个 Registry，因此“撤销前缓存的上下文立即失效”尚未接入请求链路；PR2 由唯一 PEP
  完成逐请求复核。

- **PR2 类型接缝**：PR1 已定义 `RequestSecurityContext` 及受控来源标记，`MemoryAPI`
  公开签名已切到 `security: RequestSecurityContext`（接口先行合入）。受控构造入口的
  PEP 侧 `has_valid_origin()` 校验、`dispatch` 的 `security=` 签名切换与授权来源校验
  属于 PR2，不能写成 PR1 已完成能力。

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
- MCP surface 只构造 `Credentials`，不得直接构造 `AuthContext`。PR2 再把认证结果封装为
  `RequestSecurityContext` 交给 PEP。

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
20. 运行期共享状态（并发预算、限流桶）通过**具名实例**显式共享，不靠模块级单例。
21. `SecurityRuntime` 不为后续 PR 预留恒为 `None` 的**必填**能力占位字段——那会诱导消费方写
    `if runtime.authorizer:` 的 fail-open 分支。F05 明确定义为可选装配位的字段
    （`cryptography_provider` / `audit_integrity_provider`）不在此列：消费方本就必须判空，
    未装配即该能力未启用。
22. 健康检查不泄露 key、token 或主体存在性。

## 注册与配置

每个能力目录的顶层 `.py` 定义抽象接口和 Producer，`*_impl/` 中的实现通过
`@Producer.register("target")` 注册，`common.bootstrap.register_plugins()` 在配置解析前统一触发。

实现模块必须在配置解析前由 `common.bootstrap.register_plugins()` 或应用自己的注册入口
import，注册装饰器才会生效。当前核心不自动发现任意外部 Python 包；外部插件应由宿主应用
在 `Server.build` / `build_kernel` 前显式加载。

顶层段名：`security`、`authenticator`、`key_store`、`rate_limiter`、`workload_guard`、
`binding_policy`、`cryptography`、`key_provider`。

```yaml
security:
  default:
    target: standard
    params:
      authenticator: default          # 必填，无默认实现
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

未配置 `security` 段时回落 DEV 并告警。回落到 DEV 而非拒绝启动是刻意的：它把「无认证」
从隐式且不可改，变成显式、可切换、且非 loopback 时由 `BindingPolicy` 拒绝启动。

### PR1 的 bootstrap DEV 业务连续性例外

公共 Core 入口 `api.build_kernel()` / `api.assemble()` 未显式选择 permission 时，默认仍为
`SQLitePermissionManager`，不得因为 DEV 认证而改成全放行。

在 PR2 Authorizer 尚未接管 `role=ROOT` 的过渡期，只有 bootstrap `Server.build` 可以在以下
条件**同时满足**时，向 `memory_api` 配置的副本注入 `permission.default=allow_all`：

1. 用户没有配置 `security` 段，Runtime 将回落到 DEV；
2. 用户没有配置 `permission` 段；
3. DEV 的 `BindingPolicy` 仍限制实际监听地址为 loopback。

任一显式 security 或 permission 配置都必须禁止覆写，原始 `config.settings` 不得被修改。
该例外只用于保持既有本地 add/get 等业务流程，不得扩散到 Core SDK、API Key 或 Trusted 部署；
PR2 接通 Authorizer 的 ROOT 角色闸门后应删除。

## 当前扩展边界

- `Authenticator`、`PrincipalKeyStore`、`RateLimiter`、`WorkloadGuard`、`BindingPolicy`、
  `CryptographyProvider`、`KeyProvider` 均可通过 Producer 注册扩展。
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
