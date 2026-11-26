# F10：写入真异步与批量编排（add_async / batch_add_async）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-26 |
| 影响范围 | `jiuwen_memory/api/memory_api_impl/{write_ops,assembly}.py`、`jiuwen_memory/control/application/command.py`、`jiuwen_memory/control/`（新增写入编排算子与阻塞执行器端口）、`jiuwen_memory/control/engine.py`、`jiuwen_memory/control/engine_impl/{in_memory,cloud,agent}_engine.py`、`jiuwen_memory/common/lock/{lock.py,lock_impl/}`、`jiuwen_memory/construction/evolver.py`、`jiuwen_memory/construction/evolver_impl/orchestrating_evolver.py`、`jiuwen_memory/config/defaults.py`；`docs/specs/S02-memory-api.md`、`docs/specs/S03-control.md`、`docs/specs/S05-construction.md`、`docs/specs/S07-common.md`、`docs/specs/S08-config.md`；`docs/features/api/F01`、`docs/features/api/F03`、`docs/features/common/F06-distributed-lock.md`；`docs/{zh/安装指导,en/Installation Guide}/` 下的部署概览与容器化部署文档；`jiuwen_memory/{api,control,common,construction}/AGENTS.md` |
| 测试基线 | 见「验证 / 实现后的测试基线」 |
| Refs | 无（用户确认本次提交不关联 issue） |

---

## 背景

`add_async` / `batch_add_async` 虽然是协程签名，但内部并不是非阻塞的。api/F01 决策 4 写的“`add_async`
是直通引擎的真协程”与代码不符，本特性顺带修正这处描述。

**目前仍在事件循环上同步执行的阻塞点**（以 2026-11-18 的代码为准）：

| 层 | 调用 | 性质 |
|---|---|---|
| API | `_write_target` / `_batch_write_targets` → `collective.route_many` | Router LLM（同步 `LLM.chat`） |
| API | `_authorize`、`_ensure_space_writable`、`_ensure_audit_integrity_healthy`、`_log` | sqlite / 审计 IO |
| API | `_route_context` | 计算候选空间、组装判权回调，内部会读写空间注册表和权限存储 |
| Engine | `ingestor.ingest` | IO / CPU |
| Engine | `classifier.classify`（使用 `LLMClassifier` 时） | LLM |
| Engine | `evolver.evolve`、`index_builder.build` | 已经用 `asyncio.to_thread` 放到线程里，但和其他 IO 共用默认线程池 |

**批量写入是串行的**：InMemory、Cloud、Agent 三个引擎的 `batch_write` 都按输入顺序
逐条 `await self.write(...)`。`infer=true` 时每条单独抽取、单独去重，同一 scope 写 N 条
就要调 N 次抽取 LLM，每次都重新组装上下文和召回结果。api/F03 决策 4 当时有意选择串行保序，
并在已知遗留 1 里留下“按 `(scope, stream_id)` 分组并发”的方向，本特性就是来兑现这条遗留的。

**后果**：
1. 在 asyncio 宿主进程里使用 SDK 时（例如 jiuwenswarm 适配器在 agent 主循环里调 `add_async`，
   `start_background` 也把后台组件绑在调用方的循环上），一个事件循环要同时服务多个请求和
   定时任务。只要有一次路由或分类在调 LLM，整个循环就被卡住，其他请求和
   `AsyncTimerScheduler` 的定时任务都要等。HTTP、MCP、CLI 已经在入口处隔离：HTTP 用
   `ThreadingHTTPServer`，每个请求一个线程，各自 `asyncio.run`；MCP 用 `asyncio.to_thread`
   把整次调用放到线程里执行。所以它们不存在这个问题，但能享受到下面第 2 点的收益，
   也会用到决策 7 的锁。
2. 批量写入的耗时和 token 消耗都随条数线性增长，同一 scope 内本可以共享的上下文没有被利用。

## 目标 / 非目标 / 反目标

**目标**
1. `add_async` / `batch_add_async` 调用链上的同步阻塞调用全部移出事件循环。
2. 同一 scope 内的 `infer=true` 条目一起抽取、去重和消歧，节省 token。
3. 不同 scope 之间并发执行（有并发上限），提高批量写入的吞吐。
4. 编排策略做成插件：接口和实现分离，通过配置替换，并满足 S09 的插件开发规约。

**非目标**
- 不改变“等写入完成才返回结果”的接口承诺，不引入“立即返回任务 ID”的提交方式。
- 不做 LLM 限流、429 退避或配额管理，本次只提供可配置的线程池大小。
- 不做跨请求合并（见已知遗留）。
- 不把 `LLM.chat` 等同步算子改造成原生 async。

**反目标**（即使功能跑通，出现以下情况也算失败）
- 合并抽取后，`outcome.units` 不再表示“这一条产生了什么”。唯一的例外是决策 6 的兜底：
  抽取器没给出来源标注时，整批共享派生记忆，而且这种情况必须有可观测信号，不能悄悄发生。
- 批里一条坏数据导致整批条目都被标为失败。
- 并发破坏了 `continue_on_error=False` 的“失败点之后无副作用”保证。
- 同一用户并发写入时，比现在更容易产生重复记忆。
- 发生决策 8“可观测性”中列出的任一降级事件，却没有任何可观测信号。

## 决策

### 1. 阻塞调用交给执行器端口，LLM 类和 IO 类分开线程池

- 新增控制层端口 `BlockingExecutor`：接口和实现分离，通过 Factory 注册，配置选择 target。
  默认 target 为 `thread_pool`，内含两个线程池，对应两条通道：
  - `llm`：路由、分类、抽取（`evolver.evolve`）这类一次要几秒的调用；
  - `io`：鉴权、审计、空间状态检查、ingest、`index_builder.build` 这类短调用。
- **加锁和释放锁留在事件循环上，不交给执行器**：`LockProvider` 本身就是异步接口，Redis
  实现用的是异步客户端，等锁用的是 `asyncio.sleep`，在事件循环上本来就不会阻塞。如果放进
  线程池，就得在线程里再建一个事件循环，会报“没有运行中的事件循环”之类的错误（MCP 当初
  就踩过这个坑，后来用 `to_thread` 绕开）。所以只把抽取、分类、建索引、鉴权、审计这类
  真正阻塞的同步调用交给执行器。`guard` 的续租任务也跑在事件循环上。
- 为什么分池：LLM 调用一占就是几秒，如果和 sqlite、审计共用默认线程池
  （`min(32, cpu+4)`），短操作会排不上队。
- **执行器归装配上下文所有，不做进程级单例**（S09 不变量 4）。由装配根创建一个实例，
  用同一个名字共享给 API 层和控制层，同一个装配上下文里只有这一份。
- 线程数属于运行参数，按 S09 不变量 10 在运行时读取，不放在装配拓扑里。它只是线程资源
  上限，**不负责 LLM 限流**。
- 把任务交给线程时要带上 `contextvars.copy_context()`，这样 trace、privacy staging、
  ingest cancellation 在线程里都能读到。要注意：在线程里对 ContextVar 调 `set`，结果不会
  传回协程。现有代码靠的是可变状态对象（`IngestCancellation`、`_StageCapture`），对象内部
  的修改能传回来，所以这条路走得通，但这些对象要过一遍线程安全审查（见决策 9）。
- 现在用 `asyncio.to_thread` 的地方（`evolve`、`index_builder.build`、continuity 路径）
  全部改走执行器对应的通道。
- **执行器必须支持重入，防止嵌套提交时死锁**：continuity 的 writer 在工作线程里调用
  `asyncio.run(self._add_impl(...))`（`continuity_ops.py`），内层的 `_add_impl` 又会把鉴权、
  审计等任务提交给同一个 IO 池。外层占着池里的线程等内层，内层却排不上队：池大小为 1 时
  一个请求就能卡死，池更大时也可能被并发的接续请求占满。
