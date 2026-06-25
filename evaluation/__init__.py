"""评估模块（对接架构 §「可观测/指标」与 Benchmark 调研 §5 选型建议）。

两层评估，互补：
- **组件级 IR**（确定性、无需 LLM）：评测标注 ``(query, 标准相关集)`` →
  Recall@k / MRR / nDCG / MAP + 延迟，量化检索质量本身。
- **端到端 benchmark**（答案级、需 LLM judge）：多会话语料经 ``write`` 灌入 →
  ``recall`` 召回 →（可选）LLM 合成答案 → LLM-as-judge 判分，对标 LoCoMo /
  LongMemEval 等公开基准。

两层共用 :class:`evaluation.core.harness.EvalHarness`（走 ``MemoryAPI`` 公共面，
因此评测的是「整体功能」而非检索层孤件）与 :class:`evaluation.core.runner.Runner`。
"""
