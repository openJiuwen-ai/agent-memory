# F05 — HTTP / CLI 与 MemoryAPI 一对一对齐

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-05 |
| 影响范围 | `jiuwen_memory_entry/http_server/`、`jiuwen_memory_entry/cli/`、`jiuwen_memory_entry/core/api_contract.py`、`jiuwen_memory_entry/core/error_response.py`、`docs/specs/S02-memory-api.md` |
| 测试基线 | entry / common security / API 相关单测 680 项及真实 HTTP 异步写入集成测试 4 项通过，共 684 项；修改的 Python 文件 Ruff、编译及本地 CodeCheck 结构预检通过，未重跑全仓 UT，未执行云端 CodeCheck |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

## 背景

改造前的 HTTP surface 已形成一套独立协议：范围使用 `target.tenant_id` / `target.scope`，
部分参数使用 `item_id` / `k` / `hard` 等别名，`add` 还能根据视频参数改调
`submit_ingest`，返回值统一包装成 `{ok, op, ...}`。同时，HTTP、CLI 和 MCP 共用的
legacy handler 只注册部分 verb，导致 `MemoryAPI` 已有方法不能自然从 HTTP 访问。

这套形态要求调用者同时学习 Python API 和 HTTP API，两套字段、默认值、返回对象及方法
数量也会独立漂移。目标是让 HTTP 只承担认证、JSON 转换和网络传输，不再表达第二套业务
语义。CLI 同样不保留旧命令与旧返回结构：当前尚无存量使用，无需引入双向兼容转换。

## 决策

### 1. 以 MemoryAPI 公开方法集作为路由注册表

HTTP verb 集合直接取 `MemoryAPI.__abstractmethods__`。每个方法暴露为
`POST /v1/<method_name>`，因此当前 36 个公开方法全部具备同名 URL。`GET /healthz` 是唯一
不属于 `MemoryAPI` 的 HTTP 运维接口。

### 2. 请求契约从方法签名派生

请求体必须是 JSON object。除 `self` 和 `security` 外，字段集合、必填/默认关系及目标类型
由对应方法的 `inspect.signature` 与 `get_type_hints` 派生。JSON 只做以下机械转换：

- 数据类使用原字段名和原嵌套层级；
- 枚举使用枚举值字符串；
- `datetime` 使用 ISO 8601 字符串；
- `list` / `dict` / `set` / `frozenset` 递归转换为 JSON 对应结构；
- 省略可选参数时不在 HTTP 层补值，由 API 默认值生效。

未知字段、嵌套数据类未知字段和类型不匹配均在 API 调用前返回 `ValidationError`。服务端不
接受旧 `target`、`item_id`、`k`、`modality` 等别名。

### 3. security 只由认证边界注入

`security` 仍是 `MemoryAPI` 的正式参数，但不是客户端可填写的业务数据。HTTP 从认证头构造
可信 `RequestSecurityContext`，再以 `security=` 传给同名方法。请求体声明 `security`、
`actor`、`actor_*` 或其他身份字段时直接拒绝。启动器默认 `required` 且未装配生产安全运行时
时继续 fail-closed 返回 503；显式 `dev` 模式仅为本地功能测试提供固定具名 ROOT 身份，仍走
同一认证上下文构造与授权链路。

HTTP 开发入口装配的是最小 `DevHttpSecurityRuntime`，只提供 dev Authenticator；
`rate_limiter` / `workload_guard` / surface `audit` 均为空，不是完整生产 `SecurityRuntime`。
这不关闭 API 本身的授权和业务审计。认证模式按 `--auth-mode`、
`JIUWEN_MEMORY_HTTP_AUTH_MODE`、`required` 的优先级确定；CLI 本地模式只使用自己的
`--auth-mode`，不读取 HTTP 认证模式环境变量。

回环保护在 `HttpServer.serve()` 创建 socket 前执行，标准启动脚本经过该入口。
`handler_cls()` 只生成请求处理器：嵌入方自行创建 HTTP Server 时必须自行保证绑定策略，
不能把处理器视为独立的监听保护。第三方认证器未覆写 `requires_loopback_binding()` 时
同样要求回环绑定；拒绝提示使用其中性的模式名称，不引导其开启仅适用于 dev 的例外。
已注入 `binding_policy` 时由该策略优先裁决，dev 例外不能覆盖策略拒绝。

> **危险开关：`JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK=true`。**
> 在未注入独立绑定策略时，它显式解除 dev 的非回环绑定限制，不提供认证、TLS 或限流。
> 任何能连接该 HTTP 服务的人都会使用相同的测试身份，再接受 API 的业务授权判定。
> 只允许在部署边界已隔离的测试容器中使用，不能依靠 warning 防止远程访问。
> 当前记录启动 warning，dev runtime 未装配 surface audit，不产生专门的绑定例外审计事件。

