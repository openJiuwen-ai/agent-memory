# F04 — dreaming：evolve 定时演进（三态分发 / 候选源 / 持续授权）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-22 |
| 影响范围 | `jiuwen_memory/api/memory_api_impl/dreaming.py`（DreamingCoordinator）、`common/type_def/candidate.py`、`common/type_def/filter.py`（t_ingest 入册）、`control/engine_impl/evolve_dispatch.py`、`control/engine_impl/dreaming_registry.py`、`control/jobs_impl/candidate_resolver.py`、`control/jobs_impl/evolve_job.py`、`control/scheduler.py` + `scheduler_impl/async_timer_scheduler.py`、`jiuwen_memory_entry/`（handler / server / http / mcp）、`deploy/config/`、`docs/specs/S02-memory-api.md`、`docs/specs/S03-control.md`、`docs/specs/S07-common.md` |
| 测试基线 | dreaming / Scheduler / API / MCP 定向回归全绿；相关 Python 文件 `ruff check` 0 error。分文件基线见「验证」 |
| Refs | — |

## 背景

**需求**：闲时对记忆定期做演进（consolidate / forget / 未来模式），作为
定时任务长期运行。设计要求：任意新演进模式 × 定期调度零额外调度代码；
不同 scope 独立配置；不重启即可开关调频；重启自动恢复。

**判别式**：演进流程有无"evolve 调用周围的特殊逻辑"——无则复用
EvolveJob（dreaming 命中）；有（切批 / 归档 / 批级失败隔离）则新建专用
Job 类（middle 命中，`MiddleToLongJob`）。

**分层定位**（自上而下）：

```
API 层      evolve 三态分发 + 鉴权（入口 / 每 tick / fan-out 逐桶）+ DreamingCoordinator
调度层      schedule_key 去重 / interval 定时 / supports_recurring 能力声明
候选层      统一 resolve()：四源一个接口、一个产出、一个调用点
演进层      mode 分支 + 算子插件；算法内部可自由查向量库 / ES
```

## 决策

### D1：dreaming 是调度标志，不是演进模式（与 mode 正交）

不设 `EvolveMode.DREAMING`。mode 决定"对记忆做什么"，dreaming 决定
"要不要定期跑"——任意 mode × 定期调度免费组合，新增 mode 零调度层改动。
没有全局模式开关：任务存不存在完全由注册表决定，开 = 注册一条，
关 = 注销一条。

### D2：evolve 三态语义（注册键 = scope 五元组 + mode）

| `dreaming` | 语义 | 未命中注册表时 |
|---|---|---|
| `None` | 立即跑（一次性 EvolveJob，返回 job_id） | —（不查注册表） |
| `True` | 注册定时（`interval` 必填 >0；同 key 已存在 = 换绑新 Job，即改参数） | —（覆写同 key） |
| `False` | 注销：删注册项 + `scheduler.cancel` | **静默返回 None（幂等）** |

注销必须带 mode（注册键含 mode）；未命中注销不退化为立即跑，不抛
NotFoundError。scope 是精确五元组匹配，与存储层隔离语义一致——注册到哪个
桶就只处理哪个桶。

### D3：候选源四型（数据，不是代码）

```python
PredicateCandidate(filters, window)   # ① 谓词源：list_units + filters 下推，恒单桶
IdsCandidate(unit_ids)                # ② 点名源：load_units 点读，差集回显
RecallCandidate(query, channels, top_k, filters)  # ③ 召回源：Retriever 全链路，每 tick 重召回
FanOutCandidate(child, require_empty, require_nonempty)  # ④ 枚举源：scopes() 枚举子桶 × ①，多桶
```

- **统一接口**：`CandidateResolver.resolve() -> CandidateOutcome(groups=[CandidateGroup(scope, units)])`；
  EvolveJob 只调这一个方法，不感知源类型。①②③恒单桶、④多桶，演进统一
  逐桶进行（consolidate 需桶内上下文，不跨用户混桶）。
