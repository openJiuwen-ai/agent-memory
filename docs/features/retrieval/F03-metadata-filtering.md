# F03 — 元数据过滤设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | src/common/type_def/filter.py，src/common/type_def/memory.py，src/api/memory_api_impl/local_memory_api.py，src/construction/index_builder_impl/，src/retrieval/types.py，src/retrieval/retriever_impl/，src/storage/{vector,fulltext,fusion}.py，docs/specs/S02-memory-api.md，docs/specs/S04-retrieval.md，docs/specs/S06-storage.md |
| 测试基线 | `PYTHONPATH=src uv run --no-sync pytest -q -m unit` 通过；Milvus/Elasticsearch/PostgreSQL 集成测试由真实后端环境启用；本特性变更的 Python 文件通过 `ruff check` |
| Refs | — |

> 本文归档 agent-memory 的 metadata 过滤设计。目标是让 `filters` 支持类似 DSL 的树形逻辑表达，同时保持 scope 过滤仍由现有 scope 链路负责，避免把租户、用户、agent、session 隔离语义混进 metadata filter。

---

## 背景

落地前的检索过滤模型是扁平结构：

- `RetrievalQuery.filters: list[FilterClause]`
- 多个 `FilterClause` 之间隐式为 `AND`
- `FilterOp` 已支持 `EQ` / `NE` / `IN` / `NOT_IN` / `GT` / `GTE` / `LT` / `LTE` / `CONTAINS`
- `QueryParser` 将 filters 透传到 `ParsedQuery.scalar_filters`
- `UnitReader.matches_filters` 在点读真源后按扁平 `AND` 做后置复核

这个模型适合简单等值和范围过滤，但不能表达：

- `(project = "alpha" OR project = "beta") AND priority >= 8`
- `NOT status = "archived"`
- 多层 `AND` / `OR` / `NOT` 组合

同时，现有代码已经把 scope 作为独立入参向下传递：

- `MemoryAPI.search(..., context=Context(scope=...))`
- `Retriever.retrieve(scope, query)`
- `Store.search(scope, query)`

因此本设计只扩展 `filters` 的业务谓词表达能力，不改变 scope 的职责。

## 决策

### 1. `FilterClause` 继续只表示叶子条件

`FilterClause` 不应通过 `value: FilterClause` 或 `value: list[FilterClause]` 表达逻辑树。

原因是 `FilterClause` 的职责是一个字段上的原子谓词：

```python
@dataclass
class FilterClause:
    field: str
    op: FilterOp = FilterOp.EQ
    value: Any = None
```

如果把子表达式塞进 `value`，会导致同一个字段同时承担两种语义：

- 普通比较值，例如 `"active"`、`8`、`["alpha", "beta"]`
- 子过滤表达式，例如 `FilterClause(...)` 或 `FilterGroup(...)`

这会让校验、序列化、Store 下推和 UnitReader 复核都必须反复判断 `value` 到底是业务值还是表达式节点，边界不清晰。

### 2. 新增 `FilterGroup` 表示逻辑节点

树形过滤表达式由叶子节点和逻辑节点组成：

```python
FilterExpr = FilterClause | FilterGroup

class FilterLogic(str, Enum):
    AND = "and"
    OR = "or"
    NOT = "not"

@dataclass
class FilterGroup:
    logic: FilterLogic
    children: list[FilterExpr]
```

语义约束：

- `AND`：所有 children 都满足
- `OR`：至少一个 child 满足
- `NOT`：只接受一个 child
- 空 `AND` / `OR` 非法
- `FilterClause` 仍然是唯一的字段比较节点

这样 `FilterClause` 和 `FilterGroup` 分工明确：

| 类型 | 职责 |
|---|---|
| `FilterClause` | 表达字段、算子、值 |
| `FilterGroup` | 表达 `AND` / `OR` / `NOT` 逻辑关系 |
| `FilterExpr` | 统一承载叶子或逻辑树 |

### 3. `filters` 从扁平列表升级为过滤表达式

