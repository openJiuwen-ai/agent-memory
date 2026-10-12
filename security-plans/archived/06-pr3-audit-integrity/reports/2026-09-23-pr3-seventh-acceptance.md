# PR3 第七轮独立验收

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-23 |
| 对象 | `sec/audit` 第六轮问题修复版本 |
| 基线 | 当前 `sec/isolation`，不纳入后续上游翻新 |
| 结论 | **本轮实现复验通过：历史问题已关闭，未发现新的阻塞性实现问题；最终交付事项见第 5 节** |
| 前次记录 | 本地历史归档 `2026-09-22-pr3-sixth-acceptance.md` |
| 验证证据 | 本地原始 XML `2026-09-23-pr3-seventh-regression.xml`、[契约比对](2026-09-23-pr3-seventh-contracts.json) |

## 1. 验收对象与三项要求

受验提交为 `526adedb6616bf852f32ea9def39de9bbfb6b4cf`，上一轮为
`cfe65aa2433feb973f6ba0fb3f8ea4ba5e2b91cc`。
当前 PR2 与 `git merge-base sec/isolation HEAD` 均为
`93774670aabdbb10d7e3d6893d2222546c93da9b`。

| 要求 | 本轮结论 |
|---|---|
| 基于当前 PR2 | **通过**，当前 `sec/isolation` 是受验提交祖先，不要求此次刷新最新上游 |
| 严格遵守冻结接口，无增删 | **通过本轮检查**，8 个核心契约/导出文件去除 docstring 后 AST 与 PR2 一致；相关契约回归通过 |
| 规划功能与代码、文档一致 | **本轮实现复验通过**，此前已提出的实现缺口关闭；已同步当前实现说明。MCP 实际传输和全仓门禁仍有验证边界，正式测试与提交整理仍待交付 |

接口比对覆盖 MemoryAPI、api 公共导出、AuditLogger、审计完整性 base/chain_store/包导出、
KeyProvider、AuditEvent。没有将实现类履行已冻结 capability 的方法算作新增接口。
结论针对上述受验实现与明确测试范围，不替代尚未完成的最终交付门禁。

## 2. V1 关闭证据

最新修改移除了 `_NON_SENSITIVE_ID_SUFFIXES` 及任意 `_id` / `_fp` 豁免，改为
`_KNOWN_NON_SENSITIVE_FIELDS`，精确列出 `credential_id`、`key_fp`。
匹配使用完整字段名，不依据后缀、前缀或通配。其他字段继续在键名截断前做敏感词干判断。

第六轮的四项真实 SQLite 样本本轮全部通过：

| 样本 | 实测结果 |
|---|---|
| `credential_id` 非秘密凭据标识 | 保留 |
| `key_fp` 非秘密指纹标识 | 保留 |
| `raw_password_id` 未批准敏感扩展 | 脱敏，不再原值入库 |
| `kdf_output_fp` 未批准敏感扩展 | 脱敏，不再原值入库 |

因此，V1 以及此前 U2 的具体未关闭问题可以关闭。当前规则的准确边界是**已知非秘密字段
精确豁免 + 敏感词干脱敏 + 有界写入**，不是对任意字符串的秘密内容识别。服务器调用点
仍须保证允许字段携带真实的非秘密标识；不承诺把秘密放进任意普通字段都能自动识别。

## 3. 历史问题与规划覆盖复核

累计 **52 项独立故障/边界样本全部通过**，包含前六轮的 19 + 10 + 12 + 4 + 3 + 4 项。

| 方面 | 本轮确认 |
|---|---|
| 普通审计、完整性 provider、store 与 key capability 分离 | 保持既有接口与具名装配；相关装配/能力门控回归通过 |
| 链式证明、head/genesis、schema、增量高水位和有界扫描 | 历史篡改与缺失样本、正式单测通过 |
| SQLite 原子追加、重开续链、health 同连接互斥 | 既有线程/多连接回归与独立故障样本通过；不扩称生产负载或所有数据库已验证 |
| 关键写失败传播与 mutation 隔离 | 真实 SQLite 失败、显式 mutation 及路由隐式建空间样本通过 |
| 密钥轮换 | 正常最小 CAS 预算、签名期重复轮换元数据一致性、持续轮换总工作量耗尽样本通过 |
| 外部锚点契约 | fake anchor 的状态与前缀核对回归通过；产品级外部锚点仍不在本期交付 |
| VERIFY_AUDIT、PEP、WorkloadGuard | 授权、参数限制、独立预算、异常释放等回归通过 |
| detail 字段与大小边界 | 敏感词干、精确标识豁免、键/值/数量限制、系统字段保留等旧样本通过 |

