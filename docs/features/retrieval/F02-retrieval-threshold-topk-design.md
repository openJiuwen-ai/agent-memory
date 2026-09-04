# F02 — 检索相关性阈值 + 候选预算重构（jiuwen_memory/retrieval）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-01 |
| 影响范围 | `jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`、`jiuwen_memory/retrieval/recaller_impl/vector_recaller.py`、`jiuwen_memory/retrieval/query_parser_impl/simple_query_parser.py`（配置迁移）、`jiuwen_memory/config/defaults.py`、`jiuwen_memory/storage/vector.py` + `jiuwen_memory/storage/vector_impl/milvus_vector.py`（`score_higher_is_better` 契约）；文档 `docs/features/retrieval/F01-retrieval-impl-design.md` |
| 测试基线 | `pytest tests/unit/retrieval tests/integration/retrieval` 全绿（exit 0；真实后端未连通时按约定 skip） |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档一次检索链路末段的增强：在 `PipelineRetriever` 引入**统一的相关性阈值阶段**、把原
> `top_m` 单旋钮**拆为「召回超采样 + 精排预算」两组**、并在向量通道加**语义前置阈值**。接口契约见
> `docs/specs/S04-retrieval.md`；各实现规约总览见同目录 `F01-retrieval-impl-design.md`（其编排顺序与
> 参数表已随本次同步）。本文聚焦「本次改了什么、为什么这样选」。

---

## 背景与薄弱点

改动前 `PipelineRetriever` 末段为 `融合 → 截断 top_m 预算 → 点读复核 → (可选)精排 → 截断 top_k → 披露`，
存在三处薄弱：

1. **几乎没有阈值过滤**：仅在精排分支内有一行写死的 `survivors = [su for su in survivors if su.score > 0.0]`；
   不精排时完全无阈值。`top_k` 是唯一数量上界，永远回填到 `top_k`（哪怕全不相关），无「无可信结果」信号。
2. **`top_m` 一值两用**：`rerank_top_m`（默认 50）既当每路召回量 `recall_k = max(top_k, top_m)`，又当融合后
   精排预算 `budget = fused[:top_m]`。召回宽度与精排成本被同一个值绑死，且 `top_k > top_m` 时候选池被静默压住。
3. **截断早于时间复核的边界**：融合后先 `fused[:top_m]` 截断，再点读做 event-time / as_of 后置复核；时间窗外的
   高分候选可能占满预算，把时间窗内的相关候选在复核前就截掉（召回不完整）。

---

## 决策

### 决策 1：统一的相关性阈值阶段（`apply_threshold`）

在精排之后、`top_k` 截断之前插入一个统一阈值阶段，替换并归并原写死的 `> 0` 过滤。

**算法**（`survivors` 已含精排分或融合分）：

1. **正分门**：先丢 `score ≤ 0` 的候选，按分降序。**两条路径都生效**：纯 RRF 融合分恒正不受影响；但 `weighted_rrf` 零权重通道产出的 0 分、或自定义 fuser 的非正分候选，在未精排路径也会被丢弃（改动前该路径无过滤会保留在尾部）——视为语义修正：零证据候选不进入结果（有钉行为测试）。
2. **双阈值判定** `_pass(score)`：
   - **绝对** `min_score`：`score < min_score → 砍`；
   - **相对** `ratio`：`score < ratio × 最高分 → 砍`。
3. **前缀截断**：降序 + 阈值单调 → 一旦某条不过，其后皆不过，数「过线前缀」`n_pass` 即可。
4. **兜底回填** `min_results`：`floor = min(min_results, top_k)`，`keep_n = max(n_pass, floor)`，
   `kept = positive[:keep_n]`（不足时从「正分但低于阈值」的候选回填到下限）。
5. 返回 `(kept, detail)`，**不**做 `top_k` 截断（交由原有 `final = survivors[:top_k]`）。

**`calibrated` 门控（防呆）** —— 阈值作用在哪种分上是关键前提：

- 默认装配注入 reranker（`rerank_enabled` 默认 True），绝大多数检索经精排，`survivors` 的分是 reranker
  **校准分**（bge normalize→[0,1]，典型阈 0.3~0.5）→ 绝对阈值有意义。
- 但精排可关（`query.rerank=False` / `rerank_enabled=false`）。未精排时 `survivors` 的分是 **RRF 融合分**
  （`1/(k+rank+1)`，微小恒 > 0、无绝对意义）；若仍套用按 rerank 量纲设的 `min_score`（如 0.4）会**清空结果**。
- 故 `apply_threshold` 接收 `calibrated = reranked` 形参：**绝对 `min_score` 仅在 `calibrated=True` 生效**；
  未校准路径 `abs_min = 0`，只走相对阈值。

**相对阈值分校准/未校准两套配置**：`apply_threshold` 按 `calibrated` 选用
`min_score_ratio` 或 `min_score_ratio_uncalibrated`。两者出厂默认均为 `0.0`（关闭）；
原始方案的 `0.6/0.3` 仅保留为显式配置参考，默认值调整原因见
`F04-score-max-fusion.md` 决策 4。

