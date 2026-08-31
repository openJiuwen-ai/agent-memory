# 安全域接口契约（接口先行）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-20 |
| 影响范围 | `jiuwen_memory/common/security/`、`jiuwen_memory/api/`、`bootstrap/core/`、`bootstrap/mcp_server/transport_security.py`、`docs/specs/S02-memory-api.md`、`docs/specs/S07-common.md` |
| 关联文档 | [F05 公共安全架构](../../../security-plans/F05-common-security-architecture.md)、[PR1/PR2 接口说明文档](../../../security-plans/2026-08-17-PR1-PR2-接口说明文档.md)、[F04 安全接口与加密设计](F04-security-interfaces-and-encryption.md) |
| 状态 | **接口先行、实现暂缓**：契约层已合入，`*_impl` 实现包与 Server lifecycle 接线随后续实装 PR 合入 |

## 1. 背景与目标

主项目处于版本发布阶段，认证/加密/隔离的完整安全实现（`authentication_impl` /
`cryptography_impl` 等）暂缓合入。经项目负责人评审通过的方案是**先把公共接口固定**：
类型、抽象契约、公开函数签名与导出按 F05 架构与接口说明文档原样合入，运行链路
保持原状（不启用任何新认证/授权逻辑）。

这样实装 PR 合入时只填充实现，不再发生公共签名层面的破坏性变更。

## 2. PR1 固定的接口（认证 / 加密 / 保护）

### 2.1 公共值对象（`security/types.py`）

| 类型 | 语义 |
|---|---|
| `Credentials` | 一次认证的原始凭据材料（协议无关、已归一化） |
| `AuthContext` | 认证产出：actor `Scope` + role + 凭据可信度信息 |
| `RequestSecurityContext` | 把可信身份绑定到一次具体请求（request_id / peer / surface / started_at / attributes） |
| `CryptoContext` | 密码学调用上下文 |
| `Role` / `Surface` / `ROLE_RANK` | 三级角色、接入形态枚举与角色偏序 |

`RequestSecurityContext` 的 `attributes` 构造时统一冻结为只读映射；来源绑定
（`_origin`）机制已就位，**受控构造入口（`new_request_context` / `internal_context`）
随 PR2 合入**（见 §5.1）。PR1 过渡期由 surface（`bootstrap.core.auth_middleware.authenticated`）
直接构造。

### 2.2 能力契约（各子包 `base.py`）

| 子包 | 契约 |
|---|---|
| `authentication/` | `Authenticator`（authenticate/mode/requires_concurrency_guard）、`PrincipalKeyStore`、`CredentialStatusRegistry` |
| `cryptography/` | `CryptographyProvider`（encrypt/decrypt/health）、`KeyProvider`（active_key/rotate/wrap/unwrap/health） |
| `protection/` | `RateLimiter`、`WorkloadGuard`、`BindingPolicy` |
| `runtime.py` | `SecurityRuntime`（跨能力装配根；PR2 起增加 `authorizer` 引用） |

### 2.3 Bootstrap 接缝（`bootstrap/core/auth_middleware.py`）

```python
credentials_from_headers(headers, peer_address="") -> Credentials

authenticated(
    authenticator, credentials, audit=None, limiter=None, *,
    workload_guard=None, surface=None,
) -> Iterator[RequestSecurityContext]

credentials_for_transport(  # bootstrap/mcp_server/transport_security.py
    transport, *, context=None, environ=None,
) -> Credentials
```

- `authenticated()` 按「限流 -> 并发预算 -> 认证 -> 构造请求上下文」执行，yield
  `RequestSecurityContext`；ContextVar 仍设置 `AuthContext` 但只作日志/trace 辅助，
  授权不读它，退出时 `finally` reset。
- `credentials_for_transport()`：MCP stdio 读 `AGENT_MEMORY_API_KEY`；Streamable HTTP
  逐请求提取 Bearer 与 socket peer，缺 FastMCP Request Context 必须 fail-closed，
  **不回退读取进程环境变量**。

## 3. 与旧 `SecurityProvider` 的关系

