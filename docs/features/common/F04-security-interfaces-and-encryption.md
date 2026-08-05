# 安全接口与加密设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | `src/common/encryption/`、`src/storage/kv_impl/`、`src/control/engine_impl/`、`docs/specs/S07-common.md`、`docs/specs/S06-storage.md` |
| 测试基线 | `local` EncryptionProvider 直接行为校验通过，`EncryptedKVStore` 单测函数直接执行通过；当前环境缺少 pytest/ruff runner |

本文由原 `docs/security/security.md` 迁入 common 特性归档，作为认证、授权、隔离、加密与审计的安全设计基线。后续 `common/encryption` 接口、`EncryptedKVStore`、`cloud_engine` 读写编排与安全配置均以本文为设计入口。

当前落地状态（2026-07-27）：`common/encryption` 接口已提供 `EncryptionProvider` /
`EncryptionProducer`，`storage/kv_impl/encrypted_kv_store.py` 已提供 KV 加密装饰器；
`encryption_impl/local_envelope.py` 已提供 `local` ENC1 AES-GCM
真实加解密 provider。KMS / Vault provider 仍未实现。

---

## 1. 安全模型总览

### 1.1 三道防线

一个 Agent 记忆框架的安全可以抽象为三道相互独立的防线：

```
① 认证 (Authentication)
   "你是谁?"
   解析请求来源,输出身份上下文
        │
        ▼
② 授权 (Authorization)
   "你能干什么?"
   基于身份 + 角色 + 租户,做访问控制
        │
        ▼
③ 数据保护 (Data Protection)
   "你动不了落盘数据"
   加密 + 完整性校验
```

**核心不变量**：身份信息（org / user 或 agent / role）**永远来自认证层产出的 AuthContext**，不来自 URI、不来自请求体参数、不来自未经校验的 HTTP header（trusted 模式也必须有明确的网关信任边界）。user 与 agent 是同级主体；Agent 代 user 操作时，委托关系来自已验证的 `acting_user`，不能由调用方自报。

> **关联设计文档**：本框架的三道防线与项目的「透明可治理」设计原则一脉相承——见 [`design/vision.md` §3 设计原则](../../design/vision.md)（记忆可检视、可编辑、可审计、可回溯、可遗忘）与 [`design/architecture.md` §12 横切关注点](../../design/architecture.md)（安全合规：scope 权限、端侧数据不出端、传输/存储加密、可遗忘）。

### 1.2 不在三道防线内、但同样重要

| 关注点 | 归属 |
|---|---|
| **Key 管理与分发** | 部署运维 + 框架内置（见 §6） |
| **配置安全** | 部署运维（本文不展开） |
| **传输安全（TLS）** | 部署运维（本文不展开） |
| **审计日志** | 框架内置（见 §7） |

确保 TLS 终止在前置代理（nginx/ALB），框架本身不强制 HTTPS，但生产部署必须有。

---

## 2. 认证（Authentication）

> **关联设计文档**：认证的执行点（PEP, Policy Enforcement Point）落在接口层——每个 API 方法先 `check(identity, scope, action)`、落带 identity 的入口审计，通过后才委托业务。详见 [`design/architecture.md` §9 记忆接口层](../../design/architecture.md)。

### 2.1 设计原则

1. **可插拔认证模式**：框架应支持多种认证模式，在启动时由配置决定，不要硬编码。
2. **每个请求必须过认证**：没有任何 endpoint 能绕过认证层（健康检查可例外）。
3. **单次验证、上下文传播**：认证中间件只验证一次身份，结果注入请求上下文，后续流程不再重复校验身份。
4. **常时间比较（timing-safe）**：所有密钥比对必须使用 `hmac.compare_digest` 或等价的常时间函数。
5. **可插拔算子用注册式工厂（Factory + Producer）**：`agent-memory` mem2.0 的所有核心抽象（PermissionManager / AuditLogger / Governor / Engine / KVStore 等）用 `XxxProducer(Factory)` + `@Producer.register("name")` 自注册，装配时 `Producer.dep(root, default="name")` 按名取实例。安全模块的认证/权限/审计算子同样遵循此模式。
6. **应用层 bootstrap 已生成**：`bootstrap/` 下有 CLI / HTTP server / MCP server / SDK 四种接入形态的薄封装，安全模块通过 bootstrap 挂载。`deploy/` 下有 Docker / local 部署方案。

### 2.2 三种认证模式

框架应支持三种模式的自动推断与显式配置：

> **Demo 实现注记**：demo 的 `AuthMode` 是 **DEV / API_KEY / OAUTH** 三档（TRUSTED 留 TODO 未做）。
> demo 用 OAUTH(MCP OAuth 2.1)替代了 TRUSTED 的位置——agent 走 OAuth bearer token 接入，数据面 bearer-only。
> 自动推断同指南：有 `root_api_key` → API_KEY，否则 → DEV。

```python
from dataclasses import dataclass
from enum import Enum


class AuthMode(str, Enum):
    DEV = "dev"           # 无认证,恒返回 ROOT
    TRUSTED = "trusted"   # 信任上游网关已认证
    API_KEY = "api_key"   # 框架自己校验 API key
    OAUTH = "oauth"       # AI Agent 经 OAuth 2.1 bearer token 接入(见 §2.4)


@dataclass
class ServerConfig:
    # 如果为 None,则自动推断
    auth_mode: AuthMode | None = None
    # 自动推断逻辑:有 root_api_key → API_KEY,否则 → DEV
    # OAUTH 通常显式配置(数据面 bearer-only,与 API_KEY 并列,不靠 root_api_key 推断)。
    root_api_key: str | None = None

    def get_effective_auth_mode(self) -> AuthMode:
        if self.auth_mode is not None:
            return self.auth_mode
        if self.root_api_key:
            return AuthMode.API_KEY
        return AuthMode.DEV
```

#### 2.2.1 DEV 模式

**语义**：不验证任何认证凭据，**无条件返回最高权限身份（ROOT）**。

```python
if auth_mode == AuthMode.DEV:
    # 无条件返回 ROOT 身份
    return AuthContext(actor=Scope(org="*"), role=Role.ROOT)
```

> **主干实现注记**（F01）：主干的 ROOT actor 是**空 `Scope()`**，不是
> `Scope(org="*")`。`SQLitePermissionManager.check` 的第一条规则是
> `actor == Scope() → True`（platform admin 全局放行），而 `org="*"` 会先撞上
> 「跨 org 一律拒绝」规则——用 `org="*"` 的 ROOT 反而寸步难行。
> 见 `src/common/authentication/authentication_impl/dev_authenticator.py`。

**约束**：DEV 模式只允许监听 localhost。启动时如果检测到非 localhost 绑定，应当 `sys.exit(1)` 并打印错误消息。**注意覆盖容器化场景下 `0.0.0.0` 这种最危险的情况**：

```python
_LOCALHOST_ADDRS = {"127.0.0.1", "localhost", "::1"}

def enforce_dev_localhost_binding(bind_host):
    # 兼容 list / tuple 多网卡绑定
    hosts = [bind_host] if isinstance(bind_host, str) else list(bind_host or [])

    # 显式拒绝 0.0.0.0 / :: / 空(它们意味着所有网卡)
    DANGEROUS = {"0.0.0.0", "::", "", None}
    if any(h in DANGEROUS for h in hosts):
        print("FATAL: DEV mode cannot bind to 0.0.0.0 / :: / empty host.")
        sys.exit(1)

    # 拒绝任何非 localhost
    if not all(h in _LOCALHOST_ADDRS for h in hosts):
        print(f"FATAL: DEV mode requires localhost binding, got {hosts}")
        sys.exit(1)

    # 容器化场景下补一层警告:即便绑了 127.0.0.1,容器网络配置可能仍暴露
    if os.path.exists("/.dockerenv") or os.environ.get("KUBERNETES_SERVICE_HOST"):
        print("WARNING: DEV mode in container. "
              "Verify your network config (port mapping / Service) does not expose this port.")
```

> DEV 模式唯一正确的用途：本地开发、单机调试。**永远不要**在非 localhost 上 DEV 模式运行。容器化场景下，即使绑了 `127.0.0.1`，也要保证 Docker/K8s 的网络配置不会把端口转发出去——这一层 guard 无法替你检查。生产部署必须显式配 `auth_mode: api_key` 或 `trusted`。

> **主干实现注记**（F01）：主干把这段拆成两半——`common.authentication.binding.check_dev_binding(hosts)`
> 是**纯函数**，非 localhost 抛 `ValidationError`，容器场景走 `logging.warning`；
> `sys.exit(1)` 与 stderr 上的 `FATAL:` 留在 `bootstrap/http_server/__main__.py:main`。
> 这样 guard 本身可被单测直接断言（`tests/unit/common/authentication/test_binding.py`），
> 而不必在测试里捕获 `SystemExit`。

#### 2.2.2 TRUSTED 模式

**语义**：信任上游网关（如 nginx、API Gateway）已经完成认证，**网关负责校验身份**，框架只读取网关注入的 header。

```python
if auth_mode == AuthMode.TRUSTED:
    # 身份声明由受信网关注入；user/agent 是同级主体，必须二选一
    org_id = request.headers.get("X-Org-Id")
    principal_type = request.headers.get("X-Principal-Type")
    principal_id = request.headers.get("X-Principal-Id")

    if not org_id or principal_type not in {"user", "agent"} or not principal_id:
        raise AuthenticationError("Invalid trusted principal")

    # role 不能从 header 读——必须查服务端注册表
    role = principal_role_store.get_role(org_id, principal_type, principal_id)

    # 如果框架也配置了 Root API Key，可以用来锁住“网关到框架”这一跳
    if configured_root_api_key:
        # 校验网关传下来的共享密钥
        gateway_key = extract_api_key(request)
        if not hmac.compare_digest(gateway_key, configured_root_api_key):
            raise AuthenticationError("Invalid gateway key")

    actor = Scope(org=org_id, **{principal_type: principal_id})
    return AuthContext(
        actor=actor,
        role=role,
    )
```

**关键设计**：role 不来自 header——header 说「你是谁」，框架自己要查「你能干什么」。这样即使网关被攻破或误配，也无法任意提权。

> **主干实现注记**（F01）：`TrustedAuthenticator` 查的 header 名一律是**小写常量**
> ——归一在 `bootstrap.core.auth_middleware.credentials_from_headers` 里做了一次
> （RFC 9110 §5.1，header 名大小写不敏感），authenticator 侧不再重复处理大小写。
> `principal_role_store.get_role` 对应主干的 `PrincipalKeyStore.get_role(actor)`：
> 参数是一个 `Scope` 而非三元组，与本仓 `Scope` 的实际形状对齐。
> 主体查不到时抛 `AuthenticationError`（不回落任何默认 role）。

#### 2.2.3 API_KEY 模式

**语义**：框架自己验证 API Key。Root API Key 比对成功后直接返回 ROOT 身份；普通主体的 API Key 查注册表。

