# F06 — 接入层参数完整转发

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-13 |
| 影响范围 | `jiuwen_memory/api/`、`jiuwen_memory_entry/core/`、`jiuwen_memory_entry/mcp_server/`、`docs/specs/S02-memory-api.md`、`docs/design/architecture.md` |
| 测试基线 | `ruff check` 通过；`tests/unit/jiuwen_memory_entry` + `tests/unit/api` 835 项通过；全量 `tests/unit -m unit` 通过；`test_http_server_security.py` 为既有 Windows socket 时序 flaky，单独重跑通过，与本特性无关 |
| Refs | #221、#226；#246 的四处透传由 PR #358 负责（已合入 mem2.0，e6805dd） |

## 背景

接入层（HTTP / CLI / MCP / 旧进程内 `handler.dispatch`）本身不承载业务语义，它只承担两件事：把契约允许的字段**无损**交给 `MemoryAPI`，把契约不允许的输入**明确**拒掉。

F05 已经完成了前半段的基础设施——HTTP / CLI 的字段集合、默认值与类型解码统一从 `MemoryAPI` 签名派生，不再维护第二份参数清单。本特性在此之上把接入层的职责补齐为可验收的保证：**转发完整性**（签名里声明的参数，任一路径都能真正传到）、**值语义**（「缺省」与「显式空值」是两种意图，不能被真值判断抹平）、**类型严格性**（超出契约的类型与枚举值在边界处报错，不做静默转换）。

补齐这三块之前，接入层在 JSON 到 typed 对象的转换中会出现字段静默丢弃、显式清空被当作未填写、未识别字面量被当作默认值的情况——后果是**返回 200 但语义与调用方意图不符**，且没有错误信号可供发现。这正是本特性要消除的形态。

## 交付的保证

以下行为是调用方可以依赖的契约，也是本特性的验收点。

### G1 字段无损：签名里有的，路径上就有

`SpaceSpec.owner`、`SpaceMember` 的两轴角色与成员时间、`DeleteSelector` 的选择条件、`Grant.actions` / `expires_at`、视频消息的 `occurred_at` 等字段，经 HTTP / CLI / MCP / 旧进程内入口均可到达底层 typed 对象。dataclass 参数细化到字段级，嵌套字段同样不丢。

### G2 缺省与显式空值可区分

可选字段的判定基于「键是否出现」，而非真值：`policy={}`、`metadata={}`、`status=""` 表达「显式清空」并如实下发；键缺失或为 `null` 才走历史缺省。

同一原则适用于 `grant` / `revoke` 的 `actions`：**缺省**沿用历史兼容的只读授权（旧调用方行为不变）；**显式空值**（`[]`、`""`、`[" "]`）直接报错——否则一次「动作没写清」的调用会静默变成只读授权，撤销时也只撤掉只读那一条，两者都返回 200，调用方无从察觉。

### G3 类型边界严格，不做静默转换

- 布尔：未识别的字符串报 `ValidationError`，不再静默取假；
- 枚举：不做 `str()` 归一（`None` 不会变成 `"None"`），非法值走统一错误映射返回 400 而不是 500，已是枚举实例的入参放行；
- 整数：显式拒绝 `bool` 与 `float`，不接受 `int(True)` / `int(1.5)` 这类静默转换。

### G4 契约外输入在边界处被拒

契约外的字段与非法值在进入业务逻辑前被拒，调用方拿到明确错误，而不是「请求成功但没生效」。
「被判非法后如何报错、各路径是否同口径」属于错误语义，见 [F07](F07-access-surface-error-semantics.md)。

### G5 各接入路径同口径

HTTP / CLI / MCP 对字段集合与合法性的判定一致：`MemoryAPI` 签名是唯一参数清单；MCP 保持显式工具签名，靠契约锁钉住，不手写第二份清单。

## 决策

### 1. HTTP / CLI 继续从方法签名派生契约

沿用 F05：HTTP 与 CLI 的字段集合、默认值与类型解码仍由 `MemoryAPI` 签名派生
（`core/api_contract.py`），不维护第二份参数清单。MCP 保持显式工具签名，由
`test_mcp.py` 的契约锁保证「工具参数名集合 **==** 契约参数集合」。
三者因此同源，任何一侧的字段集合都不会独立漂移。

### 2. Core 历史兼容入口按 typed 字段补齐，并新增覆盖锁

旧进程内入口 `handler.dispatch`（`jiuwen_memory_entry/core/handler.py`）是对 `MemoryAPI`
的平行手写实现，是唯一不与签名派生共享来源的路径。本次将其按 typed 字段补齐，
并新增 `test_legacy_contract_coverage.py` 作为防漂移锁，使这条平行实现同样受契约约束：
路由分类、契约参数覆盖（dataclass 参数细化到字段）、声明的 legacy 键确实被读到、
`gap(...)` 必须写明原因且当前确实不可达。

**legacy 面实际支持哪些字段，以该测试的覆盖表为单一事实来源**，本文不复制字段清单，
避免两处描述再次漂移。

### 3. 解析助手收紧为「不猜意图」

字符串解析只接受契约明确允许的形态，遇到歧义输入一律报错，由调用方显式给出正确值，
而不是替它选一个默认解释。行为见 G3。

### 4. 值级判定以「键是否出现」为抓手

`api_contract` 的可选字段判定从真值判断改为 `is not None`，让「显式空值」有机会表达出来；
`handler` 侧对 `mode` 等字段同样以 `payload.get(...) is not None` 判定缺省，
因此空串（显式给值）与键缺失（走缺省）会得到不同结果。行为见 G2。

### 5. 视频摄入的 `occurred_at` 走 `submit_ingest` 参数

`occurred_at` 属于消息发生时间，因此扩展 `MemoryAPI.submit_ingest` 与 MCP 工具
`memory_submit_ingest` 的签名，由后台 `add` 原样接收。**不借 metadata 旁路传递**。

MCP 工具侧该参数注解为 `str | None`，与本仓既有约定一致：FastMCP 会把客户端 ISO
字符串 coerce 成 `datetime` 对象再进工具函数，而共享契约的 JSON 边界只接受 ISO 字符串。

### 6. 本次不重复 PR #358 覆盖的四处

`search.as_of`、`delete.mode`、`update.tier`、`evolve.channel` 由 PR #358 负责，本次不改，
避免两边同时修改同一函数体造成合并冲突。这四处在覆盖锁中以 `gap(...)` 显式登记。

> 进展：PR #358 已合入 mem2.0（e6805dd）。本特性分支合并后，四处透传来自 #358 的实现
> 与本分支的其余加固共存；覆盖锁中的 4 行 `gap(...)` 已按设计删除并登记为已支持。

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

1. **`revoke` 不传 `actions` 时仍只撤只读**：缺省语义保持向后兼容；若当初以 `actions: ["write"]`
   授权，撤销时须显式带上 `actions`。目标形态按 `grant_id` 精确定位（`GrantStore` 未实装，
   见 F05 §5.4）。
2. **`Grant.actions` 为显式空数组时，`PermissionManager` 是静默 no-op**：本特性只收紧了 legacy
   入口的解析（显式空值报错），HTTP 路径仍会把空集合传到 `grant()` 而不报错。该差异属于
   安全域接口边界问题，不在本特性范围内。
3. jiuwenSwarm 的参数适配按范围约束暂不处理。
