# JiuwenSwarm 记忆 Adapter

## 模块地图

| 文件 | 职责 |
|---|---|
| agent_memory_provider.py | Provider、HTTP/普通 SDK 客户端、共享 SDK continuity 客户端；continuity_request 校验 DTO 并注入可信上下文 |
| sdk_runtime.py | 同进程共享 runtime、lease、统一有界执行池和真实在途操作计数、关闭状态机 |

## 行为铁律

- 共享池只适用于 mode=continuity、持久 fixed_path SQLite Registry；HTTP 和普通 SDK 不进入共享池。
- Pool 按规范 Registry 路径排他，同路径不同配置/主体返回冲突；Provider 不共享请求安全上下文。
- Registry 路径与配置摘要通过 `jiuwen_memory.api.sdk_continuity_runtime_identity` 取得；Adapter 不直接导入内核配置或安全类型包。
- Provider 在调用线程取得可信安全上下文，客户端只执行已经转换的参数；业务 payload 不能注入 security。
- submit 接受的任务追踪到 concurrent future 实际结束，超时/取消不得传播成底层任务取消。
- SDK continuity 的读写操作共用执行池；globals.sdk_max_workers 默认 16（正整数），globals.sdk_max_queue_size 默认 256（非负整数，0 表示不预留等待容量）。执行及等待槽位合计限制为两者之和，满载时在提交前抛 RateLimitedError，不执行请求。Registry 修改仍由内部实例锁保护；不承诺读写提交顺序，需要读到本次写入时先等待写入完成。
- 并发参数属于可信装配配置，首次 acquire 时校验并纳入共享配置摘要；省略和显式默认值兼容，同 Registry 使用不同参数返回冲突，改值需释放旧 runtime 后重装。取消/超时不归还仍在执行或排队的操作槽位。
- 最后 lease 离开且所有已接受调用结束后关闭；关闭中不准入，关闭失败不复用，不能依赖 GC 释放锁。
- 共享 SDK 的 Portal 由 pool entry 持有，Provider 只借用 handle；单个 Provider 退出不得关闭它。最后 runtime lease 释放并排空在途操作后，先关闭 Portal、等待浏览器请求结束，再关闭内核；Portal 关闭失败时不关闭仍可能被使用的内核，并禁止复用。普通 SDK 仍由 Provider 关闭 Portal。
- HTTP 工具与角色模式沿用既有入口；SDK continuity 暴露任务/恢复工具，日常读写由 prefetch/sync_turn 或 continuity_request 执行。
- Portal 启动授权在签发时固定 Provider key；省略时只允许唯一已注册来源，多来源必须显式选择。请求拒绝空 key，来源注销后不能转用其他 Provider；未知或已注销 key 不得签发新链接。
- continuity 调试日志使用 `[continuity.*]` 标记，记录操作、Session/task/binding/event 身份、条数、缺口和耗时；不记录正文、查询描述或凭证。Provider 沿用宿主 logger 名，sdk_runtime 使用本模块 logger，由宿主按需转发。

## 与其他子目录的边界

MemoryAPI 执行授权和任务操作；Adapter 不维护任务目录或业务任务映射，也不为单个 Session 关闭共享内核。
JiuwenSwarm 的 ContinuityRail、用户候选确认和 metadata.json 激活指针属于上游宿主。
