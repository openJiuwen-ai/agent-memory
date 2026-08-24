# Agent Memory Control

**规约文档**：[S03-control.md](../../docs/specs/S03-control.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

编排层（管理面）：不直接生产/检索记忆,而是管理它们的生命周期与使用规则。`MemoryEngine` 是接口层各语义的编排中枢，驱动接入层、构建层、检索层、存储层完成实际工作；其余算子（Pipeline/Lifecycle/Governance/Permission/Scheduler/IngestJob/Policy/Space）各自管一个治理切面。

所有算子继承 `ControlOperator`（`base.py`），由外部装配注入到引擎，引擎本身不实现具体能力。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | `ControlOperator` 抽象基类 + `ControlOperatorType` 枚举；所有算子的自描述契约 |
| `types.py` | 控制层数据类型（Action/Grant/Channel/JobInfo/MemoryPatch/DeleteSelector/BatchWrite* 等），被本层所有文件及上游 `api/` 依赖 |
| `engine.py` | `MemoryEngine` 抽象接口——跨层编排中枢，异步协程 |
| `pipeline.py` | `MemoryPipeline` 抽象接口——按记忆类型选择构建/查询 profile |
| `lifecycle.py` | `LifecycleManager` 接口——状态流转（transition）与到期清扫（sweep） |
| `governance.py` | `Governor` 接口——检视/血缘回溯/审计查询 |
| `permission.py` | `PermissionManager` 接口——跨 scope 授权与校验 |
| `scheduler.py` | `Scheduler` 接口——hot/background 双通道演进调度 |
| `ingest_job.py` | `IngestJobController` 接口、任务数据类型与 Producer——长耗时摄入任务管理 |
| `policy.py` | `PolicyManager` 接口——运行时可变策略读写 |
| `space.py` | `SpaceManager` 接口——space 创建/读取/列表/更新/归档/删除/导出/用量/策略/成员 |
| `__init__.py` | 公开导出全部接口类与数据类型 |
| `engine_impl/` | MemoryEngine 实现目录：`in_memory_engine.py`（本地最小实现）/ `cloud_engine.py`（云侧 message_type/profile 编排） |
| `*_impl/` | 每个算子对应一个实现子目录，含具体实现类；Producer 定义在顶层接口文件，具体实现用 `@XProducer.register(...)` 自注册 |
| `bootstrap.py` | `register_controllers()` 统一 import 各 `*_impl/` 包，触发实现自注册（幂等） |
| `pipeline_impl/` | MemoryPipeline 实现目录（metadata） |
| `space_impl/` | SpaceManager 实现目录（kv） |
| `job_impl/` | IngestJobController 实现目录（in_process：后台队列、状态持久化与 payload 幂等） |

## 文件关系

- 顶层 `.py` 只定义抽象接口，零实现逻辑
- `types.py` 不依赖本层其他文件（纯数据定义），被本层各接口和 `src/api/` 共同依赖
- 每个 `*_impl/` 子目录：具体实现类 + 尾部 `@XProducer.register("<target>")` 注册函数，由外部装配消费
- 顶层接口文件不 import `*_impl/`；`*_impl/` import 顶层接口文件
- Producer 工厂定义在对应顶层接口文件中（如 `engine.py` 的 `EngineProducer`），不要新增独立 `*_producer.py`

## 行为铁律

0. **系统控制只读 `system_metadata`**：Engine/Pipeline/PermissionContext 所需的
   `infer` / `procedural` / `middle` / 路由和内部状态不得从 `user_metadata`
   读取或 fallback。`MemoryPatch` 对两个命名空间分别 merge-update。

1. **引擎不实现具体算法能力**：`MemoryEngine` 只编排，Ingestor/构建算子/Retriever/Storage 全部由装配注入。**记忆本体的写入一律经 `IndexBuilder`**——engine 不直接调用 `Storage` 的 `add`/`update`/`delete`；读取（`get`/`list`/`scopes`）与生命周期治理（`LifecycleManager`）不受此限。禁止绕过 Storage 抽象绑定具体后端或在 engine 内调用 LLM。
2. **引擎方法一律异步协程**：同步调用由 `api/` 层自行桥接（`asyncio.run`），engine 内不做同步阻塞。
3. **鉴权不在本层执行**：`PermissionManager.check` 由 `api/MemoryAPI` 在入口调用，engine 信任传入的 scope 已鉴权。Engine 提供 `permission_context_for_unit`、`list_with_permission_contexts` 和 `permission_contexts_for_delete`，供 API 使用真源 metadata 做类型化鉴权；list 的 items、count 与 contexts 必须来自同一次 KV 列表查询。禁止在 engine 内部重复 check。
4. **LifecycleManager 只做 Scope 内非破坏式标记**：`transition` / `supersede` 必须接收完整 Scope，只标记该 Scope 下的目标 id，绝不物理删除。物理删除（purge）走 engine 的 `delete` 路径 + `DeleteMode.PURGE`。
5. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 producer 工厂被外部装配消费，不被顶层接口引用。
6. **Pipeline 只做 profile 选择**：`MemoryPipeline` 选择一组已装配的 `IndexBuilder` / `Evolver` / `Retriever` / `Classifier` 绑定，不实现抽取、巩固、索引、检索算法，不让 construction/retrieval 反向依赖 control。
7. **PermissionContext 由可信边界构造**：add/search/list 的请求 context 来自 API 入参；list 当前页实际 unit 与 get/update/delete 的已有 unit context 必须由 Engine 从真源元数据解析，不能信任调用方声明 memory_type。
8. **权限路由与数据范围绑定**：RoutingPermissionManager 只按 PermissionContext 选择
   delegate；API 必须把授权所依据的路由字段回注为系统过滤谓词。未知路由值和直接
   policy 名落最小权限 fallback，fallback 不得配置为 allow_all。
9. **space 是权限硬边界**：`PermissionManager.check` 先按 `org + space` 判断 owner-cover；同 org 跨 space 默认拒绝，只有 `Scope()` 或显式 grant 可跨 space。owner-cover 的主体路径由 `PermissionContext.metadata["principal_path"]` 选择（默认 `user_agent`，可选 `agent_user`）。
10. **space policy 是主体路径来源**：`LocalMemoryAPI` 在鉴权前读取目标 space policy，并用其中的 `principal_path` 覆盖 `PermissionContext.metadata["principal_path"]`；调用级 metadata 不能临时改变已有 space 的主体路径。
11. **Space id 全局唯一**：`KVSpaceManager` 在根 Scope 维护全局 Space 注册键；不同 org 创建同一非空 Space id 必须报 `ConflictError`。
12. **治理读取按已鉴权 Scope 定位**：Governor 的 `inspect` / `trace` 必须接收 API 已鉴权 target Scope，不得仅按 unit id 跨 Scope 扫描。
13. **批量写入保序且不鉴权**：Engine 的 `batch_write` 只接收 API 已前置校验的归一化 item，按输入顺序复用 `write`；不得在 Engine 内并发提交或重复执行 `PermissionManager.check`。
14. **Ingest 任务按 Scope 隔离**：任务状态查询为纯读取，不更新进程缓存或
    `payload_id -> job_id` 映射；`_find_existing` 只有在任务 Scope 与请求 Scope
    完全一致后才维护映射，READ 鉴权由 MemoryAPI 执行。

## 双通道调度机制

`Scheduler` 管理两条执行通道：

- **HOT**：在线低时延——write 返回前完成的轻量索引构建走这条
- **BACKGROUND**：离线异步——重的抽取/升华/重索引提交到这条，不阻塞主链路

`MemoryEngine.write` 返回时 background 任务尚未完成；`evolve` 显式触发时返回任务 id，通过 `Scheduler.status` 查询进度。

## write 路径调用级开关（infer / procedural）

`InMemoryEngine.write` 先经可选 `MemoryPipeline.select_for_write` 选择构建 profile，再据 `system_metadata` 下推的两个开关分三路（详见 `docs/features/api/F02-write-infer-extract.md` 决策6-8）：

- **`procedural="true"`**（过程记忆）：原文不落 KV；Extractor 汇总成一条 PROCEDURAL 后由 Evolver 直接落盘（`DynamicEvolver` 也走父类 procedural 路径，不判定）。
- **`infer="true"`**（同步抽取）：原文落 `/messages/{id}` 但不建索引；Evolver 收集上下文后调用 Extractor，派生候选经 Evolver 落盘（`OrchestratingEvolver` 走 `_dedup_batch`，`DynamicEvolver` 走 consolidate→reflect→落盘）。
- **缺省（infer=false）**：原文经 Classifier 后直接落 `/memory/{id}` + 建索引（直写路径，不去重）；去重交给显式 `evolve()` 触发。
- **evolver 缺失**：procedural/infer=true 但未注入 `Evolver`（`None`）时抛 `RuntimeError`——装配问题暴露而非静默降级。

> tier+tags 的产出路径：**infer=false** 时由 `Classifier`（LLMClassifier）给原文打；**infer=true** 时由 `Extractor` 在派生时一并产出（不经 classifier）。两条路径产出同口径（episodic/semantic/procedural + tags）。procedural 路径 tier 固定 PROCEDURAL。

**KV key 前缀分离**（决策6）：真源 key 按「是否建索引」带前缀——`/memory/{id}`（建索引记忆）、`/messages/{id}`（未建索引 infer 原文）；前缀常量与 helper 在 `common.type_def.memory` / `common.type_def.raw`。**正排的 key 拼装与序列化由 `ForwardIndexBuilder`（写）与 Storage（读 `get`/`list`）分担**，两侧共用 `memory_key` + `memory_codec`；控制层只传 MemoryUnit 与 unit_id。原文不经 Storage——构建层注入独立的 `KVStore` 自行读写 `/messages/`。仅 `kv_space_manager` 的跨类型全局遍历（统计、清空 space）与 `EncryptedKVStore` 的按前缀加密策略仍直接匹配前缀。

引擎只调用注入的 Evolver（`OrchestratingEvolver` 或 `DynamicEvolver`，由装配/pipeline 选择），不直接调用 LLM。write 同步路径中的动态抽取仍要求 `infer=true`；
metadata 用 `_extract_prompt_<strategy>` / `_consolidation_prompt_<strategy>` / `_reflect_prompt_<strategy>` 传 prompt key（引用 yml `prompts` 段）。

## pipeline 路由

`MemoryPipeline` 是 control 层的跨构建/查询 profile 编排点：

- 写入侧：`select_for_write(units)` 返回 `PipelineBinding`，默认 `metadata` 实现读取 `MemoryUnit.system_metadata[route_key]`（默认 `memory_type`）。
- 查询侧：`select_for_recall(query)` 返回 `PipelineBinding`，默认 `metadata` 实现优先
  读取 `RetrievalQuery.extensions[route_key]`，其次从规范化 FilterExpr 提取逻辑上
  强制成立的 `system_metadata.<route_key>` 唯一等值（`memory_type` 裸字段仅作兼容别名）。
- `PipelineBinding` 只绑定组件引用：`index_builder`、`evolver`、`retriever`、可选 `classifier`。
- `InMemoryEngine` 仅接受 `space=""` 的本地兼容域，使用绑定后的 `index_builder/evolver/classifier` 处理 write；profile 选择 `OrchestratingEvolver` 或 `DynamicEvolver` 决定 EXTRACT 路径。recall 使用绑定后的 `retriever`；`list` 把分页、类型、过滤和 extensions 透传 `Storage.list`，不经 pipeline。未注入 pipeline 时走原单 profile 字段。
- `CloudEngine` 使用 `message_type`（默认 metadata key）选择构建/查询 profile，写入后固化 `system_metadata["message_type"]` 与 `system_metadata["pipeline"]`，并校验真源 unit.scope 与 target scope 一致。
- 未配置 `pipeline.default` 时不启用 pipeline，行为等价旧单 pipeline；用户通过 YAML 显式声明后启用。

## 本地约束

- `types.py` 中 `DeleteSelector` 各条件取「与」关系，至少给出一项；Engine 收到空 selector 必须抛 `ValidationError`
- `UpdateMode.SUPERSEDE`（默认）生成新 id，旧 id 标记 superseded——`update` 返回的记忆 id 可能与传入的 `unit_id` 不同
- `DeleteMode.PURGE` 是唯一物理删除路径；会删除真源、移除索引，并递归删除 provenance 后代
- `DeleteMode.DOWNWEIGHT` 不改变 lifecycle，只降低 `system_metadata.importance`
- 运行时策略的读写职责归 `PolicyManager`；Engine 不承载具体策略存储或策略键校验逻辑
- space 元数据、space policy、成员和 offboarding 状态职责归 `SpaceManager`；Engine 的 `purge_space` 负责枚举目标 `org + space` 的全部子 Scope 并清理记忆真源与索引
- `PolicyManager` 只管理运行时可变策略；未知 key 或试图新增 key 必须抛 `PolicyError`
- 所有算子必须实现 `operator_type()` 和 `health()`（继承自 `ControlOperator`）
- 跨模块规则（如 scope 隔离、MemoryUnit 跨层传递）见 `docs/specs/`，不在本文件重复