```python
if auth_mode == AuthMode.API_KEY:
    api_key = extract_api_key(request)

    # Step 1: 先比 Root API Key
    if configured_root_api_key and hmac.compare_digest(api_key, configured_root_api_key):
        return AuthContext(actor=Scope(org="*"), role=Role.ROOT)

    # Step 2: 查主体注册表
    identity = key_manager.resolve(api_key)
    if identity is None:
        raise AuthenticationError("Invalid API key")

    return identity
```

> **主干实现注记**（F01）：`compare_digest` 在主干里两边都 `.encode("utf-8")`
> 成 **bytes** 再比。str 版本在参数含非 ASCII 字符时抛 `TypeError`，那会让一次
> 认证失败变成 500 而不是 401——把「凭据错误」暴露成「服务器错误」，
> 且绕过了统一的失败审计路径。见
> `src/common/authentication/authentication_impl/api_key_authenticator.py`。

### 2.3 API Key 系统

#### 2.3.1 Key 的存储

> **Demo 实现注记**：demo 的 `KeyManager` user key 已用 **Argon2id**（`time_cost=4, memory_cost=128MB, parallelism=2`，
> OWASP 2024+ 推荐）存储（防 GPU 暴力）。另存一个 **sha256 key_fp** 作确定性查找键 + OAuth token 绑定锚
> （Argon2 每次 salt 不同不能作键/锚）。Root API Key 不入注册表，单独 `compare_digest` 明文比对；它与第 5 章的 Encryption Root Key 无关。

```python
class PrincipalKeyStore:
    def __init__(self, api_key_hashing_enabled: bool):
        self.hashing_enabled = api_key_hashing_enabled

    def store_key(
        self,
        org_id: str,
        principal_type: str,
        principal_id: str,
        role: str,
    ) -> str:
        """生成 API Key，存进主体注册表，返回一次性明文。"""
        key = generate_api_key()

        # fingerprint 基于"明文 key"计算:既是确定性查找键,也是 OAuth token
        # 绑定锚(见 §2.3.1)。必须在 Argon2 哈希之前算--哈希后无法反推明文重算。
        key_fp = sha256(key)

        if self.hashing_enabled:
            # Argon2id 哈希
            from argon2 import PasswordHasher
            stored_value = PasswordHasher().hash(key)
            is_hashed = True
        else:
            stored_value = key
            is_hashed = False

        # 持久化到注册表（JSON 文件或 DB）
        principal_registry[org_id][principal_type][principal_id] = {
            "key": stored_value,
            "key_prefix": key[:8],
            "key_fingerprint": key_fp,
            "role": role,
            "is_hashed": is_hashed,
        }

        # 维护前缀索引:供 resolve_api_key(§2.3.2)按前 8 字符定位候选,
        # 避免全表扫描。索引只存定位三元组,记录本体仍在 principal_registry。
        prefix_index.setdefault(key[:8], []).append((org_id, principal_type, principal_id))

        # 返回明文 key——这是调用方唯一一次拿到它
        return key
```

> **主干实现注记**（F01）：主干把 `store_key` 拆成对外的
> `PrincipalKeyStore.issue(actor: Scope, role: Role) -> str` 与实现内部的前缀
> 索引维护——索引是**实现细节**，不该出现在跨实现的 ABC 上。另有三处收紧：
> `api_key_hashing_enabled` 开关**不提供**（缺 `argon2-cffi` 时在装配期抛
> `ValidationError`，绝不回落明文，铁律 #3）；`role=ROOT` 抛
> `PermissionDeniedError`（§3.2 禁止自签发 ROOT）；`actor` 必须且只能指定
> `user` 或 `agent` 之一。第一期唯一实现注册名为 **`memory`**（进程内），
> Argon2 是它的内部细节而非后端名。

**重要**：`api_key_hashing_enabled` 建议**默认开启**。Argon2id 推荐参数（2024+ 标准）：**`time_cost=4, memory_cost=128 * 1024 (128 MB), parallelism=2`**。这是当前 OWASP 推荐的最低值，适合 2026 年的硬件水准。金融、医疗等合规场景应进一步提高（`time_cost=6+`)。如果默认关闭，当加密层也关闭时，key 就是磁盘上的裸明文。

```python
# 创建 PasswordHasher 时显式指定参数（不要用库默认）
from argon2 import PasswordHasher

hasher = PasswordHasher(
    time_cost=4,      # 迭代次数
    memory_cost=131072,   # 128 MB(KiB 单位)
    parallelism=2,
    hash_len=32,
    salt_len=16,
)
```

**Key 的哈希方式**：
```python
import hmac
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

# 明文存储时:用 HMAC 常时间比对
def verify_plain_key(stored_key: str, provided_key: str) -> bool:
    return hmac.compare_digest(stored_key, provided_key)

# 哈希存储时:Argon2 自身常时间
def verify_hashed_key(stored_hash: str, provided_key: str) -> bool:
    try:
        return PasswordHasher().verify(stored_hash, provided_key)
    except VerifyMismatchError:
        # 提供的 key 与存储哈希不匹配 -> 正常的"密码错误"路径
        return False
    except (InvalidHashError, VerificationError):
        # 存储的哈希格式损坏或参数不符 -> 视为校验失败,fail-closed
        return False
```

**防 timing attack 的完整校验流程**：
```python
def resolve_api_key(api_key: str) -> AuthContext | None:
    # 1. 比 Root API Key（常时间）
    if configured_root_api_key and hmac.compare_digest(api_key, configured_root_api_key):
        return AuthContext(actor=Scope(org="*"), role=Role.ROOT)

    # 2. 前缀索引(dict 直查,非 timing-sensitive)
    #    prefix_index: key_prefix(前8字符) -> list[(org, principal_type, principal_id)]
    #    候选只存定位三元组,真正的记录回 principal_registry 取(见 §2.3.2 store_key)。
    key_prefix = api_key[:8]
    candidates = prefix_index.get(key_prefix, [])

    # 3. 逐个候选常时间比对
    for org, p_type, p_id in candidates:
        record = principal_registry[org][p_type][p_id]
        stored_key = record["key"]
        if record["is_hashed"]:
            matched = verify_hashed_key(stored_key, api_key)
        else:
            # HMAC 常时间
            matched = hmac.compare_digest(stored_key, api_key)
        if matched:
            actor = Scope(org=org, **{p_type: p_id})
            return AuthContext(
                actor=actor,
                role=record["role"],
                acting_user=p_id if p_type == "user" else "",
            )

    return None
```

**已知缝隙**：前缀索引定位（`prefix_index.get(key_prefix)`)本身不是常时间的，前 8 字符是否命中可能被 timing 区分。但这只泄露「是否存在某前缀的 key」，结合 key 本身的熵足够高（每 key 独立），实际风险有限。

**进一步加固**（必要时）——保证「前缀不命中」和「前缀命中但 key 错」走相同时间路径：

```python
# 缓解:任何未命中都补一次 dummy Argon2 verify,把时间 pad 到上界
_DUMMY_HASH = PasswordHasher(time_cost=4, memory_cost=131072, parallelism=2).hash(
    "dummy-key-not-used-anywhere"
)

def resolve_api_key_constant_time(api_key: str) -> AuthContext | None:
    # ...前面 Root API Key 和前缀查询不变...

    if not candidates:
        # 即使没候选,也跑一次 Argon2 verify(吃满时间)
        try:
            PasswordHasher().verify(_DUMMY_HASH, api_key)
        except Exception:
            pass
        return None

    # 正常候选比对
    ...
```

适合「被合规审计」或威胁模型包含 timing 侧信道的场景。常规部署的风险/收益比不高（因为单次 Argon2 verify 已把响应时间拉到 ~10ms 级，本身就模糊了 prefix 命中差异），按需启用。

API Key 的生成、分发、轮换与撤销统一见第 6 章。本章只约束认证侧行为：明文 key 仅在签发时返回一次，服务端随后只保存验证所需材料；轮换或撤销后旧 key 必须立即失效，并级联处理其授权的 OAuth token。

### 2.4 OAuth 2.1 集成

如果需要支持 AI Agent 通过 OAuth 接入，OAuth 作为一套完整的授权协议，设计上需要明确以下机制：

#### 2.4.1 OAuth 的基本设计

- 框架自己充当 Authorization Server(IdP)，不是 OAuth client。
- Token 为**不透明随机串**，用 SHA-256 哈希索引，不在 token 里嵌入 payload。
- Token 类型与生命周期：Access Token（短期，如 1h）、Refresh Token（长期，如 30d）、Authorization Code（极短，如 5min）。

#### 2.4.2 Token 与 API Key 的绑定

每个 OAuth token 签发时必须记录授权它的 Principal API Key 的 SHA-256 fingerprint。Agent 代 user 时，`actor` 是 agent、`acting_user` 是授权用户，fingerprint 绑定该 user 的 API Key；这是可撤销委托关系，不是层级从属。每次 token 校验时重算绑定主体当前 key 的 fingerprint，两者不匹配则拒绝：

```python
class OAuthToken:
    token_hash: str      # sha256(token_value)
    actor: Scope         # 已认证执行者：user 或 agent
    acting_user: str     # agent 代 user 时填写，否则为空
    authorizing_principal: Scope
    role: str
    authorizing_key_fp: str  # 签发时记录的 key 指纹
    expires_at: int
    revoked: bool = False

def verify_bearer_token(token_value: str) -> AuthContext | None:
    token_hash = sha256(token_value)
    token = token_store.get(token_hash)

    if token is None or token.expires_at < now() or token.revoked:
        return None

    # 关键：校验绑定 Principal API Key 的 fingerprint 是否匹配
    # principal_registry 按 [org][principal_type][principal_id] 三层嵌套存储
    # (见 §2.3.2 store_key),不能拿 Scope 对象直接 .get()。
    ap = token.authorizing_principal
    if ap.user:
        p_type, p_id = "user", ap.user
    elif ap.agent:
        p_type, p_id = "agent", ap.agent
    else:
        return None  # token 绑定的主体缺失,拒绝
    principal = principal_registry.get(ap.org, {}).get(p_type, {}).get(p_id)
    if principal is None:
        return None

    current_key_fp = principal["key_fingerprint"]
    if not hmac.compare_digest(current_key_fp, token.authorizing_key_fp):
        # key 被轮换过 → 该 token 已失效
        token.revoked = True
        token_store.persist(token)
        return None

    # role-downgrade 检测:token 嵌入 role 不能高于当前 role
    current_role = principal_role_store.get(token.actor)
    if ROLE_RANK[token.role] > ROLE_RANK[current_role]:
        return None  # 降级前的 stale token

    return AuthContext(
        actor=token.actor,
        acting_user=token.acting_user,
        role=token.role,
        from_oauth=True,
        authorizing_key_fp=token.authorizing_key_fp,
    )
```

#### 2.4.3 Refresh Token Rotation 与重放检测

