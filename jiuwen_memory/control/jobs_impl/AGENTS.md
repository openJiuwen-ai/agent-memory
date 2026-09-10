# Agent Memory Jobs

**规约文档**：[S03-control.md](../../../docs/specs/S03-control.md)、
[S05-construction.md](../../../docs/specs/S05-construction.md)

本目录实现控制层 Job：读取已授权范围内的数据、调用注入的构建算子、归集任务结果。
Job 自身只执行一轮；是否排队、何时触发、是否周期执行由 Scheduler 决定。

## 模块地图

| 文件 | 职责 |
|---|---|
| `__init__.py` | 导入实现以触发默认 JobFactory 注册 |
| `evolve_job.py` | 普通内容演进 Job 与 Spec；排除中期记忆后调用内部 Evolver |
| `middle_to_long_job.py` | 中期转长期 Job 与 Spec；连续性切批、调用 Evolver、归档成功原文；注册默认 JobFactory |
| `hierarchy_job.py` | 显式一次性 TIME 建树 Job 与 Spec；捕获请求、可选锁、候选读取与构建调用、真实结果报告 |
| `hierarchy_candidates.py` | 建树只读收集器：完整分页、精确 Scope 身份、旧根全部父层/叶补齐、反向引用核对与保护限额 |
| `hierarchy_derive_job.py` | 周期 TIME 增量 Job/Spec：同源 profile、实时开关、逐层推进、下层待定边界、跨轮故障闸 |
| `hierarchy_incremental.py` | 完整水位扫描、未挂父候选、迟到/过窗诊断及子树证据，不写记忆 |

## 行为铁律

1. **运行时构建依赖同源**：各 Spec 不在装配期解析 Evolver/IndexBuilder；Engine
   必须注入相应 Job 必需的同源实例。HierarchyJob 的运行时 KV 优先于 Spec 兜底，
   不另猜真源，不因缺失注入回落默认构建实现。
2. **内部调用使用 EvolveRequest**：Job 不向 Evolver 透传旧 units/mode 参数形态。
   HIERARCHY 走专用 Job，不经过 EvolveJob 的内容抽取路径。
3. **先收齐候选再写**：HierarchyJob 只从 `/memory/` 收集 ACTIVE TIME snapshot 与
   home 中相交旧 time_span/scene/event，补齐旧根全部父层和叶；不按 infer 过滤，不读 messages。
   Scope 边界必须在子引用点读前检查。分页漂移、超限、缺子或非法双向引用均失败，
   不截断后调用 Composer；有旧父时再次流式核对入边，防止遗漏区间外反向子。
   身份键必须保留完整五维 Scope 与 id。
4. **显式重建与周期增量分开**：HierarchyJob 固定 interval=0，任务 Scope 等于
   tree_home_scope；不扩大到其它 kind/角色链，也不自行触发周期、写后或召回时建树。
   任务可跨 session 收叶，实际 time_span/scene/event 分组由 Composer 决定；
   不允许用较短角色链隐式降级已有父层子树。
5. **可选锁覆盖读取到构建**：HierarchyJob 在持锁范围内完成候选读取和 Composer
   调用；取消同步线程工作时，先等待已启动线程结束再释放锁。无锁时不声称互斥，
   失锁/超时报告失败，不宣称回滚已经发生的写入。
6. **真实终态与修复边界**：Job 返回明确终态。HierarchyJob 缺少 hierarchy_result、
   complete=false 或存在 repair 均为 FAILED；无候选是成功空操作。错误、计数和
   修复信息保留在任务结果中，不把部分失败伪装成功，不提供自动修复或事务保证。
7. **周期不扩大授权 home**：HierarchyDeriveJob 只处理启动时固定的 home，不清空
   agent/user/session，不全库发现 home。每轮检查 enabled/auto_derive；profile 只来自
   运行时 Evolver 绑定的 Composer，Spec 强制注入同源 KV/Evolver/Policy。
8. **逐层只处理未挂父输入**：水位读取 home 的全部 ACTIVE 同角色父，不受 lookback
   截断；迟到、过窗、缺子、分页异常、超限均先报告失败，不截断后推进。下层 pending
   起点阻挡上层封口；一层失败即停。部分写入/取消/写后失锁保留共享内存故障闸，
   不重试修复；同一 Spec 再注册也不清闸，人工处理后才能启用新 Runtime。

## 与其他子目录的边界

- API 执行权限和策略闸门；Job 只消费已授权的范围，不接收身份或执行 PEP。
- `evolution/validation.py` 提供请求与 Scope 边界校验，不承载读写编排。
- Construction 决定切分、父内容和结构写入；Job 不实现 TimeSpanMerger，不直接写记忆本体。
- Scheduler 决定执行时机并保留 Job 终态；Job 不持有 Scheduler 或自行创建周期循环。
