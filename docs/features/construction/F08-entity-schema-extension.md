# 可选的实体 Schema 属性抽取

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-21 |
| 影响范围 | `jiuwen_memory/api/`、`jiuwen_memory/config/`、`jiuwen_memory/construction/`、`docs/specs/S02-memory-api.md`、`docs/specs/S05-construction.md`、`docs/specs/S08-config.md` |
| 测试基线 | `tests/unit/construction/test_entity_schema_extension.py` |

## 背景

通用抽取器能生成自由文本记忆，但无法保证实体类型、属性名称和输出粒度符合业务约束。
另一方面，mem2.0 已有 `MemoryUnit.entities`、EntityLinkService 和 EntityStore，不应再建立一套
Schema 专用实体真源、实体 ID 或索引协议。

本特性的目标是增加一条显式启用的 Schema 属性抽取路径：调用方提供实体类型及候选属性，
抽取器只生成白名单内的属性事实。属性成功落盘后，其所属实体及属性名写回相应 Source
MemoryUnit 的标准 `entities` 字段；后续反向索引继续复用既有 EntityLinkService。

## 决策

### 1. 功能显式启用，与默认链路隔离

Schema 复用统一 `assemble()` / `build_kernel()` 入口。`globals.schema_enabled` 在
`defaults.py` 中默认为 `false`；只有装配时显式设为 `true` 才条件注册
Schema target。调用方还必须显式选择 `entity_schema` Extractor 和
`schema_orchestrating` Evolver 才会进入 Schema 链路；默认 target 均不改变。
开关关闭却配置 Schema target 时装配 fail-closed，防止同一进程曾经注册过 Schema
target 后绕过开关。

Schema 代码放在现有模块的对应目录中。统一 assembly 只增加配置判定和条件注册点，
不修改官方 `OrchestratingEvolver`、`DynamicEvolver`、Storage 或 `MemoryUnit` 定义。

### 2. 两阶段抽取并严格使用选中属性集

Extractor 先让模型从完整 Schema 中选择本轮相关的 entity type 和 property，再只把选中的
Schema 发送给属性生成 Prompt。Normalizer 使用同一个选中 Schema 校验结果，而不是使用
完整 Catalog；模型即使额外输出完整 Schema 中未被选中的属性，也会被拒绝。
只有显式 `relevant_properties=["all"]` 表示选中该类型的全部属性；缺失、非数组或空数组
不会扩展成全量 Schema。Schema Selection 调用或根 JSON 解析失败时降级为完整 Schema；
合法的 `selected_entities=[]` 表示本轮没有可抽取类型，不再调用属性生成 LLM。

模型响应必须是单个根 JSON 对象。属性逐条校验 entity type、property name、来源
`source_unit_ids`、Scope 和显式说话者绑定。未知 entity type 不会被自动改成某个已选类型。
所有这些错误都会进入同一个纠错 Prompt，默认最多尝试三次；重试耗尽后，保留某次
响应中数量最多的合法属性并隔离其无效兄弟。如果没有任何合法属性则本轮 Schema
抽取失败；模型明确返回 `entities=[]` 则表示正常的空抽取。重试次数可以在 Extractor 配置中调整。

### 3. 每个属性生成一个标准 MemoryUnit

一个实体可以对应多个属性 MemoryUnit；每个属性 MemoryUnit 只表达一个属性事实，并使用：

- `content`：包含明确主语的属性事实文本；
- `entities=[]`：Property Unit 本身不进入实体反向索引；
- `system_metadata`：只保存 Schema 名称、版本、实体类型、实体明文和属性名；
- `source_ref` 与 `provenance`：回指支持该事实的原始消息；
- `temporal.t_event`：仅在属性具有可完整解析的日期或时间时填写。

属性 Unit 成功持久化后，Evolver 按其 `provenance` 找到对应 Source Unit，把
`schema_entity_name` 和 `schema_property_name` 去重聚合到 Source 的 `entities`。一个 Source
支持多个实体和多个属性，Property Unit 仍通过 `source_ref/provenance` 回指 Source。

### 4. Source-first 保证原始信息不丢失

非 procedural Schema 写入先持久化并索引原始 Source MemoryUnit，再执行 LLM 抽取：

