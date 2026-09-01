# 安全域接口契约（接口先行）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-20 |
| 影响范围 | `jiuwen_memory/common/security/`、`jiuwen_memory/common/audit/`、`jiuwen_memory/api/`、`bootstrap/core/`、`bootstrap/mcp_server/transport_security.py`、`docs/specs/S02-memory-api.md`、`docs/specs/S07-common.md` |
| 关联文档 | [S02 记忆接口层](../../specs/S02-memory-api.md)、[S07 公共组件层](../../specs/S07-common.md)、[F04 安全接口与加密设计](F04-security-interfaces-and-encryption.md) |
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
- `cryptography_impl/`（KeyProvider 后端；`LocalKeyProvider` 补齐 MAC capability）
- `authorization_impl/`（`Authorizer` / `GrantStore` / `DelegationStore` 的后端实现）
- `audit_integrity_impl/`（版本化规范化 + 链式 HMAC 的 `AuditIntegrityProvider`；内存 /
  SQLite 审计后端叠加 `ChainedAuditStore`；锚点产品实现）
- 上述接缝接入实际 Server lifecycle（HTTP / MCP / CLI）
- 删除过渡桥 `jiuwen_memory/common/security/legacy.py` 与全部 `legacy_request_context(...)` 调用点

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

## 6. PR3 固定的接口（审计完整性）

### 6.1 完整性契约包（`security/audit_integrity/`）

| 项 | 内容 |
|---|---|
| `AuditIntegrityProvider` | 完整性能力契约（`base.py`）：`capabilities` / `chain_store` / `record_chained` / `verify` / `active_key_ref` / `health` / `is_test_only`；`chain_store()` 暴露实际使用的 store 供装配 identity 校验；`AuditIntegrityProducer.TOP_NAME = "audit_integrity"` |
| `Proof` | 链式证明，独立于 `AuditEvent.detail` 的 frozen 值对象（format_version / sequence / previous_digest / digest / key_id / key_epoch） |
| `AuditIntegrityStatus` | `unsupported` / `clean` / `tampered` / `incomplete` / `rollback_suspected`；除 `clean` 外都不得当 clean（unsupported / incomplete 亦然） |
| `AuditVerificationLimits` / `AuditVerificationResult` | 服务端可信单次资源上限，以及 `verify_audit` 的 frozen、不含秘密结构化返回；`to_body()` 输出 §6.4 的 Body |
| `AnchorStatus` / `AnchorState` | 外部锚点核对状态枚举（`""` / `ok` / `lagging` / `conflict` / `unavailable`）及结果；`checked=False` 只能使用 `UNCHECKED`，`checked=True` 必须给出非空状态，wire 仍输出枚举字符串值 |
| `AuditIntegrityError` 族 | 单根于 `AgentMemoryError`：`AuditMigrationRequiredError` / `ChainConflictError` / `AuditSchemaError` / `KeyCapabilityError` |
| `ChainedAuditStore` | 后端原子链式追加 capability（`chain_store.py`）：`read_head` / `append(record, expected_head)` / `read_stable_snapshot(after_sequence)` / `scan(after_sequence, limit, *, through_sequence)` / `health` |
| `ChainStoreCapability` | 六布尔行为声明：persistent / atomic_append / stable_head_snapshot / key_epoch / external_anchor / streaming_scan；不从 target 名推断后端性质 |
| `AuditAnchor` | 外部可信锚点契约（`anchor_head` / `read_anchored` / `health`）；`GENESIS_DIGEST = "0" * 64` |

`AuditLogger` 基类**不**增加 `verify_integrity` / `get_chain_head` 等默认方法——默认空实现
会把「不支持」伪装成「支持但较弱」（fail-open 公共契约）；完整性能力只经显式
`ChainedAuditStore` capability 表达。

增量验证的窗口语义固定如下：

