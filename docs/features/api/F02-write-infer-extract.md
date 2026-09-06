# F02 — write 路径演进策略重构 + infer 同步抽取开关

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-05 |
| 影响范围 | jiuwen_memory/control/engine_impl/in_memory_engine.py、jiuwen_memory/control/AGENTS.md、jiuwen_memory_entry/core/api_contract.py、docs/specs/S02-memory-api.md、docs/specs/S03-control.md、docs/features/construction/F01-construction-spec-design.md |
| 测试基线 | 历史 A-04 定向回归 15 passed；当时 `pytest tests/unit` 有 4 个既有失败：2 个因未安装可选依赖 `torch`，2 个因 EntityIndexBuilder logger 名与 caplog 监听名不一致，均与本特性无关 |
| Refs | — |

> 本文归档 **write 路径演进策略的两点变更**：(1) 默认路径不再自动提交 background EXTRACT；(2) 新增 `system_metadata["infer"]=="true"` 同步抽取开关。两者是一个连贯决策的两面——把"是否在写入时抽取"的选择权从"框架硬编码自动提交"交还给"调用方按场景显式选择"。

---

## 背景

记忆系统的写入路径（`MemoryEngine.write`）原本遵循 [`docs/features/construction/F01`](../construction/F01-construction-spec-design.md) 共享前提 1 定下的**双通道**立场：

> 写入路径只做 Classifier 分类 → KVStore 落盘 → IndexBuilder 建索引 → **提交后台 EXTRACT**（< 250ms）；提取/去重/冲突消解/遗忘全放 Background Evolver。

这一立场是为了保住写入时延（agent 等待），其代价是「写入的原始记忆先以 EPISODIC 入库、派生 SEMANTIC 事实要等后台 EXTRACT 跑完才出现」。在两条新诉求下，这个代价变得不可接受：

1. **外接记忆 provider 的同步语义**：`jiuwen_memory_adapter/JiwenSwarm/agent_memory_provider.py` 把本系统适配成 openjiuwen `MemoryProvider`，其 `sync_turn` 契约要求「写完即可被下一轮 `prefetch` 召回派生事实」——若 write 后派生事实要等 background EXTRACT（且 `InProcessScheduler` 当前是**同步执行**，见 F01 已知遗留 1）才出现，provider 的同步语义失效，agent 下一轮检索不到刚写入的事实。

2. **对齐 mem0 `add(infer=True)`**：mem0 的 `add` 支持 `infer=True` 在写入时同步抽取事实。本系统作为可替代 mem0 的独立记忆子系统，需要在 `write` 暴露等价开关，否则上层（如 `ExternalMemoryRail`）无法做语义对等迁移。

但**直接把 write 默认改成同步抽取**会重蹈 F01 拒绝方案 A 的覆辙（时延失控、写入路径脆弱）。于是本特性的核心取舍是：**默认仍不同步（保双通道立场），但提供一个显式 `infer=true` 开关让需要同步语义的调用方自行 opt-in**——把时延代价显式化、由调用方按场景承担。

与此同时，原有"write 后自动提交 background EXTRACT"的行为在默认路径下被移除：因为 (a) `InProcessScheduler` 同步执行使"自动提交"实质等于"同步阻塞"，与双通道立场的"异步"初衷相悖；(b) 大量调用方（SDK/CLI/examples）实际依赖的是"先 write 再显式 `evolve()`"或 `infer=true`，自动提交反而制造了不可控的后台 LLM 风暴。演进触发权交还给调用方显式 `evolve()` 或 `infer=true`。

## 决策

### 1. write 默认路径不再自动提交 background EXTRACT

`InMemoryEngine.write` 默认分支（`infer` 非真值）流程改为：

```
Engine 构造 RawPayload（含 assets）→ Ingestor.ingest（自行映射 assets）
→ Engine 补 tags 等编排字段 → Classifier.classify
→ IndexBuilder.build（统一交付 Storage + 构建 hot 索引）
→ 返回 units
```

末尾的 `Scheduler.submit(scope, EXTRACT, BACKGROUND)` **删除**。演进由调用方显式 `MemoryAPI.evolve(scope, EXTRACT)` 触发，或经 `infer=true` 同步走（见决策 2）。

