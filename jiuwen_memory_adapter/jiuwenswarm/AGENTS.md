# JiuwenSwarm 记忆 Adapter

## 模块地图

| 文件 | 职责 |
|---|---|
| agent_memory_provider.py | Provider、HTTP/普通 SDK 客户端、共享 SDK continuity 客户端；continuity_request 校验 DTO 并注入可信上下文 |
| experience_author_key.py | 内置 SDK 装配经验评价器之前，获取或原子保存部署作者密钥 |
| memory_scene.py / memory_scenes.py | 场景协议、请求级上下文与独立实例工厂注册表 |
| base_document_scene.py | Code/Team 共用的项目绑定、文档写入配置及预取超时保护 |
| work_memory.py / coding_memory.py / team_memory.py | Work/Coding/Team 工具、预取、项目归属、提示词及普通 SDK 配置要求 |
| pipeline_profiles.py | 普通文档的具名写入配置与 Team 抽取器装配 |
| memory_tools.py | 共用工具定义与文档读取、编辑操作 |
| sdk_runtime.py | 同进程共享 runtime、lease、统一有界执行池和真实在途操作计数、关闭状态机 |

## 行为铁律

- 内置 SDK 的经验文档模式直接输出 `skills_dir/distilled/<skill_name>/SKILL.md`，保留现有同名技能，不启动发布清单同步。HTTP 与其余仍使用技能清单的模式同步时，仅更新或清理带当前空间来源标记且正文摘要未变的文件。

- 经验检索不按 experience_capabilities 或 tier 排除条件动作；该清单只用于非空时的技能同步筛选。空间授权及经验类型、类别过滤保持生效。

- Team 自动回写和主动提炼通过 add 的 `system_metadata["sources"]` 提交材料。用户原文、派单和交付均由宿主标记来源。
- 群体请求用可信宿主绑定坐标。HTTP 成员管理使用 Memory 预登记的专用身份，数据请求使用普通身份。

- 普通 Work/Coding 按场景策略执行；接续预取与回写保持优先，SDK 接续只注册任务/恢复工具，不将文档工具交给接续客户端。
- Code/Team 分别继承 BaseDocumentScene，仅复用项目绑定、文档配置与超时保护；工具、Pipeline 和召回策略各自定义，Team 不继承 Code。
- 普通 Coding/Team 在配置来源解析后覆盖 write_document=true；SDK 接续保留独立配置基准，不应用场景覆盖，HTTP 不应用本地 SDK 配置。
- 注册表仅保存工厂，每个 Provider 创建独立场景；普通文档可注入宿主持有者共享 Runtime；未注入时各自持有。资源锁与接续共享池保持独立。
- sdk_runtime.py 中的共享池只适用于 mode=continuity、持久 fixed_path SQLite Registry；HTTP 和普通 SDK 不进入共享池。
- Pool 按规范 Registry 路径排他，同路径不同配置/主体返回冲突；Provider 不共享请求安全上下文。
- Registry 路径与配置摘要通过 `jiuwen_memory.api.sdk_continuity_runtime_identity` 取得；Adapter 不直接导入内核配置或安全类型包。
- 经验文档开关通过 `jiuwen_memory.api.should_write_document` 归一。
- 经验作者密钥由内置 SDK 装配注入，部署配置和环境变量优先；本地生成的密钥存放于部署配置目录。
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

Team 的项目、团队和 Group 坐标由宿主绑定。工具使用同一绑定的读写客户端。项目日记及记忆文件的文档边界由 Memory 校验，项目或团队的预取兜底使用已授权文档。

Work 给定项目时，自动召回与搜索工具使用 project 等值过滤；画像独立读取 USER.md。
Team 预取画像、项目和团队文档及语义召回并行且分别限时。项目和团队独立采用召回或文档兜底，单段异常保留其他结果。

普通文档自动同步和显式写入工具统一调用场景 write_route；pipeline/infer 不暴露为工具参数。
SDK 加载配置并应用场景覆盖后，仅当 write_document 归一为 true 时，通过
configure_document_pipelines 装配文档场景流程，保留具名覆盖及 default/video；
非文档 Work 保留原 Pipeline，不注入 Shadow 去重依赖。共享 Runtime 不保存当前场景。
HTTP 服务端需装配同名流程，客户端不创建本地 Runtime。查询、读改文档和后台 evolve 保持既有入口。
