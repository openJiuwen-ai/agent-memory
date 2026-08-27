---
name: agent-memory1.0_skill
description: 通过 HTTP API 调用 agent-memory1.0 记忆服务，召回与写入历史处理记忆
scripts:
  search_ok: "agent-memory1.0 历史记忆召回完成"
  save_ok: "agent-memory1.0 记忆写入完成"
  update_ok: "agent-memory1.0 记忆更新完成"
  delete_ok: "agent-memory1.0 记忆删除完成"
  flush_ok: "agent-memory1.0 缓冲区刷新完成"
---

# agent-memory1.0_skill

## 触发条件

需要召回或写入用户历史处理记忆时调用。本 Skill 直接调用 agent-memory1.0 容器的 HTTP API（端口 8517），
不依赖 MemoryRail 框架。内置熔断器、批量写入缓冲、fail-open 降级等完整功能。

## call_mcp 参数

### 召回记忆

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"search\", \"user_id\": \"{user_id}\", \"query\": \"用户 {user_id} 的历史处理记录\", \"top_k\": 5}"
}
```

### 写入记忆

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"save\", \"user_id\": \"{user_id}\", \"content\": \"处理结果...\", \"role\": \"assistant\"}"
}
```

### 缓冲写入（批量积累后统一刷新）

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"save\", \"user_id\": \"{user_id}\", \"content\": \"中间结果...\", \"buffer\": true}"
}
```

### 刷新缓冲区

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"flush\", \"user_id\": \"{user_id}\"}"
}
```

### 查看状态

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"status\"}"
}
```

### 更新记忆

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"update\", \"user_id\": \"{user_id}\", \"mem_id\": \"{mem_id}\", \"memory\": \"修正后的内容...\"}"
}
```

### 删除单条记忆

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"delete\", \"user_id\": \"{user_id}\", \"mem_id\": \"{mem_id}\"}"
}
```

### 批量删除记忆

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"batch_delete\", \"user_id\": \"{user_id}\", \"mem_ids\": [\"mem_abc\", \"mem_def\"]}"
}
```

### 更新画像变量

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"update_variables\", \"user_id\": \"{user_id}\", \"variables\": {\"status\": \"normal\", \"priority\": \"high\"}}"
}
```

### 删除画像变量

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"delete_variables\", \"user_id\": \"{user_id}\", \"names\": [\"old_var_name\"]}"
}
```

### 消息溯源

```json
{
  "script_command": "python agent-memory1.0_skill/scripts/run_memory_operation.py",
  "script_params": "{\"operation\": \"trace\", \"message_id\": \"{message_id}\"}"
}
```

## 参数说明

### 通用参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `operation` | string | 是 | — | `search` / `get` / `save` / `update` / `delete` / `batch_delete` / `update_variables` / `delete_variables` / `trace` / `flush` / `status` |
| `user_id` | string | 是* | — | 用户标识，*status 操作不需要 |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识（与 MemoryRail 保持一致） |

### search 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 否 | `"业务关键词"` | 搜索查询文本 |
| `top_k` | int | 否 | `5` | 返回结果数量上限 |
| `threshold` | float | 否 | `0.0` | 向量搜索相似度阈值（0.0 = 不过滤） |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |

### get 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `top_k` | int | 否 | `20` | 返回结果数量上限 |

### save 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `content` | string | 否 | `""` | 要写入的文本内容（简化模式） |
| `role` | string | 否 | `"assistant"` | 消息角色（简化模式） |
| `messages` | array | 否 | — | 完整消息列表 `[{"role":"...","content":"..."}]`（与 content 二选一） |
| `session_id` | string | 否 | `""` | 会话 ID |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |
| `buffer` | bool | 否 | `false` | `true`=缓冲写入，`false`=直接写入 |
| `enable_long_term_mem` | bool | 否 | `true` | 启用长期记忆 |
| `enable_semantic_memory` | bool | 否 | `true` | 启用语义记忆 |
| `enable_episodic_memory` | bool | 否 | `true` | 启用情景记忆 |
| `enable_summary_memory` | bool | 否 | `true` | 启用摘要记忆 |
| `enable_user_profile` | bool | 否 | `true` | 启用用户画像 |

### flush 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `session_id` | string | 否 | `""` | 会话 ID |

### status 操作

无需参数。

### update 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `mem_id` | string | 是 | — | 要更新的记忆 ID |
| `memory` | string | 是 | — | 修正后的记忆文本内容 |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |

### delete 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `mem_id` | string | 是 | — | 要删除的记忆 ID |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |

### batch_delete 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `mem_ids` | array | 是 | — | 要删除的记忆 ID 列表 |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |

### update_variables 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `variables` | object | 是 | — | 键值对 `{"变量名": "变量值"}`，如 `{"status": "normal"}` |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |

### delete_variables 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `names` | array | 是 | — | 要删除的变量名列表 |
| `scope_id` | string | 否 | `"edp_agent"` | 多租户隔离标识 |

### trace 操作

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `message_id` | string | 是 | — | 要追溯的消息 ID（通常从记忆的 `source_id` 字段获取） |

## 返回字段

### search 操作

```json
{
  "success": true,
  "operation": "search",
  "user_id": "<YOUR_USER_ID>",
  "scope_id": "edp_agent",
  "total_results": 3,
  "variables": {"category": "general", "priority": "normal"},
  "results": [
    {
      "content": "2024-01-15，用户 USER_001 处理结论：正常...",
      "type": "summary",
      "score": 1.0,
      "source": "summary_search"
    }
  ],
  "search_summary": "共召回 3 条记忆：画像变量 2 项 + 向量搜索 0 条 + 摘要搜索 1 条 + 页面检索 2 条",
  "breaker_open": false
}
```

### save 操作（直接写入）

```json
{
  "success": true,
  "operation": "save",
  "user_id": "<YOUR_USER_ID>",
  "mode": "direct",
  "message_count": 1,
  "breaker_open": false
}
```

### save 操作（缓冲写入）

```json
{
  "success": true,
  "operation": "save",
  "user_id": "<YOUR_USER_ID>",
  "mode": "buffered",
  "buffered_messages": 1,
  "buffer_total_chars": 1523,
  "buffer_threshold": 20000,
  "breaker_open": false
}
```

### save 操作 — 空内容时

```json
{
  "success": false,
  "operation": "save",
  "user_id": "<YOUR_USER_ID>",
  "error": "没有可写入的消息内容（全部为空或仅包含空白字符）",
  "mode": "filtered",
  "breaker_open": false
}
```

> **v1.1+ 修复**：当所有消息的 `content` 均为空字符串或仅包含空白字符时：
> - Skill 本地过滤（防御层）→ 返回 `success: false`（如上）
> - 若绕过 skill 直接调用 mem1.0 HTTP API → mem1.0 返回 HTTP 422 `{"detail": "No valid messages (all content is empty)"}`
>
> PR #244 已修复：mem1.0 服务端现在过滤空内容和纯空白内容（`content.strip() == ""`）。Skill 仍保留本地过滤作为熔断/buffer 场景的防御层。

### flush 操作

```json
{
  "success": true,
  "operation": "flush",
  "user_id": "<YOUR_USER_ID>",
  "flushed_count": 5,
  "breaker_open": false
}
```

### status 操作

```json
{
  "success": true,
  "operation": "status",
  "circuit_breaker": {
    "open": false,
    "failure_count": 0,
    "threshold": 5,
    "cooldown_seconds": 120,
    "breaker_open_until": 0.0
  },
  "config": {
    "base_url": "<YOUR_MEMORY_BASE_URL>",
    "timeout": 30,
    "buffer_chars_threshold": 20000,
    "state_dir": "/tmp/mem1_skill_state"
  },
  "buffers": {}
}
```

### update 操作

```json
{
  "success": true,
  "operation": "update",
  "user_id": "<YOUR_USER_ID>",
  "scope_id": "edp_agent",
  "mem_id": "mem_abc123",
  "message": "Memory updated successfully",
  "breaker_open": false
}
```

### delete 操作

```json
{
  "success": true,
  "operation": "delete",
  "user_id": "<YOUR_USER_ID>",
  "scope_id": "edp_agent",
  "mem_id": "mem_abc123",
  "message": "Memory deleted successfully",
  "breaker_open": false
}
```

### batch_delete 操作

```json
{
  "success": true,
  "operation": "batch_delete",
  "user_id": "<YOUR_USER_ID>",
  "scope_id": "edp_agent",
  "deleted": 2,
  "failed": 0,
  "errors": [],
  "breaker_open": false
}
```

### update_variables 操作

```json
{
  "success": true,
  "operation": "update_variables",
  "user_id": "<YOUR_USER_ID>",
  "scope_id": "edp_agent",
  "updated_count": 2,
  "message": "Variables updated successfully",
  "breaker_open": false
}
```

### delete_variables 操作

```json
{
  "success": true,
  "operation": "delete_variables",
  "user_id": "<YOUR_USER_ID>",
  "scope_id": "edp_agent",
  "deleted": 1,
  "breaker_open": false
}
```

