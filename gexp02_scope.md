# G.EXP02 Lambda 复杂度规范修复范围

## 规则与范围

- 规则：G.EXP02，避免使用内容过长、嵌套或包含复杂逻辑的 lambda 表达式。
- 分支：`refactor/issue-155`。
- 扫描对象：用户指定的 4 个实际 Python 文件；不扫描或修改测试、`.claude/` 和清单外文件。
- 判定：超过 120 字符或具有嵌套/多分支计算的 lambda 应提取为具名函数。

## 路径与修复记录

| 用户路径 | 实际路径 | 初始命中 | 修复方式 | 最终位置 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `evaluation/metrics/ir_metrics.py` | 同原路径 | `ir_metrics()` 内 recall、precision、nDCG、MRR、MAP 的评分 lambda | 提取 `_recall_score`、`_precision_score`、`_ndcg_score`、`_mrr_score`、`_map_score`，以 `partial` 保留 `k` 绑定。 | 61-77 | 已修复 |
| `jiuwen_memory/control/space_impl/kv_space_manager.py` | 同原路径 | 成员排序 lambda | 提取 `_member_sort_key()`。 | 206 | 已修复 |
| `jiuwen_memory/control/jobs_impl/middle_to_long_job.py` | 同原路径 | 候选连续性排序 lambda | 提取 `_unit_ingest_sort_key()`。 | 42 | 已修复 |
| `jiuwen_memory/common/reranker/reranker_impl/api_reranker.py` | 同原路径 | Cohere、DashScope 请求体及响应解析 lambda | 提取 `_cohere_body`、`_cohere_results`、`_dashscope_body`、`_dashscope_results`。 | 39-60 | 已修复 |

## 验证

- 对 4 个目标文件复扫，未发现 lambda 表达式。
- 具名函数保留原回调签名、排序键、请求 JSON 和响应解析结果。
- 已通过 `python -m compileall` 与 `git diff --check`。
