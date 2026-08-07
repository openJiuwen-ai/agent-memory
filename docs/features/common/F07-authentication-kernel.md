# F07 — 认证内核、三档认证模式与速率限制

## 元信息

| 项 | 值 |
|---|---|
| 原始编号 | security/F01（已迁移到 common/F07） |
| 日期 | 2026-07-29 |
| 实施阶段 | 认证与加密期（对应 [F04](F04-security-interfaces-and-encryption.md) §术语说明中的"认证与加密期"） |
| 影响范围 | `src/common/security/authentication/`、`src/common/security/protection/`、`src/common/security/types.py`、`bootstrap/core/auth_middleware.py`、对应镜像测试目录与各 surface 装配入口 |
| 测试基线 | 改动前 `2 failed, 656 passed, 60 skipped`；改动后 `2 failed, 788 passed, 60 skipped`。**两个失败是同一对**（`test_bge_m3_embedder.py` 的 `torch` 未安装，`embed` extra 未装），与本改动无关 |
| 依据 | [F04 安全架构总纲](F04-security-interfaces-and-encryption.md) §1.1 核心不变量、§2 认证、§3 授权角色、§7 审计、§8.1 速率限制、§9 铁律 #1 |
| 规范契约 | [S09 安全横切契约](../../specs/S09-security.md) |
| Refs | — |

> **2026-08-05 F05 迁移后记。** 本文记录的是 2026-07-29 的决策过程，正文保留原貌。
> 落点与若干细节已随 F05 Common Security 改变，以下述为准：
>
> - **目录**：`common/authentication/` + `credential_store/` + `admission/` + `type_def/auth.py`
>   收敛为 `common/security/{authentication,protection,cryptography}/` 与 `security/types.py`。
>   旧平铺路径只是历史状态，不再作为约束。
> - **`AuthMode` 枚举已删除**（决策 3 的"三档模式"仍在，但不再由封闭枚举表达）：
>   `mode()` 返回开放字符串，核心不得按其分支，行为差异一律由 capability 方法声明。
> - **`Argon2Guard` → `WorkloadGuard`**（决策 12）：从"Argon2 专用"泛化为"昂贵安全操作的
>   并发预算"，进 Factory（`TOP_NAME` 为 `workload_guard`），内置实现 `semaphore`。
>   `default_argon2_guard()` 全局单例与"重复装配报错"随之取消——共享改由**具名实例**
>   显式表达，同一个 `workload_guard.shared_budget` 被谁引用从配置里就能读出来。
> - **决策 13 的 `allow_plaintext` 已彻底删除**，不再是"默认 False"而是不存在该开关
>   （F05 §明文策略）。
> - **装配面收敛**：`Server.build` 不再逐个 `_build_authenticator` / `_build_rate_limiter`，
>   改为装配一个 `SecurityRuntime`（`TOP_NAME` 为 `security`），由它持有全部能力引用
>   并在启动期统一健康检查。详见 [S09](../../specs/S09-security.md)。
>
> 决策 1–11、14+ 的实质结论不受影响，只是落点改名。

> **行文简称**：下文（及本模块所有代码注释）里的 **security.md** 一律指上表「依据」
> 那份文档（现为 [F04 安全架构总纲](F04-security-interfaces-and-encryption.md)）。
> 它原在 `docs/security/security.md`，上游 `c76eb90` 迁入 common 特性归档并改名为
> `F04-security-interfaces-and-encryption.md`，2026-08-06 更新为当前安全架构总纲；
> 章节编号未变，故简称与 §号沿用不改。

> **为什么速率限制在这份文档里而不是单开一份**：它唯一的存在目的是保护认证。
> API_KEY 模式下每次 `authenticate` 跑一次 Argon2id verify（128 MiB × time_cost=4，
> 约 50~200ms），无限制触发能把进程的 CPU 与内存同时打满——**这个可用性风险
> 是引入 Argon2 时一并带进来的**，不是一件独立的事。把「留了个洞」和「补上了」
> 记在同一份文档里，比拆成两份、再让读者去两处对照要诚实。

## 背景

### 漏洞：身份可由请求体伪造

改动前 `bootstrap/core/handler.py` 的 `_actor_scope(payload)` 直接从请求体
读取调用方身份：