```python
async def exchange_refresh_token(old_refresh: str) -> tuple[str, str]:
    """消费旧 refresh token,发放新的 access + refresh pair。"""
    hash_ = sha256(old_refresh)
    record = token_store.get_refresh(hash_)

    if record is None or record.expires_at < now():
        raise InvalidTokenError("Refresh token expired or not found")

    if record.consumed:
        # 注意：重放！按 RFC 9700 §4.14 撤销整个家族
        token_store.revoke_family(record.family_id)
        raise InvalidTokenError("Refresh token replayed, family revoked")

    # 先发放新的 access + refresh pair,才能确定旧 token 被谁替换
    new_access = issue_access_token(record)
    new_refresh = issue_refresh_token(record)

    # 标记旧 token 为已消费,并记录被哪个新 token 替换(支撑家族可追溯)
    record.consumed = True
    record.replaced_by = sha256(new_refresh)
    token_store.persist(record)

    return new_access, new_refresh
```

#### 2.4.4 特权提升闸门

通过 OAuth token 认证的请求，**不能再用自己的身份去签发新的 OAuth 状态**（防止偷来的短期 token 洗成长期 refresh chain）：

```python
if ctx.from_oauth:
    raise PermissionDeniedError(
        "OAuth-minted credentials cannot issue new OAuth state"
    )
```

### 2.5 MCP / 工具协议接入

如果框架以 MCP 等工具协议暴露接口给 AI Agent：

```python
# ASGI 中间件模式：复用同一个认证分流器，统一产出 AuthContext
async def mcp_auth_middleware(scope, receive, send):
    try:
        ctx = await auth_dispatcher.authenticate(extract_headers(scope))
    except AuthenticationError:
        # 返回 JSON-RPC error
        await send_json_rpc_error(scope, send, 401, "Unauthorized")
        return

    # AuthContext 通过 ContextVar 传播，请求结束必须 reset
    token = _ctx_var.set(ctx)
    try:
        await handle_mcp_request(scope, receive, send)
    finally:
        _ctx_var.reset(token)


# 每个工具函数取 ctx
_ctx_var: ContextVar[AuthContext | None] = ContextVar("mcp_auth_ctx", default=None)

def get_ctx() -> AuthContext:
    ctx = _ctx_var.get()
    if ctx is None:
        raise UnauthenticatedError("No authenticated context")
    return ctx
```

401 响应时，应返回 `WWW-Authenticate: Bearer resource_metadata=...`(RFC 9728)，让 MCP client 自动发现授权服务器。

---

## 3. 授权（Authorization）

> **关联设计文档**：本框架的授权以 `org > user = agent > session` scope 模型为载体——user 与 agent 是同级主体，检索/写入默认限制在各自主体 scope 内，跨主体访问需显式授权。授权检查在接口层以 `identity`（调用方）与 `scope`（目标）分离的形式执行，见 [`design/architecture.md` §3.2 作用域与多租户](../../design/architecture.md) 与 [`design/architecture.md` §9 记忆接口层](../../design/architecture.md)。

### 3.1 角色模型

框架应提供三级角色，从最少权限开始：

> **Demo 实现注记**：demo 三档角色为 **user / org_admin / ROOT**（对应指南 USER/ADMIN/ROOT）。
> demo 的 org_admin 比指南的 ADMIN 更细：绑具体 org、**org 首个 user 自动成为 admin**（引导）、可自治提拔/降级本 org
> admin（对称）、受**最后一个 admin 保护**、永不能签 ROOT、走 api_key 不进数据面。
> `agent-memory` mem2.0 的 `permission_impl/` 已有两个实现：`AllowAllPermissionManager`（全放行，测试用）和
> `SQLitePermissionManager`（SQLite ACL：grant 持久化 + revoke 软撤销 `revoked_at` + owner scope covers + 跨 org 拒 + grants 表查询）。
> demo 的 `DemoPermissionManager` 多一层 `acting_user`（agent 经 user 授权代其操作时从 ContextVar 取）。这是同级主体间的委托关系，不是 agent 从属于 user；该信息计划通过 AuthContext 侧车与 SQLitePermissionManager 协作。
> 使用 Factory/Producer 注册模式：`@PermissionProducer.register("sqlite")` 自注册，装配时 `PermissionProducer.dep(root, default="sqlite")` 取实例。

```python
class Role(str, Enum):
    USER = "user"      # 普通用户,只能在自己租户内操作
    ADMIN = "admin"    # 管理员,可管理本租户内用户
    ROOT = "root"      # 超级管理员,可跨租户操作
```

**ROLE_RANK**（用于 role-downgrade 检测）：
```python
ROLE_RANK = {
    Role.USER: 0,
    Role.ADMIN: 1,
    Role.ROOT: 2,
}
```

### 3.2 权限清单

| 操作门 | USER | ADMIN | ROOT |
|---|---|---|---|
| 读/写自己的主体 scope | ✅ | ✅ | ✅ |
| 管理本租户 user/agent（非提权） | ❌ | ✅ | ✅ |
| 创建/删除租户 | ❌ | ❌ | ✅ |
| **跨租户修改权限** | ❌ | ❌ | ✅ |
| 系统级配置修改（reindex 等） | ❌ | ❌ | ✅ |
| **提升任意用户 role** | ❌ | ❌ | ✅ |

**明确禁止**：普通主体注册/创建入口**不能创建 ROOT 主体**。ROOT 只能由已有 ROOT 通过专门的 role 变更接口提升：

```python
async def register_principal(org_id, principal_type, principal_id, role):
    if role == "root":
        raise PermissionDeniedError(
            "Cannot create ROOT principals via registration endpoint; "
            "use dedicated set_role endpoint"
        )
    # ... 正常创建 user/agent/admin
```

### 3.3 权限检查点

#### 3.3.1 装饰器模式

```python
from functools import wraps


def require_role(*allowed_roles: Role):
    """装饰器:要求请求携带最少角色。"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = get_auth_context()  # 从当前可信认证上下文取
            if ctx.role not in allowed_roles:
                raise PermissionDeniedError(
                    f"Requires one of {allowed_roles}, got {ctx.role}"
                )
            return await func(*args, **kwargs)
        return wrapper
    return decorator
```

#### 3.3.2 读级 vs 写级检查

建议分开两种检查，因为读和写的授权策略可能不同（比如 temp 目录非 ROOT 只读）：

```python
def ensure_access(target: Scope, ctx: AuthContext):
    """读级检查：验证 target 对 ctx 是否可访问。"""
    if not is_accessible(target, ctx):
        raise PermissionDeniedError(f"Access denied to {target}")


def ensure_mutable_access(target: Scope, ctx: AuthContext):
    """写级检查：先做可读检查，再校验 WRITE 动作。"""
    ensure_access(target, ctx)
    if not permission_manager.check(ctx.actor, target, Action.WRITE):
        raise PermissionDeniedError(f"Write denied to {target}")
```

#### 3.3.3 路径穿越防护

```python
def normalize_uri_parts(uri: str) -> str:
    """规范路径并拒绝穿越,返回以 '/' 连接的相对路径片段。"""
    segments = uri.split("/")

    # 拒绝:
    for seg in segments:
        if seg in (".", ".."):
            raise PermissionDeniedError(f"Path traversal denied: {uri}")

    # 拒绝反斜杠(Windows 兼容)
    if "\\" in uri:
        raise PermissionDeniedError(f"Backslash denied: {uri}")

    # 拒绝盘符前缀
    if re.search(r'^[A-Za-z]:', uri):
        raise PermissionDeniedError(f"Drive letter denied: {uri}")

    return "/".join(segments)
```

### 3.4 ROOT 的特殊约束

> **Demo 实现注记**：demo v2 数据面 **bearer-only**，ROOT（走 Root API Key）**根本不进数据面**——
> 故本节「ROOT 数据 API 必须显式指定 org、principal_type、principal_id」的 guard 在 demo **不触发**。ROOT 只用于管理面
> (`/admin/*`、`/v1/audit`)。数据面身份由 agent 的 OAuth token 携带（agent+user 双因子）；agent 与 user 仍是同级主体，`acting_user` 表达用户授权后的代操作目标。
> 若未来放开 ROOT 数据面，需补此 guard。

ROOT 因为没有「所属租户」，访问**数据 API 时必须显式指定**当前要操作的 org、主体类型（user/agent）和主体 ID，避免误落到默认主体：

```python
if ctx.role == Role.ROOT and not ctx.from_oauth:
    # ROOT 必须显式指定 org 与同级主体分支
    if not explicit_org or principal_type not in {"user", "agent"} or not principal_id:
        raise InvalidArgumentError(
            "ROOT must specify org, principal_type and principal_id for data APIs"
        )
```

**例外**：OAuth 方式认证的 ROOT 已经由 token claim 绑定了身份，跳过此约束。

**豁免列表**：部分系统 API（健康检查、admin 管理）可以免：
```python
ROOT_IMPLICIT_ALLOWED = {
    "/api/v1/system/status",
    "/api/v1/system/wait",
    "/api/v1/debug/health",
}
ROOT_IMPLICIT_ALLOWED_PREFIXES = ("/api/v1/admin", "/api/v1/observer")
```

### 3.5 角色提升路径 bootstrap

ROOT 的产生不通过 API——而是通过配置声明：

```
T=0: 部署者在配置文件中声明 Root API Key
      (framework.conf / env var / K8s secret)
        │
T=1: 启动时 server 加载 Root API Key
        │
T=2: 持有 Root API Key 的人调 API 建 org、user 或 agent
        │
T=3: ROOT 通过 PUT .../role 可把某个 user 或 agent 提升为 ROOT
      产生"被提升的 ROOT"(与"声明式 ROOT"在权限检查中等价)
```

**所以 ROOT 有两个来源**：
- **声明式 ROOT**（配置层）：来自 Root API Key，是系统的起始 ROOT。
- **提升式 ROOT**（API 层）：来自 ROOT 调 `PUT .../role`，归属具体 org 和主体。

两者在运行时权限检查中等价，但只有声明式 ROOT 可以「无中生有」。

---

## 4. 多租户隔离（Isolation）

> **关联设计文档**：本框架的路径前缀注入是项目 scope 模型的存储层落地。scope 层级为 `org > space > user/agent > session`：`space` 是 org 下的逻辑隔离单元；`user` 与 `agent` 在 space 内的归属顺序由 `principal_path` 决定。跨主体或跨 space 访问必须经显式授权。详见 [`design/architecture.md` §3.2 作用域与多租户](../../design/architecture.md)。

### 4.1 设计的核心：身份注入路径前缀

`user` 与 `agent` 是 space 内的记忆主体：用户自己的记忆落 `Scope(org, space, user)`，Agent 自身记忆落 `Scope(org, space, agent)`。默认 `principal_path=user_agent` 时 user 是 agent 的上级主体；`principal_path=agent_user` 时 agent 是 user 的上级主体。`session` 只能挂在已确定的主体路径下。

Agent 经用户授权代其操作记忆时，委托关系由认证产物表达：`AuthContext.actor = Scope(org, space, agent)`、`AuthContext.acting_user = user`，目标仍是 `Scope(org, space, user)`。这里的“代 user”是授权关系，不代表 agent 必然从属于 user，也不把 agent 写入用户记忆的目标 scope。

