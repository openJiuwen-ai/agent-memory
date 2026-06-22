---
name: migrate-claude-config
description: >
  将 Claude Code VIBE CODING 工程配置从一个项目迁移到另一个项目。
  适用于：新建团队项目的 Claude 配置、从其他项目复制并适配配置、
  补充缺失语言覆盖、移除源项目多余的语言/业务覆盖、修正项目特定的引用和路径。
learned_from: 2026-06-03
last_applied: 2026-06-21
last_project: openJiuwen/deepsearch (Python-only FastAPI + LangGraph, repo: gitcode.com/openJiuwen/deepsearch, branch: dev)
---

# 迁移 Claude Code 配置到新项目

## 场景

需要在新项目（或已有项目）建立完整的 Claude Code VIBE CODING 团队协作工程配置。
典型场景：从其他项目复制了全套 `.claude/` 配置，需要适配到当前项目。

迁移方向决定工作重点，**先判断源项目和目标项目的技术栈差异**：
- **扩展覆盖**（源 ⊂ 目标）：如 Python-only → Python + C++，需**新增**语言规范、agent 审查项、lint 分流。见 Phase 4A。
- **收缩覆盖**（源 ⊃ 目标）：如 C++/NPU 项目 → 纯 Python 项目，需**移除**源项目特有的 C++/NPU/clang-format/算子测试逻辑，并删除整体无关的 skill。见 Phase 4B。
- 收缩方向极易遗漏，因为残留的 C++/NPU 引用「看起来像合法配置」，不会报错，只会在 AI 实际执行时（如跑不存在的 `run_op_ut.sh`、探测不存在的 `csrc/`）才暴露。本次 deepsearch 迁移即属此类，且源 skill 拷入后长期未做收缩适配。

## 整体文件结构

```
项目根目录/
├── CLAUDE.md              # Claude 入口，引用 AGENTS.md
├── AGENTS.md              # 跨 agent 共享的项目说明书
├── .claude/
│   ├── settings.json      # 权限、环境变量、模型配置
│   ├── agents/            # 子 agent 定义
│   ├── skills/            # 自定义 skill（每个一个子目录）
│   ├── rules/             # 编码规范（分语言）
│   ├── hooks/             # hooks.json 事件钩子
│   ├── commands/          # 自定义 / 命令
│   └── contexts/          # 模式上下文（dev.md 等）
└── .clang-format          # C++ 格式化配置（仅含 C++ 的项目才有；纯 Python 项目无此文件）
```

> 注：以上是「全功能」结构。实际文件随目标项目技术栈裁剪——纯 Python 项目不应有 `.clang-format` 及任何 C++/NPU 相关 rules/agents/hooks 分支。

## 迁移步骤

### Phase 1: 审计源配置（复杂度：中）

1. **列出所有文件**：`find .claude/ -type f | sort`
2. **按类别归类**：
   - 可直接复用：通用 agent（security-reviewer）、git workflow 类 skill
   - 需修改适配：含项目特定引用的文件（仓库名、分支、路径）
   - 需删除：源项目特有的业务逻辑、不相关的工具
   - 需新增：当前项目有但源项目没有的语言/框架覆盖
3. **识别语言覆盖差距（双向）**：
   - 缺口（需新增）：检查 agents/ 是否只覆盖一种语言、rules/ 是否缺对应语言规范、hooks/ lint 是否只针对一种文件类型、skills/ 暂存路径与测试命令是否完整。
   - 冗余（需移除）：检查配置里是否含**目标项目根本不存在的**语言/工具/目录引用——如目标是纯 Python 项目，却残留 `csrc/`、`.clang-format`、`clang-format`、AscendC 算子、`run_op_ut.sh`、NPU/`@pytest.mark.npu` 等。这些来自源项目，需整段删除而非改名。
   - 用 `ls` / 配置文件确认目标项目的真实技术栈（`pyproject.toml` 有没有、有没有 `csrc/` 与 `.clang-format`、主分支名），不要凭源配置假设。

4. **识别脚本内部的结构性假设**（grep 仓库名扫不到的隐藏耦合）：
   - skills/ 下的 `.py` 脚本常硬编码：项目根探测逻辑（如 `if (current / "dynamic_emb").is_dir() and (current / "csrc").is_dir()`）、lint 工具选择、测试目录、命令分流。
   - 这些是代码逻辑而非配置字符串，**必须逐个打开脚本读其 `_find_project_root` / `check_*` / `run_tests` 等方法**，按目标项目结构改写。