- `read_stable_snapshot(after_sequence=N)` 在同一临界区/事务读取第 N 条 checkpoint、链头和
  最后一条记录。`N=0` 使用 genesis；`N>0` 必须返回恰好第 N 条的 record，缺失时验证结果
  是 `incomplete`，不得从 genesis 重来、跳过缺口或信任调用方历史 digest。
- provider 先验证 checkpoint proof，再把其 digest 用作续链基线；checkpoint proof 计入
  `checked_count`。通过后即使没有新记录，`high_water_mark=N`。
- 每页 `scan` 固定传同一快照的 `through_sequence=head.sequence`，只返回
  `after_sequence < sequence <= through_sequence`。验证期间的并发追加不进入本次结果，
  留给下一次；页空、序号缺口、截断或无法到达该快照链头一律为 `incomplete`，不能报 clean。
- `high_water_mark` 是本次连续成功验证到的最高 sequence，不是返回瞬间的动态链头；只有
  到达稳定快照链头才可为 clean。合法并发只允许追加，更新/删除/截断属于损坏或攻击路径。

### 6.2 `ProtectedAuditLogger`（`jiuwen_memory/common/audit/protected_audit_logger.py`）

`record(event)` 委派 `AuditIntegrityProvider.record_chained`（失败抛
`AuditIntegrityError`，不吞错、不降级）；`query(filters, limit)` 透传底层
`AuditLogger`。构造时调用 `provider.chain_store()`，要求它与 `audit_logger` 对象 identity
相同；不再仅靠“同一具名实例”的注释假定关键不变量。wrapper 不持有需自管 `close` 的
资源，由审计日志生命周期所有者统一关闭。

### 6.3 `KeyProvider` 的 MAC capability 与 `SecurityRuntime` 装配位

```python
class KeyProvider(ABC):
    def supports_mac(self) -> bool: ...      # 默认 False
    def mac(self, message, *, purpose) -> tuple[bytes, KeyRef]: ...
    def verify_mac(self, message, tag, *, purpose, ref) -> bool: ...
```

不支持 MAC 的 provider 不得静默回退——`mac` / `verify_mac` 默认抛
`NotImplementedError`，由装配期 `supports_mac()` 检查先行拦住；`purpose` 参与密钥
派生实现用途隔离（审计完整性固定 `audit-integrity:hmac:v1`）。

`SecurityRuntime` 增加 `audit_integrity_provider: AuditIntegrityProvider | None = None`：
与 `cryptography_provider` 同为可选装配位（完整性 opt-in，未装配即普通审计）；装配后
纳入 `health()`，provider 不持有需 Runtime 关闭的资源。

### 6.4 `MemoryAPI.verify_audit`（PEP）与接入形态边界

```python
def verify_audit(
    self, *,
    security: RequestSecurityContext,
    after_sequence: int = 0,
    page_size: int = 1000,
    max_samples: int = 20,
    anchor_policy: str = "if_configured",  # if_configured / required / skip
) -> AuditVerificationResult: ...
```

- `verify_audit` 与既有 `audit` 是两个独立入口，不合并；本 PR 只为新入口使用
  `VERIFY_AUDIT`，`audit` 继续按 legacy `READ` 对根 scope 判权，避免使存量精确匹配
  `action='read'` 的授权记录失效。迁移到目标动作 `READ_AUDIT` 须另行评审并迁移授权数据；
  验证输入只允许服务端参数，不接受调用方传入 expected digest / key / proof / chain head；
- provider 与专用 `audit_verify_guard` 成对注入；全量验证占该 `WorkloadGuard` 的一个
  独立并发槽，耗尽抛 `RateLimitedError`。本期不注册 `verify_audit` HTTP verb，也不修改
  generic handler 的既有错误映射（`RateLimitedError` 仍按默认路径返回 400，与 FAQ 一致）；
  未来随真实认证接入改为 HTTP 429 时，应作为影响既有接口的独立兼容性变更评审；
