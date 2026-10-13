# F07 — 接入层错误语义与严格边界

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-13 |
| 影响范围 | `jiuwen_memory_entry/mcp_server/`、`jiuwen_memory_entry/cli/`、`jiuwen_memory_entry/core/config_loader.py`、`jiuwen_memory_entry/http_server/__main__.py`、`jiuwen_memory/control/engine_impl/in_memory_engine.py` |
| 测试基线 | `ruff check` 通过；`tests/unit/jiuwen_memory_entry/test_mcp.py`（含新增 37 项严格边界用例）通过；`tests/unit/jiuwen_memory_entry/test_cli.py`、`test_config_loader.py`（含新增回归）通过；`tests/unit/api/test_space_api.py` 与 `tests/integration/test_dev_fallback_business_continuity.py`（OFFLINE 空间治理往返）通过 |
| Refs | MR #359 合入前复测反馈的五个问题；均非该 MR 引入（存量缺陷） |

## 背景

对 MR #359 提交版本的复测发现五处接入层缺陷，共同模式是**错误被静默吞掉或误报成功**：

1. **MCP 静默忽略额外参数**：客户端传入 `security` 等契约外字段，FastMCP 的
   `ArgModelBase` 未配置 `extra`（pydantic 默认 ignore），字段在进入工具函数前被丢弃；
   而同一请求走 HTTP/CLI 会被 `parse_request` 以 400 拒绝——三条路径严格性不一致。
2. **OFFLINE 删除空间失败**：`InMemoryEngine.purge_space` 开头按 `scope.space` 守卫拒绝，
   而 `delete_space` 两步事务先置 `DELETING` 再 purge——删除必败且重试同败，空间永久卡在
   删除中。
3. **CLI 误报成功**：HTTP 返回 200 但响应体不是合法 JSON 时，`_read_json` 生成
   `BadResponse` 错误体却沿用 200 状态；`_write` 只看状态决定退出码——输出错误体、
   退出码却是 0。
4. **quiet 模式吞掉错误详情**：`_render_quiet` 只提取 `id` / `unit_id`，认证失败、
   记忆不存在等错误体被渲染成空行。
5. **损坏配置触发 traceback**：`load_layer` 把 `JSONDecodeError` / `YAMLError` 裸抛，
   CLI `main()` 只捕 `CliError` / `OSError`，直接崩溃；HTTP 侧配置加载也在 try 块之外。
   附带发现：顶层是数组（如 `[]`）的配置层在合并循环中被**静默跳过**，调用方以为
   配置生效了实际没有。

## 决策

### 1. MCP 工具参数模型收紧为 `extra=forbid`

工具全部注册后，对每个工具的 `arg_model` **子类化覆盖** `model_config` 为
`extra="forbid"`（`mcp_server/__main__.py::_tighten_tool_arg_models`）。收紧前后字段
集合与 JSON Schema 必须一致（实现内断言），仅拒绝契约外字段。三条接入路径（HTTP /
CLI / MCP）自此同口径：契约外字段一律报错，不再有「MCP 静默丢、HTTP 报 400」的分叉。

### 2. `InMemoryEngine.purge_space` 去掉 space 守卫

本引擎**写路径**全部经 `_ensure_local_scope` 拒绝 space scope——引擎内不可能存在空间
记忆，purge 天然是 no-op（返回 `[]`），元数据删除正常完成。OFFLINE 装配下空间治理
（create/delete）自此对称可用。写路径守卫保持不变（`test_in_memory_engine_rejects_non_empty_space`
钉住）。

### 3. CLI 退出码由「真实结果」决定，不由传输状态字面值决定

HTTP 2xx + 非 JSON 体是**协议层故障**：`_read_json` 解码失败映射为 502（错误响应体本身
非 JSON 时保留原状态码，信息更有用）。退出码 1、错误体走 stderr——不再出现「输出
BadResponse 却报成功」。

### 4. quiet 只压缩成功结果，错误体完整可见

`_render_quiet` 遇错误体输出 `error: <类型>: <message>`，与 text 模式同格式。quiet 的
语义是「成功时只要 id」，不是「失败时什么都不说」。

### 5. 配置层错误统一走白名单异常

`load_layer` 把解析失败（JSON/YAML）与顶层非对象统一转为 `ValidationError`
（`AgentMemoryError` 白名单），三个 surface 都按既有错误映射接住：

- HTTP：配置加载移入 `main()` 的 try 块，报一行错误、退出码 2；
- CLI：`main()` 的捕获列表加入 `ValidationError`，与 `CliError` 同口径；
- MCP：模块级装配路径不变（启动失败即退出）。

## 拒绝的方案

- **patch FastMCP SDK 源码或升级依赖**：侵入第三方包、影响面不可控；子类化覆盖
  `model_config` 在注册后完成，等价且零依赖变更。
- **在每个工具函数里手动校验未知键**：36 个工具 × 手写清单，正是 F06 用契约锁消灭的
  第二份参数表。
- **`purge_space` 改为显式删除 space scope 数据**：本引擎根本不可能有这类数据（写路径
  拒绝），为不可达路径加逻辑只会引入新分支。
- **BadResponse 保持 200 状态**：调用方脚本按退出码判断成败，语义上「没拿到合法结果」
  就是失败。

## 验证

- `tests/unit/jiuwen_memory_entry/test_mcp.py` §F：37 项——全部工具的 arg_model 拒绝
  `security` 注入（parametrize）+ 完整 `tool.run` 路径在校验层被拒且不触达 `_invoke`。
- `tests/unit/api/test_space_api.py`：`purge_space` 对非空 space 名是 no-op（引擎级回归）。
- `tests/integration/test_dev_fallback_business_continuity.py`：OFFLINE+DEV 装配
  create→delete→get(404) 完整往返。
- `tests/unit/jiuwen_memory_entry/test_cli.py`：2xx+非JSON→502/退出码 1；错误响应保留
  原状态码；quiet 渲染错误详情；损坏 `--config` 退出码 2 无 traceback。
- `tests/unit/jiuwen_memory_entry/test_config_loader.py`：解析错误转 `ValidationError`
  （JSON/YAML 各一）；顶层非对象（`[]` / 字符串 / 数字）报错。

## 已知遗留

1. `tests/unit/jiuwen_memory_entry/test_http_server_security.py` 在 Windows 下偶发
   `WinError 10053`（socket 时序），与本次改动无关，建议单独立项排查。
