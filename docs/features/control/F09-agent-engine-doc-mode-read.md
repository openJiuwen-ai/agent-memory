# F09 — AgentEngine 文档模式读路径经 DomainStore + lifecycle 绕过

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-14 |
| 影响范围 | `jiuwen_memory/control/engine_impl/agent_engine.py`（构造注入 domain_store + 读路径 + update/delete lifecycle 绕过 + evolve 传 domain_store + `_build` 注入）、`jiuwen_memory/control/engine_impl/doc_mode_support.py`（新增共用件）、`jiuwen_memory/control/engine_impl/cloud_engine.py`（提取共用件、行为不变）、`tests/unit/control/test_agent_engine.py`（新增）、`jiuwen_memory/control/AGENTS.md`（铁律1 + AgentEngine 描述）、`jiuwen_memory/storage/AGENTS.md`（本地约束17） |
| 测试基线 | `pytest tests/unit/control/test_agent_engine.py` 10 绿；`pytest tests/unit/control/` 全绿（5 个 e2e skip 需外部 LLM/Redis，与本次无关）；`ruff check` clean |
| Refs | 待确认（见提交约定） |

---

## 背景

AgentEngine（`engine_impl/agent_engine.py`）的 agent 场景配置显式开启文档模式（`write_document=true`，
真源是 markdown + 影子索引，**不写 KV**——KV 仅保留为 vector chunk_tracking）。但
AgentEngine 照搬 InMemoryEngine 的 KV 直读路径，没接上 `CompositeDomainStore` 早已实现
的文档模式 shadow 分流（F08 §5.3：`get`→`shadow.get_units` line126、`list`→
`shadow.list_units` line127、`scopes`→`shadow.scopes`）。文档模式下 KV 不持有
MemoryUnit，导致多条路径断口：

| 路径 | 断点（修复前） | 后果 |
|---|---|---|
| `list`/`get`/`_load`/`_list_units`/`scopes()` | 直连 `self._kv` 读空 | list 返空、get 抛 NotFoundError |
| `delete`/`purge_space` 候选扫描 | `kv.scopes()` + `_list_units` 读空 | 静默漏删 |
| `update` SUPERSEDE | `lifecycle.supersede` 读 KV 空 | NotFoundError |
| `delete` FORGET/ARCHIVE | `lifecycle.transition` 读 KV 空 | lifecycle 不变但 `remove(SOFT)` 退索引→真源与索引不一致 |
| `evolve` | 传 `kv=self._kv` 给 resolver | resolver 走 KV 分支读空候选 |
| `sweep_expired` | `lifecycle.sweep()` 读 KV 空 | 静默失效（**本次遗留**） |

`CloudEngine` 已正确经 DomainStore 读取 + 文档模式 SUPERSEDE 绕过 `lifecycle.supersede`
（`cloud_engine.py` 的 SUPERSEDE 分支）。唯独 AgentEngine 没接——`_build` 没注入
domain_store，照搬了 InMemoryEngine 的 KV 直读 / lifecycle 直调。根因见记忆
`doc-mode-supersede-kv-lifecycle-break`：`KVLifecycleManager` 的 supersede/transition/
sweep 都从 KVStore 取 unit，文档模式下走它必 NotFoundError / 静默失效。（该根因已由
issue #261 根治——`KVLifecycleManager` 注入 `domain_store` 后按 `should_write_document()`
文档分流，见「方案 A（后采纳）」。）

## 决策

### 决策 1：AgentEngine 持有 domain_store，读路径对齐 CloudEngine

构造函数新增 `domain_store: DomainStore` 必填参数（retriever 之后），存
`self._domain_store`。`list`/`get`/`_load`/`_list_units`/`candidate_scopes`/
`scopes()` 调用点改走 `self._domain_store.*`——文档模式分流到 shadow、非文档模式
分流到 KV（DomainStore 内部按 `should_write_document` 互斥分支）。`_build` 经
`manager.domain_store(resolve_name(config, "domain_store"))` 注入，对齐 CloudEngine
装配。保留 `kv: KVStore`（dreaming 注册表经 `engine.kv`，注册表 key 非 MemoryUnit）。

