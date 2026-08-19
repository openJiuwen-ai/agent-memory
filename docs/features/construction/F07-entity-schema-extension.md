# Entity Schema 抽取隔离扩展

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-19 |
| 状态 | 可选扩展，Schema 抽取、Entity Identity 与 Property Merge |
| 影响范围 | `jiuwen_memory/api/`、`jiuwen_memory/construction/`、`jiuwen_memory/storage/`、`docs/specs/S02-memory-api.md`、`S05-construction.md`、`S06-storage.md` |
| 测试基线 | Schema 定向测试 31 passed；Storage + Construction 回归 353 passed、5 skipped |
| Refs | — |
| 代码边界 | 与同类官方实现并列的 opt-in 新增文件，不修改官方 Assembly、Evolver、Storage 与 Bootstrap |
| 持久化模型 | 每个 property 一个版本化 MemoryUnit；原始输入一个 Source MemoryUnit |

## 背景

官方 mem2.0 的默认 `OrchestratingEvolver` 在 `infer=true` 时把原文放入短期
`/messages/`，然后抽取派生记忆。Schema LLM 的 JSON、实体/属性白名单或时间一致性
校验若连续失败，派生为空，而且原文不能从普通 `/memory/` 索引召回。

本扩展需要迁移 Entity Schema 抽取，同时尽量不碰官方文件，避免上游更新时在
`construction/evolver_impl/orchestrating_evolver.py`、`construction/bootstrap.py` 和
API 装配器产生冲突。

## 决策

### 1. 仅新增隔离包

实现文件按项目职责放置：Extractor 位于 `construction/extractor_impl/`，Evolver 位于
`construction/evolver_impl/`，Schema 与 Prompt 位于 `construction/`。新增的
`register_schema_constructors()` 显式注册 `entity_schema` Extractor 和
`schema_orchestrating` / `schema_dynamic` Evolver；`build_schema_kernel()` 再委托官方
`build_kernel()` 完成
其他组件装配。官方 bootstrap、实现包 `__init__.py` 和原 API 装配器均不修改。

### 2. 抽取流程

```text
输入 MemoryUnit
  -> Schema Selection（可选，只把选中属性送入 Prompt）
  -> Entity Generation（message_mapping / entities / edges）
  -> Normalizer（使用本轮 selected schema 精确校验）
  -> 每个合法 property 构造一个 MemoryUnit
```

根响应必须是一个完整 JSON object；只允许完整 Markdown fence 包装，不再从解释文字中
扫描并误收内部对象。结构化抽取默认最多校验三次，由
`schema_validation_attempts` 配置。

### 3. Source-first 降级

`SchemaOrchestratingEvolver` 和 `SchemaDynamicEvolver` 只覆盖官方 `_evolve_extract()`：

```text
原始输入
  -> 先写 /memory/ 并建索引（memory_role=source_evidence）
  -> 再调用 Schema Extractor
       成功：追加 property MemoryUnit
       失败：记录 schema_extract_degraded，保留 Source MemoryUnit
```

Source 持久化失败会直接抛错；只有 LLM/Schema 抽取失败可降级。这样 API 返回的
`created_ids` 始终能从真源加载，不会把“Gold 对话丢失”伪装成成功。

### 4. Entity Identity 与 Property Merge

Property Merge 前先由 `SchemaEntityResolver` 解析实体身份：同 Scope、同 Schema 下优先复用
精确基础名称与 entity type；需要时用 CREATE/UPDATE Prompt 判断；不同显式
说话人和“具体人名 ↔ 泛化 User”禁止合并。解析结果写入
`schema_entity_id/schema_entity_key`。`SchemaEntityRegistry` 随后把 canonical Entity 写入
`/schema/entities/`；如果 Storage 配置了独立 `schema_entities` vector/fulltext port，还会写
`entity_id#sfN` 多向量和实体全文文档。Entity 真源与检索不依赖图存储。

Schema 扩展通过独立的 `schema_composite` Storage target 装配这些命名端口。该 target 直接
复用官方 `CompositeStorage` 数据面，只在装配期额外识别 `schema_entities`，不修改官方
`composite_storage.py`。端口是否声明就是 Entity 独立索引的能力开关；未声明时 Entity Registry
仍写 KV 真源，Resolver 使用 KV/MemoryUnit fallback。最小内存配置如下：

```yaml
vector_store:
  schema_entities: memory
fulltext_store:
  schema_entities:
    target: memory
    params: {tokenizer: default}
storage:
  default:
    target: schema_composite
    params:
      kv_store: default
      vector_store: default
      fulltext_store: default
      graph_store: default
```