> **Demo 实现注记**：参考 demo 将操作方 `actor` 与目标记忆归属分开：OAuth token 同时证明 agent 身份和用户授权，`AuthContext` 保存 `actor` 与 `acting_user`，PEP 校验后只把 user target scope 下沉到 Engine/Store。

这是**最关键的隔离设计**，必须在第一天就做对。

URI 不携带可由调用方伪造的主体身份。路径前缀由已认证的 actor、已授权的 target scope 和服务端规则共同确定：

```python
def scope_namespace(scope: Scope) -> str:
    """把同级 user/agent 主体映射为互斥的物理路径分支。"""
    if scope.user and scope.agent:
        raise ValidationError("user and agent are peer principals; choose one owner")
    if scope.user:
        owner = f"user/{scope.user}"
    elif scope.agent:
        owner = f"agent/{scope.agent}"
    else:
        owner = "_org"
    if scope.session:
        if owner == "_org":
            raise ValidationError("session requires a user or agent owner")
        owner = f"{owner}/session/{scope.session}"
    return f"/local/{scope.org}/{scope.space}/{owner}"
```

结果：
```
Scope(org="acme", space="product", user="u1")
  → /local/acme/product/user/u1

Scope(org="acme", space="product", agent="a1")
  → /local/acme/product/agent/a1

Scope(org="acme", space="product", user="u1", session="s1")
  → /local/acme/product/user/u1/session/s1
```

相同名称的 user 与 agent 仍落到不同分支；不同 org 也落到不同租户子树。路径结构本身不授予访问权，真正的授权决定必须在 PEP 完成。

### 4.2 URI Scope 设计

框架应定义清晰的顶层 scope 集合：

```python
# 对外暴露的 scope
PUBLIC_SCOPES = ["user", "agent", "resources"]

# 内部 scope（用户不能直接操作）
INTERNAL_SCOPES = ["temp", "queue", "upload", "_system"]
```

- **user**：用户自身的记忆数据；
- **agent**：Agent 自身的经验与状态；
- **resources**：org 内导入的共享知识资源；
- **session**：不是独立顶层主体，必须位于某个 user 或 agent 分支下；
- **temp**：临时存储（非 ROOT 只能读写自己的子路径）；
- **upload**：上传暂存区（内部使用）；
- **_system**：系统配置文件（ls 时隐藏，普通主体自动拒绝）。

每个 scope 都应注册自己的访问规则：
```python
def is_accessible(target: Scope, ctx: AuthContext) -> bool:
    # ROOT 什么都能看
    if ctx.is_root:
        return True

    # org 是硬边界
    if target.org != ctx.actor.org:
        return False

    # user/agent 只能直接访问自己的同级主体分支
    if ctx.actor.user and target.user == ctx.actor.user and not target.agent:
        return True
    if ctx.actor.agent and target.agent == ctx.actor.agent and not target.user:
        return True

    # agent 代 user：acting_user 来自用户授权，不来自请求参数
    if ctx.actor.agent and ctx.acting_user and target.user == ctx.acting_user:
        return True

    return permission_manager.check(ctx.actor, target, Action.READ)
```

### 4.3 同级主体之间的授权

`user` 与 `agent` 之间不存在隐式上下级权限。允许跨主体访问的方式只有：

1. **用户授权 Agent 代操作**：OAuth/授权流程把 user 与 agent 同时绑定到 token，认证层产出 `AuthContext(actor=agent, acting_user=user)`；PEP 只允许目标为该 `acting_user`。
2. **显式 Grant**：user↔user、agent↔agent 或 user↔agent 的其他共享均走 `PermissionManager.grant`，不通过路径嵌套表达。
3. **管理员权限**：org_admin 只能管理本 org，ROOT 才能跨 org；管理员身份也不能由请求参数自报。

授权撤销后，后续请求必须 fail-closed。对于 OAuth 委托，撤销用户授权、轮换绑定的 API Key 或撤销 token family 都必须使 Agent 代操作立即失效。

### 4.4 WRITE 路径的隔离检查

写入操作必须检查当前主体是否对目标 scope 有写权限。与读路径不同，写路径还要确认目标只属于一个同级主体，并确保最终物理路径由已鉴权的 target scope 生成。

```python
def ensure_mutable_access(target: Scope, ctx: AuthContext) -> None:
    if target.user and target.agent:
        raise ValidationError("target cannot contain both user and agent")
    if permission_manager.check(ctx.actor, target, Action.WRITE):
        return

    # agent 代 user 的补充授权仍以服务端 AuthContext 为准
    delegated = bool(
        ctx.actor.agent
        and ctx.acting_user
        and target.user == ctx.acting_user
        and target.org == ctx.actor.org
    )
    if not delegated:
        raise PermissionDeniedError(f"write denied to {target}")
```

**调用点**（所有写操作必须经过）：
```python
@require_authenticated
async def write_file(uri: str, target: Scope, data: bytes, ctx: AuthContext):
    ensure_mutable_access(target, ctx)
    normalized = normalize_uri_parts(uri)  # 防止 ../ 出界
    path = f"{scope_namespace(target)}/{normalized}"

    # 可选加密
    if encryptor:
        data = await encryptor.encrypt(target.org, data)

    return await raw_fs.write(path, data)
```

### 4.5 ROOT 跨租户操作

ROOT 做跨租户检索时返回空根目录列表，走全局搜索。其他主体默认只搜索自己的同级主体分支；Agent 代 user 时，可以额外加入已授权 user 的分支，但这不改变二者的同级关系：

```python
def get_search_roots(context_type, ctx):
    """根据 context_type 返回检索起点 URI 列表。"""
    if not ctx or ctx.is_root:
        return []   # ROOT 走全局搜索

    roots = [scope_namespace(ctx.actor)]
    if ctx.actor.agent and ctx.acting_user:
        delegated_user = Scope(org=ctx.actor.org, user=ctx.acting_user)
        roots.append(scope_namespace(delegated_user))

    if context_type == "resource":
        return ["mem://resources"]
    return [f"{root}/memories" for root in roots]
```

---

## 5. 数据加密（Encryption at Rest）

> **关联设计文档**：存储加密是「端侧数据不出端、传输/存储加密」原则的落地。端云协同场景下，热/私有记忆留端、冷/共享上云，选择性同步需加密传输——见 [`design/vision.md` §4 支柱四 端云协同](../../design/vision.md) 与 [`design/architecture.md` §11 部署架构](../../design/architecture.md)。可插拔存储后端（SQLite/PostgreSQL/Milvus 等）的加密生效边界见 [`design/architecture.md` §5.2 存储抽象](../../design/architecture.md)。
>
> **Demo 实现注记**：`security_demo` 已实现 ENC1 信封加密（crypto/ 模块：BlobEncryptor + LocalRootKeyProvider + ENC1 envelope，AES-256-GCM + HKDF-by-org，AAD 绑定 scope，fail-closed）。接缝在 EncryptedFSStore（装饰 FSStore，对 Engine 透明）。`agent-memory` mem2.0 的存储层用 KVStore（非 FSStore），加密移植时改为 EncryptedKVStore（装饰 KVStore，同构）。加密模块通过 store_factory 接缝注入（见可移植模块 ClawAegis/memory-server）。外部加密模块可不依赖 demo 的 crypto/，自行实现 KVStore 装饰器。

### 5.1 三级信封加密 + AES-256-GCM

推荐方案：三级密钥树 + AES-GCM 认证加密。

```
Encryption Root Key (加密顶层主密钥,由 Key Provider 托管)
   │  HKDF-SHA256(encryption_root_key, salt, info=org_id)
   ▼
Org Key (组织级派生密钥)
   │  AES-256-GCM 加密
   ▼
File Key (每个文件随机 32 字节)
   │  AES-256-GCM 加密文件内容
   ▼
密文
```

#### 为什么三级

| 级 | 目的 |
|---|---|
| **Encryption Root Key** | 加密顶层主密钥，静态数据的机密性依赖于它。生产环境由 KMS/Vault 保护；需要派生 Org Key 时，仅加密子系统可在受控内存中使用明文。它不是认证用的 Root API Key。 |
| **Org Key** | 从 Encryption Root Key 经 HKDF 为每个 org 派生一把。一个 org 的 key 泄露不影响其他 org。 |
| **File Key** | 每个文件一把随机密钥。加密的内容独一无二，破解一个不影响其他文件。 |

这一层层的相互隔离确保：某 org 的 Org Key 泄露，依然无法解析其他 org 的加密数据；丢失某项 File Key，影响范围仅限于该文件本身。

#### 信封格式

```
Magic "ENC1"(4B) | Version(1B) | Provider(1B) |
EFK长度(2B) | KeyIV长度(2B) | DataIV长度(2B) |         ← 12B 定长头
加密的FileKey | KeyIV | DataIV | 加密内容              ← 变长体
```

密文自描述——头里记录 provider 类型，解密时按头里的 provider 类型走对应路径。

> **实现注记（主干与本节的偏离）**：信封实现在
> `src/common/encryption/encryption_impl/local_envelope.py`，头是
> **11 字节**（`!4sBBHHH`），比下方代码块里的 `HEADER_SIZE = 12` 少一字节——
> `4+1+1+2+2+2 = 11`，12 是把 struct 的对齐算进去了。字段构成与本节一致。
>
> **缺口：没有 `key_id`**。密文无法自述「我是用哪把根密钥加密的」，因此轮换根
> 密钥后所有历史密文立刻不可解——只能停机全量重加密或双写。补法是在头里加一个
> `KeyIdLen(1B)` + 变长体最前面一段 `key_id`，轮换即退化成一次配置文件编辑
> （keyring 保留旧 key、`current_key_id` 指向新 key）。这是信封格式的改动，属于
> `common/encryption/` 的面，记在
> [storage/F02 已知遗留](../storage/F02-encrypted-storage.md)。

```python
ENVELOPE_MAGIC = b"ENC1"
VERSION = 0x01
HEADER_SIZE = 12  # 4 + 1 + 1 + 2 + 2 + 2

def build_envelope(
    provider_id: int,
    encrypted_file_key: bytes,
    key_iv: bytes,
    data_iv: bytes,
    encrypted_content: bytes,
) -> bytes:
    header = struct.pack(
        "!4sBBHHH",
        ENVELOPE_MAGIC,
        VERSION,
        provider_id,
        len(encrypted_file_key),
        len(key_iv),
        len(data_iv),
    )
    return header + encrypted_file_key + key_iv + data_iv + encrypted_content


def parse_envelope(ciphertext: bytes) -> dict:
    if len(ciphertext) < HEADER_SIZE:
        raise CorruptedCiphertextError("Envelope too short")

    magic, version, provider_id, efk_len, kiv_len, div_len = \
        struct.unpack("!4sBBHHH", ciphertext[:HEADER_SIZE])

    if magic != ENVELOPE_MAGIC:
        raise InvalidMagicError(f"Invalid magic: {magic}")
    if version != VERSION:
        raise CorruptedCiphertextError(f"Unsupported version: {version}")

    offset = HEADER_SIZE
    efk = ciphertext[offset:offset + efk_len]
    kiv = ciphertext[offset + efk_len:offset + efk_len + kiv_len]
    div = ciphertext[offset + efk_len + kiv_len:offset + efk_len + kiv_len + div_len]
    encrypted = ciphertext[offset + efk_len + kiv_len + div_len:]

    return {
        "provider_id": provider_id,
        "encrypted_file_key": efk,
        "key_iv": kiv,
        "data_iv": div,
        "encrypted_content": encrypted,
    }
```

