# evaluation —— 记忆系统评测

对接 `docs/design`：能力对标见 [vision.md](../docs/design/vision.md) §能力对标、基准选型见
[memory_benchmarks.md](../docs/design/memory_benchmarks.md) §5、指标口径见 architecture.md
「可观测：检索 token / p50·p95 时延 / 容量」。

## 两层评测

| 层 | 测什么 | 指标 | 依赖 | 状态 |
| --- | --- | --- | --- | --- |
| **组件级 IR** | 检索召回/排序质量本身 | Recall@k · Precision@k · MRR · nDCG@k · MAP + p50/p95 时延 + 回传 token | 仅检索链路（in-memory 装配） | ✅ 可跑 |
| **端到端 QA** | 整体功能（write→recall→答案） | QA 准确率（LLM-as-judge）+ 按类目分桶 | 全链路 + LLM judge + 公开数据集 | ✅ 通路就绪（待下载数据集 + 配 judge endpoint） |

两层共用同一套 harness/runner——都走 `MemoryAPI` 公共面（`write`/`recall`），评的是**整体功能**而非检索层孤件。

## 目录

```
evaluation/
├── core/          # 框架：types(数据契约) · harness(装配+采集) · runner(编排) · report(json/md)
├── benchmark/     # 适配器：jsonl_dataset(自定义评测标注集) · locomo_adapter · longmemeval_adapter · data/(下载区·gitignore)
├── metrics/       # ir_metrics(确定性·全实现) · perf_metrics(吃轨迹) · qa_metrics(分类目) · llm_judge(可插拔)
├── scripts/       # run_ir_eval(可跑) · run_e2e_eval(骨架)
└── smoke_test/    # golden_ir.jsonl + test_ir_smoke(CI 回归基线)
```

## 用法

```bash
# 组件级 IR（默认跑内置冒烟评测基准）
python3 evaluation/scripts/run_ir_eval.py

# 用同一评测标注集对比装配改动（加权融合 / 结构化披露）
python3 evaluation/scripts/run_ir_eval.py --fuser weighted_rrf --discloser structured

# 自定义评测标注集
python3 evaluation/scripts/run_ir_eval.py --dataset path/to/golden.jsonl --json out.json

# 冒烟（确定性回归，不在默认 pytest testpaths 内，需显式触发）
pytest evaluation/smoke_test
```

## 端到端 benchmark（LoCoMo / LongMemEval）

适配器与 judge 已就绪（按各数据集官方 schema 解析），只需两步外部输入：

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
- `LLMJudge`（`metrics/llm_judge.py`）两步：召回记忆→合成答案→比对参考答案，输出 CORRECT/WRONG，
  含**拒答判定**；`qa_accuracy` 按 `metadata['bucket']` 分桶（`acc:<bucket>`）。
- 大数据集先切片：`LoCoMoDataset(path, samples=[0,1], max_questions=20)`（LongMemEval 同参）。

> 接新公开数据集 = 写一个 `Dataset` 子类（`seeds()`/`queries()`），metric/judge 复用。
> 仅「边写边问」类（MemoryAgentBench/PersonaMem）需补时间线扩展，详见上层评估说明。

## 评测标注 JSONL 格式

```jsonl
{"type": "seed",  "key": "m1", "content": "...", "tags": ["coffee"]}
{"type": "query", "query_id": "q1", "text": "...", "relevant_keys": ["m1"], "top_k": 5}
```

`scope` 可省略（默认 `org=eval,user=u1`）；`relevant_keys` 指向 seed 的 `key`。
