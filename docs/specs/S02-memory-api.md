# S02 — 记忆接口层（Memory API Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/api/ |
| 最近一次修订日期 | 2026-11-12 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md，docs/features/api/F05-http-memory-api-alignment.md，docs/features/api/F06-access-parameter-forwarding.md，docs/features/api/F07-access-surface-error-semantics.md，docs/features/F08-personal-memory-portal.md |
| 关联特性文档 | docs/features/api/F01-memory-api-impl-design.md，docs/features/api/F02-write-infer-extract.md，docs/features/api/F03-batch-write-api.md，docs/features/api/F04-memory-metadata-separation.md，docs/features/api/F05-http-memory-api-alignment.md，docs/features/api/F06-access-parameter-forwarding.md，docs/features/api/F07-access-surface-error-semantics.md，docs/features/api/F11-batch-update-api.md，docs/features/F01-system-spec-design.md，docs/features/construction/F02-dynamic-extraction-consolidation.md，docs/features/construction/F04-cc-memory-compat.md，docs/features/construction/F05-construction-spec-multimodal-design.md，docs/features/construction/F08-entity-schema-extension.md，docs/features/common/F01-memory-layer.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F08-memory-tree.md，docs/features/common/F09-log-privacy.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/retrieval/F08-query-rewrite.md，docs/features/control/F04-permission-context-routing.md，docs/features/control/F05-cloud-engine-design.md，docs/features/config/F01-config-source.md，docs/features/control/F07-collective-memory-design.md，docs/features/F04-dreaming.md，docs/features/ingest/F02-assets-ingestor-boundary.md、[F04 安全能力与设计决策](../features/common/F04-security-interfaces-and-encryption.md) |

## 明文读取与独立维护边界

独立维护工具不改变 MemoryAPI 的方法、参数、返回类型及公共导出。加密读取遇到明文时继续
按既有 BackendError 异常契约失败，错误信息提示显式迁移或清空；不能返回明文、空结果
或新增错误结果对象，不增加 HTTP/CLI/MCP 的业务方法及专用状态码转换。

源码部署工具 `python -m deploy.migration.encrypt` 独立提供 inspect/migrate/clear；
编排、格式校验和逐项结果属于 deploy/migration，不从 jiuwen_memory.api 导出，
也不在启动、搜索或读取中调用。部署配置、凭据、支持范围及退出码见 F04 §5.3，
底层可选维护能力见 S06。


## 文档分工

对外接口相关内容分四处维护，互不替代。

| 位置 | 记什么 |
|---|---|
| [architecture.md §6](../design/architecture.md) | **已实现**接口的清单（方法 / 语义 / 入参 / 出参） |
| `jiuwen_memory/api/` | **已实现**接口的代码（`memory_api.py` 契约，`memory_api_impl/` 实现） |
| 本文（S02） | 对外接口的详细介绍与用法：含已实现，以及已设计但尚未实现的增量；尚未实现处用 **状态：已设计、尚未实现** 标明。方法总览同时指向对应特性文档 |
| `docs/features/api/`（F01–F07） | 特性决策（为什么这样改）；不重复罗列全部方法签名。各 F 覆盖哪些方法见下文方法总览与特性文档对照 |

已设计但尚未实现的接口完成代码开发上库时：去掉本文（及受影响 F 文档）中的尚未实现标注，并把该方法（或增量入参）补进 architecture.md §6。
## 个人记忆公开接口

普通 `list(scope, ...)` 和 `search(query, context, ...)` 继续使用原返回结构。
默认个人视图复用这两个公开方法，个人模式需显式启用：

- `list` 通过 `extensions={"personal": {...}}` 传列表筛选条件及可选的
  `snapshot`、`include_lifecycle_counts`，省略 `scope`。个人模式使用
  `personal` 中的筛选条件，不与普通 `filters`、`memory_types` 或其他扩展混用。
  MCP 的 `memory_list` 遵循同一规则。按指定范围查询时仍需提供 `scope`。
- `search` 在 `context.extensions` 传 `{"personal": {"source": "应用标识"}}`，
  `context.scope` 留空。支持 `query`、`top_k` 和可选来源。与 `spaces`、
  职责视图参数（`duty`、`task_id`、`anchor`、`view_budget`）或其他检索选项组合时，
  返回 `ValidationError`。个人结果包含各命中的完整 Scope 和排序口径。

SDK 和 HTTP Portal 共用这两个入口。API 在当前记忆实例内按认证身份发现候选范围，
并逐范围执行原接口鉴权。宿主可为新增绑定写入位置，列表与检索仍按个人可读范围查询。

发现前先校验可信请求身份，随后逐范围判权。个人范围有用户标识时只选择本人；已有
共享范围中的记录还须具有可验证的作者归属。不能确认属于当前用户的记录不自动纳入。
这条规则不赋予 Portal 共享管理权限。

| 操作 | 行为 |
|---|---|
| list | 各范围读取完成后汇总、筛选、计数和分页，返回 PersonalPage |
| get | 读取指定范围和 ID 的当前记录，复核绑定与作者归属 |
| add | Portal 入口确定个人写入范围并标记 `source_app=portal`，再调用原 `MemoryAPI.add` |
| correct | 先复核当前记录，再调用 update(SUPERSEDE) 创建新版本，返回实际新记录 |
| forget | 先复核当前记录，再调用 delete(FORGET)，返回实际处理的 ID |
| search | 逐范围调用 MemoryAPI.search，每个结果再通过 get 读取最新授权正文和属性 |

列表按 Scope 和记录 ID 固定排序。默认模式对每个候选范围执行一次授权读取，
跳过范围级拒绝，不在读取记录后吞掉鉴权或存储错误。跨 Scope 没有全局事务快照。聚合上限为10000条，超过则返回容量错误，不静默截断。
后续页必须携带第一次读取的 snapshot；成员或来源变化时返回冲突，要求重新分页。
snapshot 检查成员和来源，不冻结正文，详情仍需重新读取。

### 列表筛选与选项

所有条件在完整授权集合上执行，再计算 count、snapshot 和分页。来源、Agent、项目及
分类选项从筛选前的完整集合生成，不能只使用当前页。None 表示不限制该条件。

| 参数 | 匹配对象 | 空字符串的含义 |
|---|---|---|
| source | system_metadata.source_app | 未标注来源 |
| agent | scope.agent，agent_label 仅作显示名 | 未关联 Agent |
| project | system_metadata.project 优先，coords.project 兜底 | 跨项目／未标注 |
| tier | MemoryTier.value | 不接受空串，须使用有效枚举或 None |
| memory_class | system_metadata.memory_class | 未分类 |
| lifecycle | active／superseded／forgotten／archived；None 为全部状态 | 不接受空串 |
| query | 正文或标签的大小写不敏感子串 | 不增加文字条件 |

MemoryTier 完整枚举为 working、core、episodic、semantic、procedural、archival。
记忆分类为独立字符串，支持自定义类别；tier=archival 与 lifecycle=archived 是不同维度。

PersonalPage 返回 items、count、snapshot、sources、agents、projects、memory_classes。
传 `include_lifecycle_counts=true` 时，`lifecycle_counts` 返回应用其他筛选条件之后、
应用状态条件之前的各状态计数。
agents 在 Python 接口中是一组 `(id, label)` 元组，HTTP 返回对应的二元素数组。
其他选项集合使用字符串。

### 个人检索

来源过滤先于候选数量截取。个人全量检索显式包含全部项目及缺少项目字段的旧 KV 记录，
并与作者、来源条件按 AND 合并；原有 Scope、权限及内核策略限制继续生效。普通
MemoryAPI.search 的默认项目行为不变。

个人检索的每个子查询须在 top_k 截断前限定到精确绑定 Scope（包含 session）的已授权
记录集合，来源与作者过滤也须先应用于候选集合。需要按条复核角色证据的记录按普通检索
规则排除在召回候选之外，列表和详情继续按原规则检查证据。返回定位符必须能在同一 Scope
严格回读。普通文档召回跨 session 的行为
不变。个人检索候选总量上限为 10000，文档模式还受 SQLite 连接的查询参数上限约束。
无法取得完整候选快照时明确失败，不丢弃异常
命中后把剩余结果当成完整结果。

个人检索从各个 Scope 的排名中轮流取结果，再截取 top_k，响应的 scores_comparable
为 false。分组以完整 Scope 为准。子查询失败返回错误，不能
将部分结果当成完整结果。返回正文、摘要及 metadata 来自刚通过鉴权的原记录。

## 配置管理公开接口

`assemble_managed_runtime` 和 `assemble_managed_server_runtime` 接受可信宿主提供的
配置存储、可管理的具名 LLM、地址策略及授权回调，在同一次装配中返回 configuration。
`ManagedConfiguration.read`、save、retry_publication 每次调用都要求授权回调明确放行；
宿主负责校验可信身份、实例权限及审计。保存返回持久版本和 applied，不返回密钥。
此能力不改变通用 admin_set 的行为。

`HttpServer.build_personal` 在同一个 Runtime 中提供个人记忆和配置接口。普通 HTTP
启动时自动发现个人记忆范围，安全配置声明固定个人身份时才会自动定位配置库。
SDK 和 HTTP 共用定位顺序，优先使用文档根目录，其次使用实际加载的配置文件路径。
可信宿主可以显式接入配置存储和授权。

所有候选位置都无法创建配置库时，记忆服务仍可运行，页面无法编辑设置。已有的保存配置
无法读取时，启动报错。字段校验、版本、凭据和应用规则见 S08。

## HTTP 托管配置接口

两个接口使用与现有 HTTP API 相同的认证方式，也接受 Portal 浏览器会话。
当前实例的 `ManagedConfiguration` 决定调用方是否有权读写，以及哪些配置项可以管理。
`fields` 列出当前开放的配置项；`changes` 和 `restore` 使用其中的完整配置键。
当前可写项来自已装配的 LLM 字段。响应禁止缓存。

| HTTP 路径 | JSON 请求 | 主要响应字段 |
|---|---|---|
| `POST /v1/read_config` | 空对象 `{}` | 未装配配置管理时返回 `available: false`。可用时返回 `available: true` 及 `version`、`applied_version`、`apply_state`、`fields`、`instance` |
| `POST /v1/save_config` | 保存并应用时传 `version`、`changes` 和可选的 `restore`。应用失败后可单独发送 `{"retry": true}` 重试应用最新已保存版本 | `version`、`applied` |

## Portal 页面操作与接口对应

浏览器使用同源 JSON POST。页面通过会话获取认证身份，使用 Scope 和记录 ID 定位已有
记录。服务端按当前身份检查记录归属和权限，历史记录缺少用户标识时还会核对作者。
普通 HTTP 客户端继续使用原认证、请求和返回格式。

| 页面操作 | HTTP 路径 | 请求内容 | 主要响应字段 |
|---|---|---|---|
| 列表、筛选和概览 | `/v1/list` | `offset`、`limit`；筛选、分页快照和概览计数选项放在 `extensions.personal` | 个人列表原始结果；浏览器会话额外取得 `add_context`，包含 `agent` 和 `agent_locked` |
| 语义检索 | `/v1/search` | `query`、`top_k`；来源放在 `context.extensions.personal.source` | `items`、`scores_comparable`；每个命中携带 `scope` 和 `item` |
| 读取详情 | `/v1/get` | `scope`、`unit_id` | 一条 `MemoryUnit` |
| 新增记忆 | `/v1/add` | `content`、`scope`（只允许选填 `agent`）、可选 `system_metadata.project` | 新增的 `MemoryUnit` 数组 |
| 纠正记忆 | `/v1/update` | `scope`、`unit_id`、`patch.content` | 新版 `MemoryUnit`，通过 `supersedes` 指向旧版 |
| 遗忘记忆 | `/v1/delete` | `selector.scope`、只含一个 ID 的 `selector.unit_ids`、`selector.mode: "forget"` | 已遗忘的 ID 数组 |
| 模型设置 | `/v1/read_config`、`/v1/save_config` | 见 HTTP 托管配置接口 | 同上 |
| HTTP 宿主取得页面链接 | `/portal/api/launch` | 使用原认证凭据，可带 `write_target`（`space`、`agent`、`session`） | `portal_url` |
| 浏览器兑换会话 | `/portal/api/bootstrap` | `token`，可选 `embedded` | 独立页面建立 HttpOnly Cookie，iframe 获得页面会话票据 |

页面从 `MemoryUnit.segments` 拼接正文，时间取 `temporal.t_ingest`。HTTP
`write_target` 由宿主提供，与认证身份合成完整写入范围，随会话绑定。独立访问可以
选择 Agent，组织和用户取自认证身份。新增、纠正、遗忘继续由原 `MemoryAPI` 执行写入鉴权；
浏览器纠正使用 SUPERSEDE，遗忘使用 FORGET。两种操作都要求目标仍有效，否则返回 409。
文档写入和索引仍使用原链路。
启用文件监听时，普通文件同步会保留纠正历史和遗忘状态。

SDK 插件默认启动本地页面服务，`portal.enabled` 可关闭它。`portal.port=0` 时自动分配
端口。重复初始化会复用原实例和监听。端口冲突时，记忆实例继续运行，`portal_status`
报告页面不可用，显式打开页面时会重试。HTTP 模式使用原服务端口。两种模式都通过
`open_portal()` 为每次打开取得一次性链接。SDK 页面句柄只关闭自己的网络资源，
Runtime 仍由宿主负责关闭。
宿主可在初始化前读取 `portal_status["supported"]` 来决定是否显示入口。
SDK 模式的 `available` 表示页面监听已启动。HTTP 模式在首次请求链接前为 `False`，
之后反映取链接是否成功。HTTP 初始化沿用 `/healthz` 探活，在调用 `await open_portal()`
时签发链接。`portal_url` 仅提供 SDK 本地链接。

身份复用既有认证或本地会话，浏览器不能声明 actor 或角色，也不能指定列表或检索的授权范围。本机部署的 Host、
同源检查和会话规则见 S10；Prompt 管理、通用模型依赖追踪和宿主重启不属于 Portal 能力。

## Runtime 关闭契约

`MemoryRuntime.close(*, wait: bool = True)` 幂等。关闭时先停止看门狗和新调度/摄入，
在飞任务收尾之后才释放 dreaming leader 锁、回收 Runtime 自有循环线程，再释放影子索引及安全依赖。
`wait=True` 等待上述流程完成；`wait=False` 安排后台收尾，已进入的同步调用和必要的归档
收尾仍可能继续执行，后续可再次调用 `close(wait=True)` 等待全部完成。关闭后不允许重新
启动 Runtime。不得在 Runtime 自有循环线程或调度器所属循环内阻塞等待自身关闭。
异步调度器的详细关闭契约见 S03；直接使用 MemoryAPI 的业务方法不代替 Runtime 生命周期管理。

关闭前先校验是否会等待所属工作线程自身；校验失败不启动收尾。资源清理按步骤隔离异常：
一个步骤失败不跳过其他安全的停止动作。无法确认看门狗、摄入或调度任务停止时，保留
leader 锁与存储；确认停止后，锁释放失败不阻断存储清理。影子索引获取或关闭失败不得
静默忽略。`wait=True` 等待本次安全清理尝试结束后报告失败，多项失败通过 ExceptionGroup
汇总；先 False 后 True 同样可获知后台异常，重复关闭不重复执行清理。自有循环停止请求
失败时，不继续阻塞等待其线程退出，也不宣称循环已关闭。

## Metadata 公共 API 契约

接入来源标记约定：jiuwenswarm 写入的 `system_metadata.source_app` 为 `jiuwenswarm`，
`agent_label` 为适配器配置的 agent 标识，供展示与 `system_metadata.<key>` 筛选；
它们不是可信身份或授权依据。历史记录及多来源派生记录可能缺少这些字段。


`add` / `add_async` / `batch_add` 以及 `BatchWriteItem` 分别接收
`system_metadata` 和 `user_metadata`，不再接收混合 `metadata`。`MemoryPatch` 对两个
dict 分别做 merge-update。用户过滤的规范路径为 `user_metadata.<key>`；裸自定义
字段仅在规范化边界作为该路径的兼容写法，`metadata.<key>` 拒绝。

群体记忆的内核字段（作者标记、判定命中的类别名、判定标签键）全部落 `system_metadata`：
它们由内核写入、内核解释并参与判定与检索谓词，谓词路径为 `system_metadata.<key>`。
`system_metadata` 同时是对外入参，因此写入与改写入口拒绝调用方占用这些键
（`KERNEL_SYSTEM_METADATA_KEYS` 与判定表解析出的标签键集合），见 F07「写入边界校验」。

## HTTP / CLI 与协议适配契约

HTTP 是 `MemoryAPI` 的远程传输封装，不定义第二套业务接口。当前
`MemoryAPI.__abstractmethods__` 的 43 个公开方法全部按以下规则暴露：

```text
POST /v1/<MemoryAPI 方法名>
  -> JSON 字段按同名参数反序列化
  -> 注入认证产生的 security
  -> 调用同名 MemoryAPI 方法
  -> 原返回值递归序列化为 JSON
```

除 `security` 外，请求体字段名、嵌套层级、必填/可选关系和默认值均直接取对应
`MemoryAPI` 签名。`Scope` 使用 `org` / `space` / `user` / `agent` / `session` 原字段，
`Context`、`MemoryPatch`、`DeleteSelector`、`Grant`、`SpaceSpec` 等数据类按原字段嵌套；
枚举使用枚举值字符串，`datetime` 使用 ISO 8601 字符串，`None` 使用 JSON `null`。
省略可选字段时由 `MemoryAPI` 自身的默认值生效。未知字段和数据类内部未知字段均返回
400，不接受 `target`、`item_id`、`k`、`hard`、`modality` 等 HTTP 兼容别名。

