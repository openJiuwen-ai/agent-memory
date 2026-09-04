# F04 — 通道内 max 归一化 + 通道间取最大值融合（jiuwen_memory/retrieval）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-23 |
| 影响范围 | `jiuwen_memory/retrieval/fuser_impl/score_max_fuser.py`（新增）、`jiuwen_memory/retrieval/fuser_impl/layered_merge.py`（新增）、`jiuwen_memory/retrieval/fuser_impl/rrf_fuser.py`、`jiuwen_memory/retrieval/fuser_impl/weighted_rrf_fuser.py`、`jiuwen_memory/retrieval/fuser_impl/__init__.py`、`jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`（阈值默认值）、`jiuwen_memory/config/defaults.py`、`deploy/docker/online/config.yml`；关联 `jiuwen_memory/storage/fulltext_impl/elasticsearch_fulltext.py`（`text_analyzer`）；文档 `jiuwen_memory/retrieval/AGENTS.md`、`F01-retrieval-impl-design.md`、`F02-retrieval-threshold-topk-design.md` |
| 测试基线 | `uv run --frozen pytest tests/unit/retrieval tests/unit/config/test_online_profile.py tests/unit/storage/fulltext_impl/test_elasticsearch_fulltext.py tests/integration/retrieval -q`（exit 0） |
| 数据来源 | LoCoMo 10-sample 基线日志离线重放（2618 条 gold，其中 2303 条被召回覆盖） |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档融合阶段的可选策略：新增 **CombMAX**（`score_max`，通道内 max 归一化 + 通道间取最大值），
> 保留 RRF 为出厂默认融合器，并为全部 Fuser 实现补齐**分层召回的通道内归并**。同时将相对阈值
> 出厂默认归零。接口契约见 `docs/specs/S04-retrieval.md`；阈值与候选预算的既有设计见
> `F02-retrieval-threshold-topk-design.md`；分层召回整体设计见 `docs/features/common/F01-memory-layer.md` §6。

---

## 背景与薄弱点

### 1. RRF 对单通道候选的结构性压制

RRF 按名次倒数计分（`1/(k+rank)`，k=60），通道命中**数量**的权重高于通道内相似度。双通道候选即使两路均处末位（rank 80+80，得分 `2/140 ≈ 0.0143`）也不低于单通道 rank 10（`1/70 ≈ 0.0143`）。

离线重放显示，被召回的 gold 中单通道命中者进入融合前 20 的比例仅 **1.7%**。反事实验证：缺席通道若以 rank 80 命中，被截断的 gold 中 97% 可越过预算线——决定因素是"另一通道是否缺席"，而非候选自身强度。

### 2. 加法融合无法解决该问题

替换为归一化分数加法（CombSUM）曾作为候选方案实现，实测未能改善：进 top20 由 RRF 的 66.7% 仅升至 67.1%。

原因是**加法同样偏好多通道命中**，只是程度较轻：两路在场时分母为 2，单通道候选上限 `1/2`，双通道上限 `1`。这是 CombSUM 的固有性质，与归一化方式无关——分别以 sigmoid、min-max、max 归一化实测，单通道 gold 进 top20 分别为 6.5%、14.8%、0%，均未解决。

### 3. 固定参数的归一化不具备跨语料通用性

加法方案配套的 BM25 sigmoid 归一化（`1/(1+e^(−steepness×(BM25−midpoint)))`，参数按查询实词数分档）借鉴自 mem0 `utils/scoring.py`。在本项目语料上实测严重错配：

| | BM25 原始分 | 经 midpoint=7.0 归一化 |
|---|---|---|
| p50 | 3.32 | 0.099 |
| p90 | 6.22 | 0.384 |
| p99 | 11.54 | 0.938 |

78.3% 的候选归一化后低于 0.2，中位数 0.099；同批 vector 通道余弦中位数为 0.607。名义等权的两个通道，实际话语权约为 **0.16 : 1**。

根因是 BM25 绝对分数不可跨系统比较——它依赖语料规模与平均文档长度（本项目 unit 中位长度 45 字符）、`k1`/`b` 参数、IDF 变体与分析器实现。任何以固定阈值为中心的归一化都无法跨语料通用。

