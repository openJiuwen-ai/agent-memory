---
name: gitcode-committer
description: >
  GitCode PR 检视技能。支持五种命令：
  (1) gitcode-review <N> - 检视 PR #N (2) gitcode-comment <N> <comment> - 提交检视意见
  (3) gitcode-approve <N> - 批准 PR (4) gitcode-check <N> - 检查合并设置
  (5) gitcode-get - 获取艾特你的 PR 通知
  需要 GITCODE_TOKEN 环境变量。
---

# GitCode PR Review

## 前置条件

- `GITCODE_TOKEN` 已设置
- `.claude/skills/gitcode-config.json` 配置文件（GitCode 系列 skill 共享）

### 配置文件自动生成（首次运行必做）

**如果 `.claude/skills/gitcode-config.json` 不存在**，AI 必须立即执行以下步骤生成配置：

1. **Read** `.claude/skills/gitcode-config.example.json`，理解各字段的 `$generate_*` 元注释说明
2. **Bash** 执行 `git remote -v` 和 `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}` 提取 upstream/fork/base_branch
3. **Glob** 扫描工程根目录，**Read** `pyproject.toml` / `go.mod` / `pom.xml` 等（根据存在的文件判断工程类型），推断 `source_dirs`
4. **Read** `.gitignore`（若存在），提取 `exclude_dirs`（以 `/` 结尾且无通配符的目录条目）
5. **Write** 生成的 `.claude/skills/gitcode-config.json`
6. **校验 `.gitignore`**：确认其中含 `.claude/skills/gitcode-config.json` 忽略规则（该文件含个人 fork 信息，禁止入库）。缺失则追加该行；用 `git check-ignore -v .claude/skills/gitcode-config.json` 确认规则生效
7. **展示生成的配置文件内容，要求用户确认正确性**
8. **用户确认后**继续执行本 skill（配置已就绪）

用户确认前不得继续后续流程。

## 五种命令

### 1. gitcode-review <N>
获取 PR 详情、文件、commits、评论，分析变更并生成检视报告。报告格式和检视策略见 `references/committer-reference.md`。

### 2. gitcode-comment <N> <comment>
向 PR 提交评论。

**必须调用 `pr_commenter.py` 脚本，禁止裸 curl 拼 JSON body**（shell 插值会破坏中文编码）：

```bash
python .claude/skills/gitcode-committer/scripts/pr_commenter.py comment <N> "<comment>"
```

评论内容较长时，先写入临时文件再通过脚本的 stdin 传入，或直接修改脚本调用 `comment_on_pr(client, N, body)`。

### 3. gitcode-approve <N>
评论 `/approve` + `/lgtm`，自动检查：
- `close_related_issue` 为 true → 自动关闭
- 无关联 issue → 提醒提交人

### 4. gitcode-check <N>
检查合并设置，提供 squash commit message 建议。

### 5. gitcode-get [--hours N] [--all]
通过 Notifications API 获取艾特你的 PR 列表。

## API 端点速查

| 操作 | 端点 |
|------|------|
| PR 详情/文件/commits | `GET /repos/:owner/:repo/pulls/:number[/files|/commits]` |
| PR 评论 | `GET/POST /repos/:owner/:repo/pulls/:number/comments` |
| 仓库通知 | `GET /repos/:owner/:repo/notifications?type=referer` |

认证用 `PRIVATE-TOKEN` header。完整 API 列表见 `references/committer-reference.md`。

## 注意事项

- 大型 PR 只分析核心文件
- CI 失败的 PR 优先关注 CI 问题
- 评论客观、具体、有建设性
