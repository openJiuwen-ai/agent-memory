# F09 — 检索次级排序：带内按重要度与时效性 tie-break

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-29 |
| 影响范围 | jiuwen_memory/retrieval/{base.py,bootstrap.py,retriever_impl/pipeline_retriever.py,fuser_impl/secondary_scorer/}，jiuwen_memory/config/defaults.py，docs/specs/S04-retrieval.md |
| 测试基线 | 全量 6219 passed / 86 skipped / 4 failed（Python 3.11.6，本地开发机（aarch64））；4 例失败都是本机缺 `watchdog` 库，改动前基线同款，无新增失败。skip 比早先记录的 31 例多，是环境差异：本机缺 elasticsearch、nano-graphrag、torch 等可选库，Milvus、Redis、PostgreSQL 测试服务未启动，相应用例跳过。ruff 0.16.8 无新增告警 |
| 关联特性 | docs/features/construction/F10-importance-and-source-attribution.md（重要度的产出方） |

## 背景

长期运行后同等相关的记忆很多。业务期望「越新越优先」「越重要越优先」，但有一条硬约束：
**不能让「不相关但新且重要」反超「相关但旧且不重要」**——相关性必须是第一主键，时效性与
重要度只能做次要键。

改动前排序完全基于相关性：召回各路打分 → Fuser 融合（RRF 等）→ 可选 Reranker 用校准分
替换融合分 → 阈值裁剪。时间只做硬过滤（`as_of` / `time_from` / `time_to`），
`system_metadata.importance` 虽已投影为可过滤索引字段，但 Fuser 与 Reranker 都不读它。

用户最初的想法是「相关性得分一致时先看重要度、再看时效性」。方向对，但精确相等几乎不会
发生（RRF 分跨路累加、Reranker 校准分是连续浮点），按字面实现等于永不触发；而相关性只差
0.001 这种噪声级差距时次要键又介入不了。

## 决策

1. **分带 tie-break**。候选已按相关性降序；以带首分为锚，分数 ≥ 带首分 − `score_band`
   （闭区间）的候选同带，否则开新带并以该候选为新带首。带宽因此恒 ≤ `score_band`，不会
   因相邻小落差首尾相连地无限延伸。带间严格保持相关性顺序，带内按次要键稳定降序（同分
   保持原相关性顺序）。
2. **带内次要键 = `importance_weight·importance + recency_weight·recency`**。两维都在 [0,1]，
   线性组合让高重要度老记忆与低重要度新记忆可以互补；两个权重是带内相对权重，不要求
   和为 1，等比缩放不改变排序。
3. **时效性用软衰减 `1/(1+age_days/recency_tau_days)`，基于 `Temporal.t_ingest`**。永不为零、
   单调递减；`t_ingest` 是记忆写入时间，各产出点都会盖时间戳，缺失率最低。缺失时取 0，
   未来时间（时钟漂移）裁剪为 1，无时区的时间按 UTC 解释。
4. **重要度在读取侧按数值解析**。`system_metadata.importance` 存的是浮点字符串，读取时
   `float()` 解析后再比较，绕开字符串字典序在位数不同时失真（`"0.5"` > `"0.95"`）；缺失、
   非数值、NaN/Inf、布尔取 0.5，越界裁剪 [0,1]。
5. **只在精排路径（`calibrated=True`）生效**。未精排时分数是 RRF 融合分（约 1/61 量级、分布
   密集），绝对带宽 0.05 会把全部候选并成一带，次要键淹没相关性。未精排时编排层不调用算子，
   直接记 `skipped: uncalibrated`；算子内部的同款门控保留作第二道防线，业务算子不遵守契约
   也不会在未精排路径上重排。
6. **位置：阈值之后、`top_k` 之前**。实际链路是 rerank → rollup（MaxP，父继承子分）→
   threshold → secondary_rank → top_k。放在阈值之后，被裁掉的候选不会复活；放在 top_k 之前，
   截断保留的才是排序后最该返回的前 k 条。rollup 已完成父子分数继承，次级排序看到的就是
   最终相关性分。
