# PR2 上游集成与委托代写收口

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-21 |
| 影响范围 | API PEP/装配、安全 Runtime/Authorizer、接入与评测、S02/S10 |
| 测试基线 | 可用单测与集成测试：2751 passed、6 skipped、0 xfailed；环境排除见验证 |
| Refs | #143 |

## 背景

PR1 已同步最新上游，带来日志隐私、DEV 多身份映射、评测目录和接入变动。
PR1 验证记录明确要求 PR2 消费具名 ADMIN 角色、恢复 agent 代表 user 写入的原始业务
成功断言，不能保留 strict xfail，也不能以普通用户写入替代代理成功路径。
此前独立 SDK 没有绑定真实凭据 Registry，虽然失闭，但无法完成有效 API Key 的正常业务。

## 决策

1. 保留 PR2 历史，以 merge 纳入 PR1；按旧 PR1 的语义基线区分上游改动与 PR2 实装，
   保留最新目录和上游业务逻辑。合并提交与本轮实现、测试、文档提交分开。
2. SDK 与 Server AUTO 共用内核构建的 Runtime、Authorizer 和真实凭据源。显式替换
   Runtime 时替换 Registry，健康检查后生效，不遗留旧 issuer；由持有者统一关闭资源。
3. 经确认采用**私有服务端装配配置**补齐委托绑定，不新增公开方法、认证 target 或
   AuthContext 字段。原认证器先验证凭据，再把其指纹绑定到同一 DelegationStore 的记录。
   该适配仅接受 USER 级单主体 agent 与具名用户委托，不借 ROOT/ADMIN 绕过委托范围。
4. PEP 从有效记录派生资源坐标与作者标记；真实认证 actor 不变。PDP 再查委托真源和
   委托人当前空间内容权，成员撤销立即影响下一次判定。治理操作不借用委托，委托不自动
   创建 fallback 空间。检索沿用派生坐标和两族系统谓词，审计仍记录实际 agent。
5. 服务端投影使用私有保留前缀 `_delegator.`，调用方 metadata 同名前缀在 PEP 剥离。
   author_principal/author_agent 仍为内核保留键。评测入口使用固定 DEV 认证器，绝不从
   被测目标 Scope 生成认证身份。

### 私有配置范围

在既有 `security.<name>.params` 下装配，非 HTTP payload 或动态授权管理接口：

- `delegation_store`：具名 Store，默认 `default`；必须与 PDP 读取的实例相同。
- `delegation_bindings`：凭据指纹 → delegation_id；不是原始 token → 用户的映射。
- `delegations`：可选可信部署初始化记录。可引用已有记录而不提供此项；记录必须有带
  时区的到期时间，仅允许既有 DELEGATABLE_ACTIONS。Store 已撤销记录不能被初始化复活。

初始化记录包含既有 Delegation 字段，例如 delegator 为 `{org: local, user: u1}`，
delegate 为 `{org: local, agent: a1, session: s1}`，可配置 allowed_spaces、
bound_credential_id、bound_session。具体可运行配置见 HTTP 集成测试 fixtures。
生产持久化应选择已有持久 Store；memory Store 的状态不跨进程重启。

## 拒绝的方案

- 修改冻结 AuthContext 或认证 actor 为 user+agent：违反单主体与认证/资源归属分离。
- 从请求 Scope 反推用户：把被访问资源误当身份来源，允许代理自行选择被代理人。
- 双 PDP、固定 DEV allow_all 或保留 xfail：掩盖迁移未完成，无法证明原业务连续性。
- 只验证委托有效、不复核委托人当前成员权：撤销成员后代理仍可继续代写。
- 仅在 Server 注册凭据源：有效 SDK 请求也会失败；从独立命名空间猜测 issuer 则会错源。

## 验证

Python 3.13.9，执行 `tests/unit tests/integration`，关闭 pytest cache 并使用独立 basetemp。
排除外部服务目录 `tests/integration/storage`；当前环境缺少可选 torch、dotenv、requests，
另排除 `test_bge_m3_embedder.py`、`test_middle_e2e_real_llm.py`、
`test_multimodal_adapter.py` 三个文件。结果为 **2751 passed、6 skipped、21 warnings**；
警告为既有 security.local 迁移弃用提示。没有将代理用例排除或标为 xfail。

- HTTP 群体记忆 30 项全通过：JSON/YAML、四写入口、具名管理员、代理和普通用户、
  作者归属、用户/团队落点、检索、跨用户隔离、未绑定 agent 伪造拒绝。
- SDK 覆盖内联/具名认证器、不同 Runtime 名称、两种装配入口、真实 KeyStore 撤销、
  Registry 替换和生命周期。代理专项覆盖缓存上下文失效、过期、未来起效、绑定错配、
  空间/动作限制、成员撤销、治理拒绝、metadata 伪造及真实 actor 审计。
- 原授权/空间判定测试纳入扩大回归。冻结基类和安全值对象与 PR1 无差异；PR3
  audit_integrity 和 ProtectedAuditLogger 与 PR1 无差异，未新增或激活完整性实现。
- 收尾补强跨空间截断审计与委托终局语义后，API/路由专项 86 passed，安全/委托专项
  532 passed；全范围回归复跑仍为 2751 passed、6 skipped。相对 PR1 的 92 个现存
  改动 Python 文件通过 Ruff check/format 检查，compileall 与 git diff --check 通过。

## 已知遗留

本页保留上游集成当时的验证记录，不再作为“无阻断项”的现行结论。同日后续独立验收发现
R1–R9，修复决策与扩大回归见 [F13](F13-pr2-independent-acceptance-closure.md)。这不是
依赖齐全、真实数据库/LLM 部署的全量认证。上述环境排除项应在相应 CI/部署环境补跑。
私有委托配置是服务端装配过渡，动态委托管理
公开接口仍须单独评审；PR3 审计完整性、FS 加密不在本次实装范围。