**原则**：`top_k = 数量上界`、`阈值 = 质量下界`，AND 组合；结果数可 `< top_k`（欠填是正确且期望的，不拿
次阈值结果凑数）。`min_results` 仅从**正分候选**回填；若无正分（例如 logit 型 reranker 对全部候选打负分），
返回空，`min_results` 不兜底（有意为之）。

### 决策 2：候选预算解耦（去 `top_m`，召回宽度 ≠ 精排成本）

把原 `top_m` 单旋钮拆为两组独立配置：

- **召回超采样**（撒宽网，喂融合）：`recall_k = max(top_k × over_fetch_factor, over_fetch_floor)`
  （默认 `4` / `60`，即 mem0 风格 `max(limit×4, 60)`）。
- **精排预算**（控 cross-encoder 成本）：`budget = fused[: max(rerank_max, top_k)]`（本次引入时默认
  `rerank_max=50`，后调整为 `60`，见文末注记；且**永不低于 `top_k`**，避免静默欠召）。

收益：召回撒宽网提升跨通道融合质量，同时**顺带修复背景 3 的截断边界**（时间窗内候选更不易被前排挤掉）；
精排成本由 `rerank_max` 单独封顶，不随召回宽度膨胀。

**召回硬上限 `recall_max`**：`top_k` 无上限（`RetrievalQuery` 默认 10、API 层不裁剪），经 `factor` 放大后
会把后端召回压力放大 `factor` 倍。故加 `recall_k = min(recall_k, recall_max)`（默认 100，0=不限）——融合池
≤ `recall_max × 通道数`，间接封顶下游点读/复核/重排。检索层不信任调用方 `top_k`，此为纵深防御；上层 API
仍应在信任边界处约束 `top_k`。

### 决策 3：向量通道语义前置阈值（`min_similarity`）

在 `VectorRecaller` 内，召回后对命中按 `score >= min_similarity` 过滤，再进融合（默认 `0.0` 关闭）。命名取
`min_similarity`（相似度下限）而非 `min_score`——与 retriever 侧作用在 rerank 校准分上的 `min_score` 家族
**同名不同量纲**，改名避免误配。作用：
融合前先砍掉明显不相关的语义命中，省下游点读/复核/精排预算并降噪。

**边界与防呆**：检索链路统一要求「分越大越相关」的 cosine / IP 语义。
chunk→unit MaxP、分层 MaxP、融合排序和 `min_similarity` 都依赖该方向。
**距离型度量（如 Milvus L2）越小越相关，会静默反转整条链路的相关性**。
打分方向由 **store 接口契约声明**：`VectorStore` 基类提供
`score_higher_is_better() -> bool`（默认 True），距离型后端必须 override 返回
False（Milvus 按 `metric_type` 判定）。`VectorRecaller.__init__` 无条件校验该契约：
store 声明为距离型时直接 `raise ValidationError`，与 `min_similarity` 是否开启无关。
**拒绝而非自动转换**：L2 距离无界、无统一 [0,1] 映射，自动反转会让分数和阈值语义含糊。

---

## 配置键（算子实例 params，非 globals）

这些参数只被单个算子消费（非跨切面），按约定放在各算子的实例 `params` 下，经
`Factory.cfg_get`（只读本实例 params）读取——与 `fuser.k` / `graph_recaller.depth` 同模式。
命名空间已限定作用域，故不带 `retrieval_` 前缀。

**`retriever.default.params`（8 个，`PipelineRetriever` 消费）**

| key | 类型 | 默认 | 含义 |
|---|---|---|---|
| `over_fetch_factor` | int | `4` | 每路召回超采样倍数：`recall_k = max(top_k×factor, floor)` |
| `over_fetch_floor` | int | `60` | 每路召回下限（撒宽网底座） |
| `recall_max` | int | `100` | 每路召回硬上限（0=不限）：`recall_k` 封顶，防超大 `top_k` 经 factor 放大压垮后端 |
| `rerank_max` | int | `60` | 精排预算封顶：`budget = fused[:max(rerank_max, top_k)]`，不低于 `top_k`（本次引入时 `50`，后调整为 `60`，见文末注记） |
| `min_score` | float | `0.0` | 绝对阈值（0=关；**仅校准/已精排路径**生效） |
| `min_score_ratio` | float | `0.0` | 相对阈值（**校准路径**，`score ≥ ratio×最高分`；0=关） |
| `min_score_ratio_uncalibrated` | float | `0.0` | 相对阈值（**未校准路径**；0=关） |
| `min_results` | int | `0` | 欠填兜底下限（0=关；自动夹到 ≤ top_k） |

**`recaller.vector.params`（1 个，`VectorRecaller` 消费）**

