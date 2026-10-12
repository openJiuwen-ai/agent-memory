# 授权判定与资源隔离实装复审

> 编号由 F10 调整为 F11，让位于最新上游的 F10-authentication-kernel。
> 本文保留 2026-09-08 的历史决策与验证基线；2026-09-21 的 PR1 合并及剩余整改见
> [F12](F12-pr2-upstream-integration.md)，以下现行接线说明已同步。

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-08 |
| 影响范围 | `jiuwen_memory/common/security/authorization/`、`jiuwen_memory/api/`、`jiuwen_memory_entry/core/`、`docs/specs/S10-security.md` |
| 测试基线 | `tests/unit` + `tests/integration`（排除外部存储）共 2477 项：2465 通过 / 9 跳过；2 项因本机未安装可选 `torch` 失败，1 项 Windows HTTP socket 瞬态失败并已隔离复验通过。排除整个可选 BGE-M3 测试文件后为 2449 通过 / 9 跳过，唯一 socket 瞬态同样隔离复验通过 |

## 背景

PR1 已同步到最新上游，PR2 的旧实现建立在被重写过的旧目录与旧 API 编排上，不能直接合并。
本次以最新 PR1 为代码基线、以已经合入的 `sec/isolation-api` 接口和安全设计文档为固定契约，
按语义迁移 PR2 实现，并重新执行接口、实现规范和 PR 边界复审。

## 决策

1. 采用“最新 PR1 + 语义重放”的集成方式：只迁移 PR2 的授权实现和必要测试，再适配最新的
   `LocalMemoryAPI` mixin、`MemoryRuntime`、结构化 dispatch 与装配生命周期；不合并旧分支历史。
2. 固定契约继续以 `MemoryAPI` 为唯一 PEP、`Authorizer` 为唯一 PDP。空间事实是 PEP 构造的
   服务端可信输入，不构成保留第二套 `PermissionManager` 生产判定链的理由。
3. `GrantStore` / `DelegationStore` 是授权真源；服务端生成 `grant_id`，撤销按 ID 幂等且单调。
   认证产生的 `RequestSecurityContext` 必须在读取业务事实或产生副作用前完成来源、时效和凭据复核。
4. 集成分支在关闭全部复审阻断项后方可合入；最终关闭证据归档在
   `security-plans/problems/2026-09-08-sec-isolation-final-acceptance.md`。

## 拒绝的方案

- **直接 merge / cherry-pick 旧 `sec/isolation`**：会带回已被上游拆分的 facade 与旧编排方式，
  冲突面大且难以区分真实语义变化。
- **把 `PermissionManager` 与 `Authorizer` 的双判定称为 PR3 前过渡方案**：冻结接口明确禁止旧
  Manager 成为生产路径或第二授权真源；PR3 只负责审计完整性，不能承担授权迁移退场。
- **把凭据在线复核和 `legacy_request_context` 退场推回 PR1 或后续 PR**：PR1 已固定 capability
  与受控构造入口，PR2 的职责正是逐请求消费 Registry 并收敛全部 surface 的显式安全上下文。
- **用绿测代替契约验收**：现有测试中有用 503 固化“Registry 未装配”过渡态的用例，测试通过
  只能证明实现自洽，不能证明符合冻结接口。

## 验证

- `tests/unit` + `tests/integration`（排除需外部服务的 `storage` 子目录）共收集 2477 项：
  2465 通过、9 跳过、3 失败。其中 BGE-M3 文件的 2 项失败由本机未安装可选 `torch` 导致；
  另 1 项是 Windows `urllib` 读取本机 HTTP 404 响应时的 `WinError 10053`，隔离复验通过。
- 排除整个可选 `tests/unit/common/test_bge_m3_embedder.py` 后：2449 通过、9 跳过；同一个
  Windows socket 瞬态在隔离复验中通过，不涉及授权判定或响应断言。
