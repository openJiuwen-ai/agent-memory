# JiuwenMemory for Claude Code / Codex / OpenCode

> Persistent cross-session memory for AI coding agents via **jiuwen-memory** server.
>
> 一个插件目录同时覆盖 **Claude Code**（12 hooks）、**Codex**（6 hooks）、**OpenCode**（TypeScript 插件 + 2 命令）。

## 它做什么

| 平台 | 接入方式 | hook 数 | 命令 |
|---|---|---|---|
| Claude Code | marketplace 一键安装 | 12 | — |
| Codex | marketplace 一键安装 | 6 | — |
| OpenCode | TypeScript 插件 + opencode.json 配置 | session/message/system 事件 | 2 |

所有平台共享同一个 `scripts/_shared.mjs`（hook 脚本）和 `opencode/` 下的 TS 插件，只是配置入口不同。所有捕获与注入都直接调用 memory_server 的 REST API（`127.0.0.1:8000`），不依赖任何额外服务。

### 数据流

```
Claude Code / Codex / OpenCode agent loop
    │
    ├── hooks / plugin 事件 (生命周期拦截)
    │   ├── session-start.mjs → /health (仅探测服务存活，不搜索记忆)
    │   ├── prompt-submit.mjs → /add_messages/ (后台记录用户 query) + /search → stdout 注入相关记忆
    │   ├── post-tool-use.mjs → no-op (工具结果不记录)
    │   ├── pre-compact.mjs → /search → stdout 注入记忆防丢
    │   └── stop.mjs → no-op (agent 回答不记录)
    │       │
    │       └── memory_server (127.0.0.1:8000, HTTP REST)
    │           └── LongTermMemory engine
    │
    └── OpenCode commands (/recall /remember)
        └── 手动搜索 / 保存记忆
```

## 前置条件

只需启动 jiuwen-memory 的 memory_server：

```bash
python -m jiuwen_memory.server.memory_server     # 默认 127.0.0.1:8000
```

验证：

```bash
curl http://127.0.0.1:8000/health    # {"status":"healthy",...}
```

所有 hook 脚本和 OpenCode 插件都直接调用 memory_server 的 REST API，无其他服务依赖。

## 安装

根据你的平台一键安装插件（无需手动 clone）：

### Claude Code

```bash
# 注册 marketplace
/plugin marketplace add openJiuwen-ai/agent-memory

# 安装插件
/plugin install jiuwen_memory@jiuwen-memory-plugins
```

这会安装完整插件：12 lifecycle hooks。重启 Claude Code 后生效。

> hook 配置由 `hooks/hooks.json` 提供。

### Codex

```bash
# 注册 marketplace
codex plugin marketplace add openJiuwen-ai/agent-memory

# 安装插件
codex plugin add jiuwen_memory@jiuwen-memory-plugins
```

重启 Codex 后生效。

**可选 — 启用 lifecycle hooks。** Codex 不自动从插件 manifest 读取 hooks；需要手动合并到 `~/.codex/hooks.json`（或项目 `.codex/hooks.json`）。你可以手动编辑 hooks.json，将 `hooks/hooks.codex.json` 的内容复制进去（修改路径为绝对路径）。

Codex hooks 还需要 `codex_hooks` feature flag：

```toml
# ~/.codex/config.toml
[features]
codex_hooks = true
```

### OpenCode

**1. 拷贝插件文件和命令到 OpenCode 配置目录**

OpenCode 的插件文件放在 `~/.config/opencode/plugins/`，命令放在 `~/.config/opencode/commands/`。从本仓库的 `opencode/` 目录拷贝过去：

```bash
# 创建目标目录
mkdir -p ~/.config/opencode/plugins ~/.config/opencode/commands

# 拷贝 TS 插件（核心：会话/消息捕获 + 记忆注入）
cp ~/agent-memory/jiuwen_memory/agent-memory-plugin/jiuwen_memory_code_agent/opencode/jiuwen-memory-capture.ts \
   ~/.config/opencode/plugins/

# 拷贝命令（/recall、/remember）
cp ~/agent-memory/jiuwen_memory/agent-memory-plugin/jiuwen_memory_code_agent/opencode/commands/recall.md \
   ~/.config/opencode/commands/
cp ~/agent-memory/jiuwen_memory/agent-memory-plugin/jiuwen_memory_code_agent/opencode/commands/remember.md \
   ~/.config/opencode/commands/
```