该方案还需按查询实词数分档，而实词计数依赖英文停用词表，中文查询实际恒定落入最短档位，档位自适应机制失效。

### 4. 分层召回下同通道多路被当作多个信号源

L0/L1 分层召回后同一通道存在多个 recall 实例，而 `KeywordRecaller`/`VectorRecaller` 的 `channel()` 对三层返回同一值——`layer` 是 recaller 上的独立属性，融合层不可见。后果分两类：

- 计分类融合（RRF/加法）重复累加同 unit 的多层命中；
- 归一化类融合按层各自取基准——l0 若仅召回 2 条（最高分 3），其弱候选会被归一化到 1.0，与 l2 最强候选（最高分 20）同级。

两者均为**索引覆盖差异，非相关性差异**：unit 是否具备 layers 取决于 `LayerAnnotator` 是否为其生成分层（`layers_threshold` 以下不生成）。

`F01-memory-layer.md` §6.3 的设计原文为「同 unit_id 多路多层级命中聚合——**同通道取 MaxP，跨通道累加**」，既有实现同通道亦执行累加，偏离该设计。

### 5. 相对阈值默认值放大上述影响

`F02` 初始引入的相对阈值默认为 `min_score_ratio=0.6` /
`min_score_ratio_uncalibrated=0.3`，按当前最高分比例裁剪，与上述偏置叠加时误杀显著。

---

## 决策

### 决策 1：CombMAX 融合（`score_max`）

```
combined(u) = max_c  weight_c × ( score_c(u) / max_score_c )
```

两步，均在融合阶段完成，**不改变召回**：

1. **通道内 max 归一化**：每路以本次召回的最高分为 1.0，其余按比例折算。消除量纲差异而不引入阈值参数——"多少分算高"由本批数据给出，不依赖语料规模、BM25 实现或分析器。
2. **通道间取最大值**：候选取其在各通道归一化分中的最大值，而非求和。

`weight_c` 默认全 1.0（`fusion_channel_weights` 可选覆盖），用于人工压制或抬升某一路，不参与归一化——归一化基准始终是该通道自身的最高分。

**方法出处**：CombMAX 与 CombSUM/CombMNZ 同出 Fox & Shaw (TREC-2, 1994)。后者在"多检索系统检索同一语料"的同质信号场景通常更优；本项目为词法与语义的异质通道且候选集取并集，结论相反。

**实例**（离线重放中的真实查询 `When did Jolene and her partner try scuba diving lessons?`）：

| 候选 | BM25 | cos | 加法融合分 | 名次 | CombMAX 分 | 名次 |
|---|---|---|---|---|---|---|
| 640bdbac | 20.35 | 0.735 | 0.866 | 1 | 1.000 | 1 |
| 0c1300a9 | 20.44 | — | 0.498 | 22 | 1.000 | 2 |
| **2adc945c（gold）** | — | 0.711 | 0.356 | **38** | 0.967 | **3** |
| 6d7b20ca | 9.29 | 0.660 | 0.598 | 12 | 0.898 | 4 |

gold 为全场语义第二强（0.711，最高 0.735），因 keyword 未命中，加法下得 `(0.711+0)/2 = 0.356`，被两路均弱于它的 `6d7b20ca`（`(0.5+0.66)/2 = 0.598`）反超十余位。

### 决策 2：分层召回的通道内归并（`layered_merge`）

融合前统一前处理：同一通道的多路分层结果按 unit 取最高分，归并为一路并按分数重排。
该策略明确以「分越大越相关」为链路不变量；L0/L1/L2 必须使用同类后端与同一
分词/度量配置。`VectorRecaller` 在装配期拒绝 L2 等 lower-is-better 度量，不在
归并阶段自动反转或换算分数。

```python
merge_layered_channels(candidates) -> list[list[ScoredUnit]]
```

**该函数由全部三个 Fuser 实现共用**——分层是链路级变化，不应由单一融合策略各自处理。

对 CombMAX 而言归并**不可省略**：它消除的不仅是重复计数，更是归一化基准按层分裂的问题（见背景 4）。执行顺序必须为「归并 → 归一化 → 取最大值」。