#### 加解密流程

```python
class FileEncryptor:
    def __init__(self, key_provider: "KeyProvider"):
        self.provider = key_provider

    async def encrypt(self, org_id: str, plaintext: bytes) -> bytes:
        # 不变量：每次 encrypt 都必须生成全新的 file_key 和 data_iv。
        # AES-GCM 在同一密钥下 IV 复用会导致灾难性安全失败(可恢复明文)。
        # 即使 IV 是 12 字节随机,同一 key 下也只能用 ~2^32 次(生日界限)。
        # 我们的方案是"每文件一对全新 (key, iv)",所以这个不变量本质上要求:
        # 绝对不要为了"性能"缓存或复用 file_key —— 一次性使用是安全前提。
        file_key = secrets.token_bytes(32)       # 每文件随机密钥(不缓存!)
        data_iv = secrets.token_bytes(12)         # 随机 IV(不复用!)

        # 用 AES-GCM 加密文件内容
        encrypted = await aes_gcm_encrypt(file_key, data_iv, plaintext)

        # 用 Key Provider 加密 file key
        encrypted_file_key, key_iv = await self.provider.encrypt_key(
            file_key, org_id
        )

        return build_envelope(
            self.provider.provider_id,
            encrypted_file_key, key_iv, data_iv, encrypted,
        )

    async def decrypt(self, org_id: str, ciphertext: bytes) -> bytes:
        # 明文文件兼容:不是 ENC1 魔数的直接返回
        if not ciphertext.startswith(ENVELOPE_MAGIC):
            return ciphertext

        parsed = parse_envelope(ciphertext)

        file_key = await self.provider.decrypt_key(
            parsed["encrypted_file_key"], parsed["key_iv"], org_id
        )

        try:
            return await aes_gcm_decrypt(file_key, parsed["data_iv"], parsed["encrypted_content"])
        except AuthenticationFailedError:
            # 数据被篡改,GMAC 校验失败
            raise
```

#### 兼容明文

加密功能的开关应当做到：

- 开加密后新写的内容都是 `ENC1` 信封；
- 老的未加密文件在读取时**可按明文正常处理**——通过判断魔数 `ENC1` 头部实现兼容；
- 加密/未加密的文件可以共存，随时开关。

```python
async def decrypt(self, org_id: str, raw: bytes) -> bytes:
    if not raw.startswith(ENVELOPE_MAGIC):
        # 未加密的文件,直接返回
        return raw

    # 走正常解密逻辑
    ...
```

> **实现注记（主干把它做成了开关，且落在 provider 上）**：
> `LocalEnvelopeEncryptionProvider` 有 `allow_plaintext` 参数（默认 `True`，即本节
> 描述的行为）。加个开关的理由是这条兼容规则在两个部署阶段的正确答案相反：
>
> - **迁移期**必须宽松。加密层上线时，库里全是加密前写的明文；一律拒绝就等于
>   上线即全量不可读。
> - **迁移完成后必须收紧**。此时「读到明文」只可能意味着有人绕过了加密层直接写
>   底层存储，或者配置被改坏了。宽松模式下这两种情况都会被静默放行——而这正是
>   降级攻击的着力点：攻击者只要能往底层写明文，就能让读路径完全跳过解密。
>
> 开关只有 provider 上这一个，两个存储装饰器（KV / FS）都不重复提供同语义旋钮
> ——两个开关意味着两处配置、两种组合，其中「装饰器宽松 + provider 严格」这类
> 组合没有任何意义，只会在排查时多一个要查的地方。
>
> 无论开关如何，**写路径永远加密**
> （`test_encrypted_fs_store_write_always_encrypts_even_when_plaintext_allowed`）。
> 开关若顺带放松了写，迁移期写进去的数据会永远是明文而调用方毫无察觉。

### 5.2 Key Provider 抽象

框架应通过 Key Provider 这个策略接口来解耦上层的加密逻辑与底层的密钥托管方式：

```python
class KeyProvider(ABC):
    """Key Provider 抽象.所有密钥操作通过此接口访问。"""

    @property
    @abstractmethod
    def provider_id(self) -> int:
        """返回 provider 类型编号,写入信封头。"""
        ...

    @abstractmethod
    async def derive_org_key(self, org_id: str) -> bytes:
        """为指定组织派生 Org Key。"""
        ...

    @abstractmethod
    async def encrypt_key(self, plaintext: bytes, org_id: str) -> tuple[bytes, bytes]:
        """加密 File Key。返回(密文, IV)。"""
        ...

    @abstractmethod
    async def decrypt_key(self, ciphertext: bytes, iv: bytes, org_id: str) -> bytes:
        """解密 File Key。"""
        ...

    @abstractmethod
    async def get_encryption_root_key(self) -> bytes:
        """获取 Encryption Root Key；远程 provider 必须在受控边界内实现。"""
        ...
```

> **实现注记（主干与本节的偏离）**：主干没有独立的 `KeyProvider` 顶层抽象——
> 对外的策略接口是 `common.encryption.EncryptionProvider`
> （`encrypt(plaintext, *, context, aad)` / `decrypt(...)` / `health()`），密钥托管
> 方式是它的实现细节（`LocalEnvelopeEncryptionProvider` 内部持有一个
> `LocalKeyProvider` 做 HKDF 派生与 data key 包装）。两处具体偏离：
>
> 1. **接口是同步的，不是 `async def`**。`KVStore` / `FSStore` 的方法全是同步的
>    （`get(scope, key) -> bytes`）。异步 provider 会逼着同步的 `get` 内部调
>    `asyncio.run(...)`，而这在一个已有事件循环的进程里直接抛
>    `RuntimeError: asyncio.run() cannot be called from a running event loop`——
>    也就是说，在真实的 ASGI 部署下必炸。要么整个存储层改异步（远超本期范围），
>    要么 provider 同步。选后者。远程 provider（Vault/KMS）用同步 HTTP 客户端实现，
>    这是它们的库都支持的形态。
> 2. **`get_encryption_root_key()` 不在对外接口上**。`EncryptionProvider` 只暴露
>    `encrypt` / `decrypt` / `health`，根密钥不跨接口边界。这是收紧不是缺失：把根
>    密钥交出接口边界，就等于要求每个调用方都正确处理它的生命周期（不落日志、
>    不进异常、用完清零）——而 KMS/HSM 类 provider **根本交不出来**，根密钥永远
>    不离开硬件。（`LocalKeyProvider` 上还有这个方法，但那是实现内部的类，不是
>    存储层能看到的接口。）

#### 5.2.1 LocalProvider（本地开发/单机）

Encryption Root Key 存在本地文件（hex 32 字节，+ 0600 权限）：

```python
class LocalProvider(KeyProvider):
    provider_id = 0x01

    def __init__(self, key_file: str):
        self.key_file = Path(key_file).expanduser()
        self._encryption_root_key: bytes | None = None

    async def get_encryption_root_key(self) -> bytes:
        if self._encryption_root_key:
            return self._encryption_root_key
        self._encryption_root_key = await self._load_or_create()
        return self._encryption_root_key

    async def _load_or_create(self) -> bytes:
        if self.key_file.exists():
            hex_key = self.key_file.read_text().strip()
            return bytes.fromhex(hex_key)

        encryption_root_key = secrets.token_bytes(32)
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        self.key_file.write_text(encryption_root_key.hex())
        os.chmod(self.key_file, 0o600)  # 仅所有者可读写
        return encryption_root_key

    async def derive_org_key(self, org_id: str) -> bytes:
        encryption_root_key = await self.get_encryption_root_key()
        return await self._hkdf_derive(encryption_root_key, org_id)

    async def _hkdf_derive(self, encryption_root_key: bytes, org_id: str) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"framework-kek-salt-v1",
            info=b"framework:kek:v1:" + org_id.encode(),
        )
        return hkdf.derive(encryption_root_key)

    async def encrypt_key(self, plaintext: bytes, org_id: str) -> tuple[bytes, bytes]:
        # 先用 Org Key 加密 File Key
        org_key = await self.derive_org_key(org_id)
        iv = secrets.token_bytes(12)
        encrypted = await aes_gcm_encrypt(org_key, iv, plaintext)
        return encrypted, iv

    async def decrypt_key(self, ciphertext: bytes, iv: bytes, org_id: str) -> bytes:
        org_key = await self.derive_org_key(org_id)
        return await aes_gcm_decrypt(org_key, iv, ciphertext)
```

**安全性**：LocalProvider 的 Encryption Root Key 以明文存在磁盘（仅靠文件权限 0600）。**只适合开发/单机**，生产环境应使用 Vault 或云厂商 KMS。

#### 5.2.2 VaultProvider（HashiCorp Vault）

使用 Vault 的 transit secrets engine 保护 Encryption Root Key：Vault 内部的 KEK 永不导出，只执行 encrypt/decrypt；Encryption Root Key 以 KEK 加密后存入 KV，启动时解密到应用内存。

```python
class VaultProvider(KeyProvider):
    """
    用 Vault transit engine 管理 Encryption Root Key。
    - transit 引擎储存一个永不导出的 KEK(wrapping key)
    - Encryption Root Key 是 32B 随机串,用 KEK 加密后存进 KV 引擎
    - 启动时从 KV 读密文 → transit 解密 → 得明文 Encryption Root Key
    """
    provider_id = 0x02

    def __init__(
        self,
        vault_addr: str,
        vault_token: str,
        transit_mount: str = "transit",
        kv_mount: str = "secret",
        kv_version: int = 1,
        key_name: str = "framework-root-key",
        encrypted_key_path: str = "framework-encrypted-root-key",
    ):
        self.client = hvac.Client(url=vault_addr, token=vault_token)
        self.transit_mount = transit_mount
        self.kv_mount = kv_mount
        self.kv_version = kv_version
        self.key_name = key_name
        self.encrypted_key_path = encrypted_key_path

    async def initialize(self):
        """启动:确保 transit 引擎 + Encryption Root Key 存在。"""
        if not self.client.is_authenticated():
            raise AuthenticationFailedError("Vault auth failed")

        # 确保 transit 引擎启用
        engines = self.client.sys.list_mounted_secrets_engines()
        engine_path = f"{self.transit_mount}/"
        if engine_path not in engines.get("data", {}):
            self.client.sys.enable_secrets_engine(
                backend_type="transit", path=self.transit_mount,
            )

        # 确保 KEK 在 transit 里存在
        try:
            self.client.secrets.transit.read_key(
                name=self.key_name, mount_point=self.transit_mount,
            )
        except Exception:
            # 不存在,创建
            self.client.secrets.transit.create_key(
                name=self.key_name, key_type="aes256-gcm96",
                mount_point=self.transit_mount,
            )

    async def derive_org_key(self, org_id: str) -> bytes:
        encryption_root_key = await self.get_encryption_root_key()
        return await self._hkdf_derive(encryption_root_key, org_id)

    async def get_encryption_root_key(self) -> bytes:
        """从 KV 读密文 → transit 解密 → 得明文 Encryption Root Key。"""
        try:
            if self.kv_version == 2:
                secret = self.client.secrets.kv.v2.read_secret_version(
                    path=self.encrypted_key_path, mount_point=self.kv_mount,
                )
                encrypted_b64 = secret["data"]["data"]["key"]
            else:
                secret = self.client.secrets.kv.v1.read_secret(
                    path=self.encrypted_key_path, mount_point=self.kv_mount,
                )
                encrypted_b64 = secret["data"]["key"]

            encrypted = base64.b64decode(encrypted_b64)
            # transit 解密
            decrypted = self.client.secrets.transit.decrypt_data(
                name=self.key_name,
                ciphertext=f"vault:v1:{base64.b64encode(encrypted).decode()}",
                mount_point=self.transit_mount,
            )
            return base64.b64decode(decrypted["data"]["plaintext"])

        except Exception as e:
            raise ConfigError(f"Failed to load Encryption Root Key from Vault: {e}")

    async def encrypt_key(self, plaintext: bytes, org_id: str) -> tuple[bytes, bytes]:
        org_key = await self.derive_org_key(org_id)
        iv = secrets.token_bytes(12)
        encrypted = await aes_gcm_encrypt(org_key, iv, plaintext)
        return encrypted, iv

    async def decrypt_key(self, ciphertext: bytes, iv: bytes, org_id: str) -> bytes:
        org_key = await self.derive_org_key(org_id)
        return await aes_gcm_decrypt(org_key, iv, ciphertext)
```

