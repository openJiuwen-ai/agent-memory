# Temporal 新增 t_message —— 消息时间与事件时间分离

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-10 |
| 影响范围 | `src/common/type_def/memory.py`、`src/common/type_def/memory_codec.py`、`src/common/type_def/memory_filter.py`、`src/ingest/ingestor_impl/simple_ingestor.py`、`src/construction/extractor_impl/keyword_extractor.py`、`src/construction/extractor_impl/llm_extractor.py`、`src/construction/extractor_impl/dynamic_llm_extractor.py`、`src/construction/abstractor_impl/llm_abstractor.py`、`src/retrieval/retriever_impl/predicate_builder.py`、`src/retrieval/retriever_impl/unit_reader.py` |
| 测试基线 | 见"验证" |
| Refs | — |

## 背景

`Temporal` 原有 4 个时间字段：`t_event` / `t_ingest` / `t_valid` / `t_invalid`。其中 `t_event` 的语义在不同写入路径下**不一致**，存在语义混淆：

- **原始消息单元**（Ingestor 产出）：`simple_ingestor.py` 把 `payload.occurred_at`（消息/对话发生时间）写入 `t_event`——此时 `t_event` 的语义是"消息发生时间"。
- **派生单元**（Extractor 抽取）：`keyword_extractor.py` / `llm_extractor.py` 从内容中提取事件时间写入 `t_event`——此时 `t_event` 的语义是"内容描述的事件发生时间"。

两者是不同语义角色。以"14号下午去医院，20号去复查"（对话发生在 15 号 10:00）为例：

- 对话/消息发生时间：15 号 10:00（对话什么时候发生的）
- 事件发生时间：14 号下午、20 号（内容描述的事件什么时候发生的）

混淆导致：检索侧 `predicate_builder` / `in_event_window` 按 `t_event` 做时间窗过滤时，无法区分"用户问昨天聊了什么"（应按消息时间过滤）和"用户问上周发生了什么事件"（应按事件时间过滤）——两者都打到同一个 `t_event` 字段，语义不可分。

## 决策

### 1. 新增 `t_message` 字段，`t_event` 语义净化

`Temporal` 新增 `t_message: datetime | None`，承接"消息/对话发生时间"语义。`t_event` 语义净化为"仅事件时间"（内容描述的事件发生时间）。

```python
@dataclass
class Temporal:
    """双时间模型：消息/事件/摄入/有效期，支持时间点回溯（as_of）。"""
    t_event: datetime | None = None    # 事件时间：内容描述的事件发生时间
    t_ingest: datetime | None = None   # 摄入时间：系统记录时间
    t_valid: datetime | None = None   # 生效时间
    t_invalid: datetime | None = None  # 失效时间
    t_message: datetime | None = None  # 消息时间：消息/对话发生时间（新增）
```

三个时间的语义边界：

| 字段 | 语义 | 举例 | 写入方 |
|---|---|---|---|
| `t_message` | 消息/对话什么时候发生的 | 15 号 10:00 | Ingestor（从 `payload.occurred_at`） |
| `t_event` | 内容描述的事件什么时候发生的 | 14 号下午 | Extractor（从内容提取） |
| `t_ingest` | 系统什么时候记录的 | 15 号 10:00:05 | Ingestor / Engine（UTC now） |

### 2. `t_event` 保持单值，不改 list

一条记忆内容含多个事件时间（"14号去医院，20号复查"）时，**优先靠 Extractor 拆分**成多条派生 unit，各自 `t_event` = 14号 / 20号。这与 architecture §9.1 "信息提取 → 从原始数据抽取事实/事件"的设计意图一致——Extractor 的职责就是把多事件内容拆成单事件派生。

确实不可拆分的聚合单元（如 consolidate 后的摘要）需要多事件时间时，走 `metadata["t_events"]: list[str]`（ISO 格式），不侵入结构化字段——过滤仍用主 `t_event`。

### 3. 多消息时间同理走 metadata

consolidate 合并多 source 时会有多个消息时间。合并后的摘要 unit 主 `t_message` 取一个（如最早或最近），其余进 `metadata["t_messages"]: list[str]`（ISO 格式）。内核过滤逻辑不读 metadata 里的 list，业务层自行处理。

### 4. 写入侧适配

| 位置 | 改动 |
|---|---|
| `simple_ingestor.py` | `t_message=payload.occurred_at`；`t_event` 不再从 `occurred_at` 取（原始消息不描述事件，改为 `None`） |
| `keyword_extractor.py` | 派生 unit 继承 `t_message=source.temporal.t_message`；`t_event` 不再继承 source（keyword_extractor 不从内容提取事件时间，`t_event` 改为 `None`） |
| `llm_extractor.py` / `dynamic_llm_extractor.py` | 派生 unit 继承 `t_message=source.temporal.t_message`；`t_event=_parse_event_date(c.event_date)`——提取不到就是 `None`，不再回退 `source.temporal.t_event`（改动后 source 的 `t_event` 对原始消息单元已是 `None`，回退无意义） |
| `llm_abstractor.py` | 合成 unit 继承 primary source 的 `t_message` |