### Phase 2: 核心文件创建（复杂度：中）

1. **AGENTS.md**（跨 agent 共享说明书）：
   - 项目是什么、做什么
   - 核心架构（分层、关键数据流）
   - 构建/测试命令
   - 指令优先级
   - 不要抄源项目的具体内容，只仿照格式和思路

2. **CLAUDE.md**：
   - 入口文件，引用 `@AGENTS.md`
   - 记录 Claude 特定的导入或工作流备注
   - 引导查看 `.claude/rules/` 和 `.claude/settings.json`

3. **settings.json**：
   - 添加 ruff、pytest、clang-format、bash 脚本执行权限
   - 配置项目特定的环境变量

4. **`.claude/skills/gitcode-config.json`**（GitCode 系列 skill 的共享配置，多个 skill 依赖）：

   `gitcode-issue-resolver`、`gitcode-committer`、`gitcode-ci-monitor`、`gitcode-git-commit-push`、`gitcode-pr-creator`都通过 `GitCodeClient.from_config()` / `PRClient.from_config()` 读取仓库信息；`gitcode-smart-commit`则用 `jq` 直接读同一份配置（`fork.remote_name`/`base_branch`/`source_dirs`/`exclude_dirs`），且是唯一实现「配置缺失时按 `$generate_*` 自动生成」流程的 skill。共 **6 个** skill 依赖此配置（`gitcode-fix-codecheck` 不读它——仓库信息由 PR 链接/调用方传入，仅需 `GITCODE_TOKEN` + agent-browser）。
   这是**一份合并后的统一配置**（早期分散为工程根 `issue-resolver.json` + committer 目录 `gitcode-committer.json`，已合并为单一来源）。迁移时**必须为目标工程生成这份配置**，否则这些 skill 会回退到交互式提示或 `_discover_from_git_remote()` 的猜测（origin=主仓、其他=fork），在非标准 remote 命名的工程里会推断错误。

   - 放在 **`.claude/skills/`** 下（两套 client 的 `from_config` 都按 `__file__` 上溯到此目录）。
   - **`gitcode-config.example.json` 是「自描述模板」，按它生成 `gitcode-config.json` 是迁移的核心做法**（不是照抄静态占位值）。每个字段都带 `$generate_*` 元字段说明探测规则。`gitcode-smart-commit` 的 SKILL.md「配置文件自动生成」一节给出了标准生成流程，迁移时照此执行：
     1. **Read** `gitcode-config.example.json`，理解各字段的 `$generate_*` 说明。
     2. **Bash** `git remote -v` + `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}`，提取 upstream/fork 的 owner/repo/remote_name 和 base_branch。
     3. **Glob/Read** 工程根（`pyproject.toml`/`go.mod`/`pom.xml` 等，按存在的文件判断工程类型）推断 `source_dirs`。
     4. **Read** `.gitignore`（若存在），提取 `exclude_dirs`（以 `/` 结尾且无 glob 通配符的目录）。
     5. **Write** `.claude/skills/gitcode-config.json`。
   - **remote 探测：不确定就问用户，不要瞎猜**（这是 example `$generate_remote_name` 的明确要求）：
     - **upstream.remote_name**：有名为 `upstream` 的 remote 选它，否则用 `origin`；两者都没有时**列出所有 remote 询问用户哪个是主仓**。
     - **fork.remote_name**：取 `git remote | grep -vx <upstream_remote> | head -1`；若有多个非主仓 remote 或没有其他 remote，**列出所有 remote 询问用户哪个是 fork**（可选「无 fork」）。

     ```bash
     # upstream remote：upstream 优先，否则 origin（都没有则需问用户）
     if git remote | grep -qx upstream; then UP_REMOTE=upstream
     elif git remote | grep -qx origin; then UP_REMOTE=origin
     else echo "无 upstream/origin，列出 remote 询问用户"; git remote -v; fi
     # fork remote：除主仓外的那个（多个或零个则需问用户）
     FORK_REMOTE=$(git remote | grep -vx "$UP_REMOTE" | head -1)
     up=$(git remote get-url "$UP_REMOTE"   | sed -E 's#\.git$##' | sed -E 's#.*[:/]([^/]+/[^/]+)$#\1#')
     fk=$(git remote get-url "$FORK_REMOTE" | sed -E 's#\.git$##' | sed -E 's#.*[:/]([^/]+/[^/]+)$#\1#')
     base=$(git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>/dev/null | sed "s#^$UP_REMOTE/##")
     echo "upstream=$up($UP_REMOTE) fork=$fk($FORK_REMOTE) base=${base:-main}"
     ```

   - 用探测结果填模板（字段含义见下表），写到 **`.claude/skills/gitcode-config.json`**：

     ```json
     {
       "upstream": {"owner": "<up_owner>", "repo": "<up_repo>", "remote_name": "<UP_REMOTE>", "base_branch": "<base>"},
       "fork": {"owner": "<fork_owner>", "repo": "<fork_repo>", "remote_name": "<FORK_REMOTE>"},
       "branch_prefix": {"bug": "fix/issue-", "feature": "feat/issue-", "refactor": "refactor/issue-"},
       "source_dirs": ["<目标工程主包目录/>", "..."],
       "exclude_dirs": [".claude/", ".cursor/", ".vscode/", ".idea/"],
       "poller": {"interval_seconds": 60, "trigger_on_assign": true,
                  "trigger_on_mention": true, "trigger_on_labels": ["auto-resolve"],
                  "mention_keywords": ["@bot-resolve"], "auto_trigger": false}
     }
     ```

   | 字段 | 含义 | 适配要点 |
   |------|------|---------|
   | `upstream.owner/repo` | 主仓 owner/repo | 从 `$UP_REMOTE` URL 提取，勿照抄源工程 |
   | `upstream.remote_name` | 主仓本地 remote 别名 | upstream 优先，否则 origin；都没有时问用户 |
   | `upstream.base_branch` | 主仓主分支 | `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}` 探测当前分支跟踪的上游分支；失败时列出分支询问用户 |
   | `fork.owner/repo` | 个人 fork owner/repo | 从 fork remote 提取；fork repo 名可能与主仓不同；非 fork 流可留空 |
   | `fork.remote_name` | fork 的本地 remote 别名 | **最易错**：`grep -vx <upstream_remote>` 取剩下那个；多个/零个时问用户|
   | `source_dirs` | 选择性暂存的代码目录 | 按工程类型自适应扫描（Python 含 `__init__.py` 顶层目录/pyproject packages，Go/Java/C++ 各有规则）+ 追加 tests/server 等 |
   | `exclude_dirs` | 排除目录 | 固定模式 `.claude/ .cursor/ .vscode/ .idea/` + `.gitignore` 提取的目录条目 |

