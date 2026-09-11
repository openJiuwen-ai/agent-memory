# MCP surface：MemoryAPI 的 Model Context Protocol 接入面

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-11 |
| 影响范围 | `jiuwen_memory_entry/mcp_server/`（`__main__.py`、`transport_security.py`、`DESIGN.md`），`tests/unit/jiuwen_memory_entry/test_mcp.py`、`test_mcp_transport_security.py` |
| 测试基线 | `pytest tests/unit/jiuwen_memory_entry/test_mcp.py tests/unit/jiuwen_memory_entry/test_mcp_transport_security.py`（92 + 5 全绿）；`ruff check` 通过 |

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
3. **`submit_ingest` 采用参数袋**：G.FNM.03 限制单工具入参数（阈值 5），而
   `submit_ingest` 契约有 5 个必填参数。以 dataclass `SubmitIngestArgs`（字段即
   契约参数）压成 1 个工具入参 `args`，模型可见 Schema 为嵌套一层的
   `{"args": {...}}`，不需要平台豁免。
4. **认证 fail-closed**：`JIUWEN_MEMORY_MCP_AUTH_MODE`（required | dev，默认
   required）。`required` 未装配生产认证器时业务调用全部拒绝；`dev` 使用固定
   `local/developer` ROOT 测试身份、仅允许回环绑定。身份只经
   `auth_middleware.authenticated`（`Surface.MCP`）注入 `security`，payload 中的
   `security/identity/actor` 等保留字段在契约边界直接拒绝。
5. **契约锁测试**：`test_mcp.py` 对 36 个工具锁「转发参数 ⊆ 契约参数、契约必填 ⊆
   转发参数、代表性 payload 可过 `parse_request`」；参数袋工具（`submit_ingest`）
   锁 dataclass 字段与契约参数对齐。

## 拒绝的方案

| 方案 | 原因 |
|------|------|
| 像 HTTP/CLI 一样从 `MemoryAPI` 反射生成 MCP 工具 | FastMCP 要求显式装饰器函数与面向模型的 docstring（模型凭它选工具填参），反射无法生成模型可读描述；`Context` 注入还要求保留运行时注解对象 |
| `submit_ingest` 申请平台豁免 6 参数 | 与 G.FNM.03 的目标冲突；参数袋方案不需要豁免，且嵌套 Schema 对模型更稳定 |
| dev 模式跳过 MemoryAPI 授权判定 | 会把本地测试习惯带进生产路径；保留授权判定、只固定身份，dev 仍是「同一张授权网下的测试身份」 |
| 限流 / workload_guard 随生产认证器接入自动生效 | `authenticated` 的 `limiter`/`workload_guard` 是显式参数，MCP 面当前调用链未传——不能在文档里许诺不存在的接线；待生产认证 runtime 接入时一并显式装配（见已知遗留 1） |

## 验证

- `pytest tests/unit/jiuwen_memory_entry/test_mcp.py`（92 用例：36 工具契约锁 +
  旧字段/身份字段拒绝 + 失闭与 Surface.MCP 注入 + 功能闭环含 evolve→job_status 与
  consolidate→trace 血缘链 + Schema 无 ctx 泄漏 + FastMCP.call_tool 协议编组）
- `pytest tests/unit/jiuwen_memory_entry/test_mcp_transport_security.py`（5 用例）
- `ruff check` 通过
- 行为抽检：新协议调用成功；旧协议字段（`tenant_id`/`item_id`/`k`/`hard`）在契约
  边界拒绝——旧协议失败是预期安全行为，非缺陷

## 已知遗留

1. **限流与 workload_guard 未接线**：`_invoke_blocking` 调用 `authenticated` 时不传
   `limiter`/`workload_guard`，生产认证 runtime 接入时需一并构建传入，不会随认证器
   自动生效。
2. **`_SRV` 无显式统一关闭**（`__main__.py` 模块级装配、`main()` 阻塞运行）：涉及
   S09 第 13 条生命周期要求，属 cf38c2a 引入的原有待办（非本轮回归），待统一生命
   周期管理时补 stdio/HTTP 两路 shutdown 接线。
3. **extensions 透传与序列化边界**：`memory_search` 的 `context.extensions` 逐字
   透传给内核；返回值中含不可字符串化扩展对象时在 `to_jsonable` 序列化边界提前
   失败——复现点在 `jiuwen_memory_entry/core`（handler / api_contract），待 core 侧
   统一修复后补 MCP 面回归用例。
4. **管理面/治理面/Space 工具鉴权依赖管理动作授权**：dev 身份走旧授权链（按 scope
   归属判定、不读 role）时这些操作返回 PermissionDenied（F05 授权链过渡期缺口，
   非缺陷）；待 ROOT role 接入 PermissionManager 后重测。
5. **部分参数未透传**（add 的 assets/system_metadata/user_metadata/occurred_at、
   search 的 filters/as_of/disclosure）——按「模型对话场景」精选，后续按需补充。
6. **OFFLINE 内存栈不跨进程持久**——持久化需接真后端 config。