```python
def _actor_scope(payload: Body) -> Scope:
    """Claimed actor scope; defaults to payload scope, with optional explicit override."""
    if any(key in payload for key in ("actor_tenant_id", "actor_scope", ...)):
        ...
        return Scope(org=actor_org, user=str(payload.get("actor_scope", "")), ...)
```

docstring 自己写了 "Claimed" —— 这是**调用方声明的**身份，未经任何校验。

利用链（已在改动前的 HEAD 上端到端实测）：

```python
srv.dispatch("add", {"tenant_id": "acme", "scope": "alice", "content": "alice secret"})
# → 200，alice 写入

srv.dispatch("search", {"tenant_id": "acme", "scope": "alice", "query": "secret",
                        "actor_tenant_id": "evil", "actor_scope": "mallory"})
# → 403，攻击者用真实身份读，被正确拒绝

srv.dispatch("search", {"tenant_id": "acme", "scope": "alice", "query": "secret",
                        "actor_tenant_id": "acme", "actor_scope": "alice"})   # 改两个字段
# → 200 ['alice secret']
```

**授权层是对的**（honest read 正确返回 403）；洞在于**认证层根本不存在**，
攻击者可以任意填写 `actor_*` 把自己变成任何人。这两行 payload 的差异就是本
特性要消除的东西。

`_actor_scope` 在 `handler.py` 有 13 处调用点，覆盖 add / search / get /
update / delete / evolve / job / inspect / trace / audit / admin / grant /
revoke —— 即**全部动词**，含管理面与授权面。

> **一处曾经的误判，留作记录**：起初以为最短利用链是「提交
> `{"actor_tenant_id": " "}` 得到空 `Scope()` → 命中
> `SQLitePermissionManager.check` 的 platform-admin 全局放行」。实测不成立：
> `_actor_scope` 不做 strip，`" "` 原样进 `Scope(org=" ")`，与空 `Scope()`
> 不相等；空 org 分支会回退到 `tenant_id`（默认 `"default"`）。危害不因此
> 降低——「冒充任意已知主体」已是完全的越权读写，只是不能一步登顶
> platform admin。测试按实测形态写。

### 三道防线的覆盖变化

| 防线 | security.md | 改动前 | 改动后 |
|---|---|---|---|
| ① 认证 | §2 | **完全没有** | DEV / TRUSTED / API_KEY 三档，配置选定 |
| ② 授权 | §3 | 有（`PermissionManager` + PEP 在 `LocalMemoryAPI._authorize`） | 不变，但**输入端从「调用方声明」换成「认证层产出」** |
| ③ 数据保护 | §5 | 没有 | 不变（见 [storage/F02 加密存储](../storage/F02-encrypted-storage.md)） |
| 审计 | §7 | 有 | 增记认证失败与限流拒绝事件 |
| 速率限制 | §8.1 | 没有（也不需要） | `RateLimiter` 抽象 + 令牌桶实现，挂在认证之前 |

### 速率限制要挡的是什么

认证挡住了「冒充身份」，但它自己成了新的攻击面：`Argon2` 的 50~200ms 单次成本
在无限制调用下是**放大器**而非防护，几十个并发失败请求就能把 CPU 打满，认证
本身变成 DoS 面。这不是理论风险——`memory_key_store.py` 用的是 OWASP 2024+
推荐参数（128 MiB × time_cost=4），单次 verify 的内存占用就是 128 MiB。

所以三道防线之外还要补第四件事，且它必须挂在**认证之前**：等认证跑完再限流，
被保护的资源已经消耗掉了。

## 决策

### 决策 1：ROOT 的 actor 是空 `Scope()`，不是 `Scope(org="*")`

security.md §2.2.1 的示例写 `Scope(org="*")`。在本仓这**不能用**：
`SQLitePermissionManager.check` 的第一条规则是 `actor == Scope() → True`
（platform admin 全局放行），而 `org="*"` 会先撞上「跨 org 一律拒绝」规则
——ROOT 反而寸步难行。

连带结论：`AuthContext.actor` **不给默认值**。若给了，「忘了传 actor」会
静默得到空 `Scope()` 即全局权限——最糟糕的 fail-open 形态。

### 决策 2：认证不进 `build_kernel`

认证是**传输层相关**的（凭据从 HTTP header / MCP / CLI 各自的形态来），
内核形态无关。放进 `build_kernel` 会让 `LocalMemoryAPI` 同时承担 AuthN 与
AuthZ 两件事。

