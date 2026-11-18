# F11 评测并发执行与断点续跑

> 日期：2026-11-17
> 关联 issue：GitCode #326 / #328 / #329
> 影响模块：`evaluation/longmemeval/`、`jiuwen_memory/common/`（embedder/reranker）、
> `jiuwen_memory/construction/`（extractor）

## 1. 背景

LongMemEval 端到端评测（LLM 抽取 + 检索 + 回答 + 判分）单题约 **20 分钟**，
45 题串行需要 **约 15 小时**。实际运行暴露两个工程问题：

- **无断点**：2026-11-17 一次评测在跑了 11 小时 24 分、2590 次 LLM 调用后崩溃，
  **24 题的工作量全部丢失**（`Runner.run()` 需全部 case 跑完才落盘）。
- **并发不可用**：`--concurrency > 1` 会触发底层模型的线程安全问题。

## 2. 决策

### 2.1 断点放在评测侧，不进内核

内核 `jiuwen_memory/` 全文无 `checkpoint` 概念；`Job` / `Scheduler` 的失败语义是
「记录并丢弃」，与「可恢复」相反——这是内核的正确设计（一次 `evolve` 失败不应让
长期运行的服务停机）。

断点续跑建模的是「如何把一批测试用例跑完」，属于测试编排，不是记忆系统语义。
把它塞进内核等于让记忆系统知道自己正在被评测，是层级污染。

**判据**：断点保护的是「业务数据一致性」还是「测试流程进度」？前者进内核，后者留评测。

### 2.2 断点 key 必须是 `query_id`，不是位置下标

`dataset.queries()` 返回的是**筛选后**的列表。用 `enumerate()` 的位置做 key 时，
`--samples 0,1` 与 `--samples 2,3` 都是位置 0、1——**两个完全不同的题集会撞同一个断点文件**。

实测后果：`--samples 0,1` 跑完后续跑 `--samples 2,3`，返回**第一批**的答案，
退出码 0、`recall@10` 仍为 1.0。这是**静默错误**，比崩溃危险得多。

### 2.3 命中前必须校验覆盖范围

断点里存的结果必须**恰好覆盖**本次请求的 `query_id` 集合。空、缺题、多题一律视为未命中。

实测反例：清空断点文件里的 `results` 后，输出 `checkpoint hit, skip 0 query(s)`、
`n_queries=0`、全部指标归零、退出码 0——一次崩溃后的残file 就能伪造出「成功」报告。

### 2.4 指纹覆盖全部影响结果的参数

包括 `samples`、`question_ids`、`granularity`、`recall_top_k`、`answer_cutoff(s)`、
`dialogue_turn_max_chars`、`infer`、`concurrency`。

漏任何一项 → 不同配置的断点互相命中。实测：`granularity` 从 `dialogue_turn`
改成 `session` 后续跑，命中的仍是 dialogue 级结果。

### 2.5 并发是「测速旋钮」，不是「结果旋钮」

评测有两个正交维度：

| 维度 | 受并发影响 | 处理 |
| --- | --- | --- |
| 正确性（IR / QA） | 否 | 并发照常产出 |
| 性能（wall-clock 延迟） | **是** | 并发时从套件中剔除 |

`perf_metrics` 测的是 `perf_counter()` 的墙钟差值。并发执行时单次请求的耗时
包含排队等待，测出的是「当前并发下的排队延迟」而非「系统的检索速度」。

因此 `build_metric_suite(concurrency=N)` 在**构造期**决定套件内容，
让「并发 + perf 指标」这个非法组合无法被拼出来。

### 2.6 计时补丁不得在并发下安装

`run_query` 通过替换 `storage.recall` 实现内部耗时诊断。`storage` 是进程级共享对象，
多线程会互相覆盖补丁，导致 `expected exactly one storage recall call, got N`。

并发时不安装补丁，`storage_recall_wall_ms` 记 0——该指标在并发下本就不成立。

### 2.7 artifact 模式与并发互斥

`--artifact-dir` 记录 `_pre_dedup_calls` / `_retrieval_audits` 等跨 scope 共享的采集状态，
且原本就要求单 scope（`len(jobs) != 1` 时抛错）。并发时必须显式拒绝，而非静默错乱。

## 3. 被拒绝的方案

| 方案 | 拒绝理由 |
| --- | --- |
| 断点放内核（给 `Job` 加 checkpoint） | 语义错位；内核失败语义是「丢弃」，与可恢复相反 |
| 用 scope 做断点 key | scope 含每次运行唯一的 `run_id`，断点永不命中（已实测） |
| 按 `len(batch_id)` 截前缀匹配 source_id | 抄写错误会改变长度（`ccbf1798` → `ccb1798-...`），实测失效 |
| 并发时仍产出 perf 指标并加标注 | 依赖调用方自觉读标注；让非法状态不可能存在更可靠 |
| 每线程独立加载模型 | 显存翻倍（2 × 6.2G 模型 + KV cache），16G 卡放不下 |

## 4. 验证基线

### 断点

```
强杀后断点文件: 3 个
续跑命中:       3 次
产出:           result.json ✅
完整跑 vs 续跑：IR 指标 6 项逐项相等 ✅
```

### 并发

```
串行指标数 19（含 perf）
并发指标数 14（perf 已剔除）
IR 指标一致 ✅
```

### 测试

| 文件 | 用例数 |
| --- | --- |
| `tests/unit/evaluation/test_checkpoint.py` | 29 |
| `tests/unit/evaluation/test_concurrency.py` | 5 |
| `tests/unit/construction/test_source_id_resolution.py` | 16 |
| `tests/unit/common/test_bge_m3_concurrency.py` | 3 |

全量回归：4099 个测试，1 个既有失败（`test_spacy_extract_with_model`，中文模型跑英文
句子，与本改动无关）。

## 5. 已知遗留

1. **并发时 embedder / reranker 仍串行**（底层模型非线程安全，用 `RLock` 保护）。
   它们不是瓶颈（检索约 430ms，LLM 抽取约 20 分钟），影响可忽略。
   要进一步并发需给每线程独立模型实例，显存不允许。
2. **并发分支不读写断点**。当前 48 题规模下并发本身已能把时间压到可接受范围；
   并发 + 断点的组合涉及跨线程写者唯一性，留待后续。
3. **`--artifact-dir` 与 `--concurrency > 1` 互斥**，已在构造期显式报错。