- **重入规则按通道区分**（同时防死锁和防续租被卡住）：用线程本地标记识别“提交任务的线程
  本身就是本执行器的工作线程”，然后：

  | 提交方 | 提交到 `io` | 提交到 `llm` |
  |---|---|---|
  | 事件循环（非工作线程） | 排队 | 排队 |
  | `io` 工作线程 | **当前线程内联执行** | 排队到 llm 池 |
  | `llm` 工作线程 | **当前线程内联执行** | **当前线程内联执行** |

  - **防死锁**：只有“io 工作线程等 llm 池”这一种跨池排队等待；llm 工作线程从不排队等别的
    任务，所以不会出现循环等待。io 池和 llm 池的大小都为 1 时，接续写入也能完成。
  - **防续租被卡住**：嵌套在工作线程里的事件循环（例如 continuity 内层的 `asyncio.run`）
    持有 per-user 锁时，`guard` 的续租任务就跑在这个循环上。按上表，它发出的耗时 LLM 调用
    （抽取、分类、路由）会排队到 llm 池，循环本身空闲在 `await` 上，续租能按时执行。只有短的
    io 任务会内联执行。**约束**：持锁期间，事件循环所在线程上内联执行的单个任务，耗时必须
    远小于锁租约，因此 io 通道不得承载 LLM 类调用。
  - 现状：continuity 内层固定 `infer=false`，不加锁，所以目前不会出现“内层持锁”的情况；
    这条规则是为了以后出现嵌套桥接时也成立。
  - **续租保证的前提：需要加锁的写入只能从宿主事件循环或 io 工作线程进入**。按上表，llm 工作
    线程上的提交一律内联执行。如果有插件在 llm 工作线程上启动内层事件循环并持锁，耗时的
    Evolver 就会卡住这个循环，续租也就停了。所以编排器在获取锁之前要检查当前线程：如果是本
    执行器的 llm 工作线程，直接拒绝这次写入（抛出内部的 `ValidationError`，说明“不允许在 llm
    工作线程上发起需要加锁的写入”），不拿锁、不写入。这条约束写入 S03 的执行器契约。
- **API 层直接持有执行器**：执行器是控制层端口，由装配根注入 `LocalMemoryAPI`。这符合
  S09 不变量 8，也不违反 S02 第 19 条，因为 API 层没有新增对 LLM 或 Store 的调用，只是把
  已有的调用换到别的线程上执行。之所以不把前置步骤打包交给控制层执行，是因为那样控制层
  就要反过来执行 API 层的鉴权逻辑。
- **前置步骤的顺序是安全约束**，线程切换不能改变这个顺序：

  ```
  [io]  入参校验 → 审计完整性预检 → 判定上下文（_route_context，可能隐式建 fallback 空间）
  [llm] route_many（只在需要判定时执行）
  [io]  鉴权（_authorize）→ 空间状态检查 → 编排器 / 引擎写入
  ```

  审计预检必须在任何副作用之前完成（T1 约束），鉴权必须在引擎写入之前完成，
  `route_many` 不得早于审计预检。PEP 仍然逐条只执行一次，顺序和现在一样。
- **健康检查和生命周期**（S09 不变量 13）：执行器提供 `health()` 和 `close(wait)`。
  `MemoryRuntime.close` 关闭时按这个顺序执行：先停止接收新请求，再等执行器里正在跑的任务
  完成，最后关闭线程池并释放下游依赖。以前 `asyncio.run` 结束时会调
  `shutdown_default_executor` 顺便回收线程，改用自有线程池后不再有这个兜底，所以必须由装配根
  显式关闭，不能指望解释器退出时自动清理。
- `MemoryEngine` / `MemoryAPI` 中 `add_async` 的签名不变。

### 2. 新增控制层算子 `WriteOrchestrator`（方案 A）

- **调用链位置**：在 `application/command.py` 的 `MemoryCommandService` 和 `MemoryEngine`
  之间。`write` / `batch_write_aligned` 不再直接调 Engine，改为交给编排器。这里说的是
  调用顺序，不是代码放在哪个文件。
- **代码位置**：S03 规定 `application/` 里只放非算子的应用端口，没有 Producer。编排器有
  Producer、有 `health`、实现可以替换，属于标准的控制层算子，所以按算子的惯例放置：
  接口和注册放在 `control/` 顶层，实现放在 `control/write_orchestrator_impl/`。
  `MemoryCommandService` 只持有注入进来的编排器实例。
- 接口和实现分离，通过 Factory 注册。配置项 `write.orchestrator` 选择 target：
  - `grouped`（缺省）：分组 → 切批 → 加锁 → 组间并发（有上限）→ 调 Engine 的批量原语；
  - `sequential`：兼容开关，不分组、不并发、不合并，结果和执行顺序与现在兼容。新增的只有
    锁等待和锁超时：infer 写入仍然加 per-user 锁（决策 7），所以兼容模式下并发的 infer 写入
    也不会产生重复。
  - 配置了未知的 target 时，装配阶段直接失败（S09 不变量 2）。
- **由能力决定走哪条路**（S09 不变量 6）：引擎的 `supports_grouped_write` 为 `False` 时，
  即使配置了 `grouped`，也按决策 3 真值表的第 5 行执行（不分组、不并发、不合并，仍然加锁，
  **`continue_on_error` 沿用调用方的取值**），并在 `capabilities` / `health` 里如实报告
  “不支持批量写，已退化为逐条执行”。不能靠配置假定所有引擎都支持批量写。
- 编排器只负责编排：不鉴权、不审计、不调 LLM。鉴权和审计仍由 API 层逐条完成（api/F03
  决策 3 不变），符合 S02 第 19 条的职责边界。
- 单条 `add_async` 也经过编排器，当作只有一条的组处理，所以同样受决策 7 的锁保护。
- 编排器提供 `health()`，报告实际生效的 target、锁是否就绪、执行器状态。编排器本身不持有
  线程资源，生命周期跟随执行器和锁。

### 3. 开关与默认行为真值表（默认行为只以本表为准）

- **合并抽取会改变派生结果**：一起抽取、去重、消歧，结果和逐条抽取不一样。返回结构
  不变，但这是行为变化，需要在 S02、本文档和变更说明里写明。默认开启是有意的选择，
  兼容方式是提供下面的开关，满足 S09 不变量 14 的“显式适配”。
- **这是对 S09“默认配置回归”要求的一次明确例外，由产品决定**：S09 要求，没有声明新插件
  时，默认行为保持不变。本特性有意让同一份旧配置升级后进入真值表第 1 行，派生记忆会和
  以前不同；即使切回 `sequential`，也会多出等锁超时和锁后端故障两种失败情形（见决策 7）。
  这一偏离要在变更说明里写明，并用“旧配置升级回归”测试兜底（见“验证”）。
- **升级保证的可观察范围**（“其余行为一致”具体指什么）：
  - **保证不变**：
    - 每条输入都有且只有一个对齐的 outcome，`error` / `error_type` / Skipped 的语义不变，
      回执与实际落盘状态一致；
    - 授权：每条恰好执行一次 PEP，顺序符合决策 1，拒绝时的处理和审计不变；
    - 失败策略：`continue_on_error` 的语义不变（包括引擎降级的情况，见第 5 行）；
    - 已承诺的顺序：同一 `(scope, stream_id)` 内按输入顺序提交；`continue_on_error=False`
      和 `sequential` 下按全局输入顺序提交。
  - **允许变化**（必须写进变更说明）：
    1. infer 合并抽取后的派生记忆内容（第 1 行）；
    2. 锁带来的两种新增失败：`LockTimeoutError` 和锁后端不可用（所有行）；
    3. 插件看到的调用批次和跨组执行顺序：`classifier` / `index_builder` / `evolver` 收到的
       是一批 unit，而不是一条；`continue_on_error=True` 时，不同组之间并发、交错执行，
       不再按全局输入顺序。调用方能看到的回执语义不受影响。
- 两个开关分别控制并发和合并，**而且合并必须在分组的前提下才生效**：
  - **编排器**（`write.orchestrator`：`grouped` / `sequential`，缺省 `grouped`）：控制
    是否分组、组间是否并发；
  - **合并抽取**（`merge_extract`：`on` / `off`，缺省 `on`）：只在 `grouped` 下生效，
    控制同一批里的 infer 条目是否一起抽取。部署级在配置里设置；调用级在批级
    `system_metadata` 里放同名键，与部署级取**逻辑与**（只能关、不能开），值为 `"false"`
    时这一批改为逐条抽取。单项上的同名键按批级处理并剥离，不参与判定、不落盘。这个调用级
    开关的处理方式和 `infer` / `middle` / `procedural` 一样：在写入边界剥除，不写入存储，
    并登记为内核保留键（`KERNEL_SYSTEM_METADATA_KEYS`）。
- `continue_on_error=False` 是第三个决定因素，同样列在下表里。

