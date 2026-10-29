# Schema 属性记忆的冲突巩固（CONFLICT_CONSOLIDATE）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-27 |
| 影响范围 | `jiuwen_memory/construction/`（`evolver.py`、`evolver_impl/conflict_consolidator.py`、`evolver_impl/orchestrating_evolver.py`、`evolver_impl/schema_orchestrating_evolver.py`、`extractor_impl/entity_schema_extractor.py`、`schema_prompts.py`）、`jiuwen_memory/api/memory_api_impl/local_support.py`；`docs/specs/S02-memory-api.md`、`docs/specs/S05-construction.md`、`docs/specs/S08-config.md`；`jiuwen_memory/construction/AGENTS.md` |
| 测试基线 | 新增 21 例（`test_conflict_consolidator.py` 17 例、`test_evolver_dedup.py` 新增 4 例）；`pytest tests/unit/construction` → 909 passed |
| Refs | —（issue 号待补） |

## 背景

Schema 模式把每条抽取事实写成一条独立 property MemoryUnit，归属键是
`(schema_entity_id, schema_property_name)`。同一实体的同一属性会随多轮写入持续累积：

- 同一事实被不同 chunk 重复抽到，形成完全重复或仅措辞不同的条目；
- 旧值被新值取代，但新旧两条都还是 `ACTIVE`（属性 Merge 只在写入批次内匹配）；
- 同一事件被拆成互补片段，单看每条都不完整。

检索侧因此会同时看到互相矛盾的属性事实，问答拿到的上下文自相矛盾。现有两条收敛路径
都不覆盖这种形态：

- `CONSOLIDATE` 是「abstractor 抽象升华 + 通用相似度 Dedup」，按单条写入流水工作，
  按文本相似度判断而不按实体属性定位，产物是新的合成记忆而不是收敛既有属性；
- Schema 属性 Merge Planner 只覆盖同一批写入与 source update，不跨轮清理历史累积。

因此需要一条显式触发、按实体属性成簇的巩固链路。算法分四段：成簇 → 关系检测 →
动作规划 → 应用动作，按 Schema 的数据形态重构。

## 算法流程

```text
seed（property + ACTIVE）
  → 两维聚类 (schema_entity_id, schema_property_name)
  → 簇过滤（成员数 ≥ min_cluster_size）
  → settled 检查（全员带 consolidation_done_at 则跳过）
  → 确定性去重（归一化 content 相同 → 留最新，其余退役）
  → LLM#1 关系检测（conflict / duplicate / near_duplicate / complementary）
  → LLM#2 动作规划（create / merge / update / archive）
  → 应用计划（产物 build；旧版置 SUPERSEDED 并移出检索）
  → 打完成标记（FORWARD_ONLY）
```

| 阶段 | 输入 | 边界 / 阈值 | 输出 | 失败处置 | 打完成标记 |
|---|---|---|---|---|---|
| seed 过滤 | 调用方 units | `is_schema_property_unit` 且 `ACTIVE` | seed 集合 | 无 seed → 空跑 | 否 |
| 聚类 | seed 命中的 `(实体, 属性)` 键 | 只展开 seed 命中的键 | 簇 | — | 否 |
| 簇过滤 | 簇 | 成员数 ≥ `conflict_consolidate_min_cluster_size` | 保留簇 | 不足 → 直接跳过 | 否 |
| settled 检查 | 簇 | 全员带 `consolidation_done_at` | 待处理簇 | — | 否 |
| 确定性去重 | 簇内成员 | 归一化 content 相同 | 保留最新、其余退役 | 不依赖 LLM 是否成功 | 退役项不打标 |
| 关系检测 | 簇上下文 | 合法 JSON、id 属于该簇 | issue groups | 异常/不可解析 → 放弃该簇 LLM 动作 | 否 |
| 动作规划 | 每个 issue group | 合法 JSON、同簇 id、merge ≥2 源 | actions | 异常/不可解析 → 放弃该簇 LLM 动作 | 否 |
| 应用计划 | actions | 四类动作 + 字段校验 | 产物、退役集 | 非法动作跳过 | — |
| 落盘与标记 | 产物、退役集、标记集 | 经 IndexBuilder | 见下方顺序 | 不回滚（非事务） | 成功处理后 |

持久化顺序（不可交换）：

1. `IndexBuilder.build` 写产物（create / merge / update），不进入通用相似度去重；
2. 退役集置 `SUPERSEDED` 并写 `t_invalid`，`update(FORWARD_ONLY)` 回写本体，
   `remove(SOFT)` 移出检索；
3. 按 id 从真源重载，只对仍为 `ACTIVE` 的对象写
   `consolidation_run_id` / `consolidation_done_at`，`update(FORWARD_ONLY)` 回写。