拷贝后的目录结构：

```
~/.config/opencode/
├── plugins/
│   └── jiuwen-memory-capture.ts     # TS 插件
└── commands/
    ├── recall.md                    # /recall
    └── remember.md                  # /remember
```

**2. 在配置文件里启用插件**

编辑 `~/.config/opencode/opencode.json`（全局）或项目 `.opencode/opencode.json`，把插件文件加入 `plugin` 字段：

```json
{
   "mcp": {
    "jiuwen_memory": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/mcp",
      "enabled": true
    }
  },
  "plugin": ["./plugins/jiuwen-memory-capture.ts"]
}
```

重启 OpenCode 后生效。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JIUWEN_MEMORY_URL` | `http://localhost:8000` | memory_server REST API 地址 |
| `JIUWEN_USER_ID` | 按平台自动（见下） | 记忆归属的用户 id，显式设置则覆盖平台默认值 |

> **user_id 默认值（未设 `JIUWEN_USER_ID` 时）**：
> - Claude Code → `cc-user`（探测到 `CLAUDE_PLUGIN_ROOT`）
> - Codex → `codex-user`（探测到 `PLUGIN_ROOT`）
> - OpenCode 插件 → `opencode-user`
> - 无法识别平台 → `__default__`
>
> 按平台隔离是为了避免 Claude Code 和 Codex 共用同一份 hook 脚本时记忆互相串掉。每个 user 的记忆在服务端天然隔离。
| `JIUWEN_MEMORY_PROJECT_NAME` | (无) | 覆盖项目名（默认取 git toplevel basename） |
| `JIUWEN_SCOPE_ID` | `opencode-default`（仅 OpenCode 插件） | OpenCode 插件的 scope_id 覆盖 |
| `JIUWEN_MEMORY_API_KEY` | (无) | Bearer token，服务端开启鉴权时必填 |
| `JIUWEN_CODEAGENT_DEBUG` | (off) | 设 `1` 开启 hook 脚本调试日志 |
| `JIUWEN_OPENCODE_DEBUG` | (off) | 设 `1` 开启 OpenCode 插件调试日志 |
| `JIUWEN_SDK_CHILD` | (off) | 设 `1` 标记当前为 SDK 子上下文，跳过捕获防重复 |

> **scope 隔离**：hooks 脚本把 `resolveProject(cwd)`（git toplevel basename）作为 `scope_id`；OpenCode 插件从 `ctx.worktree / ctx.project.id` 推导 basename 作为 `scope_id`，可用 `JIUWEN_SCOPE_ID` 覆盖。每个项目的记忆相互隔离。

## 验证它是否工作

安装后，启动一个新的 agent 会话并检查：

1. **验证服务存活**：`curl http://localhost:8000/health`
2. **验证 hook 自动捕获**（仅 Claude Code / Codex 完整安装版）：发送一条消息，稍后在 memory_server 中确认用户 query 已被写入记忆（仅记录用户输入，不含 agent 回答/工具结果）
3. **试用命令**（仅 OpenCode）：`/recall [topic]` 或 `/remember [content]`

## hooks → 端点映射

> **写入策略**：Claude Code / Codex 只记录**用户输入的 query**（`UserPromptSubmit`）。Agent 的回答、工具调用结果、子 agent 结果、任务总结都**不写入**记忆——相关 hook 已改为 no-op，仅排空 stdin。其余 hook 仍保留 `/health` 探测和搜索注入（不写记忆）。

### Claude Code hooks（12 个）

| hook | memory_server 端点 | 写入记忆? | stdout 注入? |
|---|---|---|---|
| SessionStart | `/health` only | ❌ | ❌ |
| UserPromptSubmit | `/add_messages/` + `/search_memory/` + `/search_user_history_summary/` | ✅（仅用户 query） | ✅ |
| PreToolUse | no-op placeholder | ❌ | ❌ |
| PostToolUse | no-op | ❌ | ❌ |
| PostToolUseFailure | no-op | ❌ | ❌ |
| PreCompact | `/search_memory/` + `/search_user_history_summary/` | ❌ | ✅ |
| SubagentStart | no-op | ❌ | ❌ |
| SubagentStop | no-op | ❌ | ❌ |
| Notification | no-op | ❌ | ❌ |
| TaskCompleted | no-op | ❌ | ❌ |
| Stop | no-op | ❌ | ❌ |
| SessionEnd | no-op | ❌ | ❌ |

