# PR2 授权与隔离最终验收

## 结论

**通过，可以合入。** 当前 `sec/isolation` 以最新 PR1 `sec/auth-encryption` 为基线，严格保留
`sec/isolation-api` 已固定的公共接口，只实装 PR2 授权与隔离能力；PR3 审计完整性仍保持空接口
状态。此前复审与回应文档中的阻断项均已关闭，DEV、API Key、HTTP、进程内适配器和空间授权
业务链路可按预期运行。

按本次明确要求，最终交付相对 PR1 **合并为一个完整提交**，不按仓库通常的代码、测试、文档
三提交方式拆分。

## 验收范围与基线

- 实现基线：`sec/auth-encryption` 的最新 PR1。
- 接口基线：`sec/isolation-api` 与 `docs/specs/S10-security.md`。
- 复核对象：PR2 授权实现、PEP/PDP 装配、凭据在线复核、全部接入面身份传递、空间授权、
  JiuwenSwarm 进程内适配，以及历史复审问题的回应与修复。
- PR 边界：未新增或激活 `audit_integrity_impl`，PR3 仍仅保留固定接口。

## 验收结果

| 检查项 | 结论 | 证据摘要 |
|---|---|---|
| 固定接口无增删 | 通过 | `authentication/base.py`、`authorization/base.py` 与 `sec/isolation-api` 无差异；未向公共基类加入凭据源或 DelegationStore 管理钩子 |
| 无额外公共认证器 | 通过 | 生产代码只有 `authentication/authentication_impl/dev_authenticator.py` 一个 DEV 实现；公共 `ScopedAuthenticator` 已删除，仅在 `tests/support/` 保留测试替身 |
| 唯一 PEP/PDP | 通过 | `MemoryAPI` 是唯一 PEP，Runtime 与 PEP 绑定同一 Authorizer 实例；显式 Runtime 也会回绑，test-only Authorizer 在生产装配和 Server 启动时拒绝 |
| 凭据撤销同源复核 | 通过 | Registry 从实际 Authenticator 的实现私有钩子取得签发真源；内联、具名 API Key 与显式 Runtime 路径均覆盖 |
| Grant / Delegation 语义 | 通过 | 服务端生成 `grant_id`，按 ID 幂等撤销；Space Delegation 在 Grant 查询前判定，合法委托不受无关 GrantStore 故障影响 |
| 身份不可由业务参数自述 | 通过 | `DispatchRequest.security` 必填，legacy 身份桥已删除；payload actor/request_id/surface 不参与可信上下文构造 |
| JiuwenSwarm 进程内入口 | 通过 | composition root 必须注入 `security_provider`，按每次真实调用身份生成上下文；缺失时 fail-closed 并提示改走 HTTP/API Key |
| DEV 业务连续性 | 通过 | DEV 的 `role=ROOT` 经真实 Authorizer 放行跨组织 add/get；角色降为 USER 即 403；不恢复空 Scope 特权 |
| 旧临时放行件退场 | 通过 | `with_local_dev_security()` 只补 DEV Runtime，不再注入 `AllowAllPermissionManager`；该兼容类仍可供测试/旧接口使用，但不激活、不进入生产判定链 |
| API Key / Trusted 隔离 | 通过 | 非 ROOT 跨 org 或跨主体仍拒绝；显式配置不被 DEV 适配器覆盖 |
| PR3 边界 | 通过 | 未出现 `audit_integrity_impl`、HMAC 链或锚点产品实现，既有 PR3 接口没有删除 |

## 本轮补充修正

验收期间除同事整改外，又关闭了以下边界问题：

1. 删除对冻结 `Authenticator` / `Authorizer` 基类的额外公共方法，改为实现层私有装配钩子。
2. 显式 `SecurityRuntime` 的 Authorizer 回绑到 PEP，防止同类型不同实例形成两套授权真源。
3. Server 拒绝 `is_test_only()` Authorizer，避免显式注入绕过生产装配守卫。
4. Space Delegation 调整为先于 GrantStore 查询，严格遵守固定判定顺序。
5. 删除 DEV 适配器残留的 `permission.default=allow_all` 注入；业务连续性由 ROOT 角色闸门保障。
6. 同步修复当前代码与 F05/F09/F10、S07/S10、模块 `AGENTS.md` 的状态和路径描述。

## 验证记录

- 相对最新 PR1 改动的 82 个现存 Python 文件：`ruff check` 通过，`ruff format --check` 通过。
- `compileall`：通过。
- `git diff --check`：通过。
- `tests/unit` + `tests/integration`（排除需外部服务的 `tests/integration/storage`）：
  2477 项中 2465 通过、9 跳过；2 项因当前环境未安装可选 `torch` 失败；1 项 Windows 本机
  HTTP socket 偶发 `WinError 10053`，对应 404 用例隔离复验通过。
- 排除整个可选 `tests/unit/common/test_bge_m3_embedder.py` 后：2449 通过、9 跳过；同一 socket
  瞬态用例隔离复验通过。该失败发生在 `urllib` 读取本机响应阶段，不涉及授权结果断言。
- PR2 定向链路：DEV 业务连续性、身份伪造拒绝、空间授权、JiuwenSwarm、HTTP 安全、显式
  Runtime 同一 PDP、test-only Authorizer 拒绝等用例通过。

## 非阻断说明

- 当前验收机没有安装可选 `torch`，因此 BGE-M3 的两个设备选择测试不能在本机验证；与 PR2
  授权/隔离代码无关。
- Windows 本机 HTTP 测试观察到偶发连接被系统中止，失败用例隔离重跑通过；建议 CI 在目标
  Linux 环境继续保留全量门禁。
- `AllowAllAuthorizer`、`AllowAllPermissionManager` 的兼容实现文件没有从仓库删除，但生产
  装配会拒绝前者，DEV 适配器不再注入后者；两者均不是当前生产业务链的中间件。
