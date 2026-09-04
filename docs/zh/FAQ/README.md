# agent-memory FAQ：问题定位手册

面向使用者与运维的**问题定位手册**：遇到问题时，按"日志位置确认 → 现象分流 → 进入对应场景按步骤排查"的路径走。安装、Docker 部署、配置等通用问题见[部署说明](../../../deploy/docker/README.md)。

## 目录

- [一、问题定位](#一问题定位)
  - [1. 日志位置确认](#1-日志位置确认)
  - [2. 现象分流](#2-现象分流)
  - [3. 场景 1：服务不通 / 起不来](#3-场景-1服务不通--起不来)
  - [4. 场景 2：请求直接报错](#4-场景-2请求直接报错)
  - [5. 场景 3：写入成功但召回为空（最高频）](#5-场景-3写入成功但召回为空最高频)
  - [6. 场景 4：召回结果不符预期](#6-场景-4召回结果不符预期)
  - [7. 场景 5：请求慢](#7-场景-5请求慢)
- [附录 A：错误码对照表](#附录-a错误码对照表)

---

## 一、问题定位

### 1. 日志位置确认

**引擎日志在哪看**

引擎日志默认**只输出到终端**；要落盘需在 `config.yml` 的 `memory_api.globals` 段配置：

```yaml
memory_api:
  globals:
    log_file: /var/log/agent-memory/engine.log   # 落盘路径；不配则只终端输出
    log_level: INFO                                # 默认 INFO
```

按你的运行形态找到日志：

| 运行形态 | 引擎日志在哪看 |
|---|---|
| Docker | `docker compose logs -f agent-memory`（终端流）；容器内 `log_file` 配置路径（若配） |
| 本地 HTTP 服务 | 启动终端；`log_file` 配置路径（若配） |
| CLI | 控制台；`log_file` 配置路径（若配） |
| SDK（进程内） | 引擎日志不冒泡到宿主 root logger，宿主需直接读 `agent_memory.*` 子树或自行调 `setup_logging` |

两点预期管理：

- **HTTP / CLI 入口通常不记请求级日志**；内部异常仅记录脱敏的 `request_id` 和异常类型。请求的成功/失败信息全部在 HTTP 响应体里；HTTP 响应同时提供 `request_id` 与 `X-Request-ID`，可用于和审计记录、下游日志关联。
- 日志名以模块前缀自标识（如 `agent_memory.construction.index_builder_impl.vector_index_builder`），按前缀过滤即可区分层。

**如何在海量日志里定位到"我这个请求"的日志段**

HTTP 请求由服务端生成贯穿响应与审计的唯一 request id；引擎内部日志仍主要靠两个业务标识串联：

1. **scope**（`org/space/user/session`）——`Engine.write ...` / `Recaller ...` 等行都带 scope，用它过滤日志缩小到该租户/会话的日志段；
2. **unit id 前 8 位**——`VectorIndexBuilder` 的 WARN 行带它，可从 add 响应的 `item_id` 直接对应到具体记忆单元。

### 2. 现象分流

分流的第一依据是 **HTTP 响应体**——多数问题读完响应就有答案，先读响应，再决定要不要翻日志。

| 字段 | 含义 |
|------|------|
| `ok` | `true` 表示请求链路本身成功走完 |
| `error` / `message` | 失败时的异常类名与原因；类名对照[附录 A](#附录-a错误码对照表) 定位语义 |
| `item_id` | add 成功返回写入的记忆 id；`null` 且带 `skipped` 见下 |
| `skipped` | add 特有：`infer=true` 时派生记忆全部被去重判为 update/noop，**是正常语义，不是失败** |
| `trajectory` | search 带 `"trace": true` 时返回的检索轨迹，见[场景 3](#5-场景-3写入成功但召回为空最高频) 第 ② 步 |

判定优先级：`error` 非空即失败（进入场景 2）；`ok=true` 只代表请求链路成功，**不保证记忆已可检索**（索引构建失败会静默降级，进入场景 3）。

错误响应统一包含 `error`、`message`、`request_id`、`retryable` 四个字段，并在 `X-Request-ID` header 返回同一个 request id。400/策略类错误的 `message` 会经过脱敏；401/403/404/409/429/500/503 使用固定泛化文案，HTTP 不返回 traceback，也不会回显凭据。

拿到现象后，对号入座：

```text
遇到问题
│
├─ 服务起不来 / 请求根本发不通 ──────────→ 场景 1
├─ 请求有响应，但返回 error ────────────→ 场景 2
├─ 写入返回 ok=true，但 search 召回为空 ─→ 场景 3（最高频）
├─ 能召回，但结果不对（漏/偏/多/旧）────→ 场景 4
└─ 请求能通，但慢 ─────────────────────→ 场景 5
```

### 3. 场景 1：服务不通 / 起不来

**① 探活**：`curl http://localhost:8137/healthz` 只确认**服务进程本身**存活（正常返回 200 `{"status": "ok", "profile": ...}`）；它**不探测后端**——Redis/ES/Milvus 不可用时 healthz 仍返回 200。后端不可用表现为实际请求报 `BackendError`（进入[场景 2](#4-场景-2请求直接报错)）或容器 unhealthy（进入 ②）。

**② 看容器状态**：`docker compose ps`，找 unhealthy / 反复重启的容器。

**③ 分服务看日志**：`docker compose logs <服务名>`。应用容器看启动期日志找启动异常；后端容器（Redis/ES/Milvus）看各自报错（端口冲突、挂载路径、内存不足是常见原因）。

**④ 服务起来但请求 404**：`{"error": "UnknownVerb"}` → verb 拼写错误，对照路由表（add / batch_add / search / list / get / update / delete / evolve / ...）。

### 4. 场景 2：请求直接报错

**① 读状态码与 error 类名，对照下表分流**

| error 类名 | 出问题的层 | 下一步 |
|---|---|---|
| `ValidationError` | 请求参数校验 | 按 `message` 自查入参（metadata 已拆分为 system/user 两段、`k` 非正整数、参数缺失），无需查日志 |
| `NotFoundError` / `ConflictError` / `PermissionDeniedError` | API 层语义校验 | id 不存在 / 重复创建 / scope 未授权，按 `message` 处理 |
| `InvalidExtractionJSONError` | LLM 抽取 | 日志搜 `Extractor`（含 `LLM response is not valid JSON` WARN），确认 LLM 端点可用、返回未被截断 |
| `LockError` / `LockTimeoutError` / `LockLostError` | 分布式锁 | 多实例并发写同一 scope 的锁竞争；检查 Redis 锁配置与实例数 |
| `BackendError` | 存储层 | 后端网络/IO 故障；转[场景 1](#3-场景-1服务不通--起不来) ②③ 分服务看容器日志 |
| `PayloadTooLarge` | HTTP 入口 | 请求体超过服务端限制；压缩或拆分请求后再发送 |
| `StorageRetrievalError` | 召回层 | 关键词+向量通道**同时**故障，按 Milvus / ES 分别排查 |
| `UnsupportedStorageCapabilityError` | 装配配置 | 配置里移除了某后端但算子仍引用其能力，核对 config 与算子一致性 |
| `InternalError` | 内部 bug | 响应只返回固定 `internal server error`；使用 `request_id` 关联服务端失败记录与审计 |

完整状态码映射见[附录 A](#附录-a错误码对照表)。

**② 按 scope 过滤引擎日志**（见[1. 日志位置确认](#1-日志位置确认)），定位该请求的日志段。

**③ 沿调用链逐层下钻**（各层日志前缀与含义）：

```text
POST /v1/<verb>
→ HTTP 入口（verb 分发 + 参数校验，无日志；ValidationError 在此抛出）
→ API 层（鉴权与参数装配，无日志）
→ Engine（日志前缀 Engine.，write 按 infer/procedural/middle 分派）
   ├─ 写入 → 抽取与索引
   │    ├─ Extractor（前缀 Extractor:，LLM 抽取与重试）
   │    └─ IndexBuilder（前缀 Forward/Fulltext/Vector/HybridIndexBuilder:，
   │         嵌入与向量写入失败在此静默降级为 WARN）
   └─ 召回
        ├─ Recaller（前缀 KeywordRecaller: / VectorRecaller:，带 hits=/units= 命中数）
        └─ Retriever / Fuser（多路融合；部分通道失败返回 ChannelError）
→ 存储层（后端 IO 失败抛 BackendError；scope 隔离在此强制）
```

### 5. 场景 3：写入成功但召回为空（最高频）

`ok=true` ≠ 记忆可检索。写入路径中，嵌入与向量写入失败**只记 WARN 并继续**，HTTP 仍返回 `ok=true`。按以下顺序排查：

**① 按写入时间点检查引擎日志，确认索引真正建立**

| 日志行 | 含义 |
|---|---|
| `Engine.write infer=True: N originals, M derived added, scope=...` | infer 抽取路径完成，M 为派生条数 |
| `ForwardIndexBuilder: building forward index for N units` | 记忆本体（正排 KV）写入，缺它则 get/list 都查不到 |
| `VectorIndexBuilder: building index for N units` | 向量索引开始构建 |
| `FulltextIndexBuilder: building index for N units` | 全文索引构建 |
| **WARN** `VectorIndexBuilder: Embedder.embed failed for unit ...` | **嵌入失败，该单元向量索引缺失**——关键词可召回、向量召不回的直接根因 |
| **WARN** `VectorIndexBuilder: VectorStore.insert failed for scope ...` | **Milvus 写入失败**，向量索引整体缺失 |
| `Extractor: received N units, M accepted after preprocessing` | LLM 抽取入口；M=0 说明预处理全部拒收 |

命中 WARN 的处理：

- `Embedder.embed failed` / `VectorStore.insert failed` → 静默降级已发生，修复嵌入端点 / Milvus 后重写该批记忆；本体（正排）不受影响，`get`/`list` 仍可查到
- `Extractor` 的重试类 WARN → 内部有重试，达到阈值才上抛错误；只有最终失败才是写入失败

**② search 带 `"trace": true` 分通道二分**

响应的 `trajectory` 数组每步含 `stage` / `channel` / `candidate_count` / `cost_ms`，哪个通道在哪一步归零一目了然：

- Keyword 通道 `candidate_count>0` 而 Vector 通道为 0 → 嵌入问题，回到 ① 查降级 WARN
- 两个通道都为 0 → scope 不一致或后端故障，进入 ③ / [场景 1](#3-场景-1服务不通--起不来)
- 也可在日志搜 `KeywordRecaller:` / `VectorRecaller:` 的 `hits=N units=N` 行核对各通道命中数

**③ 核对 scope 一致性**

写入与查询的 scope（`org` / `space` / `user` / `session`）必须完全一致——**scope 是隔离轴，跨 scope 查不到是设计行为**，不是丢数据。

**④ 带 as-of 时间回溯的查询**：确认单元 `t_valid` 已正确落值，缺失 `t_valid` 的单元会被 as-of 查询漏掉或排序异常。

### 6. 场景 4：召回结果不符预期

**① 召回的 metadata 为空**：SDK `recall()` 需要 metadata 输出时必须显式带 `output_fields=["metadata"]`，否则返回空。

**② metadata 过滤不生效**：过滤键必须加 `user_metadata.` 前缀，裸键名匹配不到。

**③ 召回了"本不该出现"的旧记忆**：bitemporal 模型下 as-of 查询会按 `t_valid` 回溯历史版本，属正常语义；若单元缺失 `t_valid` 会造成误回溯，转[场景 3](#5-场景-3写入成功但召回为空最高频) ④。

**④ 结果偏/漏**：search 加 `"trace": true` 看各通道 `candidate_count`（见[场景 3](#5-场景-3写入成功但召回为空最高频) ②），判断是单通道质量问题还是融合问题。

### 7. 场景 5：请求慢

| 现象 | 定位 |
|---|---|
| 首次请求慢（数秒） | **正常**：local 模式 bge 嵌入模型装载，之后常驻内存 |
| 写入慢（infer=true） | LLM 抽取链路耗时；日志搜 `Extractor:` 确认 LLM 端点延迟与重试次数 |
| 召回慢 | `trace=true` 的 `trajectory` 里每步 `cost_ms` 直接给出耗时环节；向量通道慢优先查 Milvus 负载 |
| 间歇性慢 | 后端资源争用；`docker stats` 看容器资源水位 |

---

## 附录 A：错误码对照表

agent-memory 的错误体系是**两层结构**（区别于数字错误码）：

1. **异常层**：12 个异常类（基类 `AgentMemoryError` + 11 个子类；另有锁异常族与抽取异常族，见 A.2），SDK 调用方可跨后端、跨层用同一套异常捕获；
2. **HTTP 层**：HTTP 入口统一转状态码，响应体固定包含 `error`、`message`、`request_id`、`retryable`，并通过 `X-Request-ID` 返回同一个 request id。

### A.1 异常类 ↔ HTTP 状态码对照表

映射规则：HTTP edge 按下表将异常翻译成稳定状态；非 HTTP legacy dispatch 保持既有兼容语义。

| 异常类 | HTTP | 语义 | 典型成因 |
|---|---|---|---|
| `NotFoundError` | 404 | 目标实体/记录/键不存在 | get/update/inspect/trace 的 id 不存在；scope 为空 |
| `MethodNotAllowed` | 405 | HTTP 方法不受支持 | 仅使用已注册的 POST/GET 入口 |
| `PermissionDeniedError` | 403 | actor 无权对 target scope 执行该 action | policy 未授权该 actor/action/scope 组合 |
| `ConflictError` | 409 | 与现有记录冲突 | insert 时 id 已存在；space 重名 |
| `PartialFailureError` | 409 | 批量操作部分成功 | 按 `retry_action` 重试 `failed` 项；响应保留 `completed` / `failed` / `retry_action` |
| `ValidationError` | 400 | 入参非法或越界 | metadata 含非标量值（dict/list）；`k` 非正整数；参数缺失 |
| `UnsupportedCapabilityError` | 400 | 组件不支持请求能力 | 请求的 modality 或路由能力未装配 |
| `PolicyError` | 400 | 运行时策略操作被拒 | policy 键未知；试图修改不可变配置 |
| `AuthenticationError` | 401 | 凭据缺失/格式非法/校验不通过 | API key 错误或缺失；对外固定 `authentication failed` |
| `RateLimitedError` | 429 | 超出速率上限（发生在认证之前） | 限流桶耗尽；`retryable=true`，`Retry-After: 1` |
| `HealthCheckError` | 503 | 组件健康检查失败 | Redis/ES/Milvus/嵌入器等组件 health() 探测失败；`retryable=true` |
| `BackendError` | 503 | 底层存储非预期失败 | 后端网络/IO 故障；`retryable=true` |
| `PayloadTooLarge` | 413 | 请求体超过限制 | 缩小或拆分请求；`retryable=false` |
| `UnsupportedStorageCapabilityError` | 400 | Storage 未声明请求的端口能力 | 配置里去掉了某后端但算子仍引用其能力 |
| `StorageRetrievalError` | 400 | 所有选中召回入口均失败 | 关键词+向量通道同时故障 |
| 非预期异常 | 500 | `{"error": "InternalError", "message": "internal server error"}` | 内部 bug；使用 `request_id` 关联服务端失败记录与审计 |

未知 verb：404 `{"error": "UnknownVerb"}`。

### A.2 写入路径的特有错误

| 异常 | 触发点 | 含义 |
|---|---|---|
| `InvalidExtractionJSONError` / `InvalidExtractionCandidateError` | LLM 抽取环节 | LLM 抽取输出不是合法 JSON / 候选结构不合法；内部有重试，最终失败才上抛（HTTP 面落 500） |
| `LockError` / `LockTimeoutError` / `LockLostError` | 分布式锁 | 多实例并发写同一 scope 时锁竞争/租约失效 |

### A.3 错误信息脱敏说明

`safe_error_message` 会将异常文本中的 `password / passwd / pwd / token / api_key / secret` 键值、`Authorization: Bearer/Basic ...` 头、URL 内嵌凭据（`//user:pass@`）统一替换为 `<redacted>`，并截断到 200 字符。HTTP 400/策略类错误统一调用该函数；401/403/404/409/429/500/503 使用固定泛化文案。HTTP 不返回 traceback，排障时用响应中的 `request_id` 到服务端日志和审计记录定位。