- **四源 filters 契约**：①本体（选料即条件句）；②无（id 即完整答案，叠加
  条件 = 越权替用户筛）；③收窄用（query 管像什么，filters 管还须满足什么，
  与 search 接口同构）；④嵌套在 child（选桶靠 scopes 枚举 + 形状约束，
  桶内选料委托①）。
- **候选源必须是数据不是代码**（JSON 可序列化 → 进注册表 → 持久化 →
  重启可恢复）。dict DSL 形态：`{"type": "predicate", "window": 3600}`。
- **注册前 canonical 校验**：dict 与 Python dataclass 都经 `to_dict → from_dict`
  round-trip；`window/top_k/unit_ids` 等值域统一校验。`fan_out.child` 只允许
  predicate，显式传其他 type 必须拒绝，禁止静默扩大为全量选批。
- **②点名源遇缺 id**：照常演进 + 差集回显（`requested_ids` / `loaded_ids` /
  `skipped`）——不静默漏、不炸整批，与检索侧 UnitReader"底层不裁决，
  结果可追责"同构。
- **③每 tick 重新召回**（刻意）：top-k 是"当前时刻"的答案，语义随时间
  漂移，不适合增量语义。
- **fan-out 覆盖判定**（`covered_by`）：父 scope 非空字段 = 前缀约束；
  `require_empty` / `require_nonempty` 对子桶 scope 字段施形状约束（如
  "所有用户桶" = `require_empty={"agent","session"}`）——只收窄不放大，
  无越权面。桶集合来自 Engine 候选数据面的 `candidate_scopes()`：
  InMemoryEngine 走 KVStore，CloudEngine 走 DomainStore，避免正常查询与 dreaming
  看到两份不同数据。
- **链条归属**：EvolveJob 拥有链条（候选 → middle 排除 → 演进 → 回显），
  Engine 只做装配与分发（candidate→resolver 翻译），Scheduler 拥有调度。
  立即跑与定时跑共用同一个 `EvolveJob.run()`；红线：立即跑必须经
  EvolveJob 提交，不得另写第二份链条。
- **容量边界**：单桶和整个 fan-out 每轮均最多 10000 条候选，
  fan-out 最多 1000 个桶；`IdsCandidate.unit_ids` 和
  `RecallCandidate.top_k` 均上限 10000。超限 `ValidationError`，调用方须用
  scope / filters / window 收窄。

### D4：候选源双注册表（kind 判别键单源）

| 注册表 | 位置 | 用途 |
|---|---|---|
| `_CANDIDATE_CODECS` | `common/type_def/candidate.py` | 序列化（dict DSL ↔ dataclass） |
| `_RESOLVER_FACTORIES` | `control/engine_impl/evolve_dispatch.py` | 装配（candidate → resolver） |

每型 dataclass 持 `kind: ClassVar[str]`（与 dict DSL 的 `type` 值同源），
两侧分派不可能漂移。重复 / 空 kind → `ValidationError`；未注册 kind 统一
`ValidationError`（鉴权前拦截）。新增候选源 = 新 dataclass + 两表各一条，
零分支表改动（isinstance 阶梯已移除）。

### D5：时间窗轴 t_ingest + filters 下推

时间窗轴用 `t_ingest`（摄入时间，恒非空——内核强制盖章），不用
`t_message`（调用方可传空，`gte` 下静默排外）。`window` 每 tick 现算
`FilterClause("t_ingest", GTE, cutoff_ms)` 与静态 filters AND 合并后经
`list_units(filters=...)` 下推（cutoff 不落盘——注册时算死会让窗口起点
冻结）。t_ingest 已入 `_BUILTIN_FIELDS` 白名单（2026-09-15）。

缺省**不**注入 `lifecycle=active` 静态子句：`candidate=None` 是谓词全量
源（现状行为等价物）；forget 等模式恰需处理终态记忆，要 active-only
由调用方显式传 filters。