旧 `jiuwen_memory/common/security/security.py`（`SecurityProvider` 系，服务于存储加密装配）在实现
PR 落地前继续从 `jiuwen_memory.common.security` 顶层导出，既有消费方不受影响。新契约的同名异常
从各能力子包取，不与顶层旧导出冲突。两者并存期间新代码一律依赖 F05 契约层。

## 4. 暂缓合入清单（实装 PR 交付）

- `authentication_impl/`（dev / trusted / api_key 三种 Authenticator 及 KeyStore 后端）
- `cryptography_impl/`（KeyProvider 后端）
- `authorization_impl/`（`Authorizer` / `GrantStore` / `DelegationStore` 的后端实现）
- 上述接缝接入实际 Server lifecycle（HTTP / MCP / CLI）
- 删除过渡桥 `common/security/legacy.py` 与全部 `legacy_request_context(...)` 调用点

## 5. PR2 固定的接口（隔离 / 授权）

### 5.1 `RequestSecurityContext` 的受控构造入口（`security/request_context.py`）

```python
new_request_context(auth, *, surface, peer="", attributes=None) -> RequestSecurityContext
internal_context(authenticator) -> RequestSecurityContext
```

构造点从各 surface 收到这一处：`request_id` 服务端生成、`started_at` 取服务端时钟、
`attributes` 只由系统组件写入——三条不变量只有一处实现。产物携带来源绑定
（HMAC over 全部安全字段，进程内随机 key），`has_valid_origin()` 可判定其是否出自
受控入口，`replace(attributes=...)` 一类的旁路提权因此可被识别。

`internal_context(authenticator)` 的 `authenticator` **必填**：进程内直连的身份仍由
authenticator 产出，不接受调用方以 `Scope` 自述身份，也不再有「无参领 ROOT」的默认。

`bootstrap.core.auth_middleware.authenticated()` 改为经 `new_request_context` 构造，
`surface` 由适配层写入（缺省 `INTERNAL`），`peer` 只取传输层对端地址。

### 5.2 授权域公共类型（`security/types.py`、`security/authorization/`）

| 项 | 内容 |
|---|---|
| `Grant` | frozen；`grantor` / `grantee` / `actions: frozenset[Action]` / `expires_at` / `grant_id` / `revoked`，含 `is_active(*, now)`；`grant_id` 构造时默认留空，actions 在构造边界冻结并校验 |
| `Action` | 12 个动作；`control.types.Action` 只作同对象兼容再导出，过渡期 PermissionManager 仅执行原五动作 |
| `Authorizer` | 授权判定契约（`authorization/base.py`），产出 `AuthorizationDecision`；`SecurityRuntime.authorizer` 引用 |
| `RoutingFieldsProvider` | `PermissionManager` 与 `Authorizer` 共用的策略路由 capability，默认空元组；PEP 只依赖这一份契约 |
| `GrantStore` / `DelegationStore` | 授权真源契约（`authorization/store.py`）：`grant_id` 是存储主键，撤销按 ID |
| `scope_covers` | 主体路径感知的 Scope 覆盖判定（`authorization/scope_rules.py`） |

`api` 包的 `Grant`/`Action` 导出切到安全域类型；`control.types` 为兼容既有导入路径
再导出同一对象，不再定义第二套四字段 Grant / 五成员 Action。API 将同一 Grant 实例
交给过渡期 `PermissionManager`，不做会丢弃 `grant_id` / `revoked` 的结构转换。

### 5.3 `MemoryAPI` 公开签名：`identity: Scope` → `security: RequestSecurityContext`

公开方法的安全输入统一改名为 `security`。通常保持 keyword-only；原签名为
`check_write(scope, identity, *, ...)` 的预检接口保留第二位置参数，替换后为
`check_write(scope, security, *, ...)`，避免身份类型迁移同时引入无关的参数顺序破坏。
授权面的目标契约（**签名固定、语义随实装启用**，见 §5.4）：

