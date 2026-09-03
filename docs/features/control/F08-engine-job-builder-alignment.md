# F08 — Engine 与后台 Job 的 IndexBuilder/Evolver 对齐（E-06 收口）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-03 |
| 影响范围 | `jiuwen_memory/control/jobs_impl/middle_to_long_job.py`（Spec 删自解析 + 缺注入校验）、`jiuwen_memory/control/jobs_impl/evolve_job.py`（同）、`jiuwen_memory/control/engine_impl/in_memory_engine.py`（`evolve` 注入 evolver）、`jiuwen_memory/control/engine_impl/cloud_engine.py`（同）、`tests/unit/control/test_job_builder_alignment.py`（新增）、`tests/unit/control/test_middle_two_instances_e2e.py`（补注入） |
| 测试基线 | `pytest tests/unit/control` 全绿（4 个既有环境依赖 skip：real-LLM e2e ×4、双实例 Redis e2e ×1）；`pytest -m unit` 全量仅 `tests/unit/construction/test_entity_linker.py` 2 例顺序性日志捕获闪烁（隔离运行通过，与本特性无关） |
| Refs | [E-06（issues 清单）](../../pluginized-assembly-current-issues.md)、[`F06-middle-term-memory`](F06-middle-term-memory.md)、[`S03-control`](../../specs/S03-control.md) |

---

## 背景

[issues 清单 E-06](../../pluginized-assembly-current-issues.md)：Engine 写入用一套
`IndexBuilder`，而后台 Job 在装配期自行解析另一套：

- `MiddleToLongJobSpec._build_middle_to_long_job_spec` 按 `vector_enabled` 推导默认
  Builder（`hybrid` / `fulltext`），并经 `EvolverProducer.dep` / `IndexBuilderProducer.dep`
  解析 default 实例；
- `EvolveJobSpec._build_evolve_job_spec` 经 `EvolverProducer.dep(config, default="orchestrating")`
  解析 default evolver。

F06 决策 4 已在 Engine 侧通过 `get_job` 运行时覆盖入参注入正确实例（middle 路径传
pipeline binding 的 `index=`/`evolver=`），但只覆盖不堵漏：

1. **静默回退风险**：注入缺失时（调用方漏传、新接入的 Engine 忘传）Spec 兜底的
   default 实例悄悄生效——原文由 A Builder 写入、归档却调 B.remove，表现为重复记忆、
   索引查不到或旧索引清不掉，且无任何报错。
2. **EvolveJob 完全没注入**：`Engine.evolve` 只传 `mode`，Job 用的 evolver 是 Spec
   装配期解析的 default——与 Engine 写入/演进用的实例可能不同，演进产物的索引维护
   可能走另一套 Builder。
3. **多 profile 必然错位**：`CloudEngine` 按 `message_type` 选 binding，Spec 侧只有
   单一 default，两者天然对不齐。

## 决策

### 决策 1：Spec 装配期不再解析 index/evolver

- `MiddleToLongJobSpec.index` / `.evolver`、`EvolveJobSpec.evolver` 改为可选字段
  （默认 `None`）；`_build_middle_to_long_job_spec` / `_build_evolve_job_spec` 不再
  读 `vector_enabled`、不再调 `IndexBuilderProducer.dep` / `EvolverProducer.dep`。
- Spec 字段保留（不删 dataclass 字段）：作为手工装配/测试的预烘焙注入点，
  `with_scope` 取值顺序是 `kwargs 运行时注入优先，Spec 字段兜底`。生产装配链
  （`JobFactoryProducer.register("default")`）不设这两个字段——即生产路径上
  注入是唯一来源。

### 决策 2：缺注入显式报错，不猜默认

`with_scope` 校验：`evolver` / `index` 解析后仍为 `None` 时抛
`ValidationError`（`jiuwen_memory/common/errors`），错误信息注明「由 Engine 经
get_job 运行时注入，Spec 不自行解析」。装配错误在 Job 构造点暴露，不再静默
退化成 default 实例——对应 E-06 验收标准第 3 条。