落点：`src/common/security/{authentication,protection}/` 提供契约与实现，`Server.build`（bootstrap 层）装配
authenticator，`auth_middleware` 在各 surface 的请求入口调用。内核只接收
已认证的 `identity`。

`common.bootstrap.register_plugins()` 必须在 `KernelConfig.from_dict` **之前**调用——
`authenticator` / `key_store` 两个顶层段名要先进
`Factory.known_top_names()`，否则配置解析期会把它们当未知段拒掉。

### 决策 3：无 argon2-cffi 时 fail-closed，不回退明文

`argon2-cffi` 是 `security` extra 的可选依赖。缺失时 `key_store` 在**装配期**
抛 `ValidationError`，绝不降级为明文比对或 sha256 单轮。security.md §2.3.1
明说那让 key 变成磁盘裸明文；铁律 #3 fail-closed。

启动失败比静默地用一个不安全的存储好。

### 决策 4：MCP 在非 DEV 模式下不可用

MCP 的凭据传递机制（security.md §2.5）需要专门设计。第一期 MCP surface 与
CLI 的 `InProcessClient` 一样过一个**空** `Credentials()`：DEV 模式下可用，
非 DEV 模式下全部工具调用认证失败。

**这是有意的**：在 §8.2「MCP 协议的攻击面」设计落地前，让 MCP 在生产模式下
不可用，好过让它无认证可用。限制已写进 `mcp_server` 的模块 docstring。

### 决策 5：payload 里的 `actor_*` 字段报 400，不静默忽略

删掉读取逻辑后客户端仍会继续发这些字段。静默忽略会让运维以为「我加了
actor_scope 限制」仍然生效，写出错误的安全认知。显式报错迫使调用方改用凭据。

例外：`audit` verb 的 `actor_agent` / `actor_session` 是**查询过滤谓词**
（筛「历史事件的操作者是谁」），与身份声明同名但语义不同，对该 verb 放行。

### 决策 6：默认配置（无 `authenticator` 段）回落 DEV 并 WARNING

不打断任何人的本地开发。第一期只是把「无认证」从**隐式且不可改**变成
**显式、可切换、且非 localhost 时拒绝启动**。

DEV 模式绑非 loopback 地址时进程**拒绝启动**（返回码 1 + stderr FATAL），
而不是警告——警告会被忽略，而这个错配的后果是全部数据。

### 决策 7：限流按调用方地址分桶，不按 `key_fp`

security.md §8.1 的草图按 `key_fp` 分桶。那防的是「单个合法 key 打爆配额」
（配额公平），不是这里要防的「攻击者打爆 CPU」——攻击者每次换一把随机 key 就
换一个新桶，按 `key_fp` 分桶对枚举与耗尽两种攻击都不生效。真正能收敛攻击的是
来源地址。按 key 的配额公平是独立需求，本期不做。

### 决策 8：`allow()` 返回 `bool`，不抛异常

限流是**事实陈述**，翻译成 HTTP 429 是 `auth_middleware` 的事。这与
`PrincipalKeyStore.resolve` 返回 `None` 同理，且不构成 fail-open——调用方拿到
`False` 唯一能做的就是拒绝。

`RateLimitedError` 进 `common/errors.py`（与 `AuthenticationError` 并列）：它
**是**跨层契约，429 与 401 的语义完全不同——一个该稍后重试，一个该换凭据。
回归防线：`test_rate_limited_is_not_an_authentication_error`。

### 决策 9：`peer` 为空串时放行

进程内直连与 MCP stdio 没有网络对端，没有可收敛的攻击面，限流只会把本地 CLI
卡住。所以 `Server.build` 只在 HTTP surface 传 limiter，其余 surface 传 `None`。

### 决策 10：默认按认证 capability 分岔，远程可达实现默认**开**

默认选择不按封闭 `AuthMode` 分支，而读 `requires_loopback_binding()`：仅限 loopback
的实现默认 `unlimited`，显式声明可远程暴露的实现默认 `token_bucket`。这允许业务新增
认证 target 而不修改 Server 枚举分支；网关后部署可显式选择 `unlimited` 把限流交给网关。

非 DEV 默认开而不是默认关：默认关等于「必须读过 §8.1 才知道要配」，而没配的
后果是一个能打挂进程的可用性漏洞。默认开的代价是运维可能撞上 429，但那会伴随
一个明确的状态码和一个明确的配置项；默认关的代价是**没有信号**。

