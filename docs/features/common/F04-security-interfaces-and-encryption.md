# F04 — 安全能力与设计决策

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-14 |
| 影响范围 | `jiuwen_memory/common/security/`、API PEP、各接入形态、存储加密及相关模块规约 |
| 验证范围 | 安全契约、第三方替换、故障、生命周期及各接入面业务回归；见「验证要求」 |
| 规范契约 | [S10 安全横切契约](../../specs/S10-security.md)、[S02 记忆接口层](../../specs/S02-memory-api.md)、[S07 公共组件层](../../specs/S07-common.md)、[S06 存储层](../../specs/S06-storage.md) |
| Refs | #143、#222、#270、#280、#293 |

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
安全能力特性文档只保留 F04。MemoryAPI 方法名和请求参数保持稳定；读取明文新增明确的错误返回，底层插件契约按公开能力演进。
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
| 隐私分级与用户访问控制 | 可选正则分级、持久化前脱敏、隔离片段密文和独立 Passkey 查看 | 处理环境可信且可使用原文；仅 inline TEXT/CODE；旧库须迁移；文档模式、LLM 分类及分级 TTL/cloud/share 执行未实装 |

身份和目标资源必须分开。`AuthContext.actor` 恒为 org 非空、user/agent 恰一的已认证
单主体；资源 `Scope` 则可以按 `principal_path` 表达 user/agent 层级。资源坐标、URI、
metadata、业务请求头不能替代认证身份。`Role.ROOT` 来自服务端认证事实，不从空 Scope
或目标形状推导；普通主体不能通过签发接口自行取得 ROOT。

隔离和加密不覆盖所有攻击面：向量、摘要、缓存、embedding 队列与原始资产应分别评估
暴露面；已认证但恶意的主体仍受授权限制。TLS 终止、网关信任链、Secret 分发、数据库/文件
权限、备份与日志保留属于部署边界，不能因 API 鉴权或 KV 加密就宣称全部受保护。

### 本地视频源文件准入

目标记忆的 WRITE 权限不代表对服务端任意媒体文件的读取权。内置视频 Normalizer
在实际读取前使用服务端 `local_media_roots`，按已判权的落点 Scope 五维精确选择目录。
无绑定默认拒绝，绑定不可通过 content、assets、metadata 或工具参数声明；不同 Scope
需要分别配置，不能把包含多个租户数据的父目录当作通用白名单。策略与 privacy 独立。
匹配对象是 `RawPayload.scope`，不是认证主体：启用归属判定后，落点归一为 org/space，
user/agent/session 均为空，此时仅绑定 org/space；未启用时沿用实际落点的各维。

支持绝对本地路径和绝对 file URI，拒绝依赖进程 CWD 的相对输入；原资产引用、文件名
及视频投影结构保持稳定，新增部署目录绑定。
接入方须先创建并配置合法导入目录；旧部署未配置时视频读取会明确拒绝。拒绝以 ROOT
角色或关闭隐私开关代替源文件准入，也不以新增通用媒体上传服务扩大本轮范围。

解析后检查真实路径归属，拒绝父级穿越与网络文件来源。POSIX 从打开的配置根目录
描述符起步，核对装配时记录的设备号/inode，再用 dir_fd 和 O_NOFOLLOW 逐段打开；
根被另一真实目录替换时拒绝，正常更换导入根需重新装配。描述符由显式 try/finally
上下文成对托管，流包装失败也必须释放；
Windows 用句柄固定祖先目录、拒绝 reparse point 并禁止删除共享。
从经过检查的普通文件句柄复制到任务私有目录，后续 ffmpeg/ASR/VLM 仅消费副本，
不重新打开原路径。每次暂存使用独立子目录，同一任务重复暂存相同文件名不会覆盖旧
副本。副本随任务结束或失败清理，复制失败清除未完成副本并报 BackendError；输入
目录应由可信服务维护，不能用
路径准入替代主机文件权限或抵抗已控制主机的进程。受信代码注入的自定义 backend
自行负责其数据源边界，不是请求方可配置的绕过开关。

残余风险：路径准入不能识别硬链接的原始来源；根外文件若被硬链接到导入根内，仍可被
当作根内普通文件读取。不能依赖符号链接/reparse point 检查覆盖硬链接；导入根及其
父目录须由可信服务维护，限制不可信主体的写入、链接与目录替换权限，必要时将导入区
放在独立文件系统。该能力不抵抗已掌握相应主机文件权限的主体。

验证涵盖五维不匹配、相邻前缀目录、路径穿越/链接逃逸、空策略拒绝、文件副本稳定性，
并保留多模态路由、端口传递、写入/异步摄入与检索回归。竞态回归在公开路径解析边界
注入替换，覆盖根被真实目录替换与根描述符固定后的相对打开；另验证流包装异常后所有
已申请描述符均关闭、重复暂存及复制失败分类。平台不支持创建符号链接时
相关用例显式跳过，POSIX/Windows 分支在对应 CI 主机验证。

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

