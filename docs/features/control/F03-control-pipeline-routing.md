# F03 — 控制层 Pipeline 路由

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-27 |
| 影响范围 | `jiuwen_memory/control/`、`docs/specs/S03-control.md` |
| 测试基线 | `PYTHONPATH=. uv run --no-sync pytest -q tests/unit/control/test_pipeline.py tests/unit/control/test_permission_context_routing.py` 通过；本特性变更的 Python 文件通过 `ruff check` |

## 背景

记忆系统需要支持同一内核内的多种记忆类型 pipeline：例如情景记忆使用普通 LLM extractor，coding 记忆使用代码模型、代码向量化或不同检索策略。此前配置层已经能声明多个具名组件，但 `InMemoryEngine` 只持有一组 `IndexBuilder` / `Evolver` / `Retriever` / `Classifier`，写入和查询都无法按记忆类型选择不同 profile。

## 决策

新增 control 层 `MemoryPipeline` 抽象，把 pipeline 作为控制层编排能力，而不是 construction 或 retrieval 的本地能力。原因是 pipeline 同时影响构建和查询，放在单一子层会让另一侧反向依赖，或复制路由规则。

第一版实现 `metadata` pipeline（路由字段存于系统命名空间）：

1. 写入侧按 `MemoryUnit.system_metadata[route_key]` 路由。
2. 查询侧按 `RetrievalQuery.extensions[route_key]` 路由，规范化 `FilterExpr` 中逻辑上
   强制成立的 `system_metadata.<route_key>` 唯一等值作为兜底；OR 多值、NOT、AND 冲突不参与路由。
3. route 只返回 `PipelineBinding`，其中包含已装配的 `index_builder`、`evolver`、`retriever`、可选 `classifier`。
4. `InMemoryEngine` 仍负责数据面编排：写入时使用选中绑定处理分类/索引/同步抽取，查询时使用选中绑定的 retriever。
5. 默认配置不注入 pipeline；未配置 `pipeline.default` 时 `InMemoryEngine` 使用旧的单组组件字段，用户通过 YAML 显式声明后才启用 profile 路由。

## 拒绝的方案

拒绝把 `routing_extractor` 和 `routing_retriever` 分别放到 construction/retrieval 中作为唯一方案。这样短期改动少，但路由规则会重复，且 construction/retrieval 不能表达“同一个 memory_type 同时影响构建和查询”的跨层一致性。

拒绝让 construction/retrieval import control 的共享 router。那会打破现有依赖方向：control 可以编排 construction/retrieval，反过来不成立。

拒绝第一版实现完整 `RoutingEngine`。它可以覆盖更多行为，但会一次性牵涉 scheduler、显式 evolve、索引维护和生命周期路径，改动面大于当前需求。

## 验证

新增 `tests/unit/control/test_pipeline.py`：

- `system_metadata.memory_type=coding` 写入时使用 coding profile 的 `IndexBuilder`。
- `Context.extensions["memory_type"]="coding"` 查询时使用 coding profile 的 `Retriever`。

## 已知遗留

- 显式 `MemoryEngine.evolve(scope, mode)` 仍委托单一 `Scheduler`，没有按 profile 路由后台演进任务。
- 第一版 route key 只支持能唯一提取并规范为字符串的等值；复杂条件、优先级规则和组合路由需要后续扩展。
- Pipeline profile 当前要求同时声明构建和查询组件；未来可以拆成 write-only / recall-only profile。
