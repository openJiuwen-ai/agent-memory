# F07 — 接入层错误语义一致性

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-13 |
| 影响范围 | `jiuwen_memory_entry/mcp_server/`、`jiuwen_memory_entry/cli/`、`jiuwen_memory_entry/core/config_loader.py`、`jiuwen_memory_entry/http_server/__main__.py`、`jiuwen_memory/control/engine_impl/in_memory_engine.py` |
| 测试基线 | `ruff check` 通过；`tests/unit/jiuwen_memory_entry/test_mcp.py`（含新增 37 项严格边界用例）通过；`tests/unit/jiuwen_memory_entry/test_cli.py`、`test_config_loader.py`（含新增回归）通过；`tests/unit/api/test_space_api.py` 与 `tests/integration/test_dev_fallback_business_continuity.py`（OFFLINE 空间治理往返）通过 |
| Refs | 与 F06 同属接入层补全；本特性各项均非同一 MR 引入（存量），在 MR #359 合入前复测中集中暴露 |

## 背景

接入层还有第三项职责：**失败必须是显式的**。调用方判断成败依靠的信号并不多——HTTP 状态码、CLI 退出码、stderr、MCP 抛出的异常——一旦接入层吞掉错误、把非结果当成功、或把错误详情压缩成空行，调用方就失去了全部依据，只能看到「返回 200」这种无信息量的假成功。

本特性把「错误如何呈现」固化为契约，覆盖三件事：**失败显式化**（错误一律转为公开面白名单异常，不裸抛解析器异常、不静默跳过）、**报文与退出码语义**（状态码与退出码反映真实结果，而非传输状态的字面值）、**各接入路径同口径**（同一种错误在 HTTP / CLI / MCP 上得到同一处置，不出现「某条路径静默接受」）。[F06](F06-access-parameter-forwarding.md) 保证的是参数能否无损到达，本特性保证的是失败能否被明确看见——两者互补，共同构成接入层的完整契约。

## 交付的保证

以下行为是调用方可以依赖的契约，也是本特性的验收点。

### E1 协议层故障不被当作成功

HTTP 2xx 但响应体不是合法 JSON，属于**协议层故障**：CLI 输出错误体、退出码非 0。已是失败的错误响应若非 JSON，保留原状态码（信息更有用）。

### E2 契约外输入在边界处被拒，且各路径同口径

未知字段在进入业务逻辑前被拒；HTTP / CLI / MCP 三条路径判定一致，不存在「某条路径静默丢字段、另两条报错」的分叉。

### E3 错误详情不被输出模式压缩

quiet 只压缩**成功结果**的输出。失败时错误体必须完整可见（`error: <类型>: <message>`），不能因为选了精简模式就只打出一个空行。

### E4 配置错误走白名单异常

损坏的 `--config` 层（JSON/YAML 解析失败、顶层非对象）报一行错误并给出退出码，不打印 traceback；三个 surface 走同一种公开异常类型，调用方可统一 `except`。

### E5 空间治理在 OFFLINE 装配下闭环

`create_space` → `delete_space` → `get_space` 必须闭环（末步 404）。删除是两步事务，任一环节失败都会让空间永久卡在删除中，因此该链路不得依赖仅在生产装配下成立的前提。

## 决策

### 1. MCP 工具参数模型收紧为 `extra=forbid`

工具全部注册后，对每个工具的 `arg_model` **子类化覆盖** `model_config` 为
`extra="forbid"`（`mcp_server/__main__.py::_tighten_tool_arg_models`）。收紧前后字段
集合与 JSON Schema 必须一致（实现内校验），仅拒绝契约外字段。三条接入路径（HTTP /
CLI / MCP）自此同口径：契约外字段一律报错，不再有「MCP 静默丢、HTTP 报 400」的分叉。

### 2. `InMemoryEngine.purge_space` 不设 space 守卫

本引擎**写路径**全部经 `_ensure_local_scope` 拒绝 space scope——引擎内不可能存在空间
记忆，purge 天然是 no-op（返回 `[]`），元数据删除正常完成。OFFLINE 装配下空间治理
（create/delete）自此对称可用。写路径守卫保持不变（`test_in_memory_engine_rejects_non_empty_space`
钉住）。

### 3. CLI 退出码由「真实结果」决定，不由传输状态字面值决定

HTTP 2xx + 非 JSON 体在 `_read_json` 解码失败时映射为 502（错误响应体本身非 JSON 时
保留原状态码，信息更有用）。退出码 1、错误体走 stderr——不再出现「输出 BadResponse
却报成功」。

### 4. quiet 只压缩成功结果，错误体完整可见

`_render_quiet` 遇错误体输出 `error: <类型>: <message>`，与 text 模式同格式。quiet 的
语义是「成功时只要 id」，不是「失败时什么都不说」。

### 5. 配置层错误统一走白名单异常

`load_layer` 把解析失败（JSON/YAML）与顶层非对象统一转为 `ValidationError`
（`AgentMemoryError` 白名单），三个 surface 都按既有错误映射接住：

- HTTP：配置加载移入 `main()` 的 try 块，报一行错误、退出码 2；
- CLI：`main()` 的捕获列表加入 `ValidationError`，与 `CliError` 同口径；
- MCP：模块级装配路径不变（启动失败即退出）。

错误文案统一为英文（`config file <path> is not valid JSON: ...` 等），与其余接入层
错误面一致。

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
