# Schema Temporal 子模块

本目录把持久化的 Schema Property `MemoryUnit` 在查询期间组装为只读实体时间线。
`TemporalEntity` 只承担查询时组装与计算，不是新的持久化真源。正式检索按实体投影为
`RetrievedItem`，同时保留结构化 `RetrievalResult.schema_temporal`；必要的 Source
fallback 仍来自真实 `MemoryUnit`。

## 模块地图

| 文件 | 职责 |
|---|---|
| `model.py` | event-time / knowledge-time 双轴模型和 latest/snapshot/history/range |
| `assembler.py` | Property MemoryUnit → `TemporalEntity` |
| `reader.py` | 反向索引点读、存量回退与时间可见性复核 |
| `time_extractor.py` | 中英文绝对/相对时间窗的确定性解析 |
| `query.py` | 显式扩展与自动时间意图合并为统一查询计划 |
| `recall.py` | 相互独立的 Entity seed 与 Property 直达路径 |
| `shrink.py` | 查询相关的逐实体属性预算与二次选择 |
| `searcher.py` | 水合、裁剪、直达晚融合、同属性邻居和时间过滤编排 |
| `selector.py` | 组织临时时间线、Property 候选与 Source fallback |
| `formatter.py` | 保留时间精度的实体时间线上下文格式化 |

## 行为铁律

1. **真源不变**：不得持久化完整 `TemporalEntity`，也不得创建 synthetic MemoryUnit。
2. **两轴独立**：event-time 表示事实何时发生；`[t_valid, t_invalid)` 表示系统何时知道，
   两者不得互相填充。
3. **先知识时间，后事件时间**：候选召回和点读都要复核 knowledge-time；完整时间线水合后
   才执行 snapshot/range 等 event-time 选择。
4. **双路互不截断**：Entity 名额不得删除 Property 直达命中；裁剪后按 unit id 回融直达
   Property，并为非 `default_property` 扩展同属性前后各配置数量的邻居。
5. **Source-first 有界兜底**：只从普通召回已授权、已复核的 Source 候选中保留缺失或时间
   语义不完整的证据，不扫描并混入整个 Scope 的 Source。
6. **实体级正式输出**：Property/Source 必须先按真实 unit 完成点读和复核；最终每个
   TemporalEntity 投影为一个实体级 `RetrievedItem`，其 Entity id 不得当作 MemoryUnit id
   点读。Source fallback 在最终投影后占用受控槽位并携带消息时间。
7. **精度不造假**：year/month/day 使用半开区间，年/月不得补成虚构的具体日期。
8. **失败可降级**：Schema 时序支路失败时记录 TEMPORAL 通道错误，普通候选链路继续工作。

## 与其他子目录的边界

**本模块管**：Schema Property 的查询时组装、双轴选择、实体内裁剪、实体级投影及
Source fallback。

**不管**：Schema 抽取与写入、Property Merge、图扩展、Episode 建模、独立外部 Reranker。

## 最小回归

```powershell
python -m pytest tests/unit/retrieval/test_schema_temporal.py `
  tests/unit/storage/test_schema_property_index.py
```
