# F06 — Access 参数完整转发

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-13 |
| 影响范围 | `jiuwen_memory/api/`、`jiuwen_memory_entry/core/`、`jiuwen_memory_entry/mcp_server/`、`docs/specs/S02-memory-api.md`、`docs/design/architecture.md` |
| 测试基线 | `ruff check` 通过；`tests/unit/jiuwen_memory_entry` + `tests/unit/api` 835 项通过；全量 `tests/unit -m unit` 通过；`test_http_server_security.py` 为既有 Windows socket 时序 flaky，单独重跑通过，与本特性无关 |
| Refs | #221、#226；#246 的四处透传由 PR #358 单独负责 |

## 背景

接入层（HTTP / CLI / MCP / 旧进程内）在 JSON 到 typed API 对象的转换过程中会静默丢弃字段。
除 PR #358 已处理的检索时点、删除模式、记忆层级与演进通道外，以下几类也存在断链：

- **整字段未转发**：`SpaceSpec.owner`、`SpaceMember` 的两轴角色与成员时间、
  `DeleteSelector` 的选择条件、`Grant.actions` / `expires_at`、视频消息的 `occurred_at`。
- **值级静默降级**：可选字段用真值判断，导致 `policy={}`、`metadata={}`、`status=""`
  这类「显式清空」被丢弃；枚举直接构造使非法值越过统一错误映射，形成 500 而不是 400。
- **解析助手自身的宽松**：布尔未识别值静默取假、枚举先 `str()` 再匹配、整数解析接受
  `bool` / `float`。

这类缺陷的共同后果是**返回 200 但语义与调用方意图不符**，且没有错误信号可供发现。

## 决策

### 1. HTTP / CLI 继续从方法签名派生契约

沿用 F05：HTTP 与 CLI 的字段集合、默认值与类型解码仍由 `MemoryAPI` 签名派生
（`core/api_contract.py`），不维护第二份参数清单。MCP 保持显式工具签名，由
`test_mcp.py` 的契约锁保证「工具参数名集合 **==** 契约参数集合」。

### 2. Core 历史兼容入口按 typed 字段补齐，并新增覆盖锁

旧进程内入口 `handler.dispatch`（`jiuwen_memory_entry/core/handler.py`）是对 `MemoryAPI`
的平行手写实现，契约新增可选字段时没有任何机制强制两边同步——这才是漏传的发生地。
本次补齐上述已有 typed 字段，并新增 `test_legacy_contract_coverage.py` 作为防漂移锁：
路由分类、契约参数覆盖（dataclass 参数细化到字段）、声明的 legacy 键确实被读到、
`gap(...)` 必须写明原因且当前确实不可达。

**legacy 面实际支持哪些字段，以该测试的覆盖表为单一事实来源**，本文不复制字段清单，
避免两处描述再次漂移。

### 3. 解析助手收紧为「不猜意图」

- 布尔：未识别的字符串不再静默取假，改为 `ValidationError`。
- 枚举：去掉 `str()` 归一（`None` 不再变成 `"None"`），捕获 `TypeError`，并放行已是
  枚举实例的入参。
- 整数：显式拒绝 `bool` 与 `float`，不接受 `int(True)` / `int(1.5)` 这类静默转换。

### 4. 可选字段的「缺省」与「显式空值」必须分开

值级判定从真值判断改为 `is not None`，使 `policy={}`、`metadata={}`、`status=""` 能表达
「显式清空」。同一原则适用于 `grant` / `revoke` 的 `actions`：

- **缺省**（键缺失或 `null`）沿用历史兼容的只读授权，保持旧调用方行为不变；
- **显式空值**（`[]`、`""`、`[" "]`）直接报错。

后者若回落成只读，一次「动作没写清」的调用会静默变成只读授权（grant），撤销时也只撤掉
只读那一条（revoke）——两者都返回 200，调用方无从察觉。

### 5. 视频摄入的 `occurred_at` 走 `submit_ingest` 参数

`occurred_at` 属于消息发生时间，因此扩展 `MemoryAPI.submit_ingest` 与 MCP 工具
`memory_submit_ingest` 的签名，由后台 `add` 原样接收。**不借 metadata 旁路传递**。

MCP 工具侧该参数注解为 `str | None`，与本仓既有约定一致：FastMCP 会把客户端 ISO
字符串 coerce 成 `datetime` 对象再进工具函数，而共享契约的 JSON 边界只接受 ISO 字符串。

### 6. 本次不重复 PR #358 覆盖的四处

`search.as_of`、`delete.mode`、`update.tier`、`evolve.channel` 由 PR #358 负责，本次不改，
避免两边同时修改同一函数体造成合并冲突。这四处在覆盖锁中以 `gap(...)` 显式登记。

## 拒绝的方案

- **为 HTTP / CLI 再维护一份独立参数表**：接口增加可选参数时容易再次遗漏；签名派生契约
  已同时完成字段校验、类型解码与默认值处理。
- **把未知枚举或布尔字符串当作默认值**：请求会返回成功但执行不同语义，等于保留静默错误。
- **把视频 `occurred_at` 写入 metadata**：下游 `add` 不会把它解释为消息发生时间，且会污染
  元数据命名空间。
- **用「payload 键 vs 契约参数名」的纯静态比对代替显式归属表**：scope 类字段、适配器上提的
  `items` / `spec` / `patch` / `selector`、嵌套 dict 都会误报，噪声约 60 条，无信号；
  必须把「该参数由谁提供」写死成显式表才可用。
- **把 legacy 字段清单抄进本文**：两份描述会各自漂移，与设立覆盖锁的目的相悖。

## 验证

- `tests/unit/jiuwen_memory_entry/test_handler_entry_params.py`：字段转发、缺省值、非法枚举、
  真假字符串、嵌套 scope、成员两轴角色与时间、视频 `occurred_at`、`actions` 缺省与显式空值。
- `tests/unit/jiuwen_memory_entry/test_legacy_contract_coverage.py`：legacy 覆盖锁本体，
  并对「宣称为已支持而未透传」「键名拼错」「契约参数漏登记」「路由未分类」「补上透传后
  gap 行未删」五类漂移做过负向验证。
- `tests/unit/api/test_submit_ingest_occurred_at.py`：`occurred_at` 经任务控制器转发到 `add`。
- `tests/unit/jiuwen_memory_entry/test_mcp.py`：工具参数与契约的一致性锁。
- `python -m pytest -q -p no:cacheprovider tests/unit/jiuwen_memory_entry tests/unit/api`
- `python -m pytest -q -p no:cacheprovider -m unit tests/unit`

## 已知遗留

1. **#246 的四处透传**仍缺失，待 PR #358 合入；覆盖锁中的 `gap(...)` 行届时会失败，强制删除。
2. **`revoke` 不传 `actions` 时仍只撤只读**：缺省语义保持向后兼容；若当初以 `actions: ["write"]`
   授权，撤销时须显式带上 `actions`。目标形态按 `grant_id` 精确定位（`GrantStore` 未实装，
   见 F05 §5.4）。
3. **`Grant.actions` 为显式空数组时，`PermissionManager` 是静默 no-op**：本特性只收紧了 legacy
   入口的解析（显式空值报错），HTTP 路径仍会把空集合传到 `grant()` 而不报错。该差异属于
   安全域接口边界问题，不在本特性范围内。
4. jiuwenSwarm 的参数适配按范围约束暂不处理。
