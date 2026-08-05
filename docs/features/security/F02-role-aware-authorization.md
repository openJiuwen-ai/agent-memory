# F02 - 角色感知授权与 Agent 代操作委托

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-29 |
| 影响范围 | **修改**：`src/control/permission.py`、`src/control/permission_impl/sqlite_permission_manager.py`、`src/control/permission_impl/allow_all_permission_manager.py`、`src/control/permission_impl/routing_permission_manager.py`、`src/api/memory_api_impl/local_memory_api.py`、`src/common/authentication/authentication_impl/trusted_authenticator.py`、`src/common/type_def/auth.py`、`docs/specs/S03-control.md`、`src/api/AGENTS.md`、`tests/unit/api/test_build_kernel_config.py`<br>**新增**：`tests/unit/control/test_permission_role_aware.py`、`tests/unit/api/test_authorization_with_auth_context.py`；**补充**：`tests/unit/common/authentication/test_authentication_impl.py`（委托用例） |
| 测试基线 | 改动前 `15 failed, 657 passed, 60 skipped`；改动后 `15 failed, 710 passed, 60 skipped`。**15 个失败是同一组**（`test_jieba_tokenizer.py` 的 `jieba` 未装、`test_bge_m3_embedder.py` 的 `torch` 未装、`test_local_encryption_provider_encrypts_enc1_and_round_trips` 的 Windows POSIX 权限位限制），与本改动无关 |
| 依据 | [`docs/features/common/F04-security-interfaces-and-encryption.md`](../common/F04-security-interfaces-and-encryption.md) §3.1 角色、§3.2 操作与角色映射、§3.5 ROOT 等价性、§4.3 路径 1（Agent 代操作）、§9 铁律 #3（fail-closed） |
| Refs | — |

> **行文简称**：下文里的 **security.md** 一律指上表「依据」那份文档（详见
> F01 的同名说明）。F01 造出了 `AuthContext` 的 `role` 与 `acting_user` 两个字段，
> 本期把它们接进授权判定--两份文档是同一根线的前后两段。

## 背景

F01 落地后，`AuthContext` 的 `role` 与 `acting_user` 是两个**孤儿字段**：认证层算出来、
进审计 detail、然后在授权边界被丢掉。这留下三个缺口：

### 缺口 1：ROOT 由 actor 形状识别，不是由 role

`SQLitePermissionManager.check` 第一条规则是 `actor == Scope()` 即全局放行。这是
**声明式 ROOT** 的 actor 形态。但 security.md §3.5 明写「提升式 ROOT」（绑了具体
org/user、`role=ROOT`）与它在运行时权限检查中**等价**。今天不等价：一个提升式 ROOT
在 PDP 眼里就是普通用户。

更严重的是反方向：PDP **没有纵深防御**。`PrincipalKeyStore.issue` 拒绝签发空 actor 的
key，但那道闸在 `security/` 层。换一个 authenticator 实现、或将来加 OAuth 通道，没人
保证那个前置假设还在。`AuthContext(actor=Scope(), role=USER)` 这种「空 actor + 非ROOT
role」的产物今天能拿到全局放行--靠的是数据形状的巧合，不是显式判定。

### 缺口 2：agent 代 user 操作不可能放行

`_owner_scope_covers(Scope(agent="a1"), Scope(user="u1"))` 恒 `False`：primary 维
（默认 `user`）不等。grants 表里也没有这条。所以 §4.3 路径 1（用户授权 Agent 代操作）
必然 403--`acting_user` 这个字段没有任何消费方。

### 缺口 3：PEP 的 `identity` 与 ContextVar 的 `AuthContext` 可以不一致

`_authorize(identity, ...)` 的 `identity` 是调用方传的；`AuthContext` 在 ContextVar 里。
两者指向不同主体时没有任何东西强制相等。今天 handler 传的就是 `get_current().actor`，
恒等；但直接调 `LocalMemoryAPI` 的代码（另一个 surface、一段脚本）可以传一个不相干的
`identity`，接线前那会被当成真身份。

## 决策

### 决策 1：`auth` 是 keyword-only，默认 `None`，`actor` 保留

```python
def check(self, actor, target, action, context=None, *, auth: AuthContext | None = None) -> bool
```

`auth` 放 keyword-only 且默认 `None`：33 处既有 `_authorize` 调用点、所有单测、
`build_kernel` 直连路径都不传它也能跑--`None` 时退回纯 ACL。`actor` 保留：它是 ACL
的主语，且 `auth is None` 的兼容路径要用它。`auth` 不是 `actor` 的替代，是 `actor`
**推不出来**的两样东西（`role`、`acting_user`）的载体。

### 决策 2：`auth.actor != actor` 即拒绝（fail-closed，铁律 #3）

