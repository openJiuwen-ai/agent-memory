# F03 — batch_write / batch_write_async 批量写入接口设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-04 |
| 影响范围 | src/api/，src/control/，bootstrap/core/handler.py，docs/specs/S02-memory-api.md，docs/specs/S03-control.md |
| 测试基线 | `tests/unit/api/test_batch_write.py`、`test_batch_handler.py`、`tests/unit/control/test_cloud_engine.py` |
| Refs | — |

> 本文归档 `batch_write` / `batch_write_async` 的第一版设计。目标是提供批量提交入口，
> 但不改变单条 `write` 的语义、权限边界、审计粒度、`infer=true` 上下文语义和
> `procedural=true` 过程记忆语义。

---

## 背景

当前 `MemoryAPI.write` / `write_async` 一次只表达一条内容写入。调用方做批量导入、
会话回放、离线迁移或多段对话同步时，只能在外层自行循环调用：

```python
for item in items:
    api.write(...)
```

这会带来三个问题：

1. **调用方重复实现错误处理和结果对齐**：每条输入可能产生多条 `MemoryUnit`，也可能在
   `infer=true` 下因 dedup 返回空。外层循环需要自己维护 input index、成功/失败、
   空结果和异常归因。
2. **同步/异步入口语义容易漂移**：同步场景用 `write` 循环，异步场景可能直接
   `asyncio.gather(write_async(...))`，但同一 stream 的 `infer=true` 写入依赖最近
   `/messages/` 上下文与去重召回，盲目并发会破坏顺序语义。
3. **后续优化缺少稳定接口**：真正的批量优化可能发生在 Ingestor、Classifier、
   IndexBuilder、KVStore 或 Evolver 层。没有批量 API 时，只能在调用方侧并发，无法由内核
   根据 scope、stream、metadata 和后端能力做安全调度。

因此新增批量写入接口，但第一版必须把“批量提交”和“并发执行”分开：batch 是调用语义，
不是默认并发承诺。

## 决策

### 1. 新增 BatchWriteItem / BatchWriteOutcome / BatchWriteResult

新增公共数据结构承载逐项输入与逐项结果。类型落在 `control/types.py`，由 `api` 和
`control` 共同引用，避免用松散 dict 穿过层边界。

批量接口采用“API 顶层默认参数 + item override”模型：多个 item 之间高度重复的字段沿用
`write` 的参数风格放在 `batch_write(...)` 形参上，每个 `BatchWriteItem` 只写差异。归一化后
再按单条 `write` 语义执行。这样避免额外引入 `BatchWriteOptions` 这种单条 write 不存在的封装。

因此 `BatchWriteItem` 表达的是“这一条 message 自己的字段”，而 `batch_write(...)` 的顶层参数
表达的是“这一批 message 共享的默认写入字段”。两者不是两套语义：item 缺省字段由顶层参数补齐，
补齐后的每条 item 都必须能等价转换为一次单条 `write(content, scope, source, *,
tags, metadata, occurred_at)` 调用。

```python
@dataclass
class BatchWriteItem:
    content: str
    scope: Scope | None = None
    source: Modality | None = None
    assets: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    occurred_at: datetime | None = None
    stream_id: str = ""
    sequence: int | None = None
    idempotency_key: str = ""


@dataclass
class BatchWriteOutcome:
    index: int
    item: BatchWriteItem
    units: list[MemoryUnit] = field(default_factory=list)
    error: str = ""
    error_type: str = ""


@dataclass
class BatchWriteResult:
    outcomes: list[BatchWriteOutcome]
```

字段语义：

| 字段 | 语义 | 外提判断 |
|---|---|---|
| `content` | 本条写入的文本/结构化投影，进入 `RawPayload.data`，再成为 `MemoryUnit.content` | 不外提，必须逐 item |
| `scope` | 目标记忆范围，参与权限、space 隔离和存储命名空间 | 常重复，可由 `batch_write(..., scope=...)` 提供默认值 |
| `source` | 来源模态/类型：text、image、audio、video、code、document | 常重复，可由 `batch_write(..., source=...)` 提供默认值 |
| `assets` | 本条原始资产引用列表，例如图片 URL、音频路径、PDF 对象存储地址 | 通常逐 item，不默认外提 |
| `tags` | 标签，参与过滤、权限上下文和治理 | 可由顶层公共 tags 与 item tags 合并 |
| `metadata` | 调用级/记忆级元数据，包含 `infer`、`procedural`、`memory_type`、`message_type`、动态 prompt key 等 | 可由顶层公共 metadata 与 item metadata 合并 |
| `occurred_at` | 事件发生时间，进入 `RawPayload.occurred_at` / `Temporal.t_event` | 可由 `batch_write(..., occurred_at=...)` 提供默认值 |
| `stream_id` | 一组必须保序的写入流，例如会话、回放或导入任务 | 常重复，可由 `batch_write(..., stream_id=...)` 提供默认值 |
| `sequence` | stream 内顺序号 | 不外提，必须逐 item |
| `idempotency_key` | 单条写入幂等键 | 不外提，必须逐 item |

