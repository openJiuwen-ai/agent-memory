"""评估模块（对接架构 §「可观测/指标」与 Benchmark 调研 §5 选型建议）。

两层评估，互补：
- **组件级 IR**（确定性、无需 LLM）：ground truth ``(query, 相关 unit 集)`` → Recall@k / MRR /
  nDCG / MAP + 延迟，量化检索质量本身。
- **端到端 benchmark**（答案级、需 LLM judge）：多会话语料经 ``write`` 灌入 →
  ``recall`` 召回 →（可选）LLM 合成答案 → LLM-as-judge 判分，对标 LoCoMo /
  LongMemEval 等公开基准。

两层共用 :class:`evaluation.core.harness.EvalHarness` 与
:class:`evaluation.core.runner.Runner`。当前仓库提供
:mod:`evaluation.api_adapter` 作为 evaluation-only baseline adapter，待真实 API
接入后可沿同一注入点替换。
"""