### D6：调度层——schedule_key 去重 + supports_recurring fail-fast

- `Job.schedule_key` 属性（默认任务类名）：`EvolveJob` 覆写为 scope 五元组
  + `"evolve"` + mode；驱动 Job 前缀 `dreaming:`，一次性 Job 前缀
  `evolve:`。Scheduler 去重 / skip-tick / cancel 只依赖该键，零业务知识。
- `Scheduler.supports_recurring` 能力声明（默认 False，fail closed）：
  dreaming 注册前校验，不支持 → `ValidationError`（"注册了定时但只同步
  跑一次"的静默降级当场暴露）。生产部署默认调度器 `async_timer`。

### D7：持续授权 + PEP 边界（鉴权整体上移 API 层）

- **注册表 `DreamingEntry` 记录 `created_by`**（注册者 actor，可持久化）。
- **每 tick 复验**：驱动 Job 以注册者身份复验（与入口同一套 `_authorize`
  组件，判定不漂移）；`PermissionDeniedError` → 注销注册表 + 父定时器
  终态 FAILED（`stopped_reason=permission_denied`，防重启复活）。
- **恢复即复验**：重启恢复逐条以 created_by 复验，失效任务不复活。
- **fan-out 逐桶裁决**（`DreamingCoordinator.authorize_fan_out`）：每轮
  枚举命中桶 → 逐桶鉴权 + 空间可写校验 → 获准桶才提交；拒绝桶标签经
  `denied_scopes` 仅以 `count:N` 脱敏摘要随 JobInfo 回显（不静默吞、不中断
  其余桶，也不泄露无权资源名）。每轮重新裁决，权限变化即时生效。
- **架构归属**：`DreamingCoordinator` + `DreamingDriverJob` 在 API 层
  （`api/memory_api_impl/dreaming.py`）；Control 层纯执行（Engine.evolve
  只剩立即执行链，收 API 层裁决产物 `buckets` / `denied_scopes` 透传；
  FanOutResolver 收已裁决获准桶列表，`buckets=None` = 内核直调路径
  自行枚举、无授权语义）。驱动 Job 不执行演进——每 tick = 复验 →
  fan-out 裁决 → 经 `commands.evolve` 派生一次性 EvolveJob（唯一执行
  链条，`JobInfo.detail.spawned_job_id` 回显派生关系）。

### D8：单实例部署模型

1. **写序**：submit → `registry.save`；save 失败回滚 `cancel`（register
   抛错前 cancel 已提交 Job 再上抛；restore 换绑失败 cancel 新 Job +
   warning 跳过、entry 保留旧值下次重启可恢复）——不留"定时器在跑、
   注册表无记录"的幽灵任务。
2. **leader 锁**（双路径）：支持周期任务的长命实例启动时无论注册表是否为空
   都须取锁，register 前也复核领导权。注入 `LockProvider`（生产推荐，Redis）
   或使用 KV owner+TTL 兜底，两路都按 lease/3 自动续租；每个 tick 执行前再次
   复核。续租失败回调取消本实例全部 driver；tick 复核失锁时父任务以
   `stopped_reason=leadership_lost` 失败停摆。两条路径都停止后续执行步骤，
   已进入的同步 Evolver 调用不强制中断。
   `assembly` 在存在 `lock.dreaming` 时把该独立具名实例注入注册表；local/online
   Docker profile 已与 Redis KV 同地址显式装配 Redis lock，不静默使用进程锁。
   LockProvider 租约不由 registry 覆盖，使用 provider 配置的
   `lease_ms`（默认 30 秒）。注册、注销、恢复与失锁清理在协调器内共用同一串行区间；
   register/unregister 均要求当前实例仍是 leader，非 leader 只报错，不删
   共享注册表。恢复幂等缓存只在当前 leader 任期内有效；失锁或主动
   放锁后清空，下次获锁必须重扫。恢复逐条在 submit 后和 registry.save 后
   复验租约，中途失锁取消本地驱动并立即中止，不继续处理后续记录。