### 2.4 Trusted 主体初始化

选择 Trusted 配置后，空 memory 注册表需要在同一服务内预置主体。早期测试借用
API Key 认证器的 Store 签发不用的 Key，再替换入口认证器；这种做法依赖内部实现，
并把主体登记不必要地耦合到 API Key 的签发与撤销。

现在由受信启动代码调用 `jiuwen_memory.api.register_trusted_principal`，向实际 Trusted
认证器登记组织内的 user/agent 和 USER/ADMIN 角色。函数按增量能力委派同一 Store，
不导出实现类、不查看生命周期依赖清单来猜 Store，也不要求了解网关厂商或上游登录协议。
协议仍是专用 gateway_key 与固定身份头，角色来自服务端注册表。

内置 memory Store 分开保留显式登记角色和凭据记录。同角色登记幂等，角色冲突拒绝，
与 Key 签发共享状态锁，避免并发产生角色分叉。API Key 的旧签发/撤销行为保持：
只有显式登记的主体在最后一把 Key 被撤销后仍有 Trusted 角色，原 Key-only 主体不受影响。

拒绝用自动生成的废弃 Key 模拟登记、开放匿名注册、信任 X-Role、在初始化时写 ROOT，
以及给旧 ABC 新增必选抽象方法。入口只做受信启动初始化，不交付动态改角色/停用、
HTTP 管理接口或持久化产品；管理认证和持久化需要独立设计。契约见 S10，验证覆盖
纯 Trusted 配置、无 Key 登记、身份与权限负例、幂等/冲突、并发和旧插件兼容。

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
装配过渡：委托存储只由 Authorizer 选择，Security 在启用凭据委托绑定时通过公开
DelegationSourcesCapability 复用其唯一实际真源（按对象身份去重）。不按 target、实例名
或私有字段推测，也不回退构建 `default`；没有或有多个不同真源时拒绝启动，重复暴露
同一个实例则允许。未启用委托绑定时不增加这一唯一性要求。

委托相关的 `security.<name>.params` 配置只保留指纹到 delegation_id 的 delegation_bindings，以及可选
可信初始化 delegations；不再接受 delegation_store 选择项，旧配置需删除该项。拒绝保留
双处选择或隐式默认回退，因为它们会让认证绑定和授权/撤销各自选择存储，增加配错风险。
Authorizer 仍可独立用于业务判定，依赖生命周期由统一装配根管理，不转移到 Authorizer。
这不是原始 token 到用户的映射，不接受 HTTP payload 或客户端 header 指定被代理人；
初始化也不能复活撤销记录。

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

Runtime 注册的周期 HIERARCHY 也不能沿用一次性许可。API 显式注入绑定 home 的准入回调，
每轮读取、构建派发和持久化批次前复验原注册上下文的可信来源、到期及在线凭据状态，
刷新空间事实后复验 WRITE/UPDATE 权限及空间可写状态。回调失败即拒绝，不降级放行；
security/actor 不向 Engine、Job 或 Composer 传递。确认无写入时报告失败，部分写入则
保留已完成计数与修复要求；这不提供撤销已开始底层调用或跨进程写入 fencing 的能力。

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

托管配置的内置 SQLite 覆盖文件包含非密模型设置与受保护凭据引用；POSIX 下新文件以 0600
创建，拒绝非私有或非当前用户所有的既有文件，各平台拒绝符号链接或非普通文件。其他
平台的文件访问保护由部署方保证。模型 API Key 经既有 local CryptographyProvider 与
KeyProvider 信封加密后存入同库的独立 model_credentials 表，普通覆盖只保留不可变
引用。加密根密钥在实例覆盖库旁的 .model-key 文件，由已有 provider 管理私有文件权限；
不是入站认证 KeyStore，也不以模型 API Key 作为根密钥。凭据目录须由当前用户所有且
不可被其他用户写入；已有凭据缺少根密钥时拒绝解密，不重新生成密钥冒充恢复成功。
保护能力不足时该字段只读。该保护不替代部署磁盘保护，数据库与根密钥一并被读取后
不能承诺凭据仍保密；移动实例时需保持或迁移路径绑定与密钥，不承诺任意拷贝可解密。
统一静态加密开启时，内置托管配置装配拒绝该组合，不能另写明文文件绕过部署保护。
在 Portal 切换模型服务时，网址、API Key 和模型名要一起保存。同一个服务内调整模型名
可沿用原 Key。加密保存 Key 时同时记录服务地址，重启后也会检查两者是否匹配。

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