两个身份来源不一致，要么是接线错误要么是攻击，两种都拒。返回 `False` 而非抛异常--
`check` 的契约是给出布尔判定，`PermissionDeniedError` 留给 PEP 翻译成 403。

### 决策 3：ROOT 按 role 判定；空 actor 降级为兼容回退

`auth is not None` 时：`role is ROOT` 即全局通过；**空 `actor` 不再**自动等于
platform admin（见缺口 1 反方向）。`auth is None` 时保留旧的 `actor == Scope()`
规则--那是后台 job、单测、`build_kernel` 直连的路径，没有认证上下文，不该被角色闸门
打红。

> 实现注记：空 actor 的显式拒绝不能省。否则它会命中 `_owner_scope_covers` 顶部的
> 「parent 为空即覆盖一切」通配分支--那个分支是给 grant 行匹配用的，不该被 actor
> 借道。这是实现中暴露的第三处「靠形状表达语义」的坑。

### 决策 4：委托在 owner-cover 之后、grants 查询之前

`_delegation_covers(auth, target, action)` 条件全部取自服务端认证产物，**没有一项来自请求体**：

- `auth.actor.agent` 非空（只有 agent 主体能代操作，反向不成立）；
- `auth.acting_user` 非空；
- 同 `org` + `space`（org 是硬边界 §4.2；同名 user 在别的 space 不是同一份数据）；
- `target.user == acting_user`（委托目标只能是该 user 本人）；
- `target.agent` 为空（代 user 操作的目标是该 user 的分支，不是它名下另一个 agent 的分支）。
- `action` 落在 `_DELEGATABLE_ACTIONS`（READ/WRITE/UPDATE/DELETE）内：委托只覆盖记忆
  CRUD，**不含 SHARE**--否则 agent 拿到一次请求级委托后，可对 `acting_user` 的
  scope 发 SHARE 给自己写长期 Grant，把临时委托升级成永久访问（审计 P1-1）。
  用显式 allowlist 而非「非管理面即允许」，是为了让新增 Action 默认**不**落入委托。

放在 owner-cover 之后：能被 owner-cover 放行的不必走委托。放在 grants 之前：委托是
比显式 grant 更强的声明（「我就是替这个 user 做的」），先判它能让代操作不必额外建
grant 记录。

### 决策 5：管理面靠 `resource_type`，不靠 target 形状

`_management_plane_denies(auth, action, context)` 要求 ROOT 的资源由
`PermissionContext.resource_type` 表达：

- `admin` / `audit`：任何动作都要 ROOT；
- `space` + `WRITE`/`DELETE`：创建/删除租户要 ROOT（§3.2）。同为 `resource_type="space"`
  的 `get`/`update`/`archive` 走 READ/UPDATE，不在此列--否则普通用户连自己所在 space
  的名字都拿不到。

> **实现中对计划的修正**：计划初稿把 `grant` 也列进管理面。落地时否掉了：§3.2 那行
> 说的是「**跨租户**修改权限」，而跨 org 的 grant 今天已被 `actor.org != target.org`
> 挡住；对自有 scope 发 grant 是 Grant 模型的主用途，闸进 ROOT 会废掉正常共享。见
> `test_sharing_own_scope_is_not_a_management_operation`。

`auth is None` 时不闸：没有认证上下文时无从判定角色，沿用旧 ACL。

### 决策 6：`AuthContext` 在 PEP 取，不在 PDP 取

`LocalMemoryAPI._authorize` 调 `get_current()` 取出后透传。`PermissionManager` **不得**
自行读 ContextVar：PDP 应当是其入参的纯函数，否则单测要先布置环境态才能跑，判定依据
也不再显式可见。`get_current()` 返回 `None` 时 PDP 退回纯 ACL。

> **接线验证**：`set_current` / `reset_current` 的调用方在 `bootstrap/core/auth_middleware.py`
> 的 `authenticated()` 上下文管理器（line 86/90），HTTP / MCP / CLI 三个 surface 都用它
> 包住 dispatch。故真实请求路径里 ContextVar 会被填充，`get_current()` 在 PEP 拿得到值。
> 这条在实现时专门核过--它正是「看起来通了、真请求时没通」那一类坑。

## 拒绝的方案

| 不做 | 为什么 |
|---|---|
| `Role.ADMIN` 的额外权限 | §3.2 属 ADMIN 的那行（管理本租户 user/agent）在本仓一个接口都没有：`PrincipalKeyStore.issue` 只有测试调用方。凭空造闸门守一扇不存在的门是 dead flexibility（CLAUDE.md §3）。`test_admin_role_is_not_enough_for_admin_plane` 钉住当前行为；租户管理面落地时它应当改，改动会撞在这里，那正是它存在的意义。 |
| `require_role` 装饰器 | 它会在 PEP 之外造第二道角色检查点，而两道点不一致时没有规则说谁赢。角色判定留在 PDP 一处（决策 5），PEP 只管取上下文透传。 |

