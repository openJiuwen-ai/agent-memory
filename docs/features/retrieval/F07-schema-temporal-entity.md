# Schema TemporalEntity 双轴检索

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-18 |
| 影响范围 | `jiuwen_memory/construction/`、`jiuwen_memory/retrieval/`、`jiuwen_memory/storage/`、`jiuwen_memory/config/`、`docs/specs/S04-retrieval.md`、`docs/specs/S05-construction.md`、`docs/specs/S08-config.md` |
| 测试基线 | `tests/unit/construction/test_entity_schema_extension.py`、`tests/unit/retrieval/test_schema_temporal.py`、`tests/unit/storage/test_schema_property_index.py` |

## 背景

Schema 属性以「每属性一个 MemoryUnit」的形式持久化后，普通混合检索能找到
相关 Property，却不理解同一实体的属性历史。只按单个 unit 排序会带来三类问题：

- 查询命中某个实体后，无法在实体内对属性做第二次裁剪；
- 单条 Property 命中可能在实体名额截断时丢失，同属性相邻历史也不可见；
- `t_event` 和 `[t_valid, t_invalid)` 被当成一条时间轴，无法表达
  「事情在何时发生」与「系统在何时知道该事实」的差异。

另一方面，Schema 采用 Source-first：即使属性抽取为空或失败，原始 Source
MemoryUnit 仍是可检索的耐久证据。时序检索若只读 Property，会破坏这条完整性
保障。

## 决策

### 1. TemporalEntity 是只读临时视图

TemporalEntity 在检索期间由已持久化的 Schema Property MemoryUnit 按 canonical
identity 组装；identity 依次取 `schema_entity_id`、`schema_entity_key`、
`type::name`。它用于实体内属性选择和时间线运算，不是新的记忆真源，不写入 KV、
向量库或全文索引，也不创建 synthetic MemoryUnit。

Schema Temporal 正式输出按实体投影：每个 `TemporalEntity` 对应一个
`RetrievedItem`，其 `unit_id` 是稳定 Entity id，`content` 是保留时间精度的完整实体
时间线；结构化视图同时保存在 `RetrievalResult.schema_temporal`。必要的 Source-first
兜底仍使用真实 MemoryUnit id。调用方不得把 Entity 视图项的 `unit_id` 当作
MemoryUnit id 点读；Property/Source 仍是唯一持久化真源。

### 2. 默认关闭，请求级显式启用

`globals.schema_temporal_enabled` 是装配期硬门，默认为 `false`。只有开关开启
时，`PipelineRetriever` 才会装配 Schema TemporalEntity 选择器。单次查询通过
`RetrievalQuery.schema_temporal` 显式启用；传输层也兼容
`RetrievalQuery.extensions["schema_temporal"]`。可选的
`schema_temporal_auto_enabled` 在 `parsed.time_from/time_to` 已有时间窗，或内置
规则能从原始 query 解析出时间窗时自动启用。

扩展值支持 `true`、模式字符串，或包含下列字段的字典：

- `mode`: `latest | snapshot | history | range`；
- `event_at` / `event_from` / `event_to`: 事件时间点或半开区间；
- `event_precision`: `unknown | year | month | day | datetime`；
- `knowledge_as_of`: 系统有效时间回溯点；
- `property_names`、`include_undated`、`include_archived`；
- `fallback_policy`: `none | full_timeline`，后者仅用于 `snapshot/range` 空结果；
- `entity_limit`、`per_entity_limit`。

非法组合或时序选择异常记录为 TEMPORAL 通道降级错误，并继续使用已融合的
普通候选；未启用或未触发时不改变普通检索行为。

### 3. 实体与 Property 两条独立路径

时序选择在普通 Reranker、阈值、top-k 与 disclosure 之后运行，并保留两条互补的信号：

1. **Entity 路径**：最终普通结果提供实体 seed，并与独立 `schema_entities`
   全文/向量索引召回做 RRF；实体向量的 `entity_id#sfN` 命中按 owner 去重，
   再按实体读取完整属性集并在实体内使用查询二次选择 Property；
2. **Property 路径**：通过 Schema 过滤的关键词/向量通道独立直达召回 Property，
   再以晚融合方式回填到所属 TemporalEntity。直达 Property 不受 Entity 数量
   截断影响。

实体间独立返回，不使用一个全局 `break` 将后续实体全部截断。对每个直达
Property，从同实体、同属性的有序时间线向前和向后各扩展
`property_extension_step` 条，默认为 3，使变化前后的事实同时可见；通用兜底
属性 `default_property` 不做邻居扩展。

### 4. 两条时间轴独立计算

Property 条目同时保留：

- **event-time**：事实描述的发生时间，由 `schema_event_start/end/precision`
  表达；日和日时精度同时使用 `temporal.t_event`；
- **knowledge-time**：该 MemoryUnit 在系统中的 `[t_valid, t_invalid)` 有效区间。

`knowledge_as_of` 先决定当时系统「知道哪些版本」，再由 `mode` 按 event-time
选择 `latest`、`snapshot`、`history` 或 `range`。年/月精度使用半开区间参与
相交计算，不伪造具体日期写入 `t_event`。

### 5. Source-first 兜底进入时序输出

在属性抽取缺失、无事件时间或 Property 内容无法完整支撑查询时，选择器
从普通融合候选中保留尚未被直达 Property 充分覆盖的 Source MemoryUnit。
充分的结构化 Property 仍是主路，Source 只是受控兜底，避免扫描并将整个 Scope
的原始对话混入结果。Source 在最终实体投影后按保留比例融合，并在内容前写入
`message_time` 与“事件时间未结构化抽取”的提示，因此不会再被共享 top-k 挤掉，
也不会把消息时间伪装成事件时间。