管理卷初始化显式选择 ext4 与 journal，并在关闭临时 mapper 前核查实际类型、
has_journal 特性及非零内部 journal inode；挂载检查复用相同的 journal 判定，缺失或
无法核查均拒绝。工具路径仍从系统目录解析并规范化，不依赖 mkfs.ext4 的 argv[0]
别名语义：它在部分发行版是 mke2fs 的符号链接，解析后裸名调用会受宿主默认类型影响，
可能生成 ext2 却返回成功。显式参数与产物核查共同保证初始化交付既有契约要求的形态。
失败关闭已打开的临时 mapper 并保留新镜像；旧版生成的 ext2 不自动重格式化或迁移。
这是管理卷可用性与产物一致性约束，文件系统类型不改变 dm-crypt 的块层加密能力。

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

### 5.3 显式明文记忆维护

开启加密不触发启动迁移，也不在 get/search 中尝试转换旧数据。内置 provider 通过可选
格式识别能力拒绝非信封输入；格式识别不是认证，也不证明输入一定是合法明文。
正常 get/list/search/inspect/trace/read_md_file/export_space 沿已有 BackendError 异常契约
报错，消息提示显式迁移或清空；不返回原明文、空结果或不存在。MemoryAPI 方法签名、
返回类型和公共导出保持原样，HTTP/CLI/MCP 使用既有错误处理，不增加专用结果对象或 409 映射。
损坏 ENC1、错误密钥等仍走原有完整性/后端失败契约，不自动修复或回退明文。
检索只在实际读取到此类数据时失败，不为了检查明文在每次搜索前扫描全库。

独立工具按实际装配的 StoreManager 保护清单选择当前 KV 端口，沿包装器公开能力扫描；
不扫描插件注册表、工作区全盘或调用方任意路径。同一物理位置去重，保护绑定冲突在
写入前拒绝。缺少可靠清单、位置身份、枚举或原子比较能力时返回 `unsupported`。
Redis 等后端的有损命名空间若不能还原原 Scope，扫描与 JSONL 输入均明确拒绝，
在鉴权和操作前校验每条记录的 Scope，不猜测归属或折叠写入其他命名空间。
内置 Memory/SQLite/Redis/PostgreSQL KV 支持；扫描对象仅为尚未过期的 `/memory/{id}`
当前格式 MemoryUnit，不把 `/messages`、治理元数据、任意 FS 资产或索引投影猜成记忆。
FS 未提供安全枚举与原子比较能力时明确报不支持，整体不报告全部成功；部署侧索引、
影子库及 Markdown 继续使用 §5.2 的卷/后端加密，不套应用层信封。
需要只处理已支持的具名 KV 时，部署方显式选 `--port`，结果仅代表所选范围。

工具使用实际配置的认证器及同源 Authorizer：USER 仅本人主体，组织 ADMIN 仅本 org，
ROOT 可跨 org。USER 的 space 是硬边界，须与目标精确相等，包括双方均为空的情况；
空绑定不覆盖具名空间，已有非空 agent/session 绑定继续生效。Grant/Delegation 不能
扩大此专用维护范围。枚举 Scope 后先鉴权再加载其记录，不向普通用户展示其他主体的
ID、数量或内容；逐条操作前重核查失效/撤销和真实物理身份。配置与端口选择是受信部署
输入，不得将整个配置或服务端路径作为 HTTP/MCP 请求参数。具备主机文件与根密钥权限
的本地代码不在 Python 层权限隔离的威胁边界内。

工具先校验版本、ID、五维 Scope、内容/元数据及物理 key/Scope 一致性；兼容的当前落库
格式直接处理，不重新抽取或重建业务事实。旧混合元数据格式返回 `format_upgrade_required`，
损坏信封不能作为明文重试。`inspect` 只检查；`migrate` 调用 encrypt，先自检密文再用
原子比较替换，并解密持久化回读核对；`clear` 仅比较删除本次确认的合法明文，不清库、
不删除密文。比较失败返回冲突，不盲目覆盖；已有记录保留绝对到期时间，扫描后到期或
消失的记录不重建。已加密且通过认证的记录跳过，不重复加密。
逐项提交，失败项与成功项分别返回；回读失败保留 `committed=true`，不能把已提交写入
说成已回滚。正常业务/启动流程不调用此工具，维护时应停止业务写入；CAS 仅防止覆盖
并发变化，不提供跨后端整批事务。