| key | 类型 | 默认 | 含义 |
|---|---|---|---|
| `min_similarity` | float | `0.0` | 向量通道语义前置阈值——相似度下限（0=关；仅 cosine/IP，距离型度量装配期拒绝） |

> 删除的键：`retrieval_rerank_top_m`（被 `over_fetch_*` + `rerank_max` 取代）。
> `min_score_ratio` 与 `min_score_ratio_uncalibrated` 当前默认关闭，需要时按场景显式配置；
> 阈值机制、绝对 `min_score` 与 `min_results` 回填保持不变。变更原因见
> `F04-score-max-fusion.md` 决策 4。
> **精排预算调整（2026-07-23）**：`rerank_max` 由 `50` 提高到 `60`。gold 因构建层去重优化
> 而细碎化（单查询 gold 数上升），固定召回深度下进精排池的比例被稀释；离线重放显示精排池
> `top50→top65` 使已召回 gold 进池率提升约 5–6pp。这与本文「拒绝的方案」中否决的「放宽精排
> 预算到全池 127」不矛盾——后者是把预算放到召回上限、成本翻倍，此处是小幅上调（成本仅 ×1.2）；
> 对比见 `F04-score-max-fusion.md`「拒绝的方案」中对全池扩容的否决条目。
> **迁移说明**：这批参数（连同 `query_parser` 的 `sanitize_enabled` / `sanitize_strip_code`）原置于
> `globals`，现按「globals 仅放跨切面参数」的约定移入各算子实例 `params`。

---

## 编排位置（`PipelineRetriever.retrieve`）

```
查询理解 → 前置谓词 → 并行多路召回(recall_k 超采样; 向量通道 min_similarity 前置过滤)
        → 融合 → 截断精排预算(budget_n = max(rerank_max, top_k)) → 点读真源 + 后置复核
        → (可选)重排(记 reranked 标志) → 相关性阈值(apply_threshold, calibrated=reranked)
        → 截断 top_k → 渐进披露
```

轨迹 `threshold` 步 detail 记：`in / positive / passed / backfilled / out / dropped / calibrated /
min_score / min_score_ratio`（`min_score`/`min_score_ratio` 记本次实际生效值，按 `calibrated` 选出）。

---

## 拒绝的方案

- **关键词通道加绝对前置阈值**：被拒。BM25 / 重叠比未校准（ES `_score` 无界、内存重叠比非相关性分），绝对
  阈值跨 query 行为不稳定。语义前置阈值只用于已校准的 cosine 通道；关键词降噪交由融合 + 重排 + 相关性阈值兜住。
  若确需关键词裁剪，只能用相对（比例尺度无关），本次不做。
- **用 ScoreFuser 校准融合分做阈值**：被拒（搁置）。默认装配重排恒开，相关性阈值作用在 reranker 校准分上即可；
  ScoreFuser 对「有重排」链路几无增量（rerank 接管排序/阈值，候选入选用更大超采样更划算）。留作「不重排」降级
  路径的备选——届时把该路径的 `calibrated` 置 True 即可复用本阶段。
- **BM25 分数归一化**：被拒（随 ScoreFuser 搁置）。RRF 按名次融合、丢弃量纲，且重排恒开会重打分覆盖上游分，
  归一化在当前链路是 no-op。它与 ScoreFuser 同生共死，非本次目标。
- **全量重排**：被拒。召回宽度与精排成本解耦（超采样撒网 + `rerank_max` 封顶），平衡召回率与重排成本。
- **per-query 覆盖阈值/预算**：被拒（后续）。不给 `RetrievalQuery` 加 `min_score`/`min_results`/预算字段。

---

## 验证

- 新增 `tests/unit/retrieval/test_threshold.py`：绝对/相对阈值、都关保留正分、全砍空、`min_results` 回填、
  回填夹 top_k、空输入、`calibrated` 防呆、显式配置 `0.6/0.3` 时的相对阈值分路选择、未校准路径正分门钉行为
  （`test_positive_gate_applies_uncalibrated_path`）。
- `tests/unit/retrieval/test_recallers.py`：`min_similarity` 前置过滤（阈值高于最高分→空、低于→保留最相关）；
  store 声明距离型度量时，无论 `min_similarity` 是否开启均在装配期抛
  `ValidationError`（`test_vector_recaller_rejects_lower_is_better_metric`）。
- `tests/integration/retrieval/test_pipeline_retriever.py`：`budget_expands_to_top_k`、`over_fetch_recall_width`
  （factor/floor 双主导）、`recall_max_caps_recall_k`、`retrieval_over_fetch_read_from_config`（含 `recall_max`）；
  `default_config_threshold_active_end_to_end`（出厂默认装配的阈值端到端行为）、
  `rerank_requested_without_reranker_records_skip`（降级轨迹可见）、
  `recall_max_below_floor_warns`（矛盾配置装配期告警）。
- 基线：`pytest tests/unit tests/integration/retrieval` → 371 passed, 1 skipped（redis 无关）；`ruff check` 零告警。
