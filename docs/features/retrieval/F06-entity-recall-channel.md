# F06 — 实体关联召回并入 fulltext L2（hash-only 归并）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-03 |
| 影响范围 | jiuwen_memory/storage/、jiuwen_memory/construction/、jiuwen_memory/retrieval/、docs/specs/S05-construction.md、docs/specs/S06-storage.md |
| 测试基线 | 相关定向测试 176 passed |

> 本文描述实体关联记忆召回的**当前业务实现**。召回侧接口契约见 [`docs/specs/S04-retrieval.md`](../../specs/S04-retrieval.md)；写入侧归并规约见 [`docs/specs/S05-construction.md`](../../specs/S05-construction.md) 的 IndexBuilder 段。

## 概述

实体是记忆里比内容更稳定的锚点（人名、项目名、概念名）。实体关联召回的能力是：给定一条命中查询的记忆，把"和它引用同一实体的其它记忆"也召回进来——即按 `entity → linked_memory_ids` 反向索引反查。

本链路**并进 `KeywordRecaller` 的 fulltext L2 召回内部**做"第二批扩展"，不单列召回通道；写入侧维护一张纯 `{entity_text_hash → linked_memory_ids}` 的倒排表，**hash 精确匹配归并，不存 embedding、不做向量语义归并**。整条链路由一个开关 `entity_enabled` 控制，默认关闭。

## 链路总览：一条链路、两个执行点、一个开关

| 端 | 执行点 | 作用 | 开关 |
|---|---|---|---|
| 写入侧 | `HybridIndexBuilder` 从 `Storage.entity_port()` 获取实体端口，组合 `EntityLinkService` → `find_by_entity_text_hash` / `execute_operations` | 随记忆写入维护「entity_text_hash → linked_memory_ids」倒排（hash 精确归并，无向量） | `entity_enabled` |
| 召回侧 | `KeywordRecaller._build` 从同一个 `Storage.entity_port()` 获取实体端口，`recall` 内部 `_expand_by_entities` | L2 batch 1 候选的 entities 反查拿到 batch 2 关联 unit，中位数锚定打分并入候选 | `entity_enabled` |

`entity_enabled` 默认 **False**。关闭时：写入侧不建索引、召回侧 `KeywordRecaller` 不从 Storage 获取实体端口（`_expand_by_entities` 自动跳过）——两端一致降级，零开销。

> **默认 False 的理由**：实体反向索引是重依赖特性——需 ES entity 索引，且依赖上游在写入前把 `unit.entities` 明文抽好填充。默认关、显式开是更稳的工程姿态。开启时召回侧不对 query 做实体抽取、不拉模型；写入侧只消费 `unit.entities` 明文，为空的 unit 直接跳过不入实体索引。

### 开关配置

`entity_enabled` 在部署配置 `memory_api.globals` 段下。config.yml 是静态文件 COPY 进镜像，运行时由 `__main__.py` 展开 `${VAR:-默认值}` 环境变量后合并到内置默认（见 [`jiuwen_memory/config/defaults.py`](../../../jiuwen_memory/config/defaults.py)）。

| 配置项 | 默认值 | 作用 | 生效时机 |
|---|---|---|---|
| `entity_enabled` | `false` | 实体链路总开关（写入建索引 + 召回 L2 扩展） | 运行时 |

