# MCP surface：MemoryAPI 的 Model Context Protocol 接入面

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-11 |
| 影响范围 | `jiuwen_memory_entry/mcp_server/`（`__main__.py`、`transport_security.py`；原 `DESIGN.md` 已并入本文档），`tests/unit/jiuwen_memory_entry/test_mcp.py`、`test_mcp_transport_security.py` |
| 测试基线 | `pytest tests/unit/jiuwen_memory_entry/test_mcp.py tests/unit/jiuwen_memory_entry/test_mcp_transport_security.py`（92 + 5 全绿）；`ruff check` 通过 |
| 备注 | 本文档吸收原 `jiuwen_memory_entry/mcp_server/DESIGN.md`（已删除），模块级设计、方案取舍与已知遗留统一在此维护 |

## 背景

MCP 宿主（Claude Desktop / Cursor / Claude Code 等）需要把记忆能力注册为 MCP 工具，
供宿主中的模型在对话里自主调用。HTTP/CLI 的命令面与参数校验可从 `MemoryAPI` 签名
反射生成（`jiuwen_memory_entry/core/api_contract`），但 MCP 工具是**手写**的——
FastMCP 工具函数需要面向模型的 docstring 与显式运行时类型注解（`Context` 注入依赖
运行时注解对象，字符串化注解会破坏机制），无法走同一条反射路径。工具签名因此失去
结构上的防漂移屏障，需要契约锁测试补位；同时「模型是调用方」意味着 docstring 承担
「让模型选对工具、填对参数」的职责，与面向人的 CLI help 写法不同。

## 决策

1. **工具集与 `MemoryAPI` 全量对齐（36/36）**，命名 `memory_<method>`：与 HTTP/CLI
   共享同一套 `api_contract` 参数校验（`parse_request`）与调用桥（`invoke_api`），
   MCP 面只做协议翻译、零业务编排。
2. **async 工具 + `asyncio.to_thread` 隔离**：FastMCP 在事件循环线程裸调工具函数，
   而同步 `MemoryAPI` 方法在 api 层内部用 `asyncio.run` 桥接协程——两者相遇必抛
   "cannot be called from a running event loop"。执行体放入工作线程（无运行中循环，
   内部桥接照常工作），结果经 await 回流事件循环。
3. **`submit_ingest` 保留扁平协议**：工具签名 5 个业务参数 + `ctx`（FastMCP 框架
   注入的传输上下文，不进模型可见 Schema）。G.FNM.03（单工具入参数阈值 5）按业务
   参数计恰在阈值内，`ctx` 以注释说明豁免依据；不改变公开请求形状
   `{"content", "scope", "source", "payload_id", "source_ref"}`。
4. **认证 fail-closed**：`JIUWEN_MEMORY_MCP_AUTH_MODE`（required | dev，默认
   required）。`required` 未装配生产认证器时业务调用全部拒绝；`dev` 使用固定
   `local/developer` ROOT 测试身份、仅允许回环绑定。身份只经
   `auth_middleware.authenticated`（`Surface.MCP`）注入 `security`，payload 中的
   `security/identity/actor` 等保留字段在契约边界直接拒绝。
5. **契约锁测试**：`test_mcp.py` 对 36 个工具锁「工具签名参数 ⊆ 契约参数、契约必填
   ⊆ 工具参数、代表性 payload 可过 `parse_request`」，防止工具签名与 API 签名漂移。

## 拒绝的方案

| 方案 | 原因 |
|------|------|
| 像 HTTP/CLI 一样从 `MemoryAPI` 反射生成 MCP 工具 | FastMCP 要求显式装饰器函数与面向模型的 docstring（模型凭它选工具填参），反射无法生成模型可读描述；`Context` 注入还要求保留运行时注解对象 |
| `submit_ingest` 用 dataclass 参数袋压参（6 → 2） | 会把公开请求从扁平改成嵌套 `{"args": {...}}`——FastMCP 按函数签名生成客户端可见参数结构，协议形状随之改变，既有 MCP 调用方全部失配（S09 第 14 条兼容要求）；仅为「参数数量」指标付出破坏公开协议的代价不成立。契约锁测试同步失败也证明该改动与契约冲突 |
| `submit_ingest` 维持 6 参数且不做任何豁免说明 | 静态检查报警无法闭环；豁免必须有依据——`ctx` 是框架注入参数、不是业务参数，理由已注释在工具定义处 |
| dev 模式跳过 MemoryAPI 授权判定 | 会把本地测试习惯带进生产路径；保留授权判定、只固定身份，dev 仍是「同一张授权网下的测试身份」 |
| 限流 / workload_guard 随生产认证器接入自动生效 | `authenticated` 的 `limiter`/`workload_guard` 是显式参数，MCP 面当前调用链未传——不能在文档里许诺不存在的接线；待生产认证 runtime 接入时一并显式装配（见已知遗留 1） |

