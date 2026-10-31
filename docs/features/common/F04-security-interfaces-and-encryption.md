# F04 — 安全能力与设计决策

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-31 |
| 影响范围 | `jiuwen_memory/common/security/`、API PEP、各接入形态、存储加密及相关模块规约 |
| 验证范围 | 安全契约、第三方替换、故障、生命周期及各接入面业务回归；见「验证要求」 |
| 规范契约 | [S10 安全横切契约](../../specs/S10-security.md)、[S02 记忆接口层](../../specs/S02-memory-api.md)、[S07 公共组件层](../../specs/S07-common.md)、[S06 存储层](../../specs/S06-storage.md) |
| Refs | #143、#270 |

## 背景与维护边界

早期请求入口从 payload 的 `actor_*` 字段生成调用身份，攻击者可冒充另一个已知主体。
安全改造先固定接口，再分期实现认证/加密、授权/隔离与审计完整性。认证解决「你是谁」，
授权解决「你能对哪些资源做什么」，数据保护解决「绕过 API 取得落盘内容后能否读取或篡改」。
三者不可互相替代；限流、并发预算与绑定策略同时保护认证和服务可用性。

本页统一描述当前安全能力、设计取舍和边界，不按提交、同步日期或整改轮次追加流水。
早期 demo、异步接口草图和 PR1 过渡方案不作为当前实现要求；历史文档由 Git 保留，
逐次复现与验证笔记只在本地 security-plans 保存。本页不是“所有问题已关闭”的证明。

维护约定：安全能力变化时刷新本页现行描述，不因上游合并、基线适配、门禁与验收等工作
在 `docs/` 新建文档或追加过程笔记。现有 specs 继续承载契约、模块 AGENTS.md 继续承载
实现地图，涉及它们的事实纠偏同步更新既有文件。阶段性接口备忘已整合到既有 specs，
安全能力特性文档只保留 F04。MemoryAPI 业务签名保持稳定，底层插件契约按公开能力演进。
本次不合并或删除独立的模型服务 SSL、存储 SSL 等上游专题文档。

源码中的历史 `F05 §...` / `security.md` 简称是早期安全架构来源注记，不指 SSL 文档；
现行设计以本页、冻结契约以既有 specs 为准，不执行旧草图中的接口或默认值。

## 1. 能力状态与威胁边界

| 能力域 | 当前状态 | 边界 |
|---|---|---|
| 认证与凭据（PR1） | 已合入上游；dev / api_key / trusted、memory KeyStore 与凭据状态 Registry | OAuth 资源服务器、持久化 PrincipalKeyStore 未实装 |
| 资源保护（PR1） | loopback BindingPolicy、令牌桶/不限流、信号量 WorkloadGuard、有界 HTTP | 令牌桶与并发预算是进程内状态，不提供跨副本总配额 |
| 静态加密（PR1 / PR1-1） | local KeyProvider、ENC1 KV/FS 装饰器、统一装配开关及索引部署保护契约 | 索引依赖加密卷或后端加密；远端依赖部署方核查；KMS/Vault/HSM 未实装 |
| 授权与隔离（PR2） | 唯一 PEP/PDP、Grant/Delegation 真源、角色和空间两轴、代理代写 | 私有委托配置不等于动态管理公开接口 |
| 审计记录 | 普通 AuditLogger、认证/业务判定留痕、真实 actor 和请求关联 | 普通日志没有链式防篡改保证，保留期由部署方管理 |
| 审计完整性（PR3） | 可选 chained_hmac、内存 / SQLite 链式 Store、LocalKeyProvider MAC 与受保护审计 | 外部锚点无产品实现；未装配返回 unsupported，本地密钥轮换状态不跨重启保存 |

身份和目标资源必须分开。`AuthContext.actor` 恒为 org 非空、user/agent 恰一的已认证
单主体；资源 `Scope` 则可以按 `principal_path` 表达 user/agent 层级。资源坐标、URI、
metadata、业务请求头不能替代认证身份。`Role.ROOT` 来自服务端认证事实，不从空 Scope
或目标形状推导；普通主体不能通过签发接口自行取得 ROOT。

隔离和加密不覆盖所有攻击面：向量、摘要、缓存、embedding 队列与原始资产应分别评估
暴露面；已认证但恶意的主体仍受授权限制。TLS 终止、网关信任链、Secret 分发、数据库/文件
权限、备份与日志保留属于部署边界，不能因 API 鉴权或 KV 加密就宣称全部受保护。

## 2. 认证与可信上下文

### 2.1 开放实现与具名身份

认证、授权、防护、密码学能力经 Factory/Producer 和具名依赖组合；差异由 capability
声明，不按封闭 AuthMode 枚举或 target 白名单在核心分支。宿主须在配置解析前注册插件，
没有外部 entry-point 自动发现。SDK 一次 `assemble_runtime(config=...)` 即可取得
`runtime.authenticate(Credentials(...))` 和 `runtime.api`；认证返回 SDK surface 的
可信上下文，业务方法仍显式传 security。认证使用同次装配的 Authenticator/Registry
和并发预算；缺少安全配置、已关闭或预算耗尽时明确拒绝，不隐式启用 DEV。
独立 `build_configured_security_runtime(config)` 每次新建安全图，只用于独立装配或
显式注入，不能用它再次构建配置来寻找已有 SDK 的安全实例。
请求认证在接入边界执行，API 消费其结果；composition
root 装配同源 Runtime 不意味着把协议解析或认证执行塞入 MemoryAPI。