`source` 与 `assets` 的边界必须保持清楚：`source` 是内容来自什么模态，`assets` 是原始材料的
引用，`content` 是从原始材料规约出的可检索文本/结构投影。三者在 `MemoryUnit.segments` 内
按段绑定。例如图片输入可表示为 `source=IMAGE`、`assets=["s3://.../whiteboard.png"]`、
`content="图片中是一张白板，上面写着 Q3 roadmap"`。

归一化规则：

- `scope`：item 有值用 item，否则用顶层 `scope`；最终必须非空。
- `source`：item 有值用 item，否则用顶层 `source`。
- `tags`：`batch_write(..., tags=...) + item.tags`，允许去重但必须保持首见顺序。
- `metadata`：`{**batch_write(..., metadata=...), **item.metadata}`，item 覆盖顶层默认值。
- `occurred_at`：item 有值用 item，否则用顶层 `occurred_at`。
- `stream_id`：item 有值用 item，否则用顶层 `stream_id`。
- `sequence` / `idempotency_key`：只接受 item 级值，不从顶层参数继承。

例如：

```python
api.batch_write(
    [
        BatchWriteItem(content="Alice likes tea", sequence=1),
        BatchWriteItem(
            content="白板上写着 Q3 roadmap",
            source=Modality.IMAGE,
            assets=["s3://bucket/whiteboard-q3.png"],
            sequence=2,
        ),
    ],
    Scope(org="acme", space="prod", user="alice"),
    identity=Scope(org="acme", space="prod", user="alice"),
    metadata={"infer": "true"},
    stream_id="session-1",
)
```

归一化后等价于按顺序执行两次单条 `write`：第一条继承顶层 `scope/source/metadata/stream_id`；
第二条继承顶层 `scope/metadata/stream_id`，但使用 item 自己的 `source=IMAGE` 和 `assets`。

`outcomes` 必须与输入顺序一一对应。单条成功但返回空列表仍是成功结果，表示这条写入被
dedup 判为 UPDATE/NOOP 或抽取无新增，不等于失败。`BatchWriteOutcome.item` 建议保存归一化后的
item，方便调用方确认实际执行参数。

### 2. API 暴露同步 / 异步两个入口，语义与 write 保持一致

新增：

```python
def batch_write(
    self,
    items: list[BatchWriteItem],
    scope: Scope | None = None,
    source: Modality = Modality.TEXT,
    *,
    identity: Scope,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    stream_id: str = "",
    continue_on_error: bool = True,
) -> BatchWriteResult

async def batch_write_async(
    self,
    items: list[BatchWriteItem],
    scope: Scope | None = None,
    source: Modality = Modality.TEXT,
    *,
    identity: Scope,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    stream_id: str = "",
    continue_on_error: bool = True,
) -> BatchWriteResult
```

同步入口只桥接异步入口，不维护第二套实现：

```python
def batch_write(...):
    return asyncio.run(self.batch_write_async(...))
```

每个归一化 item 的字段语义与 `write` 完全一致。顶层 `scope/source/tags/metadata/occurred_at`
表达批量默认值；item 仍可覆盖，支持同一批导入多个 scope。`identity` 仍是整个 batch 的
调用方身份，不进入 item。

### 3. 鉴权、空间状态和审计按 item 粒度执行

批量接口不能把整批请求做成一次粗粒度 WRITE 鉴权。每个归一化后的 `BatchWriteItem` 都必须
复用单条 write 的边界逻辑：

```text
归一化顶层默认参数 + item
→ 校验 metadata
→ 构造 write PermissionContext
→ PermissionManager.check(identity, item.scope, WRITE, context)
→ _ensure_space_writable(item.scope)
→ 委托 Engine 写入
→ 记录 item 级 audit
```

