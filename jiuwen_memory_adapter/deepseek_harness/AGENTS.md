# DeepSeek Harness 记忆插件（npm）

## 模块地图

| 文件 | 职责 |
|---|---|
| index.js | 发现/探活（instance_nonce）、安装令牌读取、`system-prompt/assemble` 注入、`turn/end` 回写、`jiuwen-memory service ensure` 兜底 |
| test/hook.test.js | `node --test`：上下文注入、服务不可达原样返回、nonce 失配、只取人发消息、令牌解析 |
| README.md | 事件约定、安装命令、实测结论 |

## 行为铁律

- 插件不持有记忆状态：每轮现读发现文件与令牌，nonce 不匹配视为不可用。
- 记忆故障不阻断该轮：探活、recall、capture 失败都返回原载荷/静默。
- `turn/end` 只取 `source === "user"` 的 `user/message` 作为 prompt、最后一条助手消息作为 answer；工具、系统与注入记忆不进入正文。
- recall 超时 2 秒；令牌缺失或无唯一 dsh 安装时不做任何请求。
- `renderContext` 把注入文本包在 `<agent-memory-context>…</agent-memory-context>` 中，条目正文里的标记字面量先中和（S11「召回注入标记」）。

## 与其他子目录的边界

服务契约在本机 `jiuwen_memory_entry/local` 的 `/local/hook/*`；本插件只做 JS 侧发现与转发，不实现授权、去重或写入判定。npm 包名与 `peerDependencies` 版本见 `package.json`，真机事件载荷以宿主实测为准。