### trace 操作

```json
{
  "success": true,
  "operation": "trace",
  "message_id": "msg_abc123",
  "message": {
    "role": "user",
    "content": "原始对话内容...",
    "timestamp": "2026-08-26T15:36:17.517757+08:00"
  },
  "breaker_open": false
}
```

> **时区说明**：v1.1+（对应 mem1.0 PR #244 后），`timestamp` 字段使用**本地时区**（带时区偏移，如 `+08:00`），不再强制使用 UTC。Agent 在解析时间戳时应考虑时区信息。

### trace 操作 — 消息不存在时

```json
{
  "success": true,
  "operation": "trace",
  "message_id": "msg_nonexistent_id",
  "message": {
    "found": false,
    "message_id": "msg_nonexistent_id"
  },
  "breaker_open": false
}
```

> **v1.1+ 修复**：当 `message_id` 不存在时，mem1.0 不再返回 HTTP 500，而是 `{"found": false}`（PR #244 已修复 `message_manager.get_by_id` 的 `BaseError` 异常处理）。Skill 仍返回 `success: true`，调用方需检查 `message.found` 字段判断溯源是否成功。

### 失败时（fail-open）

```json
{
  "success": false,
  "operation": "search",
  "user_id": "<YOUR_USER_ID>",
  "error": "连接失败: Connection refused",
  "results": [],
  "breaker_open": false
}
```

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MEM1_BASE_URL` | `<YOUR_MEMORY_BASE_URL>` | agent-memory1.0 服务地址 |
| `MEM1_API_KEY` | `<YOUR_MEMORY_API_KEY>` | API 认证密钥 |
| `MEM1_TIMEOUT` | `30` | 请求超时（秒） |
| `MEM1_STATE_DIR` | `/tmp/mem1_skill_state` | 熔断器/缓冲区状态文件目录 |
| `MEM1_CIRCUIT_BREAKER_THRESHOLD` | `5` | 熔断器连续失败阈值 |
| `MEM1_CIRCUIT_BREAKER_COOLDOWN` | `120` | 熔断器冷却秒数 |
| `MEM1_BUFFER_CHARS_THRESHOLD` | `20000` | 缓冲区字符数阈值（超过则自动 flush） |
| `MEM1_DEFAULT_SCOPE_ID` | `edp_agent` | 默认 scope_id（多租户隔离） |

## 召回链路

```
search 操作（与 MemoryRail _http_search_mem1 完全对等）:
  0. POST /get_variables/              ← 用户画像变量（risk_preference 等）
  1. POST /search_memory/              ← 向量语义搜索
     ↓
  2. POST /search_user_history_summary/ ← 对话摘要搜索
     ↓ 无结果或结果不足
  3. POST /get_user_mem_by_page/       ← 页面检索（兜底，仅在前 3 路返回空时触发）
     ↓
  合并去重 → 返回 results + variables
```

## 写入链路

```
save 操作 (buffer=false):
  POST /add_messages/  →  直接写入 agent-memory1.0

save 操作 (buffer=true):
  追加到缓冲区文件  →  满足阈值时自动 flush
                    →  或手动调用 flush 操作
```

## 更新与删除链路

```
update 操作:
  POST /update_mem_by_id/  →  用新内容替换指定记忆

delete 操作:
  POST /delete_mem_by_id/  →  删除单条记忆

batch_delete 操作:
  POST /batch_delete_mem/  →  批量删除多条记忆（向量 + KV 批量删，非逐条 HTTP）

update_variables 操作:
  POST /update_variables/  →  创建或更新用户画像变量

delete_variables 操作:
  POST /delete_variables/  →  按名称删除用户画像变量
```

## 消息溯源链路

```
trace 操作:
  POST /get_message_by_id/  →  返回原始消息的 role / content / timestamp
  用途：从记忆的 source_id 追溯到原始对话，满足审计要求
```

## 熔断器 (Circuit Breaker)

```
正常状态:  failure_count = 0, breaker_open = false
  ↓
第1次失败: failure_count = 1
第2次失败: failure_count = 2
...
第5次失败: failure_count = 5 → 熔断器打开! 冷却 120 秒
  ↓
冷却期间: 所有操作直接返回 success:false，不发起 HTTP 请求
  ↓
120 秒后:  自动复位，尝试下一次调用
  ├─ 成功 → failure_count 归零，恢复正常
  └─ 失败 → 重新开始计数...