目标内部契约：

```python
@dataclass
class RetrievalQuery:
    text: str = ""
    filters: FilterExpr | None = None
    ...
```

兼容规则：

- 旧的 `list[FilterClause]` 继续接受
- 进入检索链路前统一规范化为 `FilterGroup(FilterLogic.AND, clauses)`
- 单个 `FilterClause` 可以直接作为 `filters`
- `None` 表示没有 metadata 过滤条件

因此，旧写法：

```python
filters=[
    FilterClause("project", FilterOp.EQ, "alpha"),
    FilterClause("priority", FilterOp.GTE, 8),
]
```

规范化后等价于：

```python
FilterGroup(
    FilterLogic.AND,
    [
        FilterClause("project", FilterOp.EQ, "alpha"),
        FilterClause("priority", FilterOp.GTE, 8),
    ],
)
```

### 4. API 边界可以接受 dict DSL，但内核只消费结构化类型

为了便于 HTTP / SDK 调用，API 边界可以接受类似 DSL 的 dict：

```python
filters = {
    "AND": [
        {"project": {"in": ["alpha", "beta"]}},
        {"priority": {"gte": 8}},
        {
            "OR": [
                {"assignee": "alice"},
                {"assignee": "bob"},
            ]
        },
        {
            "NOT": {"status": "archived"}
        },
    ]
}
```

规范化规则：

- `{field: value}` 等价于 `FilterClause(field, EQ, value)`
- `{field: {"op": value}}` 等价于指定算子的 `FilterClause`
- 顶层多个 field 或逻辑键默认组合为 `AND`
- `AND` / `OR` 的值必须是非空 list
- `NOT` 只接受一个表达式
- 未知算子、空字段名、错误 value 类型在 API 边界报错

dict DSL 只存在于 API / SDK 兼容边界。进入 `QueryParser`、Store 和 `UnitReader` 前，必须先转换成 `FilterExpr`。

### 5. filters 不处理 scope 逻辑

`filters` 只表达 metadata、标签、生命周期、时间标量等业务谓词，不表达 scope。

scope 继续由现有链路处理：

```python
Retriever.retrieve(scope, query)
Store.search(scope, query)
```

过滤表达式中不允许出现 scope 字段，例如：

- `scope`
- `tenant`
- `org`
- `user`
- `agent`
- `session`
- `scope_*`

这些字段如果需要参与隔离，应进入 `Context.scope` 或对应的 scope 类型，而不是进入 `filters`。

### 6. 生产 Store 完整下推，UnitReader 做纵深复核

向量和全文生产 Store 必须在 `limit/top_k` 截断前完整下推 `FilterExpr`；否则先取
未过滤 top-k 再做后置过滤，会漏掉排序靠后但满足条件的真实命中。`UnitReader`
仍使用同一套 evaluator 对完整表达式复核，用于防御索引滞后和后端语义偏差，但它
只能保证最终返回项不误召，不能找回已被 top-k 截断的候选。

执行顺序：

1. API / SDK 输入规范化为 `FilterExpr`
2. `QueryParser` 把 `filters` 放入解析结果
3. lifecycle、valid-time、event-time 等系统谓词作为外层 `AND` 与用户表达式合并
4. Milvus / Elasticsearch / pgvector 在 top-k 截断前完整下推合并后的表达式
5. `UnitReader` 点读真源后执行完整过滤表达式

图和内存实现不属于当前生产过滤保证范围；它们可依赖 UnitReader 做返回精度复核，
但不保证过滤条件下的 top-k 完整性。

### 7. metadata 保留 JSON 原生类型，不做查询侧隐式转换

`MemoryUnit.metadata`、`RawPayload.metadata`、`Document.metadata` 和
`VectorRecord.metadata` 统一使用 `dict[str, Any]`。写入和更新边界只接受 JSON
标量（string / number / boolean / null）或字符串数组，并拒绝与系统索引字段同名的
保留 key。索引构建直接复制业务 metadata，再用真源系统字段覆盖保留字段，不再把
业务值统一字符串化。