### 决策 11：桶表 LRU 有界

桶按 peer 建，而 peer 由远端决定。无界字典会让这个「防资源耗尽」的组件自己变成
资源耗尽的入口。超出 `max_tracked`（默认 10000）时淘汰最久未活跃的那个——它最
可能已经补满，淘汰等于重建成满桶，不丢有效状态。

关闭限流必须显式配 `target: unlimited`，不能靠把 `capacity` 写成 0：那种反着读
的魔法值在配置文件里读不出意图（`capacity: 0` 是「一个令牌都不给」还是「不
限流」？），而读不出来的配置就是会被写错的配置。参数非法在**装配期**报错。

### 决策 12：Argon2 verify 进程级并发上限（审计 P1-3）

IP 令牌桶限的是「单地址的请求速率」，限不住「同时在跑的 Argon2 verify 数」--
后者才是 CPU/内存耗尽向量：单 IP 30 个并发错误 key = 30 × 128 MiB 同时驻留 ≈
3.75 GiB。新增 `common/admission/concurrency_guard.py` 的 `Argon2Guard`（进程级
`BoundedSemaphore`），
在 `auth_middleware.authenticated` 里 limiter 之后、
authenticate 之前 acquire，耗尽即 429（非阻塞，不排队--排队会让线程无界堆积）。
acquire 成功后用 `finally` 释放。默认上限 4（按「给认证留 512 MiB」算），由
`argon2.max_concurrent` 配置。是否装配由认证实现的
`requires_concurrency_guard()` capability 决定：API_KEY 需要，TRUSTED/DEV 不需要，
未知第三方实现默认需要（fail closed）。Argon2Guard 不进 Factory：
进程级状态按配置实例化多份没有意义，用 `default_argon2_guard()` 取单例。同进程
重复装配不同 `max_concurrent` 报错（不静默忽略）；`max_concurrent=0` 装配期炸
（不用 `or` 吞成默认）。

### 决策 13：加密默认 fail-closed，`allow_plaintext` 默认 False（审计 P2-3）

`LocalEnvelopeEncryptionProvider` 此前默认 `allow_plaintext=True`：读不带 ENC1 magic
的内容直接原样返回。迁移期方便，但迁移完成后，拥有底层存储写权限的攻击者可用任意
明文替换密文，绕过 AES-GCM tag 与 AAD。改为默认 `False`（fail-closed）；迁移期读
旧明文须显式 `allow_plaintext=true`，且应有结束条件（迁移完成后关闭、计数归零）。

### 决策 14：HTTP 两阶段准入与全局连接上限（审计 P2-4 / 验收 P1-HTTP）

此前 `handle_post` 先 `rfile.read(length)` 再进认证/限流，无上限意味着超大 body、
负数/非数字 Content-Length、慢速上传都能耗尽内存与线程。改为**两阶段准入**：
(1) `_parse_content_length` 只校验 header（非数字/负数 -> 400，超 4 MiB -> 413），
不读 body；(2) 提凭据 + limiter/认证--慢连接在读 body 前就被 limiter/认证挡住；
(3) 通过后才 `_read_body` 按已校验长度读。`Handler.timeout`（默认 30s）防单连接
慢速上传占线程；`daemon_threads=True` 让慢请求线程不阻塞进程退出。

验收补强：单连接 timeout 限不住「持续补充连接」的线程耗尽。新增
`_BoundedThreadingHTTPServer`：`process_request` 入口用 `BoundedSemaphore`
（`_MAX_CONCURRENT_REQUESTS` 默认 256）限并发，耗尽直接 503 拒绝（不进 handle、
不占读 body 预算）。release 在处理线程结束（`_process_and_release`）而非 spawn 后，
否则限不住。慢上传与超限连接测试见 `test_http_body_limits.py` / `test_http_slow_upload.py`。

### 决策 15：FS 文件大小硬上限（审计 P2-5 / 验收 P2-FS / 复验 P2-FS）

