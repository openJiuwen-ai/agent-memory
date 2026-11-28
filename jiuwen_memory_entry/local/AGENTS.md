# 本机记忆服务（local）

## 模块地图

| 文件 | 职责 |
|---|---|
| home.py | 数据目录解析（`JIUWEN_MEMORY_HOME` / `~/.jiuwen-memory`）、`MemoryHome` 路径、`init_home` 模板渲染（0600 原子写） |
| service_template.yaml | 服务配置模板：文档模式 + SQLite 固定路径 + `local_token` + 接续策略开启 |
| locks.py | 私有目录与跨进程文件锁；Portal 与本机服务共用 |
| discovery.py | 发现文件 `run/service.json`、无认证据活、`ensure_service` 拉起（POSIX/Windows/Linux） |
| service.py | 常驻进程：`/local/*` 路由、租约、reload 保端口与 nonce、空闲退出、hook recall/capture/retry、后台恢复 |
| client.py | `ServiceClient`（status/health/lease/release/reload/post） |
| cli.py | `init`（交互终端完成后询问扫描授权）/ `service` / `connect` / `disconnect` / `hook` / `hooks` / `sync` / `scan` / `takeover` / `migrate` / `space` / `op` 子命令 |
| install.py | `connect`/`disconnect` 步骤 0–6：安装 ID、令牌、连接登记、空间成员、reload、状态核对、宿主配置合并 |
| hosts/ | 宿主注册表 `registry.HOSTS`：每个宿主声明 `CAPABILITIES`、MCP/hook 写入器、hook 默认值、`syncable`/`native_hooks`/`manual_ok` 与实例探测；`MCP_WRITERS`/`HOOK_WRITERS`/`HOOK_DEFAULTS`/`SYNC_HOSTS`/`HOOK_HOSTS` 均由注册表派生。安装/CLI 只消费注册表，新增宿主不需要改安装分支。当前宿主：Codex、DSH、Hermes、WorkBuddy、JiuwenSwarm（原生、不可同步、手动落地） |
| sync/ | 宿主记忆同步：`grant.py` 授权单、`access.py` 受限只读访问、`model.py` 稳定 `ItemId`/`ScanResult`、`ledger.py` 来源/输出/待清理三表、`engine.py` 写入与判删、`space.py` 空间锁/恢复、`plan.py` 冻结计划、`readers/` 四家宿主读取器 |
| turns.py | 每轮待写记录、任务选择、操作记录（冻结请求/回执/状态/保留期） |
| capture_filter.py | 注入记忆剔除与答案截断；召回注入标记常量与 `strip_context_blocks`（同步入账前也调用） |
| scan.py | 本机 Agent 扫描与自动接入（D16）：`ScanGrant`（`scan.json`，0600，`scan_grant_lock` 下读改写）、只看路径的 `PathView`、`ProbeResult`、`probe_all`、`candidates`/`connect_results`、忽略与恢复、待授权同步 |
| hooks.py | 项目解析、绑定选择、recall 预算、capture 冻结与回放、回显鉴权、retry |
| recovery.py | 后台推进非终态操作（经安装令牌走服务自身 `/local/hook/retry`） |
| migrate.py | `plan_migration`/`apply_migration`/`recover_admin`（D12） |

## 行为铁律