开启步骤：config.yml 置 `entity_enabled: true`，配置 `entity_store` 命名空间（ES hosts/index），并在 `storage.default.params` 中引用 `entity_store: default`。`constructor` 和 `recaller.keyword` 只引用 `storage: default`，不再各自注入底层 EntityStore。召回侧不拉模型、写入侧只消费 `unit.entities` 明文，无 NER 兜底抽取。

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
3. **分组**：按完整五维 `Scope` 分组，同组共享一次 bulk 查询/写入。旧的 `space_id + filters` 后端参数只由 Storage 内部兼容适配器生成，上层不感知。
4. **两级归并**（[`_link_group`](../../../jiuwen_memory/construction/index_builder_impl/entity_index_builder.py#L263)）：
   - **hash 精确**：`find_by_entity_text_hash` 按 hash term 查。
   - **命中 → LINK**：追加新 unit_id（去重已有）。
   - **未命中 → INSERT**：直接当新实体建文档，不做向量归并。
5. **bulk 提交**：`execute_operations` 一次提交整组 INSERT/LINK，per-item 粒度返回（`EntityBatchResult`，partial failure 不抛异常）。

### 隔离：完整五维 Scope（后端可内部 routing）

entity 索引的公开隔离契约是完整五维 `Scope`：`org`、`space`、`user`、`agent`、`session`。后端可以把 `scope.space` 或派生的 `space_id` 用作 ES routing，并把 Scope 维度写入文档字段；但 routing 只用于定位，不能扩大可见范围。兼容旧后端所需的 `space_id + EntityStoreFilters` 由 Storage 内部适配器生成，`EntityIndexBuilder` 和 `KeywordRecaller` 不直接构造这些参数。因此，同 org 下不同 space、同 user 下不同 agent/session 的实体都不会串读，除非调用方显式使用同一个 Scope。

## 召回侧

### L2 内部两批扩展（`KeywordRecaller`）

[`keyword_recaller.py`](../../../jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py)，注册名 `keyword`，`channel()` 返回 `RecallChannel.KEYWORD`（不单列实体通道）。`recall` 流程：

1. **batch 1**（L2 fulltext 召回）：`TextQuery` 经 `FulltextStore.search` 拿 hits + records，`aggregate_to_units` 归并到 unit 粒度（MaxP）。
2. **短路**：`batch1 ≥ top_k` 时跳过 batch 2——扩展打分 `anchor × boost`（boost ≤ cap = max/median）注定 ≤ batch 1 最高分，挤不进 top_k 高分区，反查 + 点读 + 过滤属无用功。
3. **batch 2**（实体关联扩展，[`_expand_by_entities`](../../../jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py#L117)）：
   - 从 batch 1 records 的 `metadata['entities']` 收集所有明文 → `normalize → hash_entity_text` 去重得 hash 集合。
   - `storage.entity_port().find_by_entity_text_hash(scope, hashes, limit=...)` 反查 → 拿 `EntityRecord` 列表，取其 `linked_memory_ids`（**排除已在 batch 1 的 unit_id**，避免重复）。
   - 聚合 `raw_contrib: unit_id → Σ idf(df_e)`，`df_e = len(er.linked_memory_ids)` 是该实体关联的记忆数（**真正的文档频率**，user-scope 内）。
4. **中位数锚定打分**：
   - batch 1 命中数 ≥ 3：`anchor = median(batch1 scores)`。
   - batch 1 < 3（含 0）：fallback `anchor = max(batch1 scores) × 0.5`（batch 1 为 0 时 max=0.0，扩展不触发，无影响）。
   - `cap = max(batch1 scores) / anchor`（≥1），`boost = min(Σ idf, cap)`，`score2 = anchor × boost`。
5. **预筛 + 点读过滤**（生产过滤先于 top_k，S04:40 不变量）：
   - 按 `raw` 相关分预筛到 `_ENTITY_EXPANSION_TOP_K=20`（纯内存无 IO），再点读这 ≤20 个真源做 `is_retrieval_candidate` 过滤（lifecycle×as_of / event-time / scalar_filters），剔除无效候选。点读量从"反查拉回全部 id（最坏上千）"压到 ≤20。
6. **合并 + 截断**（[`_merge_maxp`](../../../jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py#L241)）：batch 1 + batch 2 按 unit_id MaxP（同 unit 取高分）降序，**recall 末尾 `[:top_k]` 截断**，严格遵守 `Recaller.recall` 契约（≤ top_k）。

### 计分排序逻辑

`KeywordRecaller.recall` 最终返回的 `list[ScoredUnit]` 按 `score` 降序排列。这个分由两批候选各自打分后 MaxP 合并得出，全程只在本通道内排序，不与其它召回通道交互（跨通道融合由 `PipelineRetriever` 的 Fuser 负责，见 [F04](F04-score-max-fusion.md)）。

#### batch 1（fulltext 命中）打分

`FulltextStore.search` 返回 `ScoredID(id, score, metadata)`，score 由后端给出：

- **ES 后端**（`elasticsearch_fulltext.py`）：`match` 查询的 BM25 相关性得分（`_score`）。
- **内存后端**（`in_memory_fulltext_store.py`）：词重叠率 `hits / len(tokens)`，模拟 BM25 的"命中词占比"。

两种后端绝对分值不同，但语义一致——**分越大越相关**。`aggregate_to_units` 把同 `unit_id` 的多条命中（全文按 unit 建索引时为恒等映射，通常 1:1）按 **MaxP**（取最高分）归并到 unit 粒度，产出 batch 1 的 `list[ScoredUnit]`，记其分值集合为 `S1`。

#### batch 2（实体关联扩展）打分

扩展候选的 `unit_id` 来自 `EntityRecord.linked_memory_ids`（反查命中），**没有自己的 fulltext 命中分**，故以 batch 1 的分值分布为锚点派生。打分量用 **IDF**（实体文档频率），不用查询命中数——见下方「IDF 衰减 vs 旧 decay」的修正说明。

```
raw_u = Σ_{e ∈ E_u} idf(df_e)         df_e = len(er.linked_memory_ids)
       idf(df) = 1 / log(1 + df)      df=1→1.44, df=10→0.42, df=1000→0.145

anchor = median(S1)            当 |batch1| >= 3
       = max(S1) × 0.5          当 |batch1| < 3（含空，max=0 时扩展不触发）

cap = max(S1) / anchor         （≥1；保证 score2 ≤ max(S1)）
boost = min(raw_u, cap)
score2 = anchor × boost
```

- `df_e` = `len(EntityRecord.linked_memory_ids)`，该实体关联的记忆数（**真正的文档频率**，user-scope 内）。df 越大（实体越泛化）→ idf 越小 → 抑制高频泛化实体。
- `raw_u` = unit u 被命中的所有实体的 idf 之和。多实体命中 → Σ 累加 → 提权（方向修正）。
- `cap` = batch 1 最高分 / 锚点，恒 ≥ 1。`boost = min(raw_u, cap)` 保证 `score2 = anchor × boost ≤ anchor × cap = max(S1)`，**严格不压过 batch 1 最高分**。
- `median` 取 batch 1 全部分值的中位数。锚点取中位数而非 max，保证 batch 2 大多落在 batch 1 中段排位。
- fallback 的 `max×0.5`：batch 1 候选不足 3 条时中位数不稳性差，改用最高分的一半兜底，给扩展项一个保守但非零的分值。

#### IDF 衰减 vs 旧 decay（修正说明）

旧实现用 `decay(count) = 1 / (1 + 0.001 × (count-1)²)`，`count` 是"该 unit 被多少个**查询命中**的实体关联"（查询命中数 qh），**不是实体文档频率**。两个问题：

1. **count 量错了**：`count` 只统计查询命中，不统计实体全局文档频率。检视者例子：一个实体关联 1000 条记忆，查询只命中它一个实体，每条候选的 count 都是 1，decay=1.0，**零抑制**——1000 条候选全以满中位数分拉回，RRF 挤进 top_k 中段污染结果。
2. **衰减方向反了**：count 大（命中的查询实体多）应更相关（像 TF），却反向衰减压权。

新实现用 `idf(df)`（df 是实体文档频率）+ Σ 累加，同时修两个问题：高频泛化实体 df 大 → idf 小 → 抑制；多实体命中 → Σ 累加 → 提权。

#### 合并排序

`_merge_maxp(batch1, batch2)`：两批按 `unit_id` MaxP 归并（batch 2 已排除 batch 1 已有的 unit_id，故实际无冲突，直接拼接），按 `score` 降序排序。**截断不在此方法**——`recall` 末尾 `result[:top_k]` 截断，严格遵守 `Recaller.recall` 契约（≤ top_k）。

### 为什么用中位数锚定而非绝对分

`PipelineRetriever` 的 RRF Fuser 融合跨通道时**只用 rank 不用绝对 score**（[`rrf_fuser.py`](../../../jiuwen_memory/retrieval/fuser_impl/rrf_fuser.py)）。所以 batch 2 的绝对分值不重要，重要的是它在 KEYWORD 通道内的相对排位合理。锚定在 batch 1 中位数上，保证扩展记忆既不会压过 batch 1 原生命中（`boost ≤ cap → score2 ≤ max(S1)`），也不会分太低被后续 rank 截断丢掉。fallback 的 `max×0.5` 是 batch 1 候选太少时中位数不稳的保护。

**IDF 抑制**：`idf(df) = 1/log(1+df)`。高频泛化实体（df=1000，关联千条记忆）idf≈0.145，单实体命中的候选 score2 = anchor × 0.145，强抑制；低频精确实体（df=1）idf=1.44，给高分。

### 失败隔离

**召回侧**：Storage 的实体端口查询失败（`find_by_entity_text_hash` 抛异常）→ `_expand_by_entities` 捕获返空 list，batch 2 为空，recall 退化为原 L2 召回，不中断。实体端口未声明（`entity_enabled=false` 或 endpoint 未配）→ `_expand_by_entities` 前置判断直接返空。

**写入侧**：
- `EntityLinkService._link_group` 的 hash 精确查询失败（`find_by_entity_text_hash` 抛异常）→ **整组 abort + 计 failed_count**，不降级成"全 INSERT"——查不到不等于不存在，误 INSERT 会造重复实体文档（同 hash 多条 `EntityRecord`，召回侧 `find_by_entity_text_hash` 命中多条，`raw_contrib` 累加翻倍，打分失真）。下次同实体写入时查询恢复 → hash 命中 → LINK，自愈。
- `EntityIndexBuilder.build` 的 `link_memories` 失败 → **不阻断 write**（entity 是增强层，fulltext/vector 已落盘、真源 KV 已在前置 write 落盘，失败不丢数据），但 **error 级别可见** + 带回 `EntityLinkResult` 的 failed_count 便于告警与对账。下次同实体写入时 hash 命中可自愈。

### 一致性窗口与恢复（弱一致性规格）

entity 索引是最终一致，非原子。明确以下窗口与恢复方式：

| 场景 | 窗口 | 影响 | 恢复 |
|------|------|------|------|
| 精确查询失败 | 查询失败期间写入的 group | 该 group 整组 abort，不造重复 | 下次同实体写入时查询恢复 → LINK 自愈 |
| `link_memories` 整体失败 | 该批 unit 的实体链接未建 | entity 索引 stale，召回侧扩展召回不到关联记忆 | error 日志告警 + 下次同实体写入自愈 |
| 并发首次写入（read-then-insert 竞争）| 两并发 write 同时查未命中 → 各自 INSERT | 同 hash 可能造两条文档 | 召回侧多命中，`raw_contrib` 翻倍，被 IDF cap + top_k 吸收；LINK 的 painless 脚本幂等去重，同文档内不重复加 unit_id |
| update 先 unlink 再 link | link 半段失败 | 该 unit 的实体链接被清空 | 下次同 update/write 时重新 link 自愈 |

**与 fulltext/vector 一致**：fulltext/vector 的 `update` 也是"先删后建"（`_store.delete` + `insert` / `_vector_store.delete` + `build`），同构的弱一致性；`rebuild()` 全栈都是 `return None`（索引与真源同生命周期，无独立重建路径），entity 的空 rebuild 是工程惯例非 entity 独有。真源 KV 的 `add`/`update` 也是逐条 insert 非原子。全栈不保证原子性，统一靠"下次写入自愈"的最终一致模型。

### 端到端数值示例

query = `"alice"`，4 个 unit（`[content, entities]`）：

| unit | content | entities | fulltext 命中 | batch1 score |
|---|---|---|---|---|
| u1 | `alice alice alice` | `[Alice, Bob]` | 3/3 | 1.0 |
| u2 | `alice bob coffee` | `[Alice, Bob]` | 1/3 | 0.333 |
| u3 | `alice works` | `[Alice]` | 1/2 | 0.5 |
| u4 | `bob plays piano` | `[Bob]` | 0 | — |

batch 1 = `[u1:1.0, u3:0.5, u2:0.333]`（3 条，触发 median 锚定）。entities 合集 = `{Alice, Bob}` → 反查 `Alice→[u1,u2,u3]`、`Bob→[u1,u2,u4]`（两者 df 都=3）→ batch 2 = `{u4}`（u1/u2/u3 已在 batch 1，排除）。u4 只被 Bob 关联，`raw_u4 = idf(df=3) = 1/log(4) = 0.7214`。

`median(S1) = median([1.0, 0.5, 0.333]) = 0.5`，`max(S1) = 1.0`，`cap = 1.0/0.5 = 2.0`，`boost = min(0.7214, 2.0) = 0.7214` → `score2(u4) = 0.5 × 0.7214 = 0.361`。

合并降序结果：

| rank | unit | score | 来源 |
|---|---|---|---|
| 1 | u1 | 1.0 | batch 1 |
| 2 | u3 | 0.5 | batch 1 |
| 3 | u2 | 0.333 | batch 1 |
| 4 | u4 | 0.361 | batch 2（实体扩展） |

u4 通过 Bob 关联被扩展召回（fulltext 未命中 alice）。IDF 打分下 u4 的分（0.361）低于 batch 1 中位数锚点（0.5）但仍高于 batch 1 末位 u2——既未被淹没也未曾压过 batch 1 最高分 u1（cap 保证 `score2 ≤ max(S1) = 1.0`）。此例由 `tests/unit/retrieval/test_keyword_recaller_entity_e2e.py::test_e2e_entity_expansion_brings_in_linked_unit` 验证。

## 验证

### 单元测试

- `tests/unit/construction/test_entity_linker.py`（18）— 两级归并（hash 精确 / INSERT/LINK）、`unlink_memory` 的 UNLINK_UPDATE/DELETE 分类与跨 user 隔离、partial failure、分组隔离、准入过滤、查询失败不降级 INSERT、build 失败可见不阻断。
- `tests/unit/construction/test_hybrid_entity_wiring.py`（5）— `HybridIndexBuilder` 组合 `EntityLinkService` 的 build/update/remove 委托、降级、容错。
- `tests/unit/retrieval/test_query_parser.py`（3）— `SimpleQueryParser` 不建议实体通道。
- `tests/unit/retrieval/test_keyword_recaller_entity_e2e.py`（13）— 端到端：写入带 entities 的 unit → HybridIndexBuilder.build（fulltext L2 落盘 entities + entity_linker 建反向索引）→ KeywordRecaller.recall（batch1 fulltext + batch2 实体扩展 + IDF 打分 + MaxP 合并 + top_k 截断）。覆盖扩展召回关联 unit、batch1<3 fallback、无 entities 不扩展、去重、top_k 截断、batch1≥top_k 短路、IDF 抑制高频实体、点读量 ≤20、S04:40 过滤先于截断。

### 关键场景

| 场景 | 验证方式 | 结果 |
|------|---------|------|
| 写入/召回归一化对齐 | L2 候选 entities 与写入 entities 同源，normalize+hash 一致 | ✅ |
| `entity_enabled=false` 默认关 | KeywordRecaller 不获取 Storage Entity 端口，`_expand_by_entities` 跳过，退化为原 L2 召回 | ✅ |
| 高频实体不淹没精确实体 | IDF 抑制：df=1000→idf≈0.145，df=1→1.44；低频实体关联记忆分 > 高频实体关联记忆分 | ✅ |
| top_k 契约 | recall 末尾 `[:top_k]` 截断，batch1+batch2 合并后 ≤ top_k | ✅ |
| batch1≥top_k 短路 | `find_by_entity_text_hash` 调用次数=0，不做无用扩展 | ✅ |
| 生产过滤先于 top_k（S04:40） | batch2 预筛 20 后点读过滤，无效候选（SUPERSEDED）被剔，有效候选保留 | ✅ |
| 点读减量 | `storage.get` 的 unit 数 ≤ `_ENTITY_EXPANSION_TOP_K=20` | ✅ |
| Storage Entity 端口查询失败（召回侧） | `_expand_by_entities` 捕获返空，batch 2 空，不中断 | ✅ |
| Storage Entity 端口查询失败（写入侧） | `_link_group` 整组 abort 计 failed，不降级 INSERT 造重复 | ✅ |
| Storage Entity 端口未声明 | `_expand_by_entities` 前置判断返空 | ✅ |
| 写入侧 entity 失败 | `build` 不阻断 write，error 级别可见 + failed_count 对账 | ✅ |

## 已知遗留

1. **`rebuild()` 仍 no-op**：`EntityIndexBuilder.rebuild()` 直接 return None——但这是**全栈惯例**：fulltext/vector/hybrid 的 `rebuild()` 都是 `return None`（注释"索引与真源同生命周期，无独立重建路径"），不是 entity 独有。补救路径：存储故障后重新写入累积自愈，或手动调 `link_memories`。

2. **跨语言同实体不并表**：归并只走 hash 精确匹配，"Alice" 与 "爱丽丝" hash 不同，会建成两条记录，召回时各自命中——功能不坏，但关联召回在跨语言场景下召回率下降。若上游抽取阶段对同实体统一了语种则无此问题。

3. **batch 2 打分依赖 batch 1 分值分布**：中位数锚定在 batch 1 scores 上。若 batch 1 分值整体偏低/偏高，batch 2 会随之偏低/偏高——但因 RRF Fuser 只用 rank，绝对值漂移不影响跨通道融合，只影响 KEYWORD 通道内 batch 2 相对 batch 1 的排位。中位数锚定 + `boost ≤ cap → score2 ≤ max(S1)` 已保证 batch 2 恒不压过 batch 1 原生命中。