- DEV 无映射时产出 `local/developer` USER；Root API Key 产出 `system/root` ROOT。
  不使用空 actor 或 `org="*"` 表达特权。DEV 仅用于显式本地开发，并受回环绑定限制。
- DEV 映射用服务端预设标识选择具名身份，未知或缺失标识拒绝。映射是开发测试能力，
  不是生产凭据系统；HTTP 显式配置已有 Runtime 时不被便捷映射覆盖。
- 固定 DEV 通过认证器 `params.role` 显式选择 user/admin/root，缺省仅 USER。默认无认证头
  请求不获得跨 org/user 特权；这是最小权限边界，替代早期为业务兼容默认 ROOT 的方案。
  需要跨组织联调的脚本须显式配置 ROOT。role 不取请求头或 payload；固定 role 与身份映射
  互斥，映射仍按每条配置决定角色；非法值启动拒绝。所有 DEV 继续限制本地回环。
- 默认 USER 同时收紧管理面：`audit`、`verify_audit`、`admin_get/set/all` 的系统根资源
  需要显式 ROOT；`create_space` 可由本 org ADMIN 执行，跨 org 需 ROOT。成员、空间策略及
  空间状态管理不等同于组织角色：空间授权启用时仍按归属/有效成员治理轴及授予上界判定，
  USER 有治理权仍可成功，ADMIN 不是全空间超级管理员。MANAGE_PRINCIPAL/MANAGE_POLICY
  是最低 ADMIN 的动作契约，不是这些空间入口的实际动作或新发布的主体网络管理接口。
  各形态加载方式、完整受影响入口及角色迁移见现有 [配置指南](../../zh/API文档/config.md#默认-user-的管理入口迁移)。
- API Key 先按常时间 bytes 比较部署级 Root Key，再由 PrincipalKeyStore 验证主体 key。
  非 ASCII 错误 key 不应将认证拒绝变成 TypeError/500。主体 key 禁止签发 ROOT。
- Trusted 必须有明确网关信任边界；身份材料由网关注入，角色从服务端 Store 查询，
  不是信任调用方自述 role。默认要求 gateway_key；只有显式 allow_no_gateway_key
  并已建立受信反代/mTLS 等网络边界时才允许不配，不作为普通部署缺省。
- HTTP / CLI / MCP 默认 required；未配置认证时 fail-closed，DEV 必须显式选择。
  不恢复 PR1 早期的隐式 DEV 或 `permission.default=allow_all` 兼容注入。

PrincipalKeyStore 的 memory 实现保存 Argon2id hash，不保存明文；SHA-256 fingerprint
用于定位和凭据引用，不替代慢哈希验证。当前显式参数为 time_cost=4、memory_cost=128 MiB、
parallelism=2、hash_len=32、salt_len=16；这是当前实现参数，不在本页宣称外部标准合规。
缺 argon2-cffi 在装配期拒绝，不降级到明文或单轮 SHA-256。未命中路径补 dummy verify，
避免明显的存在性时差；已经 verify 过候选时不再重复补一次。测试取中位数检测量级差异，
不是对网络端到端恒定时间的承诺。

### 2.2 来源、时效与在线撤销

`RequestSecurityContext` 只由受控入口签发：服务端 request_id、开始时间、surface、peer
和可信 attributes 与认证事实绑定。进程内调用也须经显式 Authenticator；ContextVar
仅用于日志/trace，不作为授权身份真源，请求退出必须 finally reset，未认证不返回默认身份。

固定的 `Scope` 仍是可变 dataclass。安全边界复制 actor 与 Store 内对象，来源证明绑定
全部安全维度；构造后原地修改 actor 或替换可信属性会使 PEP 检查失败。拒绝为了浅冻结
问题修改全局公开 Scope 类型，也不扩展 PermissionManager 的冻结签名。

唯一 PEP 先验证来源、完整性、时效和凭据状态，再读取任何空间事实、业务 Store、缓存
或创建 fallback space。需要在线复核的身份按 `(credential_type, credential_issuer)`
访问与认证器同源的 Registry；缺指纹、未知 issuer 或撤销均失闭。SDK、Server AUTO 与
显式 Runtime 注入必须同样绑定真实真源；替换时先健康检查再替换，不能留旧 issuer。
Server 通过 API 包公开装配入口取得已绑定的 ServerRuntime，入口审计与业务审计同源，
关闭路径由该句柄统一负责。绑定和一致性校验留在内核包内，不由 Access 访问 API 或
MemoryRuntime 的私有成员；ServerRuntime 在上游 MemoryRuntime 生命周期协议上增量
提供安全组合与审计，不改变冻结的 MemoryAPI/安全 ABC。
Root Key / trusted gateway 等 capability=false 的身份可以有审计指纹，不能仅据非空
credential_id 猜测撤销能力。AuthContext 不携带 Callable、Store 或 `acting_user` 授权结果。

### 2.3 接入迁移与业务连续性

HTTP / CLI / MCP 通过认证中间件和共享 API 契约显式传递 security；MCP 保留上游完整
扁平参数、必填项和默认值，框架 ctx 不进入 Schema。stdio 从 AGENT_MEMORY_API_KEY
提取材料；Streamable HTTP 逐请求读取 Bearer/trusted headers 与真实 peer，缺请求
上下文时失闭，不回退到服务端进程环境变量。API Key 载体不等于 OAuth 2.1 资源服务器。

legacy handler 的 security 必填，已删除 legacy_request_context 与 payload actor 自述
桥。`actor_*` 身份声明显式拒绝，而 audit 的 actor_agent/actor_session 是历史查询
过滤项，不能当调用身份。插件适配器由 composition root 注入可信 provider，缺失即拒绝
进程内路径；示例/评测显式走 DEV 认证器，不从被测目标 Scope 构造“认证身份”。
DEV 自身范围访问由真实 Authorizer 判权；跨组织管理须显式 ROOT，不借空 Scope 或 allow_all。

## 3. 授权判定与真源

### 3.1 唯一 PEP/PDP、角色与路由

MemoryAPI 是唯一业务 PEP，Authorizer 是唯一 PDP。旧 PermissionManager 仅保留历史
兼容，不进入生产判定链、不双写授权状态。标准链在时效/身份检查后，依次执行管理角色
闸门、ROOT、org 硬边界、owner-cover、Delegation、Grant、默认拒绝。空间链继承前缀
并执行其独立两轴判据；不能用普通 Grant 绕过管理角色闸门。

管理面角色闸门终局：ADMIN 只管本 org，系统根资源和 VERIFY_AUDIT 等要求 ROOT；
ADMIN 不是“天然可读所有内容”。普通 READ 与 READ_AUDIT / VERIFY_AUDIT 不互认。
ROOT 可跨 org，但资源归属仍来自经过校验的目标，不能将默认落点误写成认证 actor。
未实现的主体角色管理网络接口不能因动作枚举存在就被宣称可用。

RoutingAuthorizer 只使用服务端资源事实选择策略，未命中进入明确安全 fallback。
组合策略任一可达 delegate 为 test-only，组合也为 test-only，生产装配拒绝。
路由字段查询共用既有 capability，不增加第二套授权入口。
拒绝结果带稳定 reason，允许结果带 rule；依赖故障传播 BackendError，不伪装成 403。

### 3.2 Grant、撤权与 Store

Grant 构造边界冻结动作集合，公共导出只有一套值对象，不做丢失 grant_id/revoked 的
新旧类型转换。服务端生成 grant_id；公共管理与 PDP 使用同一具名 GrantStore。
legacy 接入透传 actions 与 expires_at，授权响应返回服务端生成的 grant_id；撤销须携带
该 ID，不退回按 grantor/grantee 元组匹配，也不从业务 payload 推导调用身份。
`revoke(grant, *, security)` 保留为适配入口：除 grant_id 外的请求字段均不参与定位或
鉴权。客户端保存 grant 返回值，或通过 `Grant.for_revocation(grant_id)` 构造 ID 引用；
旧的无 ID 条件撤销必须改为保存服务端 ID，不能自动恢复宽泛条件匹配。

撤权从真源读 ID 对应的真实 grantor，按 REVOKE_SHARE 判权，再原子检查 grantor
未变化才执行撤销；不能信任请求带来的 grantor，也不能把随机 ID 当授权凭据。未知 ID
幂等无副作用，重复撤销不刷新原 SQLite 撤销时间。旧 Store ABC 不增加必填方法，
通过公开 GrantManagementCapability 的 get_grant/revoke_if_grantor 完成；不访问实现
私有钩子。管理域只允许一个共享 GrantStore，singular/plural 方法是同一真源的两种视图。
create_grant 在单真源内原子创建：相同 ID/记录重投幂等，冲突或撤销重放拒绝，抛异常
不得留下此次创建的有效权限；内存先复制后发布，SQLite 在同一事务提交或回滚。
这是 Store 级按 ID 幂等，不承诺丢失网络响应后的新 grant 请求自动去重。

memory/SQLite GrantStore、DelegationStore 对同 ID 活动记录执行全字段更新；撤销单调，
不接受重放复活。内存读写复制可变 Scope，防止对象别名绕过锁内检查。SQLite 委托
allowed_spaces 使用带 encoding 标记的 JSON，不据内容外观猜编码：旧空串/单值兼容，
旧含逗号行有歧义时拒绝，由可信运维依据原集合重新写入；坏 JSON/未知版本不变成无限制。

### 3.3 有限委托与代理代写

委托依服务端 DelegationStore 逐次复核，不凭上下文中的 ID 直接放行。进入委托判定后，
失效委托不静默回退 Grant。判据覆盖主体、有效期、动作、资源范围、空间 allowlist、
凭据与会话绑定；普通主体、角色或 metadata 不自行产生代理关系。

PR1 DEV 映射只证明单主体，没有凭据到委托记录的绑定入口。经确认采用私有服务端
装配过渡：`security.<name>.params` 下指定同源 delegation_store、指纹到 delegation_id
的 delegation_bindings，以及可选可信初始化 delegations。不是原始 token 到用户的
映射，不接受 HTTP payload 或客户端 header 指定被代理人；初始化也不能复活撤销记录。

代理代写适配限于 USER 级单主体 agent 与具名用户委托。PEP 由有效记录派生资源归属和
作者标记，认证 actor 仍是实际 agent；PDP 再复核真源与委托人当前空间内容权。
成员撤销影响下一次判定；治理操作不借用委托，不自动创建 fallback 空间。
`_delegator.` 是私有服务端投影前缀，剥离客户端同名前缀；author_principal/author_agent
是系统保留键。检索使用派生资源坐标和系统谓词，审计记录真实 actor，而非伪装为用户。

### 3.4 插件能力与资源所有权

新增能力独立于旧 ABC，不改变既有抽象方法集合或对外 MemoryAPI/AuthContext。
管理、委托真源、凭据真源、空间事实、入口路由和资源依赖通过公开类型契约表达，
不再以 getattr 探测未声明的私有方法。组合策略完整透传声明，实例生命周期内保持稳定。

只实现旧 GrantStore 的第三方插件仍可用于只读判定；未声明管理能力时，grant/revoke
统一返回 UnsupportedCapabilityError，不伪造成功。显式设置
globals.require_grant_management=true 的完整管理部署在装配期校验，缺能力拒绝启动；
要求同时约束显式 Runtime/PDP 替换后的最终真源，不仅约束默认配置实例。
该选项默认 false，不能无故阻断既有只读插件。不同 GrantStore 的扇出写入拓扑始终拒绝。

SecurityRuntime 和无认证 SDK 的 MemoryRuntime 都持有明确的安全资源所有者。
组合对象声明由本装配根拥有的依赖，借用资源不列入；授权 Store、原始 AuditLogger
及完整性 provider 声明的链 Store/KeyProvider 均纳入所有权图。受保护审计 wrapper
不再单独拥有同一连接；普通审计也必须关闭。根按对象身份去重，先停止使用者
再关闭依赖，重复关闭幂等。对象只关闭自身，不各自级联关闭同一共享 Store；关闭故障
记录并传播错误、继续释放其他已确认安全的资源，失败项可由资源所有者再次 close 重试。
停止未确认时保留使用者依赖与 leader 锁，不把清理通知的 best-effort 语义用于安全屏障。
assemble 返回的 API 可通过独立关闭能力释放其 Runtime；多个包装共享
资源时在装配阶段显式共享生命周期根，已关闭句柄不得用于新的健康装配。
Runtime 沿用上游非等待关闭：入口封闭后由单个收尾线程等待后台任务及状态写入结束，
再释放锁、回收自有 loop 和依赖。close(wait=True) 等待同一结果并报告异常，重复等待
不重复清理。文档模式的 watcher 准入/同步工作屏障仍可在停止请求阶段等待。
共享安全视图直接关闭同样执行后台停止屏障，不等待可能阻塞于资源根锁的收尾线程；
拓扑/状态短锁与资源清理锁分离。仅有 close 能力的额外注入组合也交由同一所有者释放，
Server 不在非等待返回后直接关闭它。排队的旧代 watcher 工作不能在关闭后进入数据面。
创建回滚包含取消/中断；启动失败按创建清单释放本次新建资源，不误关既有借用依赖。
具名缓存由 AssemblyContext 持有，只有同一上下文按 producer/name 共享；新装配
即使名称相同也建立独立实例和生命周期，不能通过全局 reset 打断存活实例。跨上下文
共享外部 Runtime 必须显式注入并绑定所有者，关闭只失效自己上下文的缓存。拒绝
将暂时 health 故障当作盲重建理由。插件构造尚未返回的内部资源由插件自己局部清理。
契约细节和扩展边界见 S10。

## 4. 空间、内容与返回快照隔离

`org > space > principal_path > session` 是资源覆盖模型；user 与 agent 在认证上是同级
主体，在资源上可按空间 policy 的 user_agent / agent_user 排列。纯 Scope 前缀或物理
路径不授予权限；Grant 和 Delegation 都须由 PDP 检查，非 ROOT 不越过 org 硬边界。

SpaceAwareAuthorizer 保留上游内容轴/治理轴、成员角色、归属主体档与作者规则，但终局
收敛到同一 PDP。PEP 从 SpaceManager 读取可信事实，客户端 metadata 不能覆盖空间
policy 或委托人事实。授予上界仅 ROOT 免除普通成员限制；ADMIN 不自动获得内容权，
普通成员不能自提。空间类型与 owner/owners 清单按当前代码维护在既有 specs。

检索把系统隔离谓词与用户 filters 外层 AND；路由和可读空间剪枝不被用户 filters 覆盖。
跨空间部分成功保留独立错误明细，授权后端全失败返回 BackendError，不折算为“没有权限”。
缓存、索引候选、后台队列同样不能绕过真源复核；路径/符号链接等本地文件边界由对应
存储实现维护，不把旧 demo 的 normalize_uri_parts 草图当作通用安全保证。

返回对象也是授权边界：

- list 一次取得当前分页与对应真源权限上下文，逐条 READ；不能用第二次分页校验旧内容。
- inspect/trace 对每条 MemoryUnit（包括祖先）的 Scope、作者与类型/管道路由判权。
- get 保留请求 ID 前置检查，选定 as_of 版本后对最终返回快照再次 READ；同 ID 的内容
  并发变化或另一个历史版本不能沿用旧权限。get/inspect/trace 共用快照权限投影。

历史读取采用保守边界：当前请求条目与实际选中版本都必须可读。本页不改变公开 MemoryAPI、
权限查询接口或 frozen 值对象；门禁整改不能以删除逐条安全断言换取绿测。

### 4.1 dreaming 与生命周期

保留最新上游的 evolve 三态、candidate/interval、fan-out、损坏记录隔离与取消/恢复机制。
注册、每 tick、恢复与逐桶调用经同一 PEP/PDP。服务端 created_by 保留五维 actor 并附带
私有 `_request_context_v1` JSON 字符串，记录角色、原认证/到期时间、凭据指纹、issuer、
复核 capability、委托 ID 和可信环境，不保存原始 token 或进程来源证明。

恢复只信任服务端私有注册表；仅有 actor 的旧记录跳过，不补猜身份。坏字段/未知角色/
无时区时间/非布尔能力拒绝并隔离。tick 构造新的受控请求，但不刷新认证期限；凭据与
委托仍逐次回真源验证。capability=false 保持冻结语义，如需停止其长期注册，须通过
受控管理路径注销，不能仅因有指纹擅自变成在线复核身份。

注册与 tick 的独立授权检查都保留。Runtime 先停止 watcher、新调度和摄入准入，等待
摄入与调度器在飞任务收尾后，再释放 dreaming leader 锁、自有 loop、依赖资源，避免双跑。
公开 MemoryRuntime 提供 authenticate 和上游 start/start_background/close；Server 门面转发而不访问私有端口。
身份上下文留在 API 安全边界，不透传到业务控制/检索/存储端口。

## 5. 静态加密与生效范围

CryptographyProvider 与 KeyProvider 独立，通过具名依赖共享实例。存储消费同步加解密
契约，provider 不自行读取根密钥配置；密钥装载归 KeyProvider。不使用异步草图强迫
同步 Store 内部 asyncio.run，也不通过接口暴露加密根密钥。

local 信封采用独立随机 data key 和 AES-256-GCM；根密钥经 HKDF 按 purpose/org
派生包裹密钥，再包裹 data key。新写 ENC1 v2，读兼容 v1。v2 内容 AAD 绑定完整 Scope、
用途、metadata、object_id、format_version、key_id/epoch，包裹层亦绑定用途、org 与
KeyRef；跨资源复制或篡改头部不能成为合法解密。v1 按旧派生/AAD 只读兼容，不冒充 v2。

非 ENC1、未知格式、缺字段、AAD/tag 错误或未知密钥代均 fail-closed。不提供
allow_plaintext 开关，不把解密失败返回为明文或密文。旧明文须离线迁移或显式使用
未加密 Store，不能在同一加密 provider 中静默降级。

### 5.1 KV/FS 应用层保护

加密在字节存储边界完成，避免在 add/search/get 等业务入口重复接线。显式选择
encrypted target 仍受支持；`globals.storage_encryption_enabled` 默认 false，开启后
统一保护全部具名 KV/FS 端口、消息 KV、外部注入 KV 及 raw 别名。开关严格接受 bool
或 true/false 字符串；关闭不拆除显式 encrypted，也不迁移旧数据。只装配 provider
不会自动加密数据。RoutingKVStore 应在加密层内。

KV 只加密 value，key/Scope 保持可寻址；get/mget/scan 解密后返回，list 解密后按原
MemoryUnit 语义过滤、计数和分页。FS 的 key、返回 ref 和后续别名须映射至稳定
object_id，更新不能换对象标识；未提供该能力的后端不能包装为 encrypted。
FS 绑定完整 Scope、用途、对象标识与版本作为 AAD；整对象有界读写，默认明文上限
64 MiB，密文有独立上限。get 支持字节或流；篡改、明文、错误密钥、越限和无效流均拒绝。
不提供 append/mv 的额外保证、分块加密或 MemoryAPI 资产上传/下载流程。

公共 StoreManager 解析边界检查完整 encryption_inventory 与公开端口对象一致性，
不允许替代 manager 绕过保护开关。插件通过公开 StoreEncryptionBinding 声明底层依赖、
provider 和格式，自动包装按 Producer/config 选择实现并按对象身份复用，避免重包 raw
别名。缺少保护清单或无法证明全部可切换端口受保护的动态 manager 拒绝装配。
具名依赖使用同一 AssemblyContext 缓存；包装回填不污染其他装配，启动失败仍由上游
资源所有权机制关闭本次新建依赖，外部注入的借用后端不转为隐式所有。

### 5.2 索引及治理数据库的部署保护

全文、向量、图、融合、实体索引、Markdown 真源、SQLite 文档影子索引以及审计、
权限、Grant/Delegation SQLite 库采用部署侧磁盘、卷或后端静态加密。直接把索引字段
套 AES-GCM 会破坏分词、ANN、排序和过滤，因此保持现有检索算法，不实现密态检索，
也不引入应用层全库解密再检索流程。运行中的应用、ES、LLM 与检索消费侧仍处理明文。

插件注册 at_rest_locations 声明实际持久化位置，不使用配置层 target 白名单；未知
能力、替换插件未重新声明或位置不明均拒绝。每个具名实例的 at_rest 声明包含
method（volume/backend）、reference 和匹配实际配置的 bindings。AtRestLocation
将部署证明与位置约束分开：SQLite :memory: 可免部署证明，db_path 仍固定；ConfigSource
初始覆盖及运行时位置变化拒绝，重新核查后重装配。Markdown/shadow 须显式指定 root/db_path。

Linux 启动器验证 LUKS2/dm-crypt、ext4 内部 journal、完整块设备加密路径及本机 Docker
daemon，将 local/online/postgres 的持久化 named volumes 替换为加密挂载下的 bind，
并生成应用配置及保护声明。ES 数据目录、Milvus 的 etcd/MinIO 依赖、Postgres WAL 等
须随实际数据布局一起覆盖。普通目录、远端 daemon 和 Docker Desktop VM 不借用本机
挂载证明。初始化、解锁与离线迁移由管理员显式执行，不自动格式化旧盘或覆盖既有密钥。
操作说明见已有 [部署 README](../../../deploy/encryption/README.md)。

远端服务及其他操作系统由部署方实施静态加密并提供核查记录。应用只验证声明及位置，
不将布尔声明冒充远端磁盘的密码学证明，也不持续监控管理员替换宿主挂载。
卷密钥与应用根密钥分别管理。备份、快照、导出及临时数据离开保护卷后须独立保护；
开启开关不会自动迁移明文，关闭也不能让 raw 端口读懂旧 ENC1 数据。

```yaml
memory_api:
  globals:
    storage_encryption_enabled: true
    storage_encryption_cryptography: default
    storage_encryption_kv: {target: encrypted}
    storage_encryption_fs: {target: encrypted, params: {max_plaintext_bytes: 67108864}}
  cryptography:
    default:
      target: local
      params: {key_provider: default}
  key_provider:
    default:
      target: local
      params: {key_file: /secure/application.key, create_key_file: false}
  fulltext_store:
    default:
      target: elasticsearch
      params:
        hosts: https://es.internal:9200
        at_rest:
          method: backend
          reference: operator:es-volume-verification
          bindings: {hosts: 'https://es.internal:9200'}
```

## 6. 密钥管理与分发

Root API Key、主体 API Key 与 Encryption Root Key 必须用独立随机值、配置和轮换流程。
认证 key 泄露允许冒用身份；加密根密钥泄露允许解密受保护数据，但不自动赋予 API 权限。
部署凭据与主体 key 通过受控带外 Secret 交付，主体 key 明文仅签发时返回；现有 Store
契约不等于已经发布主体注册、提升 ROOT 或 OAuth token 管理网络接口。

### create_key_file 默认值与轮换

LocalKeyProvider 默认 create_key_file=false。缺少有效密钥源时启动拒绝，不静默生成
无人管理的新根密钥；本地开发显式启用才创建。POSIX 文件收权到 0600；Windows 用
DACL 去掉继承和无关显式授权，只保留服务账号。配置脱敏不改变实际业务配置值。

本地多代轮换在锁内读取同一代 KeyRef/根密钥快照，旧代保留在进程内；新随机根密钥
不持久化。进程重启后轮换写出的新代密文可能不可读，因此不能把 local.rotate 宣称为
生产可跨重启轮换。KMS/Vault/HSM 和持久化历史代管理尚未实装；需相应 provider 才能
承担该部署能力。丢失加密根密钥不能靠 API Key 恢复。

## 7. 审计与完整性边界

普通审计记录可信 actor、action、target、decision、时间、request_id 及必要安全引用。
授权不读 ContextVar；业务审计从显式受控上下文取得 actor/角色/凭据引用，代理代写仍记
实际 agent。认证失败和限流拒绝分开；认证错误对外保持笼统，避免枚举主体。
不记录原始 API Key/token/password 或消息正文；技术 ID、指纹和必要细节仍须评估隐私。
detail 的词干过滤保留保守匹配，但将 `credential_id`、`key_fp`、`content_id`、
`token_count` 列为精确非秘密字段，避免关联 ID 和计数被整值遮蔽而丢失可观测性。
拒绝按 `_id`/`_count` 后缀或改变词干边界泛化放行，未知扩展和 KDF 材料仍受过滤。
允许字段仍执行类型/长度限制，过滤后再计算证明，不改变调用方事件或旧链验证语义。
普通运行日志的通用隐私过滤与独立设计见 [F09](F09-log-privacy.md)，不修改 API 响应。
文档标题、抽取/巩固 trace 的正文、完整 prompt、模型响应和 Scope 也必须标记脱敏；
只保留计数、类型和经确认的技术 ID，不改变送入模型或持久化的原始数据。
Markdown 标题渲染在正常写入和补偿恢复中均显式标记 `title`、`md_title`、团队值和
`memory_class`；类别可能含未知非空字符串，不把它当作天然安全的技术枚举。
采用现有标记过滤机制，保留诊断模板而不依赖字符串 PII 扫描或降低日志级别。
日志保护不归一化业务类别或改写文件分流；对应回归同时检查已知/未知类别、正常写入
及补偿恢复不含测试 PII，生成文档及业务元数据保持原样。日志回归直接捕获产生日志的
logger，并在退出时恢复状态，不依赖全量装配后的根日志传播配置。

READ_AUDIT 与 VERIFY_AUDIT 独立授权。verify_audit 使用独立资源 guard 和服务端验证
上限，不接受调用方的 digest/key/proof。普通 AuditLogger 基类不增加完整性方法；只有
security 显式引用 audit_integrity provider 时才启用链式保护，顶层段未被 Runtime 引用
或 provider 与业务 AuditLogger 不同源时装配拒绝。入口认证事件与 API 业务事件使用同一
ProtectedAuditLogger，内联 provider 配置也按实际实例接线，不能静默退回普通日志。

chained_hmac 对事件、Scope、UTC 时间、detail 和链上下文做版本化规范化，以用途隔离的
HMAC 签名，常时间比较摘要，按 proof 自带 KeyRef 验证历史密钥。内存后端只用于显式
测试选择，SQLite 通过锁内事务与 CAS 原子追加。空库可初始化，旧无签名记录须离线迁移，
已启用库缺表、缺列、head 不一致或未知 schema 版本均拒绝，不自动修补证据。

增量验证先验证精确 checkpoint，并在同一稳定快照的固定链头内分页；缺 checkpoint、
缺页、序号缺口、尾删或未知证明版本都不能报告 clean。连续成功水位在首个错误处冻结，
错误样本受请求值、服务端配置和硬上限约束。密钥变化在链上记录与实际签名一致的轮换
边界，CAS 和轮换竞争均有界重试；不落盘原始密钥、中间派生材料或自由扩展秘密字段。

mutation（包含 HIERARCHY 演进提交、后台建树注册与 Markdown 文件编辑）在副作用前做
完整性健康预检；后台注册即使因功能关闭而跳过也执行相同预检。写链失败统一为 AuditIntegrityError，并锁存
degraded，后续 mutation 不因一次成功自动恢复。授权插件的原子撤权、Runtime 共享资源
生命周期和 dreaming 三态分发保持原有能力边界，审计预检不能将它们替换为旧执行路径。
审计 detail 的 decision/request_id 由服务端写入，系统字段不受普通 detail 键数预算挤占。

外部 AuditAnchor 只有固定契约，没有产品实现；required 无锚点时拒绝。无可信外部锚点
不能识别整个数据库回退到合法历史前缀，本地 rotate 不提供跨重启历史密钥持久化。
unsupported、incomplete、tampered 和 rollback_suspected 不能等价为 clean。契约见
S10。
原 F04 的 90/365 天保留期与每日/100MB 轮换只是规划建议，不是现有框架已执行的策略。

## 8. 资源保护与部署攻击面

限流在认证之前执行，按真实 peer 而非任意 key fingerprint 分桶，避免随机 key 换桶。
内置 token_bucket 默认 capacity=30、refill_per_sec=5、max_tracked=10000，LRU 保持
桶表有界；关闭使用显式 unlimited，不用 capacity=0 暗示不限流。无网络 peer 的本地
路径不凭空生成地址。网关后请求共用 peer 时应按聚合流量配置或交给网关限流。

IP 桶不能限制同时运行的昂贵 verify 数，WorkloadGuard 在认证前非阻塞申请预算，
耗尽拒绝且 finally 释放；按具名实例表达共享，不恢复旧 Argon2Guard 模块级单例。
未知第三方认证器的保守 capability 防止保护漏挂。分布式僵尸网络、多副本配额与每 key
公平配额仍不是本地 peer 桶解决的问题。

HTTP 先检查 Content-Length（非法/负数 400，超过 4 MiB 413），再准入/认证，最后读
请求体；默认读取超时 30 秒。有界线程服务默认最多 256 个连接处理槽，耗尽 503，不
产生无界线程队列；槽在处理完成后释放。不能为处理 Windows 提前拒绝连接中止现象
而先读未认证 body、取消资源上限或修改成功断言。

DEV 不允许 0.0.0.0、:: 或其他非回环绑定；容器转发、代理和 TLS 仍需运维保证。
传输安全应复用既有 SSL 配置/上游专题，而不是在本页恢复 demo 的 TLS 或 OAuth 旁路。
密钥驻内存、索引明文、依赖更新与真实后端凭据的保护须按部署验证，不能承诺 Python
字符串可被可靠清零或内存绝不被换出。

## 9. 拒绝的方案与验收检查清单

- 客户端自述 actor、目标 Scope 反推被代理用户、ContextVar 暗传身份：破坏可信来源边界。
- 以空 Scope 表达 ROOT、隐式 DEV、生产 allow_all 或两条 PermissionManager/Authorizer
  判定线：形成默认放行或真源分叉；具名身份和真实角色闸门已替代这些过渡方案。
- 固定 DEV 缺省 ROOT：忽略凭据的开发入口不应同时隐式取得跨租户权限；采用默认 USER、
  可信部署显式提权，不改变 ROOT 本身的角色语义，也不通过硬编码 Scope 比对绕开 PDP。
- 为修安全缺陷破坏 Scope/MemoryAPI 的兼容形状或新增公共调试方法：采用防御性复制
  和兼容的公开 capability；底层插件协作必须完整声明，不以冻结为由依赖私有方法。
- 仅判断请求 ID/Scope，不检查真实撤权目标和实际返回快照：可绕过作者、路由或版本隔离。
- 猜测 SQLite 逗号集合、坏编码当无限制、撤销记录被初始化复活：扩大或恢复已撤权限。
- 缺依赖/密钥时回落明文、provider 内允许旧明文、宣称 KV 加密覆盖索引/FS：掩盖部署风险。
- 整文件覆盖旧 PR2/上游以消冲突、删除工具参数或把扁平 schema 改参数袋：
  丢失上游能力或跨越冻结边界，必须按语义保留两侧必要接线。
- 用 xfail、删除故障注入/同源断言、getattr 隐藏私有访问或全局关闭门禁：弱化安全证据。
  必要白盒测试/装配访问只作有理由的局部例外，平台不接受时走例外复核流程。

每次安全更新须检查：可信来源先于副作用、单主体 actor、管理角色闸门、真源同一、撤销
单调、目标/返回快照判权、系统谓词不可覆盖、加密 fail-closed、资源预算与审计不泄密。
同时核对固定公共接口和 PR 阶段边界。实现、测试与本页/受影响既有规约统一交付一个
实现 PR 提交并关联实际 issue；本地 problems 工作记录不纳入该提交。

## 验证要求

安全能力验收以行为契约为准，不以测试总数作为全部问题闭环的证明。至少覆盖：

- 所有公开入口拒绝伪造/过期/篡改/撤销上下文，拒绝前不得触达业务端口。
- 相同 Store 契约在内存、SQLite 和第三方替身上运行；验证授权真源同一、管理动作隔离、
  真实目标原子撤销、创建失败不留有效权限、按 ID 幂等且撤销不复活。
- 旧 ABC 只读插件可替换；未声明可选能力返回不支持，显式要求管理能力的部署启动拒绝。
- 测试插件注册在用例结束后恢复；跨测试共享的替身放在无注册副作用的支持模块中，
  默认目录收集与单独执行均应通过，不能依赖测试模块的导入顺序。
- 组合策略完整透传委托/空间事实/路由与资源依赖；共享资源只关闭一次，借用资源由其
  所有者关闭，关闭故障不阻止其余清理，SDK 未配置认证时也释放授权 Store。
- 上游扁平工具参数、正常代理业务、返回快照隔离和 dreaming 持续复核保持回归覆盖。
- 真实 SQLite 审计链覆盖非默认增量参数、损坏/尾删/轮换/并发快照及写链故障失闭，
  保护启用后的返回版本快照仍须重新判权；日志脱敏不能挤掉服务端 request_id/decision。
- Ruff/本地静态检查与华为平台专有门禁分开验收；真实数据库、模型与服务在对应环境验证。
- KV/FS 真实密文落盘、重装配读取、AAD/错密钥/篡改拒绝及有界流；替代 manager 与
  第三方包装器均服从统一开关，独立上下文不串缓存，关闭开关兼容旧插件。
- SQLite 内存位置的初始覆盖及连接建立前后切盘拒绝，原数据保留且不产生磁盘文件；
  持久化索引和治理数据库逐实例声明、未知能力拒绝，部署布局覆盖全部持久化依赖。

逐次复现、测试结果、环境排除及门禁工作记录只在本地 security-plans 保存，不作为
本页的章节追加；本地笔记不纳入 PR，也不成为正式文档的链接依赖。

## 已知遗留

- 本地静态检查不替代华为专有规则验收；平台规则重扫、局部例外认可和 Windows
  早拒绝连接中止的连续回归须在对应平台验证。
- 真实 Linux LUKS + Docker 全栈的结果一致性、重开卷及性能需在完整部署环境验收；
  Windows/macOS 尚无自动卷加密部署检查，远端保护仍依赖运营方核查。
- 真实数据库/缓存、ML 模型、LLM 服务与部署环境未全覆盖；按对应 CI/部署补跑，不能
  把跳过解释为功能已验收。healthz 的配置名称暴露与认证失败细分审计仍需独立评估。
- memory PrincipalKeyStore 重启丢状态；memory 授权 Store 亦如此。选择 SQLite
  Grant/DelegationStore 不会自动让 PrincipalKeyStore 持久化。第三方 GrantStore 若支持
  公共 grant/revoke，须声明原子管理能力；旧逗号空间编码需人工明确迁移。
- 私有委托装配是过渡，动态委托管理公开接口不在当前冻结接口内；若有新产品需求须
  先评审契约，设计/取舍仍维护本页，不再新建安全 feature。
- 外部审计锚点尚未实装；FS 加密、KMS/Vault/HSM、跨重启轮换、OAuth
  资源服务器和分布式限流/公平配额都不能作为本分支已完成能力。
