# evaluation —— 记忆系统评测

对接 `docs/design`：能力对标见 [vision.md](../docs/design/vision.md) §能力对标、基准选型见
[memory_benchmarks.md](../docs/design/memory_benchmarks.md) §5、指标口径见 architecture.md
「可观测：检索 token / p50·p95 时延 / 容量」。

## 两层评测

**组件级 IR**

- 测什么：检索召回/排序质量本身。
- 指标：Recall@k、Precision@k、MRR、nDCG@k、MAP、p50/p95 时延、回传 token。
- 依赖：baseline adapter + ground truth 数据。
- 状态：baseline 可跑，待真实 adapter。

**端到端 QA**

- 测什么：整体功能（write→recall→答案）。
- 指标：QA 准确率（LLM-as-judge）+ 按类目分桶。
- 依赖：baseline/真实 adapter + LLM judge + 公开数据集。
- 状态：adapter 骨架可跑，待真实 API + 数据集 + judge endpoint。

两层共用同一套 harness/runner。当前先走 evaluation-only baseline adapter 的
`write`/`recall`，后续真实 adapter 接入后仍走同一注入点，评的是**整体功能**
而非检索层孤件。

## 目录

```
evaluation/
├── api_adapter.py # evaluation-only in-memory baseline adapter
├── core/          # 框架：types(数据契约) · harness(装配+采集) · runner(编排) · report(json/md)
├── benchmark/     # 数据集适配器与 data/(下载区·gitignore)
├── metrics/       # ir_metrics(确定性·全实现) · perf_metrics(吃轨迹) · qa_metrics(分类目) · llm_judge(可插拔)
├── scripts/       # run_ir_eval：adapter 接入后可跑 · run_e2e_eval：骨架
└── smoke_test/    # golden_ir.jsonl + test_ir_smoke(CI 回归基线)
```

## 用法

```bash
# 当前可跑：数据集解析 / 指标计算 / baseline adapter E2E
python3 -m pytest evaluation/smoke_test

# 组件级 IR（默认跑内置 ground truth + baseline adapter）
python3 evaluation/scripts/run_ir_eval.py

# 真实 adapter 接入后可用同一 ground truth 对比装配改动（加权融合 / 结构化披露）
python3 evaluation/scripts/run_ir_eval.py --fuser weighted_rrf --discloser structured

# 自建 ground truth
python3 evaluation/scripts/run_ir_eval.py --dataset path/to/golden.jsonl --json out.json
```

## 端到端 benchmark（LoCoMo / LongMemEval）

数据集适配器与 judge 骨架已就绪（按各数据集官方 schema 解析）。当前 baseline adapter 可用于本地通路验证；接公开 benchmark 仍需两步外部输入：

```bash
# 1. 下载数据集到 data/（gitignore，不入库）
#    LoCoMo:      https://github.com/snap-research/LoCoMo    → data/locomo10.json
#    LongMemEval: https://github.com/xiaowu0162/LongMemEval  → data/longmemeval_s.json

# 2. 配 judge（OpenAI 兼容 endpoint，智谱/豆包/OpenAI 均可）并运行
export JUDGE_BASE_URL=...  JUDGE_MODEL=...  JUDGE_API_KEY=...
python3 evaluation/scripts/run_e2e_eval.py --dataset locomo
python3 evaluation/scripts/run_e2e_eval.py --dataset longmemeval
# 不配 judge 也能跑：只出 IR/性能，qa_accuracy 自动跳过
```

实现要点：
- **LoCoMo**：消息→seed（`key={sample_id}/D{session}:{turn}`）、QA→query（`evidence`→`relevant_keys`）；
  **每个 sample 独立 scope**；category 5 对抗题跳过；`bucket=cat{N}`。
- **LongMemEval**：turn→seed（`key={qid}/{session}#{turn}`）、question→query；证据优先用 turn 级
  `has_answer`、否则回退 `answer_session_ids`；**每道题独立 scope**；`bucket=question_type`；
  `question_date` 透传给 judge 作「今天」（时序题）；`_abs` 题标 abstention。
- `LLMJudge`（`metrics/llm_judge.py`）两步：召回记忆→合成答案→比对 ground truth，输出 CORRECT/WRONG，
  含**拒答判定**；`qa_accuracy` 按 `metadata['bucket']` 分桶（`acc:<bucket>`）。
- 大数据集先切片：`LoCoMoDataset(path, samples=[0,1], max_questions=20)`（LongMemEval 同参）。

> 接新公开数据集 = 写一个 `Dataset` 子类（`seeds()`/`queries()`），metric/judge 复用。
> 仅「边写边问」类（MemoryAgentBench/PersonaMem）需补时间线扩展，详见上层评估说明。

## Ground Truth JSONL 格式

```jsonl
{"type": "seed",  "key": "m1", "content": "...", "tags": ["coffee"]}
{"type": "query", "query_id": "q1", "text": "...", "relevant_keys": ["m1"], "top_k": 5}
```

`scope` 可省略（默认 `org=eval,user=u1`）；`relevant_keys` 指向 seed 的 `key`。