**为何删而非保留**：`InProcessScheduler` 同步执行下"自动提交 background"是名不副实的——它并不异步，而是同步阻塞 write 直到 EXTRACT 的 LLM 调用跑完。这既没兑现双通道的时延承诺，又让 write 时延不可预测。在控制层换上真异步 Scheduler 之前（S03 范围的已知遗留），自动提交弊大于利，故移除。真异步 Scheduler 落地后，是否在默认路径恢复可选自动提交，另行决策（见已知遗留）。

### 2. `system_metadata["infer"]=="true"` 同步抽取开关

`write` 据 `system_metadata["infer"]` 真值（`str(...).strip().lower() == "true"`，大小写/空白不敏感）分两路：

- **`infer="true"`**：原始单元只落 KV 真源**不建索引**；hot path 同步调 `self._evolver.evolve(units, EvolveMode.EXTRACT)` 走 Evolver EXTRACT 全链路——`Extractor.extract` 抽取派生记忆 → `_dedup_batch` 判定+落盘+建索引（ADD/UPDATE/SUPERSEDE/NOOP）。**不提交** background EXTRACT（已同步抽取，避免重复）。返回**派生单元列表**（从 `EvolveResult.created_ids` 反查 KV 读回，对齐 mem0 `add(infer=True)` 返回派生事实）。
- **缺省 / 非 `"true"`**：决策 1 的默认路径——原始落盘 + 建索引，不自动提交演进。

**为何经 Evolver 而非独立 Extractor**：`infer=true` 仍必须走 Evolver 的 `_dedup_batch`，否则每轮写入都新增派生记忆、不去重，记忆迅速膨胀。Evolver 自带 extractor，engine 不重复注入独立 Extractor（避免双实例 + 双装配）。

**evolver 缺失显式报错**：`infer="true"` 但装配未注入 `Evolver`（`None`）时抛 `RuntimeError("Engine.write infer=True requires an Evolver")`——装配问题暴露而非静默降级。默认装配 `evolver: orchestrating` 总是注入，故仅非默认装配才可能触发。

### 3. HTTP / CLI 直接传入 API 元数据并保留原返回值

HTTP `/v1/add` 与 CLI `add` 不经过 legacy handler，而是通过共享
`core/api_contract.py` 调用 `MemoryAPI.add`：

- `system_metadata` 承载 `infer` / `procedural` 等内核解释字段，
  `user_metadata` 承载业务自定义字段；不接受混合 `metadata` 参数，拆分规则见 API F04。
- JSON 对象按同名参数传入，保留 API 支持的标量或字符串数组类型；不统一 string 化，
  API 写入边界仍校验类型和保留 key。
- `infer=true` 下可能合法返回空列表（本次没有新建的派生单元）。HTTP 返回
  `200 OK` 和 `[]`，CLI 的 JSON 输出也是 `[]`；不伪造 `item_id`，不额外添加
  `ok` / `op` / `skipped`。旧 envelope 只属于 MCP 等 legacy handler 调用方。

## 拒绝的方案

### 方案 A：write 默认永远同步抽取（推翻双通道）

**描述**：write 时默认就调 Extractor + Dedup，一步到位，不区分 infer 开关。

**拒绝原因**：
- 重蹈 [`F01` 拒绝方案 A](../construction/F01-construction-spec-design.md) 覆辙——一次 write 触发多次 LLM 调用（抽取 + 去重判定），agent 等待分钟级，写入时延失控
- LLM 不可用时 write 直接失败，而数据面本应始终可用——写入路径变脆弱
- 多数调用方（SDK/CLI/批量导入）不需要同步派生事实，强加同步抽取是普适性倒退

> 故本特性只把同步抽取作**可选开关**，默认立场不变。这与 F01 双通道立场不冲突——F01 反对的是"默认同步"，infer 开关是"显式 opt-in 同步"。

### 方案 B：infer 走独立 Extractor，不经 Evolver

**描述**：engine 注入独立 `Extractor`，`infer=true` 时直接 `extractor.extract(units)` 落盘，不走 Evolver `_dedup_batch`。

**拒绝原因**：
- 绕过去重——每轮 sync_turn 都新增派生记忆，重复事实堆积，记忆膨胀失控
- 双实例 Extractor（engine 一个 + Evolver 一个）装配冗余，且两处 extractor 配置可能不一致
- 失去 Evolver 的 ADD/UPDATE/SUPERSEDE/NOOP 决策——无法把"新事实 vs 补充已有 vs 取代旧版"分流

### 方案 C：infer 同步抽取 + 仍提交 background EXTRACT

**描述**：`infer=true` 同步走 EXTRACT 后，照旧 `Scheduler.submit(scope, EXTRACT, BACKGROUND)`。

