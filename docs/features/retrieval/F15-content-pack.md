# F15 — Top-K 后置内容打包算子（ContentPacker）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-24 |
| 影响范围 | `jiuwen_memory/retrieval/{base.py,bootstrap.py,__init__.py,types.py,staged_retriever.py,content_packer.py,content_packer_impl/,retriever_impl/pipeline_retriever.py}`、`jiuwen_memory/config/defaults.py`、`jiuwen_memory/common/type_def/context.py`、`jiuwen_memory/api/memory_api_impl/{query_ops.py,local_support.py}`、`docs/specs/S04-retrieval.md`、`jiuwen_memory/retrieval/AGENTS.md` |
| 测试基线 | `tests/unit/retrieval/test_content_packer.py`、`tests/unit/retrieval` 全量、`tests/unit/config/test_defaults.py`、`tests/integration/retrieval/test_pipeline_retriever.py` 全量通过；变更文件 `ruff check` 无新增告警 |
| Refs | #278 |

> 在 `PipelineRetriever` 完成 `cut_to_top_k` 之后、进入 `Discloser` 之前，以独立算子 `ContentPacker`（基类 + Producer + `AdaptiveContentPacker` 默认实现）在 `budget_ratio`（默认 0.8）预算内对 K 条候选做价值筛选与单条正文无模型压缩，并逐条（`MemoryUnit`）向后传递给 `Discloser`。阶段方法 `pack_content` 在 `StagedRetriever` 抽象声明。命名使用 `content_*`，避免与后续「上下文 / context」类操作冲突。

---

## 背景

长对话问答中，检索返回的 Top-K 记忆占下游提示词绝大部分输入 token。目标是记忆 token 降低约 20%（≥ 18%），问答准确率相对不打包基线持平或上升（下降在 0.5 个百分点以内也算达标），且不引入任何额外模型调用。

在 LoCoMo 会话 2–9（1,307 题，GLM-5.2 作答与判分，每组 2 轮，预算比例 0.8）上的评测结果：

| Top-K | 策略 | 基线准确率 | 打包后准确率 | 差值（95% CI） | 记忆 token/题 | 降幅 |
|---|---|---|---|---|---|---|
| 10 | 角色缩写 + 紧凑 | 84.62% | 85.08% | +0.46 [−0.28, +1.19] | 384 → 297 | 22.6% |
| 20 | 电报式 | 88.18% | 88.14% | −0.04 [−0.77, +0.69] | 752 → 593 | 21.1% |
| 50 | Mixed-Score 直接删条 | 91.58% | 91.64% | +0.06 [−0.91, +0.68] | 1,861 → 1,482 | 20.4% |

三档 95% CI 均跨 0，结论为「记忆 token 稳定降低 20%–23% 且问答准确率持平」。

部分业务要求返回记忆条数不得少于 Top-K（例如不允许 50→40 的 drop）。此类约束仅存在于单次请求，不得写入 globals 出厂配置。

---

## 决策

### 决策 1：独立 `ContentPacker` 算子，阶段声明在 `StagedRetriever`

链路位置：`threshold` → `secondary_rank` → `cut_to_top_k` → **`pack_content`（`ContentPacker`）** → `disclose`。

- 新增 `RetrievalOperatorType.CONTENT_PACKER`、抽象基类 `ContentPacker(RetrievalOperator)` 与注册式工厂 `ContentPackerProducer`（顶层命名空间 `content_packer`）。
- `StagedRetriever` 抽象声明 `pack_content(state)`；`PipelineRetriever` 实现并装配。
- 默认实现 `AdaptiveContentPacker` 在 `content_packer_impl` 下以 `@ContentPackerProducer.register("default")`（及别名 `"adaptive"`）自注册。
- 输出保持逐条 `ScoredMemoryUnit`，维持原相关性相对顺序；未改写条目直接复用原对象，改写条目经 `dataclasses.replace` 浅拷贝替换 `segments` 并清空旧 `layers`（`vectors`/`metadata` 等字段零拷贝复用引用），保证主链路异常回退（`return final`）时原候选完好。

### 决策 2：Mixed-Score 价值排序 + 按规模三级无模型自适应压缩

预算 = `budget_ratio` × 当前 K 条记忆原始 token 总和。

1. **五项 Mixed-Score 优先级**：
   `w_rel × minmax(检索得分) + w_question × 问题词重合度 + w_recency × 时间新近度 + w_entity × 实体重合度 − w_cost × 单条长度占比`（默认权重 `0.45 / 0.50 / 0.0 / 0.0 / 0.05`，`recency_tau_days=30.0`）。
2. **三级自适应策略（`strategy="auto"`）**：
   - `≤ 10` 条（`role_compact`）：角色缩写 + 同日日期前缀剥离；仍超预算时尾部转电报式。
   - `11–20` 条（`telegraph`）：高分头部保原文，尾部去重后脱英文虚词。
   - `> 20` 条（`drop`）：按 Mixed-Score 贪心保留，其余整条剔除。

### 决策 3：默认关闭；查询级覆盖；请求级禁止删条（不进 globals）

- 总开关 `content_pack_enabled` 出厂在 `globals` 默认为 `False`；`RetrievalQuery.content_pack` 可单次覆盖启停。
- **禁止删条**仅请求级：`Context.extensions["content_pack_keep_count"]` → API 边界写入 `RetrievalQuery.content_pack_keep_count`（不进 globals）。为 true 时：
  - `AdaptiveContentPacker` 仍尽量让高价值头部保留原文，但对尾部只压缩、不去重删条，也不因预算弹尾；本应走 `drop` 时改走保条数的 telegraph；
  - 轨迹 `suppressed_drops` 记录「按原策略本应丢掉、但业务要求保留」的条数（含 drop 与 telegraph 去重）；
  - 保条数优先于预算硬顶：压缩后总 token 可能仍高于 `budget_ratio`；
  - 主链路护栏：输出条数必须等于输入 Top-K，否则记 error 并退回原 Top-K。
- `expand_depth > 0` 自动跳过；非法多重集子集或异常时脱敏告警并退回原 Top-K。

### 决策 4：命名 `content_*` 而非 `context_*`

避免与后续「上下文 / context」类能力（如 query_context、会话上下文）在命名与配置段上冲突。

---

## 拒绝的方案

| 方案 | 否决理由 |
|---|---|
| 融合进 `Fuser` / `Reranker` / `Discloser` | 同前：精排前偏置、读写共用打分器、一对一披露铁律 |
| 仅提供检索层外围纯函数 | 无法经标准 Config / `retrieve` 启用，缺轨迹与降级 |
| 把 `keep_count` 放进 globals / defaults | 条数约束是业务请求语义，不应成为全局部署默认；专家明确要求走 search extensions |
| 继续使用 `context_packer` 命名 | 与后续上下文操作易混淆 |

---

## 验证

- `tests/unit/retrieval/test_content_packer.py`：三档路由、五项权重、`keep_count` 保条数与 `suppressed_drops`、参数校验、`dataclasses.replace` 浅拷贝不污染输入候选。
- `tests/integration/retrieval/test_pipeline_retriever.py`：主链路 `pack_content`、查询级开关、`keep_count` 护栏、异常降级。
- `tests/unit/retrieval/test_pipeline_stage_guards.py`：`pack_content` 阶段前置守卫。

---

## 已知遗留

1. **中英文规则差异**：`role_compact` / `telegraph` 英文虚词表面向英文；中文可配 `strategy: drop`，或在 `keep_count` 下仅做有限压缩。
2. **默认近似分词计数**：可经 `count_tokens` 注入精确分词器。
