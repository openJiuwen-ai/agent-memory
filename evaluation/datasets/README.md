# 输入数据目录

这里只存放评测输入，不存放运行结果、日志、审计包或缓存。两个 mini 都是从同目录
标准数据集机械截取的原格式样本，不使用 Mock，也不更换测评链路。

- `locomo/`：LoCoMo 原始数据和可提交的微型样本。
- `longmemeval/`：LongMemEval 原始数据和可提交的微型样本。
- `memoryagentbench/`：MemoryAgentBench Conflict Resolution 的原始 parquet 与单子集 JSON；
  评测只读 JSON，仅 `mh_6k` 入库（生成方式见该目录 README）。
完整数据文件默认被 `.gitignore` 排除，仅保留冒烟输入。