**拒绝原因**：
- 重复抽取——同步 EXTRACT 已抽取并落盘派生记忆，background 再扫一遍原始记忆做同样抽取，纯重复 LLM 风暴
- dedup 虽能最终把重复派生判 NOOP，但每次都要付出全量召回 + LLM 判定代价，与"抑制每轮 EXTRACT 风暴"的初衷相悖

## 验证

### 单元测试

- `tests/unit/construction/test_evolver_dedup.py` — 13 passed：去重四态（ADD/UPDATE/SUPERSEDE/NOOP）+ 降级场景 + 高相似实质差异改走 LLM。其中 supersede/update/json-fallback 三例用 `dedup_high_similarity=1.01` 抬高短路阈值，强制走 LLM 判定分支验证（见 [construction F01](../construction/F01-construction-spec-design.md) 测试基线）
- `tests/unit/construction/test_extractor.py` — 14 passed：含 `test_extract_batch` 批量提取一次调用返回全部候选、source_id 回指正确源 unit
- `tests/unit/api` — 全绿：write 路径 + 装配

### 端到端验证

- `jiuwen_memory_adapter/JiwenSwarm/_e2e_real.py` — provider ↔ 服务(8137) 全链路：conclude / sync_turn(infer=true) / on_session_end / prefetch / search / profile
- `examples/quickstart*.py` — add → search → get → update → evolve 全链路

### 关键场景验证

| 场景 | 验证方式 | 结果 |
|------|---------|------|
| infer=true 同步抽取可被下一轮召回 | provider `sync_turn` 后 `prefetch` 命中派生事实 | ✅ |
| infer=true 全 dedup 合法返空 | API 返回 `[]`；HTTP 为 `200` + `[]`，CLI JSON 为 `[]`，不添加业务包装 | ✅ |
| 默认 write 不触发后台 EXTRACT | write 后无 background 任务、时延不被 LLM 拖累 | ✅ |
| evolver 缺失 + infer=true | 抛 RuntimeError（装配问题暴露） | ✅ |

## 已知遗留

1. **默认路径不再自动提交 background EXTRACT** 是基于 `InProcessScheduler` 同步执行现状的取舍。待控制层换上真异步 Scheduler（S03 范围）后，可重新评估"默认路径可选自动提交 background EXTRACT"——届时异步提交不再阻塞 write，双通道立场的原始设计可恢复。本特性不预先实现，留给那次决策。

2. **infer=true 同步抽取的时延** 仍受 Extractor LLM 调用拖累（派生 + 去重判定各一次 LLM）。对时延敏感的调用方应慎用 infer=true 或配 `extractor: keyword`（规则切分、~0 LLM）兜底。

3. **metadata 暂无中央 schema**：写入边界已约束可接受值类型和系统保留 key，但不维护
   业务 key → 类型的全局声明。同一 key 在不同记忆间的类型稳定性仍由调用方负责。

---

## 增量更新（2026-07）

> 以下章节在原 F02 基础上叠加，记录 infer 同步抽取落地后的演进：KV key 前缀分离、infer 上下文增强抽取（原文做指代消解、召回记忆做去重）、过程记忆抽取、write 去 classify、`/v1/list` 收窄、provider 新增过程记忆工具。

### 决策6：KV key 前缀分离（/messages/ vs /memory/）

unit 真源在 KVStore 里的 key 由裸 `{unit.id}` 改为带前缀，按「是否建索引」二分：

- `/messages/{id}` — 未建索引的 infer=true 原始消息（engine infer 分支落盘、不调 `IndexBuilder.build`）。拉取做指代消解/语境时用 `scan(scope, prefix="/messages/")` 直取。
- `/memory/{id}` — 所有建索引的记忆：infer=true 派生（evolver 落盘 + 建索引）+ 默认路径原文（engine 默认分支落盘 + 建索引）+ update SUPERSEDE 新版 + 过程记忆。dedup/retrieve 回查命中必在此前缀下。

前缀常量与 helper 下沉到结构定义层（与 MemoryUnit/RawPayload 同处）：
- `common.type_def.memory`：`MEMORY_KEY_PREFIX`、`memory_key(unit_id)`
- `common.type_def.raw`：`MESSAGES_KEY_PREFIX`、`messages_key(unit_id)`
- 既有 `/index/chunks/{id}`（vector_index_builder chunk 簿记）维持不变，同款前缀风格，互不冲突。