### Phase 3: 批量修正项目引用（复杂度：高）

需要系统扫描以下内容，全部替换为当前项目的值（下表示例方向：源 `Ascend/RecSDK_for_lingqu` C++/NPU → 目标 `openJiuwen/deepsearch` 纯 Python）：

| 类别 | 检查内容 | 示例（源→目标） |
|------|---------|------------------|
| 仓库名 | owner/repo | `Ascend/RecSDK_for_lingqu` → `openJiuwen/deepsearch` |
| 主分支 | base_branch | `develop` → `dev`（务必确认项目实际主分支，勿假设 master/main/develop） |
| 源码目录 | source_dirs | `dynamic_emb/`, `csrc/` → `openjiuwen_deepsearch/`, `server/`, `tests/` |
| 排除目录 | exclude_dirs | 补充目标项目运行时产物：`output/`, `logs/`, `workspaces/` |
| 测试路径 | test commands | `cd tests/ut/dynamic_emb_op && bash run_op_ut.sh` → `uv run pytest -m "not llm"` |
| 模块名 | Python imports | `from recsdk.xxx` → `from openjiuwen_deepsearch.xxx` |
| API URL | GitCode/CI URL | 源项目 URL → 当前项目 URL |

**修正策略——分轮扫描，勿依赖单一关键词**：一次只搜「仓库名」会漏掉只携带「分支名」或「目录名」残留的文件（本次 `develop` 残留就分布在 `git-commit-push/SKILL.md`、`pr_api_reference.md`、`api_docs.md` 这些不含仓库名的文件里，首轮按 RecSDK 关键词扫描完全没命中）。至少分四轮独立扫描：
```bash
# 第 1 轮：仓库标识
grep -rinE "RecSDK|lingqu|<源 owner>|<源 repo>" .claude/skills/
# 第 2 轮：分支名（用 \b 边界避免误伤 develop_xxx）
grep -rinE "\b(master|develop)\b" .claude/skills/
# 第 3 轮：结构性目录/工具（源项目特有）
grep -rinE "dynamic_emb|csrc|clang-format|run_op_ut|run_python_ut|AscendC" .claude/skills/
# 第 4 轮：模块 import
grep -rinE "from recsdk|import recsdk" .claude/skills/
```
扫描时排除 `skills/learned/`（迁移历史笔记，本就记录源项目名，属正常）。建议并行派发多个 agent 同时处理不同 skill 目录。

