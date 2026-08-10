# F06 — 实体关联召回并入 fulltext L2（hash-only 归并）

> 本文描述实体关联记忆召回的**当前业务实现**。召回侧接口契约见 [`docs/specs/S04-retrieval.md`](../../specs/S04-retrieval.md)；写入侧归并规约见 [`docs/specs/S05-construction.md`](../../specs/S05-construction.md) 的 IndexBuilder 段。

## 概述

实体是记忆里比内容更稳定的锚点（人名、项目名、概念名）。实体关联召回的能力是：给定一条命中查询的记忆，把"和它引用同一实体的其它记忆"也召回进来——即按 `entity → linked_memory_ids` 反向索引反查。

本链路**并进 `KeywordRecaller` 的 fulltext L2 召回内部**做"第二批扩展"，不单列召回通道；写入侧维护一张纯 `{entity_text_hash → linked_memory_ids}` 的倒排表，**hash 精确匹配归并，不存 embedding、不做向量语义归并**。整条链路由一个开关 `entity_enabled` 控制，默认关闭。

## 链路总览：一条链路、两个执行点、一个开关

| 端 | 执行点 | 作用 | 开关 |
|---|---|---|---|
| 写入侧 | `HybridIndexBuilder` 组合 `EntityLinkService` → `find_by_entity_text_hash` / `execute_operations` | 随记忆写入维护「entity_text_hash → linked_memory_ids」倒排（hash 精确归并，无向量） | `entity_enabled` |
| 召回侧 | `KeywordRecaller._build` 注入 `entity_store`，`recall` 内部 `_expand_by_entities` | L2 batch 1 候选的 entities 反查拿到 batch 2 关联 unit，中位数锚定打分并入候选 | `entity_enabled` |

`entity_enabled` 默认 **False**。关闭时：写入侧不建索引、召回侧 `KeywordRecaller` 不注入 `entity_store`（`_expand_by_entities` 自动跳过）——两端一致降级，零开销。

> **默认 False 的理由**：实体反向索引是重依赖特性——需 ES entity 索引，且依赖上游在写入前把 `unit.entities` 明文抽好填充。默认关、显式开是更稳的工程姿态。开启时召回侧不对 query 做实体抽取、不拉模型；写入侧只消费 `unit.entities` 明文，为空的 unit 直接跳过不入实体索引。

### 开关配置

`entity_enabled` 在部署配置 `memory_api.globals` 段下。config.yml 是静态文件 COPY 进镜像，运行时由 `__main__.py` 展开 `${VAR:-默认值}` 环境变量后合并到内置默认（见 [`jiuwen_memory/config/defaults.py`](../../../jiuwen_memory/config/defaults.py)）。

| 配置项 | 默认值 | 作用 | 生效时机 |
|---|---|---|---|
| `entity_enabled` | `false` | 实体链路总开关（写入建索引 + 召回 L2 扩展） | 运行时 |

开启步骤：config.yml 置 `entity_enabled: true` + 配 `entity_store` 命名空间（ES hosts/index）+ `constructor`/`recaller.keyword` 两端 params 各引用 `entity_store: default`。召回侧不拉模型、写入侧只消费 `unit.entities` 明文，无 NER 兜底抽取。

## 写入侧

### 实体抽取（职责在 entity 链路之外，前移到上游）

写入侧只消费 `unit.entities` 明文。`unit.entities` 为空（上游未产出 entities）的 unit 直接跳过，不入实体索引——entity 链路本身不做抽取、不回退 NER、不调 extract_batch。`EntityLinkService.__init__` 不收 `extractor` 参数，抽取职责前移到写入前的上游阶段（由该阶段把 entities 抽好填进 `unit.entities`，`EntityLinkService` 只做建链/归并）。

### 实体反向索引投影：L2 文档落盘 entities 明文