反向索引是 Schema Property 写入层的共享派生索引；
`schema_enabled=true` 时，`IndexBuilderProducer` 在任意具体 target 外包装
Entity → Property Unit ID 维护器。TemporalEntity 只消费该索引，
`schema_temporal_enabled` 不会改变写入拓扑。`FORWARD_ONLY build` 记录排除项，避免
后续 Scope 回填误纳入未建检索索引的 Property；`FORWARD_ONLY update` 保留已有
membership，以支持软删后的历史时间线。Property
canonical identity 依次取 `schema_entity_id`、`schema_entity_key`、`type::name`，且
必须具有非空 `schema_property_name`。首次维护一个 Scope 时，写路径对该 Scope 的
全部 MemoryUnit 只做一次兼容回填，依次提交 membership/pointer，并在最后写入 Scope
watermark 作为完整提交标记。watermark 缺失或失效时读取侧回退 Scope 列表查询；
维护中途失败必须使 watermark 失效，因此不会把部分索引误当成权威结果。携带
MemoryUnit 的硬删和 `remove_with_scope()` 均同步维护索引。若一体化或自定义 manager
没有 KV 端口，则不包装写路径，读取侧直接使用 DomainStore Scope fallback。

### 6. 配置与预算

默认调优参数是：

- `schema_temporal_entity_top_k=20`；
- `schema_temporal_property_top_k=50`、`schema_temporal_property_top_n=25`；
- `schema_temporal_rrf_k=60`；
- `schema_temporal_max_properties_per_entity=20`；
- `schema_temporal_property_allocation_min_factor=0.5`、最大因子 `1.5`；
- `schema_temporal_property_rerank_enabled=true`，复用 Retriever 已装配的 Reranker；
- `schema_temporal_direct_property_rerank_enabled=false`；
- `schema_temporal_entity_rerank_enabled=false`、实体精排输入最多 4000 字符；
- `schema_temporal_property_extension_step=3`；
- `schema_temporal_formatting_enabled=true`、每实体最多 16000 字符；
- `schema_temporal_source_fallback_enabled=true`；
- `schema_temporal_source_policy=missing_or_incomplete`；
- `schema_temporal_source_top_k=20`、`schema_temporal_source_ratio=0.3`；
- `schema_temporal_source_max_chars=1200`；
- `schema_temporal_auto_enabled=false`。

数量预算用于两条路径召回、实体内裁剪与最终实体/Source 输出。它们不会把
TemporalEntity 变成可持久化结果。`max_properties_per_entity` 是实体内裁剪的
基准预算；直达
Property 晚融合和邻居扩展可以使最终条目数超过该基准。

### 7. 本期边界

本期不迁移以下能力：

- 实体或记忆图、图边持久化和图扩展；
- episode 抽取、Episode 节点或 Episode → Entity/Memory 边；
- Schema 专用的第二套外部 Reranker（本期复用 Retriever 已装配实例）；
- 将 TemporalEntity 存储为 MemoryUnit 或新的 Store 真源。

## 拒绝的方案

### 持久化一个合成 TemporalEntity MemoryUnit

拒绝。时间线只投影为标记明确的 `RetrievedItem`，不伪装成可点读 MemoryUnit；
Property/Source 的鉴权、生命周期和血缘在组装前完成复核。

### 用全 Scope 扫描作为唯一实体属性读取方式

拒绝。该方式随记忆数量线性增长。反向索引只在首次维护整个 Scope 时做一次全量
回填，并用 Scope watermark 区分「已完整回填」与「尚未建好或维护失败」；watermark
缺失或失效时才执行兼容回退，而不是在每次实体读取时扫描全 Scope。

### 用消息时间填充缺失的事件时间

拒绝。`t_message` 表示来源消息的发送时间，不是事实必然发生的时间。
无明确事件时间的 Property 可在 `content` 中附带 Source message date 作为
as-of 上下文，但 `t_event` 必须保持为空。

## 验证

- 默认配置不装配选择器，普通检索结果不变；
- 显式扩展值与条件自动启用均能生成可观测的 Schema Temporal 轨迹；
- 验证 `latest/snapshot/history/range` 及 event-time/knowledge-time 的独立过滤；
- 验证直达 Property 不受 Entity 名额截断，并扩展非 `default_property` 同属性
  前后各 3 条；
- 验证属性缺失时 Source-first 证据仍能进入候选；
- 验证每个 Entity 只返回一个格式化 `RetrievedItem`，并保留结构化
  `RetrievalResult.schema_temporal`；
- 验证必要 Source 在最终投影后保留且显式携带消息时间；
- 验证反向索引的增、改、携带 MemoryUnit 的硬删、权威空集和存量回退语义。

## 已知遗留

1. TemporalEntity 当前仅消费 Schema Property 和受控 Source fallback，不组装
   Episode 或图关系；
2. Schema 实体内 Property 裁剪复用 PipelineRetriever 已装配的可选 Reranker；
   未配置时按实体内召回分和词法分确定性降级；
3. Entity → Property 反向索引会在 Scope 首次维护时自动执行一次全量兼容回填，但当前
   `IndexBuilder.rebuild()` 仍未提供显式的全 Scope 运维重建入口；
4. TemporalEntity 不自行解析别名或重做实体合并；它依赖上游 Entity Identity
   产出的 canonical entity id，实体身份质量仍受抽取与 Resolver 判断质量影响。