### Phase 4A: 补充缺失的语言覆盖（扩展方向，复杂度：高）

从 Python-only 扩展到 Python + C++ 项目时：

1. **rules/ 新增 cpp-standards.md**：
   - 格式（.clang-format 为准）
   - 命名规范
   - pybind11 边界规则
   - AscendC 算子开发规范
   - 内存管理
   - include 顺序

2. **agents/ 补充 C++ 审查规则**：
   - code-reviewer：增加内存安全、pybind11 边界、AscendC 规范
   - tdd-guide：增加 C++ 测试命令（gtest/bash run_op_ut.sh）
   - refactor-cleaner：增加 C++ 死代码检测命令
   - planner：增加跨层判断（Python 层 vs C++ 层 vs pybind11 边界）

3. **hooks/ 补充 C++ lint**：
   - PostToolUse 按文件扩展名分流：`.py` → ruff，`.cpp/.h` → clang-format
   - `xargs` 必须带 `-r` 参数（无匹配文件时不挂起）

4. **skills/ 补充 C++ 路径和命令**：
   - gitcode-smart-commit：暂存路径增加 `csrc/`
   - verification-loop：增加 C++ 测试和 clang-format 检查

### Phase 4B: 移除源项目多余的语言/业务覆盖（收缩方向，复杂度：高）

从 C++/NPU 项目迁到纯 Python 项目时，源配置里所有 C++/NPU 痕迹都要清除，**不是改名而是删除**：

1. **删除整体无关的 skill 目录**：源项目特有、目标项目用不上的 skill 整个删掉（如本次 `dynamic-emb-guide` 是 RecSDK 的 AscendC 框架理解指南，与 deepsearch 无关）。删前确认无其他文件 `grep -rin "<skill-name>"` 引用它。
2. **改写脚本逻辑**（见 Phase 1 step 4）：`pre_commit_check.py` 这类脚本里的 C++ 检查方法（`check_cpp`、clang-format 调用）、NPU 测试路径（`tests/ut/dynamic_emb_op`、`run_op_ut.sh`）整段删除，项目根探测改用目标项目标志（`pyproject.toml` + 主包目录），lint 改为纯 Python（ruff + `pytest -m "not llm"`）。
3. **rules/ 删除 cpp-standards.md**，并从 AGENTS.md / code-standards.md 移除 C++ 专属段落（除非目标项目确有 C++）。
4. **agents/ 收窄审查项**：去掉裸指针/内存安全/pybind11/AscendC 等仅 C++ 相关的审查维度。
5. **hooks/ 简化 lint 分流**：去掉 `.cpp/.h → clang-format` 分支。
6. **删除残留配置文件**：源项目带来的 `.clang-format`、`cppcheck` 配置等。
7. **SKILL.md / 文档** 中的 C++ 命令示例、目录引用、敏感词示例同步改为 Python 语境。

### Phase 5: 项目特定 Skill 创建（复杂度：低）

为项目框架理解创建一个 guide skill，包含：
- 目录结构和模块职责
- 层边界和数据流
- 关键类的查找表
- 调试入口点

### Phase 6: 验证与修复（复杂度：中）

1. **检查 hooks 正确性**：
   - Counter 不能用 `$`（PID），每次调用PID不同，用固定文件名
   - Stop hook 路径检查用项目相对路径 + `-f` 判断
   - git push warning 匹配所有 remote（不仅是 `origin`）