**适配点**（全库扫查）：
- 落盘：engine.write（infer→`/messages/`、默认→`/memory/`）、evolver `_persist`/`_apply_decision`/FORGET（派生→`/memory/`）、engine.update（OVERWRITE/SUPERSEDE→`/memory/`）。
- 回查：engine `_load`/`_list_units`、dedup `_load_unit`、unit_reader `load`、governor `_find`（只查 `/memory/{id}`，inspect 语义是建索引记忆）。
- 按 key 匹配 id：lifecycle transition/supersede 改用带前缀 key 直接比对（`memory_key(unit_id)` 构造 dst_key，`scan(scope, MEMORY_KEY_PREFIX)` 限定）。
- delete 扫描保持全扫（按 `unit.id` 匹配 raw 内 unit，不依赖 key 前缀；PURGE/DOWNWEIGHT 回写用扫描到的带前缀原 key）。
- scheduler background EXTRACT 的 `kv.scan(scope, prefix=MEMORY_KEY_PREFIX)` 只扫 `/memory/`（loads 过滤非 unit，喂 evolver 的是建索引记忆；`/messages/` 的 infer 原文已同步抽取，background 不重扫）。

### 决策7：infer=true 上下文增强抽取（原文做指代消解、召回记忆做去重）

infer=true 同步抽取时，**evolver 内部**收集两类上下文参考项（evolve 接口签名不变，仍 `evolve(units, mode)`）：

- `recent_originals`：最近 10 条 infer=true 原始消息（MemoryUnit，落 `/messages/`）。**用于指代/代词消解与语境丰富**——让 extractor 理解"它/他/那个"指什么、对话背景。只拼进 extractor prompt，**不参与去重**。
- `related_memories`：用 `dedup.recall` 召回的 10 条相关记忆（MemoryUnit，落 `/memory/`）。**用于去重**——拼进 extractor prompt 告知大模型已有这些记忆、不要再抽重复的。

两类参考项都经 `ExtractContext`（`construction.base`）承载，只作 prompt 参考，**不进 `extract()` 的提取来源列表**（本轮 units 是唯一提取来源）。

**去重两层**（不再在 evolver 做产出后向量过滤）：
1. prompt 提示：llm_extractor 的 user prompt 追加 `## Existing related memories (do NOT duplicate information these cover)` 段，列出 related_memories 的 content。
2. `_dedup_batch` 兜底：extractor 产出的候选仍经现有去重链（向量召回 + LLM 判定 ADD/UPDATE/SUPERSEDE/NOOP）落盘。

`recent_originals` 不参与去重——原文与派生事实语义粒度不同，按相似度判重复易误删（如原文"我喜欢猫"和派生"用户喜欢猫"）。原文"防重复"由 extractor"只从本轮提取"的语义 + `_dedup_batch` 全库向量去重兜底覆盖。

**收集逻辑放 evolver**（非 engine）：`_maybe_collect_extract_context` 检测 `system_metadata["infer"]=="true"` → `_recent_infer_originals`（`scan(/messages/)` + 按 `t_ingest` 降序取 10，排除本轮自身）+ `_related_memories`（`dedup.recall` 召回，复用去重向量空间，返回完整 MemoryUnit）。任一步失败降级为空列表，不阻断。

**extracted 为空跳过 dedup**：`extractor.extract` 返回空时，evolver 直接返回空 `EvolveResult()`，不调 `_dedup_batch`（空列表走去重无意义、省一次召回）。CONSOLIDATE 同理。

### 决策8：过程记忆抽取（procedural=true）

新增独立于 infer 的调用级开关 `system_metadata["procedural"]=="true"`，触发**过程记忆抽取**：

- 原文**不落 KV**（不进 `/messages/` 也不进 `/memory/`）。
- evolver EXTRACT 分支检测 procedural → **跳过 context 收集**（不检索 10 条、不拉 10 条）、**跳过 `_dedup_batch`**，extractor 产 1 条 PROCEDURAL 执行历史直接 `_persist` 落 `/memory/` 建索引。
- extractor（llm_extractor）`_extract_procedural`：新增 `_PROCEDURAL_SYSTEM_PROMPT`，要求 LLM 把本轮汇总成 **1 条** 结构化执行历史（目标/步骤/结果），provenance 回指全部本轮 unit，tier=PROCEDURAL。keyword_extractor 降级：无 LLM，把原文原样合成 1 条 PROCEDURAL（仍保证"1 条过程记忆"契约）。