3. **restore fail-fast + 逐条隔离**：锁被他人持有（未过期）→ `RuntimeError`。
   默认 in_process 且空表仍可 no-op；周期调度器即使空表也持 leader 锁。持锁后
   重新扫描注册表，单条非法 interval/candidate/submit/save 失败只跳过该条。
4. **长命 surface 钩子**：`Server.restore_dreaming()`（`jiuwen_memory_entry/core/server.py`）
   经 `getattr` 探测（不属于 MemoryAPI 公共契约——任意主体可触发的恢复
   会把部署模型决定权让渡给调用方）；HTTP / MCP server 启动时调用，
   短命进程（CLI）不得调用。
5. **批次标记归属**：dreaming 批次标记若被系统行为依赖，必须落
   `system_metadata.*`；`user_metadata` 只放用户业务标注。

### D9：JobInfo 回显语义

- `mode` 回落源统一 `job.mode or type(job).__name__`（五处路径一致，
  鉴权点对同一任务看到同一 mode）。
- 授权拒绝：父定时器终态 FAILED；退出路径只把 RUNNING → SUCCEEDED
  （FAILED / CANCELLED 不覆写）。
- 同 schedule_key 窗口内复活（重新注册走刷新路径）时清理上一生命周期的
  `stopped_reason` / `finished_at` 残留。
- 更新现有 schedule_key 时若 registry.save 失败，恢复旧 Job 声明；只有首次
  新建失败才 cancel，避免复用 job_id 时误杀原健康任务。
- Scheduler 的 cancel/status 均 marshal 到私有循环；shutdown cancel 后 await
  全部 Timer/drain 协程再关闭循环。父周期任务汇总最近运行状态；一次性
  任务与已终止父周期任务共用有界 JobInfo 历史，最多保留 10000 条。
- 注册表单条损坏记录（坏 JSON / 缺字段）：`load_all()` 逐条容错
  warning + 跳过，保留在 KV（装配修复后下次重启可恢复），不拖垮健康任务。
- 注册键使用 scope 五元组 + mode 的规范 JSON/Base64URL 编码，不再依赖
  `/` / `:` 分隔；兼容读旧键并在后续 save 时迁移。
- 空间已归档/冻结/删除等持久不可写状态以
  `stopped_reason=space_not_writable` 停止父周期任务并移除声明，
  不在后续 tick 无限失败重试。

## 入口接线

HTTP 与 CLI 经 `api_contract` 从 `MemoryAPI.evolve` 签名机械派生契约
（`--dreaming` / `--interval` / `--candidate` 自动生成，畸形类型在
`parse_request` 边界 400）；MCP 走共享 dispatch（`handler.py::_evolve`），
`memory_evolve` 工具签名暴露三参数。注册时的认证身份写进台账
`created_by`，成为每轮 tick 复验授权的锚点。

## 扩展指南

**新增演进 mode**（三步，调度/注册表/持久化/恢复零改动）：

| 步 | 改动 |
|---|---|
| 1 | `EvolveMode` 枚举加值（`construction/evolver.py`） |
| 2 | Evolver 加分支方法（主体工作量） |
| 3 | 调 `evolve(scope, mode, dreaming=true, interval=N)` 注册（无代码改动） |

**新增候选源类型**（四条，零分支表改动）：新 dataclass（含 `kind: ClassVar[str]`
+ `__post_init__` 校验）+ `_CANDIDATE_CODECS` 一条 codec + 新 Resolver 类 +
`_RESOLVER_FACTORIES` 一条工厂。

**场景速查**：