### Codex hooks（6 个）

| hook | memory_server 端点 | 写入记忆? | stdout 注入? |
|---|---|---|---|
| SessionStart | `/health` only | ❌ | ❌ |
| UserPromptSubmit | `/add_messages/` + `/search_memory/` + `/search_user_history_summary/` | ✅（仅用户 query） | ✅ |
| PreToolUse | no-op placeholder | ❌ | ❌ |
| PostToolUse | no-op | ❌ | ❌ |
| PreCompact | `/search_memory/` + `/search_user_history_summary/` | ❌ | ✅ |
| Stop | no-op | ❌ | ❌ |

## OpenCode 插件机制

OpenCode 的 TS 插件 (`opencode/jiuwen-memory-capture.ts`) 不走 stdout 注入，而是直接操作 `output.system[]` / `output.context[]`，比 hooks 的 stdout→context 管道更直接。所有调用都直接打 memory_server REST API。

| 钩子 | 作用 | 记忆写入? | 注入? |
|---|---|---|---|
| `session.created` | 探测 `/health`，初始化 per-session 状态 | ❌ | ❌ |
| `session.deleted` | 清理 per-session 缓存 | ❌ | ❌ |
| `message.updated`（assistant） | AI 回复结束后，把暂存的用户 query 写入记忆 | ✅（延后写入） | ❌ |
| `chat.message` | 存用户 query、标记 pending、**阻塞执行一次 search 并缓存** | ❌（写入延后到 message.updated） | ❌ |
| `experimental.chat.system.transform` | 读取缓存 search 结果注入 `output.system[]` | ❌ | ✅ system prompt |
| `experimental.session.compacting` | 压缩前注入 `output.context[]`（命中缓存，否则 fallback 搜索） | ❌ | ✅ context |

> **关键设计**：搜索只在 `chat.message` 阻塞执行一次并缓存到 `sessionSearchResult`，`system.transform` / `compacting` 全程只读缓存、不重复搜索——同一轮多次触发也不会产生额外 HTTP 调用。用户 query 不在 `chat.message` 立即写入，而是延后到 `message.updated`（AI 回复结束）写入，避免打断对话。

## 命令（仅 OpenCode）

| 命令 | 用途 |
|---|---|
| `/recall` | 搜索记忆 |
| `/remember` | 保存记忆 |

## 目录结构

```
jiuwen_memory_code_agent/
├── .claude-plugin/plugin.json       # Claude Code 入口（hooks 指向）
├── .codex-plugin/plugin.json        # Codex 入口
├── hooks/
│   ├── hooks.json                   # Claude Code hooks (12)
│   └── hooks.codex.json             # Codex hooks (6)
├── scripts/
│   ├── _shared.mjs                  # 共享常量/HTTP/项目解析/输出格式
│   ├── session-start.mjs            # SessionStart → /health only
│   ├── prompt-submit.mjs            # 记录 prompt + 搜索注入记忆
│   ├── pre-tool-use.mjs             # no-op placeholder
│   ├── post-tool-use.mjs            # no-op
│   ├── post-tool-failure.mjs        # no-op
│   ├── pre-compact.mjs              # 压缩前注入记忆
│   ├── subagent-start.mjs           # no-op
│   ├── subagent-stop.mjs            # no-op
│   ├── notification.mjs             # no-op
│   ├── task-completed.mjs           # no-op
│   ├── session-end.mjs              # no-op
│   └── stop.mjs                     # no-op
├── opencode/
│   ├── plugin.json
│   ├── jiuwen-memory-capture.ts     # TypeScript 插件
│   └── commands/
│       ├── recall.md
│       └── remember.md
├── plugin.json                      # 顶层元信息
└── README.md
```

## Troubleshooting

如果 hook 没有自动捕获或注入失败：

1. **检查 memory_server 是否运行**：`curl http://localhost:8000/health`
2. **检查环境变量**：`JIUWEN_MEMORY_URL`、`JIUWEN_USER_ID`
3. **检查 `~/.jiuwenmemory/.env`**：配置文件
4. **开调试日志**：`JIUWEN_CODEAGENT_DEBUG=1`（hooks）/ `JIUWEN_OPENCODE_DEBUG=1`（OpenCode 插件）
5. **重启 agent**：修改配置后重启 Claude Code / Codex / OpenCode

## License

Apache-2.0
