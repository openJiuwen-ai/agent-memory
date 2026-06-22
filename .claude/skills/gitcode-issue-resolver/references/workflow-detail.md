# Issue Resolver 详细工作流

## 配置文件自动生成（首次运行必做）

**如果 `.claude/skills/gitcode-config.json` 不存在**，AI 必须立即执行以下步骤生成配置：

1. **Read** `.claude/skills/gitcode-config.example.json`，理解各字段的 `$generate_*` 元注释说明
2. **Bash** 执行 `git remote -v` 和 `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}` 提取 upstream/fork/base_branch
3. **Glob** 扫描工程根目录，**Read** `pyproject.toml` / `go.mod` / `pom.xml` 等（根据存在的文件判断工程类型），推断 `source_dirs`
4. **Read** `.gitignore`（若存在），提取 `exclude_dirs`（以 `/` 结尾且无通配符的目录条目）
5. **Write** 生成的 `.claude/skills/gitcode-config.json`
6. **继续执行本 skill**（配置已就绪）

生成完成后不需要用户手动重新运行，直接进入后续流程。

## Remote / 分支解析（所有命令前先执行）

**配置文件驱动**：所有 remote/分支信息从配置读取，不回退 git remote 探测。
后续命令统一用 `$UPSTREAM_REMOTE`/`$FORK_REMOTE`/`$BASE_BRANCH`：

```bash
CONFIG=.claude/skills/gitcode-config.json
# （配置缺失时上述自动生成步骤已执行，此处直接读取）
UPSTREAM_REMOTE=$(jq -r '.upstream.remote_name // "origin"' "$CONFIG")
FORK_REMOTE=$(jq -r '.fork.remote_name // empty' "$CONFIG")
BASE_BRANCH=$(jq -r '.upstream.base_branch // empty' "$CONFIG")
BASE_BRANCH=${BASE_BRANCH:-main}
echo "upstream=$UPSTREAM_REMOTE fork=$FORK_REMOTE base=$BASE_BRANCH"
```

## Phase 1: 配置校验命令

```bash
# 检查配置文件
cat .claude/skills/gitcode-config.json

# 检查 token
echo $GITCODE_TOKEN

# 验证 client 初始化
GITCODE_TOKEN=<token> python gitcode-issue-resolver/scripts/issue_fetcher.py \
    --list --state open 2>&1 | head -5

# 校验 fork remote
git remote get-url "$FORK_REMOTE"
```

必填字段（配置文件或 git remote 中必须有）：`upstream.owner`, `upstream.repo`

**绝不要推送到主仓（`$UPSTREAM_REMOTE`），必须推送到 fork remote（`$FORK_REMOTE`）。**

## Phase 5: 分支创建命令

```bash
# 1. 先 fetch 最新 upstream 主干（关键：避免后续 MR 污染）
git fetch $UPSTREAM_REMOTE $BASE_BRANCH

# 2. 保存当前工作区变更
git stash

# 3. 基于最新主干创建分支
git switch $UPSTREAM_REMOTE/$BASE_BRANCH -c <prefix>/issue-<N>  # fix/ feat/ refactor/

# 如果 git switch 被拒绝（权限系统），分两步：
git branch <prefix>/issue-<N> $UPSTREAM_REMOTE/$BASE_BRANCH
git switch <prefix>/issue-<N>

# 4. 恢复工作区变更（如果有）
git stash pop
```

分支命名：bug→`fix/issue-N`, feature→`feat/issue-N`, refactor→`refactor/issue-N`

## Phase 7: 提交与 MR 创建

```bash
# 提交
git add <files>  # 实际应从配置 source_dirs 循环暂存，参考 gitcode-smart-commit
git commit -m "<type>(scope): <desc>

Resolves #<N>"

# Rebase（关键：确保 MR 只有本次修复的提交）
git fetch $UPSTREAM_REMOTE $BASE_BRANCH && git rebase $UPSTREAM_REMOTE/$BASE_BRANCH

# 推送
git push $FORK_REMOTE <branch>
# 如需 force push（例如修正了已经推送的分支）：
git push $FORK_REMOTE <branch> --force-with-lease
```

**MR 创建 (create_pull_request):**

```bash
GITCODE_TOKEN=$GITCODE_TOKEN \
python -c "
import sys, os, json
sys.path.insert(0, 'gitcode-issue-resolver/scripts')
from gitcode_client import GitCodeClient

client = GitCodeClient.from_config('.claude/skills/gitcode-config.json')
result = client.create_pull_request(
    title='<type>(scope): <desc>',
    # head 格式: fork_owner/fork_repo:branch
    head='<fork_owner>/<fork_repo>:<branch>',
    base=client.base_branch,
    body='''<MR_BODY>''',
)
print(json.dumps(result, indent=2, ensure_ascii=False))
"
```

**MR 创建后验证：**

```bash
# 确认 PR 提交数
GITCODE_TOKEN=$GITCODE_TOKEN \
python -c "
import sys, os, requests
sys.path.insert(0, 'gitcode-issue-resolver/scripts')
# ... 调用 /repos/{owner}/{repo}/pulls/{N}/commits
# 确认 commits 数量只包含本次修复
"
```

**MR body 模板：**

```markdown
What type of PR is this?
/kind <bug|feature|refactor>

## Self-checklist
- [x] 设计 - [x] 测试 - [x] 验证
- [ ] 接口 - [ ] 文档

## 修复内容

### 问题描述
<简要说明 bug 表现和影响>

### 修改内容
**文件:行号** - 修改说明
<代码 diff 或关键代码片段>

### 验证结果
<测试通过情况 + 预期行为验证>

Resolves #<N>
```

从返回的 `html_url` 字段提取 MR 链接展示给用户。

## 评论模板

### 分析评论
```
## 🤖 自动分析结果
### 问题定位 / 修复方案 / 影响范围
---
*由 gitcode-issue-resolver 自动生成*
```

### 修复完成评论
```
## ✅ 修复已提交
分支: `<branch>` | 提交: `<hash>` | MR: <url>
---
*由 gitcode-issue-resolver 自动生成*
```

## 常见问题

### MR 中包含大量无关提交
原因：分支基于旧主干，包含多次 merge 主干的合并提交。
解决：
1. 基于最新 `$UPSTREAM_REMOTE/$BASE_BRANCH` 创建新分支
2. cherry-pick 修复提交到新分支
3. 推送新分支，用新 head 创建 MR（GitCode API 不支持修改已有 PR 的 head）

### `create_pull_request` 返回 400: Project not found
原因：head 格式错误。GitCode API 需要 `owner/repo:branch`，不是 `owner:branch`。
解决：使用 `fork_owner/fork_repo:branch` 格式。

### 推送被 Claude Code 权限拒绝
解决：提示用户在终端手动执行 `git push $FORK_REMOTE <branch>`。