| # | `continue_on_error` | 编排器 | `merge_extract` | 分组 | 组间并发 | infer 抽取 | 直写入库 | infer 加锁 | 与现行为 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `True` | `grouped` | `on`（**默认**） | 是 | 是 | 每批合并一次 | 每批合并一次 | 是 | infer 派生结果不同 |
| 2 | `True` | `grouped` | `off` | 是 | 是 | 逐条（组内按顺序） | 每批合并一次 | 是 | 结果一致 |
| 3 | `False` | `grouped` | 任意（忽略） | 否 | 否 | 逐条（按输入顺序） | 逐条 | 是 | 结果一致 |
| 4 | 任意 | `sequential` | 任意（忽略） | 否 | 否 | 逐条（按输入顺序） | 逐条 | 是 | 结果和执行顺序一致；多了锁等待和锁超时 |
| 5 | 沿用调用方 | `grouped`，但引擎 `supports_grouped_write=False` | 任意（忽略） | 否 | 否 | 逐条（按输入顺序） | 逐条 | 是 | 结果一致；`continue_on_error` 语义不变 |

  - **加锁不由编排器控制**：决策 7 的 per-user 锁对表中各行一致生效，编排器的 target 和
    `continue_on_error` 的取值都不影响是否加锁。锁只增加等待，正常情况下不改变结果；
    锁超时的情形见本节末尾的条件说明。
  - **引擎能力降级单列为第 5 行**：只关闭分组、并发和合并，错误处理策略沿用调用方传入的
    `continue_on_error`。`True` 时，失败条目之后的条目照常执行；`False` 时，失败条目之后的
    条目标为 Skipped。降级时会输出可观测信号（见决策 8）。
  - 单条 `add_async` 在任何一行下，infer 写入都会加锁。
  - 第 2~5 行的“结果一致”是有条件的：**在没有锁竞争的情况下**，派生记忆和现在的逐条模式
    相同，执行时机可能不同（并发、等锁）。等锁超时时，会多出一种现在不存在的结果：相应条目
    因 `LockTimeoutError` 失败（决策 7）。

### 4. 分组与切批

- **分组键**：`(载体 scope, stream_id)`。载体 scope 指 API 层解析出落点之后的 `item.scope`。
  没传 `stream_id` 时，整个 scope 算一组。不同 stream 不放进同一次抽取，避免两段无关的
  对话互相干扰。
- **组内顺序**：按输入顺序（沿用 api/F03 决策 5，默认不按 `sequence` 重新排序）。
- **哪些路径合并**：

  | 路径 | 处理方式 |
  |---|---|
  | `infer=true`（不含 middle、procedural），且 `merge_extract=on` | 合并：一批只调一次 `evolve(batch, EXTRACT)` |
  | `infer=true`，且 `merge_extract=off` | 逐条 evolve，但仍在组内顺序执行 |
  | `infer=false`（直写） | 合并：一次 `classify`，一次 `index_builder.build`（前提是 IndexBuilder 能给出逐条结果，见下） |
  | `procedural=true` | 不合并，逐条执行；它的契约是“每轮产出 1 条过程记忆” |
  | `infer=true` + `middle=true` | 不合并，逐条执行；后台 Job 本来就会批量处理 |
  | continuity 接续项 | 不经过编排器，保留现在按输入顺序冲刷的逻辑 |

  组内按输入顺序，把相邻的同类条目合成一段；遇到不同类的条目，先把当前段写完，再处理下一段。
  这和现有 `flush_ordinary` / `flush_continuity` 的保序做法一致。
- **直写合并需要逐条结果**：批量 `index_builder.build(units)` 可能在写完前几条之后抛错。
  如果整批都标为失败，回执就和实际落盘状态对不上。为此在 S05 的 IndexBuilder 契约里新增
  可选能力（暂名 `grouped_build`，非抽象，默认不支持）：声明支持的实现，必须返回每个 unit
  是否已提交的逐条结果；部分失败时，已提交的条目回执为成功，未提交的回执为失败。实现没有
  声明这个能力时，直写段在执行前就改为逐条 build（仍在组内执行，仍可组间并发），不合并。
  内置 IndexBuilder 需要补齐这项能力。
- **批量路径的 Schema 反向索引按“已确认正排提交”维护**：`build_grouped` 返回的
  `COMMITTED` 只表示正排（真源）已确认提交，检索索引仍可能附带 `error`；它与逐条
  `build(mode=ALL)` 的成员语义一致。`SchemaPropertyIndexingBuilder` 必须对已提交单元
  `upsert`，不能调用 `mark_forward_only`（后者会把单元排除出反向索引，
  `CONFLICT_CONSOLIDATE` 的实体展开就拿不到成员）。
  - 反向索引是派生数据：`upsert` 失败（例如首次全量扫描遇到无法解码的旧记录）**不得**
    抛给调用方、也不得改写 delegate 已给出的正排状态。但也不能把部分成功报成完整成功：
    受影响的 unit 保持 `COMMITTED`、同时附上索引维护错误（S09 §12），并按 scope
    **独立**尝试失效；失效本身失败时同样附错误、继续处理其余 scope。错误按**每条回执**
    单独包装（原异常挂在 `__cause__` 上），不共享异常实例，避免后处理串到别的 unit。
  - **失效先写修复标记再删 watermark**：`invalidate_scope` 先写 `needs-repair`，读侧见到
    该标记一律按未建索引回退真源；随后才删 watermark。这样即使 watermark 删除失败（双
    故障），读取方也不会信任缺成员的旧视图。前缀扫除与 `_commit_scope` 都不得提前删
    `needs-repair`：扫除跳过该键，新 watermark 写成功后才清标记。恢复入口分两类：后端
    故障恢复后重跑 `invalidate_scope`（或下一次成功写入重建），也可走 `rebuild`；旧版本
    bug 写坏的数据（watermark=complete、成员为空、exclusion 残留）不会被运行时失效触发，
    需对受影响 scope 执行 `python -m deploy.migration.backfill repair-schema-index --org ...`
    （维护窗口内执行；内部 `SchemaPropertyIndex.repair_scope()` 清除该 scope 的**全部**
    exclusion，写 needs-repair 失效后立即按真源重建，重建失败则保留标记并报告）。不按
    生命周期筛选：正常的归档/取代走 `update(FORWARD_ONLY)`，不写 exclusion，且已归档成员
    本就应留在反向索引里供历史回看；只恢复 ACTIVE 会让「被误排除、后又归档」的成员永久
    缺席。冲突巩固在索引为空时也会回退真源扫描；恢复前读取方始终走真源核对。
  - 同批若有 `UNKNOWN` 条目，其正排可能已经落盘但没有进入反向索引：处理完已确认成员后
    让相关 scope 逐个失效，避免查询把缺成员的旧索引当成完整结果。
  - **回执对齐由消费方统一执行**：`align_unit_receipts` 在校验后返回与输入等长、顺序
    一致的列表，schema 包装器、隐私包装器、引擎的 outcome 分配与摄入记账都必须使用
    对齐后的**位置**，不能再按裸 `unit_id` 建表——同名 id 折叠会把一个 scope 的成功
    套给另一个 scope。
  - 对齐规则：数量一致且位置 id 匹配时按位置使用；数量不符时只采信**一一对应**的 ID：
    同一 id 出现多个候选、或同一 id 返回多份回执（无论顺序）都全部 `UNKNOWN` 并告警，
    绝不猜归属，也绝不让回执顺序决定结果。
  - 隐私包装器的 `refresh_revision` 失败同样按派生数据故障处理：该 unit 保持
    `COMMITTED`，后续 unit 继续处理；若回执已有索引错误（例如检索索引部分失败），
    为该条生成**单条复合错误**（原错误挂在 `__cause__` 上），不能原地 `add_note`——
    内置 Hybrid 会把同一异常实例放进多条回执，原地修改会串到别的 unit。
  - **`build_grouped` 抛错一律按 `UNKNOWN`**（取消/`_raise_if_cancelled` 除外）：调用方
    无法确认哪些 unit 已落盘，全部按 `WriteIncompleteError` + `recover_then_retry` 返回，
    不得当普通失败、不得自动重放；这与「正常返回但缺某项回执」的恢复语义一致。代价是
    原始异常类型只保留在消息与 `__cause__` 上（`outcome.error_obj` 是 `WriteIncompleteError`），
    调用方按「未知」而非原类型分支。