这样不同 scope、不同 `memory_type`、不同 tags 或不同 pipeline 权限可以独立判定。批量请求
本身可以额外记录一条 summary audit，但不能替代 item 级审计。若顶层 `scope` 与 item
override 同时存在，鉴权必须以归一化后的最终 `item.scope` 为准。

### 4. 第一版默认串行保序，不默认 asyncio.gather

`batch_write_async` 虽然是协程入口，但第一版内部默认按输入顺序逐条 `await engine.write(...)`。

原因：

- `infer=true` 会使用最近 `/messages/` 上下文；同一 stream 内并发会让后写入先被抽取，
  或让前一条写入尚未进入上下文窗口。
- dedup、UPDATE、SUPERSEDE 依赖当前真源状态；同一事实流并发可能产生重复 ADD 或错误 NOOP。
- 当前 engine 的 Ingestor、Classifier、KV、IndexBuilder、Evolver 多数仍是同步实现，
  `write_async` 是协程入口，不等价于内部非阻塞 IO。

后续若要并发，只能在明确排序边界后开启：不同 `(scope, stream_id)` 之间可以并发；同一
`(scope, stream_id)` 内必须按 `sequence > occurred_at > 输入顺序` 串行提交。

### 5. `stream_id` / `sequence` / `idempotency_key` 先进入数据结构

第一版可以先不实现完整幂等存储，但数据结构应预留三个字段：

| 字段 | 语义 |
|---|---|
| `stream_id` | 一组必须保序的写入流，例如一次会话、一个导入任务、一个消息流 |
| `sequence` | stream 内单调序号；存在时优先于 `occurred_at` 决定提交顺序 |
| `idempotency_key` | 调用方提供的幂等键；后续用于重复提交去重和断点续跑 |

第一版执行策略：

- 不传 `stream_id` 时，整批视为一个默认 stream，按输入顺序执行。
- 顶层 `stream_id` 为 batch 默认 stream；item 可覆盖。
- 传了 `stream_id` 但未传 `sequence` 时，仍按输入顺序执行。
- 传了 `sequence` 时，接口可以先校验同一 `(scope, stream_id)` 内没有重复 sequence；
  是否自动排序需要单独开关，默认不重排输入，避免返回顺序和执行顺序混淆。

### 6. partial success 是默认语义

`continue_on_error=True` 时，一条失败不阻断后续 item。失败项写入 `BatchWriteOutcome.error`
和 `error_type`，成功项正常返回 `units`。最终不抛整批异常，除非是批量请求本身非法
（例如 items 为空、顶层 defaults 非法或顶层 metadata 含系统保留 key）；单个 item 的结构和
metadata 错误也按 outcome 归集。

`continue_on_error=False` 时，遇到第一条失败即停止后续执行；已执行项保留 outcome，未执行项
返回 `error_type="Skipped"` 或直接不生成 outcome 二选一。建议选择“所有输入都有 outcome”，
保持结果与输入一一对应。

### 7. Engine 层新增 batch_write，但第一版可以是安全循环

为了维持 API 层薄封装，控制层应新增：

```python
async def batch_write(self, items: list[BatchWriteItem]) -> BatchWriteResult
```

第一版 engine 实现可以只是保序循环调用现有 `write`。这样公开契约先稳定下来，后续再把优化
下沉到 Ingestor 批处理、KV 批量写、IndexBuilder 批量建索引或 Evolver 批量抽取，不需要再改
API 入口。

API 层仍负责归一化、鉴权、空间状态和审计；Engine 只接收已鉴权、已归一化的 target item，
不接收 identity。

### 8. HTTP handler 增加 `/v1/batch_add`

HTTP 面建议新增独立 verb，而不是让 `/v1/add` 同时接受 object/list 两种 payload：

```json
{
  "defaults": {
    "tenant_id": "acme",
    "space": "prod",
    "scope": "alice",
    "source": "text",
    "metadata": {"infer": "true"},
    "stream_id": "s1"
  },
  "items": [
    {
      "content": "Alice likes tea",
      "sequence": 1,
      "idempotency_key": "s1-1"
    },
    {
      "content": "白板上写着 Q3 roadmap",
      "source": "image",
      "assets": ["s3://bucket/whiteboard-q3.png"],
      "sequence": 2,
      "idempotency_key": "s1-2"
    }
  ],
  "continue_on_error": true
}
```