7. **默认关闭**。总开关 `secondary_rank_enabled` 出厂在 `globals` 里为 false（与
   `rerank_enabled` 同一套写法），既有检索行为不变；与 F02/F04 阈值「机制就位、默认不启用」的策略一致。
8. **参数全部可配置，分两处放**。总开关写在 `globals` 或 `retriever.<name>.params` 都生效
   （本实例 params 优先、回退 globals），算子引用（`secondary_scorer: default`）在 `retriever`
   参数里；算子自身参数在顶层命名空间 `secondary_scorer.default.params`
   （`score_band` 0.05、`importance_weight` 0.5、`recency_weight` 0.5、`recency_tau_days` 30）。
   调算法参数不动 retriever 编排配置。
9. **独立 `SecondaryScorer` 算子，放 `fuser_impl/secondary_scorer/`，但不是 Fuser**。新增
   `RetrievalOperatorType.SECONDARY_SCORER` 与 `SecondaryScorerProducer`（顶层名
   `secondary_scorer`）。内置唯一实现 `default`；业务注册自己的 target 并把
   `secondary_scorer.default.target` 指向它即可替换算法。`PipelineRetriever` 只做编排：
   恒记 `secondary_rank` 轨迹 step（关闭时 `skipped=disabled`，明细含带数、重排条数、最大带
   大小与生效参数）；业务实现的 `explain` 或打分抛异常、或返回值不是入参的排列（增删了
   候选）时记 `error`、保持原顺序，不阻断检索。业务实现只须实现 `score`，`explain` 与
   `score_with_detail` 有空明细的默认实现。

与评审稿的差异：评审稿伪代码是锚定分带，正文却写「链式合并、只要连续落差不超过 ε 就留在
同一带」，两者在 0.90 → 0.86 → 0.82 这种序列上结果不同。实现按伪代码（锚定），保证带宽
上界有明确语义，并以用例固定该行为。评审稿编排图漏了 rollup 阶段，实现按上文第 6 条落位。评审稿轨迹表列有
`scorer_target` 字段，实现未记录；生效实现改由 `explain()` 自述（默认实现记
`strategy=band_tiebreak`），只实现 `score` 的业务算子明细为空。

## 拒绝的方案

| 方案 | 否决理由 |
|---|---|
| 精确相等才 tie-break（用户原想法） | 相关性分几乎不会精确相等，等于永不触发；噪声级差距时次要键也不介入 |
| 加权和 `relevance + α·importance + β·recency` | α、β 再小也可能让弱相关项反超强相关项，正是硬约束要防的；α、β 难调且随 query 分布漂移 |
| 固定桶分带（[1.0,0.95)、[0.95,0.9)…） | 边界伪影：0.95 与 0.949 显然相近却落入不同桶 |
| 链式分带（按相邻落差判同带） | 带宽无上界，一串小落差会把相关性差很多的候选并进同一带 |
| 带内字典序（先重要度后时效） | 重要度差 0.01 即完全压制时效，过于刚性 |
| 线性时效衰减（N 天归零） | 老记忆被完全忽略，重要度优势无法体现 |
| 用 `t_event` / `t_message` 算时效 | `t_event` 派生记忆常为空、`t_message` 可空且语义是消息时间；诉求是写入新旧 |
| 未精排路径也启用 | RRF 分量纲下绝对带宽把全部候选并成一带，相关性被淹没 |
| 算法内联进 `PipelineRetriever` | 算分算法与业务相关，内联后业务无法替换 |
| 做成新的 `Fuser` 实现 | `Fuser.fuse` 是精排前按 unit_id 融合多路；次级排序是阈值后单路带内重排，阶段、输入、职责都不同 |
| 算子放 `retriever_impl/` | 那是 Retriever 编排实现目录；算分语义与融合/排序同类，归 `fuser_impl/` |
| 重要度升级为独立数值字段以根治字典序 | 跨层改序列化/索引/保留键，归 construction 侧遗留，本特性只在读取侧解析 |