2. **批量检查常见陷阱**：
   ```bash
   # 检查是否还有源项目引用
   grep -r "openjiuwen\|jiuwen\|SnapeK" .claude/
   # 检查分支名是否正确
   grep -r "master" .claude/ --include="*.md" --include="*.json" --include="*.py"
   # 检查裸 xargs（缺 -r）
   grep -r "xargs clang-format" .claude/ | grep -v "\-r"
   # 检查 JSON 配置中是否写错 owner/repo
   grep -r '"owner"\|"repo"' .claude/ --include="*.json"
   # 检查是否还有 ~/.claude/ 硬编码路径（应改为项目级 .claude/）
   grep -r "~/.claude/" .claude/ || echo "OK"
   # 检查 gitcode-config.json 的 fork.remote_name 是否与实际 remote 一致
   git remote -v | grep -q "$(python -c 'import json; print(json.load(open(".claude/skills/gitcode-config.json"))["fork"]["remote_name"])')" && echo "remote_name OK" || echo "MISMATCH: gitcode-config.json fork.remote_name 与 git remote 不一致"
   ```

3. **脚本/配置可加载性验证**（grep 之外，确认改完不破坏运行）：
   ```bash
   # JSON 全部合法
   for f in $(find .claude/skills -name "*.json"); do python -c "import json;json.load(open('$f',encoding='utf-8'))" && echo "OK $f"; done
   # 共享配置 .claude/skills/gitcode-config.json 能加载（多个 skill 依赖）
   [ -f .claude/skills/gitcode-config.json ] && python -c "import json;json.load(open('.claude/skills/gitcode-config.json'))" && echo "OK gitcode-config.json" || echo "MISSING: 需要 .claude/skills/gitcode-config.json"
   # Python 脚本语法通过
   python -m py_compile $(find .claude/skills -name "*.py" -not -path "*/__pycache__/*")
   # GitCodeClient 能从共享配置加载（冒烟，不实际请求）
   PYTHONPATH=.claude/skills/gitcode-issue-resolver/scripts python -c "from gitcode_client import GitCodeClient; c=GitCodeClient.from_config('.claude/skills/gitcode-config.json'); print(f'upstream={c.upstream_owner}/{c.upstream_repo} fork={c.fork_owner}/{c.fork_repo}'); assert c.fork_owner, 'fork owner 为空'"
   # PRClient 默认（不传 config）也解析到共享配置的同一仓库
   PYTHONPATH=.claude/skills/gitcode-committer/scripts GITCODE_TOKEN=dummy python -c "from pr_client import PRClient; c=PRClient.from_config(); print(c.upstream_owner+'/'+c.upstream_repo)"
   # 清理源项目带来的过期字节码缓存
   find .claude/skills -name "__pycache__" -type d  # 确认后删除
   ```

4. **文件级审计清单**（逐文件排查 9 类问题）：

   | # | 检查项 | 排查方法 |
   |---|--------|---------|
   | 1 | command 文件重复注册 | 检查 `commands/*.md` 是否含 `name:` + `triggers:` frontmatter，有则删除（command 只需要 `description:`） |
   | 2 | JSON 配置字段值错误 | 逐个打开 `.claude/skills/*/xxx.json`，确认 owner/repo/remote 与项目一致 |
   | 3 | contexts 语言覆盖（双向） | 目标多语言则 `contexts/review.md` 需覆盖各语言；目标纯 Python 则删除 C++ 专属审查项（裸指针/越界/CANN API/pybind11） |
   | 4 | skill 之间职责重叠 | 两个 skill 是否描述同一场景（如 gitcode-smart-commit vs gitcode-git-commit-push），有重叠则精简次要 skill |
   | 5 | commands 步骤完整且匹配技术栈 | `/verify` 是否覆盖目标项目所有语言的 lint/test，且**不含**目标没有的语言命令（纯 Python 项目不应残留 clang-format/run_op_ut.sh） |
   | 6 | 冗余脚本文件 | `scripts/` 下是否有功能已被 hooks.json 覆盖的独立脚本，有则删除 |
   | 7 | 路径前缀不一致 | 技能文件中是否残留 `~/.claude/` 路径（应统一为 `.claude/` 项目级路径） |
   | 8 | 最大文件排查 | `find .claude/ -type f -exec wc -c {} + | sort -rn | head -10`，超过 20KB 的文件检查是否为系统自带（如 agent-browser），项目自有的大文件需要精简 |
   | 9 | 脚本内部结构性假设 | 逐个 `.py` 脚本读 `_find_project_root`/`check_*`/`run_tests`，确认目录探测、lint 工具、测试命令匹配目标项目（见 Phase 1 step 4 / Phase 4B） |

