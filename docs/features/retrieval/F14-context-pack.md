# F14 — Top-K 后置上下文打包算子（ContextPacker）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-19 |
| 影响范围 | `jiuwen_memory/retrieval/{base.py,bootstrap.py,__init__.py,types.py,context_packer.py,context_packer_impl/,retriever_impl/pipeline_retriever.py}`、`jiuwen_memory/config/defaults.py`、`docs/specs/S04-retrieval.md`、`jiuwen_memory/retrieval/AGENTS.md` |
| 测试基线 | `tests/unit/retrieval/test_context_packer.py`（新增 39 例）、`tests/unit/retrieval` 全量、`tests/unit/config/test_defaults.py`、`tests/integration/retrieval/test_pipeline_retriever.py` 全量通过；变更文件 `ruff check` 无新增告警 |
| Refs | #278 |

> 在 `PipelineRetriever` 完成 `cut_to_top_k` 之后、进入 `Discloser` 之前，以独立算子 `ContextPacker`（基类 + Producer + `AdaptiveContextPacker` 默认实现）在 `budget_ratio`（默认 0.8）预算内对 K 条候选做价值筛选与单条正文无模型压缩，并逐条（`MemoryUnit`）向后传递给 `Discloser`。

---

## 背景

长对话问答中，检索返回的 Top-K 记忆占下游提示词绝大部分输入 token。目标是记忆 token 降低约 20%（≥ 18%），问答准确率相对不打包基线持平或上升（下降在 0.5 个百分点以内也算达标），且不引入任何额外模型调用。

在 LoCoMo 会话 2–9（1,307 题，GLM-5.2 作答与判分，每组 2 轮，预算比例 0.8）上的评测结果：

| Top-K | 策略 | 基线准确率 | 打包后准确率 | 差值（95% CI） | 记忆 token/题 | 降幅 |
|---|---|---|---|---|---|---|
| 10 | 角色缩写 + 紧凑 | 84.62% | 85.08% | +0.46 [−0.28, +1.19] | 384 → 297 | 22.6% |
| 20 | 电报式 | 88.18% | 88.14% | −0.04 [−0.77, +0.69] | 752 → 593 | 21.1% |
| 50 | Mixed-Score 直接删条 | 91.58% | 91.47% | −0.11 [−0.91, +0.68] | 1,861 → 1,482 | 20.4% |

三档 95% CI 均跨 0，结论为「记忆 token 稳定降低 20%–23% 且问答准确率持平」。

---

## 决策

### 决策 1：独立 `ContextPacker` 算子（基类 + Producer + 实现），置于 `cut_to_top_k` 之后、`Discloser` 之前

链路位置：`threshold` → `secondary_rank` → `cut_to_top_k` → **`pack_context`（`ContextPacker`）** → `disclose`。

- 新增 `RetrievalOperatorType.CONTEXT_PACKER`、抽象基类 `ContextPacker(RetrievalOperator)` 与注册式工厂 `ContextPackerProducer`（顶层命名空间 `context_packer`）。
- 默认实现 `AdaptiveContextPacker` 在 `context_packer_impl` 下以 `@ContextPackerProducer.register("default")`（及别名 `"adaptive"`）自注册；业务可注册自定义实现并通过 YAML `context_packer.default.target` 替换。
- 输出保持逐条 `ScoredMemoryUnit`（即按 `MemoryUnit` 一条一条输出），维持原相关性相对顺序；被改写正文的条目通过 `dataclasses.replace` 生成新 `MemoryUnit` 副本并清空旧 `layers` 缓存，绝不原地修改底层 KV 真源对象。随后候选无缝进入既有 `Discloser` 产出 `list[RetrievedItem]`。

### 决策 2：Mixed-Score 价值排序 + 按规模三级无模型自适应压缩

预算 = `budget_ratio` × 当前 K 条记忆原始 token 总和。

1. **Mixed-Score 优先级**：
   `0.45 × minmax(检索得分) + 0.50 × 问题词重合度 − 0.05 × 单条长度占比`（全 0 分回退名次线性代理）。开发集（会话 0–4，762 题）消融表明，长程历史问答引入 0.50 时间新近度权重会误删早期证据（−1.97pp vs 去掉新近度后的 −1.31pp），故新近度置 0。