扫描通过插件显式声明的有界迭代，不先加载整库候选或调用 raw.scan。内存/SQLite/PG
每页最多取 64 个 key 再逐条读取；Redis 保留 SCAN 页内未处理 key。SQLite 插入先持有
数据库写锁，以缺失检查和唯一主键插入拒绝竞争写入；CAS 在实例锁、数据库写锁等待
结束后检查过期时间，不能修改等待期间已过期的条目。
单次扫描默认最多处理 10,000 条、256 MiB 明文，可用 --batch-size（1..10,000）及
--batch-bytes（1..256 MiB）调低。为避免零进度，首条合法记录允许单独成批超过该批
字节目标；单条实际读取仍受限于配置的明文上限的两倍加 64 KiB，超大值在后端拒绝
物化并报 record_too_large。上限限制记录体及单次结果，不把 Scope 元数据清单当作记录体。
预算用尽返回 partial 和 cursor，保留已处理记录；将 cursor 原样传给同一配置、操作与
端口的 --cursor 即可续跑，inspect 同样可推进。游标绑定位置清单和操作，不包含记忆正文
或凭据，也不授予权限；每批重新认证和判权。修复失败项后可从头重跑，已加密项仍需认证
后跳过；各批结果须汇总，后批 complete 不代表前批失败已解决。维护期间继续停止业务写入，
游标之前的新插入记录需另起扫描。JSONL 输入不支持扫描游标，应自行拆分为至多 10,000 条。

```shell
# 支持完整服务 YAML（自动读取 memory_api）或独立内核 YAML
# 凭据从 AGENT_MEMORY_API_KEY 读取，连接和加密配置须与待维护部署一致
python -m deploy.migration.encrypt --config deploy/docker/local/config.yml --operation inspect
python -m deploy.migration.encrypt --config kernel.yaml --operation migrate
python -m deploy.migration.encrypt --config kernel.yaml --operation clear --port default
# 读取上次结果的 cursor，按相同操作继续（CURSOR 为上一批返回值）
python -m deploy.migration.encrypt --config kernel.yaml --operation migrate --cursor "$CURSOR"
```

`--config` 若包含 `memory_api`，只提取该映射交给内核，忽略外层 `profile` 等服务设置；
不含该字段时按独立内核配置读取。`memory_api` 为 null、字符串或列表时明确拒绝，
不退回默认配置。选中段继续使用内核的 `${VAR}` / `${VAR:-默认值}` 展开规则；
读取或 YAML/基础结构校验失败时，CLI 返回错误码及可读 `message`（YAML 语法错误含行列），
不回显配置原文或秘密值。部署文件仍须配置真实认证及待维护后端的加密保护。

工具随源码部署，入口与实现均在 `deploy/migration/`；`assembly.py` 独立装配实际
StoreManager、ConfigSource、认证/授权和审计，`runner.py` 编排维护，`validation.py` 校验
当前格式，`encrypt.py` 提供命令行。不经 MemoryAPI 或私有 Kernel，不启动 Engine、
调度、摄入或文件监听后台任务；装配失败回滚、实际审计完整性及共享资源关闭保持同源。
认证凭据从 AGENT_MEMORY_API_KEY 读取，不接受自述 actor/role。未传 `--input` 时直接扫描；
可选 `--input` 接收每行一个现行 MemoryUnit 的 JSONL。
文件模式保留原字节，既有目标必须匹配，不能用文件覆盖不同内容；缺失目标仅 migrate
可以导入，导出文件不携带存储 TTL 时新导入记录无 TTL，不宣称恢复丢失的过期信息。
工具返回结构化结果而非操作异常；CLI complete 退出 0，failed 或待续跑的 partial 均退出 2。
partial 只表示还有后续范围，该批的成功或失败须检查 items。
输入文件及外部备份由持有者管理，工具不自动删除它们。

清空是逻辑删除，不等于安全擦除磁盘、数据库空闲页、WAL、快照或备份；原地替换也不
保证消除历史明文副本。部署方须将这些位置纳入卷/后端加密及保留清理策略。工具不承诺
全库重写、FS 资产迁移、格式升级或索引重建，也不把不支持的能力当成无明文。

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

HTTP 模式的 Portal 请求复用原服务的认证、限流和并发预算。SDK 本地页面绑定已有
Runtime，每个 Provider 签发的浏览器授权分别绑定该 Provider 自己的可信安全上下文来源；
即使多个 Provider 的 actor 相同，也不能借用彼此的角色、凭据或委托权限。每次请求重新
调用对应来源并复核宿主身份；Provider 退出只撤销其自身的 Provider 绑定，不影响共享
Portal 或其他 Provider 的会话。浏览器用 Scope 和记录 ID 定位已有记录，
服务端重新检查个人归属和权限。新增记录使用认证身份和宿主可选的绑定范围，来源由服务端标记。

HTTP 自动管理配置时，先检查安全 Runtime 和认证插件是否声明了固定个人身份，再尝试
打开或创建配置库。内置固定身份 DEV 可以通过检查。使用多身份认证或由宿主传入自定义
安全组件时，普通启动跳过配置库。宿主可以显式接入配置存储和授权。