- 一个数据目录只跑一个服务进程：启动锁 + 发现文件 + nonce 探活；reload 保持端口与 nonce，空闲退出只在无租约、无在途请求时发生。
- 数据目录只对本人可见：`init_home` 新建的 root/run/installs/data 为 0700；服务启动时 `harden_private_paths` 再把这四个目录收紧为 0700、`data/` 下已有文件收紧为 0600（兼容旧版本创建的目录），并把进程 umask 设为 077，之后服务写出的所有文件（含 SQLite journal/WAL 与 Markdown 文档目录中的文件）都只对本人可读；`TurnStore` 打开时把数据库文件设为 0600。宿主配置文件（如 Codex `hooks.json`、WorkBuddy `mcp.json`、Hermes `config.yaml`）解析失败时一律报错，不得当作空配置覆盖。
- 令牌摘要为准、令牌文件为准：`installs/<id>/token` 是唯一凭据来源；安装记录只是进度缓存，任何一步可安全重跑。
- `connect` 的 capture/project 与 connector 写入是确认后的候选策略：确认前只生成临时安装/令牌，不更新安装记录或 connector；已连接安装取消重连时保持原连接、原令牌与原策略不变。确认前任何异常也不得把已连接安装改成 `installing`，否则下次取消会误删原连接。
- 安装令牌主体只带身份（org + user/agent），space/session 必须为空；项目是每次调用的 target 维度。唯一例外是 `migrate` 用 `allow_scope=True` 登记已核实的完整旧主体，否则 owner 指纹与原绑定无法匹配。
- recall 不能阻塞宿主：2 秒超时、16 KiB 预算、失败返回空视图；慢搜索进入有界执行器（并发上限 + Runtime 关闭前 drain），超时只结束等待，不无限起线程。capture 以回答摘要识别同一轮，不依赖已过期的 pending；同轮回答不同返回 `conflict`，永不写第二次。
- 写入是否发生只由操作记录说话：`prepared` 可能已执行，`unconfirmed` 不等于未写；未写只有绑定校验失败或没有待写记录。
- 回执/refs 回显必须逐个做读权限校验；无权限只回状态，不回正文、refs 或 operation_id。
- 恢复经安装自己的令牌走正常 PEP；不得用系统身份或跳过授权直接写。令牌缺失或 401/403 时只做内核只读回执核对：partial/unknown 在撤权后转 `stopped`，并保留原回执的 operation_id/refs；网络异常、409、5xx 是临时失败，只刷新只读回执并下轮重试，绝不能转终态。每次 Runtime 装配单独持有恢复线程的停止事件，关闭 TurnStore 前先停线程并 join。操作推进锁按操作 ID 进程级共享，只在终态（committed/rejected/stopped/expired）释放；非终态锁保留到操作终态。推进/重放前先查内核回执：已提交不重写；partial/unknown 按 `retry_action` 在仍有权时推进，权限失效则停止。
- `connect`/`migrate` 对 `service.yaml` 与安装记录的读改写必须持同一文件锁；`service stop` 必须先探活并比对 `instance_nonce`，过期发现文件只清理不 kill。
- 同步先授权后读文件：`SyncGrant` 固定来源根、类型、目标空间、镜像删除与授权版本；读取器只经 `SourceAccess` 打开文件，路径必须解析在授权根内，类型必须已授权；`/local/sync` 不接受请求体自行扩大范围。`sync` 的账本身份包含来源根与目标空间；`sync_source` 记录 `writing/retryable/ok/unconfirmed/cleanup_pending`，`sync_output`/`sync_ref` 记录输出归属与逐来源引用；`space_lock` 串行化同一空间的写入/清理，`recover_source`/`recover_space` 只读判定后统一解除引用并删除（支持原子比较删除时用条件删除，默认后端在空间锁内比较摘要后普通删除，存在短窗口），`cleanup_pending` 只做幂等收尾、不重复登记回执引用；`resolve` 才落人工终态。判删只在完整扫描的范围内进行，不能按整个安装推断源端删除。同步写入只走内核标准 `add`：归属键 `sync_created_by` / `sync_initial_digest` 与其他来源键一样放进 `system_metadata`，不另设端口；恢复时先查 `supports_operation_receipt()` 再探测 `operation_receipt` / `receipt_evicted`，不得按 `hasattr` 猜内核有没有回执能力。
- 扫描只看路径：`probe` 只经 `PathView` 做 `stat`/列目录，不打开文件；未授权扫描时 `probe_all` 返回空。自动接入只接 `found` 实例，按 D14 默认 hook，从不自动开启同步授权或接管；用户断开或取消勾选的实例记入 `dismissed`，`scan include` 才恢复。服务内的定期扫描只拉起独立的 `jiuwen-memory scan run --background` 子进程，不在服务进程内调用 `connect()`（connect 会触发 reload 并 join 调度线程）。
- 当前宿主能力矩阵（`hosts.CAPABILITIES`，列序：MCP / 自动召回 / 自动写入 / 记忆导入 / 会话导入 / 接管自带记忆）：Codex 为 `supported×5/unverified`；DSH 为 `supported/supported/supported/unsupported/supported/unsupported`；Hermes 为 `supported/supported/supported/supported/unverified/unverified`；WorkBuddy 为 `supported/unverified/unverified/supported/unsupported/unverified`；JiuwenSwarm 为 `supported×5/unverified`，走原生 plugin。未核实不等于支持；目前没有宿主提供 `TakeoverWriter`，`takeover on` 一律返回不支持（退出码 3）。
- 接管（`set_takeover`）只在 hook 开启、能力为 `supported`、宿主提供写入器且该安装同步已完成时才修改宿主配置；安装记录 `takeover` 保存原值，`disconnect`、关闭 hook、重连关闭 hook、`takeover off` 时恢复，用户改过的值保留。
- 宿主配置只增不删：合并写入保留其他工具条目，回滚只删本工具写的条目/本次写入且未被用户改动的字段，用户改过的保留并上报；DSH 记录本次新增的 profile，成功重连与原记录取并集，断开时只卸载这些 profile，不能只删本地标记；查询失败不得当成未安装。
- `migrate` 授权只来自旧配置或可信导出；无法确认主体时 `unverified` 并列出损失。目标 connector 必须在启用且覆盖目标项目的候选中与已确认 actor 唯一匹配，否则签发前报错；保留每个旧 connector 的 actor/project/enabled。用户存量任务记录迁移前 owner，事件样本按关联 task 补齐 owner，只读比较同一任务迁移前后 owner；未解析 owner 的 403 不算成功，必须有当前主体的读取后备探针；写入探针必须在独立验证任务中执行并检查 close 结果，核对失败恢复原 `service.yaml` 并留下 `service.yaml.failed`。`space recover-admin` 经运行中服务的空间组件维护接口执行，不写第二套存储，也不提升安装令牌。

## 与其他子目录的边界

内核（`jiuwen_memory/`）拥有授权、接续真源、存储与审计；本目录只做进程、发现、安装、hook 编排与迁移，不实现第二套授权。
Access 规则：本目录与 `jiuwen_memory_adapter/` 只能导入 `jiuwen_memory.api`，不得导入内核其他子模块（`tests/unit/api/test_access_api_boundary.py` 强制）。
宿主适配器（Codex/DSH/JiuwenSwarm/MCP）只翻译事件与转发；recall/capture 决策在本目录的 `hooks.py`。
Portal 共享工具（启动锁、私有目录、IPC）来自本目录 `locks.py`；Portal 自身不承载内核。
