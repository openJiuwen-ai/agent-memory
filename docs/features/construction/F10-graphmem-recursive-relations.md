# F10 会话内递归索引与跨会话关系

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-18 |
| 影响范围 | 递归 Composer、自顶向下关联器和 IndexBuilder |
| 测试基线 | 同会话追加复用、端点版本、窗口预算、关联失败后重试 |

通过部署配置启用 [GraphMem overlay](../../../examples/graphmem_fact_records_overlay.yaml)。
安装项目的 `evidence` 可选依赖，将 `GRAPHMEM_TOKENIZER_FILE` 指向已绑定模型的本地
tokenizer.json，并复用现有 LLM、向量索引及写入端口。

## 决策

### 会话内索引

使用现有 MemoryAPI 摄入 Snapshot，按完整子树发起显式 `EvolveMode.HIERARCHY` 请求。
传入源会话 Scope 和树所在 Scope，新树使用 `parent_roles=[TIME_SPAN]`。
同一会话内自下而上进行分组，按每组子节点数、输入 Token 和输出视图预算递归合并。
示例每组最多 32 个子节点，提示词最多 6,000 Token，摘要视图最多 1,400 Token。
摘要保留直接子引用，同时提取带来源的实体、场景和事件时间，父节点继承有界特征。
源 Snapshot 保留原始消息。请求追加后依据输入及策略指纹复用未变分组，沿受影响路径
更新摘要。写入和索引更新复用 IndexBuilder。

### 跨会话关系

`graphmem_topdown` 先在上层建立有限候选，然后沿已验证的子引用细化。
可使用共享实体、场景向量近邻和相邻的事件时间区间，关系属性共同存于一条稳定 ID 的边。
派生索引按端点版本和策略更新，覆盖窗口在边写入成功后发布。
启用 `associate_after_hierarchy` 后可在建树成功后刷新关系。刷新失败在结果中报告
`complete=false` 和 `repair_required`，可经既有 ASSOCIATE 路径重试。

## 拒绝的方案

固定层数会把长会话的合并压力集中在最上层。全量比较跨会话节点的成本随节点数平方增长，
也会挤占有直接实体或时间证据的边的预算。

## 验证

公开构建与追加测试覆盖组复用、原文保存和来源追溯。关联测试覆盖端点版本及窗口上限。
接口、时间和一致性约束见 [S05 构建层](../../specs/S05-construction.md)。

## 已知遗留

启用自动刷新时，部署需要按 Scope 串行调度。超出有界关联窗口时报告维护失败。