### 5. 编解码兼容

`memory_codec.py` 的 temporal 数组从 4 元素扩展到 5 元素（加 `t_message`）。按 codec 自身版本策略（[memory_codec.py:31-33](file:///d:/Codes/0725_1_agentmemory/agent-memory/src/common/type_def/memory_codec.py#L31-L33)），「加字段」属于兼容演进，**不升 `_v`**——`loads` 对老数据（4 元素 temporal 数组）缺省补 `t_message=None` 即可。升版本留给"改字段含义/结构"的破坏性变更。

### 6. t_event 语义净化的老数据兼容

老数据 `t_event` 存的是对话时间（从 `occurred_at` 来的），codec 加字段后 `t_message` 为 `None`，`t_event` 保留老值——老数据 `t_event` 语义"不纯"但不会丢，后续演进可逐步补 `t_message`。

### 7. 检索侧过滤（可选 follow-up）

当前 `predicate_builder` / `in_event_window` 只过滤 `t_event`。新增 `t_message` 后，可选支持按消息时间过滤（如"昨天聊了什么"按 `t_message` 过滤）。本次落地优先把字段+写入侧改对；过滤扩展作为 follow-up。

## 拒绝的方案

### A. `t_event` 改为 `list[datetime]`

- **索引层不原生支持**：Milvus / ES / pgvector 对标量字段的 range 查询是原生能力，list 字段的 any-of 语义（任一事件时间落在窗内）多数 store 不原生支持，需要应用层二次过滤，违背"生产过滤必须先于 top-k"铁律。
- **codec 破坏性变更**：temporal 数组从 `[scalar, scalar, scalar, scalar]` 变成 `[list, scalar, scalar, scalar]`，老数据不兼容。
- **违背 Extractor 拆分粒度设计意图**：Extractor 职责就是把多事件内容拆成单事件派生 unit，让一条 unit 承载多事件等于把 Extractor 的活推给了检索过滤。


## 验证

- `Temporal` 新增字段后 `memory_codec.py` round-trip（dumps → loads）正确，老数据（4 元素）loads 缺省补 `t_message=None`。
- `simple_ingestor.py` 写入的原始消息单元 `t_message` = `payload.occurred_at`，`t_event` = `None`。
- Extractor 派生 unit `t_message` 继承自 source unit。
- `RESERVED_METADATA_KEYS` 包含 `"t_message"`。
- 现有 `predicate_builder` / `in_event_window` 对 `t_event` 的过滤逻辑不变（`t_message` 过滤为 follow-up）。

## 已知遗留

- 检索侧 `t_message` 范围过滤未落地（`predicate_builder` / `in_event_window` 当前只过滤 `t_event`），作为 follow-up 按需加。
- 老数据 `t_event` 语义"不纯"（存的是对话时间），后续演进可逐步补 `t_message` 并清洗 `t_event`。
- `t_event` 语义净化后，原始消息单元的 `t_event` 变为 `None`；若有下游消费方依赖原始消息单元的 `t_event` 有值，需同步适配。
  - **已缓解**：`t_event=None` 派生原先会被事件窗下推
    `t_event GTE/LT` 按缺失字段排他，对含时间词 query 系统性空召回。现索引投影
    恒写 `t_event`（None → 哨兵 `T_EVENT_UNKNOWN=0`），谓词改
    `OR(AND(GTE from, LT to), EQ 0)` 放行未知时间 unit，`memory_filter._field_value`
    同步把真源 None 投影为 `0` 使后置复核不砍候选。详见 S04 §过滤表达式 / F03 §8。
  - **属性问止血（同 commit）**：`time_parse` 识别属性问关键词（多大/几岁/爱好/
    是谁/住址/名字/生日/年龄…）后清空 `time_from/to`，即便 query 含「今年/昨天」
    也不下推事件窗——属性问本就不是事件时间检索，避免误下推杀 None 派生。
- **`metadata["t_events"]` / `metadata["t_messages"]` 不参与检索过滤**：当前索引层（Milvus/ES/pgvector）不原生支持"数组任一元素落在 range 内"的查询，召回后置过滤（`in_event_window`）只读结构化字段 `t_event`，不读 metadata。因此聚合单元的次要事件/消息时间**无法用于时间窗过滤**——若业务有"按多事件时间过滤"的刚需，正确答案是拆分成多条 unit（每条带自己的 `t_event`），而非依赖 metadata 兜底。metadata 方案仅保证信息不丢（审计/展示可读 + 未来索引扩展有数据基础），不提供过滤能力。