AES-GCM 整块认证要求把整个明文读入内存再加密（见 `encrypted_fs_store.py` 的已知
代价），无上限意味着一个超大输入能把进程内存吃满。`EncryptedFSStore` 新增
`max_plaintext_bytes`（默认 64 MiB）与 `max_ciphertext_bytes`（默认明文上限 +
安全余量，可显式配）。**读写两侧都用循环有界读取**（`_read_bounded_stream`）：
反复 `read(remaining)` 直到 EOF 或累计达到 `limit+1`，超限即拒。

为何用循环而非单次 `read(limit+1)`：`BinaryIO.read(n)` 允许短读（返回 < n 字节
而未 EOF），单次调用会把第一段当完整文件，造成**静默数据截断**（复验问题 1）。

读取侧 `stat` 只作**快速早拒**，不是唯一边界--stat 与随后 `get` 之间内容可能变化
（TOCTOU），故真正读取仍用循环有界，且解密后**复核**明文上限（密文被替换成另一个
合法但解出超大的信封也要拒）。密文开销不硬编码某个 provider 的精确值（EncryptionProvider
ABC 不暴露 ciphertext bound），用宽松余量，需精确控制时显式配 `max_ciphertext_bytes`。

chunked 加密（第一期不做）落地后可放宽。完整 chunked format（每块绑 chunk index、
防重排截断拼接、spooled buffer）是独立设计，不在本期。

### 决策 16：`Scope` 改为 frozen 值对象（验收第三次 P2-1）

`AuthContext(frozen=True)` 此前只是浅冻结--`actor: Scope` 可变，签发 key 后改原
actor 的 org/user 会让已签发 key 的身份跟着变（越权）。`_Record.actor` 也直接保存
调用方原始引用。`Scope` 改为 `@dataclass(frozen=True)`：身份/隔离是值对象，可变性
是安全缺陷；改某维用 `dataclasses.replace(scope, org=...)` 返回新值。影响面仅
`kv_space_manager` 两处原地修改（已改为 `replace`），不改变入参出参类型契约。

FS 短读修复（验收第三次 P2-2）：`_read_bounded_stream` 改用 `bytearray` 累积而非
`list[bytes]` + `join`--恶意 1-byte 短读会让 list 长出百万级元素，8 MiB 内容放大到
~700 MiB。bytearray 是连续缓冲区，内存与字节数成正比，不随分片数放大。

## 落地范围与现行契约索引

本特性落地了请求级 `AuthContext`、认证/凭据/准入三个 capability、DEV 绑定 guard 与
统一认证中间件。接口签名和错误语义不在 feature 文档重复维护：

- 认证上下文、Authenticator capability、YAML 选择与启动不变量：S09；
- `AuthContext`、Factory 与公共类型：S07；
- 角色授权与 agent 代操作：S03；
- 当前实现文件、注册 target 和本地行为铁律：`src/common/AGENTS.md`。

这一分工避免 feature 中的历史设计草案被误当成现行公共 API。

## 配置草案

> 以下是 2026-07-29 的草案形态（顶层嵌在 `memory_api` 下）。**现行配置形态见
> [S09 §注册与配置](../../specs/S09-security.md)**：安全能力由顶层 `security` 段组合，
> 各能力段与本草案的 target 名一致，但不再嵌套在 `memory_api` 下。

```yaml
memory_api:
  authenticator:
    default:
      target: api_key            # dev（缺省） / trusted / api_key
      params:
        root_api_key: ${AGENT_MEMORY_ROOT_KEY}   # 部署级凭据，不入注册表
        key_store: shared                        # 引用下方具名实例

  key_store:
    shared:
      target: memory             # 进程内；生产需 SQLite 后端（见「已知遗留」2）
```

TRUSTED 模式：

```yaml
memory_api:
  authenticator:
    default:
      target: trusted
      params:
        gateway_key: ${GATEWAY_SHARED_SECRET}   # 默认必须配置；缺则装配期拒绝启动（决策 P1-2）
        key_store: shared
```

网关须注入 `X-Org-Id` / `X-Principal-Type`（`user` \| `agent`）/
`X-Principal-Id`。**角色不从 header 读**——框架查
`PrincipalKeyStore.get_role`。

CLI 在 `--server` 模式下带 key：`--api-key`，缺省读环境变量
`AGENT_MEMORY_API_KEY`（让 key 不出现在 shell history 与 `ps` 输出里）。

速率限制（不配整段时按认证模式给默认，见决策 10）：

