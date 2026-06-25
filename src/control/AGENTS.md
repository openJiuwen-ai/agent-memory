# Agent Memory Control

**规约文档**：[S03-memory-manage.md](../../docs/specs/S03-memory-manage.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

编排层（管理面）：不直接生产/检索记忆,而是管理它们的生命周期与使用规则。`MemoryEngine` 是接口层各语义的编排中枢，驱动接入层、构建层、检索层、存储层完成实际工作；其余算子（Lifecycle/Governance/Permission/Scheduler/Policy）各自管一个治理切面。

所有算子继承 `ControlOperator`（`base.py`），由外部装配注入到引擎，引擎本身不实现具体能力。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | `ControlOperator` 抽象基类 + `ControlOperatorType` 枚举；所有算子的自描述契约 |
| `types.py` | 控制层数据类型（Action/Grant/Channel/JobInfo/MemoryPatch/DeleteSelector 等），被本层所有文件及上游 `api/` 依赖 |
| `engine.py` | `MemoryEngine` 抽象接口——跨层编排中枢，异步协程 |
| `lifecycle.py` | `LifecycleManager` 接口——状态流转（transition）与到期清扫（sweep） |
| `governance.py` | `Governor` 接口——检视/血缘回溯/审计查询 |
| `permission.py` | `PermissionManager` 接口——跨 scope 授权与校验 |
| `scheduler.py` | `Scheduler` 接口——hot/background 双通道演进调度 |
| `policy.py` | `PolicyManager` 接口——运行时可变策略读写 |
| `__init__.py` | 公开导出全部接口类与数据类型 |
| `*_impl/` | 每个算子对应一个实现子目录，含具体实现类；Producer 定义在顶层接口文件，具体实现用 `@XProducer.register(...)` 自注册 |
| `bootstrap.py` | `register_controllers()` 统一 import 各 `*_impl/` 包，触发实现自注册（幂等） |

## 文件关系

- 顶层 `.py` 只定义抽象接口，零实现逻辑
- `types.py` 不依赖本层其他文件（纯数据定义），被本层各接口和 `src/api/` 共同依赖
- 每个 `*_impl/` 子目录：具体实现类 + 尾部 `@XProducer.register("<target>")` 注册函数，由外部装配消费
- 顶层接口文件不 import `*_impl/`；`*_impl/` import 顶层接口文件
- Producer 工厂定义在对应顶层接口文件中（如 `engine.py` 的 `EngineProducer`），不要新增独立 `*_producer.py`

## 行为铁律

1. **引擎不实现具体算法能力**：`MemoryEngine` 只编排，Ingestor/构建算子/Retriever/Store 全部由装配注入。Engine 可通过注入的 `KVStore` 完成接口语义要求的真源落盘/点读/删除，但禁止绕过 Store 抽象绑定具体后端或在 engine 内调用 LLM。
2. **引擎方法一律异步协程**：同步调用由 `api/` 层自行桥接（`asyncio.run`），engine 内不做同步阻塞。
3. **鉴权不在本层执行**：`PermissionManager.check` 由 `api/MemoryAPI` 在入口调用，engine 信任传入的 scope 已鉴权。禁止在 engine 内部重复 check。
4. **LifecycleManager 只做非破坏式标记**：`transition` 标记状态（superseded/archived/forgotten），绝不物理删除。物理删除（purge）走 engine 的 `delete` 路径 + `DeleteMode.PURGE`。
5. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。`*_impl/` 通过 producer 工厂被外部装配消费，不被顶层接口引用。

## 双通道调度机制

`Scheduler` 管理两条执行通道：

- **HOT**：在线低时延——write 返回前完成的轻量索引构建走这条
- **BACKGROUND**：离线异步——重的抽取/升华/重索引提交到这条，不阻塞主链路

`MemoryEngine.write` 返回时 background 任务尚未完成；`evolve` 显式触发时返回任务 id，通过 `Scheduler.status` 查询进度。

## 本地约束

- `types.py` 中 `DeleteSelector` 各条件取「与」关系，至少给出一项；Engine 收到空 selector 必须抛 `ValidationError`
- `UpdateMode.SUPERSEDE`（默认）生成新 id，旧 id 标记 superseded——`update` 返回的记忆 id 可能与传入的 `unit_id` 不同
- `DeleteMode.PURGE` 是唯一物理删除路径；会删除真源、移除索引，并递归删除 provenance 后代
- `DeleteMode.DOWNWEIGHT` 不改变 lifecycle，只降低 `metadata.importance`
- 运行时策略的读写职责归 `PolicyManager`；Engine 不承载具体策略存储或策略键校验逻辑
- `PolicyManager` 只管理运行时可变策略；未知 key 或试图新增 key 必须抛 `PolicyError`
- 所有算子必须实现 `operator_type()` 和 `health()`（继承自 `ControlOperator`）
- 跨模块规则（如 scope 隔离、MemoryUnit 跨层传递）见 `docs/specs/`，不在本文件重复