- **从 unit 到输入条目的错误契约**：一条输入可能产出多个 unit，builder 按 unit 报告状态，
  但 API 按输入条目给回执，所以需要在两层分别约定：
  - **S05（IndexBuilder 部分失败时的回传）**：每个 unit 的状态取 `committed` / `failed` /
    `unknown` 三种之一。抛错之前已经确认写入的记为 `committed`；确认没有写入的记为 `failed`；
    抛错发生在这个 unit 的写入过程中、无法确认是否已写入的记为 `unknown`。即使整次 build
    抛错，也必须返回已知的这部分结果，不能只抛异常。
  - **S02（汇总到同一条 outcome 的规则）**：
    | 这条输入产出的 unit 状态 | `outcome.units` | `error` / `error_type` |
    |---|---|---|
    | 全部 `committed` | 全部 unit | 空 |
    | 部分 `committed`，其余 `failed`，没有 `unknown` | 只放已提交的 unit | 失败原因，`error_type` 取 builder 抛出的领域异常类型 |
    | 全部 `failed` | 空 | 失败原因 |
    | 存在 `unknown` | 只放确认已提交的 unit | `error_type="WriteIncompleteError"`，`error` 里列出状态未知的 unit id |

    “`units` 非空但 `error` 也非空”是新增的组合，表示部分提交，需要写进 S02。调用方判断一条
    是否完全成功，看的是 `error` 是否为空，不是 `units` 是否非空。
  - 没有声明 `grouped_build` 时，按逐条 build 执行：一条输入内部如果部分失败，行为和现在
    一样（整条标为失败）。
- **切批**：按 `max_items`（条数上限）和 `max_tokens`（token 上限）切分，哪个先到按哪个切。
  不额外调 LLM 判断内容是否连续（control/F06 的切批方式），因为那会多花 token。
- 两个上限由用户配置。批越大越省 token，但抽取质量的风险也越高，这个取舍交给部署方。
  默认值由实现者根据 A/B 评测确定（见“验证”）。

### 5. Engine 新增批量原语 `write_group`（非抽象方法）

- `MemoryEngine` 新增批量写入原语（暂名 `write_group`）和能力属性 `supports_grouped_write`。
  输入是同一载体 scope、同一路径的若干条目，输出与输入逐条对齐的结果。具体签名写进 S03。
- **不加成抽象方法**（S09 不变量 1、14）：`MemoryEngine` 目前已有 20 多个抽象方法，再加一个
  会让仓库外的 EngineProducer 实现直接无法实例化。做法和同文件里
  `supports_exact_metadata_lookup`（默认 `False`）、`video_ingest_preflight`（默认 `None`）
  一样：`write_group` 默认实现退化为逐条调 `write`，`supports_grouped_write` 默认为 `False`。
- **能力声明由实际装配的组件组合决定**（S09 不变量 6、7）：合并抽取要能安全地做“失败退回
  逐条”和“按来源归属”，依赖 Evolver 的具体行为，不能只由引擎声明。为此在 S05 的 Evolver
  契约中新增一个可选能力（暂名 `grouped_extract`，非抽象方法，默认不支持），声明支持的
  Evolver 必须做到两点：
  1. **可重试错误**：派生记忆落盘之前发生的失败，要抛出约定的可重试异常类型。抛出这个类型
     就意味着“本批没有任何派生记忆提交”，可以安全地逐条重放；`/messages/` 原文要按 unit id
     幂等写入。其他异常一律视为“提交状态未知”。
  2. **逐项来源回执**：返回结果中要带上每条派生记忆在去重前对应的候选来源（原始 unit id），
     UPDATE / SUPERSEDE 也必须带上，不能只依赖最终返回对象的 `provenance`。

  **能力分两层判断**：
  - **引擎层**：`supports_grouped_write` 只回答“本批能不能分组编排”。内置引擎只在组件解析
    不出时才返回 `False`，此时编排器按第 5 行执行，并记录 `engine_degraded`。这里的“解析
    不出”指带 pipeline 却取不到 profile。
  - **组件层**：Evolver 和 IndexBuilder 是否具备批量能力，不影响能否分组，只决定段内的执行
    方式，在执行该段之前、依据本次 `write_binding` 选出的组件判断：
    - Evolver 未声明 `grouped_extract`：这个 infer 段改为逐条抽取，相当于真值表第 2 行的 infer
      处理方式；
    - IndexBuilder 未声明 `grouped_build`：这个直写段改为逐条 build。

    这两种情况都仍然分组、组间并发、加锁，并输出可观测信号；不能先合并、等失败了再补救。
    如果像早期实现那样，组件缺能力就把整批推到逐条执行，那么文档栈（`DocumentIndexBuilder`
    不支持批量 build）会因为构建器缺少能力而连合并抽取和组间并发也一起失去。而 infer 段的
    派生记忆由 Evolver 自己的构建器落盘，根本不经过引擎构建器。内置的
  `OrchestratingEvolver` 需要补齐这两项能力：现有的 `_add_messages` 冲突转 update，以及
  `_persist` 之前不提交派生记忆，是实现第 1 项的基础。
  内置的三个引擎会覆盖 `write_group`。
- 语义：
  1. 每条单独构造 `RawPayload`，单独调 `ingestor.ingest([payload])`，收集产出的 unit，
     同时记下“原始 unit id → 输入下标”的对应关系。不需要改 Ingestor 接口，来源对应关系
     天然就有。
  2. infer 段：整批调用一次 `evolver.evolve(units, EXTRACT)`。
  3. 直写段：一次 `classify`，一次 `index_builder.build`。
  4. 按决策 6 把派生记忆分回各条。
- **公共实现的切分方式**：把单条 `write` 拆成两个阶段。
  - `prepare`：每个引擎各自实现。负责构造 `RawPayload`、ingest，以及 ingest 前后的
    引擎特有处理：metadata 规整（CloudEngine 转成字符串）、`event_time` 覆盖、补标签、
    透传 `session_id`、打 `middle` 标记、判定 `write_path`、用 `write_binding` 选组件。
    产出“处理好的 unit + 路径 + 组件”。
  - `execute`：放在 `engine_impl/` 的公共实现里。负责 evolve、classify、build，以及来源
    归属、trace 记录和 ingest cancellation 的记账。
  - 引擎特有的执行差异（例如 AgentEngine 文档模式下的读写分流）通过 `execute` 接收的
    组件和钩子注入，不复制主流程。
  - 单条 `write` 改为“`prepare` + 只有一条的 `execute`”，和批量路径共用一套代码，避免
    以后维护两份实现。
  - 同一批里 `write_binding` 选出的组件不一样时，按组件再拆成多段。
- 原有的 `write` 和 `batch_write` 保留：`write` 给单条写和不合并的路径用，`batch_write`
  给 `sequential` 用。

### 6. 派生结果按来源分回各条（方案 1）

- 根据决策 5 中 Evolver 返回的**逐项来源回执**，把派生记忆分回来源条目。ingest 产出的
  原始 unit id 对应输入下标；回执中一条派生记忆指向哪些原始 unit，它就出现在哪些条目的
  `outcome.units` 里。由多条来源合成的记忆，会同时出现在这几条里。可靠的来源映射是开启
  合并的前提（不声明 `grouped_extract` 就不合并）。
- **运行时映射无效时整批共享**（用户决定）：即使 Evolver 声明了能力，运行时仍可能出现某条
  派生记忆的回执为空、或指向本批以外的 unit 的情况（例如 LLM 漏标来源）。这时，这条记忆
  出现在本批每个条目的 `units` 里。这是唯一允许整批共享的情况，并且必须留下可观测信号
  （见决策 8 的“可观测性”）。这个信号同时也用来评估抽取器的来源标注质量。
- 某条输入没有产生任何派生记忆（例如去重后判为 NOOP）时，`units=[]`，状态为成功，
  和现在逐条 infer 的行为一致。
- 现状：`KeywordExtractor` 写 `provenance=[unit.id]`；`EntitySchemaExtractor` 让 LLM 输出
  `source_unit_ids`，再写入 provenance。对于 UPDATE / SUPERSEDE，返回的是已有记忆，它的
  provenance 不一定包含本批的来源，所以**归属必须按去重之前候选记忆的 provenance 计算**，
  不能只看最后返回的对象。

### 7. 按用户加写入锁，只锁 infer 路径

