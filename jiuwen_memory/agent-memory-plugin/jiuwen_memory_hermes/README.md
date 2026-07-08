# jiuwen-memory for Hermes Agent

> Persistent cross-session memory for [Hermes Agent](https://github.com/NousResearch) via **jiuwen-memory**.
>
> This is the **deep integration** (memory provider plugin). It hooks into the
> Hermes agent loop for pre-LLM context injection, turn-level capture and
> MEMORY.md mirroring — on top of a running `memory_server`.

## 它做什么

把 jiuwen-memory 作为 Hermes 的 memory provider 接入。Hermes 每轮对话时:

- `prefetch()` — 在调用 LLM **之前**注入相关长期记忆(用户画像 / 情景 / 语义 + 历史摘要)
- `sync_turn()` — **后台**把每一轮对话写进记忆(自动抽取,不阻塞主循环)
- `on_pre_compress()` — 上下文压缩前重新注入相关记忆,避免被裁掉
- `on_memory_write()` — 把 Hermes 的 MEMORY.md 写入镜像到 jiuwen
- `system_prompt_block()` — 会话开始时注入记忆能力说明
- `on_session_end()` — 预留(逐轮已在 `sync_turn` 落库,默认 no-op)

工具侧暴露两个检索工具:`ltm_search` / `ltm_search_summary`,供 agent 主动检索。

## 前置条件

需要一个独立运行的 jiuwen `memory_server`(插件只负责发 HTTP,不内嵌引擎):

```bash
python -m jiuwen_memory.server.memory_server     # 默认 127.0.0.1:8000
```

验证:

```bash
curl http://127.0.0.1:8000/health
# {"status":"healthy","message":"Memory Engine API is running"}
```

## 安装

拷贝到 Hermes 插件目录:

```bash
cp -r jiuwen_memory/agent-memory-plugin/jiuwen_memory_hermes ~/.hermes/plugins/jiuwen_memory
```

然后在 `~/.hermes/config.yaml` 指定 provider:

```yaml
memory:
  provider: jiuwen_memory
```

启动 Hermes 即可。插件 import 时会自动读 `~/.jiuwenmemory/.env` 填充缺失的环境变量。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JIUWEN_MEMORY_URL` | `http://127.0.0.1:8000` | memory_server 地址 |
| `JIUWEN_MEMORY_API_KEY` | (无) | 鉴权 bearer;对应 server 端的 `MEMORY_API_KEY`,留空则不鉴权 |
| `JIUWEN_USER_ID` | `hermes-user` | 记忆归属的用户 id |
| `JIUWEN_MEMORY_REQUIRE_HTTPS` | (off) | 设为 `1` 时,若用明文 HTTP + 非 loopback 地址会直接报错 |

> **scope 隔离**:插件把 Hermes 传入的项目 `cwd` 作为 `scope_id`,每个项目
> 的记忆相互隔离,避免跨项目污染。`user_id` 用 `JIUWEN_USER_ID` 控制。

## 设计说明

- **自包含**:插件只用 Python 标准库(`urllib` + `threading`)调 memory_server,
  不导入 jiuwen SDK,也不需要 `httpx`。SDK 里的 async `JiuwenMemoryProvider`
  是给内部 `ExternalMemoryRail` 用的,这里不复用,以免在宿主进程里起 event loop。
- **后台写**:所有写操作(`sync_turn` / `on_memory_write`)走 daemon 线程,
  绝不阻塞 Hermes 的 LLM 轮次。
- **工具结果恒为字符串**:`handle_tool_call` 始终返回 `json.dumps(...)` 的字符串,
  避免 Anthropic 协议下非字符串 content 触发 400。

## 端点映射

| Hermes hook | memory_server 端点 |
|---|---|
| `prefetch` / `handle_tool_call(ltm_search)` | `POST /search_memory/` |
| `prefetch` / `handle_tool_call(ltm_search_summary)` | `POST /search_user_history_summary/` |
| `sync_turn` / `on_memory_write` | `POST /add_messages/`(后台) |
| `initialize` | `GET /health`(探活,不强制) |

## 仅需工具调用(可选的浅集成)

如果你只想让 Hermes 拿到记忆工具、不需要自动捕获每轮对话,可不走本插件,直接
用 jiuwen 自带的 MCP server:

```yaml
mcp_servers:
  jiuwen_memory:
    command: python
    args: ["-m", "jiuwen_memory.server.mcp_server"]
```

该路径只有工具、没有生命周期 hook —— 适合先快速体验。