```yaml
rate_limiter:
  default:
    target: token_bucket        # 或 unlimited
    params:
      capacity: 30              # 突发额度
      refill_per_sec: 5.0       # 持续速率
      max_tracked: 10000        # 桶表上界（LRU 淘汰）
```

默认值面向「交互式使用不该被限流，脚本化枚举必须被限流」这条线：30 个突发够
任何人工操作和常规客户端启动时的几次探测；持续 5 QPS 远低于 Argon2 verify 打满
一个核所需的速率。

## 破坏性变更

| 变更 | 谁受影响 | 迁移方式 |
|---|---|---|
| payload 的 `actor_*` 字段报 400 | 显式传这些字段的客户端 | 删掉这些字段，改用凭据 |
| 非 DEV 模式下无凭据请求返回 401 | 所有现有客户端 | 保持默认（DEV），或签发 key 并带 `Authorization: Bearer` |
| MCP 在非 DEV 模式下全部工具调用失败 | MCP 客户端 | 第二期解决；当前用 DEV |
| `Kernel` 新增 `audit` 字段 | 直接构造 `Kernel(...)` 的代码 | dataclass 带默认值字段，向后兼容 |
| `Server.__init__` 新增 `authenticator` 参数 | 直接构造 `Server(...)` 的代码 | 带默认值 `None`，向后兼容；但 `dispatch` 需要中间件已挂载，否则 401 |
| `handler.dispatch` 在无认证上下文时返回 401 | 直接调 `dispatch` 的测试与脚本 | 用 `set_current` / `authenticated` 包一层 |
| 非 DEV 模式下 HTTP 请求默认受限流（30 突发 / 5 QPS） | 高频客户端、压测脚本 | 调 `rate_limiter` 段的参数，或配 `target: unlimited` |

**默认配置下（无 `authenticator` 段 → DEV）现有行为不变**：所有请求得到 ROOT，
且不限流。

一个**部署形态**注意事项：网关后部署（TRUSTED 模式的常见形态）所有请求共用网关
出口 IP，会被当成同一个 peer。这种部署应显式配 `target: unlimited` 把限流交给
网关，或按聚合流量调大 `capacity`。

## 验证

| 文件 | 覆盖 | 结果 |
|---|---|---|
| `tests/unit/common/security/test_types.py` | `AuthContext` frozen / `actor` 无默认 / ContextVar 线程隔离与 reset | 11 passed |
| `tests/unit/common/security/authentication/test_authenticator.py` | ABC 契约 / Producer 注册 / bootstrap 幂等 | passed |
| `tests/unit/common/security/authentication/test_key_store.py` | issue / resolve / revoke / ROOT 禁签 / **timing pad** / 不存明文 | passed |
| `tests/unit/common/security/authentication/test_authentication_impl.py` | 三实现的正反路径 / 错误消息一致 | passed |
| `tests/unit/common/security/protection/test_binding_policy.py` | loopback 绑定策略的各类拒绝 | passed |
| `tests/unit/common/security/protection/test_rate_limit.py` | 突发/补充/并发/LRU/空 peer/装配期参数校验 | 16 passed |
| `tests/unit/bootstrap/test_auth_middleware.py` | header 归一 / bearer 提取 / **reset 保证** / 限流接线 | 28 collected |
| `tests/integration/test_identity_forgery_rejected.py` | **端到端伪造身份被拒** | 5 passed |

`tests/unit/common/security/` 当前共 169 passed（F05 迁移后含 `test_runtime.py`
与密码学子目录）；
`tests/unit/bootstrap/test_server_security_config.py` 另有 4 条配置歧义与开放 target 回归。

限流侧的关键断言：

| 断言 | 落点 |
|---|---|
| 超出突发额度即拒绝；补充速率生效后恢复 | `test_burst_up_to_capacity_then_denied` / `test_tokens_refill_over_time` |
| **并发下不超发**（多线程抢最后一个令牌） | `test_concurrent_requests_do_not_exceed_capacity` |
| 桶表 LRU 有界，不随 peer 数无限增长 | `test_bucket_table_is_bounded` / `test_eviction_drops_least_recently_used` |
| `peer` 为空串放行 | `test_empty_peer_is_never_limited` |
| **限流跑在认证之前**（认证器一次都没被调到） | `test_rate_limit_runs_before_authentication` |
| 429 与 401 可分；限流拒绝不留下上下文 | `test_rate_limited_is_not_an_authentication_error` / `test_rate_limited_leaves_no_context` |
| 审计里限流与认证失败分得开，且**不记桶余量** | `test_rate_limit_denial_is_audited_distinctly` / `test_rate_limit_audit_carries_no_bucket_state` |
| 关闭限流只能显式配 `unlimited`，`capacity: 0` 报错 | `test_disabling_is_explicit_not_a_magic_value` |

