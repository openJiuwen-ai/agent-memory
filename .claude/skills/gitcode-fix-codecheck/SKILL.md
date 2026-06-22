---
name: gitcode-fix-codecheck
description: >
  自动修复 GitCode PR 的 CodeCheck 静态检查问题。
  使用场景：(1) 用户说"修复 PR #N 的 codecheck"或"fix codecheck"
  (2) PR 流水线中静态检查 FAILED，需要获取报告并修复
  (3) 用户提供 GitCode PR 链接，需要查看并修复 CI 失败
  需要 agent-browser 和 GITCODE_TOKEN 环境变量。
---

# Fix CodeCheck

## 前置条件

1. `agent-browser` 已安装
2. `GITCODE_TOKEN` 已设置
3. `.claude/skills/gitcode-config.json` 配置文件（GitCode 系列 skill 共享）

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

## 工作流

### Phase 1: 获取 PR 信息

通过 GitCode API 获取 PR 评论，找到 CI bot 的流水线结果评论，提取 CodeCheck 报告链接。

```bash
curl -s -H "PRIVATE-TOKEN: $GITCODE_TOKEN" \
  "https://gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{pr_number}/comments"
```

- PR 评论必须用 `/pulls/{n}/comments`，不能用 `/issues/{n}/comments`
- 静态检查 SUCCESS 则无需修复，直接返回

### Phase 2: 抓取 CodeCheck 报告

openlibing.com 需要 GitCode 账号登录。CI 评论中的报告链接含 `entryCheckDashCode`，实际访问需改为 `entryCheckDash`。

**登录流程详见** `references/openlibing-login.md`。

登录后用 agent-browser 打开报告页，snapshot 获取问题列表：

```bash
AGENT_BROWSER_DEFAULT_TIMEOUT=120000 agent-browser open "<report_url>"
agent-browser wait 20000         # 等待登录跳转 + SPA 渲染
agent-browser snapshot -c        # 获取问题列表
```

报告页有三个 tab：概览、代码问题 N、敏感词问题 N。点击对应 tab 查看问题，snapshot 中每个问题包含：规则、描述、级别、状态、代码片段。

### Phase 3: 解析 → Phase 4: 修复 → Phase 5: 提交推送

按规则修复代码，验证后提交推送 fork remote。

## 常见规则及修复

| 规则 | 修复方式 |
|------|---------|
| G.CMT.01 缺少 docstring | 为公共函数添加 docstring |
| G.CMT.03 docstring 缩进 | docstring 紧跟函数声明下一行，缩进 4 空格 |
| G.FMT.01 行宽超限 | 拆分长行（≤120 字符） |
| G.FMT.09 多行 comprehension | 合并为单行，或用 for 循环替代 |
| G.PSL.03 sys.path.insert(0, ...) | 改为 sys.path.append() 并加 guard check |
| G.EDV.04 subprocess 调 shell | 用 shutil.which() 获取绝对路径，显式 shell=False |
| G.EDV.05 外部命令无绝对路径 | 用 shutil.which() 获取绝对路径 |
| WordsTool.doc1 敏感词 | 替换为标准术语（如 HBM → High Bandwidth Memory） |
| G.NAM.01-03 命名规范 | 按 PascalCase/snake_case/UPPER_SNAKE_CASE |

## 华为 CodeCheck 规则速查

- **G.CMT**: 01 公共函数 docstring, 03 缩进, 04 内容规范
- **G.NAM**: 01 类 PascalCase, 02 函数 snake_case, 03 常量 UPPER
- **G.FMT**: 01 行宽≤120, 02 4 空格缩进, 03 import 排序, 09 单行 comprehension
- **G.SEC**: 01 禁止硬编码密钥, 02 禁止 eval/exec
- **G.PSL**: 03 禁止 sys.path.insert(0, ...)
- **G.EDV**: 04 禁止 shell=True, 05 外部命令需绝对路径

## 注意事项

- openlibing.com 需要 GitCode 登录，不能用 curl
- 登录后等待 20 秒以上让 SPA 完成跳转
- 复选框用 `eval` + `dispatchEvent`，直接 `click` 无效
- 只推送到 fork remote，不推 origin