| 场景 | 操作 |
|---|---|
| 用户桶定期演进 | `scope={org, user}, dreaming=true, interval=N` |
| 公司级共享桶 | `scope={org}`（独立桶，非聚合） |
| 团队里只演进某人写的 | 上行 + filters `system_metadata.author_principal eq "user:bob"` |
| 全组织逐用户 | 候选源④ fan-out（一条注册，每 tick 枚举实际子桶，新用户自动覆盖） |
| 点名某几条 | 候选源②（id 列表会过期，即时语义为主） |
| 按主题语义选批 | 候选源③（query + filters 正交） |

## 拒绝的方案

| 拒绝 | 理由 |
|---|---|
| admin 三键全局配置（v1 方案） | 全局单份，per-scope 需矩阵式键值；evolve 天然携带 scope/mode 两个注册维度 |
| write 接口做配置入口 | 单条记忆属性（高频、用户权限）vs 调度配置（低频、管理操作）——语义/时机/权限三重错配 |
| Python callable 过滤器 | 函数无法持久化到 KV（v1 栽过"存不进注册表"的坑）、全量拉取浪费；FilterExpr 是数据可序列化 |
| 新建 DreamingJob 专用类 | dreaming 无"evolve 周围的特殊逻辑"，复用 EvolveJob |
| `t_message` 时间轴 | 调用方可传空 → `gte` 静默排外；"新增记忆"语义对位 t_ingest |
| 缺省注入 lifecycle=active | forget 等模式恰需处理终态；缺省过滤替调用方裁决，破坏点名源契约 |
| 未命中注销抛 NotFoundError / 退化为立即跑 | 幂等注销是运维刚需（重启重放不应报错）；"取消订阅却寄了份报纸"写放大且语义不可预期 |
| 注册时一次性鉴权、之后永续 | 权限回收对长命定时任务失效——恰是最需复验的场景 |
| fan-out 整体鉴权（父 scope 一次过） | 子桶可能跨 space，父 scope 授权不代表子桶授权——逐桶是唯一正确粒度 |
| Scheduler 识别 (scope, mode) 业务键 | 业务知识下沉调度器，各实现漂移；通用键在 Job 契约声明一次 |
| InProcessScheduler 加周期支持 / 注册静默降级一次性跑 | 同步实现起线程伪装异步掩盖装配错误；fail fast 优于静默降级 |
| 分布式调度器 / 无锁多实例 | 超出当前部署形态（单实例）；实例锁 + fail-fast 是当前形态最小正确解 |
| control 层 EvolveGuard 端口 + 闭包（初版落地形态） | 破坏 PEP 边界（S03：鉴权在 API 层，control 不持有 identity）；control 开第二鉴权口，判定漂移只是时间问题 |
| API 层直接持有 EvolveJob 做持续授权 | 执行链捏在 API 层，破坏"演进必须经 EvolveJob 提交"红线 |
| 恢复路径绕过复验直接重提 | 重启即洗白——与 D7 恢复即复验矛盾 |
| candidate 插件化（Factory/Producer） | 候选源必须是数据不是代码；插件化引入"往注册表塞代码对象"通道 |
| 类对象作注册表键 | dict DSL 持久化侧仍需字符串键，两侧键不同源会漂移 |
| 单一全局注册表合并 codec 与 resolver | 分属 common（数据）与 control（装配），合并造成跨层反向依赖 |
| 点名源遇缺 id 报错 / 静默忽略 | 一条边缘状态不应炸整批；静默漏处理比失败更危险——差集回显 |
| 链条抽到 Engine._evolve_once（B 形态） | Job 保持自足执行单元，EvolveJobSpec/JobFactory/E-06 注入模式全不动，改动面最小 |

## 验证

- dreaming / Scheduler / API 接入 / MCP 定向回归全绿；相关 Python
  文件 `ruff check` 0 error。
- `tests/unit/api/test_dreaming_coordinator.py`：注册 / 幂等注销 / tick
  复验拒绝停摆（FAILED 非 SUCCEEDED）/ fan-out 逐桶裁决 / 单实例锁冲突
  fail fast / 损坏与无主 entry 容错 / 空表 no-op / 写序回滚（save 失败 →
  cancel + 异常上抛，register 与 restore 两路径）/ 注册 save 期间失锁串行化 /
  leader 任期切换后恢复缓存失效 / restore 换绑期间失锁中止 /
  三态分发真实装配。