**Vault 的核心设计**（注意：transit engine 里的是 KEK，区别于此）
- transit 引擎： `framework-root-key` = 永不导出的 KEK（Vault 内部持有，只能 encrypt/decrypt）
- Encryption Root Key：32 字节随机串，用 KEK 加密后存 KV；启动时解密
- write KV 失败 → fail-closed（拒绝用临时 key 启动，防 data loss）

### 5.3 加密生效边界

```
应用层/LLM/检索                           ← 明文
    │
    ▼
FileEncryptor.encrypt(data, org_id)      ← ★ 加密
    │
    ▼
Raw FS(网络/磁盘/S3)                      ← 密文(ENC1 信封)

读时:
Raw FS(磁盘)                             ← 密文
    │
    ▼
FileEncryptor.decrypt(data, org_id)      ← ★ 解密
    │
    ▼
应用层/LLM/检索                           ← 明文
```

**加密钩子只应挂在最底的读写调用**，各方法行为：

| 方法 | 加密行为 |
|---|---|
| **write** | 先加密，再写 |
| **read** | 先读全，解密，再切片返回（AES-GCM 整块认证，不能部分解密） |
| **append** | 读全→解密→拼接→整体重新加密→全量 PUT；保证密文整体一致性，但 O(n) 写放大。<br/>**高级替代**（高频 append 场景）：chunked encryption——按固定大小分 chunk，每 chunk 独立 key+IV+GMAC tag。追加只影响最后一个 chunk。代价：需要维护 chunk 索引，且各 chunk 间没有跨 chunk 的认证绑定——攻击者可能通过重排或截断 chunks 而不被发现。不建议作为默认方案，仅在日志类 append-heavy 且安全要求不那么极端的场景按需启用。|
| **delete** | 不涉加解密，直接删 |
| **mv**（非 temp） | 经 read/write hook，密文字节变，明文一致 |
| **grep** | 回落应用层解密后匹配（无法搜密文） |
| **search(vector)** | 向量库列明文——不经过加密层 |

### 5.4 配置

当前落地的 KV 加密通过组合 `security` provider 与 `kv_store` 装饰器启用；不存在全局
`encryption.enabled` 开关。未把业务 KV 指向 `target: encrypted` 时，存储仍按 raw KV
后端的原始行为运行。

```yaml
encryption:
  default:
    target: local
    params:
      key_file: "~/.agent-memory/security/master.key"
      allow_plaintext: false

kv_store:
  raw:
    target: sqlite
    params:
      db_path: agent_memory.db

  default:
    target: encrypted
    params:
      raw_kv_store: raw
      security: default
```

**默认不启用加密包装**，因为加密增加复杂度：随机读必须全量解密、grep 必须应用层解密、append 必须读全重写。部署者在确认需要 before storage encryption at rest 场景（如文件磁盘 on laptop、S3 bucket）时才把业务 KV 指向 encrypted wrapper。

### 5.5 错误分类

```python
class EncryptionError(Exception):
    pass

class InvalidMagicError(EncryptionError):
    """魔数不是 ENC1 / ciphertext too short"""
    pass

class CorruptedCiphertextError(EncryptionError):
    """envelope 解析失败 / version 不支持 / 长度不完整"""
    pass

class AuthenticationFailedError(EncryptionError):
    """AES-GCM tag 校验失败(数据被篡改)"""
    pass

class KeyMismatchError(EncryptionError):
    """File Key 解不开（Org Key 不匹配）"""
    pass
```

区分： **KeyMismatchError = 外层 key 错；AuthenticationFailedError = 内层 tag 错（数据坏）**。

所有错误都应该 fail-closed——解密失败不返回部分数据，不 fallback 到明文。

---

## 6. Key 管理与分发

本章管理的是**认证凭据**，即第 2 章的 API Key；它不管理第 5 章用于静态数据加密的 Encryption Root Key。两者必须使用独立随机值、独立配置项和独立轮换流程，禁止复用。

| 名称 | 用途 | 典型配置/托管位置 | 泄露影响 |
|---|---|---|---|
| **Root API Key** | 认证最高权限调用方，解析为 ROOT 身份 | `root_api_key`；Secret/Vault/K8s Secret | 攻击者取得系统管理权限，但不能直接解密落盘密文 |
| **Principal API Key** | 认证具体 user 或 agent；也可作为 OAuth token 的授权锚 | API Key 注册表中的 Argon2id hash + SHA-256 fingerprint | 对应主体被冒用，绑定 token 可能需级联撤销 |
| **Encryption Root Key** | 派生 Org Key、包裹 File Key，保护静态数据 | `security.default.params.key_file` 或 KMS/Vault transit，见 §5 | 攻击者可能解密受其保护的数据，但不会因此自动获得 API 权限 |

> **命名约束**：下文的 “Root API Key” 专指认证根凭据；第 5 章的 “Encryption Root Key” 专指加密根密钥。代码中应分别使用 `root_api_key` 与 `encryption_root_key`，配置中分别放在 `root_api_key` 与 `security.*` 命名空间，不要都简称为 `root_key`。

> **关联设计文档**：端云协同部署（Edge-only / Cloud-only / Hybrid）决定了两类 key 的保管位置与分发信道。Encryption Root Key 在端侧场景不出端、云侧场景由 KMS/Vault 托管；Root API Key 只通过 Secret 管理系统交付给受信管理员。见 [`design/architecture.md` §11 部署架构](../../design/architecture.md)。

### 6.1 整体分发流程

```
① 部署者:手动生成 Root API Key(openssl rand -hex 32)
   → 写进框架配置文件
   → (也是唯一的初始 Root API Key 来源)
        │
② 启动:框架加载 Root API Key
        │
③ 持有 Root API Key → 创建 org → register_principal
        │
        响应里返回 principal_api_key（明文,一次性）
        │
④ 调用方带外交付:加密 IM/Vault/K8s Secret
        │
⑤ 最终用户/agent:使用 key(Bearer / X-API-Key header)
```

**没有「主体自取 key」的路径**：所有 key 都是 admin 颁发，带外交付。这是刻意的设计：把分发安全责任交给运维流程，框架不内置「用户名密码 → 查看 key」的功能。

### 6.2 Root API Key Bootstrap

Root API Key 不是被 API 创建的——而是在框架启动前，由部署者在配置文件中声明：

```bash
# 部署者生成（任意方法）
$ openssl rand -hex 32
→ "a1b2c3d4e5f6..."  # 64 位 hex

# 写进配置文件
$ cat ~/.framework/config.yaml
root_api_key: "a1b2c3d4e5f6..."
auth_mode: "api_key"     # 否则有 root_api_key → 自动推断 api_key
```

```python
# 启动时,root_api_key 加载进内存
config.root_api_key = load_config()["root_api_key"]

# 校验时:
if hmac.compare_digest(provided_key, config.root_api_key):
    # 你就是 ROOT —— 直接返回 ROOT 身份
    return AuthContext(actor=Scope(org="*"), role=Role.ROOT)
```

**Root API Key 不能通过普通数据 API 改**：它在独立 Secret 配置中，轮换需走受控运维流程，并确保旧值立即失效。它与 Encryption Root Key 分别轮换；轮换任一方都不应覆盖或派生另一方。

### 6.3 Principal API Key 生成

user 与 agent 是同级主体，API Key 注册表应使用 `org + principal_type + principal_id` 标识归属。具体部署可以只给 user 发 API Key、让 agent 走 OAuth，也可以为两类主体分别签发，但不能用路径嵌套表达二者关系。

```python
async def register_principal(
    key_store: PrincipalKeyStore,
    org_id: str,
    principal_type: str,
    principal_id: str,
    role: str = "user",
) -> str:
    """注册 user/agent 主体，返回 API Key（明文仅在此返回）。"""
    validate_principal(org_id, principal_type, principal_id, role)

    if role == "root":
        raise PermissionDeniedError("Cannot create ROOT via register endpoint")

    # 生成 + 持久化都在 key_store.store_key 内完成(hashing 策略由 key_store 持有,
    # fingerprint 也在其中一并存入注册表)。返回一次性明文 key。
    key = key_store.store_key(org_id, principal_type, principal_id, role=role)

    return key  # ← 明文 key 仅在此返回,之后只有哈希版本
```

### 6.4 Principal API Key 轮换

```python
def compute_fingerprint_from_store(org_id: str, principal_type: str, principal_id: str) -> str:
    """从注册表读取主体当前 key 的 fingerprint。

    必须直接读取 store_key 时持久化的 key_fingerprint 字段,而不能用
    stored_value 重算--Argon2 哈希不可逆,无法从哈希值反推明文再算 sha256。
    """
    record = principal_registry.get(org_id, {}).get(principal_type, {}).get(principal_id)
    if record is None:
        raise PrincipalNotFoundError(f"No such principal: {org_id}/{principal_type}/{principal_id}")
    return record["key_fingerprint"]


async def regenerate_key(
    key_store: PrincipalKeyStore,
    org_id: str,
    principal_type: str,
    principal_id: str,
) -> str:
    """轮换 key:旧 key 立即失效。"""
    # 先取旧 key 的 fingerprint,用于级联失效该 key 签发的所有 OAuth token。
    # 必须在覆盖存储之前取--store_key 会覆盖注册表,之后取到的就是新 key 的 fp。
    old_record = principal_registry.get(org_id, {}).get(principal_type, {}).get(principal_id)
    if old_record is None:
        raise PrincipalNotFoundError(f"No such principal: {org_id}/{principal_type}/{principal_id}")
    old_fp = old_record["key_fingerprint"]

    # 更新存储(hashing 策略由 key_store 自身持有,role 沿用原值)
    new_key = key_store.store_key(org_id, principal_type, principal_id, role=old_record["role"])

    revoke_all_tokens_with_fp(old_fp)

    return new_key
```