**恒等性保证**：未启用分层时每通道只有一个列表，归并为恒等变换，融合行为不变。

### 决策 3：不引入通道保送机制

加法方案曾配套「各通道 top-K 若未进入融合序前 N 位则提入窗口」的保送机制。CombMAX 下实测无效——每个通道的 top-1 归一化必然为 1.0，天然位于前排：

| | 进 top50 | 进 top20 |
|---|---|---|
| CombMAX 无保送 | 1995 (86.6%) | 1645 (71.4%) |
| CombMAX + 保送 10 | 1992 (86.5%) | 1645 (71.4%) |

故不引入，减少一个待调参数。

### 决策 4：相对阈值出厂默认归零

`min_score_ratio` 与 `min_score_ratio_uncalibrated` 出厂默认统一改为 `0.0`。按最高分比例裁剪会随融合分布变化误杀尾部候选，裁剪职责交由调用方 `top_k`；需要时显式配置。

`F02` 引入的阈值机制本身保留（`apply_threshold` 阶段、`min_score` 绝对门、`min_results` 回填不变），仅调整默认值。四处默认值同步：`defaults.py` 的 retriever params、`PipelineRetriever.__init__` 签名、`_build` 的 `cfg_get` 回退值——三者语义为"漏配时的出厂行为"，必须一致。

---

## 配置

**融合器选型**（命名空间实例 target，非 params）：`fuser.default` 出厂为 `rrf`；
需启用 CombMAX 时显式配 `fuser: {default: score_max}`。三个实现共享同一
`Fuser` 契约与分层归并前处理。

**算子实例 params**（非 globals）：

| 键 | 默认 | 说明 |
|---|---|---|
| `fusion_channel_weights` | `{}`（全 1.0） | 通道权重，用于人工压制/抬升某一路 |
| `min_score_ratio` | `0.0` | 校准路径（走 rerank）相对阈值 |
| `min_score_ratio_uncalibrated` | `0.0` | 未校准路径相对阈值 |

`calibrated` 由是否执行 rerank 决定（`PipelineRetriever` 中 `calibrated=reranked`），非由分数是否归一化决定。

---

## 编排位置（`PipelineRetriever.retrieve`）

```
并行召回（分层开启时同通道多实例）              ← 本次不改动
  ↓
Fuser.fuse
  ├─ merge_layered_channels：同通道归并          ← 决策 2
  ├─ 通道内 max 归一化                           ← 决策 1
  └─ 通道间取最大值                              ← 决策 1
  ↓
截断至精排预算 → 点读复核 → 可选精排 → apply_threshold  ← 决策 4 调整其默认值
  ↓
top_k 截断 → 披露
```

---

## 关联改动：keyword 通道词法归一化（`text_analyzer`）

`ElasticsearchFulltextStore` 增加 `text_analyzer` 参数，在线配置的 L0/L1/L2 三个独立
index 统一启用 `english`（词干化 + 去停用词）。此前 ES text 字段未配
analyzer（standard：不词干化、不去停用词），`attended` 无法匹配 `attend`，
通用疑问词参与打分。离线重放中该改动使 346 条 vector-only 被截断单元里的
102 条获得 keyword 命中。

online 的 L0/L1/L2 向量索引同样使用三个独立 Milvus collection，统一为
1024 维 COSINE。analyzer 仅在索引创建时生效，变更后需删除或更换索引重建。
中文场景选 `ik_max_word`（需 analysis-ik 插件）或内置 `cjk`；置空回退 standard。

---

## 拒绝的方案