本轮没有新增阻塞性代码问题。已关闭的问题不会因为旧报告仍记载历史失败而重新打开；
当前结论以本报告和本次 XML 为准，旧报告保持其受验版本的事实。

## 4. 测试结果与验证边界

| 检查 | 本轮实际结果 |
|---|---|
| 累计 52 项红队 + 安全/审计/API/接入层回归 + PR3 集成 | **1705 passed, 1 skipped**，88.15 秒 |
| 52 项独立样本 | 全部通过，已包含在 1705 中，不重复相加 |
| 当前 PR2 祖先关系 | 通过 |
| 8 组冻结契约 AST | 均一致 |
| PR3 相对 PR2 全部改动 Python 文件 ruff | All checks passed |
| `git diff --check sec/isolation HEAD` | 通过 |

环境为仓库 `.venv`。唯一跳过是缺少 `mcp` 依赖的模块，**未验证实际 MCP 传输**。
没有运行全仓所有集成测试或计划 §15.3 的全部门禁命令，也未调用真实外部锚点、验证生产迁移。
不能将本次 1705 项范围回归称作“全仓门禁全部完成”。

```powershell
.venv/Scripts/python.exe -m pytest security-plans/redteam/test_pr3_acceptance_20260922.py security-plans/redteam/test_pr3_reacceptance_20260922.py security-plans/redteam/test_pr3_third_acceptance_20260922.py security-plans/redteam/test_pr3_fourth_acceptance_20260922.py security-plans/redteam/test_pr3_fifth_acceptance_20260922.py security-plans/redteam/test_pr3_sixth_acceptance_20260922.py tests/unit/common/security tests/unit/common/audit tests/unit/api tests/unit/jiuwen_memory_entry tests/integration/test_audit_integrity_flow.py -o addopts='' -q -p no:cacheprovider --tb=short --basetemp=D:/agent-memory/.tmp-pr3-seventh-next
```

复跑使用新的空 basetemp 路径。以下已声明边界不重复列为新增缺陷：无锚点不能识别合法
历史前缀整体回滚；LocalKeyProvider 轮换状态不跨重启保存；产品级锚点不在本期；无现网
旧无签名库待迁移是实现方声明，本轮未另行检查生产部署。

## 5. 最终交付仍需完成

这些是交付事项，不是本轮新增的实现缺陷：

1. **正式测试归档**：后续几轮修复的关键独立样本仍位于被忽略的 `security-plans/redteam/`；
   最新提交只改实现文件，没有 `tests/` 变更。将关键回归纳入正式测试，确保 CI 能重现。
2. **文档与门禁**：本轮已同步 F05/F09、S07/S10、api/common AGENTS 的当前状态；文档仍在
   工作区。最终交付时纳入提交，并完成所需全仓/部署门禁，保留 MCP 未验证事实。
3. **单提交交付**：当前 PR2 后有 7 个 PR3 开发/修复提交；最终按负责人约定整理为每个
   实现 PR **一个合并提交**，统一包含代码、测试和相关文档，不恢复三连提交要求。

本轮只更新验收报告、测试证据和实现状态文档；没有修改实现、移动测试、改写历史、提交
或推送，保留了已有工作区修改。

## 6. 验收后的交付整理（2026-09-23）

第 5 节记录验收完成时的状态。随后负责人授权整理、提交和推送：后四轮 23 项独立样本
已归入正式测试，相关文档一并纳入 PR3 单提交交付，关联 issue 为 #143。
本次整理未修改受验实现代码，新增验证结果与仍保留的环境边界见
[交付记录](2026-09-23-pr3-delivery.md)。