| 方法 | 契约 |
|---|---|
| `grant(grant, *, security) -> Grant` | 返回值携带该授权的 `grant_id`，供后续精确撤销 |
| `revoke(grant, *, security) -> None` | 按 `grant.grant_id` 精确回收（幂等） |

### 5.4 当前过渡行为（与目标接口的差异）

正式接口如上，但本期**不启用**任何新认证/授权实现，运行链路仍是原有的
`PermissionManager`，**对外行为与 `mem2.0` 逐位等价**。以下差异是已知的过渡态，
实装 PR 收敛：

| 项 | 目标形态 | 当前过渡行为 |
|---|---|---|
| 身份来源 | 接入层认证产出 | `legacy_request_context(actor)` 把 payload 里的 identity 包成上下文（**假认证**：role / credential 为占位值） |
| 授权判定 | `Authorizer` + 策略 + 决策审计 | 仍走 `PermissionManager.check`，逐位等价于 identity 直传时代 |
| `revoke` 鉴权动作 | `REVOKE_SHARE` | `SHARE`（旧 `PermissionManager` 无 `REVOKE_SHARE`） |
| `grant_id` 生成 | 服务端生成，写入 `GrantStore` | **不生成**：`grant()` 原样回传入参 Grant，`grant_id` 保持为空 |
| `revoke` 定位方式 | 按 `grant_id` 精确 | 仍按 grantor+grantee+action 条件撤销，`grant_id` 不参与定位、也不做非空校验；`/v1/revoke` 的旧请求形状不变 |
| 安全域独有动作 | 由 `Authorizer` 判定 | 与 control 共用同一 Action 类型，但旧 PermissionManager 尚无管理动作角色闸门，API 在委托前抛 `ValueError`（fail-closed） |

`grant_id` 一栏是刻意的取舍：`GrantStore` 未实装前生成一个不参与定位的随机 ID，
等于用接口语义掩盖实际的条件撤销。因此本期只固定签名，不产出 ID、不据 ID 判定，
待 `GrantStore` 落地时一并启用（届时补反向测试：未知/错误 ID 不得撤销其他 Grant）。

### 5.5 `Grant` 公共导出兼容性

**决策**：`api.Grant` 继续导出安全域类型，但安全域构造器保留旧公共 API 的参数形状：
`grantor` / `grantee` / `actions` 仍可直接构造，`grant_id` 默认空值。旧调用方传入的
`list[Action]` 在值对象边界立即冻结为 `frozenset[Action]`；非 `Action` 成员在构造阶段
拒绝。这样公共名字切换不会把失败推迟到 `GrantStore`，也不要求客户端伪造服务端字段。

**拒绝的方案**：没有采用“保留必填 `grant_id`，只在迁移文档提示调用方”的方案，
因为 `from jiuwen_memory.api import Grant` 的导入路径和类型名都不变，调用方无法在导入
阶段发现硬破坏；也没有继续容忍任意 `actions` 容器原样进入 frozen dataclass，因为这会
制造表面不可变、实际持有可变 list 的值对象，并把类型错误延迟到存储或授权判定路径。

同样没有保留 `_to_control_grant` 做新旧值对象转换：该转换会静默丢弃 `grant_id` 与
`revoked`，并迫使两套类型永久共存。过渡期只保留执行引擎 `PermissionManager`，值对象
和 `routing_fields()` capability 已先收敛为单一真源；完整 `Authorizer` 调用链仍按本 PR
“接口先行、实现暂缓”的边界留给实装 PR。

**`legacy_request_context` 的移除点**：`jiuwen_memory/common/security/legacy.py` 及其全部调用点
（`bootstrap/core/handler.py`——HTTP / MCP / CLI 都经它 dispatch、
`agent_plugin/jiuwenswarm/agent_memory_provider.py`、`evaluation/core/harness.py`、
`examples/quickstart.py`、`tests/`）在 `authentication_impl` 合入、各 surface 接上
`authenticated()` 的同一个 PR 中删除。届时接入层直接产出 `RequestSecurityContext`，
`MemoryAPI` 公共签名不再变动；请求 payload 携带 identity 的临时态也在那时一并去掉。