- `tests/unit/control/test_dreaming_dispatch.py`：纯执行链 +
  candidate→resolver 注册表分派 + forget 链 + 注销态。
- `tests/unit/control/test_candidate_resolver.py`：四源 resolve 语义 +
  t_ingest 时间窗下推。
- `tests/unit/common/test_candidate.py`：kind 注册表、round-trip、校验。
- `tests/unit/control/test_async_timer_scheduler.py`：schedule_key 去重 /
  私有循环生命周期 / shutdown 收尾。
- `tests/unit/jiuwen_memory_entry/test_handler.py`：MCP dispatch 三态
  透传 + 畸形类型 400 拒绝。

## 已知遗留

- **同步写入 fencing 未实现**：协作式取消只能在执行边界检查；已进入的同步
  Evolver 仍可能完成写入。严格跨实例零重叠需要存储边界原子 fencing 校验。

- **查询接口形态待定**：dreaming 注册表对外暴露（`dreaming_list` verb 或
  扩 job 查询 verb）——`DreamingRegistry.load_all()` 已就绪，只差暴露形态。
- **时间窗无补偿**：tick 延迟漏处理的记忆等下个窗口（可升级 KV 记
  last_success_ts）。
- **middle 排除仍在 Python 侧**：NE 对字段缺失的语义跨后端验证后可下推。
- **KV list 的 filters 未编译进 SQL**：`compile_pg_filter` 已就绪（纯后端
  改动即获真实收益）；pg/ES 编译器对 `t_ingest` 的回归用例未单独补
  （Python 求值器侧已由 resolver 单测覆盖）。存量索引投影无 `t_ingest`
  字段，真后端按它过滤时存量记录缺字段排外——dreaming 语义只关心新写入，
  影响可忽略。
- **两处注册表键集一致性靠测试断言**：新增类型漏注册一侧，键集断言需
  同提交更新方能暴露。
- **`restore_dreaming` 经 getattr 探测**：公共化需先定义"谁能触发恢复"
  的授权语义，暂缓。
- **KV 锁不具备后端 CAS**：写后 owner 复核 + 每 tick 检查可检测所有权变化，
  检测失锁后通过取消信号停止后续执行步骤；严格跨进程原子竞选仍须注入 Redis LockProvider；KV 路径只作
  本地/单实例兜底。
- **多实例第二实例 fail-fast 不自动抢占**：高可用需外层进程管理，
  不在本层解决。

## 2026-09-22：内部提交与取消传播修复

内部调度循环的 `submit` 必须 await 实际提交体，否则驱动拿到协程对象，
EvolveJob 没有入队。取消采用父定时器与实例/派生任务共享的线程安全信号：
排队驱动在消费前检查，EvolveJob 在解析前、每桶派发前和工作线程中检查。
当前同步调用允许结束，后续桶停止；同 scope 无关任务继续消费。

拒绝只删除 TimerEntry：这会遗漏已排队的工作。也不通过取消整条 drain 协程
实现注销，因为它会干扰同 scope 无关任务，且不能终止已进入的同步线程。
取消生命周期不复用到重新注册的任务；活跃声明更新仍保留现有归属。

验证使用 `tests/unit/api/test_dreaming_runtime.py` 的真实装配链，仅替换底层
Evolver：覆盖实际 tick 执行、排队驱动注销及重注册、排队演进注销/失锁、
fan-out 首桶执行期间注销/失锁，以及工作线程排队期间取消。
本次合入基线：dreaming runtime/coordinator、async timer、dispatch、
candidate/resolver、application ports、CloudEngine、handler 与 MCP 定向回归
全部通过；修改的 Python 文件通过 Ruff 检查。
