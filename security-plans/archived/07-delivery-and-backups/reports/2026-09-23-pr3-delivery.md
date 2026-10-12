# PR3 交付整理记录

| 项 | 值 |
|---|---|
| 日期 | 2026-09-23 |
| 分支 | `sec/audit`，目标 `origin/sec/audit` |
| 基线 | 当前 PR2 `sec/isolation`，不刷新后续上游 |
| 关联 issue | #143，负责人确认 |
| 提交结构 | PR2 之上一个 PR3 提交，统一包含实现、测试、文档 |

## 整理内容

1. 将七个开发/修复提交整理为一个实现提交，保留第七轮已通过的实现内容。
2. 将后四轮 23 项独立样本归入正式 tests/，不再依赖本地 redteam 目录：
   - `tests/unit/common/security/audit_integrity/test_rotation_regressions.py`：5 项。
   - `tests/unit/common/audit/test_detail_security_regressions.py`：13 项。
   - `tests/integration/audit_integrity/test_write_boundaries.py`：5 项。
3. 同步 F05/F09、S07/S10、模块 AGENTS 的实现状态，并纳入工作区中已确认的
   “每个实现 PR 一个合并提交”规约修改。本次未修改受验实现代码。
4. 将[第七轮报告](2026-09-23-pr3-seventh-acceptance.md)及其
   [8 组接口比对证据](2026-09-23-pr3-seventh-contracts.json)纳入提交。
   历史红队、原始 pytest XML、历史备份和临时测试目录保留本地，不纳入交付。

## 提交前验证

| 检查 | 实际结果 |
|---|---|
| 全量单测 + PR3 集成 | **3126 passed、12 skipped**，140.01 秒 |
| 本次归档 23 项正式回归 | 23 项全部通过，包含在上述 3126 中 |
| PR3 相对当前 PR2 的改动 Python 文件 ruff check | 通过 |
| 三个新增测试文件 ruff format --check | 通过 |
| diff 空白检查 | 通过 |
| 实现变更 | 相对第七轮受验实现无改动；不改变冻结接口结论 |

```powershell
.venv/Scripts/python.exe -m pytest tests/unit tests/integration/test_audit_integrity_flow.py tests/integration/audit_integrity -o addopts='' -q -p no:cacheprovider --tb=short --basetemp=D:/agent-memory/.tmp-pr3-delivery-next
```

本地原始结果为 `2026-09-23-pr3-delivery-regression.xml`。首次收集因缺少已声明的开发依赖
`requests` 和 `python-dotenv` 失败；补齐仓库虚拟环境后重新运行，得到上述结果。

12 项跳过分别为：MCP 模块缺依赖 1、torch 缺依赖 2、HanLP 模型默认关闭 2、真实 LLM
默认关闭 4、真实 Redis 双实例默认关闭 1、Elasticsearch/Milvus 客户端缺依赖各 1。
19 个 warning 来自现有 asyncio marker 和旧 security.local 配置弃用提示。
没有把这些跳过算作通过，也没有宣称全仓外部服务集成或实际 MCP 传输已验证。

## 历史整理与推送保护

PR2 基线为 `93774670aabdbb10d7e3d6893d2222546c93da9b`。
整理前本地 HEAD 为 `526adedb6616bf852f32ea9def39de9bbfb6b4cf`；
远端仍为旧接口分支 `42d3713ec9b224c5596fa79a13f0d4bd67f0841f`。
两者均已保存在本地 `security-plans/pr3-before-delivery-20260923.bundle`，bundle 校验通过。
工作区原有文档修改另存于 `security-plans/pr3-worktree-before-delivery-20260923.patch`。

推送使用针对上述旧远端提交的显式 `--force-with-lease`，远端发生并发修改时拒绝覆盖。
不修改 PR1、PR2 或 upstream 分支。推送后核对远端 HEAD、本地 HEAD 及 PR2 之后提交数量。

## 保留的能力边界

本次完成代码、测试和文档的提交交付，不扩张第七轮的安全能力声明：实际 MCP 传输与外部
服务部署仍需对应环境验证；无外部锚点不能识别合法历史前缀回滚；LocalKeyProvider 轮换
状态不跨进程重启保存；产品级外部锚点及旧生产库迁移不在本期已验证范围。