- guard 准入后、调用 provider 前先写入 `verify_audit` 审计事件；provider 因链篡改、
  schema 损坏等抛 `AuditIntegrityError` 时异常原样传播，但发起者与发生时间已经留痕。
  guard 耗尽时调用仍抛 `RateLimitedError`，但事件的 `decision` 保持 `allow`（授权已通过），
  沿用既有 `workload_guard=exhausted` 明细表达容量准入失败，避免 `decision=deny` 的安全
  告警把限流和鉴权拒绝混为一谈；成功或 provider 异常路径不重复写第二条完成事件；
- `page_size` / `max_samples` 先做类型与下界校验，再截到服务端装配的
  `AuditVerificationLimits`；有效上限只从服务端 `globals.audit_verify_max_page_size` /
  `globals.audit_verify_max_samples` 读取。装配边界只接受真正的整数（`bool` 与包括
  `"2000"` 在内的字符串均拒绝），类型错误、负值和超过代码硬上限统一抛
  `ValidationError`，不泄漏值对象的裸
  `TypeError` / `ValueError`。PEP 对 provider 返回样本数再做一次有效上限截断并设置
  `truncated=true`，避免自定义 provider 放大返回体；
- `truncated` 只表示错误样本列表被有效 `max_samples` 截短，扫描未完成用
  `status=incomplete`，不能复用同一标志掩盖缺页；
- 真实认证接入前，`bootstrap/core/handler.py` **不注册** `verify_audit`：legacy handler
  只能从 payload 构造普通 actor，无法构造可信根管理上下文，注册后默认装配必然 403，且
  不能用 payload 自述 root 修补。当前只有形态无关的 `MemoryAPI` 一等入口；HTTP、MCP、CLI
  都暂不提供该管理面的一等入口。认证中间件接入后，HTTP 可直接使用下列
  `AuditVerificationResult.to_body()` 纯 dict；MCP tool 与 CLI command 仍需另行设计：

```json
{
  "op": "verify_audit",
  "status": "clean",
  "checked_count": 5,
  "error_count": 0,
  "truncated": false,
  "high_water_mark": 5,
  "key_epoch_range": [1, 2],
  "anchor": {"checked": false, "status": "", "detail": ""},
  "samples": [{"sequence": 3, "format_version": 1, "previous_digest": "<hex>",
               "digest": "<hex>", "key_id": "<fp>", "key_epoch": 1}],
  "detail": ""
}
```

Body 是**对外线上契约**：字段名、类型与样本 Proof 字段一经发布即为承诺，变更需评审；
不含 `ok` 字段、事件内容、密钥材料或可重放凭据。

### 6.5 当前过渡行为（与目标接口的差异）

正式接口如上，但本期**不实装**任何完整性实现（`audit_integrity_impl` 随实装 PR 合入），
主业务流程不受影响。已知过渡态：

| 项 | 目标形态 | 当前过渡行为 |
|---|---|---|
| 鉴权动作 | `verify_audit` 使用安全域 `Action.VERIFY_AUDIT`；既有 `audit` 的目标动作是 `READ_AUDIT` | `verify_audit` 直接按 `VERIFY_AUDIT` 对根 scope 判权；`audit` 为兼容存量精确匹配 `action='read'` 的授权记录，仍使用 legacy `READ`，本 PR 不做授权数据迁移 |
| 未装配 provider | —— | `verify_audit` 诚实返回 `unsupported`（`detail="audit integrity provider not configured"`），不抛错、不降级成 clean |
| `audit_integrity` 配置段 | `chained_hmac` 实现注册 | 无注册 target，配置该段装配失败（fail-closed，不静默降级为普通审计） |
| `ProtectedAuditLogger` | PEP 与 surface 记录入口都经它 | 无调用点（需要 provider 实例），仅固定接口 |
| `KeyProvider.mac` | `LocalKeyProvider` 支持 | 默认 `NotImplementedError`（所有 provider） |
| Surface 暴露 | 认证中间件产出可信根管理上下文后由 HTTP 暴露；MCP/CLI 另设一等入口 | HTTP generic dispatch、MCP tool、CLI command 均不注册；进程内调用须显式传 `RequestSecurityContext` |