`ConflictConsolidator.plan()` 只读真源、不落盘；第 1–3 步由 `OrchestratingEvolver`
执行。各阶段的取舍理由见决策 3–5。

## 决策

### 1. 新增 `EvolveMode.CONFLICT_CONSOLIDATE`，不扩现有 `CONSOLIDATE`

两者语义、输入形态、产物都不同：`CONSOLIDATE` 消费单条写入流水、产出合成记忆；
冲突巩固消费成簇历史、收敛既有记忆。新枚举值对 F04 调度框架零改动即可用（mode 无白名单
假设），入口层经 `_enum_value(EvolveMode, ...)` 自动接受，不需要新增透传分支。分支做在
公用 `OrchestratingEvolver` 上，由装配注入 `ConflictConsolidator`；未注入时该模式
空跑并返回空结果。

### 2. 只巩固 property 记忆

巩固对象是「抽取事实」。Source 记忆是证据链，被产物经 `provenance` 回指，不 merge、
不 archive source 本体。seed 过滤为 `is_schema_property_unit(unit) and lifecycle is ACTIVE`；
不满足的输入直接跳过，没有合法 seed 时模式空跑。

### 3. 两维聚类，键 = `(schema_entity_id, schema_property_name)`

属性记忆的归属是复合单值键，每条记忆恰好落一个簇，簇间天然无重叠。按「单个实体」
一维聚类时，一条记忆可命中多个实体，需要越界裁剪、seed 约束与主体一致性守卫三个补丁；
Schema 的同簇记忆天然同实体、同谓词，这些补丁全部不需要。
动作也不携带实体/属性：归属键由簇继承，不靠 LLM 反推。处理范围由 seed 决定——实体取
seed 命中的实体，且只有 `(实体, 属性)` 键被 seed 命中的簇参与本轮，同实体其它属性不
扩散；规模由 F04 候选源（`window` / `filters`）收窄，聚类层不做簇数截断。

### 4. 两段 LLM：关系检测 → 动作规划

- 第一段把一个簇切成 issue group，类型为
  `conflict / duplicate / near_duplicate / complementary`；
- 第二段对每个 group 产出 `create / merge / update / archive` 四类动作；
- 两段均 `temperature=0`，只输出 JSON；group 的 `memory_ids` 与动作引用的 unit id
  必须属于该簇，越界 id 丢弃；动作缺 content 或缺证据时跳过；
- `merge` 要求至少两个源；`archive` 只退役目标；`update` 生成新版本并让旧版退役；
- 任一段 LLM 调用异常或解析失败，该簇的 LLM 动作整体放弃且**不打完成标记**，由下一次
  调用重试；确定性去重（见算法流程）不受影响，仍会落盘。不引入进程内重试——失败代价是
  一轮全簇重算，重试逻辑交给调用方。

### 5. 现值判据是有效时间，不是现实知识

prompt 明确要求按时间做 latest-wins：`t_event` 优先，其次 `t_message`、`t_ingest`、
`t_valid`（代码侧 `_effective_time`，naive 时间按 UTC 归一）。禁止用现实世界知识或
常识判断哪条为真——存储的事实胜过模型认为的真值。两条冲突记忆时间相同或未知时
不归档：错误归档比保留两条更糟。代码侧只做确定性去重与排序，语义判断全部交给 LLM。

### 6. 落盘复用既有件，退役动作留给 FORGET

`ConflictConsolidator.plan()` 只读真源、调 LLM，产出 `ConsolidatePlan`，不落盘；
落盘全部由 `OrchestratingEvolver` 的既有落盘件完成，避免出现第二套写入链。
产物经 `IndexBuilder.build` 落盘；旧版置 `SUPERSEDED` 并写 `t_invalid`，经
`update(FORWARD_ONLY)` 回写本体、`remove(SOFT)` 移出检索，不物理删除——
二段清理留给 `FORGET`。

### 7. 幂等标记 + 失败重试

- 成功处理的簇内**全部** ACTIVE 成员与本次产物写两个扁平键
  `system_metadata["consolidation_run_id"]` / `["consolidation_done_at"]`；
  只写簇内 seed 会漏掉本轮展开拉入的老成员，簇永远无法 settled。
- 簇内每条 ACTIVE 记忆都带 `consolidation_done_at` 即视为已巩固，后续轮次跳过、
  不调 LLM；新写入或未标记的记忆会让簇重新 unsettled。
- 标记前按 id 从真源重载，只给仍为 ACTIVE 的对象打标记：入参 seed 可能是陈旧对象，
  直接回写会覆盖本轮 UPDATE 内容或复活已 SUPERSEDED 的旧版。