procedural 与 infer 互斥：procedural=true 时 even 不走 infer 的原文落 `/messages/`、不收集 context、不去重。语义是"把这轮做了什么记成一条可检索的 how-to"。

### 决策9：engine.write 调用 classify（infer=false 路径）+ Classifier 重写为纯 LLM tier+tags

**修订（2026-07）**：决策9 原为"engine.write 不再调 classify"，现修订为 **infer=false 默认路径调 classify**：

- `engine.write` 默认路径（infer=false）调 `classifier.classify(units)` 给原文打 tier+tags → 落 `/memory/{id}` + 建索引。
- `engine.write` infer=true 路径不经 classifier（extractor 产派生时自定 tier+tags）。
- `engine.write` procedural=true 路径不经 classifier（tier 固定 PROCEDURAL）。
- `InMemoryEngine.__init__` 加回 `classifier: Classifier | None = None` 可选参数；`_build` 经 `_opt_classifier` 按 config 注入（命名空间有 default 才注入，None 时跳过向后兼容）。
- `defaults.py` classifier default 改 `llm`（原 keyword）。

**Classifier 重写为纯 LLM tier+tags**（`llm_classifier.py`）：
- 去掉五维分类（topic/importance/confidence/freshness）+ 规则通道 + FeatureExtractor 依赖。
- 单次 LLM 调用产出每条 `tier`（episodic/semantic/procedural，非法兜底 EPISODIC）+ `tags`（1-3 个，清洗截断），prompt 与 extractor 的 tier/tags 抽取口径对齐。
- LLM 不可用/解析失败降级空 tags + EPISODIC，不阻断。

**tier+tags 产出路径分工**：
- infer=false → Classifier（LLMClassifier）给原文打。
- infer=true → Extractor 在派生时一并产出（不经 classifier）。
- procedural=true → tier 固定 PROCEDURAL。
- `examples/demo_classifier.py` 改为自行 `ClassifierProducer.dep` 装配实例验证分类本身。

### 决策10：/v1/list 收窄到 /memory/ 全部

历史 handler `_list` 的返回范围由全扫 `kv.scan(scope)` + loads 过滤，收窄为只返 `/memory/` 下的建索引 Memory 记忆，不再返 `/messages/` 下的 infer 原文（未建索引）和 `/index/chunks/` 簿记。

后续 F01 的 list 决策已将 `list` 上收为正式
`MemoryAPI.list -> MemoryEngine.list -> KVStore.list` 数据面接口。当前 HTTP / CLI
直接调用 API，MCP 等 legacy handler 的 `_list` 也委托该 API，不再直连 KV。`KVStore.list` 只查询 `/memory/`，并在存储适配器内完成过滤、精确计数、
稳定排序和分页。

本仓库 provider 的 InProcess `list_semantic` 当前调用 `MemoryAPI.list`，不按
`tier==SEMANTIC` 过滤，再将结果转为 provider 自身的 `{content, item_id, score}`。
HTTP / CLI 则直接序列化 `MemoryListResult`（`items` 和 `count`），其中 unit 使用原字段，
不经 `_unit_view`。provider 的远程客户端仍使用旧 flat payload / envelope，尚未适配
当前 HTTP，不能把旧 provider 的返回协议当成当前服务端协议。

### 决策11：provider 新增 agent_memory_procedural 工具

`jiuwen_memory_adapter/jiuwenswarm/agent_memory_provider.py` 新增第 4 个工具 `agent_memory_procedural`：

- `PROCEDURAL_SCHEMA`：参数 `content`（要汇总的本轮内容），description 说明"汇总成 1 条 procedural 记录、原文不存、不去重不检索"。
- `handle_tool_call` 分支：调 `self._client.add(content, scope, system_metadata={"procedural": "true"})` → 经 engine procedural 分支。返回 `{result, item_id}`。
- `get_tool_schemas` 加入它；`system_prompt_block` 补引导语。

当前 InProcess 工具通过 `add_async(..., system_metadata={"procedural": "true"})` 触发
procedural 分支。直接调用 HTTP `/v1/add` 时传同名 `system_metadata` 也具有该语义，
但本仓库 provider 远程客户端尚待适配新 HTTP 契约。需配 `extractor:llm` 才真汇总（默认 keyword 降级为原文原样存 1 条 PROCEDURAL）。

### 决策12：`infer=true + middle=true` 中期缓冲子路径