- **为什么要锁**：infer 写入要依次“召回已有记忆 → LLM 判断 → 写入”，耗时几秒。同一用户的
  两个并发请求如果都在对方写入前完成了召回，就会各自 ADD，产生重复记忆；如果同时 UPDATE
  同一条记忆，后写的会覆盖先写的。同一批内部已经有保护（`orchestrating_evolver.py` 在
  ADD 之前调 `_recheck_candidate` 再查一次），但两个并发请求之间没有。
- **锁的粒度和去重粒度一致**：key 为 `(org, space, user)`。`dedup.same_scope` 按这三项
  比较，不同 session、不同 agent 的写入也会互相去重，所以锁里不能包含 session 和 agent。
  调 `LockProvider.build_key` 时传入 agent、session 为空的 Scope，name 用固定值
  （建议 `infer_write`）。
- **只锁 infer 路径**：直写、procedural、middle 在写入时都不做去重，所以不加锁。否则在
  routing 把 scope 归一到空间级时，共享空间的所有写入都会排成一队。
- **持锁单位：同一锁 key 的连续 infer 段**。按输入顺序，把相邻、锁 key 相同（都是同一个
  `(org, space, user)`）的 infer 条目算作一段，锁的获取和释放都以段为单位。这条规则对决策 3
  真值表的各行都适用：
  - 第 1、2 行（`grouped`）：一个组的条目一定属于同一个锁 key，组内每一批就是一段，每批开始时
    获取、结束时释放，批与批之间让其他请求有机会插进来；
  - 第 3、4、5 行（逐条执行）：输入里相邻、锁 key 相同的 infer 条目是一段，持锁逐条执行完这段
    再释放。遇到直写条目或换了锁 key，就先释放当前锁，再处理后面的条目。
  - 因此一个批量请求可以同时涉及多个用户：每段各拿自己那个用户的锁，同一时刻只持有一把锁，
    不会因为锁的顺序问题产生死锁。
  - **对吞吐的影响**：分组键是 `(scope, stream_id)`，锁 key 是 `(org, space, user)`。同一
    用户的不同 session 或不同 stream 属于不同的组，但它们抢的是同一把锁，所以它们的 infer
    批次实际上是串行执行的。单用户大批量导入时，“组间并发 4”的有效并发只有 1；并发收益
    来自不同用户之间，以及直写条目。估算吞吐时要按用户数计算，不能按组数。
- **释放纪律**：获取锁失败、段内某一条失败需要停止（`continue_on_error=False` 的
  fail-fast）、被取消，这三种情况都必须先释放当前持有的锁，再停止或抛出。取消路径的做法
  见决策 8，要等线程执行完才能释放。
- **锁配置的来源与保障边界**（S07、S09 不变量 3、10）：
  - 复用 `common/lock` 端口，`LockProducer` 仍然“不设默认实现”，代码里不兜底。
  - `config/defaults.py` 是内核基线：`build_kernel` 不传 config 时直接用它装配，传了 config
    则把用户配置合并覆盖在它上面。它本来就是一套离线、进程内的栈，KV、向量、审计等也都是
    进程内实现。本特性在基线里**显式声明 `lock.default` 为 `memory`**，和其他进程内后端
    同等对待，保证默认的 `grouped` 开箱即可装配，现有不写 lock 的配置升级后也不会失败。
  - **装配期失败只覆盖一种情况**：用户配置显式删除或置空了锁配置。不论编排器是哪种
    target，infer 写入都需要锁，所以此时装配直接失败，报错里指出要配置的锁配置项。正常继承基线的配置永远都会
    有一把 memory 锁，所以**多实例部署的跨实例互斥不靠装配期强制保证，而靠部署文档约定和
    部署评审**：多实例部署必须把 `lock.default` 覆盖为 `redis`。
  - **用部署配置声明“是否多实例”，用锁的能力判断是否满足**：新增 S08 配置项
    `deployment.multi_instance`，默认 `false`。“是否多实例”是部署的属性，不是某个存储实现的
    属性，例如 sqlite 放在共享 NFS 上也可能被多个实例共用，所以 KV 端口不加能力声明。
    “锁能在多大范围内互斥”是锁自己的属性：在 S07 的 `LockProvider` 契约中新增能力声明
    （暂名 `mutex_scope`，取值 `process` / `cross_instance`，非抽象，默认取保守值 `process`），
    `redis` 实现声明 `cross_instance`，`memory` 实现保持 `process`。当
    `deployment.multi_instance=true`，而实际装配的锁的 `mutex_scope` 不是 `cross_instance`
    时，装配阶段直接失败。判断看的是能力声明，不看 target 名字（S09 不变量 9）：第三方注册的
    进程内锁换个名字也通不过检查，任何声明了跨实例互斥的实现都可以替代 redis。没有声明多实例
    时（默认值），系统无法判断是否多实例，仍然按部署约定处理，见已知遗留 5。
  - `InMemoryLockProvider` 的“检查并设置”目前只在单个事件循环内是原子的。同步 `add` 每次
    通过 `asyncio.run` 新建一个事件循环，HTTP 又是多线程，所以要加一把线程锁保护这一步，
    让它跨事件循环、跨线程也是原子的。这是对 common/F06 交付的实现契约的修改，S07 和
    common/F06 要同步更新。
- **锁不能单独保证正确性**：`LockProvider` 是基于租约的协调机制，不是共识算法。租约过期
  （例如 GC 停顿或网络分区）时，两个持有者可能同时进入临界区。锁失效时最坏的结果是：
  同一用户产生一对重复记忆，或者一次 UPDATE 被覆盖丢失。数据不会损坏，也不会越权。
  第二道防线是：
  1. 去重本身：下一次写入或整合时，`_recheck_candidate` 和去重召回仍然会看到这条重复的
     记忆，并作 NOOP 或 UPDATE 处理；
  2. 后台整合（`CONSOLIDATE`）可以合并漏掉的重复记忆。

  所以本特性的承诺是“锁正常时不会产生新的重复”，而不是“绝不重复”。
- **锁超时会让写入失败，这是有意的**：等锁超时后，这一段的条目以 `LockTimeoutError` 标为
  失败（属于领域异常，逐条记录）。不提供“拿不到锁就跳过、继续写”的选项，因为那正是
  S09 不变量 12 禁止的静默降级。等待超时和租约时长可以配置，默认值必须明显大于一批抽取的
  典型耗时；持锁期间由 `guard` 自动续租。超时会计入可观测指标（见决策 8）。这是高负载下
  新增的一种失败模式，要在 S02 的错误语义里写明。

### 8. 并发、`continue_on_error`、失败隔离、取消与可观测性

- `continue_on_error=True`（默认）：组与组之间并发，并发上限可配置；组内各批按顺序执行。
- `continue_on_error=False`：不并发，退回逐条顺序执行，也就是现在的行为，保证“失败点之后
  没有副作用”，对应决策 3 真值表第 3 行。这时不做合并抽取。`stopped_index` 和 Skipped
  的处理仍留在 API 层的 `batch_add_async`，不往下移。`grouped` 收到 `False` 时，按输入
  顺序逐条调用 `engine.write`，并且 infer 条目照样加锁，遇到第一个失败就停止（复用
  `engine.batch_write` 的 fail-fast 循环语义）。行为和第 4 行（`sequential`）相同。
- **失败隔离**：一批抽取失败后，按 Evolver 契约（决策 5）区分两种情况：
  - **抛出约定的可重试异常**（表示没有任何派生记忆提交）：这一批退回逐条抽取，只有逐条
    抽取仍然失败的条目才标为失败，其他条目正常返回。退回时直接复用已经 ingest 好的 unit，
    不重新 ingest，每个 unit 单独重跑一次 `evolve([unit], EXTRACT)`。原文不会重复写入，
    因为契约要求 `/messages/` 按 unit id 幂等写入（内置实现由 `_add_messages` 的
    冲突转 update 保证）。
  - **其他异常**（提交状态未知，例如去重或落盘中途出错）：**禁止自动重放**，整批标为失败
    并写明原因（见已知遗留 6），避免把已经部分落盘的派生记忆再写一遍。
  - 退回逐条后，`/messages/` 里已经有本批的全部原文，所以单条抽取时看到的上下文会多出
    批内后面的条目。这对指代消解没有坏处，可以接受。
- **取消**：调用方取消时，已经交给线程的任务没法中断。做法沿用 `hierarchy_job` 的规则：
  先等线程跑完，释放锁，再把 `CancelledError` 抛给调用方。这样调用方收到取消时，后台不会
  还有写入在悄悄进行。