## 验证

- `tests/unit/retrieval/test_secondary_scorer.py`：默认关闭、单候选/未精排跳过、带间不越、带内
  重排、锚定分带（0.90/0.86/0.82 分两带）、闭区间边界、稳定排序、高重要度老记忆 vs 低重要度
  新记忆及权重翻转、时效曲线与缺失/未来/无时区时间、重要度缺失/非数值/越界/按数值比较、
  全同带退化、权重等比缩放不变、未物化候选兜底、非法参数拒绝、自定义 target 替换实现、
  参数从配置读取。
- `tests/integration/retrieval/test_pipeline_retriever.py`：轨迹 step 明细完整、阈值裁掉的候选
  不复活且 top_k 在其后截断、关闭时顺序不变并记 skipped、未精排路径跳过（含忽略 calibrated 的算子）、算子异常保持原
  顺序、开启但未注入算子时装配拒绝、按配置开关装配。
- `tests/unit/config/test_defaults.py`：`secondary_scorer.default` 出厂参数、总开关默认 false、
  顶层名已注册。
- 全量回归见元信息「测试基线」。

## 观测

`secondary_rank` 轨迹 step 的 detail 记录生效参数（算子 `explain()`）与 `band_count` /
`max_band_size` / `reordered`；关闭、未精排、异常、空候选、单候选时分别记 `skipped: disabled` /
`skipped: uncalibrated` / `error` / `skipped: empty` / `skipped: single_candidate`。
`skipped: empty` / `single_candidate` 由默认算子记录，业务算子自行决定；`disabled` /
`uncalibrated` / `error` 由编排层记录，所有算子一致。

- `reordered` 是**位置发生变化的候选条数**，不是交换对数：`[u1,u2,u3] → [u3,u2,u1]` 记 2
  （u2 原地不动），不是 3。按此口径衡量变更面。

## 已知遗留

1. **未精排路径不支持**。若确需启用，可新增按最高分比例设带宽的参数；RRF 分布不均，语义
   需另行论证。
2. **带宽是全局配置，非 per-query**。与 F02 拒绝 per-query 阈值的策略一致。
3. **带宽对 Reranker 分数域敏感**。默认 `overlap` Reranker 的分数是 `hits/(len+1)`，长文档
   普遍挤在低位，0.05 的带宽可能把大量候选并成一带；BGE 关闭 normalize 时输出原始 logits，
   带宽语义也随之变化。启用前须按所用 Reranker 的分数分布校准 `score_band`。
4. **`recency_tau_days` 未经真实语料调优**。30 天是经验值，对话记忆与知识库场景可能差一个
   数量级。
5. **重要度存储仍是字符串**。读取侧已绕开字典序，索引侧的数值范围过滤仍受影响（见
   construction F10 遗留）。
6. **只看 `t_ingest`**。去重合并后的记忆用合并版本的写入时间，不回溯来源；「事件越近越
   优先」需另加 `event_recency` 维度并先解决 `t_event` 缺失率。
7. **`min_results` 回填的候选可能被带内重排到前面**。它们通过了正分门且与带首相关性相近，
   属合法行为；依赖「回填总在最后」的调用方需注意。
8. **Schema TemporalEntity 分支在 top_k 之后另行组装结果**，不经过次级排序。
9. **固定绝对带宽与 BGE 分数分布不匹配（系统测试实测）**。BGE 分数经 sigmoid 后在 top-1 之后
   呈数量级下降：F09 系统测试 LoCoMo 上第 1/2/3/10 名中位分为 0.71/0.20/0.077/0.006，81% 的查询
   在 top-10 内跌破 0.05；部署档 `min_score` 为 0，60 条候选全部放行。任何固定 `score_band` 都会把
   第 2 名之后与近零分尾部连成一带，次级排序会把排在 30 名以后的条目拉进 top-10。M1 定档在原口径与
   「只计与 top-10 有交集的带」口径下都判过度并带。治本是相对带宽（按带首分比例）或限定参与重排的
   排名，属设计变更。