### 4. 同名调用并返回原值

普通方法直接调用，`add_async` / `batch_add_async` 按 Python 协程约定等待执行并等待完成。
两类方法都只递归序列化原返回值，不增加 `{ok, op, result}` 包装，也不向成功对象插入
`request_id`。

`async def` 不等于 job API：异步写入仍分别返回 `list[MemoryUnit]` 与
`BatchWriteResult`。`evolve` 返回任务 ID、`submit_ingest` 返回 `IngestSubmission`，来自
这两个方法自身的公开契约，HTTP 不额外产生 202、job ID 或轮询协议。

### 5. CLI 采用同一契约，不做旧格式转换

HTTP 和 CLI 都不再经过 `DispatchRequest` / shared handler。请求解析、同名 API 调用和
原返回值序列化放在 `core/api_contract.py`，领域错误状态与脱敏放在 `core/error_response.py`。
HTTP DTO 不再有独立文件，HTTP 与 CLI 不各自维护一套参数转换。

CLI 当前暴露 36 个 API 同名命令；每个方法的参数由签名生成 `--<parameter_name>`，
保留原字段、默认值和 JSON 嵌套关系。`unit_id` 不改成 `item_id`，`patch` / `selector`
不再由 CLI 推导，字符串与枚举直接传文本，对象和集合使用 JSON。

- 本地：`InProcessClient` 装配一次 runtime，经认证边界直接调用 API；
- 远程：`HttpClient` 原样发送 JSON 到同名 URL，原样返回服务端 JSON；
- 返回列表、字符串、对象或 `None` 均保留原结构，不还原旧 `item` / `hits` envelope；
- `text` / `table` / `quiet` 直接消费原字段，只影响展示；
- `healthz` 与 NDJSON `batch` 是接入工具，后者复用同一个 runtime，不等于 API `batch_add`。

本地身份由注入的 Authenticator 产生，并经 `authenticated(..., surface=Surface.CLI)`
构造安全上下文；不能根据业务 scope 推导 actor。默认 `required` 未配置时返回 503；
显式 `--auth-mode dev` 才启用固定 `local/developer` 测试身份，仍执行 API 授权。
远程 CLI 的 `AGENT_MEMORY_API_KEY` 作为 Bearer 凭据发送，认证模式由服务器控制。
请求结束清理上下文，命令成功或失败均关闭 client/runtime。

方法集合、参数名和声明类型按 API 对齐；API 在类型注解之外接受的运行时扩展仍需单独核对。
目前写入 `system_metadata.coords` 存在传输解析缺口，见“已知遗留”，不应把方法覆盖理解为
所有运行时扩展都已验证等价。

MCP 和其他旧调用方的 legacy adapter 保留，本次不改它们的协议。这只保证仍调用
legacy handler 的入口；旧远程客户端不能因此继续使用已升级的 HTTP 协议。

## 拒绝的方案

### 保留旧 HTTP 并新增 `/v2`

拒绝。两套协议会长期保留字段和返回语义漂移，也与本次直接统一现有 HTTP 的范围不符。

### 继续扩充共享 handler 的 36 个业务分支

拒绝。逐方法手写参数和返回转换会再次复制 `MemoryAPI` 签名。HTTP / CLI 直接以公开 API
为契约，legacy handler 只服务 MCP 和需要它的旧调用方。

### 保留 CLI 的旧请求 / 响应双向转换

拒绝。当前仍处于开发阶段，没有存量 CLI 用户要兼容。保留旧命令会让调用方同时学习
两套接口，也会掩盖 `MemoryUnit`、`SearchResult` 等原返回类型；应一次统一到 API 格式。

### 把 `add_async` 转为提交任务并返回 202

拒绝。`add_async` 的原返回类型是 `list[MemoryUnit]`，改成 job 会改变 API 语义。任务能力
已经由 `submit_ingest`、`evolve`、`job_status` 和 `job_cancel` 明确表达。

### 手写 36 份 HTTP DTO

拒绝。手写 DTO 能提供更细的定制错误消息，但字段、默认值和嵌套类型容易与 API 漂移。
签名派生配合契约测试能在 API 增删方法或参数时立即暴露差异。

## 验证

- HTTP 方法集合与 `MemoryAPI.__abstractmethods__` 精确相等，当前为 36 个；
- 所有方法的请求字段集合与 API 签名精确相等（排除 `self`、`security`）；
- 覆盖 `Scope`、`Context`、`BatchWriteItem`、`MemoryPatch`、`DeleteSelector`、`Grant`、
  `SpaceSpec`、`SpaceMember`、枚举、集合及时间转换；