SDK 由接入应用通过 `security_provider` 提供个人身份，每次访问时重新检查。配置读取
和修改需要授权，写入前还要检查审计记录的完整性。模型凭据的保存见 §5.2。发现个人
记忆时，服务先确认请求身份，再逐个检查记忆范围。作者归属无法确认的记录不自动归入个人。

Portal 只允许本机 Host，JSON 请求要求同源，防止跨站请求和 DNS 重绑定。
宿主生成5分钟有效、只能兑换一次的随机访问授权，通过 URL 片段传递；页面在交换前
移除片段。独立页面兑换1小时有效的 HttpOnly、SameSite=Strict、`/v1/` 路径 Cookie。
iframe 默认关闭。显式配置宿主来源后，CSP 只允许对应页面嵌入。iframe 使用只存在于
页面内存的短期会话票据，经同源请求头传递，避免依赖第三方 Cookie。
授权和会话数量分别受限，达到上限后只在新增同类记录时淘汰最早的一条。服务关闭时清空。
授权不写日志或 localStorage。HTTP 每次用原凭据重新认证。若认证身份与签发链接时不同，
会话请求返回 401，用户需要从应用重新打开页面。SDK 会话还要求签发该会话的 Provider
仍处于共享 Portal 的注册状态；来源注销后该会话返回 401，不回退到其他 Provider。
SDK 启动链接省略 Provider key 时，只允许在恰有一个已注册来源时签发，并在签发时
固定该 key；零个或多个来源时拒绝签发。请求阶段拒绝未绑定来源的旧授权，不能根据
当前剩余来源动态选择身份。显式提供已注销或未知 key 同样拒绝签发。
Portal 页面、浏览器会话请求和托管配置接口的响应带 `Cache-Control: no-store`。
普通记忆 API 和健康检查响应沿用原有行为。

DEV 不允许 0.0.0.0、:: 或其他非回环绑定；容器转发、代理和 TLS 仍需运维保证。
传输安全应复用既有 SSL 配置/上游专题，而不是在本页恢复 demo 的 TLS 或 OAuth 旁路。Neo4j 存储沿用 ssl_verify/ssl_ca_cert：校验开启后拒绝非校验 TLS scheme，晚绑定 URI 同样受约束；自定义 CA 与强制加密一起传给驱动，不允许配置读取后被丢弃。
密钥驻内存、索引明文、依赖更新与真实后端凭据的保护须按部署验证，不能承诺 Python
字符串可被可靠清零或内存绝不被换出。

## 9. 隐私分级、可逆脱敏与用户访问控制

