# F06 — 统一存储直写 IndexBuilder

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-12 |
| 影响范围 | `jiuwen_memory/construction/index_builder_impl/`、`jiuwen_memory/construction/AGENTS.md`、`docs/specs/S05-construction.md` |
| 测试基线 | `uv run --no-sync pytest tests/unit/construction/test_index_builder.py -q`（22 passed）；`uv run --no-sync ruff check jiuwen_memory/construction/index_builder_impl/unified_index_builder.py tests/unit/construction/test_index_builder.py`（passed） |

## 背景

现有 `IndexBuilder` 实现都把 `MemoryUnit` 投影为向量、全文或两者组合的派生索引。统一存储装配需要一个不依赖 Chunker、Embedder 或具体索引 Store 的实现，将构建生命周期直接委托给 `Storage` 的记忆单元写接口。

## 决策

新增注册名为 `unified` 的 `UnifiedIndexBuilder`。它将输入按 `Scope` 分组后：

- `build` 调用 `Storage.add(scope, units)`；
- `update` 调用 `Storage.update(scope, units)`；
- `remove` 调用 `Storage.delete(scope, unit_ids)`；
- `rebuild` 返回 `None`，与现有最小实现的重建语义一致。

按 Scope 分组是必要条件：`Storage` 的写接口要求显式 scope，且会校验每个 `MemoryUnit.scope` 与该参数一致。该实现不生成向量、全文或分层索引；若需要这些检索投影，应继续选择 `vector`、`fulltext` 或 `hybrid`。

## 拒绝的方案

- 逐条调用 Storage：能够满足接口，但放弃同 Scope 批量写能力，也与其他批量 Builder 的行为不一致。
- 让 unified 组合既有 HybridIndexBuilder：这会引入 Chunker、Embedder 和索引 Store 依赖，违背统一存储直写模式的目标。
- 在 `IndexBuilder` 抽象接口中新增 Storage 专用方法：四个既有生命周期方法已足以表达需求，扩展接口会扩大所有 Builder 的适配面。

## 验证

单元测试覆盖跨 Scope 的 build、update、remove，以及 `unified` 的工厂装配：`uv run --no-sync pytest tests/unit/construction/test_index_builder.py -q` 通过 22 项。`uv run --no-sync ruff check jiuwen_memory/construction/index_builder_impl/unified_index_builder.py tests/unit/construction/test_index_builder.py` 和 `git diff --check` 均通过。

## 已知遗留

`unified` 不建立派生检索索引。其检索能力取决于所注入 `Storage` 自身支持的 recall/retrieve 管线，不由此 Builder 提供。

当前 `InMemoryEngine` 与 `CloudEngine` 的标准写、更新和删除链路会先调用同一 `Storage` 的写接口，再调用 `IndexBuilder`。因此不能仅将现有 profile 的 IndexBuilder 替换为 `unified`：`build` 会对已存在的 unit 再次执行 `Storage.add`，导致冲突。要在 engine 链路启用该模式，后续需要将 control 路由调整为由 `UnifiedIndexBuilder` 独占该次 Storage 写入。