查询侧同样不根据 metadata 样本或外部 schema 做隐式转换：

- `int` 与有限 `float` 视为同一数值类别，可用于范围比较；
- string、number、boolean 之间不互转；
- 范围算子的查询值必须是有限数值；
- `IN` / `NOT_IN` 的元素必须属于同一类型类别；
- `EQ` / `IN` 的正向匹配只命中标量，`CONTAINS` 只命中数组成员；
- `NE` / `NOT_IN` 分别是 `EQ` / `IN` 的逻辑否定，因此数组和缺失字段不满足正向
  标量谓词时满足其否定；
- 标量 `CONTAINS` 不退化为等值或字符串子串，数组 `EQ` / `IN` 不退化为成员匹配；
- 范围算子只作用于标量：数组字段不满足任何范围谓词，不按「任一成员命中」判定。

因此同一个业务 key 的类型稳定性由调用方负责。当前不引入独立
`metadata_schema`，也不在查询时猜测 `"8"` 应解释为字符串还是数字，避免静默改变
业务语义。

Elasticsearch 的倒排字段本身不区分单值与数组，因此文档写入时额外生成内部
`metadata_array_fields` 字段记录数组 key，查询编译器据此区分 `EQ` 与 `CONTAINS`。
该字段不进入公开 `Document.metadata`。范围算子同样受该标记约束——Lucene 的 range
对多值字段是「任一成员命中即匹配」，不加约束会比真源复核和 PostgreSQL 宽松。
为已有索引增加 mapping 不会回填历史文档，启用严格语义后需重建旧索引。
PostgreSQL/pgvector 直接通过 JSONB 值、`jsonb_typeof(...)= 'array'` 与
`jsonb_typeof(...)= 'number'` 区分标量等值、数组成员与数值范围，不需要额外 schema。

### 8. valid-time 开放区间使用索引哨兵

真源中 `Temporal.t_invalid is None` 表示开放有效期。由于缺失字段无法满足
`t_invalid > as_of` 的后端谓词，索引投影将其写为 `T_INVALID_OPEN`
（9999-12-31T23:59:59Z 的 epoch 毫秒）；真源仍保留 `None`。

历史查询把以下系统谓词作为外层 `AND` 下推，用户表达式中的 `OR` 不能稀释它们：

- `lifecycle != forgotten`
- `t_valid <= as_of`
- `t_invalid > as_of`

`UnitReader.valid_at` 直接读取真源 `[t_valid, t_invalid)` 区间；调用方显式过滤
`t_invalid` 时，后置 evaluator 则使用与索引一致的哨兵投影，避免下推和复核语义分叉。

## 示例

### 简单等值兼容

```python
RetrievalQuery(
    text="preferences",
    filters=[
        FilterClause("category", FilterOp.EQ, "preferences"),
        FilterClause("source", FilterOp.EQ, "text"),
    ],
)
```

### 树形逻辑过滤

```python
RetrievalQuery(
    text="urgent project tasks",
    filters=FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("project", FilterOp.IN, ["alpha", "beta"]),
            FilterClause("priority", FilterOp.GTE, 8),
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("assignee", FilterOp.EQ, "alice"),
                    FilterClause("assignee", FilterOp.EQ, "bob"),
                ],
            ),
            FilterGroup(
                FilterLogic.NOT,
                [
                    FilterClause("status", FilterOp.EQ, "archived"),
                ],
            ),
        ],
    ),
)
```

### dict DSL 输入

```python
filters = {
    "AND": [
        {"project": {"in": ["alpha", "beta"]}},
        {"priority": {"gte": 8}},
        {"OR": [{"assignee": "alice"}, {"assignee": "bob"}]},
        {"NOT": {"status": "archived"}},
    ]
}
```

## 拒绝的方案

