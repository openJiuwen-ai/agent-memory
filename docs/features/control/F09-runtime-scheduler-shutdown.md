# 运行时调度器生命周期收尾

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-10 |
| 影响范围 | api 装配、control Scheduler/Engine、common LoopRunner；S02/S03/S07 |
| Refs | GitHub #425 / GitCode #235 |
| 测试基线 | 原实现四个真实 runtime 装配回归均因关闭后线程存活而失败；适配 dreaming 后相关回归 1013 passed、6 skipped |

## 背景

最初的 `MemoryRuntime.close` 只关闭摄入线程池。dreaming 更新增加了根 Scheduler
任务取消，但仍未释放私有 loop 线程，也未覆盖 Engine 内联装配的独立调度器。单独取消某一次 run 也不能取消后续
周期生成的新实例。同进程多 kernel 因此积累后台线程与跨用例活动。

## 决策

1. 资源所有者负责关闭：runtime 级联到 Engine 和根 Scheduler，Engine 关闭其注入的
   Scheduler。两者不能假定同一实例，内联 Engine 配置可以建立独立的调度器。
   公共基类增加默认空生命周期入口，避免让已有无后台资源的扩展变成不可实例化。
2. 关闭阻止新提交，取消队列和周期注册，让已开始的 Job 完成。`wait=True` 等待
   Job、默认 executor 与 loop 线程退出；`wait=False` 允许异步收尾。重复关闭可等待
   第一次非等待式关闭启动的收尾过程。保留 JobInfo 供关闭后查询。
3. 提交与关闭的接纳使用线程锁排序；队列和轮盘的清理仍在自有 loop 上执行。
   与关闭竞争的提交要么被拒绝，要么进入可收尾的状态，不得产生悬挂 Future。
4. `LoopRunner` 负责线程/loop 的实际释放。业务收尾由 Scheduler 决定，Runner 仅
   取消剩余协程、关闭异步生成器及 executor；关闭后不重新建立线程。
5. runtime 即使清理某个组件失败也尝试后续组件，避免摄入退出错误留下调度线程。
6. 适配 dreaming 后，runtime 的关闭由一个后台线程统一收尾，重复调用共享完成结果。
   即使使用 `wait=False`，也必须先等待引擎和根 Scheduler 退出，再释放 dreaming
   leader 锁；清理失败保留锁并在后续 `wait=True` 时传播异常。保留上游同循环
   子任务提交、父子取消信号以及权限失效停摆行为。

## 拒绝的方案

- 仅把 wheel entry 标为完成：只能停周期触发，`run_forever` 线程仍存活。
- 仅关闭 API 的 Scheduler：漏掉 Engine 内联装配的独立实例。
- 立即取消正在运行的 Job：同步后端经 `to_thread` 执行时无法随协程取消而停止；
  会出现 API 宣称退出而后端仍写入。选择显式等待真实工作收尾。
- 为基类新增抽象 `close`：无后台资源的第三方实现无需重写一个空方法，保持兼容。
- 在 Engine 中读取全局 Factory 缓存寻找 Scheduler：无法覆盖匿名实例，还会影响
  另一个 kernel 的资源所有权；直接由持有者关闭。

## 验证

新增测试覆盖两个 Engine 的具名/内联 Scheduler、同进程两个 runtime、关闭异常传播、
周期退出、排队取消、运行中成功/失败、重复/并发关闭、关闭与提交竞争、自线程防死锁、
Runner 取消残余协程与等待 executor。

```bash
python -m pytest tests/unit/control tests/unit/api \
  tests/unit/common/test_loop_runner.py tests/unit/jiuwen_memory_entry -o addopts=-ra
```

测试使用真实线程/loop、真实 runtime 装配与离线 Job，不调用外部模型或存储服务。
早期基线另执行过 Runner 与 Scheduler 提交/关闭竞争各 100 次；本轮重新运行上述
相关套件，并新增等待/非等待关闭下 dreaming leader 锁释放顺序的两项回归。
跳过项为真实 LLM/Redis 测试及未安装的可选 MCP 依赖；没有宣称覆盖这些集成环境。

## 已知遗留

- `wait=True` 等待在飞任务，没有强制终止任意用户 Job 的期限；永久阻塞的后端仍需
  自己的超时。`wait=False` 不保证返回时所有 I/O 已结束。
- `assemble` 只返回 API，保持既有生命周期；需要显式关闭的调用方使用 `assemble_runtime`。
- 关闭不接管调用方注入的存储连接，也不阻止调用方继续直接调用普通 API；调用方必须
  先停止业务入口。多 kernel 的 Factory 装配并发隔离不属于本问题。
