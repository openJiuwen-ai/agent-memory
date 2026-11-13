# 请求级 Trace 组件设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-09 |
| 状态 | 请求级追踪已实现；后续统一设计见 F11 |
| 关联模块 | `jiuwen_memory/common/trace/`、`jiuwen_memory_entry/core/api_contract.py` |
| 接口契约 | [S07 · TraceCollector](../specs/S07-common.md) |
| 决策与拒绝方案 | [F11-new-trace.md](../features/common/F11-new-trace.md) |
| 验证环境 | WSL2 / Python 3.14.4；本文示例输出均来自实际运行，耗时与 ID 每次不同 |

本文讲组件是什么、怎么用、影响什么。接口签名以 S07 为准，方案取舍的完整记录在 F11，
本文不重复。

## 1. 需求背景

### 1.1 要解决的问题

一次写入要经过摄入、路径分流、抽取、去重、建索引；一次检索要经过解析、多路召回、
融合、重排、阈值和披露。只看最终结果，很难回答这些问题：

- 这次写入走了 raw、infer、middle 还是 procedural 路径？
- 检索候选是在哪个阶段减少的？某一路召回是否降级了？
- 去重为什么判定为 NOOP？命中了谁，分数是多少？
- 耗时集中在哪一段？

原有手段都覆盖不全：

| 现有手段 | 不足 |
|---|---|
| 分散的调试日志 | 无法按请求还原调用关系；部分日志含正文预览，不能随意打开 |
| 检索 `trajectory` | 只覆盖检索，且是结果对象的一部分，不是横切诊断 |
| `MemoryAPI.trace` | 回答的是“这条记忆来自哪里”，不是“这次请求做了什么” |

### 1.2 目标

1. 记录一次请求的处理过程，结构为「组件 → 阶段」两级命名的调用树。
2. 可按配置切换：返回给调用方、写日志、两者都要、关闭。
3. 默认关闭，关闭时开销可以忽略，返回结构不变。
4. 不改业务方法签名，不沿调用链传递 trace 实例。
5. 可对单次请求临时开启，服务无需重启。

### 1.3 非目标