2. **三级自适应策略（`strategy="auto"`）**：
   - `≤ 10` 条（`role_compact`）：对话角色称呼缩写为 `U / A`（排除 `a/an assistant` 普通名词与复数 `users`），剥离与 `Temporal.t_event` 同日的 `On YYYY-MM-DD, ` 冗余前缀；仍超预算时对尾部低分项做电报式精简。
   - `11–20` 条（`telegraph`）：Mixed-Score 前 `h` 条保留完整原文，其余剔除实词覆盖率 ≥ 80% 的重复项后去英文虚词，强制保留否定词（`not/no/never/without`）、首字母大写词与含数字词，`h` 取满足预算的最大值。
   - `> 20` 条（`drop`）：按 Mixed-Score 从高到低贪心保留不超预算的条目，其余整条剔除，保留项原文不变。极端预算一条都装不下时保底保留 Mixed-Score 第 1 名。

### 决策 3：默认关闭，支持配置级与查询级双层开关及异常降级

- 总开关 `context_pack_enabled` 出厂在 `globals` 中默认为 `False`（写在 `globals` 或 `retriever.<name>.params` 均生效，本实例 `params` 优先）；查询级可通过 `RetrievalQuery.context_pack: bool | None` 单次覆盖。
- 开启时记录 `context_pack` 轨迹步（含生效策略、进出条数、前后 token 与 `kept/telegraphed/dropped` 明细）；树展开请求（`expand_depth > 0`）自动跳过（记 `skipped=expand_depth`），避免改写或剔除待展开的结构父节点；自定义算子抛异常或返回非法非多重集子集（空结果、增条、伪造 id、输入已有重复 id 时输出再膨胀）时记 warning 与轨迹 `error`，自动降级保留原 `top_k` 候选。

---

## 拒绝的方案

| 方案 | 否决理由 |
|---|---|
| 融合进 `Fuser` | `Fuser` 处于精排前，负责多路合并粗排（80–120 条候选）；提前改写残句会污染下游 `Reranker` 语义打分，且最终 K 条尚未确定，无法按条数路由策略或计算 Top-K 预算 |
| 融合进 `Reranker` | `Reranker` 是 `common` 层跨读写共享的打分插件（`rerank -> list[float]`，源码约束不改写正文），且在 `rerank_enabled=false` 时整步跳过，绑定后会导致无精排检索无法使用压缩 |
| 融合进 `Discloser` | 违反铁律 10（`Discloser` 必须按序 1:1 单条塑形，严禁过滤删条），会污染复用 `Discloser` 的树展开 `Expander`，且与 `StructuredDiscloser` 等既有披露器互斥；拆成前置独立算子可与任意 `Discloser` 正交组合 |
| 仅提供检索层外围纯函数、不接入 `PipelineRetriever` | 调用方无法通过标准 `Config` 与 `Retriever.retrieve` / `MemoryAPI.search` 直接启用，缺少统一生命周期、轨迹可观测与异常降级保护 |
| LLM 摘要合并或小模型压缩（LLMLingua-2） | LLM 摘要每题多一次模型调用且时延增加 10%–27%，准确率提升不显著；LLMLingua-2 CPU 耗时 ~1s/条且数字/专名保留率仅 ~80% |

---

## 验证

- `tests/unit/retrieval/test_context_packer.py`：算子类型与健康检查、`ContextPackerProducer` 注册与命名空间发现、`globals` / `retriever.params` 开关优先级、三档自动路由边界值、参数校验、角色缩写及普通名词反例、电报式保留规则与重复剔除、4 策略 × 3 预算比不超预算、KV 真源不可变性与相关性保序。
- `tests/integration/retrieval/test_pipeline_retriever.py`：`PipelineRetriever` 端到端串联（`secondary_rank` → `context_pack` → `disclose` 轨迹顺序、逐条 `RetrievedItem` 输出、大 `top_k` 删条、查询级 `context_pack=True/False` 覆盖、自定义算子异常与非法返回降级，含输入重复 id 时输出膨胀的多重集子集守卫）。
- `tests/unit/retrieval/test_pipeline_stage_guards.py` 与 `tests/unit/config/test_defaults.py`：阶段前置状态守卫与出厂默认配置契约。

---

## 已知遗留

1. **中英文规则差异**：Mixed-Score 排序与 `drop` 删条同时支持中英文；`role_compact` 角色缩写与 `telegraph` 英文虚词表面向英文记忆设计，中文记忆在电报式下不删虚词，自动退化为接近删条（中文场景可直接配置 `strategy: drop`）。
2. **默认近似分词计数**：为保持核心包零第三方依赖，默认采用词块/汉字/标点正则近似 BPE 计数（LoCoMo 上与 `cl100k` 实测降幅相差 < 1.5pp）；Python 侧构造 `AdaptiveContextPacker` 时可通过 `count_tokens` 注入目标模型精确分词器。
