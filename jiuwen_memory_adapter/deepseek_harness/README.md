# @jiuwen/dsh-memory

DeepSeek Harness 插件：每轮经本机 jiuwen-memory 服务召回记忆，并在轮次结束时写回本机。

## 事件约定

| 事件 | 作用 | 载荷读取 |
|---|---|---|
| `system-prompt/assemble`（waterfall） | 召回并注入一个名为 `jiuwen-memory` 的上下文 | `prompt`、`session_id`、`turn_id`、`project`、`duty`、`top_k`、`contexts` |
| `turn/end` | 回写本轮 | 同 turn 的 `events`：仅取 `source === "user"` 的 `user/message` 作为 prompt，最后一条助手消息作为 answer |

若实测发现 `system-prompt/assemble` 不按轮触发，改用 `agent/pre-step`；本文件需同步记录实测结论。

## 安装

```bash
dsh plugin --profile desktop add @jiuwen/dsh-memory
dsh plugin --profile dsh-tui add @jiuwen/dsh-memory
```

`peerDependencies` 锁定 `@deepseek-ai/dsh` `0.2.2-rc.1`。安装前先在本机建立 DSH 安装：

```bash
jiuwen-memory connect dsh
```

插件读取 `JIUWEN_MEMORY_HOME`（缺省 `~/.jiuwen-memory`）下的 `run/service.json` 与
`installs/<id>/token`；`JIUWEN_MEMORY_INSTALL` 可显式指定安装，否则要求恰好一个已连接的
dsh 安装。服务未运行时执行一次 `jiuwen-memory service ensure`，失败则本轮不注入、不写回。

## 行为

- 每次请求前用 `/local/status` 校验 `instance_nonce`，失配视为服务不可用。
- recall 超时 2 秒；capture 失败静默；二者都不阻断该轮。
- 注入文本包含服务返回的记忆条目与提示，并附 `<!-- agent-memory: ids -->` 供去重。

## 测试

```bash
cd jiuwen_memory_adapter/deepseek_harness && node --test
```

当前 7 项通过：上下文注入格式、服务不可达原样返回、nonce 失配、只取人发消息、
无用户消息不写、令牌解析、输出结构。

## 实测结论（待补）

`desktop` 与 `dsh-tui` 两个 profile 的两轮真机会话载荷尚未在本机执行；执行后把事件名、
载荷字段与截图结论补入本节。
