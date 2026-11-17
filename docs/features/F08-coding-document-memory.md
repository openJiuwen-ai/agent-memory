# Coding 场景接入文档记忆

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-07 |
| 影响范围 | `jiuwen_memory_adapter/jiuwenswarm/` |
| 验证基线 | Provider 单测、SDK 文档持久化与隔离测试、HTTP 集成测试 |

## 背景与决策

Coding 原生主题文件与文档记忆的数据模型不同。本特性在既有 Provider 增加 Code 场景，复用内核抽取、文档存储、影子索引及 read/edit/search/add；core Rail 决定调用时机，Swarm 提供配置、项目和身份。

项目由宿主绑定，项目召回使用精确过滤，画像独立读取；搜索为空或超时回退项目 MEMORY.md。Code 工具不接受任意文件路径，不生成旧主题 frontmatter，空抽取不视为新增记忆成功。

SDK 复用宿主模型配置并开启文档写入。文件配置与内联配置保留各自原有装配基准，只在选定配置后覆盖文档写入开关，避免文件未声明的模型、端点被 Provider 默认配置补入。HTTP 只连接既有服务，由服务端配置模型、文档根、认证和看门狗；不新增 HTTP 接口，不上传 SDK 配置或本地文档。服务部署模板见 `deploy/local/document-memory-http.yaml`，操作说明见 [适配 README](../../jiuwen_memory_adapter/jiuwenswarm/README.md)。

看门狗在删除旧记录前取得文件 Scope，避免文件全部正文被替换后，新记录丢失原身份归属。

## 取舍与边界

- 不合并不同 Agent 的身份；共用 USER.md 物理路径仍遵循 Scope 过滤。SDK/HTTP 的身份映射沿用各自既有契约。
- 不维护第二套内核或分类器，也不使用 `IN ["", project]` 混合项目召回；画像有独立读取入口。
- 不迁移旧主题文件，不为普通文档模式引入 Runtime 池或 Dreaming 改造；同进程文档写入由存储资源锁协调。
- 页面回复结束不代表后台写入完成；HTTP 超时不保证服务端已取消。正式使用需消费方更新到包含本特性的发布版本。

## 验证基线

配套测试不依赖 pytest-asyncio，覆盖文件/内联/默认配置来源一致性，以及 Code 工具参数绑定、路径限制、SDK/HTTP 文档读写编辑、索引与重启恢复、画像共享、项目和 Agent 隔离、服务认证失败及看门狗同步。自动化测试使用确定性模型替身验证行为，不将抽取质量作为确定性断言。

## SDK 多 Runtime 共库协调

普通文档模式下，每个 Provider 仍独立持有 Runtime、看门狗与 SQLite 连接。文档写入、编辑补偿及看门狗完整对账共同获取根目录和索引的资源锁：共享任一资源则串行，两者都独立可并行；Scope 与项目过滤不变。目录和数据库路径归一绝对路径与符号链接，独立 SQLite 内存连接不互锁。写窗口按资源计数，避免独立目录的看门狗被其他目录写入推迟；等待锁后已关闭的看门狗任务跳过。

选择资源锁是为了同时解决旧索引重复补登、并发写事务竞争和整文件回滚覆盖。只串行 SQLite 最后的写入不能避免基于旧状态重复决策；全局锁会让独立目录互相等待；Runtime 池还需处理配置兼容及引用计数，普通文档模式不引入。

仓库内的回归入口与覆盖范围如下；SDK 测试使用离线模型和真实临时文档、索引，手工编辑同步测试启用真实看门狗。

| 已提交测试 | 覆盖范围 |
|---|---|
| [test_memory_scenes.py](../../tests/unit/jiuwen_memory_adapter/test_memory_scenes.py) | Work/Code 并行初始化、交错写入、项目隔离和关闭一方后另一方继续读写 |
| [test_coding_memory_provider.py](../../tests/unit/jiuwen_memory_adapter/test_coding_memory_provider.py) | 画像共享、项目隔离、编辑与重启恢复，以及看门狗同步手工修改 |
| [test_sync_gate.py](../../tests/unit/storage/test_sync_gate.py) | 共享根目录或索引时互斥、独立资源并行、路径别名、嵌套写窗口和超时释放 |
| [test_composite_domain_store.py](../../tests/unit/storage/test_composite_domain_store.py)、[test_composite_storage.py](../../tests/unit/storage/test_composite_storage.py) | 文档编辑/增删改的故障补偿，以及补偿期间的写窗口保护 |
| [test_local_watchdog.py](../../tests/unit/storage/test_local_watchdog.py) | 对账不重复插入、保留身份，以及关闭后跳过等待资源锁的任务 |

边界：同根目录内的不同项目仍共用索引、需要串行；持锁时间包含索引生成，慢 embedding 会延长等待。未协调跨进程或外部编辑器同时修改，不自动清理已有重复索引，不保证并发查询跨文件与索引的事务快照。


## 场景策略与配置装配

Work/Coding 的差异收敛为独立策略，Provider 通过统一协议委托；文档读写共用既有实现。场景工厂与协议分模块，避免注册表反向依赖形成循环导入。Work 工具按名称分派，视频和接续行为沿用原有开关及优先级。

普通 SDK 在解析配置来源后统一应用场景覆盖，移除跨多层传递的 `write_document` 布尔参数。保留 Work 的可覆盖默认值与 Coding 的强制文档要求；没有简单删除后者，因为独立文件或显式 false 配置仍需要场景约束。

注册表只保存工厂，普通文档场景不使用共享策略或 Runtime 单例，保证同时创建 Work/Code 时项目与配置不串用。未引入动态插件发现或新 MemoryAPI，避免将两个现有场景的重构扩展为第二套接入框架。

验证覆盖已有 Work/Coding、视频、接续、SDK/HTTP、Portal，以及新增场景注册、配置来源优先级、工具定义独立性、双模式交错写入和独立关闭。定向回归覆盖上述场景及 SDK 接续、共享 Portal、文档并发存储，共 294 项通过，使用离线模型与真实临时文档、索引及 HTTP 服务；此前已通过实际 Swarm 工厂、Core Rail 和 Memory SDK 的并行初始化、交错写入与独立关闭验证。本次不重复真实 LLM 页面验收。

接续能力保持独立的装配和调用边界：SDK 接续复用既有共享客户端、任务工具、可信上下文及 Portal 租约；
其配置不应用 Coding 的文档覆盖。普通 Work/Coding 保留场景策略和文档资源锁。
同一 Provider 显式开启接续时，预取/回写仍要求任务参数；这不代表当前 Coding Rail 支持两种能力叠加。
