# JiuwenSwarm 记忆 Adapter

## 模块地图

| 文件 | 职责 |
|---|---|
| agent_memory_provider.py | Provider、HTTP/普通 SDK 客户端、共享 SDK continuity 客户端；continuity_request 校验 DTO 并注入可信上下文 |
| memory_scene.py / memory_scenes.py | 场景协议、请求级上下文与独立实例工厂注册表 |
| base_document_scene.py | Code/Team 共用的项目绑定、文档写入配置及预取超时保护 |
| work_memory.py / coding_memory.py / team_memory.py | Work/Coding/Team 工具、预取、项目归属、提示词及普通 SDK 配置要求 |
| pipeline_profiles.py / team_policy.py | 文档写入具名配置、宿主结论的标签/内容/项目绑定校验 |
| memory_tools.py | 共用工具定义与文档读取、编辑操作 |
| sdk_runtime.py | 同进程共享 runtime、lease、统一有界执行池和真实在途操作计数、关闭状态机 |

## 行为铁律

- 普通 Work/Coding 按场景策略执行；接续预取与回写保持优先，SDK 接续只注册任务/恢复工具，不将文档工具交给接续客户端。
- Code/Team 分别继承 BaseDocumentScene，仅复用项目绑定、文档配置与超时保护；工具、Pipeline 和召回策略各自定义，Team 不继承 Code。
- 普通 Coding/Team 在配置来源解析后覆盖 write_document=true；SDK 接续保留独立配置基准，不应用场景覆盖，HTTP 不应用本地 SDK 配置。
- 注册表仅保存工厂，每个 Provider 创建独立场景；普通文档可注入宿主持有者共享 Runtime；未注入时各自持有。资源锁与接续共享池保持独立。
- sdk_runtime.py 中的共享池只适用于 mode=continuity、持久 fixed_path SQLite Registry；HTTP 和普通 SDK 不进入共享池。
- Pool 按规范 Registry 路径排他，同路径不同配置/主体返回冲突；Provider 不共享请求安全上下文。
- Registry 路径与配置摘要通过 `jiuwen_memory.api.sdk_continuity_runtime_identity` 取得；Adapter 不直接导入内核配置或安全类型包。
- Provider 在调用线程取得可信安全上下文，客户端只执行已经转换的参数；业务 payload 不能注入 security。
- submit 接受的任务追踪到 concurrent future 实际结束，超时/取消不得传播成底层任务取消。
- SDK continuity 的读写操作共用执行池；globals.sdk_max_workers 默认 16（正整数），globals.sdk_max_queue_size 默认 256（非负整数，0 表示不预留等待容量）。执行及等待槽位合计限制为两者之和，满载时在提交前抛 RateLimitedError，不执行请求。Registry 修改仍由内部实例锁保护；不承诺读写提交顺序，需要读到本次写入时先等待写入完成。
- 并发参数属于可信装配配置，首次 acquire 时校验并纳入共享配置摘要；省略和显式默认值兼容，同 Registry 使用不同参数返回冲突，改值需释放旧 runtime 后重装。取消/超时不归还仍在执行或排队的操作槽位。
- 最后 lease 离开且所有已接受调用结束后关闭；关闭中不准入，关闭失败不复用，不能依赖 GC 释放锁。
- 符合条件的 SDK 文档实例按固定 Markdown 目录和 SQLite 索引共享本地 Portal。配置覆盖和实例 ID 归 Runtime 所有，每个 Provider 分别注册浏览器授权来源。ID 或配置库冲突只影响 Portal 接入。退出时先注销来源并排空其请求，Runtime 确认关闭后才释放实例 ID。
- 其他 SDK 接续实例的 Portal 由 pool entry 持有。Provider 只借用 handle，退出时注销自己的来源。最后 runtime lease 释放并排空在途操作后，先关闭 Portal、等待浏览器请求结束，再关闭内核；Portal 关闭失败时不关闭仍可能被使用的内核，并禁止复用。
- HTTP 工具与角色模式沿用既有入口；SDK continuity 暴露任务/恢复工具，日常读写由 prefetch/sync_turn 或 continuity_request 执行。
- 进程内 Portal 启动授权在签发时固定 Provider key；省略时只允许唯一已注册来源，多来源必须显式选择。请求拒绝空 key，来源注销后不能转用其他 Provider；未知或已注销 key 不得签发新链接。
- continuity 调试日志使用 `[continuity.*]` 标记，记录操作、Session/task/binding/event 身份、条数、缺口和耗时；不记录正文、查询描述或凭证。Provider 沿用宿主 logger 名，sdk_runtime 使用本模块 logger，由宿主按需转发。

## 与其他子目录的边界

MemoryAPI 执行授权和任务操作；Adapter 不维护任务目录或业务任务映射，也不为单个 Session 关闭共享内核。
JiuwenSwarm 的 ContinuityRail、用户候选确认和 metadata.json 激活指针属于上游宿主。

Team 结论在 Provider 内完成批次校验和可见标签前缀规范化；无效项仅记录原因计数，非空批次全无效时写前报错，空提取返回 0。
Team Provider 的项目和团队坐标由宿主绑定，模型工具不能覆盖。Provider 选择 team_conclusion，团队类别由其 Classifier 生成，不能绕过保留键校验。项目/团队过滤由 Shadow 索引执行；全文和团队精确召回在截断前过滤，单独排除团队类别的向量路径沿用 KNN 候选后 SQL 过滤。反向路径解析、编辑补偿和看门狗使用同一团队键规则。

Work 给定项目时，自动召回与搜索工具使用 project 等值过滤；画像独立读取 USER.md。
Team 预取的三份文档和两段搜索并行且分别限时，命中优先，否则用对应兜底文档；单段异常不清空其他段。

普通文档自动同步和显式写入工具统一调用场景 write_route；pipeline/infer 不暴露为工具参数。
SDK 加载配置并应用场景覆盖后，仅当 write_document 归一为 true 时，通过
configure_document_pipelines 装配文档场景流程，保留具名覆盖及 default/video；
非文档 Work 保留原 Pipeline，不注入 Shadow 去重依赖。共享 Runtime 不保存当前场景。
HTTP 服务端需装配同名流程，客户端不创建本地 Runtime。查询、读改文档和后台 evolve 保持既有入口。
