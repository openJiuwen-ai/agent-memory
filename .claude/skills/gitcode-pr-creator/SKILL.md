---
name: gitcode-pr-creator
description: Generate GitCode Merge Request (MR) web links with specified commits, source repository, and target repository. Use when users need to create MR links for GitCode, specify MR source/target branches, or generate MR creation URLs with pre-filled parameters. Note GitCode uses GitLab-style Merge Requests not GitHub-style Pull Requests.
---

# GitCode Merge Request Creator

生成GitCode Merge Request（MR）网页创建链接，支持指定源仓库、目标仓库、分支和预填参数。

**重要提示**：GitCode使用GitLab风格的Merge Request（MR），而不是GitHub风格的Pull Request（PR）。

## 前置条件

`.claude/skills/gitcode-config.json` 配置文件（GitCode 系列 skill 共享）

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

## 获取仓库信息（别名无关）

不要让用户手填 `source_owner/source_repo`，优先从配置文件读取 fork 信息：

```bash
CONFIG=.claude/skills/gitcode-config.json
# ---- 优先：配置文件直接给出 fork owner/repo ----
if [ -f "$CONFIG" ]; then
  FORK_REMOTE=$(jq -r '.fork.remote_name // empty' "$CONFIG")
  FORK_OWNER=$(jq -r '.fork.owner // empty' "$CONFIG")
  FORK_REPO=$(jq -r '.fork.repo // empty' "$CONFIG")
  if [ -n "$FORK_OWNER" ] && [ -n "$FORK_REPO" ]; then
    FORK_PATH="$FORK_OWNER/$FORK_REPO"
  fi
fi

echo "source(fork) = $FORK_PATH"   # 例如 your-fork/deepsearch
```

得到的 `$FORK_PATH`（形如 `owner/repo`）即下文 URL 中的 `:source_owner/:source_repo`。

## 快速使用

### 方式1：访问分支页面（最简单，推荐）

访问源仓库的分支页面，GitCode会自动显示"Create merge request"按钮：

```
https://gitcode.com/:source_owner/:source_repo/-/tree/:branch_name
```

参数说明：
- `:source_owner` - 源仓库所有者（通常是您的用户名）
- `:source_repo` - 源仓库名称
- `:branch_name` - 源分支名称

示例：
```
https://gitcode.com/your-fork/deepsearch/-/tree/feat/issue-1
```

### 方式2：直接创建MR链接

从fork仓库创建MR（GitCode会自动识别upstream）。

**推荐：扁平 `source_branch` 参数**（GitCode 在 `git push` 成功后回显的就是这种格式，实测有效）：

```
https://gitcode.com/:source_owner/:source_repo/merge_requests/new?source_branch=:branch_name
```

示例（真实工程）：
```
https://gitcode.com/your-fork/deepsearch/merge_requests/new?source_branch=fix/issue-158
```

> 备选：GitLab 风格的嵌套数组参数 `merge_request[source_branch]=:branch` 也可用，
> 但 push 回显与实测均以扁平 `source_branch` 为准，优先用扁平格式。

### 方式3：完整参数MR链接

指定所有参数：

```
https://gitcode.com/:source_owner/:source_repo/merge_requests/new?merge_request[source_branch]=:source_branch&merge_request[target_branch]=:target_branch
```

## 预填MR表单参数

可以通过URL查询参数预填MR创建表单：

```
https://gitcode.com/:source_owner/:source_repo/merge_requests/new?merge_request[source_branch]=:branch&merge_request[title]=<标题>&merge_request[description]=<描述>
```

支持的查询参数：
- `merge_request[title]` - MR标题
- `merge_request[description]` - MR描述内容
- `merge_request[milestone_id]` - 里程碑ID
- `merge_request[label_ids][]` - 标签ID（可重复多次）
- `merge_request[target_branch]` - 目标分支

示例：

```
https://gitcode.com/username/repo/merge_requests/new?merge_request[source_branch]=feature&merge_request[title]=Add%20new%20feature&merge_request[description]=This%20MR%20adds%20a%20new%20feature
```

## 指定Commit场景

当用户指定特定commit时：

1. **确认分支包含该commit**：确保源分支包含指定的commit
2. **生成MR链接**：使用包含该commit的源分支创建MR链接
3. **在描述中说明**：在description参数中说明MR包含特定commit

示例：

```
https://gitcode.com/username/repo/merge_requests/new?merge_request[source_branch]=feature&merge_request[title]=Fix%20bug&merge_request[description]=Fixes%20issue%20with%20commit%20abc123
```

## 处理特殊字符

URL参数中的特殊字符需要进行URL编码：
- 空格 → `%20` 或 `+`
- 中文字符需要UTF-8编码
- 特殊符号如 `#`、`&`、`=` 等需要编码

使用Python示例：