5. **注意 git 跟踪状态**：`.claude/` 下的文件可能**未被 git 跟踪**（`git ls-files .claude/skills/<dir>/` 为空即未跟踪）。此时 `git diff` 看不到改动、`git rm` 删不掉，需用文件系统命令删除（Windows 上 `rm -rf` 可能被沙箱拒绝，改用 PowerShell `Remove-Item -Recurse -Force`）；迁移完成后若要纳入版本管理需手动 `git add`。

6. **端到端测试**：
   - 运行 `/review` 验证 agent 加载正常
   - 运行 `/verify` 验证测试路径正确
   - 运行 `/gitcode-smart-commit` 预检（lint + 暂存）
   - 检查 `/skills` 列表无重复条目

## 常见陷阱

1. **xargs 不带 -r**：`find ... | xargs clang-format` → 无 `.cpp` 文件时 xargs 会挂起等待输入。始终用 `xargs -r`。

2. **分支名假设**：不要假设所有项目用 `master`/`main`/`develop`，先确认。用 `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}` 探测当前分支跟踪的上游分支，或查看 `.claude/skills/gitcode-config.json` 确认主分支，再全量替换配置里的 `base_branch` / rebase 目标 / MR base。

3. **hooks.json Counter**：不能用 `$`（进程 PID）做文件名，每次调用PID不同导致计数永远为1。用固定文件名如 `claude-tool-count-session`。

4. **路径分隔符**：Windows 环境下的 `~/.claude/` 和项目相对路径不同，stop hook 用项目相对路径并加 `-f` 存在性检查。

5. **命名不准确**：Python-only 时命名的 agent（如 `python-reviewer`）在支持多语言后应改名（→ `code-reviewer`），同时更新所有引用它的地方。

6. **Superpowers 集成**：检查 agent 是否能利用 superpowers 插件能力（brainstorm、write-plan、test-driven-development、using-git-worktrees），能用的都加上引用。

7. **command 文件含 skill frontmatter 导致重复注册**：`commands/` 下的 `.md` 文件只需要 `description:`，如果误加了 `name:` 和 `triggers:`，会被同时注册为 skill，在 `/skills` 列表中重复出现。排查：`grep -l "^name:\|^triggers:" .claude/commands/*.md`，有结果则删除这两行。

8. **JSON 配置文件的 owner 字段**：迁移后 `skills/*/xxx.json` 中的 `owner`、`repo` 等字段容易遗漏修改（如 `"owner": "RecSDK_for_lingqu"` 实际应为 `"Ascend"`）。必须逐文件确认，不能依赖 grep 替换。

9. **contexts 语言覆盖不完整**：`contexts/review.md` 只列了 Python 审查项，如果项目是 Python + C++ 双语言，必须补充 C++ 特定项：裸指针/越界检查、CANN API 返回值检查、pybind11 边界规则。

10. **skill 间职责重叠**：多个 skill 描述相同场景（如 `gitcode-smart-commit` 覆盖日常 commit，`gitcode-git-commit-push` 应只保留 squash/MR 特殊操作，删除重复的日常流程描述），不精简会造成 AI 选择困惑。

11. **commands 步骤缺失**：`/verify` 命令可能只列了 Python 的 ruff + pytest，缺少 C++ 的 clang-format 和 C++ 测试命令。多语言项目必须每种语言都检查。

12. **冗余的独立脚本**：`scripts/` 下如果存在功能已被 `hooks.json` 覆盖的脚本（如 compact 触发脚本），应删除，避免维护两份逻辑。

13. **收缩方向被忽视**：从 C++/NPU 项目迁到纯 Python 项目时，最容易只改仓库名却保留 `csrc/`、clang-format、AscendC 算子测试等。这些残留不报错，只在 AI 实际执行时才暴露（探测不存在的目录、跑不存在的脚本）。迁移前必须先判断方向（见开头「迁移方向」），收缩方向走 Phase 4B。

14. **脚本内部硬编码假设**：`pre_commit_check.py` 之类脚本把项目结构写死在代码里（`_find_project_root` 用 `dynamic_emb`+`csrc` 判断、`check_cpp` 调 clang-format、`run_tests` 跑 NPU 路径）。grep 仓库名扫不到这些，必须逐脚本读方法体改写。本次该脚本的 root 探测、C++ 检查、NPU 测试三块都需重写为 Python 版。