部署时可把两个 `schema_entities` 实例分别替换为独立 Milvus collection 与 Elasticsearch
index；普通 Memory 与 Entity 即使使用相同逻辑 ID，也保存在不同后端命名空间。

`SchemaPropertyMergePlanner` 按精确 Scope 与 canonical entity id 召回历史属性，使用属性名、
属性值和事件时间批量规划：

- 无候选或 Merge LLM 失败：ADD 全部新属性，信息保全优先；
- 同一属性、同一已知事件时间的更正：新建 replacement，旧版本标记 `SUPERSEDED`；
- LLM 判定旧事实冗余或显式 property delete：旧版本标记 `ARCHIVED`；
- delete 命令自身不持久化。

Planner 只读，`SchemaPropertyMergeExecutor` 负责按“先 ADD、再失效旧版本”的顺序写 Storage
和 IndexBuilder。`use_property_merge` 默认 `false`；普通
set 退回逐属性 ADD，显式 delete 仍保持安全的 archive 语义。为兼容早期迁移配置，同时接受
`schema_property_merge_enabled` 别名，但 `use_property_merge` 显式值优先。

Entity 配置沿用原项目的 `schema_entity_resolution_enabled` 和
`schema_entity_merge_decision_enabled`；早期迁移键 `schema_entity_merge_enabled` 作为后备别名。

非 procedural 路径使用 Source-first。procedural 路径复制原项目行为：不额外保存 Source，
但仍执行 Entity Identity、Entity Registry 和 Property Merge。需要动态四步 ordinary flow 时，
配置 `schema_dynamic`，Schema property 仍绕过普通相似度 Dedup，交给 Property Merge。

### 5. 与 mem2.0 数据结构兼容

Schema 内部字段写 `system_metadata`，来源业务字段按 mem2.0 规则继承到
`user_metadata`；实体明文写入 `MemoryUnit.entities`。不新增或修改官方 `MemoryUnit` 字段。
完整日/时间复用 `Temporal.t_event`；年/月精度和半开区间暂存为
`schema_event_time_*` JSON 标量，避免修改官方 `Temporal`。

### 6. 本阶段明确不包含

本阶段已迁移 Schema 抽取、Source-first、Entity Identity、独立 Entity Registry 和
Property Merge，但不包含 Neo4j 图投影或 Schema Temporal Retrieval。Extractor 仍会校验
edge 并产出内部 intent，但 Schema Evolver 不持久化 relation intent。年/月事件区间只用于
Property Merge 的 same-event 判断，不提供时间线查询。后续功能仍以新增组件加入，不反向
修改官方 Evolver。

### 7. 使用方式

调用方从 `jiuwen_memory.schema` 使用 `assemble_schema()`，配置默认 extractor 为
`entity_schema`、默认 evolver 为 `schema_orchestrating`（动态四步选 `schema_dynamic`），写入时设置
`system_metadata={"infer": True}`。需要 Entity 独立索引时再把默认 Storage 设为
`schema_composite` 并声明 `schema_entities` 端口。完整可运行示例见
`examples/schema_extension_quickstart.py`。

## 拒绝的方案

### 本期同时持久化关系和时序图

拒绝。关系图和时间线需要独立的实体关系真源、图生命周期和检索契约；附加在 Extractor 或
Property Merge 中会破坏职责边界。本期只保留经过校验的内部 relation intent，不持久化关系图。

## 验证

- `test_entity_schema_extension.py`、`test_schema_property_merge_extension.py`、
  `test_schema_composite_storage.py`：31 passed；
- `tests/unit/storage` 与 `tests/unit/construction`（排除未被本特性修改的
  `test_entity_linker.py`）：353 passed、5 skipped；跳过项为当前环境未安装的可选
  Elasticsearch/PostgreSQL 客户端及既有惰性依赖分支；
- Ruff 检查通过；`git diff --check` 通过；
- Git hash 对比确认官方 `assembly.py`、两个官方 Evolver、`composite_storage.py`、默认配置和
  官方 Bootstrap 均与功能分支基线一致。

## 已知遗留

1. 尚未迁移 Entity/Memory/Relation 图投影、Neo4j 与 Schema Temporal Retrieval；
2. Relation intent 当前只校验和记录日志，不持久化为 Relation MemoryUnit；
3. `schema_entities` 已支持独立 vector/fulltext 端口，但真实 Milvus/Elasticsearch 的重启恢复
   集成测试尚未迁移；
4. `schema_composite` 为避免修改官方文件而保留少量 Producer 装配代码；官方
   `CompositeStorage` 构造参数变化时，需要同步审查这个薄装配器；
5. 年/月时间使用 `schema_event_time_*` 标量保存区间，只参与 Property Merge 的 same-event
   判断；完整 snapshot/range/history 查询不在本期范围。
