# F07 — 融合阶段引入 BM25 词法项（BM25_scored_fusor）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-20 |
| 影响范围 | `jiuwen_memory/retrieval/fuser_impl/bm25_scored_fuser.py`（新增）、`jiuwen_memory/retrieval/fuser_impl/__init__.py`（注册一行 import）；`config/defaults.py` 未改动，出厂默认仍为 `rrf`，算子按需选配（同 F04 对 `score_max` 的先例） |
| 测试基线 | `tests/unit/retrieval/test_bm25_scored_fuser.py` 新增 13 例全部通过；`tests/unit/retrieval/test_fuser.py` 既有 14 例未改且仍通过；`ruff check` / `ruff format --check` 干净 |
| Refs | #194 |

> 归档 PR !299（GitHub #322）：在融合（粗排）阶段新增一个注册名为 `BM25_scored_fusor` 的 `Fuser` 实现。
> 按 `jiuwen_memory/retrieval/AGENTS.md` 的词汇约定，rank 只指 Fuser，Reranker 保持独立阶段——本算子位于 fuse 阶段，不是 rerank。

---

## 背景

在 `6b232c2` 基座上测得（LongMemEval 500 题，ES 后端，其余条件不变）：

| Top10 | 配置 |
|---|---|
| 81.00% | ES，`rerank_enabled: false` |
| 66.73% | ES + 默认 `overlap` 打分（`hits / (len(toks) + 1)`） |

词面重合打分——无 IDF、无词频饱和、无条件全强度长度归一化——是掉分根因。在内置 `target=memory` 装配上把同一表达式换成 BM25，Top10 由 64.33% 升至 75.55%（+56 题）。这组数字证明问题出在**打分表达式**而非后端，但它测于 rerank 阶段、且基座已落后当前 `mem2.0` 约 30 个提交，**不能作为本算子的收益**——本算子自身效果在基准跑完前是未测量的。

另一个结构性事实：ES 对 keyword 查询只给自己的 top-k 打了 Lucene BM25 分（带全集合统计）。融合时候选集是各通道的**并集**，vector 通道召回的候选从未获得过词法分。因此"有没有词法分"编码的是**被哪条通道找到**，而非相关性。

## 决策

### 决策 1：对并集候选统一计算一次 BM25，作为 CombMAX 的新增一项

不重打分 ES 已给出的分数（其 BM25 带全集合统计，用候选池统计重算只会更差），而是为所有候选在融合时按 Lucene 公式补一个词法项：

```
idf(t)   = log(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
score(d) = Σ_t  idf(t) × freq(t,d) × (k1 + 1) / (freq(t,d) + k1 × norm(d))
norm(d)  = 1 - b + b × dl(d) / avgdl

combined(u) = max( max_c  weight_c × norm_c(u),  w_lex × norm_bm25(u) )
```

组合形状沿用 F04 决策 1 的 `score_max`（CombMAX）。各通道自身分数原样保留；词法项经池内 max 归一化后参与取最大值，**只会抬升候选、不会压低**（有单测对照 `ScoreMaxFuser` 输出断言此性质）。`bm25_lexical_weight: 0` 时精确退化为 `score_max`。

### 决策 2：opt-in，不动出厂默认

`config/defaults.py` 保持 `rrf` 为出厂默认融合器，本算子经装配配置显式启用（配置节名是既有的 `fuser`，仅 target 值取 HQ 指定名 `BM25_scored_fusor`）。与 F04 对 `score_max` 的处理一致。

### 决策 3：复用命名 tokenizer 共享实例

```yaml
fuser:
  default:
    target: BM25_scored_fusor
    params:
      tokenizer: default          # Factory.build_named 返回共享实例
      bm25_k1: 1.2
      bm25_b: 0.75
      bm25_lexical_weight: 1.0    # 0 时精确退化为 score_max
      fusion_channel_weights: {}
```

`tokenizer: default` 是承载性配置：共享实例保证候选用与建索引、产出 `ParsedQuery.tokens` 相同的分词器处理（AGENTS.md 铁律 §2）。算子优先使用 `query.tokens`，仅在其缺失时回退对原始字符串分词。

## 拒绝的方案

| 方案 | 结论 | 依据 |
|---|---|---|
| 用候选池统计重算 ES 已给的 BM25 分 | 否决 | ES 的分带全集合统计，池统计只会更差；本算子只加项不改写 |
| 在 rerank 阶段修打分表达式 | 搁置 | 该路径已用实验证明"表达式是问题"（64.33% → 75.55%），但本次目标是把词法信号前移到 fuse 阶段 |
| 设为出厂默认融合器 | 暂不 | 一行改动即可，但会改变单测装配，且端到端基准未跑完，不留未验证的默认切换 |
| 缺内容的候选（`ScoredUnit` 而非 `ScoredMemoryUnit`）词法项按 0 处理 | 否决 | 未物化候选保持其通道分，不被清零，避免 Storage 管线行为差异误杀候选 |

## 验证

- 13 个新单测全部通过，含「词法项只升不降」（对照 `ScoreMaxFuser` 输出）与 `bm25_lexical_weight: 0` 精确退化为 `score_max` 的断言。
- BM25 公式与独立参考实现在 400 组随机语料（变动 `k1`、`b`、文档长度、空文档、空查询）上对齐，误差 `abs < 1e-12`。
- 既有 `test_fuser.py` 14 例未改且通过；`ruff check` / `ruff format --check` 干净。
- **端到端基准尚未完成**（PR 以草稿提交，先评审算子形状/命名/注册）。基准计划：`target=elasticsearch`、LongMemEval 500 题，同评测套件/裁判模型/机器，四臂均跑在本 PR 基座上——①`rrf`+rerank off（新基线）②`BM25_scored_fusor`+rerank off（决定算子去留）③`score_max`+rerank off（归因：区分"换组合规则"与"新增词法项"）④`BM25_scored_fusor`+rerank on（回答开放问题 2）。①②为最小集；抽取非确定（历史 run 间 API 调用 15,414–15,712 次），噪声下限未测，计划复跑一臂。
- 披露：所有基准数字均以 DeepSeek 作为抽取/生成/裁判模型（无 GLM key），结果路径中的 `glm52` 是协议名里硬编码的字符串；各臂内部一致可比，但绝对值不可与 GLM-5.2 结果直接比较。

## 已知遗留

1. **IDF 与 avgdl 来自候选池而非全集合**。`Fuser` 接口拿不到全局统计；池宽为 `recall_max × 在场通道数`，是召回后可得的最宽样本，但输出是**池相对**词法强度，不是跨集合可比的绝对分——归一化到池最大值后再参与组合，正是为了能与其它通道安全比较。
2. **CombMAX 头部并列**。各轴最优候选均归一化为 1.0，列表头部可能并列。此为 `score_max` 既有行为，非本次引入。
3. **`rerank_enabled: true` 时本算子效果被覆盖**。`PipelineRetriever` 在 rerank 后以 `replace(survivors[i], score=scores[i])` 同时替换顺序与分数，而 `globals.rerank_enabled` 出厂为 `True` 且默认 reranker 是 `overlap`——该配置下本算子无可观察效果。基准计划含第 4 臂用数据回答，不以阻塞方式处理。
4. **是否升级为出厂默认**未定，留待基准结果与评审决定。