15. **单一关键词扫描漏网**：只按源仓库名 grep 会漏掉只携带分支名/目录名残留的文件。本次 `develop` 残留藏在 `git-commit-push/SKILL.md`、`pr_api_reference.md`、`api_docs.md` 里（均不含 RecSDK 字样），首轮没发现。必须按「仓库名/分支名/结构目录/模块名」分多轮独立扫描（见 Phase 3）。

16. **整体无关的 skill 要删除而非适配**：源项目特有、目标项目无对应概念的 skill（如 `dynamic-emb-guide` 是 AscendC 框架指南）应整目录删除。删前 `grep -rin "<skill-name>"` 确认无引用。Windows 上 `rm -rf` 可能被沙箱拒绝，用 PowerShell `Remove-Item -Recurse -Force`。

17. **`.claude/` 文件常未被 git 跟踪**：迁移来的配置可能未纳入版本管理，`git diff`/`git rm` 对其无效；删除靠文件系统命令，验证靠 grep + py_compile + JSON 加载而非 diff；完成后若要纳管需手动 `git add`。

18. **Windows GBK 控制台与 emoji/路径**：从其他环境迁来的脚本若在成功路径 `print("✅...")`，在 Windows GBK 控制台会抛 `UnicodeEncodeError`（API 调用其实已成功，只是打印崩溃，易误判失败）。验证脚本输出改用 ASCII（`[OK]`）。另外 Git Bash 会把以 `/` 开头的参数（如 `/lgtm`）做 MSYS 路径转换成 `C:/Program Files/Git/lgtm`，传斜杠开头参数时加 `MSYS_NO_PATHCONV=1`。

19. **gitcode-config.json 漏建或照抄源工程**：这份共享配置被 **6 个** skill 依赖（`gitcode-issue-resolver`/`gitcode-committer`/`gitcode-ci-monitor`/`gitcode-git-commit-push`/`gitcode-pr-creator` 通过 client 的 `from_config()` 读取；`gitcode-smart-commit` 用 `jq` 直接读取，且实现了配置缺失时的自动生成流程；`gitcode-fix-codecheck` 不读此配置），由早期分散的 `issue-resolver.json` + `gitcode-committer.json` 合并而来，位于 `.claude/skills/gitcode-config.json`。所有工程强绑定值（owner/repo/remote_name/base_branch/source_dirs/exclude_dirs）都已统一收敛到此文件，脚本不再硬编码。典型错误：(a) **漏建**——skill 运行时回退到 `_discover_from_git_remote()` 猜测或交互提示，非标准 remote 命名工程会推断错；(b) **照抄源工程或 example 占位值**——`gitcode-config.example.json` 的静态值（`fork.owner` 留空、`upstream/fork.remote_name` 写占位 `origin`/`local_fork`）只是占位；正确做法是**读各字段的 `$generate_*` 说明、按规则探测目标工程后生成**（详见 Phase 2 step 4 和 `gitcode-smart-commit` SKILL.md 的自动生成流程）。(c) **遇歧义不敢问用户**——`$generate_remote_name` 已明确「找不到 upstream/origin 时**列出所有 remote 询问用户哪个是主仓**」、「fork remote 多个/零个时**询问用户哪个是 fork（可选无 fork）**」；不要瞎猜 origin/upstream/fork 这些别名，别名是本地配置不是标准，目标工程可能叫 ryan/my-fork/personal。最易错字段：`upstream.remote_name`（决定 fetch/rebase 来源）、`fork.remote_name`（决定推送目标）、`upstream.base_branch`（用 `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}` 探测当前分支跟踪的上游分支，失败时列出分支询问用户，）。

## 工具清单

- **并行 agent**：多个独立的 skill 目录修正可以并行派发 agent 处理
- **分轮 grep 扫描**：按仓库名/分支名/结构目录/模块名分多轮搜索，单轮单关键词会漏（见陷阱 15）
- **可加载性验证**：JSON `json.load`、Python `py_compile`、关键 client `from_config` 解析冒烟（见 Phase 6 step 3）
- **diff review**：修改后 `git diff --stat` 确认改动范围（注意未跟踪文件不在 diff 内，见陷阱 17）