- 同步入口 `add` / `batch_add` 仍然通过 `asyncio.run` 转调异步实现，不受影响。执行器和锁
  都归装配上下文所有，不绑定某一个事件循环。
- **可观测性**（S09 不变量 12）：以下降级事件都要同时输出 WARNING 日志、trace 事件，并累计
  计数器，标明 scope、批大小和原因：
  - 合并抽取失败，退回逐条抽取；
  - 来源缺失，整批共享；
  - 等锁超时；
  - 引擎不支持分组编排，退化为真值表第 5 行（逐条执行，仍然加锁，`continue_on_error` 沿用调用方）；
  - IndexBuilder 未声明 `grouped_build`，该直写段在段内改为逐条 build；
  - 本次选出的 Evolver 未声明 `grouped_extract`，该 infer 段在段内改为逐条抽取；
  - 合并失败但提交状态未知，禁止自动重放，整批标为失败。

  不在 `BatchWriteOutcome` 里加新字段，以免改动公开返回结构；这些信号由日志和 trace 提供。
  计数器要能从 `health` 或 trace 查到。
  - **记录位置**：
    - `engine_degraded` 由编排器记录：引擎通过 `grouped_write_blocker(items)` 给出原因，
      编排器每批最多记一条；
    - `build_unsupported` 和 `extract_unsupported` 由引擎在段内记录，每段最多一条，只在
      段内条目数大于 1 时记录。单条段本来就不会合并，不算降级。
  - 单条写入和 `continue_on_error=False` 本来就逐条执行，不算降级，不记录。
  - 引擎和编排器共用同一个装配上下文里的计数器，从编排器的 `degrade_counters()` 就能查到
    全链路的计数。

### 9. 线程安全

以前在单线程事件循环里依次执行的对象，现在会被多个线程同时访问。实现时要逐个审查，并补上
并发测试：

- privacy 的 `_StageCapture`：一次 `batch_add_async` 里多个组并发时，会有多个线程同时写它；
- `IngestCancellation` 状态对象（`write_started`、`unit_ids` 的读、改、写）；
- Evolver、Extractor、Dedup、Router、Classifier 的内部缓存和可变成员。control/F06 的
  `middle_concurrency>1` 只是假设 Evolver 线程安全，从没验证过；
- `AuditLogger`：多个线程同时写审计，还要维护审计完整性链（chained_hmac 的有界 CAS）；
- `PrivacyProtection` 的 provider：调 `preview` 的地方会分散在多个线程里，而 provider 的
  实现不一定是无状态的；
- `InMemoryLockProvider`（决策 7），以及 `in_memory_kv_store` 等内存存储（单测和本地开发
  会用到）；
- trace 上下文在线程之间的传递。

sqlite 类存储已经用 `check_same_thread=False` 加锁的方式实现，并在 HTTP 多线程场景下验证过。

### 10. 需要同步修改的文档

- **api/F01** 决策 4：改掉“`add_async` 是直通引擎的真协程”这句描述。
- **api/F03**：决策 4 后面补一句，说明这里的串行保序已被 control/F10 取代；已知遗留 1 标为已解决。
- **S02**：
  - 新增调用级开关键 `merge_extract`；
  - 写明 `units` 按来源归属，来源缺失时整批共享；
  - 写明一条输入产出的多个 unit 状态如何汇总成一条 outcome（决策 4 的表），包括“`units` 非空但 `error` 也非空”这种部分提交的组合；
  - 写明 `continue_on_error=False` 时退回顺序执行；
  - 说明合并模式下派生结果可能和逐条模式不同；
  - 错误语义里补上 `LockTimeoutError`；
  - 更新修订日期。
- **S03**：
  - 新增 `WriteOrchestrator`、`BlockingExecutor` 两个端口（含 target 名、缺省值、
    health 和 close 的语义）；
  - 执行器的重入规则（决策 1 的按通道区分表），以及“需要加锁的写入只能从宿主事件循环或 io
    工作线程进入，从 llm 工作线程进入时拒绝”的约束；
  - Engine 新增非抽象方法 `write_group` 和属性 `supports_grouped_write`；
  - 更新 `batch_write` 表格行的描述；
  - 更新修订日期。
- **S05**：IndexBuilder 契约新增可选能力 `grouped_build`（逐条提交结果）；Evolver 契约新增可选能力 `grouped_extract`（非抽象，默认不支持），包括：
  约定的可重试异常类型及其“未提交任何派生记忆”的语义、`/messages/` 按 unit id 幂等写入的
  要求、逐项来源回执的数据结构；更新修订日期。
- **S07**：LockProvider 一节，“只交付原语、不在业务路径上加锁”改为“首个业务消费方是写入
  编排（control/F10）”；“不设默认实现”保持不变；新增能力声明 `mutex_scope`
  （`process` / `cross_instance`，非抽象，默认 `process`）；补充 memory 实现跨事件循环、
  跨线程的原子性要求；更新修订日期。
- **`jiuwen_memory/construction/AGENTS.md`**：写明 `OrchestratingEvolver` 声明
  `grouped_extract` 及其满足方式。
- **S08**：新增配置项 `write.orchestrator`、`merge_extract`、批次上限、组间并发上限、
  执行器线程数、锁等待超时和租约、`deployment.multi_instance`；更新修订日期。
- **common/F06**：背景里“本特性不改动任何业务路径”保留作历史描述，补一句说明首个消费方
  已由 control/F10 引入；注明 memory 实现的线程安全修改。
- **`jiuwen_memory/common/AGENTS.md`**：模块地图里 `lock/` 一行，以及铁律“锁只交付原语，
  本层不在任何业务路径上加锁”，改为：common 层本身仍不加锁，消费方是 control 的写入编排。
- **`jiuwen_memory/api/AGENTS.md`**：铁律 5“写入同步/异步桥接”补充：前置步骤通过执行器
  移出事件循环，并按决策 1 的顺序执行；批量写入委托编排器。
- **`jiuwen_memory/control/AGENTS.md`**：模块地图里加上编排器和执行器；行为铁律 13“批量写入保序且不鉴权……不得在 Engine 内并发提交”改为：Engine 内部仍不并发，并发和分组由 `WriteOrchestrator` 负责，组内按输入顺序执行。
- 新增的配置项写进 `config/defaults.py`。基线里显式声明 `lock.default` 为 `memory`。
- **部署文档**：`docs/zh/安装指导/部署方式概览.md`、`docs/zh/安装指导/容器化部署.md`，以及
  对应的英文版 `docs/en/Installation Guide/Deployment Overview.md`、
  `docs/en/Installation Guide/Containerized Deployment.md`，写明以下三点：
  - 多实例部署必须把 `lock.default` 覆盖为 `redis`，并设置 `deployment.multi_instance=true`；
  - 锁后端不可用时，infer 写入会整体失败；
  - 合并抽取默认开启，以及关闭它的方法。

## 拒绝的方案

- **方案 B：每个引擎在自己的 `batch_write` 里实现分组并发**：同样的逻辑要写三份，编排策略
  和引擎绑死，没法单独替换，不满足插件化要求。
- **方案 C：每个 scope 一个攒批队列，跨请求合并**：最省 token，但有几个问题：单条写入要多等
  一个时间窗口；需要常驻事件循环，而同步 `add` 通过 `asyncio.run` 建的临时循环一结束，
  后台处理任务也跟着没了；取消、关闭、背压都更复杂。以后可以作为 `WriteOrchestrator` 的
  另一个实现接进来，不需要推翻现在的方案。
- **直接 `asyncio.gather` 逐条并发**：同一 stream 内的上下文顺序会乱，去重会出现竞态
  （这正是 api/F03 决策 4 当初选择串行的理由），而且不省 token。
- **把 LLM 客户端改成原生 async**：要改所有算子的同步接口，改动太大；用线程池已经能做到
  不阻塞事件循环。
- **派生结果一律整批共享，或改成只在批级返回**：一律整批共享，单条结果就没有意义了；改成
  批级返回，会改变 `BatchWriteOutcome.units` 的公开契约。整批共享只在来源缺失时作为兜底。
- **来源缺失时只归到第一个条目**：不会放大错误，但会把结果错误地记到某一条上，调用方会
  误以为别的条目什么都没产生。最终选择整批共享，并要求带可观测信号。
- **首版默认用 `sequential`，以后再改默认**：用户明确要求合并抽取默认开启。兼容的办法是
  提供两个开关，并在文档和变更说明里写明这项行为变化。