这两项不是遗漏，是**诚实的范围**：PR② 接通已存在的接口所需要的东西，不造没有消费方的
接口。

## 落地影响（现行契约见 S03 / S08）

授权调用新增可信认证上下文输入，三个 PermissionManager 实现各自对齐：

- `SQLitePermissionManager`：决策 2~5 的全部判定；
- `AllowAllPermissionManager`：忽略 `auth`，恒 `True`（它的全部语义就是「不鉴权」，掺进角色逻辑只会让这个前提变得需要逐条确认）；
- `RoutingPermissionManager`：原样透传 `auth` 给 delegate（路由不改变授权语义，吞掉 `auth` 会让角色闸门与委托在路由型部署下静默失效）。

`TrustedAuthenticator` 增加 `X-Acting-User` header 的读取：user 主体 `acting_user` 是
它自己（与改造前逐字一致）；agent 主体读该 header。`_acting_user` 的 docstring 记了
「为什么这个 header 可信」与「为什么它和 `role` 不同处理」的对照。

## 验证

- `tests/unit/control/test_permission_role_aware.py`：PDP 自身当前 22 条判定（角色闸门、
  委托边界、管理面、向后兼容、另外两个实现的透传）。
- `tests/unit/api/test_authorization_with_auth_context.py`：PEP 接线当前 8 条（认证上下文
  确实穿过 API 抵达 PDP，含提升式 ROOT 用管理面、agent 代 user 读写、identity 与
  auth.actor 不一致被拒）。
- `tests/unit/common/authentication/test_authentication_impl.py`：`acting_user` 生产方 3 条（user 主体
  自带、agent 无 header 为空、agent 带 header 透传）。

向后兼容由 `test_no_auth_context_preserves_every_legacy_rule` 与既有 permission 测试
逐字不变地撑着：`auth=None` 时回到纯 ACL。

## 已知遗留

- ~~`auth=None` 兼容线仍服务于内核直连、后台任务和既有测试~~——已在 F05 迁移中删除，
  见下节。
- ADMIN 的租户管理接口尚未落地，因此本特性不凭空增加 ADMIN 管理面权限；对应接口出现时
  需扩展 S03 与角色门槛测试。

## 后续演进（F05 Common Security 迁移，2026-08-05）

本文档记录的是**当期**（2026-07-29）的落地事实，保留原貌以备追溯。F05 把安全收敛成
横切能力域后，下列描述**已被取代**，现行契约以 [S08](../../specs/S08-security.md) 为准：

| 本文档中的描述 | 现状 |
|---|---|
| PDP 是 `control.permission.PermissionManager.check`，返回 `bool` | PDP 是 `common.security.authorization.Authorizer.authorize`，返回带 `reason` code 与 `rule` 的 `AuthorizationDecision`。`PermissionManager` 只剩 grant/revoke 的记录写入通道 |
| `auth: AuthContext \| None`，`None` 时退回纯 ACL | 输入封闭为 `AuthContext + ResourceDescriptor + AuthorizationEnvironment`，无 `None` 分支 |
| 空 `Scope()` 是 platform admin（`auth is None` 时） | 空 actor 直接拒（`CONTEXT_MISMATCH` / `empty_actor`）；ROOT 只由 `role` 表达，dev/root 主体改为具名的 `system/dev`、`system/root` |
| `AuthContext.acting_user` 表达代操作 | 该字段已删除。委托只来自服务端 `DelegationStore`，由 Authorizer 按 `delegation_id` 复核；可委托动作见 `DELEGATABLE_ACTIONS`（不含 SHARE 与管理动作） |
| PEP 从 ContextVar 取 `AuthContext` 后透传 | 调用方显式传 `security: RequestSecurityContext`；ContextVar 降级为日志/trace 辅助传播，授权不依赖它 |
| `MemoryAPI.method(..., identity=caller)` | `MemoryAPI.method(..., security=RequestSecurityContext)`，必填 keyword-only |
| 管理面一律要求 ROOT | 按动作分级：`MANAGE_*` / `READ_AUDIT` 要 ADMIN 及以上（止于本 org），`VERIFY_AUDIT` / `ADMINISTER_SYSTEM` 与无 org 归属的系统级资源要 ROOT |
| 验证用例路径 `tests/unit/control/`、`tests/unit/common/authentication/` | 安全单测镜像到 `tests/unit/common/security/<能力域>/` |

`TrustedAuthenticator` 的 `X-Acting-User` 读取随 `acting_user` 字段一并移除——代操作
不再由请求 header 声明。
