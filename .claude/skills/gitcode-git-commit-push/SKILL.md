---
name: gitcode-git-commit-push
description: >
  非交互式 squash rebase 和 GitCode MR 创建。
  日常 commit/push/branch 管理请用 gitcode-smart-commit。
---

# Squash & MR 创建

日常提交流程见 `gitcode-smart-commit` skill。本 skill 覆盖特殊操作。

## Remote 解析（别名无关）

**配置文件驱动**：所有 remote/分支信息从 `.claude/skills/gitcode-config.json` 读取，配置缺失时 AI 自动生成。
命令统一用 `$UPSTREAM_REMOTE`/`$FORK_REMOTE`/`$BASE_BRANCH`，不要硬编码字面量。

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

### 解析逻辑参考

```bash
CONFIG=.claude/skills/gitcode-config.json
# （配置缺失时上述自动生成步骤已执行，此处直接读取）
UPSTREAM_REMOTE=$(jq -r '.upstream.remote_name // "origin"' "$CONFIG")
FORK_REMOTE=$(jq -r '.fork.remote_name // empty' "$CONFIG")
BASE_BRANCH=$(jq -r '.upstream.base_branch // empty' "$CONFIG")
BASE_BRANCH=${BASE_BRANCH:-main}   # 配置字段为空时回退到 main
echo "upstream=$UPSTREAM_REMOTE fork=$FORK_REMOTE base=$BASE_BRANCH"
```

## Non-interactive Squash

```bash
git fetch $UPSTREAM_REMOTE $BASE_BRANCH && git rebase $UPSTREAM_REMOTE/$BASE_BRANCH
GIT_SEQUENCE_EDITOR="sed -i 's/^pick /pick /;2,\$s/^pick /squash /'" \
  git rebase -i $UPSTREAM_REMOTE/$BASE_BRANCH
git push $FORK_REMOTE <branch> --force  # 需用户确认
```

squash 前有未提交改动先 `git stash`，完成后 `git stash pop`。

## 创建 MR（GitCode API）

```bash
GITCODE_TOKEN=$GITCODE_TOKEN \
PYTHONPATH=.claude/skills/gitcode-issue-resolver/scripts \
python -c "
from gitcode_client import GitCodeClient
import json
client = GitCodeClient.from_config('.claude/skills/gitcode-config.json')
# head 用完整 'fork_owner/fork_repo:branch'（GitCode 要求带 repo，
# 仅 'owner:branch' 会被误解析为 owner/<主仓名> 导致 Project not found）。
result = client.create_pull_request(
    title='<type>(scope): <描述>',
    head=f'{client.fork_owner}/{client.fork_repo}:<branch_name>',
    base=client.base_branch,
    body='''<MR 描述>''',
)
print(json.dumps(result, indent=2, ensure_ascii=False))
"
```

从 `web_url` 提取 MR 链接。

## 关闭 MR（GitCode API）

GitCode 是 Gitea 风格，关闭用 `state='closed'`（非 GitLab 的 `state_event`），
且 PATCH 至少需带一个其它字段。直接用 client 封装：

```bash
GITCODE_TOKEN=$GITCODE_TOKEN \
PYTHONPATH=.claude/skills/gitcode-issue-resolver/scripts \
python -c "
from gitcode_client import GitCodeClient
client = GitCodeClient.from_config('.claude/skills/gitcode-config.json')
print(client.close_pull_request(<mr_number>).get('state'))
"
```