```text
原始输入
  ├─ 持久化 Source MemoryUnit（失败则写入失败）
  └─ Schema 抽取
       ├─ 成功：直接新增属性 MemoryUnit
       └─ 失败：记录降级原因，保留 Source MemoryUnit
```

Schema 属性绕过普通相似度 Dedup，避免通用文本相似度把不同属性错误地 UPDATE 或
SUPERSEDE。当前语义是 append-only：每次成功抽取的属性都作为新 MemoryUnit 写入。

### 5. 复用既有实体链路

Schema Extractor 不生成自定义 `schema_entity_id`，也不维护 Schema Entity Registry。
Evolver 通过 Storage 只读加载 Source MemoryUnit，合并实体名和属性名后调用
`IndexBuilder.update(mode=ALL)`，由 IndexBuilder 统一回写 Source 本体并刷新检索索引。
IndexBuilder 看到 Source 的 `entities` 后，按现有配置调用 EntityLinkService；该服务负责
名称归一化、EntityRecord upsert 以及实体名/属性名→Source MemoryUnit 的反向链接。

因此，是否建立实体索引仍由 mem2.0 原有 `entity_enabled` 和 EntityStore 配置决定。Schema
功能本身不新增 `schema_entities` collection/index，也不要求自定义 Storage。

### 6. 本期边界

本期只包含 Schema Selection、属性抽取与校验、每属性一个 MemoryUnit、Source-first 降级和
标准实体字段接入。不包含：

- 自定义 Entity Identity、Entity Registry 或别名合并；
- Property Merge、显式属性删除或旧属性归档；
- relation/edge MemoryUnit、图投影和图查询；
- Schema 时间线、snapshot/range/history 查询；
- episode、higher-order property 或动态 property 生成。

## 拒绝的方案

### 建立 Schema 专用 Entity Registry 和独立 Storage

拒绝。mem2.0 已有 `MemoryUnit.entities` 和 EntityLinkService。再维护
`schema_entity_id/schema_entity_key`、隐藏 KV 和 `schema_composite` 会形成两套实体真源，
增加装配、重建和一致性成本。

### 在本期实现 Property Merge

拒绝。可靠合并依赖稳定实体身份、按实体属性召回和明确的版本生命周期。当前最小 PR 先保证
抽取结果不丢失并接入标准实体链路；通用 Dedup 又不适合代替属性级合并，因此属性采用直接
ADD。

### 把 Schema 行为塞进官方 Evolver

拒绝。Schema 的 Source-first 和直接 ADD 语义与默认 Evolver 不同。独立
`schema_orchestrating` 可以保持功能 opt-in，并降低与上游演进代码的冲突。

### 保留独立 assemble_schema 入口

拒绝。部署和调用方已统一使用 `assemble()`，额外入口会迫使上层改变装配调用，
也无法仅通过现有配置系统启用。默认兼容由 `schema_enabled=false` 和未变的默认
Extractor/Evolver target 共同保证。

## 验证

- 验证选中属性白名单、空选择语义、严格根 JSON、来源绑定和事件时间映射；
- 验证来源绑定与实体类型错误参与三次纠错，且未知类型不会被静默改型；
- 验证一个实体的多个属性生成多个 Unit，Property Unit 的 `entities` 为空，实体名与属性名写回 Source；
- 验证 Schema 抽取失败后 Source MemoryUnit 仍可读取和检索；
- 验证 Schema 属性不进入普通 Dedup；
- 验证标准 EntityLinkService 能从更新后的 Source Unit 建立 EntityRecord 及反向链接；
- 验证 `schema_enabled` 默认关闭，开启后统一 `build_kernel()` 能完成 Source-first
  Schema 写入。

## 已知遗留

1. 当前实体统一完全依赖现有 EntityLinkService 的名称归一化能力，不处理复杂别名或同名消歧；
2. 属性采用 append-only，尚未提供按实体和属性的版本合并；
3. Source 的 `entities` 当前写属性所属实体和属性名，不额外写属性值中提及的其他实体；
4. 关系、图和 Schema 时序检索留待独立特性设计；
5. `SchemaOrchestratingEvolver` 当前尚未接入 `Router`。启用群体记忆归属判定时，Schema
   派生属性仍沿用 Source MemoryUnit 的 Scope；未配置 Router 时不影响 Schema 抽取、属性
   落盘及 Source `entities` 写回。