## 接入与运行

MCP surface 是 MemoryAPI 的 Model Context Protocol 接入面：把记忆能力注册为 MCP 工具，
供 Claude Desktop / Cursor / Claude Code 等 MCP 宿主中的模型在对话里自主调用。
它与 HTTP/CLI 使用同一套契约（`jiuwen_memory_entry/core/api_contract.py`）与
同一套错误映射（`core/error_response.py`），不包含额外业务编排。

### 调用路径

```
MCP 宿主（Claude Desktop 等）
  ↓ JSON-RPC（stdio 或 Streamable HTTP）
FastMCP（mcp.server.fastmcp）→ @mcp.tool() 注册的 async 工具函数
  ↓ asyncio.to_thread（隔离两套事件循环，见决策 2）
_invoke_blocking：credentials_for_transport → authenticated(Surface.MCP) → invoke_api
  ↓
同名 MemoryAPI 方法 → 内核（control 编排 → 数据面/检索面）
```

- 与 HTTP/CLI 共享 `api_contract` 的参数校验（`parse_request`）与调用桥
  （`invoke_api`）；身份经 `auth_middleware.authenticated` 注入
  `security`，绝不来自工具参数（payload 中的 `security/identity/actor`
  等保留字段在契约边界直接拒绝）。
- 工具函数一律 **async**：FastMCP 在事件循环线程裸调工具函数，而同步
  `MemoryAPI` 方法在 api 层内部用 `asyncio.run` 桥接协程——两者相遇
  必抛 "cannot be called from a running event loop"。执行体经
  `asyncio.to_thread` 放入工作线程（无运行中循环，内部桥接照常工作），
  结果经 await 回流事件循环。

### 工具与参数

工具集与 `MemoryAPI` 公开方法**全量对齐（36/36）**，命名规则
`memory_<method>`：数据面 10 个（add/add_async/batch_add/batch_add_async/
search/list/get/update/delete）、任务与摄入 5 个（evolve/check_write/
submit_ingest/job_status/job_cancel）、管理面 3 个（admin_get/set/all）、
治理面 4 个（inspect/trace/audit/verify_audit）、授权 2 个（grant/revoke）、
Space 管理 12 个（create/get/list/update/archive/delete_space、export_space、
space_usage、get/set_space_policy、list/add/remove_space_member）。

- 参数名与 `MemoryAPI` 签名严格一致（`unit_id`/`top_k`/`with_trajectory`…），
  请求形状全部为扁平 `{"参数名": 值}`；`scope` 为对象
  `{"org","user","agent","session","space"}`，五维可给空串。
- docstring 面向模型撰写（何时调用、参数 JSON 形状、返回结构）——模型凭它
  选择工具与填参。
- `ctx: Context` 参数由 FastMCP 注入（Streamable HTTP 传输下携带请求头供
  凭据提取），**不进模型可见 Schema**。
- 部分参数未透传（如 add 的 assets/system_metadata/user_metadata/occurred_at、
  search 的 filters/as_of/disclosure）——按「模型对话场景」精选，后续按需补充。
- `verify_audit` 未装配审计完整性 provider 时返回 `unsupported`（不报错）；
  `delete_space` 当前实现仅支持 purge。

### 认证与运行

- 认证模式：`JIUWEN_MEMORY_MCP_AUTH_MODE`（required | dev，默认 required，
  失闭）。`required` 未装配生产认证器时业务调用全部拒绝；`dev` 使用固定
  `local/developer` ROOT 测试身份（忽略凭据、保留 MemoryAPI 授权判定），
  仅供本地功能测试，且只允许绑定回环地址——放开须设
  `JIUWEN_MEMORY_MCP_ALLOW_DEV_NON_LOOPBACK=true`（仅限隔离容器）。
- 凭据按传输归一（`transport_security.credentials_for_transport`）：
  stdio 读 `AGENT_MEMORY_API_KEY`；Streamable HTTP 逐请求读
  `Authorization: Bearer` 与 socket peer（拿不到请求上下文属接线错误，
  fail-closed 不回退环境变量）。
- 限流与 workload_guard：当前**未接线**——`_invoke_blocking` 调用认证中间件时
  不传 `limiter`/`workload_guard`（OFFLINE/本地运行时为 None，stdio 无网络
  对端）。两者是 `authenticated` 的显式参数，接入生产认证 runtime 时需一并
  构建传入，**不会随认证器自动生效**（见已知遗留 1）。

### 启动方式

需要 `pip install ".[mcp]"`（mcp SDK，**必须 <2**——2.x 已将 FastMCP 改名）。