### 6.5 Key 与 OAuth Token 的绑定

Token 记录声明了 authorizing key fingerprint：

```python
class Token:
    # 字段集与 §2.4.2 OAuthToken 对齐,避免同一概念两套定义。
    token_hash: str               # sha256(token_value)
    actor: Scope                  # 已认证执行者:user 或 agent
    acting_user: str              # agent 代 user 时填写,否则为空
    authorizing_principal: Scope  # token 绑定的 user/agent API Key 归属
    role: str
    authorizing_key_fp: str       # 签发时记录的 key 指纹
    expires_at: int
    revoked: bool = False

def verify_token(token_value: str) -> Token | None:
    token = lookup_token(token_value)

    # fingerprint 必须读 store_key 时持久化的 key_fingerprint 字段
    # (见 §6.4 compute_fingerprint_from_store),不能从 stored_value 现算--
    # Argon2 哈希不可逆,且与 §2.4.2 verify_bearer_token 取法保持一致。
    ap = token.authorizing_principal
    # Scope 携带 org/space/user/agent/session,需据此推出 (principal_type, principal_id)
    if ap.user:
        p_type, p_id = "user", ap.user
    elif ap.agent:
        p_type, p_id = "agent", ap.agent
    else:
        return None  # token 绑定的主体缺失,拒绝
    current_fp = compute_fingerprint_from_store(ap.org, ap.space, p_type, p_id)
    if not hmac.compare_digest(current_fp, token.authorizing_key_fp):
        # key 被轮换,该 token 失效
        revoke_token(token)
        return None

    return token
```

**效果**：`regenerate_key` → Principal API Key 改变 → fingerprint 改变 → 所有用旧 key 签发的 OAuth token 失效（fail-closed）。在 agent 代 user 的 demo 路径中，token 绑定的是授权 user 的 API Key fingerprint；这表示用户可通过轮换 key 使既有委托失效，不表示 agent 从属于 user。

---

## 7. 审计日志（Audit Logging）

> **关联设计文档**：审计是「可治理」原则（可检视/编辑/审计/回溯/遗忘）的一环。记忆的 `lifecycle` 用「标记失效」而非物理删除（非破坏式更新），`delete` 支持 `purge` 合规删除（物理删除真源与全部派生索引，仅留审计记录）——这两种删除都需审计留痕。见 [`design/architecture.md` §3.1 记忆单元](../../design/architecture.md)、[`design/architecture.md` §12 横切关注点](../../design/architecture.md)、[`design/architecture.md` §14 关键数据流](../../design/architecture.md)（写入路径含审计落点）、[`design/vision.md` §3 设计原则](../../design/vision.md)。

### 7.1 AuthContext：认证产物与审计来源

`AuthContext` 是认证层完成凭据校验后产生的**可信请求级安全上下文**，不是客户端提交的数据结构。API Key、OAuth Bearer Token、受信网关或本地 session 等不同认证路径，最终都必须归一为同一种 `AuthContext`，供 PEP、特权闸门和审计使用。

`AuthContext` 与 `Scope` 的职责不同：

- `Scope` 只表达 actor 或 target 的资源归属，保持 `org > space > user/agent > session` 的纯作用域模型；space 是逻辑隔离单元，user/agent 的顺序由 `principal_path` 决定。
- `AuthContext.actor` 表示已认证的操作执行者，可以是 `Scope(org, space, user)` 或 `Scope(org, space, agent)`。
- `AuthContext.acting_user` 表示当前操作对应的 user：user 自操作时等于 `actor.user`；Agent 经 user 授权代其操作时，是委托目标。它来自服务端验证过的 OAuth claim、授权记录或 session，不来自请求 body/URI；该字段不表示 user 与 agent 存在从属关系。
- PEP 使用完整 `AuthContext` 做授权和审计；鉴权通过后只把 target scope 下沉到 Engine/Store，避免认证元数据污染存储接口。

参考 `D:\agent-memory-mem2.0\examples\security_demo\auth\auth_context.py`，当前最小字段如下：

| 字段 | 来源 | 用途 |
|---|---|---|
| `actor: Scope` | API Key 注册表、OAuth token claim、受信网关 | 已认证的操作执行者；user/agent 二选一，ROOT 可使用全局 org scope |
| `acting_user: str` | user API Key 归属、用户授权记录或绑定了 user+agent 的 OAuth token | user 自操作时等于 `actor.user`；Agent 代 user 操作时为目标用户；与 user 无关的请求为空 |
| `role: str` | 服务端角色注册表或已验证 claim | ROOT/org_admin/user 等特权闸门与审计 |
| `from_oauth: bool` | 认证分流器 | 区分 OAuth 与 API Key 路径，阻止 OAuth 凭据签发新的 OAuth 状态 |
| `authorizing_key_fp: str` | 签发 token/session 时绑定的 Principal API Key fingerprint | key 轮换后的 token/session 级联失效与审计追责 |

```python
@dataclass
class AuthContext:
    actor: Scope
    acting_user: str = ""
    from_oauth: bool = False
    role: str = "user"
    authorizing_key_fp: str = ""
```

中间件构造 `AuthContext` 后，应通过显式参数或 `ContextVar` 在单次请求内传播，并在请求结束时可靠 reset。任何 handler、LLM tool_call 或业务参数都不能覆盖其中字段。

> **主干实现注记**（F01）：主干实现在 `src/common/type_def/auth.py`
> （横切结构，故落在 `common` 而非 `security` 私有），与上表有三处差异：
> `role` 是 **`Role` 枚举**（`USER` / `ADMIN` / `ROOT`，继承 `str, Enum` 以便直接
> 进 `AuditEvent.detail`）而非裸 `str`，且**无默认值**——「忘了传 role」不该
> 静默得到 `user`；dataclass 是 **`frozen=True`**，落实「任何 handler 都不能覆盖
> 其中字段」；`actor` 同样**不给默认值**，否则漏传会得到空 `Scope()`
> 即 platform-admin 全局权限，是最糟的 fail-open 形态。
> 传播用 `ContextVar`：`set_current` / `reset_current` / `get_current`，
> **reset 必须在 `finally`**（`ThreadingHTTPServer` 复用线程，泄漏的 ContextVar
> 会让下一个请求继承上一个的身份）；`get_current()` 未认证时返回 `None`，
> **不返回默认上下文**。

未来可按审计和协议演进增加以下字段：

| 候选字段 | 语义与约束 |
|---|---|
| `authenticated_at` | 凭据完成校验的服务端时间，用于判断认证上下文新鲜度 |
| `credential_type` | `api_key` / `oauth_access_token` / `session` / `trusted_gateway`；可替代单一 `from_oauth` 布尔值 |
| `credential_id` / `token_family_id` | 指向凭据或 refresh token family 的不可逆标识，支持定点撤销与重放追踪 |
| `client_id` | OAuth client/Agent 标识；应与 `actor.agent` 一致或能由服务端映射 |
| `consent_id` / `delegation_id` | 证明哪个 user 在何种授权范围内允许哪个 agent 代操作 |
| `request_id` / `session_id` | 串联一次请求、会话及其多条审计事件 |
| `memory_rewrite_at` | 记忆成功改写的服务端时间戳；只能由 PEP/MemoryAPI 在操作成功后写入，不能由客户端自报。一次请求改写多条记忆时，应优先记录在逐条 AuditEvent 中，而不是只保留单值 |

新增字段必须遵守三条约束：由可信服务端组件写入；默认不下沉到 Engine/Store；只有稳定且参与多处安全决策的字段才进入 `AuthContext`，纯业务详情优先放在 AuditEvent 的 `detail` 中。

### 7.2 必须记录的事件

安全审计日志和业务日志是两回事。审计日志记录的是**谁在什么时候做了什么决定性的安全操作**，用于事后追溯和合规检证。

以下事件**必须**记录：

| 事件类别 | 具体事件 | 关键字段 |
|---|---|---|
| **组织管理** | 创建/删除 org | org， operator， action， timestamp |
| **主体管理** | 创建/删除/恢复 user 或 agent | org， principal_type， principal_id， operator， role_before， role_after |
| **权限变更** | 改 role / grant / revoke | org， actor， target_scope， operator， old_role， new_role |
| **Key 操作** | 生成/轮换 Principal API Key 或 Root API Key | org， principal_type， principal_id， operator， key_fingerprint |
| **认证事件** | 认证失败（超出阈值） | org（如已知），principal（如已知），ip，reason，count |
| **Token 事件** | OAuth token 撤销/重放 | token_fingerprint，org，client_id，acting_user，reason |
| **资源删除** | 删除 session / 批量 rm | target_scope，operator，recursive_flag |
| **记忆改写** | update / supersede / overwrite | actor， acting_user， target_scope， memory_id， rewrite_timestamp， request_id |

### 7.3 日志记录的完整性保护

> **Demo 实现注记**：`agent-memory` mem2.0 已有独立的 `Governor` 实现（`InMemoryGovernor`）：检视（inspect，跨 scope 读 KVStore）、回溯（trace，沿 provenance 血缘）、审计查询（audit，调 `AuditLogger.query`）。`Governor` 通过 `GovernorProducer` 注册，装配时 `GovernorProducer.dep(root, default="in_memory")` 取实例。demo 的 `DemoMemoryAPI` 把 inspect/trace/audit 合并到接口层（没独立 Governor），计划对齐。
>
> `agent-memory` mem2.0 已有两个 AuditLogger 实现：`InMemoryAuditLogger`（内存）和 `SqliteAuditLogger`（SQLite 持久化）。接口已对齐 `AuditLogger(ABC)`（`record(event: AuditEvent)` + `query(filters, limit)`）。HMAC 完整性保护仍留 TODO。demo 的 `AuditLog` 未继承 ABC（签名不兼容，合并缺口），计划对齐。

审计日志本身不能被篡改——否则就失去了审计价值。