```python
from urllib.parse import quote

title = "修复 Bug #123"
encoded_title = quote(title)
url = f"https://gitcode.com/owner/repo/merge_requests/new?merge_request[source_branch]=fix&merge_request[title]={encoded_title}"
```

## 验证要求

生成MR链接前确认：
1. 源分支和目标分支都存在
2. 跨仓库MR时，源仓库是目标仓库的fork
3. 分支名称格式正确
4. 仓库路径（owner/repo）准确

## API详细文档

如需了解GitCode MR API的完整细节，参考 [api_docs.md](references/api_docs.md)。

> **网页链接 vs API 创建**：本 skill 生成的是**网页链接**，由用户在浏览器点击确认创建，
> 适合交互式、需人工核对的场景。若要**非交互式直接创建** MR（脚本/自动化），改用
> `gitcode-git-commit-push` skill 的 `client.create_pull_request()`——注意其 `head` 必须是完整的
> `fork_owner/fork_repo:branch`（仅 `owner:branch` 会被 GitCode 误解析为主仓名导致
> `Project not found`）。

## 使用流程

1. **收集信息**：
   - 源仓库：source_owner/source_repo
   - 源分支：source branch
   - 目标仓库：target_owner/target_repo（如果跨仓库）
   - 目标分支：target branch
   - 可选：commit SHA、标题、描述等

2. **选择创建方式**：
   - **最简单**：生成分支页面链接，让用户点击"Create merge request"
   - **直接创建**：生成merge_requests/new链接
   - **预填参数**：添加title和description等参数

3. **构建URL**：
   - 推荐：`https://gitcode.com/:source_owner/:source_repo/-/tree/:branch`
   - 或：`https://gitcode.com/:source_owner/:source_repo/merge_requests/new?source_branch=:branch`（扁平参数，实测有效）
   - 查询参数：`&merge_request[title]=...&merge_request[description]=...`

4. **返回链接**：
   - 提供完整的可点击URL
   - 说明链接将跳转到哪里或预填哪些字段
   - 提醒用户在网页上确认和完善信息

## 示例场景

### 场景1：简单MR（推荐方式）
用户说："帮我创建一个从feat/issue-1分支的MR链接，我的fork仓库是your-fork/deepsearch"

回答：
```
https://gitcode.com/your-fork/deepsearch/-/tree/feat/issue-1
```

访问这个链接后，点击页面上的"Create merge request"按钮即可。

### 场景2：直接创建MR链接
用户说："帮我创建MR链接，从我fork仓库your-fork/deepsearch的feat/issue-1分支"

回答：
```
https://gitcode.com/your-fork/deepsearch/merge_requests/new?source_branch=feat/issue-1
```

### 场景3：带commit的MR
用户说："我在fork仓库 your-fork/deepsearch 的 fix/issue-158 分支有个commit b71c3d1，想向主仓 openJiuwen/deepsearch 的 dev 分支提MR"

回答（扁平格式，预填标题与描述）：
```
https://gitcode.com/your-fork/deepsearch/merge_requests/new?source_branch=fix/issue-158&merge_request[title]=fix(report):%20%E4%BF%AE%E5%A4%8D%E5%85%AC%E5%BC%8F%E5%AF%BC%E5%87%BA&merge_request[description]=This%20MR%20includes%20commit%20b71c3d1
```

或者更简单的方式：
```
https://gitcode.com/your-fork/deepsearch/-/tree/fix/issue-158
```

### 场景4：完整预填
用户说："创建MR链接，从feature到main，标题是'添加新功能'，描述是'这个MR添加了用户登录功能'"

回答：
```
https://gitcode.com/owner/repo/merge_requests/new?merge_request[source_branch]=feature&merge_request[title]=%E6%B7%BB%E5%8A%A0%E6%96%B0%E5%8A%9F%E8%83%BD&merge_request[description]=%E8%BF%99%E4%B8%AAMR%E6%B7%BB%E5%8A%A0%E4%BA%86%E7%94%A8%E6%88%B7%E7%99%BB%E5%BD%95%E5%8A%9F%E8%83%BD
```

## 常见问题

**Q: 如何确保commit在分支中？**
A: MR基于分支创建，只要指定的源分支包含该commit即可。可以在MR描述中提及特定commit。

**Q: 可以直接从commit创建MR吗？**
A: GitCode MR基于分支，不能直接从commit创建。需要确保commit在某个分支上，然后使用该分支创建MR。

**Q: 跨仓库MR的源仓库必须是fork吗？**
A: 是的，GitCode要求跨仓库MR的源仓库必须是目标仓库的fork。

**Q: GitCode使用Pull Request还是Merge Request？**
A: GitCode使用GitLab风格的**Merge Request（MR）**，而不是GitHub风格的Pull Request（PR）。URL路径是`/merge_requests/new`而不是`/pull/new`。

**Q: 哪种方式最简单？**
A: 推荐访问分支页面（`/-/tree/:branch`），然后点击"Create merge request"按钮，这是最简单且最可靠的方式。