```

- 熔断器状态存储在 `{MEM1_STATE_DIR}/circuit_breaker.json`，跨进程共享
- 召回和写入共享同一个熔断器
- 冷却时间到后自动复位，无需人工干预

## 批量写入 (Batch Write)

```
save (buffer=true) → 追加到 {MEM1_STATE_DIR}/buffer_{user_id}.jsonl
                   → 累计字符数 >= MEM1_BUFFER_CHARS_THRESHOLD (20000) → 自动 flush
flush 操作         → 手动将缓冲区所有消息批量写入 agent-memory1.0
```

三种触发条件：
1. **字符数阈值**（主力）：缓冲区累计字符数 ≥ `MEM1_BUFFER_CHARS_THRESHOLD`
2. **手动 flush**：AgentRule.md 调用 `flush` 操作
3. **自动 flush**：下次 `save` 时检测到超过阈值，先 flush 再追加

## fail-open（故障降级）

所有操作出现错误时：
- 返回 `success: false` + 结构化错误信息
- 不抛异常，不中断 Agent 流程
- 熔断器打开时跳过 HTTP 调用，直接返回降级结果
- 消息内容自动截断（超过 3900 字符）

## 注意事项

1. **召回链路与 MemoryRail 完全对等**：四路召回（get_variables → search_memory → search_user_history_summary → get_user_mem_by_page 兜底），确保 Skill 方式与代码修改方式获得相同质量的记忆数据
2. **user_id 隔离**：agent-memory1.0 按 user_id + scope_id 隔离数据，确保不同用户数据不交叉
3. **不依赖 EDPAgent 框架**：本 Skill 是纯 Python HTTP 脚本，不依赖 `risk_review.skill_runtime` 等内部模块，可以独立运行和测试
4. **熔断器跨进程共享**：通过文件系统实现，确保多次 `call_mcp` 调用之间熔断器状态一致
5. **缓冲写入适用于多轮对话**：AgentRule.md 中每轮调用 `save` 时使用 `buffer: true`，对话结束时调用 `flush` 统一写入
6. **scope_id 默认值**：`edp_agent`，与 MemoryRail 的 `memory_scope_id` 保持一致

## 附：mem1.0 服务端其他端点（运维参考，未集成到 Skill）

以下端点由 mem1.0 服务端提供，但**未被本 Skill 集成**（运维场景使用，Agent 无需调用）：

| 端点 | 方法 | 用途 | 集成建议 |
|------|------|------|---------|
| `/admin/mem_meta/refresh` | POST | 触发元数据刷新任务（异步） | ❌ 运维操作 |
| `/admin/mem_meta/expired_memorys` | POST | 查询有过期记忆的 Top N 用户 | ❌ 运维操作 |
| `/admin/mem_meta/batch_delete` | POST | 批量删除过期记忆（异步） | ❌ 运维操作 |
| `/admin/mem_meta/task_status` | GET | 查询异步任务状态 | ❌ 运维操作 |
| `/delete_mem_by_scope/` | POST | 按 scope 删除某用户全部记忆 | ⚠️ 可选集成 |
| `/get_user_mem_by_page_with_total/` | POST | 分页获取（含 total） | ⚠️ 可选集成 |
| `/admin/messages/query` | POST | 管理端消息查询 | ❌ 运维操作 |
| `/admin/messages/stats` | GET | 管理端消息统计 | ❌ 运维操作 |
| `/admin/messages/detail/{msg_id}` | GET | 管理端消息详情（含完整元数据） | ⚠️ 可选（trace 替代） |
| `/logs/tail` `/logs/download` `/logs/files` | GET | 日志查询 | ❌ 运维操作 |

> **设计原则**：Skill 仅集成 Agent 必需的操作，运维/管理类端点通过独立脚本或平台工具直接调用。

## 附：v1.1 变更摘要（对应 mem1.0 PR #244）

| 变更 | mem1.0 端改动 | 对 Skill 的影响 |
|------|-------------|---------------|
| `message_manager.get_by_id` 补 `except BaseError` | `message_manager.py:138-144` | trace 操作：不存在 message_id 时返回 `{"found":false}` 而非 HTTP 500 |
| `add_messages` 端点过滤空/空白内容 | `memory_server.py:561-590` | save 操作：全部为空时服务端返回 422 |
| 返回时间戳改用本地时区 | 5 个 *_manager.py / *_store.py | trace/search/get 返回的时间戳带本地时区偏移（如 `+08:00`） |
| 新增 mem_meta 4 个管理端点 | `mem_meta_api.py` | Skill 未集成，运维参考 |

> Skill 代码无需修改即可兼容 PR #244 后的 mem1.0 服务端。