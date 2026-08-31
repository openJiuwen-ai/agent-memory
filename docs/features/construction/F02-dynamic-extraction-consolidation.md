# 动态抽取与三步编排巩固

## 元信息
| 项 | 值 |
|---|---|
| 日期 | 2026-07-25 |
| 影响范围 | jiuwen_memory/construction/，jiuwen_memory/control/，jiuwen_memory/config/，docs/specs/S05-construction.md |
| 测试基线 | 见“验证” |
| Refs | — |

## 背景

旧 `llm_extractor` 把 fact/event/preference/context 固化在单个 system prompt 中，调用方
无法按业务场景增加新的抽取策略。与此同时，候选落盘前的相似召回、ADD/UPDATE/
SUPERSEDE/NOOP 判定和写入动作长期藏在 Evolver 内，职责上属于隐式 consolidation，
却没有独立接口，也无法按本次 write 传入策略。

第一版方案引入了独立的 `Consolidator` 接口，
把判定 + 落盘合并成一个 `consolidate(candidates) -> EvolveResult` 调用。落地后发现：


- prompt 文本内联在 metadata 里，每次 write 都要把整段 prompt 序列化进 MemoryUnit，
  既冗长又会随派生候选一直落盘；
- 判定与落盘耦合，无法在落盘前插入"反思"步骤。

## 决策

1. 保留旧 Extractor 实现，新增 `dynamic_llm`。动态实现从 write metadata 读取任意
   `_extract_prompt_<strategy>`；`infer=true` 仍是抽取开关，每个策略执行一次 LLM。
   metadata 中 `_extract_prompt_<strategy>` 的值是引用 yml `prompts.extract` 段的 prompt
   **key**，运行时由 `PromptRegistry` 按 `phase=extract + key` 查真实文本作为 system prompt
   发送（registry 未配置或 key 缺失时回退把值本身当文本用，兼容内联文本）；候选记录
   `_extraction_strategy`。
2. `DynamicLLMExtractor` 采用模板方法：基类统一处理策略遍历、LLM 调用、fallback、
   策略标记和 consolidation/reflect prompt key 透传；默认 `parse_response` 按 JSON 解析，
   子类覆盖该方法解析 XML 或其它格式并转换为 `list[MemoryUnit]`。格式相关中间结构只
   存在于子类内部，不改变 Extractor 对编排器的统一输出契约。
3. **移除 `Consolidator` 接口与 `consolidation_impl/`**。判定 + 落盘合并的接口被拆开：
   - 判定（ADD/UPDATE/SUPERSEDE/NOOP）归 `DynamicEvolver._consolidate_step`，只产出 `ConsolidateDecision`，不调 KVStore / IndexBuilder；
   - 落盘延后到 reflect 步之后统一执行。
4. **新增 `DynamicEvolver`**（`evolver_impl/dynamic_evolver.py`），继承 `OrchestratingEvolver`，覆盖 `_evolve_extract` 走四步：`extract → consolidate(判定) → reflect → 落盘`。与 `OrchestratingEvolver` 平级（同属 `evolver` 顶层命名空间，注册名 `dynamic` / `orchestrating`）；其余三模式（CONSOLIDATE/ASSOCIATE/FORGET）继承父类。装配或 pipeline profile 选哪个 evolver 实例即启用哪条 EXTRACT 路径——不再需要 `evolver_mode` 开关或 `memory_operator` 注入。
5. **consolidate 只判定不落盘**：consolidate 步对每个候选调 `Dedup.recall` 召回已有
   记忆，按相似度阈值 + LLM 判定产出 `ConsolidateDecision`。无命中 → ADD；高相似度
   （≥ `dedup_high_similarity`）→ NOOP；中段 → 查 `PromptRegistry` 取 consolidate prompt
   调 LLM 判定；无 prompt 或 LLM 失败 → 回退规则。
6. **reflect 默认 no-op**：基类 `_reflect_step` 直接返回候选；子类可覆盖以在落盘前做
   反思修正。这一步为后续"反思型记忆"扩展预留接入点。
7. **prompt 配置化**：新增 yml 顶层 `prompts` 段（按 phase → key → 文本），由
   `PromptRegistry` 在装配期加载。metadata 只写 prompt 的 **key**（引用 yml 命名 prompt），
   运行时按 `phase + key` 查真实文本。四步共享同一 `PromptRegistry` 实例。