handler 负责把 `defaults` 解析为 `batch_write` 顶层默认参数，把每个 item 解析为
`BatchWriteItem`，并沿用现有 actor override 规则生成统一 `identity`。item 级范围覆盖使用
`target_scope` 嵌套对象，按 `tenant_id` / `space` / `scope` / `agent` / `session` 覆盖 defaults。
响应固定为 HTTP 200 的 `{ok, op: "batch_add", outcomes}`：每项包含原始 `input`、归一化
`item`、`items`（MemoryUnit view）、`ok`、`error` 和 `error_type`；部分失败不使用 HTTP 207。
如果未来需要每个 item 不同 actor，应另开管理接口，不在普通 batch 写入里混用。

## 拒绝的方案

### 方案 A：调用方自己 `asyncio.gather(write_async(...))`

拒绝原因：

- 它只解决提交并发，不解决结果对齐、partial success、审计聚合和幂等。
- 同一 stream 的 `infer=true` 会破坏上下文顺序和去重判定。
- 并发策略应该由内核根据 scope/stream/后端能力控制，不能交给所有调用方各自猜。

### 方案 B：batch_write 返回扁平 `list[MemoryUnit]`

拒绝原因：

- 一条输入可能产生 0/N 条输出，扁平列表无法可靠反查哪条输入产生了哪些 unit。
- partial failure 时无法表达某条失败、某条成功但 dedup 为空。
- 调用方做导入、回放、断点续跑时需要 input index 和幂等键维度。

### 方案 C：整批一次鉴权、一次审计

拒绝原因：

- batch 可以包含不同 scope、memory_type、tags 和 pipeline，粗粒度鉴权会绕过细粒度权限路由。
- 审计需要能追溯每条记忆写入的 target scope 和权限上下文。

### 方案 D：第一版直接做真正批量 KV/索引/LLM 优化

拒绝原因：

- 当前单条 write 已有 `infer`、`procedural`、pipeline routing、CloudEngine metadata 固化、
  Space 策略、审计等分支。直接下沉优化容易改变单条语义。
- 先稳定批量接口和结果模型，再分阶段优化内部执行，可以降低回归面。

## 验证

第一版实现应至少覆盖：

1. `batch_write` 是 `batch_write_async` 的同步桥接。
2. `outcomes` 与输入顺序一一对应。
3. 顶层默认参数与 item override 按归一化规则合并。
4. `source` 只表示模态，`assets` 保留原始资产引用，二者按段进入 `MemoryUnit.segments`。
5. 成功项返回对应 `units`，`infer=true` dedup 空结果仍算成功。
6. `continue_on_error=True` 时单项失败不阻断后续项。
7. `continue_on_error=False` 时失败后后续项标记 skipped。
8. 每个 item 独立执行 WRITE 鉴权，跨 scope 无授权项不能借整批通过。
9. `scope.require_space=true` 时缺少 space 的 item 被拒绝，不影响其他合法 item。
10. `metadata` 保留 key 和非标量校验复用单条 write。
11. CloudEngine 下 `message_type` routing 逐 item 生效，并固化 `metadata["pipeline"]`。
12. 同一 stream 的多条 `infer=true` 按输入顺序执行，后项可看到前项 `/messages/` 上下文。
13. HTTP `/v1/batch_add` malformed item 返回结构化错误，不产生 HTTP 500。

## 已知遗留

1. **真正并发调度暂不做**：第一版默认串行保序。后续可按 `(scope, stream_id)` 分组并发，
   但同组内必须保序。
2. **幂等存储暂不做**：`idempotency_key` 先进入数据结构；后续需要在 KV 或单独 manifest
   中记录 `(scope, stream_id, idempotency_key) -> outcome`。
3. **批量 KV/IndexBuilder 优化暂不做**：第一版 engine batch 可以循环调用单条 `write`。
   后续再评估 `insert_many`、`build_many` 或 extractor 批量 prompt。
4. **跨 item 事务暂不做**：批量写入不是 all-or-nothing 事务。需要事务语义的导入任务应在
   更高层维护 staging 和补偿。
5. **HTTP actor 逐项变化暂不做**：普通 batch 共享同一个 `identity`。多 actor 批处理属于
   管理面导入能力，应另行设计。