- 跨服务分布式追踪（不引入 OpenTelemetry 依赖）。
- 持久化与事后查询：没有“按 trace_id 查历史”的能力。
- 后台任务追踪：请求结束后才执行的任务不在范围内（见 [§2.7](#27-请求边界与后台任务)）。

### 1.4 容易混淆的三个概念

| 能力 | 回答的问题 | 触发方式 | 生命周期 |
|---|---|---|---|
| 本文的请求级 trace | 这次请求经过了哪些阶段？ | config 或 `extensions.trace` | 请求内收集，随响应返回或写日志 |
| `trace` verb（血缘回溯） | 这条记忆由哪些记忆演化而来？ | 调用 `MemoryAPI.trace` | 读取已持久化的 `provenance` |
| 检索 `trajectory` | 这次检索的步骤轨迹？ | `with_trajectory=True` | 检索结果的字段 |

响应里的 `trace` 键属于第一种。`trace` verb 的响应不会叠加过程快照，避免语义冲突。

## 2. 设计方案

### 2.1 总体结构

```
 HTTP / CLI / MCP                      legacy handler.dispatch
        │                                       │
        └──► api_contract.invoke_api ◄──────────┘  （同口径的独立填充点）
                │  1. pop_trace_target：剥离 extensions.trace
                │  2. begin_trace：建立请求作用域
                ▼
        MemoryAPI → Engine → Evolver / Extractor / Retriever
                │        trace_event / @traced / trace_annotate
                ▼        （模块级函数，经 ContextVar 找到当前请求）
        common/trace/trace.py
          ├─ _CURRENT: ContextVar[_TraceRequest]   本次请求的容器（调用树、计数、关闭标记）
          ├─ _SPAN:    ContextVar[_Node]           当前 span
          └─ TraceCollector.record()               无状态 sink（共享单例）
                │
                ▼
        end_trace → 快照 → 响应 `trace` 键 / 错误体 / 日志行
```

认证、限流和 HTTP body 解析都发生在 `invoke_api` 之前，不计入调用树。

### 2.2 零传参：ContextVar 作用域

业务代码 import 模块级函数后直接调用，不持有 collector。设计上有三个要点：

- **容器是可变对象**。`_CURRENT` 里放的是 `_TraceRequest`。`asyncio.run`、`to_thread`
  复制 context 后，副本仍指向同一个容器，所以子任务写入的节点在请求结束时都能取到。
- **当前 span 单独存一个 ContextVar**。`gather` / `create_task` 复制 context，子任务各自
  持有发起时的父 span，并发子任务是平级关系，不会互相嵌套。
- **实现类无状态**。请求数据全部在容器里，`TraceCollector` 实例可被所有请求共享，
  因此不需要每请求 `fork()` 的协议。

`ThreadPoolExecutor.submit` 不复制 context，线程池内的埋点会静默丢弃。

### 2.3 数据模型：调用树

节点分两种：

| 节点 | 产生方式 | 特征 |
|---|---|---|
| span | `@traced` / `trace_span` | 包住一段执行，有 `cost_ms`，可有 `children`；异常时记 `error`（仅类型名） |
| event | `trace_event` | 一个时间点的结构化数据；同名事件按发生顺序逐条保留 |

`trace_annotate` 把数据合并进当前 span，用于在结束前补结果计数。

快照结构：

```json
{
  "trace_id": "69c6aee828d2445eb4cf581ac7f3c05c",
  "spans": [{
    "component": "InMemoryEngine",
    "stage": "write",
    "cost_ms": 1.135,
    "children": [
      {"component": "InMemoryEngine", "stage": "ingest",
       "data": {"units": 1, "modality": "text", "path": "raw"}},
      {"component": "InMemoryEngine", "stage": "result",
       "data": {"path": "raw", "originals": 1, "returned": 1}}
    ]
  }]
}
```

- `spans` 是根节点列表，里面既可能有 span 也可能有 event。一次 `batch_add` 会有多个根。
- `dropped` 只在超出节点上限时出现。
- debug 节点带 `"level": "debug"`，info 节点不带该字段。
- `trace_id` 优先取认证中间件设置的 `request_id`，HTTP 下与响应头 `X-Request-ID` 相同；
  没有 request_id 时现生成。

### 2.4 四种实现

| target | 响应带调用树 | 写日志 | 典型用途 |
|---|---|---|---|
| `noop`（默认） | 否 | 否 | 常规运行 |
| `memory` | 是 | 否 | 单次请求诊断 |
| `log` | 否 | 是 | 服务端观测，不改响应 |
| `both` | 是 | 是 | 两者都要 |

内部只有一个模型：调用树加可选的日志 sink。`both` 是 `LogTraceCollector` 的子类，只多
一个 `buffers = True`。`noop` 声明 `enabled = False`，`begin_trace` 把作用域置空，
同时屏蔽外层作用域。

### 2.5 详细程度：TraceLevel

只有 `info`（缺省，计数、耗时、路径）和 `debug`（逐条明细）两档。低于阈值的埋点在拷贝
数据前就被丢弃，所以明细埋点可以写在循环里。低于阈值的 span 不建节点，其内事件挂到外层。

不设 warn/error：trace 记录的是正常处理过程，失败由异常和日志承载，span 已记录异常类型。

log sink 一律以 logger 的 INFO 写出，级别写在行内。这样请求开了 `log:debug` 不会再被日志
级别过滤一次。但如果 logger 本身设为 WARNING 以上，trace 日志仍然看不到。

### 2.6 响应填充

填充发生在入口层，内核返回类型不变：

| 情况 | HTTP / CLI / MCP 的返回 |
|---|---|
| noop、log，或没有产生任何节点 | 原结果，不变形 |
| 结果序列化后是 dict | 增加 `trace` 键（同名键会被覆盖） |
| 结果是 list 等其他类型 | 包成 `{"result": 原结果, "trace": 快照}` |
| 调用的是 `trace` verb | 不叠加快照（log sink 仍会写） |
| 抛出 `AgentMemoryError` | 快照挂在异常上，HTTP/CLI 经 `error_trace(exc)` 放进错误体 |
| 其他异常（映射为 `InternalError`） | HTTP/CLI 不附加，避免透出内部细节 |

MCP 错误只转成 `RuntimeError` 文本，不带 trace。legacy `dispatch` 的口径略有不同：只要
快照非空就附加，包括 500 响应。

### 2.7 请求边界与后台任务

`end_trace` 把容器标记为 `closed`。后台任务创建时复制的 context 仍指向这个容器，但关闭后
其事件一律丢弃：既不写进无人读取的 buffer，也不会用已结束请求的 trace_id 写日志。旧 span
经 `owner` 校验，不会成为下一个请求的父节点。

因此，在请求结束后才执行的流程都不进快照，例如 middle 路径的 MiddleToLongJob、定时
CONSOLIDATE、Dreaming。`evolve` verb 经调度器提交，小数据量的 in-memory 场景实测 12 次
全部进入了快照，但这取决于任务是否在请求结束前完成，**不要依赖这一点**。

### 2.8 体积护栏与隐私

| 护栏 | 值 | 说明 |
|---|---|---|
| 单请求节点数 | 128 | span 与 event 合计；超出计入 `dropped`，只限制调用树，不限制 log 行数 |
| 字符串叶子 | 256 字符 | 超出部分替换为 `...[truncated]` |
| 容器深度 | 4 层 | 超出替换为 `<dict>` 这样的类型标记 |
| 容器宽度、总字节数 | **无限制** | 实测 1000 元素的列表被原样保留 |

隐私按输出侧分别约定：

- memory 快照只返回给本次请求的发起者，不做内容审查。
- log sink 走 `agent_memory.` 日志命名空间。现有 SensitiveDataFilter 只处理显式标记的敏感
  值，**不会**自动脱敏普通 dict 的字符串。
- 埋点纪律：data 只写计数、枚举、ID、分数，不写正文、prompt、LLM 响应和凭证。
- 已知缺口：检索降级时 `error` 字段带异常文本（已过 `safe_error_message`，长度限 200，
  但这不等于完成了内容审查）。

## 3. 使用方法

### 3.1 怎么选

| 场景 | 做法 |
|---|---|
| 排查某一次 HTTP 请求 | 请求里加 `"extensions": {"trace": "memory:debug"}`，不用改配置 |
| 服务端长期观测，不影响客户端 | 配置默认 `log` |
| CLI 或 MCP 上看调用树 | 只能用配置默认（见 [§3.5](#35-cli-与-mcp-的边界)） |
| 代码里直接调用 `MemoryAPI` | 自己 `begin_trace` / `end_trace`（见 [§3.6](#36-进程内-sdk-调用)） |

### 3.2 配置默认实现

> **注意**：HTTP / MCP / CLI 只把配置文件的 `memory_api` 段交给内核，写在顶层的 `trace`
> 会被**静默忽略**，启动时不报错，也不产出 trace。

```yaml
# trace.yaml
memory_api:
  trace:
    default:
      target: memory        # noop / memory / log / both
      params:
        level: info         # info / debug，可省略
```

```bash
scripts/run-server.sh --auth-mode dev trace.yaml
scripts/run-cli.sh --auth-mode dev --config trace.yaml add \
  --content '用户偏好美式咖啡' --scope '{"org":"t","user":"u"}'
```

`--auth-mode dev` 仅用于本地。SDK 经 `Config.from_dict` 装配时没有 `memory_api` 这一层，
直接写 `{"trace": {"default": "memory"}}`。

### 3.3 单次请求覆盖（HTTP）

在 payload **顶层**加 `extensions.trace`，取值 `<target>[:<level>]`：

```bash
curl -X POST http://127.0.0.1:8137/v1/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "coffee",
    "context": {"scope": {"org": "t", "user": "u", "agent": "a", "session": "s"}},
    "extensions": {"trace": "memory:debug"}
  }'
```

| 取值 | 效果 |
|---|---|
| `memory` | 调用树，info 级 |
| `memory:debug` | 调用树，含明细 |
| `log` / `both` | 写日志 / 两者都要 |
| `noop` | 本次请求关闭 |
| `:debug`、`:info` | 沿用默认实现，只改级别 |

有三个容易踩的地方：

1. **默认是 noop 时，`:debug` 什么也不会产生**，因为沿用的默认实现就是 noop。首次排查
   请写 `memory:debug`。
2. **显式写 target 不继承配置里的 params**。默认配了 `memory` + `debug` 时，请求写 `memory`
   得到的是 info 级；要保留默认实现请用 `:debug`。
3. `search` 的 `context.extensions` 是业务透传字段，与这里的顶层 `extensions` 无关。放错
   位置不会开启 trace。

非法取值返回 400：

```json
{"error": "ValidationError", "message": "trace level 只能是 info, debug，得到 'warn'", "retryable": false, "request_id": "c1e13865..."}
```

`extensions.trace` 被剥离后，其余键仍按原签名校验。`add` 同时带 `{"trace": "memory", "foo": "1"}`
会报 `unknown field for MemoryAPI.add: 'extensions'`。

### 3.4 读取结果

**`search`（dict 结果，加 `trace` 键）**，节选：

```json
{
  "items": ["..."], "trajectory": [], "errors": [],
  "trace": {
    "trace_id": "d558da78877044ac86a5779e04ad5d99",
    "spans": [{
      "component": "PipelineRetriever", "stage": "retrieve", "cost_ms": 2.845,
      "children": [
        {"component": "PipelineRetriever", "stage": "parse", "data": {"candidates": 1, "cost_ms": 0.233}},
        {"component": "PipelineRetriever", "stage": "recall",
         "data": {"candidates": 1, "cost_ms": 2.328, "channel": "keyword", "source": "keyword_l2", "pipeline": "recall_get_rank"}},
        {"component": "PipelineRetriever", "stage": "fuse", "data": {"candidates": 1, "cost_ms": 0.0, "strategy": "rrf", "rrf_k": "60"}},
        {"component": "PipelineRetriever", "stage": "rerank", "data": {"candidates": 1, "cost_ms": 0.104}},
        {"component": "PipelineRetriever", "stage": "disclose", "data": {"candidates": 1, "cost_ms": 0.019}}
      ]
    }]
  }
}
```

**`add`（list 结果，被包装）**：顶层变成 `{"result": [...], "trace": {...}}`。

**失败请求**（`top_k=0`）：

```json
{
  "error": "ValidationError", "message": "top_k must be positive, got 0", "retryable": false,
  "request_id": "0cdfd4a76952482f97bc5c645d08d633",
  "trace": {
    "trace_id": "0cdfd4a76952482f97bc5c645d08d633",
    "spans": [{"component": "PipelineRetriever", "stage": "retrieve", "cost_ms": 0.01, "error": "ValidationError"}]
  }
}
```

**log sink** 每个节点一行，格式 `trace[<id>] <level> <path> <data>`。用 trace_id 可以串起一个
请求。span 在退出时才写，所以父节点排在子节点之后：

```text
agent_memory.common.trace.trace_impl.log_trace INFO trace[1beee523...] info InMemoryEngine.write/InMemoryEngine.ingest {'units': 1, 'modality': 'text', 'path': 'raw'}
agent_memory.common.trace.trace_impl.log_trace INFO trace[1beee523...] info InMemoryEngine.write/InMemoryEngine.result {'path': 'raw', 'originals': 1, 'returned': 1}
agent_memory.common.trace.trace_impl.log_trace INFO trace[1beee523...] info InMemoryEngine.write {'cost_ms': 1.241}
```

**解读耗时时注意**：recall 各通道的 `cost_ms` 是同一个值（上例实际运行中 7 路都是 2.328），
它是整段召回的耗时，不是单通道耗时，不能相加。

### 3.5 CLI 与 MCP 的边界

二者都走 `invoke_api`，配置默认对它们生效，但请求级覆盖基本用不上：

| 入口 | 能否请求级覆盖 |
|---|---|
| HTTP | 所有 verb 都可以 |
| CLI | 参数由方法签名生成。`add` / `search` 没有 `--extensions`；`list` 有，但 `list` 没有埋点，快照为空 |
| MCP | `memory_add` / `memory_search` 没有顶层 `extensions`；`memory_list` 有，同样无埋点 |

### 3.6 进程内 SDK 调用

直接调用 `MemoryAPI` 不会自动开作用域，返回类型也不变。需要时自己包一段：

```python
from jiuwen_memory.api import begin_trace, end_trace

token = begin_trace("memory", level="debug")
try:
    result = api.search("coffee", context, security=security)
finally:
    snapshot = end_trace(token)   # {"trace_id": ..., "spans": [...]}，无节点时为 {}
```

`begin_trace` / `end_trace` 必须成对调用，并在同一个 context 内。

### 3.7 新增埋点

```python
from jiuwen_memory.common.trace import TraceLevel, trace_annotate, trace_event, traced


class MyBuilder:
    @traced("build")                     # 同步、异步方法都可以；component 缺省取实际类名
    def build(self, units):
        trace_event(type(self).__name__, "input", {"count": len(units)})
        for unit in units:
            trace_event(
                type(self).__name__, "item", {"id": unit.id}, level=TraceLevel.DEBUG
            )
        trace_annotate({"processed": len(units)})
```

埋点须遵守：

1. **data 不写正文、prompt、LLM 响应和凭证**，只写计数、枚举、ID、分数。
2. **data 的叶子值必须能被 JSON 序列化**。框架只做截断，不转换类型，`datetime`、枚举对象
   等会原样进入快照，到 `json.dumps` 时报错。HTTP 上这会让一个业务已成功的请求变成
   500（见 [§5.1](#51-已知风险)）。枚举请传 `.value`，时间请传字符串。
3. **`trace_event` 的参数会先求值**，无论 trace 是否开启。不要在参数里做昂贵计算。
4. 普通函数（没有 `self`）用 `@traced` 时必须显式传 `component`。

### 3.8 新增 sink

继承 `TraceCollector`，实现 `record`，用 `@TraceProducer.register("<名>")` 注册，并确保启动时
import 到。可声明 `buffers`（是否保留调用树）和 `enabled`。两条约束：

- 实现必须无状态，请求数据由框架管理。
- **`record` 自己要兜住异常**。框架不做隔离，sink 抛错会传播到业务调用（见 [§5.1](#51-已知风险)）。

### 3.9 当前埋点覆盖

| 节点 | 类型 | 主要字段 |
|---|---|---|
| `<Engine>.write` | span | `cost_ms` |
| `<Engine>.ingest` | event | `units`、`modality`、`path` |
| `<Engine>.result` | event | `path`、`originals`、`returned` |
| `OrchestratingEvolver.evolve` | span | `mode`、`inputs`、`created`、`updated`、`superseded`、`forgotten` |
| `ExtractorImpl.extract` | event | `inputs`、`response_chars`、`items`、`candidates` |
| `OrchestratingEvolver.dedup` | event | `dedup_impl`、`candidates`、各决策计数 |
| `OrchestratingEvolver.dedup_decision` | event（debug） | `candidate`、`decision`、`best`、`score`、`via` |
| `PipelineRetriever.retrieve` | span | `cost_ms` |
| `PipelineRetriever.<stage>` | event | `candidates`、`cost_ms`、`channel`、阶段 detail |

各 verb 实测能否产出快照：

| verb | 快照 |
|---|---|
| `add`、`add_async` | `write → [ingest, result]` |
| `batch_add` | 每条一个 `write` 根节点 |
| `search` | `retrieve → [parse, recall×N, fuse, recheck, rerank, resolve（条件阶段）, threshold, secondary_rank, disclose]` |
| `evolve` | `evolve → [dedup_decision…, dedup]`，不保证（见 [§2.7](#27-请求边界与后台任务)） |
| `get`、`list` | **无节点，响应不带 `trace` 键** |

`update`、`delete` 等治理和管理类 verb 的调用链上也没有埋点。**响应里没有 `trace` 键不代表
trace 没开**，先确认这个 verb 是否有埋点。

## 4. 对当前框架的影响

### 4.1 改动范围

| 层 | 文件 | 变化 |
|---|---|---|
| 新增组件 | `common/trace/` | 接口、作用域、调用树、四种实现 |
| 注册 | `common/bootstrap.py` | 触发 `trace_impl` 注册 |
| 配置 | `config/defaults.py` | 新增 `trace` 命名空间（默认 `noop`）与 `ROOT_PARAMS` 项 |
| 装配 | `api/memory_api_impl/assembly.py` | `configure_default(TraceProducer.dep(...))` |
| 公共导出 | `api/__init__.py` | 重导出 `begin_trace`、`end_trace`、`TraceLevel` 等 6 项 |
| 入口 | `core/api_contract.py`、`core/handler.py` | 作用域、覆盖键剥离、响应填充 |
| 入口 | `http_server/__main__.py`、`cli/client.py` | 错误体附加 trace |
| 埋点 | Engine（in_memory / cloud）、Evolver、Extractor、PipelineRetriever、RetrieveRun | 入口加 `@traced`，检索阶段经 `RetrieveRun.record` 同时记录 `trace_event` 与按需返回的 trajectory |

`OrchestratingEvolver.evolve` 从多个 `return` 改成先赋值、统一 `trace_annotate` 后返回，
Engine 的 middle 分支同样改为先取结果再记事件。逻辑不变，但这是这次改动中仅有的业务控制流调整。

### 4.2 不受影响的部分

- 业务方法签名、`MemoryUnit`、存储 schema 都没有变化。
- 鉴权与路由：`extensions.trace` 不参与二者。
- 架构分层不变：Access 只经 `jiuwen_memory.api` 取入口；内核依赖 `common.trace`，不反向依赖
  入口层。
- trace 不继承 `Plugin`，不进 `PluginType`，与 `audit`、`lock` 同属横切组件。

### 4.3 响应兼容性

默认 noop 下，响应结构与改动前完全一致。**一旦把默认实现改为 `memory` / `both`，有节点的
响应都会变形**，这是开启前必须评估的影响：

| 消费方 | 影响 |
|---|---|
| HTTP 客户端 | `add` / `add_async` 由列表变为 `{"result": [...], "trace": {...}}`，按列表解析的代码会出错；dict 结果多一个键 |
| CLI `--output text` | `add` 不再输出 `id  内容`，变成整段 JSON |
| CLI `--output quiet` | `add` **输出空行**，脚本取不到 id（实测；noop 下正常输出 id） |
| MCP | `memory_add` 标注返回 `list[dict]`，实际会收到 dict。是否通过 SDK 输出校验**尚未验证**（本环境未安装 `mcp`） |

因此建议：生产默认用 `noop` 或 `log`，`memory` 留给单次请求覆盖。

### 4.4 性能

单次埋点开销，每种 30 万次调用取平均（测量时机器上还在跑全量测试，只看量级）：

| 场景 | 开销 |
|---|---|
| 无作用域 | ≈ 0.11 µs |
| noop 作用域 | ≈ 0.11 µs |
| info 作用域下的 debug 埋点（被丢弃） | ≈ 0.15 µs |
| info 事件落树 | ≈ 1.6 µs |

`log` / `both` 还要加上同步写日志的开销。关闭时的开销可以忽略，但不是零：埋点参数（如
构造 dict、`len()`）仍会执行。

### 4.5 运行时状态

`_DEFAULT` 和 `_TARGETS` 是**进程级全局状态**。同一进程多次 `assemble` 时，后一次的配置覆盖
默认实现，多个配置不同的 MemoryAPI 实例无法各持一份 trace 默认值。请求开始时已取得的
collector 引用不受影响。测试之间需要重建默认值。

### 4.6 文档与规约

S07、`common/AGENTS.md`、`api/AGENTS.md` 已更新。[S02](../specs/S02-memory-api.md) 目前未说明顶层
`extensions.trace` 和响应包装，而这是对外可见的行为，**建议补上**。

## 5. 其他需要说明的内容

### 5.1 已知风险

下列行为都已实测确认。现有埋点的 data 都是基础类型，不会触发第一项，但新增埋点时需要注意。

| 风险 | 触发条件与后果 | 建议 |
|---|---|---|
| 非 JSON 叶子导致 500 | data 含 `datetime` 等。业务已完成写入，响应序列化时才失败，调用方看到 500 | 对快照做 `to_jsonable`，或在 `_bound_value` 兜底转字符串 |
| sink 异常破坏业务 | `record` 抛错会传到业务调用；在 span 退出时抛错，还会**覆盖原业务异常**（实测调用方收到 `RuntimeError`，原 `ValueError` 只留在 `__context__`） | 框架对 `record` 做 try/except 并记日志 |
| 响应变形 | 默认实现设为 memory/both，见 [§4.3](#43-响应兼容性) | 生产不用 memory 作默认 |
| 配置静默失效 | 顶层 `trace` 被忽略，无任何提示 | 用户侧写 `memory_api.trace`；可考虑启动时告警 |
| 客户端可开服务端日志 | 已认证调用方可用 `log` / `both` 写服务端日志，且日志量不受 128 节点限制 | 增加 target 白名单、权限或采样 |
| 无总体积预算 | 容器宽度不限，单个埋点传大列表会撑大响应 | 增加宽度或总字节限制 |

### 5.2 排障速查

| 现象 | 优先检查 |
|---|---|
| 配了 `memory`，响应没有 `trace` | 配置是否写在 `memory_api` 段下；该 verb 是否有埋点（见 [§3.9](#39-当前埋点覆盖)） |
| 请求带 `:debug` 没反应 | 默认实现是否为 noop，改用 `memory:debug` |
| 没有 `dedup_decision` 等明细 | 级别是否为 debug；显式 target 不继承配置的 level |
| 报 `unknown field ... 'extensions'` | `extensions` 里除 `trace` 外是否还有其他键 |
| 开了 `log` 但日志里没有 | logger 级别是否高于 INFO |
| 出现 `dropped` | 超过 128 个节点，树不完整，可改用 `log` 查看全部节点 |
| 想看 middle / Dreaming 的后台过程 | 目前不支持，请求结束后的事件会被丢弃 |
| 线程池内的埋点丢失 | `ThreadPoolExecutor.submit` 不传播 context |

### 5.3 验证

```bash
pytest tests/unit/common/trace/test_trace.py \
       tests/unit/api/test_trace_pipeline.py \
       tests/unit/jiuwen_memory_entry/test_api_contract_trace.py
```

| 项 | 结果（2026-11-02） |
|---|---|
| trace 专项 | 53 例全部通过 |
| `tests/unit` 回归（`--ignore=tests/unit/multimodal`） | 工作区 3122 通过；干净 HEAD 3069 通过，增量正好是 53 |
| 既有失败 | 两边都是 12 failed + 52 errors，失败集合**逐条一致**，无新增 |
| `ruff check`（trace 组件与测试） | 通过 |
| 手工端到端 | 真实 HTTP server + curl；CLI 进程内 |

**未覆盖**：MCP 端到端（未装 `mcp`）；CloudEngine 真实后端；依赖真实 LLM 的抽取和去重判定；
并发压力下的 trace。

### 5.4 后续方向

优先级从高到低：

1. 隔离 sink 异常，并保证快照可 JSON 序列化（改动小，能消除 §5.1 中最危险的两项）。
2. 补 S02 对外说明；核实 MCP 上的响应包装是否兼容。
3. 为请求级覆盖增加 target 白名单或权限控制，补容器宽度限制。
4. 补埋点：`Engine.recall`、IndexBuilder、Ingestor、LLM 调用耗时；召回的单通道耗时。
5. 按 job 为单位追踪后台任务（独立 trace id + log sink），不复用请求作用域。
6. 把含正文的 `[trace/xxx]` 调试日志迁移到本组件。其中部分是 INFO 级别，不受 noop 开关控制。

完整的遗留列表见 [F09 · 已知遗留](../features/common/F09-trace-component.md#已知遗留)。