- **让 `FilterClause.value` 递归承载 `FilterClause`**：被拒。它会把字段比较值和逻辑表达式混在同一个字段里，类型边界不清晰，也会增加 Store 和 UnitReader 的解析复杂度。
- **直接把内核 filters 改成 `dict[str, Any]`**：被拒。dict 适合 API 输入，不适合作为检索内核契约；内核应消费类型稳定的 `FilterExpr`。
- **把 scope 编进 metadata filter**：继续拒绝。scope 是隔离轴，不是业务 metadata；混进 filter 会削弱存储层强制隔离，也破坏现有 scope 链路的不变量。
- **只在 Store 下推，不做 UnitReader 复核**：被拒。索引字段可能滞后，不同后端过滤语义也可能不同；真源复核是最终正确性边界。
- **查询时按 schema 或样本自动转换字符串数值**：被拒。当前没有中央 metadata
  schema，猜测转换会让 `"001"`、`"true"` 等业务值产生歧义；调用方应按写入类型
  构造过滤值。
- **开放 `t_invalid` 在索引中保持缺失**：被拒。生产后端对缺失字段执行范围谓词会
  排除记录，历史查询因此系统性漏召仍有效记忆。

## 验证

已覆盖以下测试基线：

- `tests/unit/common/test_filters.py`
  - `list[FilterClause]` 规范化为 `AND` 组
  - dict DSL 规范化为 `FilterExpr`
  - `AND` / `OR` / `NOT` 的合法性校验
  - scope 字段出现在 filters 中时报错
- `tests/unit/retrieval/test_unit_reader.py`
  - `AND` / `OR` / `NOT` 嵌套复核
  - 旧的扁平 filters 行为保持兼容
  - 标量 `EQ` / `IN` 与数组 `CONTAINS` 形态严格区分
- `tests/unit/storage/test_filter_compilers.py`
  - Milvus / Elasticsearch / PostgreSQL 完整编译 AND / OR / NOT、集合和数值范围
  - Elasticsearch 派生数组标记与 PostgreSQL JSONB 形态判断保持相同语义
  - 系统谓词与用户表达式保持外层 AND
- `tests/unit/construction/test_index_builder.py`
  - 业务 metadata 原生类型写入索引
  - `t_invalid=None` 投影为 `T_INVALID_OPEN`
- `tests/integration/storage/test_integration_backends.py`
- `tests/integration/storage/test_integration_fulltext.py`
  - 真实 Milvus / Elasticsearch 在 top-k 前执行过滤，并区分标量等值与数组成员
- `tests/integration/storage/test_integration_postgres.py`
  - 真实 pgvector 在 top-k 前执行过滤，并区分标量等值与数组成员

文档落地对应命令：

```bash
PYTHONPATH=src uv run --no-sync pytest -q -m unit
PYTHONPATH=src uv run --no-sync pytest -q \
  tests/integration/storage/test_integration_backends.py \
  tests/integration/storage/test_integration_fulltext.py
git diff --name-only -z HEAD -- '*.py' | xargs -0 uv run --no-sync ruff check
```

## 已知遗留

- 当前不维护中央 metadata schema，也不阻止同一业务 key 在不同记忆间发生类型漂移；
  调用方须保持同 key 类型稳定，不可比记录按不匹配处理。
- Elasticsearch 旧索引中的历史文档没有 `metadata_array_fields` 派生标记；仅增加
  mapping 不能回填，升级后必须重建索引才能获得严格的标量/数组过滤语义。
- 当前态查询的索引前置谓词只包含 lifecycle；未来生效或已经过期但仍为 ACTIVE 的记录
  会被 UnitReader 正确剔除，但可能先占用召回预算，因而在使用 TTL/未来
  `t_valid` 时仍有 top-k 完整性风险。
- `T_INVALID_OPEN` 定义了历史查询的最大支持边界；`as_of` 达到或超过该哨兵时，开放
  有效期记录无法满足严格大于谓词。
- 图和测试用内存后端未实现生产级完整下推；当前部署未启用图通道，内存后端只用于测试。
