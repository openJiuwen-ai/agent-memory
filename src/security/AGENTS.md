# Agent Memory Security

**规约文档**：[docs/features/common/F04-security-interfaces-and-encryption.md](../../docs/features/common/F04-security-interfaces-and-encryption.md)

> `docs/specs/` 下暂无安全模块 spec（S01~S07 无 security）。本模块规约以
> 上述 F04（下文简称 **security.md**，它是原 `docs/security/security.md`
> 迁入 common 特性归档后的位置）为准；跨模块契约变动（如 `AuthContext` 进入
> `PermissionManager.check`）需同步 `docs/specs/S03-control.md`。
> 第二期认证契约稳定后再考虑新增 `S08-security.md`。

认证层（三道防线的第①道）：把一次请求的凭据材料校验成可信身份 `AuthContext`。
**只回答「你是谁」，不回答「你能做什么」**——授权（第②道）在
`src/control/permission.py`，静态加密（第③道）在 `src/common/security/`（密码学
内核）+ `src/storage/{kv,fs}_impl/encrypted_*_store.py`（接线），都不在本模块。

认证模式由配置在装配期选定（`dev` / `trusted` / `api_key`），运行期不再分流：
三种模式是三个独立实现类，不是一个类里的 if/else。所有实现继承 `Authenticator`
或 `PrincipalKeyStore`，由外部装配注入，本模块不决定自己何时被调用。

## 模块地图

| 文件 | 职责 |
|---|---|
| `types.py` | `AuthMode` 枚举 + `Credentials`（frozen，一次请求的原始凭据材料）；不依赖本层其他文件 |
| `authenticator.py` | `Authenticator` 抽象接口 + `AuthProducer`（`TOP_NAME = "authenticator"`） |
| `key_store.py` | `PrincipalKeyStore` 抽象接口 + `KeyStoreProducer`（`TOP_NAME = "key_store"`）+ 模块级 helper：`fingerprint` / `key_prefix` / `generate_api_key` |
| `binding.py` | `check_dev_binding()`——DEV 模式的 localhost 强制绑定 guard，供接入形态在启动期调用 |
| `rate_limit.py` | `RateLimiter` 抽象接口 + `RateLimitProducer`（`TOP_NAME = "rate_limiter"`） |
| `audit_hmac.py` | `HmacAuditLogger` 装饰器 + `derive_audit_key`：给任意 `AuditLogger` 加链式 HMAC 完整性保护（§7.3）；`@AuditProducer.register("hmac")` 配置驱动 opt-in |
| `bootstrap.py` | `register_security()` 统一 import 各 `*_impl/` 包 + `audit_hmac`，触发实现自注册（幂等） |
| `authenticator_impl/` | 认证实现目录。当前实现：`dev_authenticator.py` / `trusted_authenticator.py` / `api_key_authenticator.py` |
| `key_store_impl/` | 主体 key 存储实现目录。当前实现：`memory_key_store.py`（进程内注册表 + Argon2id） |
| `rate_limit_impl/` | 限流实现目录。当前实现：`token_bucket_limiter.py` / `unlimited_limiter.py` |
| `__init__.py` | 公开导出抽象、工厂、helper 与 `register_security` |

`AuthContext` / `Role` / `ROLE_RANK` 及其 ContextVar 存取（`set_current` /
`reset_current` / `get_current`）不在本模块——它们是跨层数据类型，住在
`common/type_def/auth.py`。本模块产出 `AuthContext`，`api/` 与 `control/` 消费它。

## 文件关系

- 顶层 `.py` 只定义抽象接口与无状态 helper，零认证逻辑
- `types.py` 不依赖本层其他文件（纯数据定义）
- 顶层接口文件不 import `*_impl/`；`*_impl/` import 顶层接口文件
- Producer 工厂定义在对应顶层接口文件中（`authenticator.py` 的 `AuthProducer`、
  `key_store.py` 的 `KeyStoreProducer`），不新增独立 `*_producer.py`
- `trusted_authenticator.py` / `api_key_authenticator.py` 依赖注入的
  `PrincipalKeyStore`；`dev_authenticator.py` 无依赖
- `binding.py` 独立，不被本模块其他文件引用——它的调用方是接入形态的启动入口

## 行为铁律

1. **认证失败一律抛 `AuthenticationError`**：不返回 `None`、不返回默认身份。
   返回 `None` 会诱导调用方写 `if ctx is None: ctx = default` 这类 fail-open
   分支。认证只有「成功」与「失败」两种结果。
   （`PrincipalKeyStore.resolve` 返回 `AuthContext | None` 是**查询语义**不是认证
   语义——它的 `None` 必须由 `Authenticator` 翻译成 `AuthenticationError`。）
2. **所有密钥比对走 `hmac.compare_digest` 或 Argon2 的 `verify`**，禁止 `==` / `!=`。
   `compare_digest` 传入两侧都要 `.encode("utf-8")`：str 版对非 ASCII 抛
   `TypeError`，会把 401 变成 500。
