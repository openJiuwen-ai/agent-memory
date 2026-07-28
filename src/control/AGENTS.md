# Agent Memory Control

**规约文档**：[S03-memory-manage.md](../../docs/specs/S03-memory-manage.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

编排层（管理面）：不直接生产/检索记忆,而是管理它们的生命周期与使用规则。`MemoryEngine` 是接口层各语义的编排中枢，驱动接入层、构建层、检索层、存储层完成实际工作；其余算子（Pipeline/Lifecycle/Governance/Permission/Scheduler/Policy）各自管一个治理切面。

所有算子继承 `ControlOperator`（`base.py`），由外部装配注入到引擎，引擎本身不实现具体能力。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | `ControlOperator` 抽象基类 + `ControlOperatorType` 枚举；所有算子的自描述契约 |
| `types.py` | 控制层数据类型（Action/Grant/Channel/JobInfo/MemoryPatch/DeleteSelector 等），被本层所有文件及上游 `api/` 依赖 |
| `engine.py` | `MemoryEngine` 抽象接口——跨层编排中枢，异步协程 |
| `pipeline.py` | `MemoryPipeline` 抽象接口——按记忆类型选择构建/查询 profile |
| `lifecycle.py` | `LifecycleManager` 接口——状态流转（transition）与到期清扫（sweep） |
| `governance.py` | `Governor` 接口——检视/血缘回溯/审计查询 |
| `permission.py` | `PermissionManager` 接口——跨 scope 授权与校验 |
| `scheduler.py` | `Scheduler` 接口——hot/background 双通道演进调度 |
| `policy.py` | `PolicyManager` 接口——运行时可变策略读写 |
| `__init__.py` | 公开导出全部接口类与数据类型 |
| `*_impl/` | 每个算子对应一个实现子目录，含具体实现类；Producer 定义在顶层接口文件，具体实现用 `@XProducer.register(...)` 自注册 |
| `bootstrap.py` | `register_controllers()` 统一 import 各 `*_impl/` 包，触发实现自注册（幂等） |
| `pipeline_impl/` | MemoryPipeline 实现目录（metadata） |

## 文件关系

- 顶层 `.py` 只定义抽象接口，零实现逻辑
- `types.py` 不依赖本层其他文件（纯数据定义），被本层各接口和 `src/api/` 共同依赖
- 每个 `*_impl/` 子目录：具体实现类 + 尾部 `@XProducer.register("<target>")` 注册函数，由外部装配消费
- 顶层接口文件不 import `*_impl/`；`*_impl/` import 顶层接口文件
- Producer 工厂定义在对应顶层接口文件中（如 `engine.py` 的 `EngineProducer`），不要新增独立 `*_producer.py`

## 行为铁律

1. **引擎不实现具体算法能力**：`MemoryEngine` 只编排，Ingestor/构建算子/Retriever/Store 全部由装配注入。Engine 可通过注入的 `KVStore` 完成接口语义要求的真源落盘/点读/删除，但禁止绕过 Store 抽象绑定具体后端或在 engine 内调用 LLM。
2. **引擎方法一律异步协程**：同步调用由 `api/` 层自行桥接（`asyncio.run`），engine 内不做同步阻塞。
3. **鉴权不在本层执行**：`PermissionManager.check` 由 `api/MemoryAPI` 在入口调用，engine 信任传入的 scope 已鉴权。Engine 只提供 `permission_context_for_unit` / `permission_contexts_for_delete` 这类 metadata-only 解析入口，供 API 做类型化鉴权；禁止在 engine 内部重复 check。
4. **LifecycleManager 只做非破坏式标记**：`transition` 标记状态（superseded/archived/forgotten），绝不物理删除。物理删除（purge）走 engine 的 `delete` 路径 + `DeleteMode.PURGE`。
5. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 producer 工厂被外部装配消费，不被顶层接口引用。
6. **Pipeline 只做 profile 选择**：`MemoryPipeline` 选择一组已装配的 `IndexBuilder` / `Evolver` / `Retriever` / `Classifier` 绑定，不实现抽取、巩固、索引、检索算法。
7. **PermissionContext 由可信边界构造**：write/recall 的 context 来自 API 入参；get/update/delete 的已有 unit context 必须由 Engine 从真源元数据解析，不能信任调用方声明 memory_type。

## 双通道调度机制

`Scheduler` 管理两条执行通道：

- **HOT**：在线低时延——write 返回前完成的轻量索引构建走这条
- **BACKGROUND**：离线异步——重的抽取/升华/重索引提交到这条，不阻塞主链路

`MemoryEngine.write` 返回时 background 任务尚未完成；`evolve` 显式触发时返回任务 id，通过 `Scheduler.status` 查询进度。

## write 路径调用级开关（infer / procedural）

`InMemoryEngine.write` 先经可选 `MemoryPipeline.select_for_write` 选择构建 profile，再据 `metadata` 下推的两个开关分三路（详见 `docs/features/api/F02-write-infer-extract.md` 决策6-8）：

- **`procedural="true"`**（过程记忆）：原文不落 KV；Extractor 汇总成一条 PROCEDURAL 后由 Evolver 直接落盘（`DynamicEvolver` 也走父类 procedural 路径，不判定）。
- **`infer="true"`**（同步抽取）：原文落 `/messages/{id}` 但不建索引；Evolver 收集上下文后调用 Extractor，派生候选经 Evolver 落盘（`OrchestratingEvolver` 走 `_dedup_batch`，`DynamicEvolver` 走 consolidate→reflect→落盘）。
- **缺省（infer=false）**：原文经 Classifier 后直接落 `/memory/{id}` + 建索引（直写路径，不去重）；去重交给显式 `evolve()` 触发。
- **evolver 缺失**：procedural/infer=true 但未注入 `Evolver`（`None`）时抛 `RuntimeError`——装配问题暴露而非静默降级。

> tier+tags 的产出路径：**infer=false** 时由 `Classifier`（LLMClassifier）给原文打；**infer=true** 时由 `Extractor` 在派生时一并产出（不经 classifier）。两条路径产出同口径（episodic/semantic/procedural + tags）。procedural 路径 tier 固定 PROCEDURAL。

**KV key 前缀分离**（决策6）：真源 key 按「是否建索引」带前缀——`/memory/{id}`（建索引记忆）、`/messages/{id}`（未建索引 infer 原文）；前缀常量与 helper 在 `common.type_def.memory` / `common.type_def.raw`。所有落盘/回查点用 `memory_key`/`messages_key`，按 key 匹配 id 处（lifecycle）用带前缀 key 直接比对。

引擎只调用注入的 Evolver（`OrchestratingEvolver` 或 `DynamicEvolver`，由装配/pipeline 选择），不直接调用 LLM。动态抽取仍要求 `infer=true`；
metadata 用 `_extract_prompt_<strategy>` / `_consolidation_prompt_<strategy>` / `_reflect_prompt_<strategy>` 传 prompt key（引用 yml `prompts` 段）。

## pipeline 路由

`MemoryPipeline` 是 control 层的跨构建/查询 profile 编排点：

- 写入侧：`select_for_write(units)` 返回 `PipelineBinding`，默认 `metadata` 实现读取 `MemoryUnit.metadata[route_key]`（默认 `memory_type`）。
- 查询侧：`select_for_recall(query)` 返回 `PipelineBinding`，默认 `metadata` 实现优先读取 `RetrievalQuery.extensions[route_key]`，其次读取等值 filter 的 `route_key` / `metadata.<route_key>`。
- `PipelineBinding` 绑定 `index_builder`、`evolver`、`retriever`，以及可选 `classifier`。
- `InMemoryEngine` 使用绑定后的构建组件处理 write。profile 未显式绑定某组件时回退默认实例。
- 未配置 `pipeline.default` 时不启用 pipeline，行为等价旧单 pipeline；用户通过 YAML 显式声明后启用。

## 本地约束

- `types.py` 中 `DeleteSelector` 各条件取「与」关系，至少给出一项；Engine 收到空 selector 必须抛 `ValidationError`
- `UpdateMode.SUPERSEDE`（默认）生成新 id，旧 id 标记 superseded——`update` 返回的记忆 id 可能与传入的 `unit_id` 不同
- `DeleteMode.PURGE` 是唯一物理删除路径；会删除真源、移除索引，并递归删除 provenance 后代
- `DeleteMode.DOWNWEIGHT` 不改变 lifecycle，只降低 `metadata.importance`
- 运行时策略的读写职责归 `PolicyManager`；Engine 不承载具体策略存储或策略键校验逻辑
- `PolicyManager` 只管理运行时可变策略；未知 key 或试图新增 key 必须抛 `PolicyError`
- 所有算子必须实现 `operator_type()` 和 `health()`（继承自 `ControlOperator`）
- 跨模块规则（如 scope 隔离、MemoryUnit 跨层传递）见 `docs/specs/`，不在本文件重复