- 标记集合 = `processed_unit_ids ∪ 产物 id − 退役 id`；标记经 `update(FORWARD_ONLY)`
  只回写本体，不刷新检索索引。

## 拒绝的方案

| 拒绝的方案 | 理由 |
|---|---|
| 复用 `CONSOLIDATE` 并加开关 | 语义混叠：抽象升华与簇内冲突收敛的输入、产物、落盘路径都不同，既有调用方、测试和文档会被一并污染 |
| 按「单个实体」一维聚类 | Schema 下同一实体的 occupation 与 hobby 会被混进一簇，丢失属性维度；且一条记忆可命中多个实体，引入簇重叠与主体一致性守卫 |
| 用 `EntityStore` 反查同实体属性 | Schema 下 EntityStore 只登记 Source（property `entities=[]`），实测反查 178 source / 0 property |
| 让 LLM 用现实知识判断哪条为真 | 会按模型认知改写用户真实记忆；存储事实必须胜过模型的世界知识 |
| 物理删除被归档记忆 | 破坏非破坏式血缘与 as-of 回溯；归档 = `SUPERSEDED`，清理归 `FORGET` |
| 做簇数截断 / 每簇条数上限 | 静默截断会让部分簇永远轮不到巩固且日志不可见；规模由候选源收窄 |
| 独立 Evolver 实现或 Schema 私有分支 | 规划器直接落盘、独立 `EvolverProducer`、分支只做在 `SchemaOrchestratingEvolver` 内等形态都违反「落盘统一经 IndexBuilder」或复制出第二套写入链；公用分支 + 可空注入让两个 Evolver 实现不分叉 |

## 验证

### 单元测试

- `tests/unit/construction/test_conflict_consolidator.py`（17 例）：两维聚类无重叠、
  seed 范围不外溢、单条簇过滤、确定性去重、关系检测与动作规划解析、越界 id 丢弃、
  LLM 解析失败时计划为空、settled 簇跳过、退化取数、混用时区排序、产物清空派生字段。
- `tests/unit/construction/test_evolver_dedup.py`（23 例，其中 4 例覆盖巩固落盘）：
  产物经 IndexBuilder 落盘（替身只写正排，不覆盖派生索引）、被退役 seed 不打标、
  计划为空（失败簇）不打标、本轮展开拉入的非 seed 成员也打标。
- 命令与结果：`.venv/bin/python -m pytest tests/unit/construction -o addopts=-ra -q`
  → 909 passed。

### 评测

MemoryAgentBench FactConsolidation（add → consolidate → 不追加记忆重评），模型 glm-5.2，
`chunk_chars=4096`、`top_k=50`、`substring_exact_match`，评测侧把巩固输出上限覆盖为
8192：

| 子集 | BEFORE | AFTER | Δ | ACTIVE property 记忆数（前 → 后） | 答案变化 |
|---|---:|---:|---:|---:|---:|
| mh_6k | 0.34 | 0.47 | **+0.13** | 501 → 395（−21.2%） | 40/100 |
| sh_6k | 0.99 | 0.98 | −0.01 | 454 → 358（−21.1%） | 2/100 |

收益与冲突密度正相关：mh_6k（多跳、高冲突）有 40/100 的答案因巩固而改变，Δ=+0.13；
sh_6k（单跳、低冲突）基线已近天花板，只有 2/100 的答案变化。mh_6k 另有一轮复跑为
0.28 → 0.41（Δ=+0.13，475 → 378，43/100 答案变化），与本次 Δ 一致，绝对分差异来自
LLM 抽取与问答的随机性。三个 run 的记忆数降幅都在 20%–21% 之间，说明巩固工作量稳定，
得分差异来自数据本身的冲突结构。

## 已知遗留

1. 只处理 seed 触及的 `(实体, 属性)` 键，没有按 Scope 全量扫描的后台入口；覆盖度取决于
   F04 候选源能否把相关记忆带进本轮候选。
2. `ConflictConsolidator.max_tokens` 默认 2048，大簇的 JSON 可能被截断；本次评测在调用侧
   覆盖为 8192，尚未按簇大小自适应。
3. 簇间与簇内两段 LLM 全部串行，单簇一次调用；成本与延迟随簇大小和 issue group 数线性
   增长，未做批处理或并发。
4. 没有 Scope 内互斥：并发两次巩固可能各自产出新版本。幂等标记只解决顺序重跑，不解决
   并发。
5. 外部 `update` 不清巩固标记：内部改写成新版本不受影响，调用方手动改写已巩固记忆时，
   该簇可能不再巩固它。
6. 跨 Scope 不合并；同一实体分布在不同 Scope 时各自巩固，且非 Schema 数据（无
   `schema_property_name`）不支持、直接空跑。