### 决策 2：update SUPERSEDE / delete transition 文档模式绕过 KV lifecycle

文档模式下 `KVLifecycleManager.supersede/transition` 从 KV 取 unit 必失败，根因是
lifecycle 数据源与文档真源（shadow）不一致。本次选**engine 侧绕过**（对齐 CloudEngine
SUPERSEDE 范式），不选 lifecycle 数据源根治（见「拒绝的方案」）；该根治随后由
issue #261 落地（见「方案 A（后采纳）」），engine 侧绕过仍保留：

- **update SUPERSEDE**：`should_write_document()` 为真时，先 `restore_coords_from_md_filename`
  防 coords 丢失致新版落错 md 文件（见决策 3），再 `index.build([new])` 落新版，最后直接
  `old.lifecycle = SUPERSEDED; old.temporal.t_invalid = ...; index.update([old])` 落 shadow——
  不调 `lifecycle.supersede`。非文档模式维持 `lifecycle.supersede` 路径。
- **delete FORGET/ARCHIVE**：`should_write_document()` 为真时，直接
  `unit.lifecycle = target; index.update([...])` 落 shadow——不调 `lifecycle.transition`。
  DOWNWEIGHT 不改 lifecycle（上方已单独处理），不受影响。非文档模式维持 `lifecycle.transition`。
  `index.update→domain.update` 文档分支按「md 可见性」删块（#261 方案 C，见
  [F08](../../storage/F08-document-memory.md)「写入路径分流」update 行）：`FORGOTTEN`
  物理删 md 块（遗忘=从文档消失），`ARCHIVED` 保留 md 块（转冷可回溯），`SUPERSEDED` 维持
  已删；sweep FORGOTTEN（经 `lifecycle.transition` 回写真源 → `domain.update`）同口径统一删块。
- 绕过不校验 `_ensure_transition_allowed`（对齐 CloudEngine SUPERSEDE 绕过不校验的简化风格）。

### 决策 3：提取 `restore_coords_from_md_filename` 到共用件

`cloud_engine.py` 原有 `_restore_coords_from_md_filename`（文档模式 supersede 防 coords
丢失）。AgentEngine SUPERSEDE 绕过需要同一逻辑，提取到新文件 `doc_mode_support.py`
（纯函数，依赖 `COORDS_KEY`/`MD_FILENAME_KEY` 常量）。CloudEngine 与 AgentEngine 都 import
它，消除复制分叉。行为不变。

背景：old 经 `shadow.get_units` 读回，`coords` 是 TRANSIENT 键（`dumps` 剥除），读回的
old 无 coords → deepcopy 的 new 也无 → `domain_store.add` 内 `_md_path._project_of` 读
coords 落空 → md_filename 重算成 `memory/MEMORY.md`（memory 根下，跨项目可见），新版与旧版不同文件、project 维丢失。
`md_filename` 是落盘键（非 TRANSIENT，读回保留），从 old 的 `md_filename`
（`memory/{project}/MEMORY.md` 或 `.../daily_memory/...`，`USER.md` 落 root 顶层无 project 段
不回写）反推 project 回写进 new 的 coords。

### 决策 4：evolve 传 domain_store

`evolve` 把 `kv=self._kv` 改为 `kv=self._domain_store`。`candidate_resolver` 已做
KV/DomainStore 双适配（`CandidateStore = KVStore | DomainStore`，`_is_kv_store` 判别），
传 domain_store 后 resolver 走 DomainStore 分支，文档模式下正确读 shadow 候选。EvolveJob
本身不改（resolver 由 `submit_evolve` 经 `build_resolver` 装配并注入，EvolveJob 的 `self._kv`
仅是 resolver=None 时的兜底，本路径不触达）。

## 拒绝的方案

### 方案 A（当时未选，后由 issue #261 采纳）：lifecycle 数据源根治——KVLifecycleManager 注入 DomainStore

让 `KVLifecycleManager.supersede/transition/sweep` 改从 DomainStore 取 unit，文档模式
下自动分流到 shadow。**未选原因**：

