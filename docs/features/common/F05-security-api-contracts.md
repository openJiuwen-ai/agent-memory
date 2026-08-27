# 安全域接口契约（接口先行）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-20 |
| 影响范围 | `jiuwen_memory/common/security/`、`bootstrap/core/auth_middleware.py`、`bootstrap/mcp_server/transport_security.py`、`docs/specs/S07-common.md` |
| 关联文档 | 历史安全架构与 PR1/PR2 接口说明（仓库外归档）；[F04 安全接口与加密设计](F04-security-interfaces-and-encryption.md) |
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
随 PR2 合入**。PR1 过渡期由 surface（`bootstrap.core.auth_middleware.authenticated`）
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
- 上述接缝接入实际 Server lifecycle（HTTP / MCP / CLI）
- PR2：授权域（`authorization/`）、`RequestSecurityContext` 受控构造入口、
  `MemoryAPI` 的 `security=` 签名切换与 `legacy_request_context` 过渡桥