`add` 的 `infer=true` 分支下按 `middle` 二级开关再分流。`middle=true` 触发中期缓冲子路径，落地细节见 [`F06-middle-term-memory`](../control/F06-middle-term-memory.md)，这里只列与 write 路径决策相关的部分：

- 原文落 `/memory/{id}`（与建索引记忆同前缀，不走 `/messages/`）+ 建索引（原文立即可检索）+ 打 `tier=WORKING` 与 `system_metadata["middle"]="true"` 标记。
- 提交 `MiddleToLongJob` 给 Scheduler——`interval=self._middle_interval`（编排周期，属 Engine 编排职责，故留 Engine 而非 JobFactory）。Scheduler 把它注册到 per scope TimerWheel，Timer 协程周期生成实例入队，每个实例跑一次 `run()` 即返回。
- MiddleToLongJob 内做：list 候选（`tier=WORKING + lifecycle=ACTIVE + system_metadata["middle"]="true"`）→ 连续性检测切批 → `evolver.evolve(batch, EXTRACT)` → 原文归档（`lifecycle.transition(ARCHIVED) + index.remove`）。

**为何 middle 是 infer 的二级开关**：middle 路径要原文立即可检索（落 `/memory/` + 建索引），与 infer=true 同步抽取语义冲突（infer 原文不建索引、走 `/messages/`）。故 middle=true 必须在 infer=true 下生效，且走自己的子分支——分支内不再调 infer 的同步抽取，原文只落 KV 不抽取，抽取由后台 MiddleToLongJob 周期触发。

**为何不与 procedural 合并**：procedural=true 原文不落 KV（直接喂 extractor 产 1 条 PROCEDURAL），middle=true 原文必须落 KV 建索引（要可检索 + 可归档）。两者原文处置方式互斥，不可合并到一个开关。

`CloudEngine._write_middle_path` 多 profile 适配：按 `message_type` 选 binding，通过 `JobFactory.get_job` 的运行时覆盖入参 `evolver=` / `index=` 注入 binding 的（替代早期 `job._evolver = evolver` 直接赋值方案），保证 Job 内部的 evolver/index 与原文落盘时一致。详见 [`F06`](../control/F06-middle-term-memory.md) 决策 4。

## 增量测试基线

`pytest tests/unit tests/integration` 全绿（378 passed, 54 skipped）。新增/适配：
- `tests/unit/construction/test_infer_context_extract.py`（10 个）：infer context 收集、related_memories 经 prompt 去重 + `_dedup_batch` 兜底、原文不参与去重、procedural 产 1 条 PROCEDURAL 且原文不落 KV、engine infer 原文落 `/messages/`。
- `tests/unit/construction/test_evolver_dedup.py`（13 个）：内联 KV helper 适配 `memory_key` 前缀；mock extractor 签名加 `context` 参数。
- `tests/unit/construction/test_e2e_evolution.py`：原 `test_write_classify_and_recall`/`test_write_sets_tier_by_classifier`（测 write 路径分类）合并为 `test_write_recall_returns_written_unit`（write 不再 classify，tier=EPISODIC）。
- `tests/unit/control/test_lifecycle_manager.py`、`test_governance.py`、`test_engine_*`、`tests/conftest.py`、`tests/integration/retrieval/test_index_contract.py`：KV setup 适配 `memory_key` 前缀。

## 增量已知遗留

4. **procedural 与 infer 不可同时生效**：engine write 的 `procedural or infer` 共用同步抽取路径，procedural=true 优先（原文不落、跳 context/dedup）。若调用方同时传 procedural=true 和 infer=true，按 procedural 语义处理（原文不落 `/messages/`）。若需"原文留存 + 过程汇总"，应分两次 write。

5. **`_related_memories` 用 dedup.recall 召回**：复用去重向量空间，但 dedup.recall 的 `min_similarity`/`tier_filter` 配置影响召回质量。infer 场景本轮是 EPISODIC、相关记忆是 SEMANTIC——若 `tier_filter=True` 会跨 tier 失配。装配默认 `tier_filter=False`（允许跨层去重），故 infer context 召回正常；改 `tier_filter=True` 时需复核。

6. **拉原文性能**：`scan(scope, prefix="/messages/")` 只取未建索引原文，但仍需 loads 全部原文 + 按 `t_ingest` 排序取前 10。scope 内原文量大时有开销。当前同步 infer 场景（已接受 LLM 时延）且原文量远小于全库 unit 量，可忽略。若成瓶颈，可维护最近 N 条原文的 ring buffer。
