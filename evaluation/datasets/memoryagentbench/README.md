# MemoryAgentBench 输入数据

本目录对应 MemoryAgentBench 的 **Conflict Resolution**（`factconsolidation`）任务。
评测端读的是**单子集 JSON**（每个子集一个文件），不是原始 parquet。

## 仓库里只有一份

```
factconsolidation_mh_6k.json        # 默认评测子集，唯一随仓库提交的数据文件
```

其余子集（`sh/mh × 6k/32k/64k/262k`）由 `.gitignore` 排除，需要按下面的步骤自行生成。

## 自行生成全部 8 个子集

1. 从官方数据集下载原始文件：

   - HuggingFace：<https://huggingface.co/datasets/ai-hyz/MemoryAgentBench>
   - 需要的文件：`Conflict_Resolution-00000-of-00001.parquet`（约 1.5 MB，8 行 = 8 个子集）

2. 放到本目录：

   ```
   evaluation/datasets/memoryagentbench/Conflict_Resolution-00000-of-00001.parquet
   ```

3. 运行导出脚本（需要 `pyarrow`，见 `pyproject.toml` 的 `eval` 依赖组）：

   ```bash
   pip install -e ".[eval]"
   .venv/bin/python -m evaluation.memoryagentbench.export_datasets
   ```

   会在本目录生成 8 个 JSON，并打印每个子集的字符数 / 事实数 / 题数。

## 文件命名

JSON 文件名 = parquet 里的 `metadata.source`：

```
factconsolidation_mh_6k.json      factconsolidation_sh_6k.json
factconsolidation_mh_32k.json     factconsolidation_sh_32k.json
factconsolidation_mh_64k.json     factconsolidation_sh_64k.json
factconsolidation_mh_262k.json    factconsolidation_sh_262k.json
```

`mh` = multi-hop（多跳），`sh` = single-hop（单跳），数字表示上下文规模。

## 怎么切换评测子集

改 `evaluation/memoryagentbench/config.yml` 的 `runner.data`：

```yaml
runner:
  data: evaluation/datasets/memoryagentbench/factconsolidation_mh_32k.json
```

或临时用命令行覆盖：

```bash
.venv/bin/python -m evaluation.memoryagentbench.run --stage all \
    --data evaluation/datasets/memoryagentbench/factconsolidation_mh_32k.json
```

## 数据格式

每个 JSON 是单元素列表，元素含四个字段：

```json
[{
  "context":   "Here is a list of facts:\n0. …\n1. …",
  "questions": ["…", "…"],
  "answers":   [["…"], ["…"]],
  "metadata":  {"source": "factconsolidation_mh_6k", "qa_pair_ids": ["…"], "…": "…"}
}]
```

`context` 里的事实**序号越大越新**，这是 Conflict Resolution 任务的判定依据。
