---
name: gitcode-smart-commit
description: 智能 git commit 流程。自动运行 lint、test 预检，仅暂存代码目录，按 conventional commits 格式生成 commit message，排除工具配置变更。同时管理 Issue 专属分支和 fork 推送。
---

# Smart Commit

智能 git commit 流程，自动执行预检、分支管理、选择性暂存、推送和 MR 创建。

## Remote 解析（别名无关，跨工程迁移用）

命令里不要硬编码 `upstream`/`origin` 字面量。后续命令统一用`$UPSTREAM_REMOTE`（主仓，只读同步）和 `$FORK_REMOTE`（个人 fork，推送）。

**配置文件驱动**：所有 remote/分支信息从 `.claude/skills/gitcode-config.json` 读取，配置缺失时 AI 自动生成。

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

### 解析逻辑参考（实际应用配置，非直接执行）

```bash
CONFIG=.claude/skills/gitcode-config.json

# ---- 从配置文件读取 ----
FORK_REMOTE=$(jq -r '.fork.remote_name // empty' "$CONFIG")
UPSTREAM_REMOTE=$(jq -r '.upstream.remote_name // "origin"' "$CONFIG")
BASE_BRANCH=$(jq -r '.upstream.base_branch // empty' "$CONFIG")
BASE_BRANCH=${BASE_BRANCH:-main}   # 配置字段为空时回退到 main

SOURCE_DIRS=$(jq -r '.source_dirs[]' "$CONFIG")   # 多行输出，每行一个目录
EXCLUDE_DIRS=$(jq -r '.exclude_dirs[]' "$CONFIG")

echo "upstream=$UPSTREAM_REMOTE fork=$FORK_REMOTE base=$BASE_BRANCH"
echo "source_dirs: $SOURCE_DIRS"
# 推送前务必确认 fork 不为空
```

**`FORK_REMOTE` 为空**：说明只有一个 remote，需人工确认 fork 配置后再推送。

## 核心行为

1. **分支检查**（强制，不在主干分支则跳过）：
   - 当前在主干分支（`$BASE_BRANCH`）→ 根据改动类型创建 Issue 专属分支
   - 分支命名：`fix/issue-N`（bug）、`feat/issue-N`（feature）、`refactor/issue-N`（refactor）
   - 从主仓同步最新主干：`git fetch $UPSTREAM_REMOTE $BASE_BRANCH && git rebase $UPSTREAM_REMOTE/$BASE_BRANCH`

2. **预检**（强制，失败则阻止 commit）：
   - 统一入口：`python .claude/skills/gitcode-smart-commit/pre_commit_check.py`
   - Python lint：`ruff`（未安装则自动跳过，agent-memory 暂未引入 ruff）
   - 运行 smoke test：`python -m pytest evaluation/smoke_test`

3. **选择性暂存**：仅暂存配置文件 `source_dirs` 指定的目录变更
   - 示例：`src/`、`evaluation/`、`bootstrap/`
   - 自动排除配置文件 `exclude_dirs` 指定的目录和 .gitignore 覆盖的文件

4. **Commit 格式**：conventional commits
   ```
   <type>(<scope>): <subject>

   <optional numbered body>
   ```

5. **推送与 MR**：
   - 推到 fork remote（`$FORK_REMOTE`），不推主仓（`$UPSTREAM_REMOTE`）
   - 关联 Issue 号：commit message 含 `Resolves #N`，MR 描述关联 issue

6. **自动排除**：
   - 配置文件 `exclude_dirs` 指定的目录（IDE 配置、缓存、生成产物）
   - 示例：`.claude/`、`.cursor/`、`build/`、`dist/`、`logs/`、`.pytest_cache/`、`evaluation/benchmark/data/`
   - build 产物和缓存目录
   - 文档（除非本次改动即文档）

## 完整工作流

### 有 Issue 关联时

```bash
# ---- Phase 1: 分支管理 ----
# 当前在主干分支，需要切到 Issue 专属分支
git stash                          # 保存未提交变更
git checkout $BASE_BRANCH
git pull $UPSTREAM_REMOTE $BASE_BRANCH   # 从主仓同步最新
git checkout -b fix/issue-2        # 创建 Issue 专属分支
git stash pop                      # 恢复工作区

# ---- Phase 2: 预检 ----
python .claude/skills/gitcode-smart-commit/pre_commit_check.py 2>&1 | tail -20

# ---- Phase 3: smoke test（预检脚本已含，单独跑可用）----
# python -m pytest evaluation/smoke_test -q

# ---- Phase 4: 暂存与提交 ----
# 暂存配置指定的 source_dirs
for dir in $SOURCE_DIRS; do
  [ -d "$dir" ] && git add "$dir"
done
git commit -m "test(eval): add baseline adapter recall regression test

1. Add Recall@k assertion for in-memory adapter
2. Cover empty-result edge case

Resolves #2"

# ---- Phase 5: 推送与 MR ----
git push $FORK_REMOTE fix/issue-2  # 推到 fork，不推主仓
# 通过 API 创建 MR: fork/issue-branch → upstream/$BASE_BRANCH
```

### 无 Issue 关联时（日常小改动）

```bash
# 1. 查看状态
git status

# 2. 预检（lint 可选 + smoke test）
python .claude/skills/gitcode-smart-commit/pre_commit_check.py 2>&1 | tail -20

# 3. 暂存（source_dirs 示例，实际从配置变量循环）
for dir in src/ evaluation/ bootstrap/; do
  [ -d "$dir" ] && git add "$dir"
done

# 4. 提交
git commit -m "fix(eval): adjust default IR top_k

1. Change default recall top_k to 5
2. Update related smoke test expectations"

# 5. 验证
git log -1 --stat
# 如需推送：git push $FORK_REMOTE <当前分支>（不推主仓主干 $BASE_BRANCH）
```

## 分支命名规则

| 改动类型 | 分支前缀 | 示例 |
|---------|---------|------|
| Bug 修复 | `fix/issue-` | `fix/issue-2` |
| 新功能 | `feat/issue-` | `feat/issue-15` |
| 重构 | `refactor/issue-` | `refactor/issue-8` |
| 测试 | `test/issue-` | `test/issue-3` |
| 性能优化 | `perf/issue-` | `perf/issue-7` |

## 禁止行为

- 禁止在主干分支（`$BASE_BRANCH`）上直接 commit（必须切到 Issue 专属分支）
- 禁止推送到主仓（`$UPSTREAM_REMOTE`）的任何分支
- 禁止推送到 fork（`$FORK_REMOTE`）的主干分支（`$BASE_BRANCH`）
- 只能推到 fork（`$FORK_REMOTE`）的 Issue 专属分支

## Commit Message 格式

基于项目实际历史：
- `feat(evaluation): add memory evaluation scaffold`
- `feat: 支持分层记忆结构`
- `refactor: 统一 scope 隔离逻辑`
- `test(eval): add baseline adapter recall regression test`

**Type**: `fix` / `feat` / `refactor` / `test` / `perf` / `docs` / `chore`

**Scope**（可选）: 模块名，如 `api`、`control`、`retrieval`、`construction`、`storage`、`evaluation`、`config`

## 边缘情况

- **无代码目录变更**：报告用户，不提交
- **混合变更**：仅暂存代码目录，警告排除的文件
- **预检失败**：展示错误详情，阻止 commit，等待修复
- **在主干分支上**：先走分支管理流程，创建 Issue 专属分支后再提交