- **没配置锁时，代码自动用 memory 锁**：违反 S07“不设默认实现”的规定。单实例部署时看起来
  没问题，多实例部署时锁会悄悄失效，这正是 S09 不变量 10、12 禁止的情况。
- **从 `defaults.py` 删掉 lock，把 memory 锁只放在 OFFLINE profile 里，靠装配期强制**：
  `build_kernel()` 不传 config 时直接用 `defaults.py`，默认编排器又是 `grouped`，这样会让
  不传 config 的 SDK 用法和所有没写 lock 的现有配置升级后都在装配时失败，和“合并抽取默认
  开启、升级不需要改配置”冲突。改用“基线显式声明 memory 锁 + 部署约定 + 声明多实例时强制检查”，
  并在已知遗留里写明保障到哪一步为止。
- **“编排器”和“合并抽取”两个开关完全独立（`sequential` 下仍分组合并）**：这样
  `sequential` 就不再是“和现在一样”的兼容开关。最终定为合并只在 `grouped` 下生效，
  默认行为只以决策 3 的真值表为准。
- **`sequential` 不加锁，做到“一切和现在完全一样”**：用户已决定新增 per-user 锁，用来消除
  并发 infer 写入产生的重复记忆。如果兼容模式不加锁，选择兼容模式的部署就会留着这个竞态。
  而锁只增加等待，正常情况下不改变结果；锁超时和锁后端不可用是新增的失败情形，已在决策 3
  和已知遗留 8 中写明。所以所有模式都加锁。
- **给 KV 端口增加“状态是否跨实例共享”的能力声明，用来判断 memory 锁该不该告警**：
  “是否多实例”是部署的属性，不是存储实现的属性，例如 sqlite 放在共享 NFS 上也可能被多个实例共用。
  而且这样要修改 S06、storage/AGENTS.md，以及全部五个 KV 实现和加密包装。改用部署配置
  `deployment.multi_instance`。
- **每批固定拿一次锁**：一个批量请求可以按条覆盖 scope，同一批里可能有多个用户，取不出
  单一的锁 key；逐条执行的模式下也没有“批”这个概念。改为以“同一锁 key 的连续 infer 段”
  为单位加锁。
- **把 `write_group` 加成抽象方法**：仓库外的引擎插件会直接无法实例化。
- **按完整 scope（含 session）加锁**：去重是跨 session 的，这样锁不住真正会冲突的写入。
- **所有写入路径都加锁**：routing 把 scope 归一到空间级后，共享空间的所有写入都会排成一队。
- **拿不到锁就跳过锁继续写，只记一条审计**：这是静默降级，违反 S09 不变量 12。
- **用 LLM 判断内容是否连续来切批**（control/F06 的做法）：要额外花 token，和省 token 的目标矛盾。
- **一批失败就整批标失败**、**二分重试**：前者违反反目标；后者比逐条退回复杂，收益有限。
- **`continue_on_error=False` 时也合并抽取**：用户明确要求这个模式下退回逐条顺序执行，
  这样语义最简单。
- **在 `BatchWriteOutcome` 里加降级标记字段**：要改公开返回结构；用日志、trace 和计数器已经
  满足可观测要求。
- **旧配置沿用现有策略，新配置显式启用分组和锁；或者提供版本化的配置迁移**：评审依据 S09
  的“默认配置回归”提出了这个建议。它与用户“合并抽取默认开启”的决定冲突，按用户决定不采纳。
  改为在决策 3 中写明这是一次明确的例外，并用“旧配置升级回归”测试限定变化范围。
- **来源映射无效时，在 S02 中定义批级结果，让调用方能识别“整批共同产生、无法逐条归属”**：
  评审指出，日志和计数器无法让调用方从成功回执中识别误归属。用户已决定“整批共享”，并且不
  改动公开返回结构，所以不采纳。缓解办法是把 `grouped_extract` 的逐项来源回执作为开启合并的
  前提，尽量减少映射无效的情况。
- **只看内置 Evolver 的实现细节（`_add_messages`、`_persist` 的先后顺序）来决定能否安全
  重试**：替换进来的 Evolver 没有同等保证，通不过 S09 的替换测试。改为在 S05 定义
  `grouped_extract` 能力，按实际装配的组件组合判断。
- **执行器不支持重入，让 continuity 外层的调用不占用 IO 池**：需要改造 continuity writer 里
  `asyncio.run` 的同步回调结构，改动面大；而且以后出现其他嵌套桥接时还会再踩同样的坑。
  改为在执行器层统一规定：从本执行器工作线程提交的任务直接在当前线程执行。
- **执行器重入时一律内联执行，不区分通道**：嵌套循环持锁时，耗时的 LLM 调用会占住这个
  循环所在的线程，`guard` 续租无法执行，租约可能过期，临界区就会重叠。改为按通道区分：
  io→llm 的提交照常排队。
- **能力降级时退到 `continue_on_error=False` 那一行**：这会悄悄改变调用方传入的错误处理
  策略。改为单列第 5 行，沿用调用方的取值。
- **直写合并后，按整批结果给回执**：builder 部分写入后抛错时，回执会和实际落盘状态不一致。
  改为要求 IndexBuilder 声明 `grouped_build` 并给出逐条结果，否则不合并。
- **按 target 名字拒绝 `memory` 锁**：第三方注册的进程内锁换个名字就能通过检查，具备跨实例
  互斥能力的其他实现也会被误拒。改为按 `mutex_scope` 能力判断。

## 验证

按 S09 的 8 类测试组织，另外补充行为测试：

| 类别 | 内容 |
|---|---|
| 契约 | 同一套测试同时跑 `grouped` 和 `sequential`，验证输入输出、错误、Skipped 的语义；`write_group` 默认实现和三个引擎的覆盖实现结果一致 |
| 装配 | 只改配置就能切换编排器 target 和执行器 target；配置未知 target 时装配失败；用户配置显式删除了锁时装配失败（`grouped` 和 `sequential` 都一样）；`deployment.multi_instance=true` 且锁的 `mutex_scope` 不是 `cross_instance` 时装配失败（用两个不同 target 名的替身锁验证：进程内的被拒绝，跨实例的通过） |
| 替换 | 换掉编排器或执行器后，其余能力和 MemoryAPI 的业务契约不变；引擎不支持分组编排时退化为真值表第 5 行，且 `capabilities` 如实报告。区分度最高的用例：在不支持分组编排的引擎上传入 [成功、失败、成功]，`continue_on_error=True` 时第三条必须落盘，`False` 时第三条必须为 Skipped；替换成一个未声明 `grouped_extract` 的 Evolver 时，该 infer 段在段内逐条抽取，不发生合并，组间并发保留；替换成一个未声明 `grouped_build` 的 IndexBuilder（例如文档栈）时，直写段逐条 build，infer 段仍然合并抽取 |
| 共享 | API 层和控制层拿到的执行器是同一个对象；同一装配上下文里锁只有一份 |
| 安全 | 授权拒绝、跨 Scope 写入、routing 落点越界在合并模式下的处理和逐条模式一致；PEP 逐条执行恰好一次，顺序符合决策 1；审计预检早于判定 |
| 故障 | 一批里有一条必然失败、且抛出可重试异常时，只有它被标为失败；退回逐条时原文不重复；抛出提交状态未知的异常时，不自动重放，整批标为失败；IO 池和 llm 池大小都为 1 时，接续写入（continuity）能正常完成，不死锁；**续租不被内联执行卡住**：使用短租约和一个耗时超过租约的假 Evolver，在一次嵌套桥接写入持锁期间，对同一锁 key 发起第二次写入。要同时验证三点：至少观察到一次实际续租；两次写入的临界区没有重叠；两次写入最终都成功（不能靠第二次一直等到超时来让测试通过）。这个用例和单线程池的完成性用例必须同时通过。在 llm 工作线程上发起需要加锁的写入时，会被拒绝，并且没有取得锁；**直写部分失败**：让批量 builder 写完第一条后抛错，验证各条回执与实际落盘状态一致（声明了 `grouped_build` 时按逐条结果给回执，未声明时按逐条 build 执行）；**同一条输入只提交了部分 unit**：一条输入产出 A、B 两个 unit，A 已写入，B 写入时抛错（分 `failed` 和 `unknown` 两种情况），同时检查实际存储内容、返回的 `units` 和这条输入的 `error` / `error_type`，都要符合决策 4 的汇总表；锁超时以 `LockTimeoutError` 归到具体条目；锁后端不可用时 fail-closed 报 `BackendError`，不得静默跳过锁；每种降级都能看到 WARNING、trace 事件，计数器也增加 |
| 生命周期 | 执行器的 `health` 和 `close(wait)` 可用；`MemoryRuntime.close` 会等执行中的写入完成；重复装配不会泄漏线程，也不会复用旧状态 |
| 默认配置回归 | 决策 3 真值表逐行覆盖：没有锁竞争时，第 2~4 行的结果和现在一致（复用现有批量写入测试），第 3、4 行连执行顺序也一致。同一用户并发写入的回归用例，必须把 `LockTimeoutError` 当作合法结果之一，不能断言一定成功；默认配置（第 1 行）允许的行为变化以决策 3“升级保证的可观察范围”为准；不传 config 调 `build_kernel()` 也能装配成功；**旧配置升级回归**：用一份未修改的旧配置升级运行，按决策 3“升级保证的可观察范围”逐项断言：逐条结果对齐、授权、失败策略、已承诺的顺序都不变；只允许出现其中列出的三类变化 |