### 决策 3：Engine 提交时必传注入

- **middle 路径**：两个 Engine 的 `_write_middle_path` 既有行为不变——传
  `evolver=`（pipeline binding 或 Engine 单 profile 字段）与 `index=`（同源的
  IndexBuilder）。
- **`evolve()`**（本次补齐）：`InMemoryEngine.evolve` / `CloudEngine.evolve` 先校验
  `self._evolver` 非 `None`（否则 `RuntimeError`，与 write 路径「evolver 缺失抛错
  不降级」同一精神），再经 `get_job(JobType.EVOLVE, scope=..., mode=...,
  evolver=self._evolver)` 注入 Engine 装配的同一实例。

### 拒绝的方案

- **保留 Spec 默认解析 + Engine 覆盖**（修复前状态）：注入缺失静默回退错误默认，
  装配错误不可见；且 EvolveJob 无覆盖点。
- **Spec 持有全部 profile 的 builder 映射、按 message_type 自选**：把 pipeline 路由
  职责搬进 Job Spec（违反 control/AGENTS 行为铁律「Pipeline 只做 profile 选择」），
  且 Job 无法得知运行时 write 选中了哪个 binding——路由真源必须留在 Engine。
- **任务消息携带 Builder 对象（跨进程）**：不可序列化。E-06 实施方案第 3 条的
  「组件名 + profile 标识、worker 侧按名解析」对当前进程内调度器
  （InProcessScheduler / AsyncTimerScheduler）是过度设计，列为已知遗留。

## 验证基线

新增 `tests/unit/control/test_job_builder_alignment.py`（6 用例，`@pytest.mark.unit`）：

| 用例 | 验证点 |
|---|---|
| `test_middle_to_long_spec_without_injection_raises` | 缺 `evolver` / `index` 注入时 `with_scope` 分别抛 `ValidationError` |
| `test_middle_to_long_runtime_injection_overrides_spec_fallback` | 运行时注入优先于 Spec 兜底字段，Spec 侧另一套零调用 |
| `test_evolve_spec_without_injection_raises` | `EvolveJobSpec` 缺 evolver 注入抛 `ValidationError` |
| `test_engine_evolve_injects_own_evolver_into_job` | `Engine.evolve` 提交的 Job 持有 Engine 装配的同一 evolver（`is` 同一性） |
| `test_engine_evolve_without_evolver_raises` | Engine 未装配 evolver 时 `evolve` 抛 `RuntimeError` |
| `test_middle_job_uses_engine_builder_not_spec_fallback` | **E-06 验收第 1 条**：双 Builder 配置下，write 建索引与 Job 归档 `remove` 均只落在 Engine 的 Builder A，Spec 兜底 Builder B 全程零调用 |

既有用例回归：`test_engine_write_middle_path.py`、`test_middle_e2e.py`、
`test_engine_evolve_scheduler.py`（含 build_kernel 真实装配链的 evolve）、
`test_cloud_engine.py`、`test_evolve_job.py`、`test_middle_to_long_job.py`、API 层
`test_space_aware_authorization.py` / `test_permission_audit.py`（真实装配
`api.evolve`）全部通过。

## 已知遗留

- **跨进程/重启消费任务**：当前 Scheduler 均进程内，Job 实例直接持组件引用；未来
  引入分布式队列时需按 E-06 实施方案第 3 条改为「消息携带组件名 + profile 标识、
  worker 侧装配上下文按名解析、名不存在立即报错」。
- **Spec 兜底字段保留**：`MiddleToLongJobSpec.index/evolver`、`EvolveJobSpec.evolver`
  作为手工装配注入点存在；生产装配链不设，若后续确认无消费方可整体删除。
- `test_middle_two_instances_e2e.py` 默认 skip（依赖 Redis），已同步补注入参数，
  双实例语义验证需按其 skip 说明手动执行。