> 「不记桶余量」是一条容易漏的：余量能用来反推限流参数，然后贴着阈值发请求。
> 审计 detail 恰好只有 `mode` 与 `peer` 两个键，测试用 `set(detail) == {...}`
> 精确断言，多一个键就红。

### 招牌测试的实现顺序

按 CLAUDE.md §5「写测试先于修 bug」：先写
`test_identity_forgery_rejected.py`、跑一遍**看它全红**（证明漏洞真实存在）、
再做改动、再看它全绿。其中 `test_identity_comes_from_context_not_payload`
比 `test_claimed_identity_in_payload_is_rejected` 更重要——前者证明「堵上
之后认证与授权确实串起来了」，后者只证明「洞堵上了」。

### timing 测试的 flaky 防护

`test_resolve_pads_time_on_miss` 各跑 5 次取**中位数**（不是平均，避免单次
GC 抖动主导），断言比值在 `[0.5, 2.0]`。区间宽是因为要检出的是「差一整个
Argon2 verify」（~100x），不是微小偏差。

> **这条测试实测抓到过一个真实缺陷**：初版实现里「前缀有候选但 key 错」
> 会跑**两次** verify（候选一次 + 落空后的 dummy 一次），而「前缀无候选」
> 只跑一次，ratio=0.49。修的是实现不是断言——加 `verified_any` 标志，
> 只在没跑过任何候选 verify 时才补 dummy。三条路径各恰好一次。

### 手工验收（不进自动化测试）

- DEV 模式 + `--host 0.0.0.0` → 进程返回码 1，stderr 有 FATAL ✓
- DEV 模式 + `--host 127.0.0.1` → 正常启动 ✓
- API_KEY 模式下 `/healthz` 无凭据 → 200 ✓
- API_KEY 模式下 `POST /v1/add` 无凭据 → 401；带 root key → 200 ✓
- `examples/quickstart.py` 行为不变——注意它**改动前就有一个既有失败**
  （最后一步 `admin_all` 用普通 user 身份调管理面得 `PermissionDeniedError`）。
  改动后仍是**同一个**失败 ✓

## 拒绝的方案

### 拒绝 1：认证做进 `build_kernel`

内核形态无关，认证是传输层相关的；且会让 `LocalMemoryAPI` 同时承担 AuthN
与 AuthZ。见决策 2。

### 拒绝 2：无 argon2-cffi 时回退明文存储

security.md §2.3.1 明说那让 key 变成磁盘裸明文；铁律 #3 fail-closed。
装配期抛错，见决策 3。

### 拒绝 3：`get_current()` 返回默认 `AuthContext`

fail-open。中间件漏挂时请求会带着默认身份跑完，而且**没有任何症状**——
系统看起来完全正常，直到有人发现所有操作都以同一个身份记在审计里。
返回 `None` 迫使调用方显式处理。

### 拒绝 4：静默忽略 payload 里的 `actor_*` 字段

见决策 5。

### 拒绝 5：`AuthDispatcher` 一个类里 if/else 分流三种模式

参考 demo 的写法。拆成三个各自只做一件事的 `Authenticator` 实现 + Producer
按配置选：模式在**装配期**选定，运行期不再分流。一个 if/else 分流器意味着
每次请求都要重新判断「我是哪种模式」，而那是启动时就确定的事。

### 拒绝 6：错误消息区分「主体不存在」与「凭据错误」

区分即主体枚举侧信道（§2.3.2）。三个 authenticator 一律抛
`"authentication failed"`。具体原因应写进审计——但见「已知遗留」9。

### 拒绝 7：限流按 `key_fp` 分桶

见决策 7。攻击者每次换一把随机 key 就换一个新桶。

### 拒绝 8：`capacity: 0` 表示不限流

反着读的魔法值在配置文件里读不出意图，而读不出来的配置就是会被写错的配置。
关闭限流走显式的 `target: unlimited`，见决策 11。

## 已知遗留

