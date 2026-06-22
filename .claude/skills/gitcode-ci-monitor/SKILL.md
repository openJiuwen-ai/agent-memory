---
name: gitcode-ci-monitor
description: >
  监控 GitCode PR 流水线状态。获取静态检查/UT/ST 结果，
  静态检查 FAILED 时自动调用 gitcode-fix-codecheck 修复。
  可配合 /loop 实现持续轮询。
---

# CI Monitor

## 前置条件

- `GITCODE_TOKEN` 已设置（会话间不持久，每次先检查）
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

## 流程

1. **检查 Token**：`echo $GITCODE_TOKEN`，没有则向用户索要
2. **获取流水线**：curl pulls API 取最新流水线评论，一行命令完成提取+解析（详见 `references/pipeline-api.md`）
3. **响应**：

| 结果 | 行为 |
|------|------|
| 全部 PASS | 汇报通过 |
| CodeCheck FAILED | 展示失败项，询问是否运行 `gitcode-fix-codecheck` |
| UT/ST FAILED | 展示失败链接 |
| 无流水线评论 | 提示流水线可能尚未触发 |

## 关键陷阱

- PR 评论必须用 `/pulls/{n}/comments`（不是 `/issues/`）
- Token 用 `PRIVATE-TOKEN` header
- Windows 设 `PYTHONIOENCODING=utf-8`，用 `python` 非 `python3`
- 状态是 HTML entity（`&#9989;`=✅, `&#10060;`=❌, `&#128346;`=🟫）
- API 返回的 HTML 不入上下文——解析用 `references/pipeline-api.md` 中的单条命令直接输出结构化结果