行为测试：
- **事件循环不阻塞**：用 sleep 几秒的假 LLM 模拟路由、分类和抽取。并发写入的同时运行一个
  心跳协程，测量事件循环的延迟，要求最大延迟远小于 LLM 耗时（建议小于 50ms）。
- **省 token**：用能计数的假 LLM，验证同一 scope 写 N 条 infer，抽取只调
  `ceil(N / max_items)` 次（触发 token 上限时相应增加）。
- **吞吐**：K 个不同 scope 各写一批，总耗时约等于“单组耗时 × ⌈K / 并发上限⌉”，而不是 K 倍。
- **来源归属**：覆盖多条来源合成一条、一条对一条、来源缺失（整批共享）、NOOP（`units=[]`）四种情况。
- **锁**：同一 `(org, space, user)`、不同 session 的两个并发 infer 写入不产生重复的 ADD；
  不同用户之间互不阻塞；直写不加锁；从不同事件循环、不同线程调同步 `add` 时锁仍然生效。
  一个批量请求里交错着多个用户的条目时，按段分别加锁，任何时刻只持有一把锁；
  `continue_on_error=False` 遇到失败、获取锁失败、被取消之后，锁都已经释放。
- **`continue_on_error=False`**：执行顺序和 Skipped 结果与现在完全一致。
- **取消**：取消后没有正在进行的写入，锁已经释放。
- **线程安全**：决策 9 清单里的每个对象都有对应的并发测试。
- **质量 A/B**：`evaluation/` 目前的 locomo、longmemeval、memoryagentbench 三个基准都是
  端到端评测，没有“合并抽取 vs 逐条抽取”的对比方式。实现者需要另做一个小型 A/B 评测：
  同一份输入分别跑两种模式，比较派生记忆的召回率、准确率和重复率，结果填回本文档的
  测试基线，作为 `max_items`、`max_tokens` 默认值的依据。
- 现有 `tests/unit/api/`、`tests/unit/control/`、`tests/unit/common/` 全部通过，`ruff check` 通过。

### 实现后的测试基线

| 覆盖 | 测试文件 |
|---|---|
| 执行器（上下文传播、重入矩阵、关闭等待、装配隔离） | `tests/unit/control/test_blocking_executor.py` |
| 引擎批量原语（合并一次调用、来源归属、部分提交、组件不支持时前置选择） | `tests/unit/control/test_engine_write_group.py` |
| 编排（分组、并发、锁段、fail-fast、降级计数、取消等待） | `tests/unit/control/test_write_orchestrator.py` |
| 装配（多实例与锁不匹配、删锁、未知 target、调用级开关；校验实际使用的锁） | `tests/unit/api/test_assembly_write_orchestration.py` |
| 批量 [成功, 失败, 成功] 的回执与真实存储一致 | `tests/unit/api/test_batch_write_failure_isolation.py` |
| 写入口操作作用域、执行器身份与泳道；执行器/计数共享、重复装配不泄漏线程、鉴权拒绝先于引擎 | `tests/unit/api/test_write_entry_scopes.py` |
| 未知提交在单条/批量入口给同一领域异常 | `tests/unit/api/test_write_incomplete_error.py` |
| 线程安全（降级计数、ContextVar 归属、内存 KV） | `tests/unit/control/test_concurrent_write_safety.py` |
| 入口不阻塞事件循环（慢路由 / 慢摄入） | `tests/unit/api/test_write_async_nonblocking.py` |
| 合并抽取 / 批量建索引 | `tests/unit/construction/test_evolver_grouped_extract.py`、`test_index_builder_grouped.py` |
| 锁的作用域声明与跨线程 CAS | `tests/unit/common/test_distributed_lock.py` |
| 质量 A/B 工具 | `evaluation/scripts/compare_grouped_extract.py`、`tests/unit/evaluation/test_compare_grouped_extract.py` |

A/B 工具每轮各自新建引擎与 scope（否则第二轮会看到第一轮的记忆），调用次数从实际
`write_group` 采集而非按 `merge_extract` 推算。数值尚未在真实数据集上跑出：`max_items=50` / `max_tokens=6000` 目前是
设计参考值，不是评测结论。调参需要接入真实 LLM 后执行该脚本，把派生记忆的召回/准确/重复
率补到本节。

## 已知遗留

1. **跨空间派生的竞态**：开启 routing 时，派生记忆会逐条判定到其他空间，在目标空间里去重。
   锁住载体的 `(org, space, user)`，挡不住其他用户同时往同一个目标空间写。本次只保证同一
   载体内串行。
2. **跨请求合并**（方案 C）：多个独立的 `add_async` 调用之间不合并。
3. **LLM 限流与配额**：只提供线程数上限，不处理 429 和退避。
4. **重试导致原文重复**：调用方重试失败的条目时，原文可能再写一次 `/messages/`。这个问题
   逐条 infer 时就存在，不是本次引入的。
5. **跨实例互斥只在声明多实例时强制**：基线里配的是 memory 锁。部署方设置了 `deployment.multi_instance=true` 时，装配阶段会拒绝 memory 锁；如果多实例部署却没有设置这个配置项，系统无法察觉，只能靠部署文档约定和部署评审，把锁覆盖为 redis。
6. **去重或落盘中途失败不逐条重试**：只有抽取阶段失败才退回逐条重试；去重或落盘阶段出错时
   整批标为失败，避免已经落盘的部分派生记忆被重复写入。
7. **锁不能保证绝不重复**：租约失效时仍可能产生重复记忆，要靠去重和后台整合事后收敛
   （见决策 7）。
8. **锁后端不可用时，infer 写入整体不可用**：所有模式下的 infer 写入都要加锁，而 S07 规定
   锁后端不可用时 fail-closed，抛 `BackendError`。所以一旦 Redis 锁故障，整个部署的
   infer 写入都会失败，而且没有任何配置能绕过锁（绕过锁属于 S09 不变量 12 禁止的静默降级）。
   处置办法是修复锁后端，不是关掉锁。直写、procedural、middle 不加锁，不受影响。
9. **单用户的 infer 吞吐受锁限制**：同一用户的多个组会抢同一把锁，组间并发对单用户的
   infer 写入不起作用（见决策 7“对吞吐的影响”）。

## 留给实现者

- 前置步骤具体怎么切分交给线程执行，以尽量减少线程切换为准。约束有两条：事件循环上不能有
  同步调用 LLM 或存储的代码；必须遵守决策 1 的执行顺序。
- 各项默认值：llm、io 线程数，组间并发上限，锁的等待超时和租约。参考值：llm 线程数 8，
  io 线程数 `min(32, cpu+4)`，组间并发 4（和 `middle_concurrency` 保持一致）。
  `max_items`、`max_tokens` 的默认值由 A/B 评测决定。
- token 用什么方法估算（按字符数近似，还是用分词器）。
- 新增端口、算子、配置项和 Engine 原语的具体命名，公共实现写成 mixin 还是 helper。
- `grouped_extract`、`grouped_build`、`mutex_scope`、可重试异常类型、来源回执数据结构的最终命名，以及
  执行器识别“当前线程是本执行器工作线程”的具体做法。
- 计数器挂在 `health` 上还是 trace 上（两者至少满足一个）。