[`fulltext_index_builder.py`](../../../jiuwen_memory/construction/index_builder_impl/fulltext_index_builder.py) 的 `_index_metadata` 在 L2 文档 metadata 里写入 `"entities": list(unit.entities)`（[fulltext_index_builder.py:34](../../../jiuwen_memory/construction/index_builder_impl/fulltext_index_builder.py#L34)）。这是召回侧扩展的种子来源——L2 召回命中某条记录后，从该记录 metadata 读出实体明文列表。

**隐私边界**：entities 明文存在 fulltext 文档 metadata 里（fulltext 索引本就存 content 明文，无额外暴露）。entity **反向索引**（`EntityStore`）只存 `entity_text_hash` 不存明文（见「归一化与 hash」），两处隔离口径不同。

### 归一化与 hash：写入/召回两端对齐的命脉

- `EntityNormalizer.normalize`：strip + lower + 空白折叠
- `hash_entity_text(normalized)`：sha256，持久化到 entity 反向索引（ES store 泄露也不暴露实体文本明文），同时是倒排精确匹配 key

**两端对齐**：写入侧 `EntityLinkService._link_group` 对 `unit.entities` 明文 `normalize → hash_entity_text` 建/查索引；召回侧 `KeywordRecaller._expand_by_entities` 对 L2 候选记录的 `metadata['entities']` 明文做**同一套** `normalize → hash_entity_text` 反查。任一端归一化规则偏移，hash 对不上，召回失效。两端都读的是明文（写入侧 `unit.entities` / 召回侧 `metadata['entities']`，同源），不存在抽取器口径分叉。

### 两级归并（`EntityLinkService`）

[`entity_index_builder.py`](../../../jiuwen_memory/construction/index_builder_impl/entity_index_builder.py) 的 `EntityLinkService.link_memories(units)` 是 sync 调用（配合 IndexBuilder 契约），流程：

1. **准入过滤**（`EntityIndexAdmissionPolicy.decide`）：只让 SEMANTIC / CORE / EPISODIC 三个 tier 进索引；WORKING / ARCHIVAL 跳过。
2. **消费 unit.entities 明文**：`unit.entities` 非空时直接构造 `EntityMention`（type 统一 PROPER）；**为空则跳过该 unit，不入实体索引（无 NER 兜底，不调 extract_batch）**。
3. **分组**：按 `(space_id, EntityStoreFilters.key())` 分组，同组共享一次 bulk 查询/写入。
4. **两级归并**（[`_link_group`](../../../jiuwen_memory/construction/index_builder_impl/entity_index_builder.py#L263)）：
   - **hash 精确**：`find_by_entity_text_hash` 按 hash term 查。
   - **命中 → LINK**：追加新 unit_id（去重已有）。
   - **未命中 → INSERT**：直接当新实体建文档，不做向量归并。
5. **bulk 提交**：`execute_operations` 一次提交整组 INSERT/LINK，per-item 粒度返回（`EntityBatchResult`，partial failure 不抛异常）。

### 隔离：space_id routing + actor 单段 term

entity 索引的隔离维度：`space_id`（`space_id_from_scope`）走 ES routing（同 shard 聚簇）+ 文档字段（`term` filter），`actor_id`（`EntityStoreFilters.from_scope`，← scope.user）走 term 过滤。**agent/session 不作隔离维度**——实体是 user 级知识，同 user 下跨 agent、跨 session 共享实体索引。召回侧 `KeywordRecaller._expand_by_entities` 同样用 `space_id_from_scope(scope)` + `EntityStoreFilters.from_scope(scope)` 构造查询参数，与写入侧对齐。

## 召回侧

### L2 内部两批扩展（`KeywordRecaller`）

[`keyword_recaller.py`](../../../jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py)，注册名 `keyword`，`channel()` 返回 `RecallChannel.KEYWORD`（不单列实体通道）。`recall` 流程：

1. **batch 1**（L2 fulltext 召回）：`TextQuery` 经 `FulltextStore.search` 拿 hits + records，`aggregate_to_units` 归并到 unit 粒度（MaxP）。
2. **batch 2**（实体关联扩展，[`_expand_by_entities`](../../../jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py#L105)）：
   - 从 batch 1 records 的 `metadata['entities']` 收集所有明文 → `normalize → hash_entity_text` 去重得 hash 集合。
   - `entity_store.find_by_entity_text_hash(space_id, hashes, filters, limit)` 反查 → 拿 `EntityRecord` 列表，取其 `linked_memory_ids`（**排除已在 batch 1 的 unit_id**，避免重复）。
   - 统计每个 batch 2 unit_id 被 hash 命中的次数 `count`。
3. **中位数锚定打分**：
   - batch 1 命中数 ≥ 3：`score2 = median(batch1 scores) × decay`，`decay = 1 / (1 + 0.001 × (count-1)²)`。
   - batch 1 < 3（含 0）：fallback `score2 = max(batch1 scores) × 0.5`（batch 1 为 0 时 max=0.0，扩展不触发，无影响）。
4. **top_k 截断**：batch 2 默认上限 50（`_ENTITY_EXPANSION_TOP_K`），防极端发散。
5. **合并**（[`_merge_maxp`](../../../jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py#L176)）：batch 1 + batch 2 按 unit_id MaxP（同 unit 取高分），降序返回。

### 计分排序逻辑

`KeywordRecaller.recall` 最终返回的 `list[ScoredUnit]` 按 `score` 降序排列。这个分由两批候选各自打分后 MaxP 合并得出，全程只在本通道内排序，不与其它召回通道交互（跨通道融合由 `PipelineRetriever` 的 Fuser 负责，见 [F04](F04-score-max-fusion.md)）。

#### batch 1（fulltext 命中）打分

`FulltextStore.search` 返回 `ScoredID(id, score, metadata)`，score 由后端给出：

- **ES 后端**（`elasticsearch_fulltext.py`）：`match` 查询的 BM25 相关性得分（`_score`）。
- **内存后端**（`in_memory_fulltext_store.py`）：词重叠率 `hits / len(tokens)`，模拟 BM25 的"命中词占比"。

两种后端绝对分值不同，但语义一致——**分越大越相关**。`aggregate_to_units` 把同 `unit_id` 的多条命中（全文按 unit 建索引时为恒等映射，通常 1:1）按 **MaxP**（取最高分）归并到 unit 粒度，产出 batch 1 的 `list[ScoredUnit]`，记其分值集合为 `S1`。

#### batch 2（实体关联扩展）打分

扩展候选的 `unit_id` 来自 `EntityRecord.linked_memory_ids`（反查命中），**没有自己的 fulltext 命中分**，故以 batch 1 的分值分布为锚点派生：

```
anchor = median(S1)           当 |batch1| >= 3
       = max(S1) × 0.5         当 |batch1| < 3（含空，max=0 时扩展不触发）

decay(count) = 1 / (1 + 0.001 × (count - 1)²)

score2 = anchor × decay(count)
```

- `count` = 该 batch 2 unit_id 被 batch 1 候选的 entities hash 命中的次数（同一 unit 被多个 entity 关联，count 越大衰减越快，抑制高频泛化实体）。
- `median` 取 batch 1 全部分值的中位数（排序后居中值）。
- `decay ≤ 1.0`，保证 batch 2 恒不高于锚点；锚点取中位数而非 max，保证 batch 2 大多落在 batch 1 中段排位。
- fallback 的 `max×0.5`：batch 1 候选不足 3 条时中位数不稳性差，改用最高分的一半兜底，给扩展项一个保守但非零的分值。

#### 合并排序

`_merge_maxp(batch1, batch2)`：两批按 `unit_id` MaxP 归并（batch 2 已排除 batch 1 已有的 unit_id，故实际无冲突，直接拼接），再按 `score` 降序排序返回。batch 2 的 `count` 大于 1 时 `decay<1`，其分数会低于锚点，排在多数 batch 1 命中之后；`count=1` 时 `decay=1`，分数等于锚点，排位与 batch 1 中段命中持平。

最终 top_k 截断在 `PipelineRetriever` 层按调用方传入的 `top_k` 执行，本通道不二次截断 batch 1（batch 2 有自己的 50 上限防发散）。

### 为什么用中位数锚定而非绝对分

`PipelineRetriever` 的 RRF Fuser 融合跨通道时**只用 rank 不用绝对 score**（[`rrf_fuser.py`](../../../jiuwen_memory/retrieval/fuser_impl/rrf_fuser.py)）。所以 batch 2 的绝对分值不重要，重要的是它在 KEYWORD 通道内的相对排位合理。锚定在 batch 1 中位数上，保证扩展记忆既不会压过 batch 1 原生命中（decay ≤ 1.0），也不会分太低被后续 rank 截断丢掉。fallback 的 `max×0.5` 是 batch 1 候选太少时中位数不稳的保护。

**衰减权重**：`1 / (1 + 0.001 × (count-1)²)`，平方衰减。抑制高频泛化实体（一个 entity 关联几百条 unit）淹没精确实体：count≈1 → decay≈1.0；count=100 → decay≈0.09。

### 失败隔离

`entity_store` 查询失败（`find_by_entity_text_hash` 抛异常）→ `_expand_by_entities` 捕获返空 list，batch 2 为空，recall 退化为原 L2 召回，不中断。`entity_store` 未注入（`entity_enabled=false` 或 endpoint 未配）→ `_expand_by_entities` 前置判断直接返空。

### 端到端数值示例

query = `"alice"`，4 个 unit（`[content, entities]`）：

| unit | content | entities | fulltext 命中 | batch1 score |
|---|---|---|---|---|
| u1 | `alice alice alice` | `[Alice, Bob]` | 3/3 | 1.0 |
| u2 | `alice bob coffee` | `[Alice, Bob]` | 1/3 | 0.333 |
| u3 | `alice works` | `[Alice]` | 1/2 | 0.5 |
| u4 | `bob plays piano` | `[Bob]` | 0 | — |

batch 1 = `[u1:1.0, u3:0.5, u2:0.333]`（3 条，触发 median 锚定）。entities 合集 = `{Alice, Bob}` → 反查 `Alice→[u1,u2,u3]`、`Bob→[u1,u2,u4]` → batch 2 = `{u4}`（u1/u2/u3 已在 batch 1，排除），`count(u4)=1`。

`median(S1) = median([1.0, 0.5, 0.333]) = 0.5`，`decay(1) = 1.0` → `score2(u4) = 0.5 × 1.0 = 0.5`。

合并降序结果：

| rank | unit | score | 来源 |
|---|---|---|---|
| 1 | u1 | 1.0 | batch 1 |
| 2 | u3 | 0.5 | batch 1 |
| 3 | u4 | 0.5 | batch 2（实体扩展） |
| 4 | u2 | 0.333 | batch 1 |

u4 通过 Bob 关联被扩展召回（fulltext 未命中 alice），分数锚定在 batch 1 中位数 0.5，与 batch 1 中段命中 u3 持平、高于 batch 1 末位 u2——既未被淹没也未曾压过 batch 1 最高分 u1。此例由 `tests/unit/retrieval/test_keyword_recaller_entity_e2e.py::test_e2e_entity_expansion_brings_in_linked_unit` 验证。

## 验证

### 单元测试

- `tests/unit/construction/test_entity_linker.py`（13）— 两级归并（hash 精确 / INSERT/LINK）、`unlink_memory` 的 UNLINK_UPDATE/DELETE 分类、partial failure、分组隔离、准入过滤。
- `tests/unit/construction/test_hybrid_entity_wiring.py`（5）— `HybridIndexBuilder` 组合 `EntityLinkService` 的 build/update/remove 委托、降级、容错。
- `tests/unit/retrieval/test_query_parser.py`（3）— `SimpleQueryParser` 不建议实体通道。
- `tests/unit/retrieval/test_keyword_recaller_entity_e2e.py`（6）— 端到端：写入带 entities 的 unit → HybridIndexBuilder.build（fulltext L2 落盘 entities + entity_linker 建反向索引）→ KeywordRecaller.recall（batch1 fulltext + batch2 实体扩展 + median 锚定打分 + MaxP 合并）。覆盖扩展召回关联 unit、batch1<3 fallback、无 entities 不扩展、去重、top_k=50 截断。

### 关键场景

| 场景 | 验证方式 | 结果 |
|------|---------|------|
| 写入/召回归一化对齐 | L2 候选 entities 与写入 entities 同源，normalize+hash 一致 | ✅ |
| `entity_enabled=false` 默认关 | KeywordRecaller 不注入 entity_store，`_expand_by_entities` 跳过，退化为原 L2 召回 | ✅ |
| 高频实体不淹没精确实体 | 平方衰减 count=100→0.09，count≈1→1.0 | ✅ |
| entity store 查询失败 | `_expand_by_entities` 捕获返空，batch 2 空，不中断 | ✅ |
| entity store 未注入 | `_expand_by_entities` 前置判断返空 | ✅ |
| 写入侧 entity 失败 | `EntityLinkService` 吞异常 log warning，不中断 build 主链路 | ✅ |

## 已知遗留

1. **`rebuild()` 仍 no-op**：`EntityIndexBuilder.rebuild()` 直接 return None，entity 索引不支持从 KVStore 全量重建。补救路径：存储故障后重新写入累积，或手动调 `link_memories`。

2. **跨语言同实体不并表**：归并只走 hash 精确匹配，"Alice" 与 "爱丽丝" hash 不同，会建成两条记录，召回时各自命中——功能不坏，但关联召回在跨语言场景下召回率下降。若上游抽取阶段对同实体统一了语种则无此问题。

3. **batch 2 打分依赖 batch 1 分值分布**：中位数锚定在 batch 1 scores 上。若 batch 1 分值整体偏低/偏高，batch 2 会随之偏低/偏高——但因 RRF Fuser 只用 rank，绝对值漂移不影响跨通道融合，只影响 KEYWORD 通道内 batch 2 相对 batch 1 的排位。中位数锚定 + decay≤1.0 已保证 batch 2 恒不压过 batch 1 原生命中。