- 相对最新 PR1 基线，全部 82 个现存改动 Python 文件通过 `ruff check` 与
  `ruff format --check`；`compileall` 退出码 0，`git diff --check` 零报。
- 整改前的动态撤权探针曾复现阻断缺陷：同一 grant 在管理 `GrantStore` 中已撤销后，旧
  `PermissionManager` 仍返回允许——双真源当时已实际分叉，而非仅为静态结构问题。这是决策 2
  收敛到单一 PDP 的直接依据；该分叉随已知遗留第 1 项的删除而不再存在。
- 与 PR1 比较仅保留 PR3 既有接口和相关测试适配，未新增或激活审计完整性实现。

## 已知遗留

本节曾宣称四项阻断项「全部整改完成」且「无新增已知遗留」，而
`security-plans/problems/2026-09-08-sec-isolation-reacceptance.md` 的复验结论是**不通过**，
其中「Registry 与 Authenticator 同源装配」「legacy 身份自述退场」两项被判为未修复。该矛盾
由本轮整改关闭，下列每项均给出可核查的判据位置，不再以「已完成」作结论：

1. 空间级生产路径对 `PermissionManager.decide` 的调用已删除——空间级与主体面判定均由
   `Authorizer` 终局（`SpaceAwareAuthorizer` 承接空间轴两族判定），`permission` 段降为
   惰性装配告警（`assembly.py:_warn_inert_permission_namespace`）。旧 Manager 仅保留
   兼容类，已不在生产内容读写路径；实体准入等非授权组件的 decide 不属于该判定链。
2. `CredentialStatusRegistry` 由 composition root 从 Authenticator 真源调和：内核装配期
   先建空 Registry，再从同一上下文构建的 Runtime 认证器绑定真实 issuer/KeyStore，
   不从 root 的 `key_store` 命名空间猜 issuer。SDK 独立装配与 Server AUTO 均在
   `_build_kernel` 完成；Server 显式注入时替换 Registry；
   该接缝不扩充已经冻结的 `Authenticator` 公共契约
   （`LocalMemoryAPI._bind_credential_sources`）。内联与具名两种 API Key 配置均有在线复核证据：
   内联见 `tests/integration/test_dev_fallback_business_continuity.py::test_api_key_cross_principal_still_403`，
   具名见 `tests/integration/test_identity_forgery_rejected.py::test_named_api_key_online_recheck_uses_authenticator_key_store`。
3. dispatch 及全部进程内调用显式接收受控 `security`：`legacy_request_context` 与
   `legacy.py` 已删除（生产目录零引用），`DispatchRequest.security` 为必填，payload 不再
   参与身份构造。进程内调用方按可信来源分流——适配器由 composition root 注入可信安全
   provider（缺失即 `RuntimeError` fail-closed，指引改走 HTTP/API Key）；示例与评测作为
   本地 composition root，经固定 DEV Authenticator 与 `internal_context` 构造开发上下文，
   不按目标 Scope 推导身份。
   `ScopedAuthenticator` 已从生产面删除，
   仅作测试辅助类留在 `tests/support/scoped_authenticator.py`，不注册 `scoped` target、
   不在公共导出面。
4. 可信上下文校验已前移到任何空间事实读取、缓存访问和其他副作用之前
   （`pep_ops.py:_authorize_with_context` 中 `_require_trusted_request` 先于
   `_apply_space_policy_context`，中间仅夹时效判定，对应不变量 24）。

仍在的已知遗留：

- 本机未安装可选 `torch`，因此 BGE-M3 的两个设备选择用例无法在当前环境执行；这不属于
  PR2 授权与隔离路径。另观察到 Windows 本机 HTTP socket 偶发 `WinError 10053`，失败用例
  隔离复验通过。
- 独立 SDK 的空 Registry 缺口已由 F12 关闭；只有未配置 security 的装配仍不推测
  外部认证器的 issuer，未知来源继续 fail-closed。
- PR3 边界未实装：仅保留其既有接口与相关测试适配，未新增或激活审计完整性实现。