面向用户访问敏感记忆的能力统一称为用户访问控制；面向 Agent 获取、处理和使用
敏感记忆的能力称为使用控制。本节实现用户访问控制，普通 Agent 召回仍使用脱敏内容。
术语与接口边界见 [S10](../../specs/S10-security.md#隐私保护与用户访问控制)。

### 9.1 分级策略与默认值

脱敏记忆需要继续参与抽取、索引和召回，同时保留有权限的人查看原文的能力。因此采用
普通记忆保存脱敏表示、敏感片段隔离加密的方案，分开控制写入策略与原文展示。

| 默认等级 | 含义 | 写入策略 | 允许经人机认证后还原 |
|---|---|---|---|
| S1 | 公开信息 | `allow` | `true` |
| S2 | 个人隐私，例如身份证号 | `redact` | `true` |
| S3 | 凭据，例如 API Key、口令、私钥 | `redact` | `true` |

等级名称、数量、显式 `rank` 和正则映射均可配置，不按等级名称排序。`allow` 是完整的
直接写入豁免，原文可以进入普通索引和响应；`redact` 路径要求持久化正文、检索索引和
普通召回使用脱敏表示。规约、分类、抽取、摘要及去重属于可信处理，可使用本次输入
原文；不承诺处理插件、LLM、Embedder 及其外部服务看不到原文，其部署与日志策略由
宿主管控。`reveal=true` 开启的是经本次人的证明和资源判权后还原的能力，未经验证的
原文披露始终关闭；未配置人机认证器时仍可脱敏写入和召回。

隐私保护按规则检测、静态分类、等级策略和写入决策分层，通过 PrivacyProtection
接入持久化保护边界。内置类别覆盖个人证件、银行卡及常见厂商凭据，支持完整私钥块
和凭据字段捕获。类别映射、名称和开关可配置，兼容原自定义 rules，并支持
keywords/patterns/level_id 配置。显式 rules 保持替换语义，
use_builtin_rules=true 才叠加，避免升级时悄悄改变既有定制策略。

分类与脱敏使用同一份完整命中位置及逐项校验，修正“首个不合法号码压过后续有效项”
和“分类校验但替换不校验”的差异。采用最高等级合并，拒绝低敏用户规则提前返回或
分类失败回退公开等级。身份证保持既有形态保护；Luhn 用于银行卡。姓名、地址、IP
及宽泛微信密钥检测作为可开启规则保留，默认关闭以控制误报。注入规则只生成依据，
除非显式映射等级；不承诺在可信处理阶段隔离注入。

规则名、类别和等级保留为有限诊断摘要，原值和配置中的敏感词不进入依据或审计。
不新增普通查询接口，不改变既有三级默认值及 allow/redact 两种写入方式。当前不提供
稳定跨记忆假名、不可逆优先替换、多分类器/LLM 与分级治理执行；前两者
会改变可还原性或同值关联语义，其余能力仍按既定 TODO 管理。

采用正则识别可明确覆盖的隐私和凭据。重叠命中采用较高等级，
先识别再替换并保留来源分级，避免占位符或摘要抹去线索后降级。正则不能保证发现姓名、
地址或任意格式密钥等全部语义隐私；规则覆盖、误报与漏报须随部署数据验证。

### 9.2 持久化边界保护与独立敏感区

RawPayload 入口只校验输入和保留分级，不替换原文、不暂存原值密文。主要脱敏统一放在
MemoryUnit 处理完成、IndexBuilder 生成全文/向量等持久化索引之前；直接写入 KV 的原始消息、
直接 KV 写入和派生记忆同样受保护，不能只包住 Engine 的最终返回点。预切分和内容型
prompt 分批保留原子占位符及长度上限，判定型头尾截断完整省略跨界引用；过小的正数
分批上限明确拒绝。正则执行有总匹配预算，超时拒绝，不替代业务或 LLM 超时。

敏感区采用 PrivacyStore 契约，由 EncryptedPrivacyStore 包装 KVPrivacyStore，后者
依赖独立 KVStore 实例；内存、SQLite、Redis、PostgreSQL 可通过配置切换，当前示例
使用 SQLite。切换存储不改变落盘前脱敏、二次认证和正常资源判权。
隐私包装保留构建器声明的真源绑定，演进读取沿同一数据面执行；缺失绑定时遵循
原有的失闭装配规则，不以默认数据面代替写入真源。
策略层只负责识别、占位符和关联校验，Store 包装层调用既有密码学能力，后端负责
事务和持久化。复用存储层装配与数据保护机制，避免在正则策略中继续维护 SQL 和
加解密调用；不将独立敏感区混入普通记忆召回端口。私有 KV 递归内联构造，避免与
公开端口共享实例、触发隐私包装递归或被业务连接热更新切到其他库。

通用 KV 不提供多键事务，因此适配层将一个完整 Scope 的片段、清单和恢复引用编码为
单个有界状态，以原生 CAS 原子发布。内存锁、SQLite 事务、Redis 单键脚本、PostgreSQL
条件语句分别提供该能力；条件失败明确报冲突，禁止以 get/update 模拟。代价是每次
读取和发布均处理该 Scope 的完整状态，适用于当前演示及有限规模部署；默认上限
64 MiB（含密文及恢复引用），超限拒绝写入。后续大规模拆键需要同时设计多键事务或
版本根指针和回收协议，不能仅按片段拆分后放弃原子性。

旧 vault_path 配置与显式 SQLitePrivacyStore 保留专用表及密文上下文兼容，不自动转换
为 KV 格式。新示例使用独立文件；更换后端只改变读写目的地，不负责历史数据迁移。
未实现 MySQL 驱动，也不把内存或未启用持久化的远端服务描述为可靠持久化。

敏感片段值与关联清单分别密封，通过完整 Scope、对象、通道、
修订号和指纹约束还原。隔离区不注册为普通数据面；正常处理只认证引用清单，不解密
片段。合法路由通过原候选目标校验后搬运同组织密文引用，保留来源加密绑定。
Store 的 get/search 认证关联清单，授权查看的 get_fragment 才返回片段原值；透明
解密不提供授权。检索仅按 Scope、记忆 ID、revision 和 token 定位，不建立敏感
原文全文/向量索引，也不为了迁移引用而先解密再加密。

落盘保护期间有界暂存片段密文；写入/构建结束、失败或取消后清理本次暂存。
原文缓存写入保护副本，不改变正在处理的本次输入；历史缓存读取保持脱敏，不自动解密。
原始消息和正式记忆分别计引用，删除一个来源
不误删其他仍有效的引用。标签的授权预览、输入回显与业务保护分开，单条和批量均在
处理完成后、落盘前生成片段并保留等级。源等级仍可在入口识别和继承，不需要提前替换正文。

默认 Ingestor 在保留原文的同时，对非结构化规约结果切分，不截断已识别的敏感片段；
超过配置上限的单个敏感跨度明确拒绝。规约器声明的结构化中间产物完整交付下游，
隐私入口的模态与资产准入保持不变。处理中临时计算的原文向量不用于存储索引；正文改变时清除已有向量，
持久化向量由脱敏内容重新生成。Schema 辅助实体注册表只保存安全投影，不开放原文还原。

私有关联随 KV 真源提交和回收，索引包装层只保护投影。写入前保留旧关联的密文恢复引用，
委托异常时回读真源，保留实际存续版本；真源已写而索引失败时不删除新关联。无法回读时
保留候选密文及引用，待成功写入或硬删清理。先删真源再回收私有状态，投影删除不回收。
拒绝失败后无条件 discard 或预先删除关联，避免未改变的记忆失去还原能力；多个后端
仍不承诺全局事务，恢复记录不改变当前真源、人的证明和资源判权要求。

隐私保护与全后端静态加密是独立能力：前者约束持久化正文、检索索引和原文展示，后者保护普通存储
介质。关闭隐私总开关保留策略和已有占位符，不自动解密，也不修改静态加密开关。

### 9.3 独立人机认证与完整句子展示

机机认证保持原契约；独立选用可替换的 WebAuthn/Passkey，适配无桌面服务器与人的不同
设备。登记邀请经可信部署渠道发放，绑定固定 USER；机器凭据不能登记或接管人的身份。
每次查看绑定浏览器、完整目标和修订号，挑战一次使用，失败及过期也不可重放。

用户访问控制统一经 MemoryAPI 的可选 begin/finish 入口，HTTP 安全路由和 Runtime 的
HumanView 只作转发；主认证上下文由宿主延迟提供，在 API 内执行。这样认证、敏感
读取和响应都经过 API，避免接入层直接连接披露服务形成第二条读取入口。可选方法不
增加现有实现的抽象义务，也不加入 `/v1`、CLI 或 MCP 的机器工具目录。

查看请求在 API 内经常规认证与 get PEP，由 Engine 查询脱敏记忆；PrivacyProtection 暂存这份
快照，同时 HumanDisclosure 为同一次查看创建 Passkey 挑战。浏览器操作期间不保持
Engine 请求线程，服务端绑定目标、版本、浏览器与初次请求身份，不接受 verified 布尔值。
人的证明通过后仍调用既有 get PEP，复核当前授权、ACTIVE 状态及版本；当前读取用于
复查，组装底稿保持为首次暂存快照。检查通过后，由 API 调用 Engine 的专用查看能力
委托隐私模块核对快照并解密拼装，再经 API 返回独立的用户访问结果，不改写普通
MemoryUnit。还原回调离开 API finish 上下文即拒绝，身份仍由显式 PEP 校验。
`reveal=false` 的片段继续保留占位符；
摘要已舍弃的原话不会重建。审计复用最终 AuditLogger 及可选完整性保护链，在片段解密
和响应释放前记录检查结果，审计失败不释放明文。

选择短期、有界、一次消费的脱敏快照，使原文终止于人的展示响应，不回写 RAG、重排、LLM、共享缓存或
Agent 上下文。拒绝普通原文 recall、可轮询明文结果和可复用解密 bearer token；拒绝把
独立敏感区暴露为普通 KV 前缀；拒绝用机器身份、`verified=true` 或 ROOT/ADMIN 替代
本次人的证明。HTTP 协议见 [S10](../../specs/S10-security.md#隐私保护与用户访问控制)。

快照当前保存在 Runtime 进程内，最多 256 份、合计 8 MiB、最长 300 秒且受挑战有效期
约束。失败、消费、后续请求发现过期或关闭时清理；重启须重新开始，多实例须保持
begin/finish 同实例路由。拒绝持久化还原结果，也不为暂存快照引入通用结果轮询接口。
Passkey 挑战另行保存在凭据状态库；登记不依赖查看快照，在复用原状态库、浏览器
绑定与 RP/origin 配置时，未消费且未过期的登记挑战可跨重启完成。查看流程仍须
重新 begin，不能将挑战持久化等同于查看快照可恢复。

正式接入以安全路由、MemoryAPI 用户访问控制入口及 HumanView 转发能力为契约。独立的
[静态交互示例](../../../examples/privacy-human-view/README.md) 使用虚构数据和模拟验证，
只演示脱敏记忆选择、完整句子查看和版本失效提示，不作为正式前端或真实认证实现。
宿主从已授权结果自动携带目标 Scope、ID 和修订号，用户只看展示名称并操作查看；
展示名称不参与身份推导，静态示例不新增组织、用户目录或业务接口。

### 9.4 支持边界与部署前提

当前支持内联 TEXT/CODE；URI、原模态资产、文档直接编辑和文件监听组合明确拒绝。
既有语料须完成受控迁移，启动检查不把旧库默认视为已保护数据。LLM 分类、分级上云、
分享及软删后自动硬删除只保留配置，不作为生效保障；旧库自动迁移、敏感原值精确检索
和凭据更换/恢复/逐凭据撤销的产品管理入口尚未提供。

真实设备、浏览器及生物验证组合需要部署验收。服务接口隔离不防御恶意同进程插件、
掌握服务账号密钥的主体或能够读取人的浏览器的 Agent；需要更强保证时须增加进程、
账号或设备隔离。认证后才解密表示服务调用边界，不表示验证前服务端绝不持有密钥。

配置契约见 [S08](../../specs/S08-config.md#隐私配置)，运行步骤见
[配置指南](../../zh/API文档/config.md#45-隐私分级与用户访问控制)，完整叠加示例见
[privacy-human-view.yml](../../../examples/privacy-human-view.yml)。

## 10. 拒绝的方案与验收检查清单

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
- 仅恢复 mkfs.ext4 符号链接调用名或仅信任格式化命令退出码：仍依赖工具别名、宿主配置，
  无法证明初始化产物符合契约。采用显式 ext4/journal 参数及实际产物核查；不通过放行
  ext2、无 journal 或外部 journal 来兼容错误产物，也不自动重格式化已有卷。
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
- 管理卷初始化覆盖工具别名解析、真实 ext4 与内部 journal 产物，以及格式化/类型/
  journal 检查失败时关闭临时 mapper、保留镜像且不报告成功；挂载拒绝 ext2、无 journal
  和外部 journal。Linux 普通文件集成测试不替代真实 LUKS 的 init/open/check/close 验收。

- 内置与自定义规则的正反例、精确/前缀类别映射、旧配置与规则叠加、全量校验、有限依据、
  自述依据拒绝以及现有 Passkey/Store 链路；分级默认值、直接写入豁免、还原开关、
  双阶段检测和来源分级继承，以及混合片段、
  伪造/过期引用、正文/标签/元数据保护、原子占位符切分和正则超时。
- 敏感片段真实密文落盘、Scope/用途/版本/指纹绑定、篡改拒绝、派生共享、删除清理和
  失败补偿；与全后端静态加密组合时仍保持正常响应脱敏。
- 人机认证使用真实签名的虚拟认证器覆盖登记、用户验证、来源、计数器、挑战重放与
  并发消费、登记状态库重开；查看时的撤权、版本/状态失效和审计失败不能释放原文。
- 完整 API Key 隐私配置通过真实 HTTP 验证 Alice/Bob 各自访问允许，以及有效 Bob
  Passkey 对 Alice 目标的最终查看者判权拒绝；普通 get/search 始终返回脱敏内容。
- 查看页仅返回当前完整句子，机器 get/search 继续脱敏，响应不可缓存；真实认证设备
  与浏览器兼容性另行验收。行为用例见 `tests/unit/common/security/test_privacy.py`、
  `test_human_authentication.py` 及 `tests/integration/security/`。

逐次复现、测试结果、环境排除及门禁工作记录只在本地 security-plans 保存，不作为
本页的章节追加；本地笔记不纳入 PR，也不成为正式文档的链接依赖。

## 已知遗留

- 私有 KV 以完整 Scope 状态换取跨后端一致性，存在整状态读写放大及同 Scope 写冲突；
  大规模性能与真实 Redis/PostgreSQL 部署须单独验收。切换后端及旧专用表转 KV 的
  数据迁移不自动完成，远端备份、持久化和存储配额由部署方配置。
  多 Runtime 共享后端时，同一 Scope 的业务写入须串行路由；CAS 仅保护私有状态发布，
  不覆盖 checkpoint、业务写入和 confirm 之间的全流程，不提供分布式写锁。

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
- 外部审计锚点尚未实装；KMS/Vault/HSM、跨重启轮换、OAuth
  资源服务器和分布式限流/公平配额都不能作为本分支已完成能力。

接续写入身份保护：公开写入和更新拒绝 continuity_* 系统元数据前缀，调用方不能借此伪造连接来源或控制隐藏/演进行为。仅已授权接续入口通过私有写路径生成这些字段，仍执行统一 WRITE、空间可写、作者归属及审计；显式 event_time 是业务时间声明，不作为可信来源证明。

任务父子关联在 create 的 WRITE 检查之外，另以 `continuity_task_parent` 检查父任务所在项目的内容 READ；该入口与 `continuity_task_read` 同样登记在空间规则表中，复用项目归属/成员/策略判定，避免漏登记后退回不适用的条目级授权并错误拒绝合法项目用户。关联关系不赋予额外的任务或背景访问权限。
