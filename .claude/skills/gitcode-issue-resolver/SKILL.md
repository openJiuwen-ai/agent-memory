---
name: gitcode-issue-resolver
description: >
  自动分析和解决 GitCode Issue。获取 issue 详情，分析代码库上下文，
  制定修复方案，实现代码修改，运行测试验证，创建分支和 MR 并关联 issue。
  使用场景：(1) 用户说"分析 issue #N"或"解决 issue #N"
  (2) 自动实现 issue 中描述的 bug 修复或功能需求
  (3) 创建关联 issue 的 MR (4) 本地轮询检查分配给自己的新 issue
---

# Issue Resolver

## 前置条件

- `GITCODE_TOKEN` 已设置
- `.claude/skills/gitcode-config.json` 已配置（upstream + fork 仓库信息，GitCode 系列 skill 共享）
- `requests` 已安装

## 工作流

### Phase 1: 配置校验
检查配置文件、token、fork remote 有效性。

**如果 `.claude/skills/gitcode-config.json` 不存在**，AI 必须立即执行以下步骤生成配置：

1. **Read** `.claude/skills/gitcode-config.example.json`，理解各字段的 `$generate_*` 元注释说明
2. **Bash** 执行 `git remote -v` 和 `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}` 提取 upstream/fork/base_branch
3. **Glob** 扫描工程根目录，**Read** `pyproject.toml` / `go.mod` / `pom.xml` 等（根据存在的文件判断工程类型），推断 `source_dirs`
4. **Read** `.gitignore`（若存在），提取 `exclude_dirs`（以 `/` 结尾且无通配符的目录条目）
5. **Write** 生成的 `.claude/skills/gitcode-config.json`
6. **校验 `.gitignore`**：确认其中含 `.claude/skills/gitcode-config.json` 忽略规则（该文件含个人 fork 信息，禁止入库）。缺失则追加该行；用 `git check-ignore -v .claude/skills/gitcode-config.json` 确认规则生效
7. **展示生成的配置文件内容，要求用户确认正确性**
8. **用户确认后**继续执行本 skill（配置已就绪）

用户确认前不得继续后续流程。配置存在时，直接从配置读取 upstream/fork 的 owner/repo/remote_name 和主干分支名。

### Phase 2: 获取 Issue → Phase 3: 分析代码库
```bash
python gitcode-issue-resolver/scripts/issue_fetcher.py --number <N>
```
判断类型（bug/feature/refactor），定位涉及模块。

### Phase 4: 方案制定 + 人工确认
制定修复方案，在 issue 中发评论，**必须等用户确认**。

### Phase 5: 创建分支 + 实现
基于最新 upstream 主干分支（`$BASE_BRANCH`）创建 Issue 专属分支
（`fix/feat/refactor/issue-N`），禁止直接 commit 到主干。**创建分支前必须先 fetch 最新主干。**

如果 `git checkout -b` 被 Claude Code 权限拒绝，使用：
```bash
git branch <prefix>/issue-<N>
git switch <prefix>/issue-<N>
```

### Phase 6: 验证
```bash
python -m pytest evaluation/smoke_test -q
# 端到端 QA 评测（需配置 JUDGE_* 环境变量）：python evaluation/scripts/run_e2e_eval.py --dataset locomo
```

### Phase 7: 提交 + 创建 MR
- commit: `type(scope): desc\n\nResolves #N`
- **rebase upstream 主干分支**（关键：避免 MR 中出现大量无关合并提交）
- push fork 分支（如被 Claude Code 拒绝，提示用户在终端手动推送）
- 调用 `gitcode_client.create_pull_request()` 创建 MR
- **创建后验证 PR 提交数**：确认 PR 只包含本次修复的提交
- MR 模板和评论模板见 `references/workflow-detail.md`

## 轮询模式
```bash
python gitcode-issue-resolver/scripts/issue_poller.py --config .claude/skills/gitcode-config.json
```
触发：assign / @mention / 标签 `auto-resolve`。

## 禁止行为
- 禁止直接 commit 到主干分支（`$BASE_BRANCH`）
- 禁止推到主仓（`$UPSTREAM_REMOTE`）
- 只能推到 fork（`$FORK_REMOTE`）的 Issue 专属分支