3. **对外错误消息笼统**：所有失败路径共用 `"authentication failed"`，不区分
   「凭据缺失」「主体不存在」「凭据错误」——区分即主体枚举侧信道。具体原因
   只进审计事件的 `detail`。`tests/unit/security/test_authenticator_impl.py::test_all_failures_share_one_message`
   是这条的回归防线。
4. **顶层 `.py` 是纯抽象，不 import `*_impl/`**（与 control 同规）。
5. **ROOT 的 actor 是空 `Scope()`，不是 `Scope(org="*")`**：
   `SQLitePermissionManager.check` 的首条规则是 `actor == Scope() → True`，而
   `org="*"` 会撞上「跨 org 拒绝」规则，ROOT 反而寸步难行。
6. **构造 `Scope` 一律用 keyword**：`Scope(org=..., user=...)`。字段可能新增
   （如 `space`），位置参数会静默错位成越权。
7. **fail-closed，绝不降级**：`argon2-cffi` 缺失时在装配期抛 `ValidationError`，
   不回退到明文比对——回退等于把 key 变成磁盘上的裸明文。
8. **role 不从 header 读**：TRUSTED 模式下 header 只声明「你是谁」
   （org / principal_type / principal_id），「你能干什么」由框架自己查
   `PrincipalKeyStore.get_role`。防的是网关被攻破或误配时的任意提权。

## 与其他子目录的边界

**本模块管**：
- 凭据 → `AuthContext` 的校验（三种模式）
- 主体 API Key 的签发 / 解析 / 撤销（Argon2id 哈希，注册表不存明文）
- DEV 模式的 localhost 绑定 guard
- 按主体维度的速率限制（`rate_limit.py` + `rate_limit_impl/`）

**不管**：
- 授权判定（`actor` 能否操作 `target`）→ `control/permission.py`
- `AuthContext` 类型定义与 ContextVar 传播 → `common/type_def/auth.py`
- 从 HTTP / MCP / CLI 提取 `Credentials`、决定何时调用认证 → `bootstrap/`
- 静态加密的密码学内核（信封 / AES-GCM / HKDF / 根密钥）→ `common/security/`；
  把它接到存储上的两个装饰器 → `src/storage/{kv,fs}_impl/encrypted_*_store.py`。
  本模块与加密**无依赖关系**：`register_security()` 不注册任何加密实现，
  `api.build_kernel` 也从不调 `register_security()`。

## 本地约束

- `Credentials.headers` 的 key **一律小写**：HTTP header 大小写不敏感，提取方
  （`bootstrap/`）负责归一化，本模块按小写常量匹配，不在读取处重复 `.lower()`
- `key_fp`（sha256）是确定性查找键，`key_hash`（Argon2id）才是校验凭据；
  两者用途不可互换——`key_fp` 不能用于校验，`key_hash` 不能用于索引
- `InMemoryKeyStore.resolve` 的三条路径（命中 / 有候选但 key 错 / 无候选）
  必须**恰好各跑一次** Argon2 verify。无条件补 dummy 会让「有候选但 key 错」
  跑两次，造出反向的 2x 时间差——同样是可测量的侧信道
- `PrincipalKeyStore.issue` 拒签 ROOT key：ROOT 只能来自配置声明的 Root API Key
- 主体 scope 必须恰好设置 `user` / `agent` 之一，且必须有 `org`；两者都设或都不设
  会签出「整个 org」这种无主体的 key
- `check_dev_binding` 抛 `ValidationError`，不 `sys.exit`——启动期 guard 也要可测
- **TRUSTED 模式装配期必须配 `gateway_key`**：未配置时身份 header（X-Org-Id / X-Principal-* 等）可被任意能连到本端口的调用方伪造。`_build` 默认拒绝启动；确需仅靠网络隔离时显式 `allow_no_gateway_key=true` opt-in（审计 P1-2）
- **role 按 principal（org + user/agent）索引**，不含 `session`、不含 `space`：§3.1 角色是 principal 级，session/space 是资源维度不是身份维度。含 session 会让同 principal 换 session 登录查不到 role；含 space 会让「同 principal 同 role」变成两条互覆记录。revoke 单 key 时须检查同 principal 是否仍有未撤销 key，否则会误删共享 role 条目（审计 P2-2）
- **Argon2 verify 有进程级并发上限**（`concurrency_guard.py`，审计 P1-3）：IP
  令牌桶限请求速率，限不住「同时在跑的 Argon2 verify 数」--后者才是 CPU/内存
  耗尽向量。`Argon2Guard` 是进程级 `BoundedSemaphore`，非阻塞 acquire，耗尽即
  429。DEV 模式不跑 Argon2 不需 guard；默认上限 4（按 512 MiB / 128 MiB），
  由 `argon2.max_concurrent` 配置。不进 Factory：进程级状态按配置实例化多份
  没有意义
- 已知遗留（`memory_key_store.py` 顶部有详述）：无验证缓存（5~20 QPS/核）、
  进程重启后已签发 key 全部失效（生产需 SQLite 后端）