`security` 是唯一不从请求体读取的 API 参数。HTTP 从凭据建立认证上下文，并把可信的
`RequestSecurityContext` 以 `security=` 注入同名方法；请求体中的 `security`、`actor`、
`actor_*`、`identity`、`acting_user`、`principal` 和 `authenticated_user` 必须拒绝。
启动器默认使用 `required` 模式；未装配生产认证运行时则 fail-closed 返回 503，不得使用空
Scope 或请求体身份回退。HTTP 认证模式按 `--auth-mode`、`JIUWEN_MEMORY_HTTP_AUTH_MODE`、
`required` 的优先级选择。显式 `dev` 只用于隔离功能测试：未配置身份映射时，服务端
忽略凭据并生成固定具名 USER 身份，ROOT 须由可信认证器配置显式启用；配置 `http.dev_identities` 后，使用 Bearer 或 X-API-Key
选择服务端预设的 actor 与 role，缺失或未知标识返回 401，不回退到默认身份。
两种模式都经受控入口生成 `RequestSecurityContext`，仍执行 `MemoryAPI` 授权；
不能把配置的 role 等同于绕过空间权限。配置位置、字段约束与启动示例统一见
[Config 指南](../zh/API文档/config.md#33-http-开发测试配置多个身份)。
DEV 与身份映射模式均使用同一 Authorizer 判权，不注入 allow_all PermissionManager。
具名 ADMIN/ROOT 按安全域角色与目标动作判权，ADMIN 不自动取得空间内容权限。
dev 只能绑定 loopback，没有环境变量旁路，不得成为默认值或生产降级路径。

同步与异步只保留 Python 调用方式的差异。普通 `def` 方法直接调用；`add_async`、
`batch_add_async` 按原生协程语义执行 `await`，完成后直接序列化各自的
`list[MemoryUnit]` 与 `BatchWriteResult`。HTTP 不因 `async def` 自动返回 202、创建
另一套 job ID 或增加轮询协议。`evolve` 返回任务 ID、`submit_ingest` 返回
`IngestSubmission`，是这两个 API 方法本身的返回契约，与 HTTP 或协程无关。

成功响应统一使用 HTTP 200，`None` 序列化为 JSON `null`，不增加 `{ok, op, result}`
包装，也不向数据类对象插入 `request_id`；错误响应按 HTTP 状态码返回
`{error, message, request_id, retryable}`。返回数据类递归保留其字段，枚举、时间、
集合分别转换为 JSON 字符串、ISO 8601 字符串和数组。

`add`、`add_async`、`batch_add`、`batch_add_async` 请求级 `system_metadata.coords`
是普通元数据注解之外的明确例外：JSON 对象按 `dict[str, str]` 单独校验，允许 `{}`，
不接受 `null`、数组或非字符串键值；其余元数据仍遵守 `MetadataValueType`。
适配层保持字段原位置，归属判定、身份限制与是否落盘均由 API 决定。批量逐项携带该键仍拒绝，
不扩展 `user_metadata`、`MemoryPatch` 或 `check_write`。检索的对应字段仍在
`Context.extensions.coords`。请求示例与使用前提见
[写入归属坐标](../zh/安装指导/SDK部署.md#写入归属坐标-coords)。
此规则修复已有运行时契约的传输误拦截，不代表任意未声明的扩展都被允许。

HTTP 与 CLI 共用 `jiuwen_memory_entry/core/api_contract.py` 的 JSON 契约和同名调用逻辑，
不再经过 legacy `DispatchRequest` / shared handler。CLI 的 43 个命令与参数直接从
`MemoryAPI` 签名生成：方法名原样作为命令，参数原样作为 `--<parameter_name>`，
对象使用同字段 JSON；不保留 `--tenant`、`--item-id`、`--k` 等历史转换。
本地模式直接调用 API，远程模式原样发送 HTTP 请求并读取原 JSON 返回值；`text` /
`table` / `quiet` 只负责展示，不改变返回契约——错误体在任意输出模式下都完整可见
（quiet 不吞错误详情），退出码由真实结果决定（2xx 为 0；HTTP 2xx 但响应体非合法
JSON 属协议层故障，按 502 处理、退出码非零）。MCP 工具的参数模型收紧为
`extra=forbid`，契约外字段与 HTTP/CLI 同口径报错，不再静默忽略（见 F07）。

本地 CLI 的 `security` 同样由认证器与受控入口产生，不从业务参数推导 actor。
默认 `required` 未注入认证器时返回 503；显式 `--auth-mode dev` 才使用固定
`local/developer` 测试身份，并使用上述临时 DEV permission 兼容层。远程 CLI 发送 Bearer 凭据，
认证模式由 HTTP 服务端决定。单次本地调用结束后清理上下文，命令结束后关闭 runtime。
`healthz` 和逐行执行 NDJSON 的 `batch` 是 CLI 辅助命令，不属于 MemoryAPI 方法集，
后者不等于 API `batch_add`，也不增加事务语义。
MCP 与 HTTP/CLI 共用 api_contract.invoke_api，不再经过 legacy dispatch；其他旧调用方
暂留 legacy_request_adapter。MCP 两种传输均默认 required，只有显式
JIUWEN_MEMORY_MCP_AUTH_MODE=dev 才启用固定 DEV 及临时 permission 兼容层。
生产认证从 memory_api.security 装配完整 Runtime；未配置认证时逐请求 fail-closed。
MCP 在工作线程内认证并注入 security，payload 不能覆盖身份。PR2 必须统一接管上游
多身份 HTTP、旧空间/成员权限与作者归属链路，不能另建一条平行授权通道。

### HTTP 错误响应与请求关联

HTTP edge 将成功或失败响应统一写入 JSON；错误响应固定包含：

```json
{
  "error": "ValidationError",
  "message": "safe public message",
  "request_id": "server-generated-id",
  "retryable": false
}
```

`error` 保留异常类名以兼容既有客户端；`message` 是受控的对外文案，400/策略类错误
经过 `safe_error_message()` 脱敏，其余错误使用固定泛化文案。HTTP 不返回 traceback，
也不得在响应中暴露 Authorization、token、API key、密码或 URL 凭据。

| 异常或场景 | HTTP | `retryable` | 对外 `message` |
|---|---:|---:|---|
| `ValidationError` / 字段非法 | 400 | `false` | 脱敏后的字段级原因 |
| `UnsupportedCapabilityError` | 400 | `false` | 脱敏后的能力不支持原因 |
| `PolicyError` | 400 | `false` | 脱敏后的策略错误信息 |
| 缺失或非法凭据 | 401 | `false` | `authentication failed` |
| `PermissionDeniedError` | 403 | `false` | `permission denied` |
| `NotFoundError` / 未知路径或 verb | 404 | `false` | `resource not found` |
| 未支持的 HTTP 方法 | 405 | `false` | `method not allowed` |
| `ConflictError` | 409 | `false` | `resource conflict` |
| `PartialFailureError` | 409 | `false` | 脱敏后的失败原因，并保留 `completed` / `failed` / `retry_action` |
| `RateLimitedError` | 429 | `true` | `too many requests` |
| OpenAI SDK `RateLimitError`（上游模型或向量服务限流） | 429 | `true` | `too many requests` |
| `BackendError` / `HealthCheckError` | 503 | `true` | `service temporarily unavailable` |
| 请求体超过限制 | 413 | `false` | `request body is too large` |
| 未预期异常 | 500 | `false` | `internal server error` |

每个 HTTP 请求由服务端在入口生成唯一 `request_id`，并在所有响应的 `X-Request-ID`
header 返回；客户端提交的同名 header 会被忽略。错误响应同时在 JSON body 写入同一个
`request_id`，成功响应体仍保持 `MemoryAPI` 原返回值，不插入额外字段。该 ID 只用于响应、
日志和审计关联，不参与 actor、target 或权限判定。429 响应额外返回 `Retry-After: 1`，表示
客户端可按整数秒退避；其他状态不返回该 header。CLI 本地调用与 HTTP 共用
`core/error_response.py` 的领域错误状态及脱敏规则，本地错误也带独立 request ID；
命令行参数解析错误仍使用 CLI 退出码。MCP 与旧进程内 legacy dispatch 不受影响。

HTTP 与本地 CLI 保留 OpenAI SDK `RateLimitError` 类型，错误体的 `error` 为
`RateLimitError`，不转换为 `InternalError` 或领域 `RateLimitedError`。消息使用上表固定
文案，不回传上游错误详情；HTTP 沿用 `Retry-After: 1`，不透传供应商的重试头。
其他未映射 SDK 异常仍按未预期异常处理。本契约不增加自动重试或修改现有重试配置。

## 范围 / 边界

**管什么**：
- 统一对外 Core API（形态无关）：所有接入形态（SDK/CLI/Skill/MCP/HTTP·gRPC）最终映射到 `MemoryAPI`
- 鉴权执行点（PEP）：从请求安全上下文取 actor，调用 `Authorizer.authorize` 做入口鉴权
- 入口审计：写审计事件到 `AuditLogger`
- 参数装配：将调用侧参数装配为控制层可消费的内部结构
- 鉴权驱动的编排：按 `security.auth.actor` 决定资源可见范围、生成并回注系统谓词、按判权结果决定取数范围。不变量 9（授权路由值回注查询）与 12（分页命中后逐条 READ 鉴权）是这一职责的两处既有形态；跨空间检索的候选空间枚举、逐空间判权与逐空间系统谓词的生成同属此列。**范围以「判权本身及其直接输入输出」为限**，其余属机械计算或 I/O 编排，落控制层 `control/collective/`，本层只提供回调：

  | 机械部分 | 落点 | 本层提供 |
  |---|---|---|
  | 写入候选空间的渲染、排序与上限截断 | `collective/write_targets.py` | `can_write` 判权回调 |
  | 跨空间的取数上界摊配、召回扇出与结果合并 | `collective/cross_space_recall.py` | `recall` 回调、已判权的空间目标与逐空间谓词 |
- 同步/异步桥接：为同步形态桥接引擎异步协程

**不管什么**：
- 不做业务编排逻辑（全部委托 `jiuwen_memory/control`）——指内容如何抽取、演进、索引这类加工编排；上条所述的鉴权驱动编排不在此列，它是 PEP 职责的延伸。判据是「移出本层是否还能按 `security.auth.actor` 裁决」：逐空间判权循环移出即 PEP 分裂为两处，故留本层；候选集合的计算与跨空间的召回扇出都不读 `security`——前者在判权之前、后者在候选集与谓词都已定妥之后，故不留。逐空间构造 `RetrievalQuery` 的循环本身就是取数编排，随扇出一并移出
- 不直接操作存储
- 不调用 LLM / 构建 / 检索。此处「调用」指调用该层的**算子**——有 Producer 注册、实现可替换、访问模型或存储的组件（`Router` / `Evolver` / `Classifier` / `IndexBuilder` 等）。引用该层导出的类型与无状态纯函数不在此列（见 `construction/router.py` 模块文档：「API 层引用构建层类型是既有形态，依赖方向为 API → 构建，无环」）。归属判定算子的调用落在控制层的 `control/collective/routing.py`，本层调控制层
- 不做 admin 策略存储（直达 PolicyManager）

## 不变量

1. **本层是薄封装 + PEP**：数据面委托 `MemoryEngine`，治理面委托 `Governor`，授权判定委托 `Authorizer`，授权状态管理使用其具名 `GrantStore`，调度面委托 `Scheduler`，策略面直达 `PolicyManager`。API 不实现跨层业务编排，真实处理逻辑由 control 层算子及其下游 construction/retrieval/storage 完成。
2. **调用方身份不下沉**：鉴权通过后只透传已鉴权的 target `scope`，`security` 及其中的 actor 不传入控制层。
3. **`security` 为必填安全输入**：类型为 `common.security.types.RequestSecurityContext`，由接入层的 `authenticated()` 产出，调用方不得自行拼装；actor 从 `security.auth.actor` 取。除 `check_write` 为兼容原第二位置参数而保留 `(scope, security, *, ...)` 外，其余方法均为 keyword-only。
4. **search 参数拆分**：`context: Context` 在本层边界拆开——`context.scope` 作独立轴穿透，`context.extensions` 写入调用级 options；约定 key `context.extensions["max_tokens"]` 由 API 边界解析为 int 后写入 `RetrievalQuery.max_tokens`，并从透传 extensions 中移除；约定 key `context.extensions["query_context"]` 校验为字符串序列、转为 tuple 后写入 `RetrievalQuery.query_context`，同样从透传 extensions 中移除，只作为 parser 输入，不参与鉴权或硬过滤；`Context` 对象本身不进控制层。
5. **admin 不经 Engine**：admin_get/set/all 直达 PolicyManager。
6. **管理面闸门 = 根 scope**：无具体 target scope 的方法（admin_*、全局 audit）以根 scope `Scope()` 为鉴权目标——「能对根 scope 行权」即管理员闸门；租户数据/治理方法仍按各自 target scope 鉴权。
7. **`as_of` = valid-time 回溯点**：`search`/`get` 的 `as_of` 沿系统相信时间轴回溯，返回「那时被认为有效」的版本（`get` 沿 `supersedes` 版本链定位）；`None` 表示当前态。
8. **target scope 兜底**：`delete` 等以 `selector.scope or 根 scope` 为鉴权目标——未限定 scope 的跨范围操作退到根闸门，要求更高权限。
9. **路由鉴权绑定数据范围**：路由型 Authorizer 依据请求中的字段选择策略时，
   API 必须把同一个授权路由值作为系统过滤谓词回注查询，避免「按 A 类型授权、读取
   B 类型数据」。系统谓词与用户 `filters` 以外层 `AND` 合并。
10. **space 是租户隔离单元**：`Scope.space` 参与鉴权、存储命名空间、索引过滤和审计 actor/target 过滤；`scope.require_space=true` 时，具体 target scope 缺少 `space` 的数据/治理操作在 API 层拒绝。org 级 `create_space/list_spaces` 使用 `Scope(org=...)` 做管理面鉴权，不受该策略拦截。
11. **space policy 在 API 边界生效**：已创建 space 的 `principal_path` 由 `SpaceManager.get_policy` 提供，API 在授权判定前写入 `PermissionContext.metadata["principal_path"]`；调用级 metadata 不能覆盖 space policy。
12. **list 按实际资源二次鉴权**：请求显式给出的 `memory_types` 先做类型级鉴权；Engine 再以当前分页实际命中的 MemoryUnit 真源元数据返回权限上下文，API 逐条 READ 鉴权，全部通过后才返回内容。extensions 中仅 `routing_fields()` 声明的路由键参与权限路由，且对应路由值必须作为系统过滤条件回注。
13. **list 过滤和计数在 KV 内完成**：API 复制 `extensions`、规范化 `filters` 后完整下推；返回 `MemoryListResult.items` 当前页和分页前精确 `count`，不以 `len(items)` 代替总数。
14. **六类动态配置不走业务入参**：能力开关、prompt 全文、LLM/Embedder/Reranker 的 model/api_key/url、Store 连接或 `*.active` 等由 `ConfigSource.fetch` 提供（见 S08）；`add`/`search`/`evolve`/`list` 不得把上述值解释为配置写入。调用侧可传 prompt **key**、`memory_type`/pipeline 等业务选择子。
15. **安全输入唯一且不可自造**：`security` 只能来自受控构造入口——接入形态经 `jiuwen_memory_entry.core.auth_middleware.authenticated()`，进程内直连经 `common.security.request_context.internal_context(authenticator)`。请求 payload 不得声明 actor / request_id / surface。（曾存在的 `legacy_request_context` 过渡桥已随 PR2 删除。）
16. **授权面使用安全域授权类型**：`grant`/`revoke` 的公共类型是 `common.security.types.Grant` / `Action`；`grant_id` 由服务端生成、`revoke` 按 `grant_id` 精确定位——缺失 ID 拒绝，未知 ID 幂等无副作用，授权状态真源是 Authorizer 的具名 `GrantStore`（见 S10 的公开授权管理能力）。
17. **层级能力默认关闭**：普通 `add` 默认不建父树；只由显式 `evolve(..., mode=EvolveMode.HIERARCHY, hierarchy_options=...)` 或启用的后台策略触发。显式建树在 `hierarchy.enabled=false` 时生成成功跳过任务；层级查询由 search 的新增可选参数提供，展开与上卷契约见 S04。
18. **三类遍历严格分离（目标）**：`trace` 只沿 `provenance`；树下钻由 search 的 expand_depth 参数沿 `HierarchyRef` 完成；`get(as_of)` 只沿 `supersedes`/valid-time；L0/L1/L2 仅表示同一 unit 的披露层。
19. **API 与 Control 的职责边界**：API 只负责协议边界工作——输入形状和兼容参数校验、请求对象装配、`security.auth.actor`/target `scope` 的 PEP 鉴权、权限路由过滤回注、入口审计以及同步/异步桥接。API 不得调用 LLM、Extractor、Classifier、IndexBuilder、Retriever 或 Store，也不得实现写入、去重、版本、生命周期、检索排序和后台任务编排。
20. **委托对象按职责分流**：数据面 add/search/list/get/update/delete/evolve 经 `MemoryCommandService` / `MemoryQueryService` 委托 `MemoryEngine`；治理操作经 `GovernanceService` 委托 `Governor`；`delete_space` 的 purge+delete 事务经 `SpaceLifecycleService`；任务状态和取消委托 `Scheduler`/`IngestJobController`；跨 scope 授权委托 `Authorizer` / `GrantStore`；策略读写委托 `PolicyManager`；space 普通 CRUD 委托 `SpaceManager`。这些是控制层 typed 端口或算子的直接委托，不属于 API 自行实现业务逻辑。
21. **Space 删除事务在 Control**：`delete_space` 鉴权后调用 `SpaceLifecycleService`（先 `MemoryEngine.purge_space`，再 `SpaceManager.delete`，并把 purge 条数累加进 `deleted_counts` 的 `memory` / `index` / `kv`）。purge 失败则不删 space；purge 成功而 metadata delete 失败时抛 `PartialFailureError`（`retry_action=delete_space`），不得报告完整成功。重试同一入口：purge 对空空间幂等，第二步再删元数据。API 只授权、调用该端口、使 membership 缓存失效并记录入口审计；不得在 API 内联 purge+delete 或实现索引删除/存储遍历。
22. **业务逻辑下沉可验证**：新增数据面语义时，API 侧只增加契约校验/参数装配/授权映射，具体行为必须在 `MemoryEngine` 或对应 Control/Construction/Retrieval 算子中实现。API 单测应使用 spy/mock 验证委托，Control 单测应覆盖真实行为，禁止只在 API 单测中覆盖业务分支。
23. **Access 只依赖本包**：`jiuwen_memory_entry/` 与 `jiuwen_memory_adapter/` 只 `import jiuwen_memory.api`，不得 import `jiuwen_memory.api.memory_api_impl` 或其他内核包。协议转换所需的 DTO、枚举、异常、`internal_context` / `new_request_context` / `build_dev_authenticator` / `Surface` / `Credentials`，以及日志脱敏辅助函数 `install_privacy_filter` / `metadata_for_log` / `redact_for_log` / `scope_for_log` 由本包重导出。这四个辅助函数只处理 Python logging 参数，不增加 HTTP DTO 字段，也不改变 `MemoryAPI` 方法签名或响应。公开装配是 `assemble` / `assemble_runtime` / `assemble_server_runtime`（`config=dict | Config | None`），Access composition root（`jiuwen_memory_entry/core/server.py`）不 import `jiuwen_memory.config`，不访问内核实现私有成员。`Kernel` / `build_kernel` / `LocalMemoryAPI` 不是公开导出；公开运行时与 `Server` 不暴露 `kv` / `storage` / `space`。

## 接口契约

`MemoryAPI` 定义在 `jiuwen_memory/api/memory_api.py`。下列各节按委托面划分；方法总览标明状态与详细介绍所在小节。文档明确**不对外暴露**的能力（`link`、独立 `MemoryAPI.expand`）不列入本契约。

### MemoryAPI 方法总览

| 分面 | 方法 | 状态 | 详细介绍 | 特性文档 |
|---|---|---|---|---|
| 数据面（委托 MemoryEngine） | `add` | 已实现 | [add / add_async](#add--add_async) | F01、F02、F04 |
| 数据面 | `add_async` | 已实现 | [add / add_async](#add--add_async) | F01、F02、F04 |
| 数据面 | `batch_add` | 已实现 | [batch_add / batch_add_async](#batch_add--batch_add_async) | F03、F04 |
| 数据面 | `batch_add_async` | 已实现 | [batch_add / batch_add_async](#batch_add--batch_add_async) | F03、F04 |
| 数据面 | `check_write` | 已实现 | [check_write](#check_write) | 无独立 F；实现见 F01 PEP |
| SDK 数据面 | `supports_video_ingest` / `prepare_video_ingest` | 已实现 | [本地视频准入](#本地视频准入) | F09 文档模式 |
| 数据面 | `submit_ingest` | 已实现 | [submit_ingest](#submit_ingest) | 无独立 F；实现见 F01 PEP / F05；`occurred_at` 见 F06 |
| 数据面 | `search` | 已实现；平铺参数包含可选层级查询与 query rewrite | [search](#search) | F01、F04、retrieval/F08；层级见 common/F08 |
| 数据面 | `list` | 已实现 | [list](#list) | F01、F04 |
| 数据面 | `get` | 已实现 | [get](#get) | 无独立 F；实现见 F01 |
| 数据面 | `update` | 已实现；`MemoryPatch.hierarchy` 尚未实现 | [update](#update) | F01、F04；层级见 F08 |
| 数据面 | `batch_update` | 已实现 | [batch_update](#batch_update) | F11 |
| 数据面 | `delete` | 已实现 | [delete](#delete) | 无独立 F；实现见 F01 |
| 数据面 | `evolve` | 原有四种演进与 dreaming | [evolve](#evolve) | F01、F02 |
| 数据面（文档模式，经 Engine 窄端口） | `read_md_file` | 已实现；仅文档模式可用 | [read_md_file](#read_md_file) | F08 |
| 数据面（文档模式） | `edit_md_file` | 已实现；仅文档模式可用 | [edit_md_file](#edit_md_file) | F08 |
| 任务面（委托 Scheduler / IngestJobController） | `job_status` | 已实现 | [job_status](#job_status) | F01 |
| 任务面 | `job_cancel` | 已实现 | [job_cancel](#job_cancel) | F01 |
| 治理面（委托 Governor） | `inspect` | 已实现 | [inspect](#inspect) | F01 |
| 治理面 | `trace` | 已实现 | [trace](#trace) | F01 |
| 治理面 | `audit` | 已实现 | [audit](#audit) | F01 |
| 授权面（委托 Authorizer / GrantStore） | `grant` | 已实现 | [grant](#grant) | F01 |
| 授权面 | `revoke` | 已实现 | [revoke](#revoke) | F01 |
| Space 管理面（委托 SpaceManager） | `create_space` | 已实现 | [create_space](#create_space) | F01 |
| Space 管理面 | `get_space` | 已实现 | [get_space](#get_space) | F01 |
| Space 管理面 | `list_spaces` | 已实现 | [list_spaces](#list_spaces) | F01 |
| Space 管理面 | `update_space` | 已实现 | [update_space](#update_space) | F01 |
| Space 管理面 | `archive_space` | 已实现 | [archive_space](#archive_space) | F01 |
| Space 管理面 | `delete_space` | 已实现；仅 `PURGE`。`FORGET`/`ARCHIVE`/`DOWNWEIGHT` 尚未实现 | [delete_space](#delete_space) | F01 |
| Space 管理面 | `export_space` | 已实现 | [export_space](#export_space) | F01 |
| Space 管理面 | `space_usage` | 已实现；`index_count` / `audit_count` 尚未实现 | [space_usage](#space_usage) | F01；缺口见 S03 |
| Space 管理面 | `get_space_policy` | 已实现 | [get_space_policy](#get_space_policy) | F01 |
| Space 管理面 | `set_space_policy` | 已实现 | [set_space_policy](#set_space_policy) | F01 |
| Space 管理面 | `list_space_members` | 已实现 | [list_space_members](#list_space_members) | F01 |
| Space 管理面 | `add_space_member` | 已实现 | [add_space_member](#add_space_member) | F01 |
| Space 管理面 | `remove_space_member` | 已实现 | [remove_space_member](#remove_space_member) | F01 |
| 运行时策略面（直达 PolicyManager） | `admin_get` | 已实现 | [admin_get](#admin_get) | F01 |
| 运行时策略面 | `admin_set` | 已实现 | [admin_set](#admin_set) | F01 |
| 运行时策略面 | `admin_all` | 已实现 | [admin_all](#admin_all) | F01 |
| 接续面（委托 ContinuityService） | `continuity_task` | 已实现 | [continuity_task](#continuity_task登记查询和关联任务) | construction/F10 |
| 接续面 | `continuity_relation` | 已实现；角色模式 | [continuity_relation](#continuity_relation登记复核或撤回关系) | construction/F10 |
| 接续面 | `continuity_recover` | 已实现 | [continuity_recover](#continuity_recover核对未知写入与修复投影) | construction/F10 |
| 治理面（接续来源） | `quarantine_source` | 已实现 | [quarantine_source / release_source](#quarantine_source--release_source来源隔离与解除) | construction/F10 |
| 治理面（接续来源） | `release_source` | 已实现 | [quarantine_source / release_source](#quarantine_source--release_source来源隔离与解除) | construction/F10 |

---

### 群体记忆带来的契约变更（F07）

契约细节、判定规则与决策取舍见 [F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md)。「已落地」指内核已实现；接入层改造另计。

| 入口 | 变更 | 兼容性 | 状态 |
|---|---|---|---|
| `add` / `add_async` | 签名不变。`scope` 仍必填、语义不变；`system_metadata["coords"]` 在参数袋里即表示落点交由判定 | 向后兼容，不带该键的调用方行为不变 | 已落地 |
| `batch_add` / `batch_add_async` | 签名不变，批级 `scope` 仍为缺省值（逐项自带即可整批省略）。批级参数袋带 `coords` 键即转入归属判定；坐标取自批级 `system_metadata["coords"]`，逐项携带即拒绝 | 向后兼容 | 已落地 |
| `search` | 签名不变。归属坐标经 `Context.extensions["coords"]` 传入，供收窄维谓词取值；`Context.extensions["spaces"]` 在参数袋里即转为跨空间检索 | 向后兼容，不带这两个键的调用方行为不变 | 已落地 |
| `RecallChannel` | 新增取值 `space`，标记跨空间检索里某个空间整体召回失败 | 向后兼容；穷举该枚举的调用方须容纳新取值 | 已落地 |
| `delete` | 入参 `DeleteSelector` 新增 `filters`，空选择器判据随之纳入该字段；实体删除后的跨空间清理由接入方经本入口逐个执行 | 向后兼容 | 已落地 |
| `list` | 不读归属坐标，但注入第一族系统谓词 | 行为变更：个体空间内不再返回不可见条目 | 已落地 |
| `list_spaces` | `cursor` 标记废弃；`limit` 由每页条数改为返回条数上限 | 签名不变，翻页语义变更 | 已落地 |
| `create_space` | 签名不变；入参 `SpaceSpec` 新增 `owner` 字段，供开通服务声明归属主体或显式不登记 | 向后兼容 | 已落地 |
| `job_status` / `job_cancel` | 本表归属判定改造未改签名；返回值 `JobInfo.mode` 的取值域由任务类名改为演进模式（`EvolveMode` 的值），无演进模式的任务仍回落类名。后续 #285 为 job_cancel 增加可选 scope，见任务面契约 | 行为变更，不受两个开关约束：鉴权点按该取值决定任务入口取哪个动作，类名区分不了遗忘与抽取 | 已落地 |

`scope` 语义不变：它就是落点，且保持必填。落点也可以不由调用方指定、改由归属判定选择，这时在写入侧参数袋里放 `system_metadata["coords"]` 请求判定，候选空间集合由归属坐标推出、落点由判定算子在集内选择。分流判据是该键在不在，与 `scope` 的取值形态无关——键由调用方主动放入，而 `space` 为空是调用方什么都没表达时的取值，拿它触发另一条路径等于由内核解读缺省状态。`space` 非空也不否决判定请求：上游网关按自己的租户或应用标识填 `space` 是常见形态，那个取值不是本系统的空间标识，以它否决则这类接入方无路可走（直写要求空间已登记，未登记即判权拒绝）。给了 `coords` 即交出落点决定权，`scope.space` 在这条路径上不参与落点计算，真实落点由返回的记忆单元携带。两处都没有声明时拒绝，替换的是同一形态下原本含义模糊的判权拒绝。走判定路径时入参 `scope` 的主体维与 `org` 仍须与身份一致，不静默丢弃。条目的可读范围由所在空间的权限决定，写入侧不设条目级的可见性声明入参（F07）。

**归属坐标经参数袋传入**。写入侧使用 `system_metadata["coords"]`，检索侧使用 `Context.extensions["coords"]`，取值为 `dict[str, str]`，由 API 在运行期解析，免于扩展接口形参或 `MetadataValueType`。启用路由时，API 在入口取出坐标，用于归属判定或生成判定表声明的收窄谓词。该参数是瞬态字段，检索侧取出后不随 options 透传给自定义检索模块。检索坐标不进入鉴权入参，可信身份仍来自 `security`。

无路由时，检索保留 `coords` 扩展字段供自定义检索模块使用。文档模式下，`search` 同时读取非空字符串 `coords.project`，生成 `system_metadata.project IN ["", project]`，与调用方的 `filters` 取交集。项目坐标缺失或为空时，仍可用显式 `filters` 选择项目。索引在没有项目谓词时只召回未指定项目的文档。

**跨空间检索不另设入口，同样经参数袋分流**：`Context.extensions["spaces"]` 键在即跨空间，取值 `list[str]`，空列表表示「调用方可读的全部空间」。跨空间不是新的检索算法，是在单空间召回之上套的一层编排（候选空间 → 逐空间判权 → 按上界分配取数 → 逐空间召回 → 轮转合并），两族谓词与召回复用同一份实现；拆成两个入口即同一件事有两处契约，接入方须先判断部署形态才知道该调哪个，且两处一旦分叉就出现「一个入口挂了谓词、另一个没挂」的绕过通道。判据同样取键的有无：取值判空则「查我能读的全部空间」这层意图只能靠缺省状态表达，与「没打算跨空间」不可区分。取值为 `None` 按非法拒绝，不当作空列表——网关把未填字段序列化成 `null` 时若按空列表处置，一次本意为单空间的检索会静默扩到全部可读空间。

跨空间形态下 `search` 有三处差异，其余与单空间路径一致：

| 项 | 差异 |
|---|---|
| `context.scope` | 只取 `org` 维定组织边界，空间维由候选集给出、传了不生效 |
| 无权的候选空间 | 逐个剔除并记入 `RetrievalResult.errors`（`ChannelError`，`channel=space`、`error_type=PermissionDeniedError`）；候选集非空而一个都读不到时抛 `PermissionDeniedError`，与单空间路径同一处置。候选集为空是合法空结果，不抛 |
| 时延 | 随候选空间数线性增长，上界由 `space.fanout_limit` 约束 |

未装配判定算子（未声明 `router` 配置命名空间）时，上述写入侧变更全部不可达：判定表为空、`coords` 键不产生落点、`space` 为空照旧直写，全链路行为与改造前一致。装配条件是分流判据的一半，缺它则未装配部署里以空 `space` 为落点的既有调用会撞上「写入落点未声明」。这是可灰度上线的前提。

---

### 数据面（委托 MemoryEngine）

#### add / add_async

**状态：已实现**

```python
def add(
    content: str,
    scope: Scope,
    source: Modality = Modality.TEXT,
    *,
    security: RequestSecurityContext,
    assets: list[str] | None = None,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
    event_time: datetime | None = None,
) -> list[MemoryUnit]: ...

async def add_async(...) -> list[MemoryUnit]: ...  # 签名同 add
```

同步写入：鉴权 WRITE→委托 Engine→阻塞至 hot path 完成。infer/procedural 触发时返回 `created_ids` 对应的派生单元（可空），否则返回原始单元。`add_async` 为异步写入：直通 Engine 协程，供事件循环形态使用。

`source` 必须位于当前装配 Normalizer 的 `modalities()` 能力集合内。动态请求不受支持时在规约和落盘前抛 `UnsupportedCapabilityError`；各接入形态在协议边界稳定映射该领域错误，HTTP 映射为 400。媒体 URI 只有在对应模态被 Normalizer 声明支持后才能进入规约逻辑，不支持的媒体不得将 URI 字符串作为 content 写入。

##### 可选 Entity Schema 装配

Entity Schema 抽取复用统一的 `assemble(config)` 装配入口。
`globals.schema_enabled` 默认 `false`；只有装配时显式设为 `true` 才注册 Schema
target。调用方还必须在配置中显式选择 Schema Extractor 和 Schema Evolver；
仅打开注册开关不改变默认 target 及写入语义。Schema 写入仍使用
`add(..., system_metadata={"infer": True})`。开关关闭但显式配置 Schema target 时装配失败。

##### infer 开关（add 的同步抽取语义）

`add` 的 `system_metadata["infer"]` 是调用级开关，控制写入时是否同步抽取派生记忆（对齐 mem0 `add(infer=True)`）：

- **真值判定**：`str(system_metadata.get("infer", "")).strip().lower() == "true"`——大小写/
  空白不敏感，字符串 `"true"` 和布尔值 `True` 均会触发；`"false"`/`False`/缺省/
  空值走默认路径。
- **`infer="true"`**：原始记忆落 `/messages/{id}` 真源但**不建索引**；hot path
  同步走 `Engine → Evolver.evolve(units, EXTRACT)`。`OrchestratingEvolver`
  以 `_dedup_batch` 完成判定与落盘，`DynamicEvolver` 走
  extract → consolidate（只判定）→ reflect → 落盘；**不提交** background EXTRACT。
  Engine 从 `EvolveResult.created_ids` 反查并返回派生单元，因此 ADD/SUPERSEDE
  返回新派生单元，只有 UPDATE/NOOP 时合法返回空列表。
- **缺省 / 非 `"true"`（infer=false）**：原文经 `classifier.classify` 打 tier+tags（纯 LLM 抽取 episodic/semantic/procedural + 1-3 个 tags）→ 落盘 `/memory/{id}` + 建索引。classifier 未注入时跳过（tier 保持 EPISODIC 默认，向后兼容）。返回原始单元列表。
- **evolver 缺失**：`infer="true"` 但装配未注入 `Evolver` 时 Engine 抛 `RuntimeError`——装配问题暴露而非静默降级。默认装配 `evolver: orchestrating` 总是注入。

##### procedural 开关（add 的过程记忆抽取）

`add` 的 `system_metadata["procedural"]` 是独立于 infer 的调用级开关（详见 F02 决策8）：

- **`procedural="true"`**：原文**不落 KV**；喂 `Evolver.evolve(units, EXTRACT)`。extractor 把本轮汇总成一条 PROCEDURAL 执行历史，再由 Evolver 落盘（`DynamicEvolver` 也走父类 procedural 路径，不判定）。
- procedural 与 infer 同传时按 procedural 语义：原文不落 `/messages/`、不收集
  context、不去重。语义是"把这轮做了什么记成一条可检索 how-to"。

##### 动态抽取与巩固 prompt

- metadata 的公共类型是 `dict[str, Any]`；动态 prompt 控制项的值按
  `str(value).strip()` 解释为 prompt key。调用方使用
  `system_metadata["_extract_prompt_<strategy>"] = prompt_key` 或 `system_metadata.update(...)`
  传值；不存在 `metadata.append()`。
- 在 `add` 的普通同步抽取路径中，`_extract_prompt_<strategy>` 随 `infer=true`
  进入 EXTRACT；procedural 或显式 `evolve(EXTRACT)` 同样进入 Evolver 的 EXTRACT
  路径，Extractor 本身不校验 infer。支持任意非空 strategy，每个策略调用一次；
  无动态 prompt 时回退旧 Extractor。
- `_consolidation_prompt_<strategy>` 为落盘前动态巩固 prompt 的 **key**（引用 yml `prompts` 段的命名 prompt）。运行时由 `PromptRegistry` 按 `phase=consolidate + key` 查真实文本。`DynamicEvolver` 消费；无 prompt 或输出不合法时回退规则判定（高相似且 `should_direct_noop` 为真则 NOOP，否则 ADD）。
- `_reflect_prompt_<strategy>` 为反思步 prompt 的 key（同上，`phase=reflect`）。reflect 默认 no-op，子类可覆盖 `_reflect_step`。
- LLM 输出格式由 prompt 自身约定，内核不追加固定 schema。

##### infer 上下文增强与 KV 前缀分离（增量）

- **infer=true 时 evolver 内部收集上下文**（evolve 接口不变）：`recent_originals`（最近 10 条 infer 原文，做指代消解/语境，不参与去重）+ `related_memories`（`dedup.recall` 召回 10 条相关记忆，做去重提示）。两类参考项只拼进 extractor prompt，不进提取来源；最终判定由所选 Evolver 完成：`OrchestratingEvolver` 走 `_dedup_batch`，`DynamicEvolver` 走 consolidate → reflect → 落盘。详见 F02 决策7。
- **KV key 前缀分离**：真源 key 按「是否建索引」带前缀——`/memory/{id}`（建索引记忆）、`/messages/{id}`（未建索引 infer 原文）。前缀常量与 helper 在 `common.type_def.memory`/`raw`。详见 F02 决策6。
- **engine.write infer=false 调 classify**：默认路径调 `classifier.classify` 给原文打 tier+tags（纯 LLM 抽取 episodic/semantic/procedural + tags）；infer=true 不经 classifier（extractor 产派生时自定）。详见 F02 决策9。
- **`list` 收窄并上收为 API 契约**：HTTP `/v1/list`、CLI `list` 和 MCP `memory_list`
  都通过共享契约调用 `MemoryAPI.list`。
  `KVStore.list` 只查询 `/memory/` 记忆并返回当前页与分页前总数。详见 F02 决策10与
  F01 的 list 决策。

> 接续写入：`add` 另有可选 keyword 参数 `continuity: ContinuityWrite | None = None`，给出时按可靠写入语义登记到任务，见[角色关系记忆入口 / 可靠写入](#add--batch_add-的-continuity-参数可靠写入)。

> 开关由来与"为何默认不同步、为何经 Evolver 而非独立 Extractor"的取舍见 [`docs/features/api/F02-write-infer-extract.md`](../features/api/F02-write-infer-extract.md)；add 路径流程见 [`S03-control.md`](S03-control.md)。

#### batch_add / batch_add_async

**状态：已实现**

```python
def batch_add(
    items: list[BatchWriteItem],
    scope: Scope | None = None,
    source: Modality = Modality.TEXT,
    *,
    security: RequestSecurityContext,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
    stream_id: str = "",
    continue_on_error: bool = True,
    event_time: datetime | None = None,
) -> BatchWriteResult: ...

async def batch_add_async(...) -> BatchWriteResult: ...  # 签名同 batch_add
```

同步桥接批量写入；逐项归一化、WRITE 鉴权、space 校验与审计，结果始终按输入索引对齐。`batch_add_async` 串行保序批量写入；默认归集单项错误，`continue_on_error=False` 时后续项为 `Skipped`。顶层 `system_metadata` / `user_metadata` 作批次默认值，单项可覆盖。设计取舍见 [`F03-batch-write-api.md`](../features/api/F03-batch-write-api.md)。

接续批量写入：`BatchWriteItem.continuity` 逐项声明可靠写入，用于外部 Agent 历史快速导入，分阶段处理与逐项回执见[角色关系记忆入口 / 可靠写入](#add--batch_add-的-continuity-参数可靠写入)。

#### check_write

**状态：已实现**

```python
def check_write(
    scope: Scope,
    security: RequestSecurityContext,
    *,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
) -> None: ...
```

Pre-flight WRITE 鉴权，不落盘。用于长耗时摄入任务入队前拒绝无权限请求，避免 DoS（队列被无权限请求占满）。镜像 `add` 的鉴权路径与 space 可写校验，但不调 `engine.write`。后台实际写入仍保留一次鉴权作防御层。

#### 本地视频准入

`supports_video_ingest() -> bool` 查询已装配的本地视频能力。可用时，显式
`pipeline=video` 必须路由到支持结构化视频输入的抽取链。

`prepare_video_ingest(uri, scope, *, security, project) -> str` 先按视频写入的 Scope
和项目鉴权，再使用已装配的本地导入策略检查 URI，返回规范化 URI。能力缺失时抛
`UnsupportedCapabilityError`。这一步只做准入，不提交任务、不写入记忆。
调用方可用规范化 URI 计算幂等键，再调用 `submit_ingest`。后台写入仍独立鉴权并验证文件。

#### submit_ingest

**状态：已实现**

```python
def submit_ingest(
    content: str,
    scope: Scope,
    source: Modality,
    *,
    security: RequestSecurityContext,
    payload_id: str,
    source_ref: str,
    assets: list[str] | None = None,
    tags: list[str] | None = None,
    system_metadata: dict[str, MetadataValueType] | None = None,
    user_metadata: dict[str, MetadataValueType] | None = None,
    occurred_at: datetime | None = None,
    event_time: datetime | None = None,
) -> IngestSubmission: ...
```

受鉴权的长耗时摄入入队。本层先做 WRITE 鉴权，再委托 Control 任务控制器；Access / `assemble_runtime()` 不得持有 `IngestJobController`。后台实际 `add` 仍再鉴权一次。

`occurred_at` 为消息发生时间，随任务原样交给后台 `add`，语义与 `add` 的同名参数一致（见 [F06](../features/api/F06-access-parameter-forwarding.md)）；该字段属于消息时间轴，不借 `metadata` 旁路传递。

`event_time` 为含时区的事件时间，在入队前校验（不含时区直接拒绝，不进队列），随任务原样交给后台 `add` 写入 `t_event`；不得将时间藏进 metadata，或把消息时间 `occurred_at` 当作 `t_event`。

#### search

**状态：已实现**（下列签名为当前代码）。层级过滤 / 展开 / rollup 为已设计增量，见本节末。
#### 薄封装职责

API 方法的实现顺序应保持为：

```text
transport input
  → 类型/形状校验与兼容归一化
  → 构造 PermissionContext / 内部请求对象
  → Authorizer.authorize + 审计
  → 委托 Control 算子
  → 结果适配与审计
```

API 可以复制可变参数、规范化过滤表达式、把 `Context` 拆为 `scope` 和 `RetrievalQuery`，也可以将授权路由值合并为系统过滤条件；这些属于边界适配和安全约束，不属于检索或写入算法。

API 不得在上述流程中自行执行以下逻辑：

- 记忆抽取、分类、去重、巩固和索引构建；
- 检索通道选择、召回、融合、重排和披露预算计算；
- MemoryUnit 版本、生命周期或 Storage CRUD；
- 后台任务拆分、并发策略、重试和调度实现。

管理面直接委托 `PolicyManager`、`SpaceManager` 等 Control 算子是允许的；授权管理使用同一 Authorizer 的具名 GrantStore，API 仍只承担鉴权、参数装配、调用顺序和结果适配，不实现多真源事务编排。

### search

```python
def search(
    query: str,
    context: Context,
    *,
    security: RequestSecurityContext,
    filters: FilterExpr | list[FilterClause] | dict | None = None,
    as_of: datetime | None = None,
    top_k: int = 10,
    disclosure: DisclosureLevel = DisclosureLevel.L0,
    with_trajectory: bool = False,
    hierarchy_kind: HierarchyKind | None = HierarchyKind.TIME,
    hierarchy_role: HierarchyRole | None = HierarchyRole.TIME_SPAN,
    span_start: datetime | None = None,
    span_end: datetime | None = None,
    expand_depth: int = 1,
    rollup: bool = True,
    task_id: str = "",
    anchor: MemoryRef | None = None,
    duty: str | list[str] | None = None,
    view_budget: ViewBudget | None = None,
) -> RetrievalResult | PersonalSearchResult: ...
```

混合检索：鉴权 READ→拆 Context→装配 RetrievalQuery→委托 Engine。`filters/as_of/top_k/disclosure/with_trajectory` 和 `context.extensions["max_tokens"]` 的既有处理不变。

树参数的默认值直接体现在签名中，不使用内部二次补默认值。开启树功能时，省略参数
即使用 TIME/time_span、一级展开和 MaxP 上卷；显式 `expand_depth=0` 或 `rollup=False`
原样生效，即使只传其中一项。

最近上下文经 `context.extensions["query_context"]` 传入，取值是 `list[str]` 或
`tuple[str, ...]`，顺序由调用方保证；key 不存在与空序列均表示无上下文。API 边界将其
校验并转为 `RetrievalQuery.query_context`，供支持上下文感知改写的 parser 消费；非法值
抛 `ValidationError`，且 key 移除后不进入 filters、scope、权限上下文或
`ParsedQuery.extensions`。

`hierarchy.enabled` 关闭或缺失时，在树语义校验前将 kind/role/span 清为 None、
expand_depth 清为 0、rollup 清为 False，显式树参数同样被忽略。普通过滤、as_of、
top_k、披露、Scope 与权限不变；普通召回可命中已有 Snapshot，并排除生成的 TIME 父。
基础请求/传输类型校验与鉴权仍执行，不修改调用方对象或已有树数据。

开启时，role、span、展开或上卷要求有效 kind；span 成对、按 UTC 比较、闭区间相交。
结构时间独立于事件时间和有效时间。查询全部角色须显式 `hierarchy_role=None`；
开启状态下主动使用普通查询须显式传 hierarchy_kind=None、hierarchy_role=None、expand_depth=0、rollup=False，
并省略 span。内部 SearchOptions 只是参数载体，其默认值不作为公开接口预设。

无有效 TimeSpan 时不回退到未挂树叶，也不在搜索时建树。TIME 父角色查询开启上卷及
展开时，保留父节点顺序，再按精排前的融合顺序补入已召回、归属和深度合法的后代；
不再遍历整棵子树或额外 embedding 排序，候选不足时允许少于 top_k。
跨空间先按现有规则全局选父，再按空间处理顺序依次取各空间内部保持融合原序的
后代候选，仅补入已选父节点的后代，共享预算。rollup=False 或未指定 TIME 父角色时
沿用结构 BFS，按遍历顺序返回。父子总数不超过 top_k，细则见 S04。
统一入口保留来源校验、READ 鉴权、委托资源主体、空间事实及路由谓词；跨空间按原权限
逐空间分发。请求快照不修改调用者输入，运行时插件保留身份。

HTTP/CLI 仅暴露原 search，MCP 仅暴露 memory_search；参数与 SDK 同义。

接续检索：`search` 另有可选 keyword 参数 `duty`、`task_id`、`anchor`、`view_budget`，三者（duty/task_id/anchor）均缺省时行为不变；给出任一即进入任务/职责视图路径并填充 `RetrievalResult` 的可选字段。首版不支持跨空间职责视图：`context.extensions` 带 `spaces` 键且同时给出 `duty` / `task_id` / `anchor` 时返回 `ValidationError`。见[角色关系记忆入口 / 职责检索](#search-的接续参数职责检索)。
历史进程内 handler 仅在显式提供树字段时按同一类型契约解码并转发，省略字段不补默认值。
不再提供公开 search_v2/evolve_v2 或对应 MCP 工具，不新增公开 expand。

#### list

**状态：已实现**

```python
def list(
    scope: Scope | None = None,
    *,
    security: RequestSecurityContext,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    extensions: dict[str, Any] | None = None,
    filters: FilterExpr | list[FilterClause] | dict | None = None,
) -> MemoryListResult | PersonalPage: ...
```

列出已建索引记忆：支持类型/FilterExpr 过滤、自定义参数透传和分页前精确总数；只返回 `/memory/` 真源记录。

`list` 的 `memory_types` 用于数据过滤，也参与权限路由：显式传一个或多个类型时，API 层为每个
类型分别构造 `PermissionContext(memory_type=<type>)` 并逐个执行 READ 鉴权；未传类型时先按
普通 `resource_type="memory_list"` 鉴权。两种路径随后都调用
`MemoryEngine.list_with_permission_contexts`，一次取得当前页、分页前总数及 MemoryUnit 真源
`memory_type/pipeline/tags/metadata` 权限上下文并逐条二次鉴权，避免未传过滤条件时落入
宽松 fallback，也避免权限上下文与内容来自两次分页读取。

`extensions` 为 `dict[str, Any]`，API 只复制外层字典并原样透传值；内核不得隐式调用
`str()`。权限上下文构建只解释 `Authorizer.routing_fields()` 及入口路由能力声明的路由键
（extensions 优先、filter 等值兜底，与 search 同一口径），其余扩展键对权限层不透明，
不进 `PermissionContext.metadata`；只有明确声明需要落盘或跨进程传输的扩展，才由对应
边界 adapter 使用显式 codec 序列化；未知 key 原样透传。
`filters` 与 search 共用 FilterExpr/旧 list/dict DSL 规范化语义，`memory_types` 与 filters
取 AND。`org/space/user/agent/session` 属于 Scope 隔离轴，不得出现在 filters。

接续来源过滤：启用接续时，不可用来源（配置 `enabled=false` 的连接 ∪ 项目内被隔离的连接）作为系统过滤谓词 `NOT system_metadata.continuity_connector IN {不可用连接}` 与用户 filters 取 AND 下推到 Engine，在分页与 `count` 之前生效；`list` 不得因页内含不可用来源条目而整页抛 `PermissionDeniedError`。`get` 单条命中仍返回 `PermissionDeniedError`。契约见 [S11「来源治理与列表过滤」](S11-cross-agent-continuity.md#来源治理与列表过滤)。

#### get

**状态：已实现**

```python
def get(
    unit_id: str,
    scope: Scope,
    *,
    security: RequestSecurityContext,
    as_of: datetime | None = None,
) -> MemoryUnit: ...
```

真源点读：鉴权 READ→委托 Engine。`as_of` 为空时返回该 id 对应的那一条；非空时沿 `supersedes` 版本链回溯，返回 valid 区间含 `as_of` 的那一版。不存在时抛 `NotFoundError`。
请求 ID 的预检和真源权限检查通过后，返回前还必须按实际选中 MemoryUnit 的同一快照
执行 READ 鉴权（Scope、作者和类型路由）。不能用当前版本权限代替历史版本权限，
也不能用预读权限放行同 ID 的后续内容变化。

#### update

**状态：已实现**（下列 `MemoryPatch` 字段为当前代码）。`hierarchy` 为已设计增量。

```python
def update(
    unit_id: str,
    scope: Scope,
    patch: MemoryPatch,
    *,
    security: RequestSecurityContext,
) -> MemoryUnit: ...
```

修正记忆：鉴权 UPDATE→委托 Engine。`MemoryPatch` 仅非 None 字段生效：`content` / `tier` / `tags`（整体替换）/ `system_metadata`（合并）/ `user_metadata`（合并）/ `t_valid` / `t_invalid` / `mode`。

Schema source 的正文更新还会同步协调同 scope 的 Schema property 与实体关联，保留
OVERWRITE/SUPERSEDE 的 ID、版本和有效期语义。接口签名与返回类型不变，`content=""`
表示替换为空正文；不修改原始对话。普通记忆、直接修改 property 和非正文更新保持原行为。
源记录继续使用 UPDATE 鉴权；派生记录的覆盖/版本修订、新增和删除分别核验 UPDATE、
WRITE 和 DELETE，并在首次业务写入前全部完成。配置失效、抽取不完整或匹配歧义报错；
提交中断返回 `PartialFailureError`，可能留下部分写入；再次操作前需检查受影响记录，
不承诺断点恢复、固定 ID 重试或原子回滚。
决策与边界见 [F08](../features/construction/F08-entity-schema-extension.md)。

**当前限制：普通记忆后补 Snapshot 暂不支持。** `update` 不重新执行摄入层的叶提示转换；
向 `system_metadata` 补写 `hierarchy_kind`、`hierarchy_role`、`hierarchy_span_start`、
`hierarchy_span_end` 不会更新真实 `MemoryUnit.hierarchy`，也不会把普通记忆变成可建树叶。
不要把这组 metadata 更新作为修复用法；结构索引保留键同样不能绕过 API 直接覆盖。

**状态：已设计、尚未实现**（`MemoryPatch.hierarchy`）

`MemoryPatch` 的既有非空字段为 `content/tier/tags/system_metadata/user_metadata/t_valid/t_invalid/mode`，目标增加：

```python
hierarchy: HierarchyPatch | None = None
```

`HierarchyPatch` 的精确类型由 S03 定义。它只允许修改指定 kind 的结构状态、span、
受控子边和稳定顺序；不得接受未校验的完整 `HierarchyRef`、裸 `parent_id` 或任意
`child_ids` 覆写。API 仅做形状校验，Engine 必须按 S03 验证同 org+space、无环、单父、
双向一致和稳定顺序后原子应用。

`SUPERSEDE` 若目标已挂树，Engine 必须把结构位置从旧 id 一致迁移到新版本 id，再把旧版本设为 `SUPERSEDED`；不得留下指向旧版本的活动结构边。`OVERWRITE` 保持 id，但层级 patch 仍须经过相同校验。

#### batch_update

**状态：已实现**（F11）

```python
def batch_update(
    selector: UpdateSelector,
    patches: list[MemoryPatch],
    *,
    security: RequestSecurityContext,
    limit: int | None = None,
    continue_on_error: bool = True,
) -> BatchUpdateResult: ...
```

批量更新记忆：鉴权 UPDATE 后由 API 层逐条编排（复用单条 `update` 的
schema prepare→逐项鉴权→commit 链路，引擎不提供整批入口）。`UpdateSelector` 各条件取
「与」，至少一项：`unit_ids` / `scope` / `tags` / `before` / `after` / `filters`。
空 selector 抛 `ValidationError`；候选只含 `ACTIVE` 状态单元（历史版本/冷数据不参与，
避免对已替代版本补丁错位）。

`patches` 长度决定模式：`len==1` 为统一 patch（所有候选应用同一个 patch）；
`len>1` 为逐条对应（必须给 `selector.unit_ids` 且 `len(unit_ids) == len(patches)`）。
逐条模式无 `unit_ids` 或数量不匹配抛 `ValidationError`；`unit_ids` 逐项对应补丁，
缺失 id 生成 `NotFoundError` 类型的 error outcome，不静默丢弃。

`limit` 覆盖 `selector.limit`：必须为正整数（`0`/负数抛 `ValidationError`，不静默忽略）。
统一 patch 模式做 post-match 截断（全量匹配后取前 N 条执行更新）；逐条模式只做下界承诺
（不小于 `len(patches)`，否则抛 `ValidationError`），不截断候选集。
`continue_on_error=True` 时单条失败不中断整批；`False` 时失败后后续标记 `Skipped`。

鉴权流程：先 trusted_context 检查（`Action.UPDATE`, `"batch_update"`），再通过
`engine.permission_contexts_for_update(selector)` 取候选 unit 权限上下文，逐条
UPDATE 鉴权；无候选时对 selector scope 做 selector 级 fallback 鉴权。每个 `MemoryPatch`
的 `system_metadata` / `user_metadata` 复用单条 `update` 的内核保留 key 与非标量校验。

设计取舍见 [`F11-batch-update-api.md`](../features/api/F11-batch-update-api.md)。

#### delete

**状态：已实现**

```python
def delete(selector: DeleteSelector, *, security: RequestSecurityContext) -> list[str]: ...
```

删除/归档/降权：鉴权 DELETE→委托 Engine。按选择器删除，条件取「与」，至少一项：`unit_ids` / `scope` / `tags` / `before` / `mode`。返回命中 id。未给 `selector.scope` 时鉴权退到根 scope。候选生命周期规则：非 `PURGE` 模式排除 `SUPERSEDED` 历史版本（流转非法）；`PURGE` 覆盖 `SUPERSEDED` 历史版本（合规硬删，新旧版本一并物理删除，不残留明文）。

#### evolve

**状态：已实现**。原 evolve 同时支持普通四种模式和显式 HIERARCHY 建树。

```python
def evolve(
    scope: Scope,
    mode: EvolveMode,
    channel: Channel = Channel.BACKGROUND,
    *,
    candidate: CandidateSource | dict | None = None,
    dreaming: bool | None = None,
    interval: int = 0,
    security: RequestSecurityContext,
    hierarchy_options: HierarchyComposeOptions | None = None,
    reuse_existing: bool = False,
) -> str | None: ...
```

触发演进：鉴权 WRITE→（三态分发）→委托 Engine 纯执行链→返回 job_id。普通模式 EXTRACT/ASSOCIATE/CONSOLIDATE/FORGET 继续使用原演进链路；HIERARCHY 使用显式建树链路。调用成功返回 job id，不表示任务已经完成。索引维护不在此（随数据面自动跟进）。

**dreaming 三态**（F04，PEP 边界——三态编排、持续授权、fan-out 逐桶裁决都在 API 层 `DreamingCoordinator`，`api/memory_api_impl/dreaming.py`；Control 层纯执行见 S03 `evolve` 行）：`dreaming=None` 立即跑（入口鉴权 + fan-out 逐桶裁决后提交一次性 EvolveJob，返回 job_id；拒绝桶仅以 `count:N` 脱敏摘要回显）；`dreaming=True` 注册定时（`interval>0` 必填，scheduler 须 `supports_recurring`；candidate 注册前 canonical 校验；注册表记录 `created_by`，驱动 Job 每 tick 复验授权与 leader 租约，被拒或失锁即停摆）；`dreaming=False` 幂等注销（未命中返回 `None`，不退化立即跑）。注销先把注册状态持久化为 `CANCEL_REQUESTED`，再在协调器状态锁外等待 Scheduler，成功后删除；取消失败保留该状态，Driver 在 tick 开头和提交演进前按 `state + generation + job_id` 自停，重启恢复只收口而不复活。注册/注销/恢复由本实例生命周期锁串行，并只允许 leader 修改共享声明；请求落到非 leader 时抛 `RuntimeError`，不会删掉另一实例正在执行的声明。周期实例启动即竞选 leader，空注册表也持锁；恢复结果只在当前 leader 任期内幂等缓存，失锁或主动放锁后必须重扫；恢复每条记录在提交和换绑后均复验租约，中途失锁立即取消本地驱动并中止恢复。空间进入持久不可写状态时，周期任务以 `space_not_writable` 停摆并移除声明。

持续授权使用注册时的可信上下文，不以 `created_by` 中的 actor 五元组代替认证。
服务端私有注册表保留原角色、认证期限、凭据复核能力、委托与可信环境快照；恢复与
每 tick 生成受控请求上下文，并重新校验凭据和委托真源，不延长认证期限。旧记录只
有 actor、或安全快照损坏时不可恢复为有效请求；详见 S10 不变量 35。

#### 显式建树参数

新增参数追加在原参数之后：`hierarchy_options=None` 指定树类型、父层链、父驻留 Scope
和构建时间区间；`reuse_existing=False` 控制复用与重建。公开入口不接收 EvolveTaskOptions
参数袋；该类型继续用于内部类型化提交。原 candidate/dreaming/interval 语义不变。

开启树功能后，HIERARCHY 要求有界时间、TIME/Snapshot 和合法父层前缀，scope 必须
精确等于 hierarchy_options.tree_home_scope。普通模式不得携带建树选项。
HIERARCHY 只做单次任务，不接受 candidate/dreaming/interval；入口保留可信上下文、
WRITE+UPDATE 授权及空间可写检查。启用逐记忆权限路由时不支持实际建树；父 metadata
不能覆盖内核保留键或路由标签。构建参数详见 S05。

关闭或缺失 hierarchy.enabled 时，忽略 hierarchy_options/reuse_existing；HIERARCHY
返回成功跳过的 job_id，即使未提供选项或时间不完整也不做树语义校验，不读候选、不调用
建树模型。基础 mode/channel/Scope、普通参数组合和权限检查仍执行；HTTP/CLI/MCP
仍执行原有报文与字段类型解码。普通模式继续原有演进。
#### read_md_file

**状态：已实现**（F08，仅文档模式可用——非文档模式 `InMemoryEngine` 抛 `ValidationError`）。

```python
def read_md_file(
    context: Context,
    md_filename: str,
    *,
    security: RequestSecurityContext,
    line_range: tuple[int, int] | None = None,
) -> list[MemoryUnit]: ...
```

按 `md_filename`（basename：`USER.md` / `MEMORY.md` / `YYYY-MM-DD.md`）+ 行范围读 md 文件原文，返回命中的 MemoryUnit 列表。入参形态与 `search` 一致（`context: Context`）：`context.scope` 携带归属五维，`context.extensions["coords"]["project"]` 决定从哪个 project 子目录读（project 解析在 Engine 层 `project_from_context`，API 层不拆 Context）。`line_range` 为 `(start, end)` 1-based 闭区间，`None` 表示整文件，越界自动 clamp。

返回值与 `add` / `search` 同构为 `list[MemoryUnit]`——按块算 content_hash 在影子索引反查 unit_id 后取完整 MemoryUnit；`MdFileContent` 的格式转换放 provider 层。鉴权 `READ` 单段（与 `get` / `list` 同口径，F08 D5），target 是折算后的反查 scope（见下段）。返回前剥瞬态键（`coords` 等是路径解析依据，不是条目属性）。

路由装配下（`_routing_enabled()`），API 层把反查 scope 折算为空间级 `Scope(org, space)`（`md_filename → memory_class` 的 space_template 经 `principal.kernel_coords(coords, identity)` 渲染），与写入侧 `RouteDecision` 落盘形态（org+space、主体维空）对齐，避免 5 段等值反查因 space/user 两段错位静默返空。**折算出的空间级 scope 同时是鉴权（READ）与审计（`target_scope`）的 target**——与写入侧「先定落点、对落盘空间判权 + 审计」对称：调用方须对该空间有读权，空 `scope.space` 不再绕过空间轴判权，审计记录实际访问的空间而非 raw scope。路由未启用、坐标缺失或模板渲染不出时退回 raw scope，判权/审计/反查三者同轴（F08 D5.1）。

#### edit_md_file

**状态：已实现**（F08，仅文档模式可用——非文档模式 `InMemoryEngine` 抛 `ValidationError`）。

```python
def edit_md_file(
    context: Context,
    md_filename: str,
    old_text: str,
    new_text: str,
    *,
    security: RequestSecurityContext,
) -> list[MemoryUnit]: ...
```

按 `md_filename` + `old_text` 在文件内做唯一替换，返回受影响的新 MemoryUnit 列表。语义对齐 agent-core `edit_memory`：`old_text` 在文件内必须唯一（`count > 1` 抛 `ValidationError`），不存在抛 `NotFoundError`，`replace(old, new, 1)` 精确替换一处后原子写回。`old_text` 任意文本（块内片段 / 跨多块均可），不强制对齐块边界。

与 `update` 的差别：`update` 按 `unit_id` 做 SUPERSEDE/OVERWRITE（替换整个 unit.content）；本方法按文件内 `old_text` 做局部文本替换，保留文件其他部分。edit 走「继承式 SUPERSEDE」——新 unit 继承旧 unit 的元数据 + 新 id + `supersedes` 指向旧 id + 旧版标记 SUPERSEDED（F08 D4）。返回新 unit（旧版已失效，返回它会让调用方误用失效 id）。

鉴权 `WRITE` 对 `context.scope` 单段（与 `add` 同口径——edit 本质是写入新记忆，语义对齐 `add` 而非 `update`，F08 D5 / R6）。写入前置状态校验经 `_ensure_space_writable`（与 `add` 同，装配空间级判定时由 `ENTRY_RULES` 登记 `edit_md_file` 触发 `_ensure_space_state_allows`）。返回前剥瞬态键。

---

reuse_existing=True：纯新合格叶首次构建；完整现有树直接复用；完整旧树与新叶混合、
缺失请求父层或损坏结构拒绝隐式重建。False：备齐区间相交旧根的完整子树后重建，
可以同时包含新叶和旧叶。缺时且未挂树的叶仅跳过，保持原输入。

默认 False 不是去重开关：重复提交相同范围，即使没有新叶，也会重建相交旧树。
成功后新父使用新 ID，子边切换到新父，旧父归档并移除检索索引；权威叶内容保持。
请求区间是候选选择边界，不是严格修改边界。例如旧 Event 覆盖 09:00–10:00，
请求重建 09:10–09:20，仍须收齐该旧根的全部后代再重建，包括区间外子叶。
不相交的旧树不变；不得用更短父层链重建含更高父层的旧树。

后台增量不使用 reuse_existing：已有父边的节点不重复参与该层构建，但未挂上层的
TimeSpan/Scene 可继续生成上层。即使通过其他维护途径补齐了真实 Snapshot，历史节点
也可能因迟到或超出回看范围被跳过；不能承诺下一轮自动挂树。公开补 Snapshot 仍不支持。

### 任务面

建树与普通演进共用 job_status / job_cancel。返回 ID 不代表已完成；调用者读取任务
终态，SUCCEEDED 且 complete=true 才表示构建完成或合法复用/跳过。默认 in_process
在提交内完成；async_timer 在后台执行，需通过 job_status 等待终态。

#### job_status

**状态：已实现**

```python
def job_status(
    job_id: str,
    *,
    security: RequestSecurityContext,
    scope: Scope | None = None,
) -> JobInfo: ...
```

查询 Scheduler 或长耗时 Ingest 任务；Ingest 任务要求 target scope，API 对任务真实 Scope 执行 READ 鉴权与审计。先取任务（含其 scope），再据 `security.auth.actor` 对该 scope 判权。`JobInfo`：`id` / `channel` / `mode` / `scope` / `status`（`PENDING`/`RUNNING`/`SUCCEEDED`/`FAILED`/`CANCELLED`）/ `detail`。

#### job_cancel

**状态：已实现**

```python
def job_cancel(
    job_id: str, *, security: RequestSecurityContext, scope: Scope | None = None
) -> None: ...
```

取消演进或长耗时摄入任务，终态重复取消幂等。演进任务委托 Scheduler，原调用兼容；
`ing_` 摄入任务必须提供 target `scope`，缺失抛 ValidationError，任务不存在或 Scope
不匹配抛 NotFoundError。可信上下文校验先于任务查询，随后按任务真实 Scope 鉴权 WRITE、
记录审计并执行完整性预检，通过后才委托摄入控制器取消。

摄入排队任务取消后不执行任务体；运行中取消为协作式，当前已进入的同步分类、演进或写入
允许收尾。内置视频规约在 ASR 分片、文本/VLM 请求及应用层重试之间也有检查点，
取消后不再发送下一请求，轮询与退避等待可被唤醒；已发出的请求和 SDK 内部重试不能
保证立即终止。第三方插件未响应检查点时仍允许整个调用收尾。返回 None 表示请求已接受，不代表执行
已结束：`job_status` 在收尾期间仍返回 running，`detail.cancel_requested="true"`，
结束后返回 cancelled；真实业务错误仍返回 failed。取消不回滚写入，已知产物保留在
`detail.unit_ids`；进入写入后的取消或失败以 `partial_write="true"` 和
`retry_requires_reconciliation="true"` 保守标记可能残留，不以空 ID 推断零写入。
调用方应先核对真源并处理残留，再显式使用新 payload_id 重提；原 payload_id 自动重试
被 ConflictError 拒绝。确认未进入写入的取消任务可以复用原 payload_id 重新提交。

状态查询为纯读取，重启遗留任务只返回恢复后的 failed 投影，不在 READ/WRITE 鉴权或
审计完整性预检前持久化恢复。运行中遗留任务因无法证明零写入，即使未请求取消也要求
先核对结果，再使用新 payload_id 重提，这是对原有重启后直接重试行为的保守收紧。
后台终态写库失败时，当前进程仍报告实际执行终态及已知 ID，并在任务 error 与日志中
报告持久化失败；不能将业务已结束的任务留在 running，也不能吞掉真实业务错误。

周期任务注销或本地失锁停摆时，取消信号覆盖其已排队驱动和已关联的派生演进任务；后续演进桶不再开始。已进入的同步 Evolver 调用可以结束，取消不回滚已完成写入，也不提供存储 fencing 保证。同步调用返回后才观察到取消时，任务状态为 `CANCELLED`，但 `detail` 必须保留已返回的变更 ID，并回显 `partial_write`、`completed_groups`、`retry_requires_reconciliation`；调用方须先查询真源核对结果，不得把 CANCELLED 当作“零写入”直接全量重试。

---

### 治理面（委托 Governor）

本面不增加新的尚未实现对外方法。不变量 16 要求三类遍历分离：树下钻由 search 的 expand_depth 参数完成，`trace` 只沿 `provenance`，`get(as_of)` 只沿 `supersedes`；不另设 `MemoryAPI.expand`。

#### inspect

**状态：已实现**

```python
def inspect(
    unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
) -> list[MemoryUnit]: ...
```

检视完整内容与治理字段（含已失效版本）。`scope` 为目标范围，本层据 `security.auth.actor` 与目标范围鉴权 READ。

#### trace

**状态：已实现**

```python
def trace(
    unit_id: str, scope: Scope, *, security: RequestSecurityContext
) -> list[MemoryUnit]: ...
```

沿 provenance 追溯演进来源链（不沿层级树、不沿 `supersedes`）。

#### audit

**状态：已实现**

```python
def audit(
    filters: dict[str, str],
    *,
    security: RequestSecurityContext,
    limit: int = 100,
) -> list[AuditEvent]: ...
```

按条件（actor/action/layer/时间段等）检索审计留痕。无具体 target scope 时以根 `Scope()` 为鉴权闸门。
当前 audit 使用独立 `Action.READ_AUDIT`，不与普通 READ 授权互认。

#### 审计完整性面（接口先行，PR3）

| 方法 | 签名 | 语义 |
|------|------|------|
| `verify_audit` | `(*, security, after_sequence=0, page_size=1000, max_samples=20, anchor_policy="if_configured") -> AuditVerificationResult` | 审计链完整性验证：按 `VERIFY_AUDIT` 执行管理面根 scope 闸门，与使用 `READ_AUDIT` 的既有 `audit` 是两个独立入口；provider 与专用 `audit_verify_guard` 成对注入，全量验证占独立并发槽，耗尽抛 `RateLimitedError`；此时授权事件保持 `decision=allow`，沿用 `workload_guard=exhausted` 明细区分容量准入失败与鉴权拒绝。guard 准入后必须先记录验证尝试再调用 provider，使 provider 抛出的完整性异常仍可追溯发起者与发生时间。输入只允许服务端验证参数，不接受调用方传入 expected digest / key / proof。`after_sequence=N` 必须从同一稳定快照读取并验证第 N 条 checkpoint proof，扫描固定截至快照链头；并发追加留到下一次，checkpoint/序号缺口/截断导致 `incomplete`，不得从 genesis 盲接。`page_size` / `max_samples` 截到服务端可信 `globals.audit_verify_max_page_size` / `globals.audit_verify_max_samples`，装配只接受真正的整数（拒绝 `bool` 和字符串），非法类型、范围或超过硬上限统一抛 `ValidationError`；PEP 再保证返回 samples 不超过有效上限。`truncated` 只表示样本列表截断，`high_water_mark` 是本次连续成功验证到的最高 sequence。未装配 provider 时诚实返回 `unsupported`，不降级成 clean。HTTP 在认证中间件产出可信 `security` 后以同名 `/v1/verify_audit` 暴露并递归序列化原返回值；CLI 以同名 `verify_audit` 命令暴露；MCP 以 `memory_verify_audit` 工具暴露同一冻结契约（见 S10 审计完整性契约） |

---

### 授权面（委托 Authorizer / GrantStore）

本面对外方法与下列契约一致，无额外尚未实现的 MemoryAPI。MemoryAPI 是唯一 PEP，
判定委托 Authorizer，授权状态读写使用其具名 GrantStore；旧 PermissionManager
不进入生产授权路径。

#### grant

**状态：已实现**

```python
def grant(grant: Grant, *, security: RequestSecurityContext) -> Grant: ...
```

新增跨 scope 授权。`security.auth.actor` 须有权再授权 `grant.grantor` 范围，按 SHARE
鉴权并执行授予上界检查。正常路径返回带服务端生成 `grant_id` 的安全域 Grant，授权
写入与实际 Authorizer 判定同源的 GrantStore；不得双写旧 PermissionManager。

#### revoke

**状态：已实现**

```python
def revoke(grant: Grant, *, security: RequestSecurityContext) -> None: ...
```

回收授权（幂等）。按 `grant_id` 读取真实授权目标并执行 REVOKE_SHARE 鉴权，撤销须
原子绑定已鉴权的真实 grantor；不得信任请求中自述的 grantor。缺失 ID 拒绝，未知 ID
幂等无副作用，撤销状态单调，重复调用不改变原撤销时间。公共签名保持不变。

---

### Space 管理面（委托 SpaceManager）

#### create_space

**状态：已实现**

```python
def create_space(spec: SpaceSpec, *, security: RequestSecurityContext) -> SpaceInfo: ...
```

创建全局唯一 space id；以 `Scope(org=spec.org)` 做 WRITE 鉴权，成功后记录目标 space 审计。`SpaceSpec`：`org` / `space` / `display_name` / `principal_path` / `policy` / `metadata`。

#### get_space

**状态：已实现**

```python
def get_space(org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo: ...
```

读取单个 space 的基础信息与策略。

#### list_spaces

**状态：已实现**

```python
def list_spaces(
    org: str,
    *,
    security: RequestSecurityContext,
    status: SpaceStatus | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> list[SpaceInfo]: ...
```

列出 org 下 spaces；以 `Scope(org=org)` 做 READ 鉴权。

#### update_space

**状态：已实现**

```python
def update_space(
    org: str, space: str, patch: SpacePatch, *, security: RequestSecurityContext
) -> SpaceInfo: ...
```

修改 display name、status、principal_path、policy 或 metadata。`SpacePatch` 仅非 None 生效。

#### archive_space

**状态：已实现**

```python
def archive_space(org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo: ...
```

归档 space；已归档 space 的 `add/update/evolve` 会被拒绝。读取与导出保留。

#### delete_space

**状态：已实现**（当前只支持 `PURGE`）

```python
def delete_space(
    org: str,
    space: str,
    *,
    security: RequestSecurityContext,
    mode: DeleteMode = DeleteMode.PURGE,
) -> SpaceDeleteResult: ...
```

删除 space；当前只支持 PURGE。API 鉴权后调用 `SpaceLifecycleService`：先经 Engine 清该 `org + space` 下全部 user/agent/session 子 Scope 的 `/memory/` 真源与索引，再委托 SpaceManager 清 KV/messages/metadata，并汇总 `deleted_counts`。API 随后使 membership 缓存失效并记录入口审计。

**状态：已设计、尚未实现**（`mode` 取 `FORGET` / `ARCHIVE` / `DOWNWEIGHT`）。方法本身已落地，其它 `DeleteMode` 尚未实现。

#### export_space

**状态：已实现**

```python
def export_space(
    org: str,
    space: str,
    *,
    security: RequestSecurityContext,
    include_audit: bool = True,
) -> str: ...
```

创建导出记录并返回 export id。

#### space_usage

**状态：已实现**（当前统计侧重 memory/message 数量与 KV bytes）

```python
def space_usage(org: str, space: str, *, security: RequestSecurityContext) -> SpaceUsage: ...
```

查询 space 级 memory/message/KV bytes 用量。

**状态：已设计、尚未实现**（返回字段未补齐）：`SpaceUsage.index_count` / `audit_count` 待专用后端补齐（见 S03）。

#### get_space_policy

**状态：已实现**

```python
def get_space_policy(org: str, space: str, *, security: RequestSecurityContext) -> SpacePolicy: ...
```

读取 space policy。

#### set_space_policy

**状态：已实现**

```python
def set_space_policy(
    org: str, space: str, policy: SpacePolicy, *, security: RequestSecurityContext
) -> SpacePolicy: ...
```

替换 space policy，并同步主体路径。

#### list_space_members

**状态：已实现**

```python
def list_space_members(
    org: str, space: str, *, security: RequestSecurityContext
) -> list[SpaceMember]: ...
```

列出 space 成员与角色。

#### add_space_member

**状态：已实现**

```python
def add_space_member(
    org: str, space: str, member: SpaceMember, *, security: RequestSecurityContext
) -> None: ...
```

添加或更新成员角色。

#### remove_space_member

**状态：已实现**

```python
def remove_space_member(
    org: str, space: str, member: Scope, *, security: RequestSecurityContext
) -> None: ...
```

移除成员。

---

### 运行时策略面（直达 PolicyManager，不经 Engine）

`hierarchy.enabled` 已作为运行时策略键用于 Snapshot 接入及显式建树；自定义策略表必须预先声明该键才能 admin_set。该开关也控制树查询及接口默认预设；hierarchy.auto_derive 默认关闭，开启后须经 Runtime 显式注册周期任务；ensure 尚未交付。admin_set 不扩展为任意配置树，详见 S08。

#### admin_get

**状态：已实现**

```python
def admin_get(key: str, *, security: RequestSecurityContext) -> str: ...
```

读策略（直达 PolicyManager）。管理面以根 `Scope()` 的 ADMINISTER_SYSTEM 鉴权，需 ROOT。

#### admin_set

**状态：已实现**

```python
def admin_set(key: str, value: str, *, security: RequestSecurityContext) -> None: ...
```

写策略（直达 PolicyManager）；根 `Scope()` 的 ADMINISTER_SYSTEM 鉴权，需 ROOT；
键未知或不可变抛 `PolicyError`。

#### admin_all

**状态：已实现**

```python
def admin_all(*, security: RequestSecurityContext) -> dict[str, str]: ...
```

列全部策略（直达 PolicyManager）；根 `Scope()` 的 ADMINISTER_SYSTEM 鉴权，需 ROOT。

## 数据结构

### Scope —— 目标范围 vs 调用方身份（`common/type_def`）

`org > space > user/agent > session` 五维归属，同时支撑隔离与共享。各维默认 `""`。API 里目标范围与调用方身份是**两个不同语义**的位置（勿混淆）：

- **目标范围（target）**：操作作用于「谁的」记忆——`scope` 参数（或 `Context.scope` / `DeleteSelector.scope`），类型 `Scope`。
- **调用方身份（actor）**：「谁」在发起调用——不再由调用方以 `Scope` 直传，而是取自必填 `security: RequestSecurityContext` 的 `security.auth.actor`；除 `check_write` 保留第二位置参数兼容外，其余方法要求具名传入。

`space` 是全局唯一的逻辑隔离标识，`org` 表示其归属组织并继续参与权限边界；不同 org
不能创建相同的非空 space id。空 `space` 只表示兼容旧数据/单租户默认域，不参与 Space
资源注册，也不表示跨全部 space。`space` 为 keyword-only 字段，旧四段位置参数顺序仍是
`org/user/agent/session`。

### RequestSecurityContext（安全输入，`common/security/types.py`）

一次请求的可信身份绑定，是 `MemoryAPI` 所有公开方法的唯一安全输入（keyword-only 必填）。

| 字段 | 类型 | 语义 |
|------|------|------|
| `auth` | AuthContext | 认证产出；`auth.actor` 即调用方身份 Scope |
| `request_id` | str | 服务端生成，进审计与日志关联，不参与授权判定，调用方不可指定 |
| `peer` | str | 传输层对端地址（不采信 `X-Forwarded-For` 一类自述 header） |
| `surface` | Surface | 接入形态（HTTP/MCP/CLI/SDK/INTERNAL），由适配层写入 |
| `started_at` | datetime | 服务端时钟（带时区），授权时效判定的 now 由它派生 |
| `attributes` | Mapping[str, str] | 只读；只由系统组件写入，业务 payload 不得注入 |

便捷属性 `security.actor` 等价于 `security.auth.actor`。构造收在
`common.security.request_context`（见不变量 14），实例携带来源绑定，
`has_valid_origin()` 可校验其是否出自受控入口。

### Context（`common/type_def/context.py`）

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `scope` | Scope | 空 Scope | 检索目标范围（多租户隔离） |
| `extensions` | dict[str, Any] | `{}` | 调用方自定义透传配置，值原样透传；约定 key `"max_tokens"` 表示自适应披露 token 预算，由 API 边界解析为 `RetrievalQuery.max_tokens` |

> `extensions["max_tokens"]` 是 API 边界解释的约定 key，解析后从透传 extensions 中移除；无此 key 或空串时披露阶段使用默认策略。

### MemoryUnit / Segment（读取类方法返回，`common/type_def/memory.py`）

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | str | Scope 内唯一 id（每条记忆/每个版本各一） |
| `scope` | Scope | 归属（unit 级单一 owner，隔离/存储命名空间键） |
| `tier` | MemoryTier | 认知角色分类 |
| `segments` | list[Segment] | 多段内容投影（每段 = content + assets + source） |
| `source_ref` | str | 来源引用（RawPayload id / 会话 id，可溯源） |
| `temporal` | Temporal | 时间：`t_event`/`t_ingest`/`t_valid`/`t_invalid`/`t_message`；`occurred_at` 进入消息时间 `t_message` |
| `provenance` | list[str] | 演进血缘（多→一合成）：由哪些 unit 提取/升华/合并而来 |
| `supersedes` | str | 版本链（一→一更替）：本版本取代的上一版 id；空表示首版 |
| `tags` | list[str] | 标签（检索前置过滤用） |
| `system_metadata` | dict[str, MetadataValueType] | 系统扩展字段（infer / procedural / pipeline / prompt key 等） |
| `user_metadata` | dict[str, MetadataValueType] | 用户业务元数据；保留 JSON 标量原生类型，也可使用字符串数组 |
| `lifecycle` | LifecycleState | 生命周期状态 |
| `hierarchy` | HierarchyRef | 已实现：树结构，空值兼容普通记忆；细节见 S07 |

`Segment`：`content`（可治理文本/结构投影，索引与检索对象）、`assets`（本段原模态资产引用）、`source`（本段来源 Modality）。便捷只读折叠属性：`unit.content`（各段换行连接）、`unit.assets`（各段扁平合并）、`unit.source`（首段模态）——返回新对象，勿就地 `append`。

`ContentLayers` 的 L0/L1 是同一 unit 的披露层，不是多模态粒度，也不是 TIME 父子结构。

`MetadataValueType = str | int | float | bool | None | list[str]`。写入和更新边界对 metadata 执行以下约束：

- 接受 JSON 标量（string / number / boolean / null）和字符串数组；
- 不把业务值统一转换为字符串，真源与索引保留原生类型；
- 拒绝 `unit_id`、`tier`、`lifecycle`、`tags`、时间字段等系统保留 key；
- 不接受嵌套 object 或非字符串数组。

### MemoryPatch / UpdateMode（update，`control/types.py`）

`MemoryPatch` 仅**非 None** 字段生效：`content` / `tier` / `tags`（整体替换）/
`system_metadata`（合并）/ `user_metadata`（合并）/ `t_valid` / `t_invalid` / `mode`。目标增加
`hierarchy: HierarchyPatch | None = None`（**状态：已设计、尚未实现**）；其精确类型及结构事务校验见 S03。

`UpdateMode`：`SUPERSEDE`（默认、非破坏式，新 id 新版本 + 旧版标 superseded）/
`OVERWRITE`（原地覆写、同 id，旧内容仅留审计）。

### DeleteSelector / DeleteMode（delete，`control/types.py`）

`DeleteSelector` 各条件取「与」，至少给一项：`unit_ids` / `scope`（鉴权依据）/ `tags`（命中任一）/ `before`（`t_event` 早于此）/ `mode`。候选生命周期规则：非 `PURGE` 模式排除 `SUPERSEDED` 历史版本（FORGET/ARCHIVE 流转非法，`SUPERSEDED→ARCHIVED` 禁止）；`PURGE` 物理删除覆盖 `SUPERSEDED` 历史版本——合规硬删不残留明文，新旧版本一并删除。

`DeleteMode`：`FORGET`（标记遗忘，可恢复，默认）/ `ARCHIVE`（归档转冷）/ `DOWNWEIGHT`（保持 active 仅降权）/ `PURGE`（物理删真源与索引，合规硬删，不可恢复，含 `SUPERSEDED` 历史版本）。

### UpdateSelector / BatchUpdateOutcome / BatchUpdateResult（batch_update，`control/types.py`）

`UpdateSelector` 与 `DeleteSelector` 结构相同（`unit_ids` / `scope` / `tags` / `before` / `after` / `filters`），但**不携带 `mode`**——批量更新的版本语义由各 `MemoryPatch.mode` 逐条决定。新增 `after` 字段（`DeleteSelector` 没有），支持「只更新某时间段之后的消息」。`limit` 必须为正整数（`0`/负数抛 `ValidationError`）：统一 patch 模式做 post-match 截断（全量匹配后取前 N 条执行更新），逐条模式只做下界承诺（不小于 `len(patches)`），不截断候选集。各条件取「与」，至少给出一项；候选只含 `ACTIVE` 状态单元。

`BatchUpdateOutcome` 包含候选索引、`unit_id`、成功时的 `updated_unit` 或失败时的 `error` / `error_type`。`BatchUpdateResult.outcomes` 与候选顺序对齐。

### FilterExpr / FilterClause / FilterGroup（search 前置过滤，`common/type_def`）

`FilterExpr = FilterClause | FilterGroup`。单条 `FilterClause` 表示字段比较；
`FilterGroup` 用 `AND` / `OR` / `NOT` 递归组合子表达式。旧
`list[FilterClause]` 兼容为隐式 `AND`，dict DSL 可在 API / SDK 边界写成：

```python
{
    "AND": [
        {"user_metadata.project": {"in": ["alpha", "beta"]}},
        {"user_metadata.priority": {"gte": 8}},
        {"NOT": {"user_metadata.archived": True}},
    ]
}
```

`FilterOp`：`EQ`/`NE`/`IN`/`NOT_IN`/`GT`/`GTE`/`LT`/`LTE`/`CONTAINS`。
scope 不走 filters。metadata 比较严格保留类型：number、string、boolean 不互相转换；
`int` / 有限 `float` 属于同一数值类别。当前不维护中央 metadata schema，同一业务 key
的写入和查询类型应由调用方保持一致。

### MemoryListResult（list 返回，`control/types.py`）

- `items: list[MemoryUnit]`：当前分页结果。
- `count: int`：同一 Scope 和过滤条件下的分页前精确总数，不受 offset/limit 影响。

### BatchWriteItem / BatchWriteOutcome / BatchWriteResult（batch_add，`control/types.py`）

- `BatchWriteItem` 表达单项内容与可选 scope/source/tags/`system_metadata`/`user_metadata`/occurred_at/`event_time` 覆盖；`event_time` 未给时沿用批级缺省值，不含时区的单项按 `ValidationError` 归集，不影响其他项；`stream_id`、`sequence`、`idempotency_key` 首版仅用于调度和回显，不写入真源。
- `BatchWriteOutcome` 包含输入索引、归一化 item、该项产生的 `units` 与可归集的 `error` / `error_type`；成功且 units 为空仍是成功。Engine 的非领域异常也必须归集为 `InternalError`，不能使整批 HTTP 请求退化为 500。
- `BatchWriteResult.outcomes` 与输入严格一一对应。相同 `(Scope, stream_id)` 的非空 `sequence` 不得重复；接口不自动重排。
- 内部字段：`BatchWriteItem.source_ref` 只由接续批量写入在内部设置（`source_locator` → `MemoryUnit.source_ref`）；公开调用方传非空值即 `ValidationError`。
- 接续：`BatchWriteItem.continuity: ContinuityWrite | None`；`BatchWriteOutcome` 新增 `operation_id: str = ""` 与 `continuity_status: str = ""`（committed / partial / unknown / rejected，未带 continuity 的项为空）。

### DisclosureLevel / RetrievalResult（search 返回，`retrieval/types.py`）

`DisclosureLevel`：`L0`（摘要）/ `L1`（片段）/ `L2`（全文）/ `ADAPTIVE`（按 `max_tokens` 预算自动选层级）。

`RetrievalResult`：
- `items: list[RetrievedItem]` —— 每项 `unit_id` / `score`（融合/重排后最终分）/ `content`（按层级加载）/ `level`（实际披露层级）。
- 接续可选字段（仅给出 duty/task_id/anchor 时填充，否则为 null）：`paths: list[RelationPath] | None`、`gaps: list[Gap] | None`、`view_meta: ViewMeta | None`；`RetrievedItem.view: ViewAnnotation | None`。字段定义见 S11。
- `trajectory: list[TrajectoryStep]` —— 仅 `with_trajectory=True` 返回。每步 `stage`（parse/recall/fuse/recheck/rerank/resolve/threshold/disclose）/ `channel`（召回通道，非召回步为 None）/ `candidate_count` / `cost_ms` / `detail`。

### Channel / EvolveMode / JobInfo（evolve / 任务，`control/types.py`）

- `Channel`：`HOT`（在线低时延）/ `BACKGROUND`（离线异步，默认）。
- `EvolveMode`：`EXTRACT`（抽取低抽象事实）/ `ASSOCIATE`（关联分析）/ `CONSOLIDATE`（升华画像）/ `FORGET`（遗忘/清理）。`HIERARCHY`（显式建树/区间替换，经 evolve(mode=HIERARCHY)）。
- `JobInfo`（`job_status` 返回）：`id` / `channel` / `mode` / `scope` / `status`（`JobStatus`：PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED）/ `detail`。

### Modality / MemoryTier / LifecycleState（`common/type_def/memory.py`）

- `Modality`：`TEXT`/`IMAGE`/`AUDIO`/`VIDEO`/`CODE`/`DOCUMENT`（多模态来源在接入层规约为 content 文本投影 + assets 原模态资产）。
- `MemoryTier`：`WORKING`/`CORE`/`EPISODIC`/`SEMANTIC`/`PROCEDURAL`/`ARCHIVAL`。
- `LifecycleState`：`ACTIVE`（默认可召回）/ `SUPERSEDED`（被新版取代）/ `ARCHIVED`（归档转冷，不默认召回）/ `FORGOTTEN`（遗忘，无继任者）。

### Grant / Action（授权，`common/security/types.py`）

`Grant`（frozen）：`grantor`（授权方 scope）/ `grantee`（被授权方 scope）/
`actions: frozenset[Action]` / `expires_at`（None 为长期）/ `grant_id`（授权的稳定标识，
构造时默认为空，由服务端生成）/ `revoked`；`is_active(*, now)` 判定时效。
为兼容既有 `jiuwen_memory.api.Grant` 调用方，构造器继续接受不含 `grant_id` 的旧参数形状，
并在构造边界把 `list[Action]` 等动作迭代归一为 `frozenset[Action]`；非 `Action` 成员立即
抛 `TypeError`。`Action`：`READ`/`WRITE`/`UPDATE`/`DELETE`/`SHARE`/
`REVOKE_SHARE`/`MANAGE_PRINCIPAL`/`MANAGE_SPACE`/`MANAGE_POLICY`/`READ_AUDIT`/
`VERIFY_AUDIT`/`MIGRATE_MEMORY`/`ADMINISTER_SYSTEM`。

`control/types.py` 为兼容既有内部与仓外导入路径，再导出同一个安全域 `Grant`/`Action`
对象，不再维护第二套字段或枚举；API 将服务端生成 ID 的 `Grant` 写入与 Authorizer
同源的 GrantStore，不会裁掉 `grant_id` / `revoked`。唯一 PDP `Authorizer` 执行完整
角色闸门；普通 Grant 不得越过管理面角色闸门，旧 PermissionManager 不参与生产判定。

### Space 数据结构（`control/types.py`）

- `PrincipalPath`：`USER_AGENT` / `AGENT_USER`，决定 space 内 owner-cover 字段顺序。
- `SpaceStatus`：`ACTIVE` / `FROZEN` / `ARCHIVED` / `DELETING` / `DELETED`。
- `SpacePolicy`：`require_space` / `principal_path` / `storage_isolation_strategy` / `retention` / `quotas` / `index_profiles` / `pipeline_profiles`。
- `SpaceSpec`：`org` / `space` / `display_name` / `principal_path` / `policy` / `metadata` / `owner`。
- `SpaceInfo`：`org` / `space` / `display_name` / `status` / `principal_path` / `policy` / `metadata` / `created_at` / `archived_at` / `owners`。
- `SpacePatch`：`display_name` / `status` / `principal_path` / `policy` / `metadata`。
- `SpaceMember`：`scope` / `role` / `content_role` / `governance_role` / `created_at` / `expires_at`；旧 `role` 只用于存量读取兼容，实际判权使用内容轴与治理轴。
- `SpaceUsage`：`org` / `space` / `memory_count` / `message_count` / `index_count` / `storage_bytes` / `audit_count`。当前实现填充 memory/message 数量与 KV bytes；`index_count` / `audit_count` **状态：已设计、尚未实现**。
- `SpaceDeleteResult`：`org` / `space` / `deleted_counts` / `status` / `audit_event_id`。

`SpaceFacts`（frozen）承载 `org` / `space` / `info` / `members`，作为一次调用内的空间事实快照。
`SpaceSpec.owner` 是归属主体而非认证 actor；身份仍只来自可信上下文。见
[F07-collective-memory-design.md](../features/control/F07-collective-memory-design.md)「空间数据结构变更」。

### AuditEvent（audit 返回，`common/type_def/audit.py`）

`id` / `actor`（操作者 Scope）/ `target`（目标 Scope）/ `action` / `target_id` / `layer`（产生事件的层）/ `occurred_at` / `detail`。`detail` 常见约定包括 `permission_check`、`permission_reason`、`job_id`、`before_unit_id` / `after_unit_id`、`before_unit_ids` / `after_unit_ids`；其中 `before_unit_*` / `after_unit_*` 仅表示记忆单元 id，不用于调度任务 id。审计查询支持 `actor_*` 与 `target_*` scope 字段过滤。

`search/get/inspect/trace` 使用 READ，`add/evolve` 使用 WRITE，`update` 使用 UPDATE，`delete` 使用 DELETE，`grant` 使用 SHARE、`revoke` 使用 REVOKE_SHARE。未限定 target scope 的 delete 和全局管理操作退到根 scope 闸门。

## 错误语义

| 异常 | 触发场景 |
|------|----------|
| `PermissionDeniedError` | 鉴权不通过（`security.auth.actor` 对 target scope 无相应 Action 权限） |
| `NotFoundError` | `get`、`update` 目标在已鉴权 scope 内不可见 |
| `ValidationError` | 入参非法（如 `search` 的 `top_k <= 0`；`add`/`batch_add` 的 `content` 非 `str`、空串或纯空白；目标层级的 span、深度、预算、options 或 patch 形状非法） |
| `UnsupportedCapabilityError` | 请求的 `source` 模态不在当前 Normalizer 声明的能力集合内；在规约、落盘和建索引前失败 |
| `PolicyError` | `admin_set` 的键未知或为不可变配置；树开关关闭时忽略树参数，不因此抛 PolicyError |
| `ConflictError` | 写入冲突（如 id 重复）或目标结构前置条件与当前状态冲突 |
| `WriteIncompleteError` | `PartialFailureError` 子类，HTTP 409。带 `continuity` 的 `add` 回执停在 unknown / partial；携带 `operation_id`、`status`、`refs`、`retry_action` |
| `BackendError` / `HealthCheckError` | 后端故障 / 健康探测失败 |

## 鉴权流程

```
接入层 → authenticated(...) / internal_context(...) → security: RequestSecurityContext
调用方 → MemoryAPI.method(scope=target, security=security)
  → actor = security.auth.actor
  → resource = ResourceDescriptor(scope=scope, action=<对应动作>, ...)
  → Authorizer.authorize(auth=security.auth, resource=resource, environment=environment)
    # list/get/update/delete/inspect/trace 的已有资源上下文来自真源和已鉴权 target scope
    → 通过 → 委托 Engine/Governor/PolicyManager（仅传 scope，不传 security）
    → 拒绝 → 抛 PermissionDeniedError
  → 落审计事件（含 actor + action + target_id + 时间）
```

判定使用服务端构造的 `AuthorizationEnvironment`，旧 PermissionManager 不参与生产判定。
当前能力见 [F04 安全能力总纲](../features/common/F04-security-interfaces-and-encryption.md)，
公开能力扩展与资源所有权见 [S10 安全规约](S10-security.md)。assemble 返回的 API
可通过独立 CloseableSecurityCapability 关闭其 MemoryRuntime；冻结的 MemoryAPI
抽象方法集合不变。多个运行时包装共享资源时，在装配期显式共享安全生命周期根。
Runtime 的 `close(wait=False)` 封闭准入并由单个收尾线程完成后台等待和资源释放；后续
`close(wait=True)` 等待同一次清理尝试，并报告其异常，重复等待不重复执行清理。
停止文件监听的同步屏障仍可等待已进入数据面的 watcher 工作；摄入和调度任务及其
状态写入结束后，才释放 dreaming 锁、回收自有 loop，最后关闭 shadow 与安全依赖。
共享 SecurityRuntime 直接关闭也执行同一后台停止屏障，不 join 可能等待资源根锁的
收尾线程。未确认使用者停止时保留依赖和 leader 锁；多个安全清理失败汇总报告。

### SDK 单次装配与认证

```python
runtime = assemble_runtime(config=config)
try:
    security = runtime.authenticate(Credentials(api_key=token))
    result = runtime.api.list(target_scope, security=security)
finally:
    runtime.close()
```

`authenticate(credentials: Credentials) -> RequestSecurityContext` 使用同次装配的认证器，
返回 `Surface.SDK` 上下文，业务方法仍显式传 security。缺安全配置、已关闭分别抛
ValidationError，认证失败抛 AuthenticationError，并发预算耗尽抛 RateLimitedError。
HTTP/MCP 的 peer、限流和协议解析仍由各入口负责。独立安全构建 helper 每次创建独立
上下文，不与已经装配的 SDK 隐式共享；共用 Runtime 须由公开 Server 装配入口显式注入。

`revoke(grant: Grant, *, security)` 是按 ID 撤销的兼容入口。其余字段不参与定位、
归属或鉴权；客户端应保留 `grant(...)` 返回对象，或使用
`Grant.for_revocation(grant_id)`。旧的无 ID 条件撤销应迁移为保存服务端 ID；缺失 ID
继续拒绝，不恢复条件匹配。归属从服务端真源读取，并原子绑定检查后撤销。

### 公开 Server 装配入口

`assemble_server_runtime(policies=None, config=None, *, security_runtime=AUTO_SECURITY_RUNTIME)
-> ServerRuntime` 从同一公开 API 包导出；Access 只传普通配置 mapping，不解析内核私有成员。
`MemoryRuntime` 提供 `api`、`authenticate(credentials)`、`start()`、异步
`start_background()` 和 `close(*, wait=True)`。异步宿主绑定其持续运行的事件循环；同步
宿主显式启动自有 loop，两者不暴露 Store 或任务控制器。默认无 watcher 时启动为 no-op，
关闭后不得重启；启动故障必须传回调用方。`ServerRuntime` 在该协议上增量提供只读
`security_runtime` 与 `audit`，转发同一启动/关闭路径。既有 `assemble`、`assemble_runtime`
和冻结的 `MemoryAPI` 签名不变；自定义 Runtime 需实现公开认证与生命周期协议，
并适配下述周期注册端口。

装配器在返回前完成 PDP 同源校验、显式 PDP 回绑、部署管理能力/空间路由约束复核和
认证器实际凭据真源的 Registry 替换。默认哨兵选择配置 Runtime；显式 `None` 不选择
入口认证，不隐式创建 DEV。显式安全组合若提供 PDP/认证器，必须按对应公开契约完成
绑定。历史部分组合只适配已有入口能力，不替代完整生产 SecurityRuntime 契约。
`audit` 与 API 业务审计同源；未装配为 None。句柄不暴露 raw Store、任务控制器等端口。

成功返回的句柄统一管理内核和已选择安全组合的生命周期；共享资源按现有所有者协议
去重关闭。绑定失败（包括取消）清理本次内核、保留主错误，不接管调用方注入对象。
Access 只调用公开句柄的 close，不承担二次绑定或检查 API 实现私有字段。

## 实现注册机制

```
jiuwen_memory/api/memory_api_impl/
    __init__.py             # 重导出实现类
    local_memory_api.py     # LocalMemoryAPI：PEP + 委托
    assembly.py             # assemble / assemble_runtime / assemble_server_runtime / _build_kernel
```

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | add 路径中 Engine 内部调用 Ingestor |
| S03-control | 数据面委托 MemoryEngine，治理/授权/调度/space 面委托对应算子 |
| S04-retrieval | search 路径中 Engine 委托 Retriever |
| S05-construction | 目标 `HierarchyComposeOptions` / `EvolveMode.HIERARCHY` 的字段契约 |
| S08-config | 六类动态配置经 ConfigSource；不经本层业务入参写入 |
| F07-collective-memory | 本层是空间治理的鉴权点：入口到轴与动作的映射、空间事实一次读取、检索两族谓词的生成与注入均落在本层。多空间读写编排按「是否读 `security`」拆开——判权与谓词生成留本层，写入候选集的计算与跨空间的召回扇出落控制层 `control/collective/` |
| architecture.md §6 | 已实现 MemoryAPI 清单 |


### Runtime 自动建树注册

```python
async def start_background_jobs(
    scope: Scope, *, security: RequestSecurityContext,
) -> BackgroundJobStartResult: ...
```

这是 `MemoryRuntime` / `ServerRuntime` 的宿主启动接口，不增加 MemoryAPI、HTTP、CLI 或 MCP
方法，具体 API 对象也不暴露同名公开方法。Runtime 经装配期绑定的内部回调完成 PEP 和提交。
`scope` 是树驻留范围（`HierarchyComposeOptions.tree_home_scope`，简称 home）：
org/space 精确匹配，非空 user/agent/session 限定输入范围，空维度允许覆盖其子范围。
新父驻留完整 home，叶保留原 Scope；同一个 home 可包含多棵树，不是树 ID 或文件目录。
不会自动发现 home 或扩大到其他主体。注册前校验可信上下文、home 的
WRITE/UPDATE 权限和空间可写状态；当前不支持按 metadata 路由的逐候选授权。每次执行还通过
API 注入的 `BackgroundJobAdmission(scope) -> None` 准入回调复验原注册上下文的可信来源、
到期、在线凭据状态、当前 WRITE/UPDATE 授权和空间状态；刷新空间事实，不能沿用注册时
的许可。回调绑定 home，只允许该范围内目标，异常或读取失败按拒绝处理。
控制层仅持有并调用准入契约，不接收 security/actor、不自行判权。

`BackgroundJobStartResult` 包含 `job_ids: list[str]`、`skipped: bool`、`reason: str`。
树总开关或自动开关关闭/缺失时返回 skipped、空 job_ids 和 `hierarchy_disabled`；未绑定
TIME profile 时 reason 为 `hierarchy_profile_missing`。开启却未配置周期 Scheduler，或
缺少运行依赖、调度配置非法时明确报错，不降级为一次性任务。

```python
runtime = assemble_runtime(config=config)  # 配置示例见 examples/config_template.yml
# 当前身份有权访问的范围；空 session 表示涵盖这个 user/agent 下的各个 session。
home = Scope(org="org1", space="space1", user="user1", agent="agent1", session="")
result = await runtime.start_background_jobs(home, security=security)
if not result.skipped:
    info = runtime.api.job_status(result.job_ids[0], security=security)
    # 停止此 home 的任务：runtime.api.job_cancel(result.job_ids[0], security=security)
```

注册只等待任务登记，不等待首次构建；首轮在调度间隔到达后执行。同一 Runtime、同一
完整 home 的活跃注册复用定时任务 ID；取消后重新注册创建新任务，故障保护状态仍共享。
AsyncTimerScheduler 自有循环独立于调用方循环，允许用 `asyncio.run` 完成注册；宿主进程
须持续运行。结束时调用 `runtime.close()`，停止周期及排队任务并等待在飞构建收尾。
关闭后拒绝再次注册；watchdog 的 `start` / `start_background` 生命周期不变。

父定时任务状态不等于某轮执行结果：通过 detail 的 `last_run_id`、`last_run_status`、
`last_run_detail`（JSON 字符串）查看最近一次结果，也可用 last_run_id 查询该轮任务。
诊断计数、跳过语义与故障保护见 S03；注册仅存于本进程，重启后由宿主重新注册。


## search 的检索 pipeline 选择扩展键

`context.extensions["retrieval_pipeline"]` 接受非空字符串或 None；名称精确匹配，不做 trim
或大小写转换。缺省/None 保留原 profile/default 路由。API 复制参数袋后消费此键，转换为
`RetrievalQuery.pipeline`，不修改调用方 Context，也不继续透传给 ParsedQuery 或自定义召回插件。
SDK、HTTP、MCP、CLI 通过已有 extensions 通道传入，无需新增 search 参数。

请求上下文可信校验之后、单空间/跨空间分流之前，经 query application port 调用 Engine 的
公共校验方法；未知名称抛 ValidationError，附公开可选名单。即使跨空间目标为空也必须拒绝
未知名称，不把调用参数错误转换成空间 ChannelError。API 不访问 Engine 或 Retriever 私有配置。
选择只影响检索算法，不替代 scope/授权谓词校验；优先级与装配约束见 S03/S04。

## 角色关系记忆入口

> **状态：已实现（2026-11-05 设计修订）**。`role_*` 已并入 `add`/`batch_add`/`search` 并改名为 `continuity_*`、`quarantine_source`、`release_source`；`role_record`、`role_view`、`role_publish`、`role_mark_source_cutover` 已删除。剩余差距见 [F10 12.2](../features/construction/F10-role-memory.md#122-实现差距)。

本节各 DTO 的字段级定义、内部回执与关系头结构见 [S11 数据结构](S11-cross-agent-continuity.md#数据结构)，含义与用法见 [F10 第 5 节](../features/construction/F10-role-memory.md#5-数据结构)。

接续能力不再设独立的写入与视图方法：

| 能力 | 入口 |
|---|---|
| 可靠写入、历史导入 | `add(..., continuity=ContinuityWrite)`；`batch_add` 的 `BatchWriteItem.continuity` |
| 任务背景 / 职责视图 | `search(..., duty=, task_id=, anchor=, view_budget=)` |
| 任务登记与绑定 | `continuity_task(scope, request: TaskRequest, *, security) -> dict` |
| 关系登记、复核、撤回（角色模式） | `continuity_relation(scope, relation: RelationRecord, expected_revision: int, *, security) -> dict` |
| 未知写入核对与投影修复 | `continuity_recover(scope, recovery: ContinuityRecovery, *, security) -> dict` |
| 来源隔离与解除（治理面） | `quarantine_source(scope, connector, reason, *, security) -> dict`；`release_source(scope, connector, *, security) -> bool` |

HTTP `/v1/<verb>` 与 CLI 由 MemoryAPI 契约反射；MCP 由 `memory_add` / `memory_search` 携带接续参数，另注册 `memory_continuity_task`、`memory_continuity_recover` 与（角色模式）`memory_continuity_relation`（沿用 `memory_<方法名>`；JiuwenSwarm 适配器为 `agent_memory_*`）；`quarantine_source` / `release_source` 只在管理面暴露，不进入 Agent 工具清单。不再提供跨空间发布（以现有 `grant` ＋ `continuity_task(attach)` 挂引用代替）与双写切换。

仅显式配置 `continuity`（且 `mode` 为 `continuity` 或 `role`）才启用；未启用时带接续参数的 `add`/`batch_add`/`search` 与上述专用入口返回 `UnsupportedCapabilityError`。接续模式下 `continuity_relation`、`continuity_recover(kind=relation)` 返回 `UnsupportedCapabilityError`，`search` 带非 general duty 时降级（见下）。scope 必须是 org+space 的项目空间。API 执行项目权限、任务负责人、可信连接身份及关系修订 UPDATE 权校验；引用按真实 Scope 点读，不因 duty、任务绑定或图路径扩权。关系真源存于 Registry KV，不出现在普通 get/list/search 结果中。

### 共同参数、返回状态与传输

| 参数 | 含义与限制 |
|---|---|
| `scope: Scope` | Registry/事件/关系所属项目，必须给出 `org`、`space`，`user/agent/session` 为空；不是调用者身份 |
| `security: RequestSecurityContext` | SDK 必填 keyword-only 可信上下文；HTTP/MCP 由认证边界注入，不放入业务 JSON。同步 SDK 方法须在无运行事件循环的线程调用 |
| `MemoryRef` | `scope` 是原记忆完整五维 Scope，`unit_id` 是实际 MemoryUnit ID，`content_digest` 是 64 位小写 SHA-256；从已保存单元取得，不用消息 ID/任意摘要代替。摘要算法见 S11 |

专用入口中 `release_source` 返回布尔值，其余返回 `dict`。`committed` 表示该操作所要求的真源/回执已保存，不表示任务完成；关系写入还要求投影完成。`partial` 表示已有可恢复进度；`unknown` 表示写入确认不足，不能换新 ID 再写。可选关联发现的 `association_status` 与事件的 `status` 分开。

共通异常：`ValidationError` 为字段/模型/项目形状错误；`PermissionDeniedError` 为认证授权或依赖不可读；`NotFoundError` 为控制记录不存在；`ConflictError` 为修订过期、幂等键冲突、引用变化等；`WriteIncompleteError`（409）为可靠写入停在 unknown/partial；`UnsupportedCapabilityError` 为未装配接续能力或当前档位不支持。实际存储故障也可能抛出，不能把异常视为未写入证明。HTTP 状态映射沿用本规约「HTTP / CLI 与协议适配契约」。

### add / batch_add 的 continuity 参数（可靠写入）

`add(content, scope, source, *, security, ..., event_time=None, continuity: ContinuityWrite | None = None) -> list[MemoryUnit]`；`batch_add` 逐项 `BatchWriteItem.continuity`。

| ContinuityWrite 字段 | 默认值 | 含义 |
|---|---|---|
| `task_id` / `binding_id` / `event_id` | 必填 | 全局任务、有效绑定、产品侧稳定事件 ID；幂等键 = 连接、绑定、事件 ID 的摘要（`operation_id`） |
| `kind` | `"unclassified"` | observation / statement / judgment / result / experience / correction / unclassified |
| `classification_confidence` | `"unknown"` | low / high / unknown；low 强制降级为 unclassified，high 不等于事实确认 |
| `evidence_refs` | `[]` | 支撑事件的原记忆引用；写入及重试时核验 |
| `claimed_author` | `None` | 原文声称作者，不覆盖服务端认证归属 |
| `context_id` | `None` | 产品会话或上下文 ID |
| `source_locator` | `None` | 原文定位信息，写入 `MemoryUnit.source_ref` |
| `schema_version` | `"continuity-write/1"` | 固定版本；未知版本拒绝 |

参数映射：正文用 `content`；原事件时间用 `event_time`（t_event），`occurred_at` 仍是消息时间；职责背景用 `tags`（`duty:<id>`）；其余字段由内部写路径写入 `continuity_*` 系统元数据，公开调用方不得直接设置该前缀。

返回：committed 时返回正文单元；同键同内容重放返回已保存单元，不重复写正文；同键异内容 `ConflictError`；回执停在 unknown / partial 时抛 `WriteIncompleteError`，携带 `operation_id`、`status`、`refs`、`retry_action`（partial → `retry_original_write`；unknown → `recover_then_retry`，先调 `continuity_recover` 再重试）。服务端核对绑定连接与当前认证主体相同；`claimed_author` 不能替代此检查。

`batch_add`：批级与逐项 scope 须为同一项目空间，不同任务可同批；`idempotency_key` 与 `continuity` 同时给出时须一致。处理分五阶段（批量 unknown 回执 → 批量写正文 → 批量转 partial → 每任务一次背景追加 → 批量转 committed），派生与关系发现排入后续阶段。逐项结果在 `BatchWriteOutcome.operation_id` / `continuity_status` 上表达，不抛 `WriteIncompleteError`；单项失败按 `continue_on_error` 归集。

### search 的接续参数（职责检索）

`search(query, context, *, security, ..., duty: str | list[str] | None = None, task_id: str | None = None, anchor: MemoryRef | None = None, view_budget: ViewBudget | None = None) -> RetrievalResult`。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `duty` | `None` | general / diagnosis / testing / execution 或已配置职责名，或 1–4 个的列表；选择边类型、方向、优先级，不授予权限；视图路径下缺省解析为 general |
| `task_id` | `None` | 读取任务登记的共同背景作为起点，不要求交接确认 |
| `anchor` | `None` | 已知 MemoryRef 起点，优先于语义候选 |
| `view_budget` | ViewBudget 默认值 | max_depth=2、max_nodes=30、max_edges=60、max_paths=60、timeout_ms=1000；正整数，上限依次为 4/200/500/500/10000，服务端职责策略可进一步收紧 |

`duty`、`task_id`、`anchor` 均缺省时与改动前完全一致。任一给出时：`query`、`anchor`、`task_id` 至少一项非空（仅给 task_id 或 anchor 时 `query` 可为空串）；`context.extensions` 带 `spaces` 键时返回 `ValidationError`（首版不支持跨空间职责视图）；结果按 background → anchor → relation → semantic_candidate 排序，`top_k` 截断时追加非阻断 `budget_exhausted` gap；`RetrievalResult.paths` / `gaps` / `view_meta` 与 `RetrievedItem.view` 填充，schema 见 S11 `role-view/2`。权限不足的起点或路径以不泄露源身份的缺口表达；项目级权限不足直接拒绝。接续模式下不展开关系：返回普通任务背景与语义结果，`gaps` 追加非阻断 `relation_expansion_disabled`，`view_meta.mode="continuity"`。

### continuity_task：登记、查询和关联任务

签名：`continuity_task(scope, request: TaskRequest, *, security) -> dict`。

| TaskRequest 字段 | 默认值 | 含义 |
|---|---|---|
| `operation` | 必填 | create / get / find / bind / unbind / attach / close |
| `request_id` | `""` | 变更必填稳定幂等键，同键重试必须保持请求内容；get/find 无需填写 |
| `task_id` | `""` | create 由服务端生成，其他定点操作使用返回的全局任务 ID；find 通过本地上下文查找 |
| `expected_revision` | `0` | create 使用 0；其他变更使用最近读取的任务 revision。查询忽略此值 |
| `connector_id` / `context_id` | `""` | bind/find 使用：可信连接配置标识和该产品的本地上下文标识；不根据标题自动关联 |
| `context_source` | `"native"` | native 表示原生上下文 ID；adapter 表示桥接层生成的稳定标识 |
| `binding_id` | `""` | unbind 指定此前返回的绑定 ID |
| `background` | `[]` | 需要登记的 MemoryRef 列表；通常用于 attach，也可随其他变更提交；登记不复制正文、不授予读权限 |
| `required` | `true` | 本次 background 是否属于必需证据；不可读必需证据会影响接续就绪判断 |
| `reason` | `""` | 解绑原因，随撤销主体保存 |

变更返回 `{task_id, revision, replayed, task}`；`task` 含任务状态、绑定及可见 background。get 直接返回任务快照，包含 `background_partial`，不返回私有回执；find 返回 `{tasks: [{task_id, revision}]}`，不证明调用者拥有其中所有来源内容。控制记录独立保存在 Registry，不是 MemoryUnit。读取需要项目 READ，变更需要 WRITE，修改既有任务还限创建负责人。失活绑定/关闭任务不能继续写入事件；修订冲突后先 get，再决定是否以新请求提交。

### continuity_relation：登记、复核或撤回关系

签名：`continuity_relation(scope, relation: RelationRecord, expected_revision: int, *, security) -> dict`。角色模式；接续模式返回 `UnsupportedCapabilityError`。

| 参数/字段 | 默认值 | 含义 |
|---|---|---|
| `expected_revision` | 方法必填 | 首次 0；更新填写当前关系修订。要求 `relation.revision = expected_revision + 1` |
| `relation_id` | 必填 | 稳定逻辑 ID，不使用保留前缀 `continuity:`；同修订重试内容必须一致 |
| `source` / `target` | 必填 | 两端 MemoryRef；方向按关系语义定义，不随查询方向变化；不允许自环 |
| `relation_type` | 必填 | 内置六类或启动配置注册的类型；定义见 S11。修订不改变既有端点和类型 |
| `revision` | `1` | 本次新修订，不是任务 revision |
| `status` | `"candidate"` | candidate / confirmed / needs_review / revoked；旧 proposed/retracted 兼容读取 |
| `evidence_refs` / `conditions` | `[]` | 可核查依据及适用条件；confirmed 必须有依据，verifies 必须有条件。确认关系不自动确认端点事实 |
| `schema_version` / `model_version` | `memory-relation/1` / `memory-relations/1` | 当前受支持的结构及关系模型版本 |
| `batch_id` | `""` | 选择性补建批次，供撤回核对，不改变权限 |
| `conflict_kind` | `""` | factual / temporal / advisory，空表示未分类 |
| `resolution` | `""` | prefer_a（source）/ prefer_b（target）/ both_valid / escalate / defer；仅 contradicts 可填写，advisory 不允许选胜者 |
| `resolution_reason` | `""` | 提交裁决时必填的依据说明 |
| `resolution_origin` / `resolution_refs` | `""` / `[]` | 裁决来源及验证关系依赖；显式人工裁决由服务端标为 human，自动策略填写其来源。不能靠自述来源获得信任 |

返回 `{status, relation_id, revision}`，`status` 为 projecting / committed。关系真源写入 Registry KV（先不可变修订记录，再原子替换关系头），不生成 MemoryUnit。首次需项目 WRITE；修改与撤回走**双条件**：①内容侧——对两端记忆都有 UPDATE 权；②登记侧——当前认证主体对应的连接须等于关系头记录的 `connector`，或调用者在项目上有 `MANAGE_SPACE`。旧关系头 `connector` 为空时只允许 `MANAGE_SPACE`。旧修订仅可为复核/撤回进行受控读取，不因此披露失效依赖。同修订同内容重试只补投影；projecting 可用 `continuity_recover(kind=relation)` 补投影。撤回写新修订，历史与正文保留。

### continuity_recover：核对未知写入与修复投影

签名：`continuity_recover(scope, recovery: ContinuityRecovery, *, security) -> dict`。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `recovery.kind` | 必填 | event / relation / derivation；relation 角色模式 |
| `recovery.operation_id` | 必填 | event、derivation 使用写入回执的 operation_id；relation 使用 relation_id |
| `recovery.refs` | event 必填非空 | 已实际提交、可核对的 MemoryRef，必须位于当前项目 Scope；relation、derivation 可为空 |

事件恢复返回 `{status, refs, next_action: "retry_original_write"}`，恢复本身不完成背景关联；关系恢复从 KV 关系头核对 `payload_digest` 并补投影，返回 `{status, relation_id, revision}`；派生恢复只核对计划与输出标识。均需项目 WRITE；事件恢复逐条要求 UPDATE 并校验原连接主体。引用/内容与原回执不匹配时报 ConflictError，无可靠引用不能调用此方法猜测成功。MemoryAPI 不提供公开的全量索引重建入口，本方法不重新抽取正文、不做全量记忆迁移。

### quarantine_source / release_source：来源隔离与解除

签名：`quarantine_source(scope, connector, reason, *, security) -> dict`；`release_source(scope, connector, *, security) -> bool`。

| 参数/字段 | 默认值 | 含义 |
|---|---|---|
| `connector` | 必填 | 隔离的来源连接名 |
| `reason` | 必填非空 | 隔离原因，随记录持久化并写入入口审计 detail |

两方法归入治理面，按项目空间 `MANAGE_SPACE` 判权并落入口审计；隔离记录持久化于 Registry `/continuity/sources/{connector}`，含 `reason`、`operator`（主体指纹）与 `quarantined_at`，释放即删除。隔离后：search（含视图）过滤该来源条目与其提交的关系；list 以系统过滤谓词下推、分页与 count 一致；get 单条返回 `PermissionDeniedError`。隔离只影响该来源，不改写正文、不删除关系；`release_source` 对未隔离来源返回 `False`。

### 显式事件时间与接续字段保留

`add` / `add_async` 新增可选 `event_time: datetime | None`，必须含有效时区，否则抛 ValidationError；映射 temporal.t_event，独立于 occurred_at（t_message）。显式时间是调用方提供的业务声明，不证明来源或事件真实性。`batch_add` / `batch_add_async` 接收批级 `event_time`，`BatchWriteItem.event_time` 逐项覆盖；`submit_ingest` 接收 `event_time` 并在入队前校验。三者的 `occurred_at` 仍只写消息时间，不能替代事件时间。派生输出不继承来源事件时间（见 F10 4.2）。MCP 的 add、add_async、batch_add、batch_add_async、submit_ingest 都透传该字段；legacy 文本 add、视频异步提交和 batch_add（`defaults.event_time` 与单项 `event_time`）同样支持。

公开 add/add_async、batch_add/batch_add_async（公共参数与每项合并后）、update 的 system_metadata 拒绝整个 continuity_* 前缀（含未来扩展键），统一返回 ValidationError；批量逐项失败沿用 BatchWriteOutcome.error_type。user_metadata 同名键只是用户数据，不具有接续语义。仅通过 `continuity` 参数经已鉴权的内部写路径生成系统接续字段，内部路径仍执行信任上下文校验、WRITE、空间可写、作者归属与审计，不向外暴露绕过标志。

## 可逆脱敏与用户访问边界

开启保护后，普通 add/batch_add/get/list/search/update 仍使用原方法签名，对 `redact` 片段
返回脱敏表示。路由预览、检索文本/query_context 及批量输入回显遵守同一策略；更新准备
与其他可信构建处理可使用本次原文，最终 MemoryUnit 在持久化及索引生成前保护。
受保护片段的原文不进入普通召回、后处理或响应缓存。`privacy_level`、`privacy_revision`、
`privacy_message_revision`、`privacy_rule_ids`、`privacy_categories` 是内核保留
system_metadata 键，调用方不能赋值；后两项仅为有限判级依据，不构成授权证明。

MemoryAPI 提供两个可选用户访问控制入口，不加入普通机器方法目录：

| 方法 | 契约 |
|---|---|
| `begin_human_view(payload, browser, *, security=None, primary_auth=None)` | 认证、READ 判权并暂存脱敏快照，返回人机挑战；登记使用受控邀请 |
| `finish_human_view(payload, browser, *, peer="")` | 验证一次性人机证明、复核资源，经 Engine 还原并返回专用终端响应 |

`primary_auth` 是可信宿主注入的延迟认证上下文工厂，不是 JSON 字段；API 内部进入它，
保留宿主常规认证的限流、审计和生命周期。进程内调用也可提供既有可信 `security`，
二者不可同时传入。未装配能力默认拒绝；采用非抽象可选方法保持已有实现兼容。
增量 `HumanViewRuntime.human_view` 只作转发适配，begin/finish 均调用上述 API 方法。
HTTP 提供
`GET /security/privacy`、同源静态脚本及 `POST /security/privacy/begin|finish`；
访问 begin 在 API 内执行常规认证，经 get PEP 和 Engine 查询脱敏快照，交由
PrivacyProtection 短期暂存；认证挑战绑定服务端实际 Scope/ID/revision、请求身份和浏览器。
finish 在 API 内委派人的证明验证，仍调用正常 get PEP 检查当前权限和快照；仅 ACTIVE
且版本有效时，由 API 经 Engine 的独立 HumanViewRenderer 能力组装首次快照，结果经
API 返回终端。普通 HTTP `/v1`、CLI 与 MCP 机器工具不注册这些用户访问控制入口。
不提供 reveal 布尔参数、可复用解密 bearer token、明文结果轮询或 SDK/MCP 还原工具。
该能力关闭/未装配不改变普通接口。`allow` 的直接写入豁免不属于仅人可见保证；
逐项请求/响应、挑战消费、审计及故障语义见
[S10 用户访问控制契约](S10-security.md#隐私保护与用户访问控制)，配置见 S08。