1. **Argon2 128MiB×4 使单次 `resolve` 约 50~200ms**，API 吞吐上限约
   5~20 QPS/核。高 QPS 需要带撤销传播的验证缓存（第二期）。参数取
   OWASP 2024+ 推荐值，不下调。
2. **`InMemoryKeyStore` 进程重启即丢全部 key**。生产需 SQLite 后端。
   注册名是 `memory`（Argon2 是内部实现细节，不是后端名）。
3. **MCP 在非 DEV 模式下全部工具调用失败**。§2.5 的凭据传递待第二期设计。
   见决策 4。
4. **限流是进程内的，多副本各算各的**：N 个副本 = N 倍实际额度。真正的多副本
   限流要 Redis 之类的共享计数器，届时在 `security/protection/protection_impl/` 下新增一个实现，
   中间件不用改（契约已留在 `common/security/protection/rate_limit.py`）。
5. **按地址分桶挡不住僵尸网络**：来源足够分散时每个 IP 都拿到一个新满桶。能
   收敛这种攻击的是**对 Argon2 verify 本身做并发上限**（一个信号量，把同时
   进行的 verify 数压到内存能承受的范围）--已由决策 12 的 `WorkloadGuard` 实现。
6. **无按 key 的配额公平**。§8.1 草图里的 `key_fp` 分桶防的是「单个合法 key
   打爆配额」，与本期防的攻击不是一件事（决策 7）。它是独立需求。
7. **审计无链式 HMAC 完整性保护**。§7.3，第二期。
8. ~~**`handler.py:_event_view` 硬编码 `Scope` 四字段**
   （`org` / `user` / `agent` / `session`）。F03 加 `space` 后这里会漏字段。~~
   **不成立**：上游 `c76eb90` 落地五维 `Scope` 时已一并改全了三处渲染点——
   `handler.py:_scope_view`、`storage/_support.py:scope_segments`（五段占位）、
   `SqliteAuditLogger` 的 `actor_space` 列（含 `ALTER TABLE` 迁移）。写下这条时
   只查了四维版本的 handler，没复核上游同批提交，是我的疏漏。
9. **`/healthz` 返回 profile 名**，未做信息暴露评估。profile 名是部署配置的
   一部分但不是秘密，且改它会破坏现有客户端的 `healthz()` 契约，第一期保持
   原样。
10. **`Role.ADMIN` 无任何管理接口**。角色枚举已定义但**无任何消费方**：
    `PermissionManager.check(actor, target, action, context)` 的签名里没有 role
    的位置，故 §3.2 权限清单里「管理本租户 user/agent」「创建/删除租户」
    「系统级配置修改」三行**无法表达**，ADMIN 与 USER 走完全相同的判定路径。
    §3.5 的「提升式 ROOT」同理不存在（`check` 首条是 `actor == Scope()`，认的是
    actor 形状不是 role）。这是授权侧的缺口，归隔离/权限那一期。
11. **认证失败审计不记细分原因**。`_record_failure` 只记
    `mode` + `peer`。真实原因（`missing_credentials` / `unknown_principal` /
    `bad_gateway_key`）需要在 authenticator 侧另开一条**只进审计**的通道
    ——异常消息必须保持笼统（拒绝 6）。那是独立设计，不塞进本期。
12. **`AuditEvent` 缺 `acting_user` / `role` / `key_fp` / `auth_mode` 字段**。
    security.md §7.2 要求记录这四样，第一期塞进 `detail`（`dict[str, str]`）
    并在 `audit.py` 的「常见约定」注释里登记。不改 `AuditEvent` 结构——那是
    跨层结构体，改它要动 `common` / `control` / 两个 `AuditLogger` 实现 +
    `handler._event_view`。若这些键稳定使用，第二期应提升为一等字段。
13. ~~**`Scope` 仍是四维**。安全模块按四维实现，但所有 `Scope` 构造一律用
    **keyword 参数**，为 F03 插入 `space` 留接缝（位置参数会错位）。~~
    **已解除**：上游 `c76eb90` 落地了五维 `Scope`（`space` 是 `kw_only`）。
    因为构造全用 keyword，安全模块无需任何改动即兼容；`_FORBIDDEN_IDENTITY_KEYS`
    跟着补了 `actor_space` / `actor_space_id` 两个新伪造面
    （`test_space_dimension_identity_claims_are_rejected`）。
