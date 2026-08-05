# wikimem retained evaluation

本目录承接 `rust/wikimem/src/retained_eval` 在 mem2.0 中的 evaluation/profile 能力。

## 文件职责

- `__init__.py`：导出 retained_eval 兼容公共面。
- `retained_eval.py`：LoCoMo 样本归一化、EvalCase / CaseScore DTO、指标摘要、plugin list 解析、progress 文案和 harness artifact 写出。
- `example_eval.py` / `ama_bench.py`：文件系统评测和 AMA retrieval-only 运行器；必须标明标签来源，答案派生指标只能标为 proxy。

## 边界

- 本目录属于 evaluation，不得修改 `MemoryAPI`、`RetrievalQuery` 或 `Retriever.retrieve` 签名。
- LoCoMo / LongMemEval 字段只能在 evaluation adapter 内消费，不得进入通用 core 类型。
- retained 检索 profile 后续可调用现有 mem2.0 API / Recaller，但不得把数据集字段塞进 `src/retrieval/types.py`。
- artifact 写出必须保留 summary、case trace、failure report、category breakdown 和 stage profile，便于精度回归定位 miss stage。
- `.kb-research/retrieval` 记录仅可由带 Python provenance 的 workspace 消费；外部或 Rust 记录不得作为检索输入。