- 同步 `add` 与异步 `add_async` 均验证同名调用和原返回值序列化；
- 真实 HTTP socket 验证认证、fail-closed、错误映射、原返回值、健康检查及异步调用；
- 绑定边界验证 IPv4/IPv6 非回环地址在创建 socket 前被拒绝、显式 dev 例外记录 warning、
  第三方认证器不会收到 dev 例外提示，以及独立绑定策略拒绝不会被 dev 例外覆盖；
- `tests/integration/jiuwen_memory_entry/test_http_async_write.py` 经标准 `serve()` 启动真实
  HTTP，在 `in_process` / `async_timer` 两种调度配置下验证连续 `add_async`、
  `batch_add_async` 及后续 get/list；不替换 API、Engine、索引和内存存储实现。
  该成功基线不覆盖需要常驻事件循环的中期定时任务；
- 共享 JSON 契约测试统一命名为 `tests/unit/jiuwen_memory_entry/test_api_contract.py`，
  与 `core/api_contract.py` 对应；
- CLI 的全部命令、参数名与必填关系逐项对照 API 签名；未传可选参数不补默认值；
- CLI 本地与真实 HTTP 服务均验证增删改查、异步写入、旧参数与自述身份拒绝；
- 验证原 JSON 值不包装、actor 与业务 scope 分离、请求上下文清理、batch 共享实例及资源关闭；
- `examples/demo_cli.py` 使用显式 dev 认证和 API 原参数完成本地调用演示。

## 已知遗留

- HTTP server 基于 `ThreadingHTTPServer`；`invoke_api` 对协程方法使用 `asyncio.run`，
  请求结束后关闭临时事件循环。这能返回当前写入结果，但不能保证后台 Task 跨请求存活。
  已在真实链路复现：`scheduler.default.target=async_timer`，向 `add_async` 传入
  `system_metadata={"infer":"true","middle":"true","middle_interval":1}`，并将调度器
  `tick_interval` 配为 1；请求返回 200、WORKING 记忆已写入，但定时 Task 被取消、所属
  loop 已关闭。直接 await 相同链路并保持调用方 loop 运行时，定时任务能正常触发。
  既有同步 `MemoryAPI.add/batch_add` 也使用临时 loop，因此不是仅 HTTP 新增的限制。
  本次补充普通真实异步写入回归并记录该问题，不修改事件循环架构；后续需统一处理 loop
  所有权、跨线程调度、后台任务及关闭生命周期，不能只改一处 `asyncio.run`。
  后续更换执行方式不得改变 URL、参数、返回契约或把异步写入改为额外 job 协议。
- `scripts/run-server.sh` 的默认 `required` 模式仍未自动装配生产 `security_runtime`，业务接口按
  安全约束返回 503。显式 `--auth-mode dev` 可以测试业务接口，但只跳过凭据校验，不跳过
  `MemoryAPI` 授权，也不得用于生产环境。
- CLI 的生产认证器尚未接入配置装配；程序内可显式注入 Authenticator，命令行功能测试
  使用 `--auth-mode dev`。默认内存后端不会跨 CLI 进程保留记录，连续调用需 batch、持久化后端或常驻 HTTP。
- 旧 HTTP URL、CLI 命令别名与旧返回 envelope 不保留兼容；调用方需要一次性迁移。
- 写入归属坐标尚未完成传输对齐：Python API 的 `system_metadata.coords` 运行时允许
  `dict[str, str]`，但该值不在 `MetadataValueType` 的声明联合类型内。共享 JSON 解码器
  按注解校验，会在调用 API 前拒绝它；HTTP 返回 400，CLI 也在调用前报参数错误。
  需要写入归属判定时，目前应直接调用 Python API。检索侧 `Context.extensions` 声明为
  `dict[str, Any]`，`extensions.coords` 能通过 JSON 解码，不能与写入侧混为一谈。
  这是当前实现缺口，不是要改变 S02 中归属坐标的目标契约。
- 本仓库 `jiuwen_memory_adapter/jiuwenswarm/agent_memory_provider.py` 的远程客户端尚未迁移：
  请求仍使用 flat `tenant_id` / `scope` / `k`，响应仍读取 `ok` / `item_id` / `hits`。
  即使启用 dev 认证，它也不能直接连接当前 HTTP；需调用方后续对齐请求和原返回类型。
  SDK 进程内模式不经过 HTTP，但仍使用过渡安全上下文；本次不修改该 adapter。