| 方案 | 结论 | 依据 |
|---|---|---|
| 归一化分数加法（CombSUM） | 否决 | 相对 RRF 进 top20 仅 +0.4pp；加法固有的多通道偏好无法通过更换归一化消除 |
| BM25 sigmoid 固定参数归一化（mem0 方案） | 否决 | 与本项目分数分布错配（两通道实际话语权 0.16:1）；参数随语料/后端变化；实词分档依赖英文停用词表，中文失效 |
| min-max 归一化 | 否决 | 进 top50 由 85.5% 降至 77.9%；将批内最弱候选强制归零，抹去"弱但有效匹配"与"完全不匹配"的区别 |
| CombMAX + 弱共识加成（次高分 ×0.15） | 暂缓 | gold@20 略高 0.9pp，但单通道 gold@20 由 37.4% 降至 26.0%，且重新引入一个待调参数 |
| 通道 top-K 保送 | 否决 | CombMAX 下实测无效（见决策 3） |
| 放宽精排预算到全池（50 → 127） | 否决 | 被截断候选在通道内排名亦深（75% rank > 30），纯扩池需 rerank 成本 ×2.5。注：小幅上调 `rerank_max` 50→60 是另一回事，已采纳——gold 细碎化后进池率被稀释，离线重放 top50→top65 回收约 5–6pp，成本仅 ×1.2，见 `F02-retrieval-threshold-topk-design.md` 文末「精排预算调整」注记 |
| 语义路定义候选全集、keyword 仅加分（mem0 架构） | 否决 | 该架构下 92 条 keyword-only gold 将被完全排除出候选集 |
| 应用层 spaCy lemmatization | 否决 | 后端仅 ES，引擎级 analyzer 零依赖且一致性由 ES 保证；spaCy 对中文近似恒等 |
| LLM 生成同义词入索引 | 不采纳 | 产品对齐约束；同义转述差异因此成为已知上界 |
| 句级机械切块（RecursiveChunker@100 + MaxP） | 实验否决 | 切片丢失主语与指代，合并单元排名由 17 恶化至 29 |

---

## 验证

**离线重放**（LoCoMo 10-sample 基线日志，2618 条 gold 中 2303 条被召回覆盖；三方案的召回结果完全相同，融合不改变召回）：

| 方案 | 进 top50 | 进 top20 | 单通道 gold 进 top20 |
|---|---|---|---|
| RRF（当前默认） | 1936 (84.1%) | 1536 (66.7%) | 11 (1.7%) |
| 归一化加法 + 保送 | 1969 (85.5%) | 1545 (67.1%) | 42 (6.5%) |
| **CombMAX** | **1995 (86.6%)** | **1645 (71.4%)** | **242 (37.4%)** |

相对 RRF：进 top20 +109 条（+4.7pp），进 top50 +59 条（+2.5pp），单通道 gold 进 top20 +231 条。逐 sample 检查 10 个中 9 个 top20 不回归，唯一回归样本为 −0.7pp（约 2 条）。该批验证跑在 standard analyzer 的基线 run 上，方案不依赖特定分析器。

**单元测试**：`tests/unit/retrieval/test_fuser.py`。覆盖通道内 max 归一化基准、单通道候选不被折价、双通道取最大而非求和、分层先归并后归一化、多层命中无额外增益、非正分通道防除零、配置装配与通道权重。

**集成测试**：`tests/integration/retrieval/test_pipeline_retriever.py`。阈值默认值变更后，原依赖出厂默认的用例改为显式配置（保留阈值机制覆盖），另增用例锁定新默认下不裁剪的行为。

**配置/存储测试**：`tests/unit/config/test_online_profile.py` 锁定 RRF 默认与 L0/L1/L2
持久化分表；`tests/unit/storage/fulltext_impl/test_elasticsearch_fulltext.py` 锁定 analyzer
写入 ES mapping。

> 上述均为**融合层**指标，未经过 recheck、rerank 与 answerer。融合层收益向端到端答对率的传导需在线验证——此前加法方案曾出现"进池提升但 QA 层被 answerer 噪声淹没"的情况。
>
> 分层归并未纳入该批实验：实验时 `LayerAnnotator` 未装配，`unit.layers` 为空，分层链路空转；归并的恒等性保证其不改变该批数据的结论。

---

## 已知局限

1. **通道权重未经调优**。默认全 1.0，未验证词法与语义在本场景下是否应等权。
2. **CombMAX 不奖励多通道共识**。两路均命中在理论上是更强的相关性证据，本方案不予额外加分。实测该信号的价值低于单通道候选被压制的损失，但这是数据集相关的结论。
3. **仅在英文 LoCoMo 上验证**。max 归一化本身不含语言相关假设（这正是相对 sigmoid 方案的改进点），但中文语料的实际收益仍需实测。