1. `KVLifecycleManager` 是 control 面共用件（InMemoryEngine / CloudEngine / AgentEngine
   三引擎共用），改它的数据源牵动三引擎 + sweep_expired，blast radius 远大于本次修复
   （本次只修 AgentEngine）；
2. CloudEngine 的 SUPERSEDE 早已选 engine 侧绕过（不调 lifecycle.supersede），说明
   「engine 侧绕过」已是既定范式——AgentEngine 对齐它即可，无需另起 lifecycle 改造；
3. sweep 仍读 KV 是共性问题（CloudEngine 同样未修），lifecycle 根治需连带处理 sweep
   数据源，本次明确列为遗留（见「已知遗留」），不在本次 scope 内。

> **后记（issue #261）**：本方案已落地。`KVLifecycleManager` 注入 `domain_store` 后，
> `transition`/`sweep`/`supersede` 改经 `_load_units`/`_write_units` 按
> `should_write_document()` 文档分流——文档模式经 DomainStore（shadow）、非文档模式经
> KV。见 `kv_lifecycle_manager.py` 模块 docstring 与「已知遗留」#1 的标注。

### 方案 B（未选）：AgentEngine 不持有 domain_store，read 时按 should_write_document 判别走 KV 或 shadow

在 AgentEngine 内部判别 `should_write_document` 然后直连 shadow/kv。**未选原因**：
AgentEngine 不应直连 shadow 端口（绕过 DomainStore 数据面抽象，违反 S03「Engine 经
DomainStore 读取」对 CloudEngine 已确立的范式），且会复制 DomainStore 已有的分流逻辑
（list/get/scopes 的文档分支），制造第二套分叉。注入 domain_store 是零成本复用既有
分流实现。

## 验证

- `tests/unit/control/test_agent_engine.py`（AgentEngine 此前零测试覆盖）10 例：
  - 文档模式 write→list/get 命中、`_list_units`/`candidate_scopes` 非空、get 缺失抛
    NotFoundError；
  - update SUPERSEDE 不抛 NotFoundError + 新版落同 md 文件（coords dict 传 project 逼出
    反推路径）+ 旧版 lifecycle SUPERSEDED 落 shadow；
  - update OVERWRITE 同 id 落 shadow；
  - delete FORGET/ARCHIVE lifecycle 改 + 真源一致、delete by tags 命中；
  - evolve 把 domain_store 传给 resolver，候选经 DomainStore.list 读 shadow 非空；
  - 非文档模式回归 list/get 走 KV。
- `_AssertingLifecycle`（supersede/transition 调到即断言失败）证伪：文档模式下
  update/delete 确实绕过 lifecycle，不误调 KVLifecycleManager。
- `pytest tests/unit/control/` 全绿（InMemoryEngine / CloudEngine 回归不受影响）。
- `ruff check` clean（含 `doc_mode_support.py` 提取后的 import）。

## 已知遗留

1. **`sweep_expired` 仍读 KV**（已由 issue #261 修复）：`lifecycle.sweep()` 纯计算从 KV 读全量，文档模式下失效。
   根治需 lifecycle.sweep 数据源改造（注入 DomainStore 或 engine 侧重写 sweep 计算）。
   CloudEngine 同样未修——列为跨 engine follow-up，不在本次 scope。
   `KVLifecycleManager.sweep` 现按 `should_write_document()` 分流（文档模式走
   `DomainStore.scopes()`/`list`，非文档模式走 KV `scopes()`/`list_units`）。
2. **CloudEngine delete/sweep 共性遗留**：CloudEngine `delete`（非 SUPERSEDE 的 transition
   分支）/ `sweep` 也直调 lifecycle 未绕过，本次只修 AgentEngine。CloudEngine 共性问题
   在此记录为跨 engine follow-up。
3. **MiddleToLongJob `_list_working_units` 裸 KV**：AgentEngine 无 middle 路径不影响；
   InMemoryEngine/CloudEngine 文档模式下受影响，本次不修。
4. **非文档模式 lifecycle 未变**：非文档模式（KV 即真源）维持 `lifecycle.supersede/transition`
   直调——KV 持有 MemoryUnit，lifecycle 数据源与真源一致，无断点。本次绕过仅文档模式触发。