```python
import hmac
from datetime import datetime, timezone

class AuditLogger:
    def __init__(self, hmac_key: bytes):
        self._hmac_key = hmac_key
        self._entries: list[str] = []

    def log(self, event: str, **fields):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        # 链式 HMAC:每行都做 HMAC,且 HMAC 包含前一条的 HMAC
        # 注意:prev_hmac 取的是"前一条 entry 的 _hmac 字段值",必须与
        # verify_integrity() 的取法一致,否则校验必然全量误报。
        prev_hmac = json.loads(self._entries[-1])["_hmac"] if self._entries else ""
        entry["_hmac"] = hmac.new(
            self._hmac_key,
            (prev_hmac + json.dumps(entry, sort_keys=True)).encode(),
            "sha256",
        ).hexdigest()
        self._entries.append(json.dumps(entry, ensure_ascii=False))
        self._flush()

    def verify_integrity(self) -> list[str]:
        """全量校验审计日志完整性,返回被篡改的行索引。"""
        tampered = []
        for i, line in enumerate(self._entries):
            entry = json.loads(line)
            # self._entries[i-1] 是 JSON 字符串,必须先 json.loads 再取 _hmac;
            # 且取值方式与 log() 完全一致(前一条的 _hmac 字段值)。
            prev_hmac = json.loads(self._entries[i - 1])["_hmac"] if i > 0 else ""
            expected = hmac.new(
                self._hmac_key,
                (prev_hmac + json.dumps(
                    {k: v for k, v in entry.items() if k != "_hmac"},
                    sort_keys=True
                )).encode(),
                "sha256",
            ).hexdigest()
            if entry["_hmac"] != expected:
                tampered.append(i)
        return tampered
```

链式 HMAC 保证：**改一行 = 破坏该行及后续所有行的 HMAC**。不需要复杂的签名方案，但足够防止非 root 级别的篡改。

### 7.4 日志保留与轮换策略

- **在线保留**：最近 90 天，用于快速回溯。
- **归档**：冷存储（压缩加密），保留至少 365 天（合规要求长则按需放宽）。
- **轮换**：按大小或天数轮换。建议 100MB 或每天轮换一次。
- **不可删除**：审计日志不能有「删除条目」的功能。轮换只是归档，不是丢弃。

### 7.5 不记录的内容（PII 脱敏）

- 不要记录 API key 明文、password、token value——只记录它们的 fingerprint。
- 不要记录会话 messages 的正文——只能记「共 N 条消息，最后一条 ID」这种元信息。
- 如果涉及用户内容元数据，需确保符合隐私合规要求（PII 控制）。

---

## 8. 附加攻击面指引

> **关联设计文档**：分层记忆结构（L0 摘要 / L1 片段 / L2 全文，原始数据为唯一真源）与检索层（scope 为独立轴、各 Store 查询的专用 `scope` 字段做原生隔离）共同决定了索引层攻击面。向量库的明文 abstract 列、各 Store 的索引，需与内容层分开评估访问控制。见 [`design/architecture.md` §4 分层记忆结构](../../design/architecture.md)、[`design/architecture.md` §7 记忆检索层](../../design/architecture.md)、[`design/vision.md` §4 支柱二](../../design/vision.md)。

### 8.1 速率限制（Rate Limiting）

API key 认证的框架天然面临密钥枚举和资源耗尽风险。建议：

```python
class RateLimiter:
    """简单的令牌桶速率限制,按 API key 分组。"""
    def __init__(self, capacity: int = 10, refill_per_sec: float = 1.0):
        self._buckets: dict[str, "TokenBucket"] = {}
        self._capacity = capacity
        self._refill = refill_per_sec

    def check(self, key_fp: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.setdefault(key_fp, TokenBucket(self._capacity, now))
        bucket.refill(now, self._refill)
        return bucket.consume()

    # 特别关注:
    # - Argon2 verify 是 CPU 密集操作,不限制可以被打挂
    # - OAuth token endpoint 应做严格限流(防 brute force)
    # - register_principal 应做 IP 级限流(防恶意开账户)
```

### 8.2 MCP 协议的攻击面

如果通过 MCP 暴露工具给 AI Agent，需额外注意：

- **工具描述注入**：LLM 的 prompt 可以通过工具描述被污染——确保工具描述不可由用户写入。
- **参数走私**：agent 可能在 tool_call 中传入意料之外的参数值，后端应对每个参数做校验（即使前端模型已做，后端不可信任模型输出）。
- **权限传播**：agent 持有的 token 直接决定了它能做的一切。不要把 full ROOT 级别的 token 传给 agent——**最小权限原则在此处同样适用**。

### 8.3 Secret 在内存中的生命周期

- Root API Key 或 Encryption Root Key 加载到 `bytes` 对象后，**框架自身不提供 zeroize**。Python 的 `bytes` 对象不可变，GC 后数据残留是底层实现行为。两类 key 必须使用不同的内存变量和生命周期；对高安全场景应考虑：
  - 用 `memoryview` + mutable buffer（C 扩展）；
  - 或把 Key Provider 进程隔离（框架通过 IPC 访问 KMS，不持有明文）。
- **日志脱敏**：所有 `key` / `token` / `secret` 值在写日志前必须截断或哈希——至少保证「不能在日志里找到明文密钥」。
- **Docker 环境变量注意**：`docker inspect` 可看到 live container 的环境变量。如果把 Root API Key 或 Encryption Root Key 放进 `-e` 参数，任何能 `docker inspect` 的人都能拿到。

### 8.4 依赖安全

- 核心密码学依赖（`cryptography`、`argon2-cffi`、`hvac`)必须在 CI 中跟踪 CVE；
- 建议使用 Dependabot / Renovate + 安全顾问列表，每月检查；
- 对`cryptography`等关键库指定 `>=` 而不是 `==` 版本，但不能 `>=latest`（会出现 API break）——应在 CI 定期跑测试。

---

## 9. 安全开发 7 条铁律（Checklist）

写完代码后，对照检查每条：

### 1. 身份来自上下文，不来自参数

```python
# ❌ BAD: 从 URI 提取并信任主体身份
def read_file(uri: str):
    actor = extract_actor_from_uri(uri)

# ✅ GOOD: actor/委托信息来自 AuthContext，target 单独鉴权
def read_file(uri: str, target: Scope, ctx: AuthContext):
    ensure_access(target, ctx)
    path = f"{scope_namespace(target)}/{normalize_uri_parts(uri)}"
```

**自查**：代码里的 org、user/agent、role、`acting_user` 是从认证中间件的 `AuthContext` 取的，还是从 request body / URL parameter / 未验证 header 读的？如果是后者，攻击者就能在单次请求里声明身份或伪造委托。`MemoryAPI` 用 `identity: Scope`（keyword-only）作为调用方身份参数，`PermissionManager.check(identity, scope, action)` 在接口层执行；Agent 代 user 的补充委托只从可信 `AuthContext` 读取，identity/AuthContext 均不下沉到 Engine。

### 2. 所有加密比对都是常时间的

```python
# ❌ BAD: == 运算符,非常时间
if provided_key == stored_key:
    return True

# ✅ GOOD: hmac.compare_digest
if hmac.compare_digest(provided_key, stored_key):
    return True
```

**自查**：哪个地方的 key/secret/token 比对用了 `==` / `!=`? 全仓 grep `==` 找密钥比对路径，确认全部已替换成 `compare_digest`。Argon2 自带常时间验证（`PasswordHasher.verify`)。前缀索引查询（dict 直查）不是 timing-safe，但只泄露前缀 8 字符，接受此风险。

### 3. 加密 fail-closed，不是 fail-open

```python
# ❌ BAD: 解密失败 → 返回原始密文
try:
    return decrypt(key, data)
except Exception:
    return data

# ✅ GOOD: 解密失败 → 抛异常
try:
    return decrypt(key, data)
except AuthenticationFailedError:
    raise  # 不 fallback
```

**自查**：哪个 catch 了加密/解密/认证函数异常，然后 fallback 到了不安全路径?encrypt， decrypt， hmac， PasswordHasher.verify 都要 fail-closed。另外，解密函数要考虑兼容性好：不是 ENC1 魔数的直接原样返回；但只要是 ENC1 信封，解密失败就必须要拒绝，不能返回部分数据。

### 4. 写操作经过 ensure_mutable_access

```python
# ❌ BAD: 写操作只做了"读"检查
async def write(uri, data):
    ensure_access(uri, ctx)     # 不够
    fs.write(uri, data)

# ✅ GOOD: 写操作有 create 检查
async def write(uri, data):
    ensure_mutable_access(uri, ctx)  # 含特殊约束
    fs.write(uri, data)
```

**自查**：是否存在某个写操作（create / delete / mv / mkdir / upsert）只做了读级检查而没经过 `ensure_mutable_access`？**自诊断**：留意 `append`——如果你实现了 append，它用的是读级 access (`ensure_access`)而不是 create(`ensure_mutable_access`)?如果是，确认是否合理。

### 5. ROOT 数据操作必须有租户 guard

```python
# ❌ BAD: ROOT 写的文件落到 default
async def some_handler(ctx, ...):
    path = "/data/default/..."

# ✅ GOOD: ROOT 必须显式声明 org 与同级主体
if ctx.role == Role.ROOT and not ctx.from_oauth:
    if principal_type not in {"user", "agent"} or not org_id or not principal_id:
        raise InvalidArgumentError("ROOT must specify org and principal")

target = Scope(org=org_id, **{principal_type: principal_id})
path = f"{scope_namespace(target)}/..."
```

**自查**：ROOT 身份的 handler 在访问 org 内数据时，是否显式选择了 user 或 agent 主体？是否可能同时设置两者、落到默认主体或绕过 PEP？检查 OAuth token 情况：OAuth ROOT 是否通过 `ctx.from_oauth` 标志放过了此 guard？如果是，确认该豁免有已验证的 target scope 约束。

### 6. 路径穿越、符号链接、与文件系统转义

```python
# ❌ BAD
def read_file(path):
    with open(path, "r") as f:
        return f.read()

# ✅ GOOD: 复用 §3.3.3 的 normalize_uri_parts(同一份校验,不要另起实现)
def read_file(uri: str, target: Scope, ctx: AuthContext):
    ensure_access(target, ctx)
    safe = normalize_uri_parts(uri)   # 拒 .. / . / \ / 盘符,返回规范化相对路径
    path = f"{scope_namespace(target)}/{safe}"
    ...
```

> 不要为路径校验另写一份 `ensure_safe_path`--`normalize_uri_parts`(§3.3.3)已是统一入口,写操作(§4.4)与读操作都应复用它,避免两份逻辑漂移。

**自查**：是否过滤了 `..` / `.` / `\\` / Windows 盘符前缀?如果代码内部又拼接了一次 URI，确保后端层也做了对应转义。对用户可控的 URI 传入 `os.path.join` / `open()` 之类标准库函数的场景，特别小心——这些函数对 `..` 不会做防护，需要应用层提前拒绝。

### 7. 索引层（Cache / Vector / Embedding Queue）纳入攻击面

```python
# 自问:如果向量库/缓存/队列被攻破,会泄露什么?
# - abstract 列是否包含明文摘要? → 加密文件未必能保护索引层。
# - org_id 与 principal_type/principal_id 是否正确过滤? → 跨租户查询能否读到不该读的行?
# - 有没有幂等机制防重复处理? → embedding queue at-least-once 投递后,业务处理是否幂等?
# - embedding queue 文件权限? → queue.db 是否只对框架进程可读写?
```

**自查**：加密通常只覆盖「文件/对象存储」这一层。向量库的 `abstract` 列、embedding queue 的 sqlite、缓存层的 kv 存储，**往往不在加密范围内**。不要认为「文件加密了」等于「全链路安全了」。你的威胁模型里，索引层和内容层应该分开评估，并分别配置访问控制。
