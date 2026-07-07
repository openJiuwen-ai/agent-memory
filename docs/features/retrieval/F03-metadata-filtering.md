# F03 — 元数据过滤设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-03 |
| 影响范围 | src/common/type_def/filter.py，src/retrieval/types.py，src/retrieval/retriever_impl/unit_reader.py，src/storage/{vector,fulltext,fusion}.py，docs/specs/S02-memory-api.md，docs/specs/S04-retrieval.md，docs/specs/S06-storage.md |
| 测试基线 | 未执行（本文仅归档设计，未改代码） |
| Refs | — |

> 本文归档 agent-memory 的 metadata 过滤设计。目标是让 `filters` 支持类似 DSL 的树形逻辑表达，同时保持 scope 过滤仍由现有 scope 链路负责，避免把租户、用户、agent、session 隔离语义混进 metadata filter。

---

## 背景

当前检索过滤模型是扁平结构：

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

- `MemoryAPI.recall(..., context=Context(scope=...))`
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

### 6. 后置复核必须支持完整表达式

无论 Store 是否能下推完整过滤条件，`UnitReader` 都需要使用同一套 evaluator 对完整 `FilterExpr` 复核。

执行顺序保持当前思路：

1. API / SDK 输入规范化为 `FilterExpr`
2. `QueryParser` 把 `filters` 放入解析结果
3. Store 在自身能力范围内下推过滤条件
4. `UnitReader` 点读真源后执行完整过滤表达式

正确性以 UnitReader 真源复核为准。Store 下推只是性能优化，不能成为唯一正确性边界。

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

## 验证

落地实现时应补以下测试基线：

- `tests/unit/common/test_filters.py`
  - `list[FilterClause]` 规范化为 `AND` 组
  - dict DSL 规范化为 `FilterExpr`
  - `AND` / `OR` / `NOT` 的合法性校验
  - scope 字段出现在 filters 中时报错
- `tests/unit/retrieval/test_unit_reader.py`
  - `AND` / `OR` / `NOT` 嵌套复核
  - 旧的扁平 filters 行为保持兼容
- `tests/unit/storage/`
  - Store 能接收新的 `FilterExpr`
  - Store 忽略或只部分下推时，UnitReader 后置复核仍保证结果正确

文档落地对应命令：

```bash
pytest tests/unit/common tests/unit/retrieval tests/unit/storage
ruff check
```

## 已知遗留

- 当前代码仍是 `filters: list[FilterClause]`，落地时需要先增加 `FilterGroup` / `FilterExpr`，再迁移 `RetrievalQuery`、`ParsedQuery`、Store query 类型和 UnitReader evaluator。
- API / SDK 的 dict DSL 需要在 specs 中同步定义输入格式和错误语义。
- 生产 Store 的复杂表达式下推能力可以分阶段实现，但 UnitReader 必须先支持完整表达式复核。