8. **`OrchestratingEvolver.evolve` 拆分**：把 EXTRACT/CONSOLIDATE/ASSOCIATE/FORGET 四个分支
   抽成 `_evolve_extract` / `_evolve_consolidate` / `_evolve_associate` / `_evolve_forget`
   四个可覆盖方法，`evolve` 只做分派。`DynamicEvolver` 覆盖 `_evolve_extract` 即切换
   EXTRACT 路径，无需 `evolver_mode` 开关或外部编排器注入。
9. **`InMemoryEngine` 默认直写**：移除 `consolidator` 注入与默认路径里的 consolidate 调用，
   还原"原文落 /memory/ + 建索引"的直写语义。去重交给显式 `evolve()` 触发（装配选
   `orchestrating` 或 `dynamic` evolver 均可）。
10. metadata 是 `dict[str, str]`。调用方使用赋值或 `update` 传 prompt key；不新增
    `metadata.append()` 这种与 Python 字典不兼容的 API。

## 拒绝的方案

- **直接修改旧 `llm_extractor` prompt**：仍会把策略固化在代码中，也会破坏已有配置，
  因此保留旧实现并新增路由实现。
- **把 Dedup 一并合入编排器**：召回后端选择与业务决策是两个变化轴，合并后
  难以在 vector/fulltext 间复用，继续保持 Dedup 纯召回。
- **在基类中枚举 JSON/XML 等格式**：格式会持续扩展，集中分支会让内核依赖业务协议；
  因此响应格式由 prompt 自身约定（prompt 文本写在 yml `prompts.extract` 段，metadata 只
  写 key），子类只覆盖解析逻辑，基类只约束最终产出 `list[MemoryUnit]`。
- **修改 Extractor 返回类型以暴露解析结果**：会把 XML 节点、JSON 字典等格式细节传播
  到编排器，破坏统一编排契约，因此中间结构必须在子类内转换完毕。
- **Engine 内直接调用 LLM**：会破坏控制层只编排的边界；Engine 只委托构建层算子。
- **让 `DynamicEvolver` 继承 `Extractor`**：Extractor 有"不落盘"铁律，而 `DynamicEvolver`
  必须落盘；继承会让两条铁律互相冲突。改为继承 `OrchestratingEvolver`（Evolver 子类），
  复用非 EXTRACT 全部逻辑，只覆盖 `_evolve_extract`。
- **把 dynamic 逻辑做成被 `OrchestratingEvolver` 持有的独立编排器**（`DynamicMemoryOperator`
  + 独立顶层段 + `evolver_mode` 开关）：新老不对等——老 dedup 内联在 Evolver 内，新的跑到
  Evolver 外被 Evolver 调用，突兀。改为 `DynamicEvolver` 与 `OrchestratingEvolver` 平级，
  都是 `evolver` 段下的具名 Evolver 实现。
- **consolidate 步直接落盘**：会把判定与副作用耦合，无法在中间插入 reflect 步，也
  让单批候选间的"先看全部判定再决定"难以实现。改为只产出 decision，落盘延后。
- **prompt 文本继续内联在 metadata**：每次 write 都要序列化整段 prompt，且会随派生
  候选一直落盘。改为 metadata 只写 key，文本集中在 yml `prompts` 段。

## 验证

- 动态 Extractor：metadata prompt key 引用 yml `prompts.extract`、任意策略、多策略逐次
  调用、默认 JSON、XML 子类解析、策略标记、consolidation/reflect prompt key 透传、单策略
  失败隔离、无 prompt 回退、registry 缺失时回退内联文本。
- `PromptRegistry`：按 phase+key 取文本、缺失返回 None、空配置容错。
- `DynamicEvolver`：无命中 ADD 落盘；中段 LLM 判定 SUPERSEDE；非法 LLM 响应回退
  ADD；高相似度 NOOP 不落盘；procedural 走父类路径。
- Engine：默认直写路径两次重复写入都落盘（去重交给显式 evolve）。
- `pytest -m unit`：132 passed。
- `pytest tests/integration`：通过（仅外部服务依赖项 skipped：redis/milvus/
  elasticsearch/nano-graphrag）。
- 新增/重构文件 `ruff check` 通过；全仓 Ruff 仍有既存行长问题（prompt 字符串）。

## 已知遗留

- CLI/MCP 尚未为 metadata prompt key 提供专用参数，当前通过 Python API、HTTP payload 或
  batch NDJSON 传递。
- reflect 步默认 no-op，"反思型记忆"的具体语义留待后续特性扩展。
- 旧 pipeline profile 未声明 evolver 时默认走 `orchestrating`（legacy）；改为 `dynamic`
  才启用动态四步编排。
- yml `prompts` 段默认为空骨架，具体 prompt 文本需用户在配置中覆盖。