```bash
# stdio（默认）：供 Claude Desktop / Claude Code 等宿主作为子进程拉起
#   宿主配置示例（claude_desktop_config.json）：
#   {"mcpServers": {"agent-memory": {
#       "command": "python",
#       "args": ["<仓库>/jiuwen_memory_entry/mcp_server/__main__.py"],
#       "env": {"PYTHONPATH": "<仓库>;<仓库>/jiuwen_memory_entry/core",
#               "JIUWEN_MEMORY_MCP_AUTH_MODE": "dev"}}}}
scripts/run-mcp.sh [config.yml ...]

# Streamable HTTP：独立进程监听，脚本/远程可调
MCP_TRANSPORT=http MCP_PORT=8139 JIUWEN_MEMORY_MCP_AUTH_MODE=dev scripts/run-mcp.sh
# 端点：POST http://localhost:8139/mcp（JSON-RPC）；GET 同路径探活
```

调试推荐 MCP Inspector（可视化 tools/list 与 tools/call 协议帧）：

```bash
JIUWEN_MEMORY_MCP_AUTH_MODE=dev npx @modelcontextprotocol/inspector \
  python jiuwen_memory_entry/mcp_server/__main__.py
```

配置叠加与 CLI 相同：位置参数按顺序叠在内置 OFFLINE 基线之上（缺省纯内存
栈，进程退出数据即清空；接真后端传对应 config.yml）。

### 端到端示例：写入并召回一条记忆

前置：`pip install ".[mcp]"`；环境变量 `JIUWEN_MEMORY_MCP_AUTH_MODE=dev`
（固定 local/developer 测试身份，免凭据）。

**第 1 步——启动 MCP Server**（详见「启动方式」，Inspector 可视化推荐入门）：

```bash
JIUWEN_MEMORY_MCP_AUTH_MODE=dev npx @modelcontextprotocol/inspector \
  python jiuwen_memory_entry/mcp_server/__main__.py
```

**第 2 步——确认工具清单**：连接后在 Tools 页看到 36 个 `memory_*` 工具
（等价于协议层的 `tools/list`）。

**第 3 步——写入**：在 `memory_add` 表单填 `content = 我喜欢喝咖啡`、
`scope = {"org":"local","user":"developer"}`，执行后返回记忆单元——记下
返回的 `id`。等价的协议帧：

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "memory_add",
            "arguments": {"content": "我喜欢喝咖啡",
                          "scope": {"org": "local", "user": "developer"}}}}
```

**第 4 步——召回**：调 `memory_search`，query 填「喝什么」、context 填
`{"scope": {"org":"local","user":"developer"}}`——返回的 `items` 中即含
第 3 步写入的那条（附相关性得分与披露层级）。

**第 5 步——后续操作**：拿 id 可继续 memory_get / memory_update（supersede
生成新版本）/ memory_evolve（触发演进，返回 job_id 后用 memory_job_status
查询）。

注意：默认 OFFLINE 内存栈，Server 进程退出数据即清空；持久化需接真后端
config（启动时位置参数传 config.yml，叠加规则同 CLI）。

## 验证

- `pytest tests/unit/jiuwen_memory_entry/test_mcp.py`（92 用例：36 工具契约锁 +
  旧字段/身份字段拒绝 + 失闭与 Surface.MCP 注入 + 功能闭环含 evolve→job_status 与
  consolidate→trace 血缘链 + Schema 无 ctx 泄漏 + FastMCP.call_tool 协议编组）
- `pytest tests/unit/jiuwen_memory_entry/test_mcp_transport_security.py`（5 用例）
- `ruff check` 通过
- 行为抽检：`memory_submit_ingest` 扁平协议经真实 FastMCP `call_tool` 调用成功并
  正确转发 API 参数；旧协议字段（`tenant_id`/`item_id`/`k`/`hard`）在契约边界
  拒绝——旧协议失败是预期安全行为，非缺陷

## 已知遗留

1. **限流与 workload_guard 未接线**：`_invoke_blocking` 调用 `authenticated` 时不传
   `limiter`/`workload_guard`，生产认证 runtime 接入时需一并构建传入，不会随认证器
   自动生效。
2. **`_SRV` 无显式统一关闭**（`__main__.py` 模块级装配、`main()` 阻塞运行）：涉及
   S09 第 13 条生命周期要求，属 cf38c2a 引入的原有待办（非本轮回归），待统一生命
   周期管理时补 stdio/HTTP 两路 shutdown 接线。
3. **管理面/治理面/Space 工具鉴权依赖管理动作授权**：dev 身份走旧授权链（按 scope
   归属判定、不读 role）时这些操作返回 PermissionDenied（F05 授权链过渡期缺口，
   非缺陷）；待 ROOT role 接入 PermissionManager 后重测。
4. **部分参数未透传**（add 的 assets/system_metadata/user_metadata/occurred_at、
   search 的 filters/as_of/disclosure）——按「模型对话场景」精选，后续按需补充。
5. **OFFLINE 内存栈不跨进程持久**——持久化需接真后端 config。
