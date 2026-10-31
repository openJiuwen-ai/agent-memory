# F10 — LLM Reranker：语义去重与冲突消解

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-28 |
| 影响范围 | `jiuwen_memory/common/reranker/`（基类签名升级 + 新增 `llm_reranker.py`）、`jiuwen_memory/common/type_def/memory.py`（数据结构引用）、`jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`（调用点 + resolve 阶段）、`jiuwen_memory/retrieval/schema_temporal/`（调用点适配）、`jiuwen_memory/config/routing.py`（门面转发）、`docs/specs/S07-common.md`、`docs/specs/S04-retrieval.md`、`jiuwen_memory/common/AGENTS.md`、`jiuwen_memory/retrieval/AGENTS.md` |
| 测试基线 | 待实现后补 |
| Refs | — |

## 一、背景

### 1.1 问题

检索返回的候选中可能混着两类问题：

1. **近重复**：不同 `unit_id`、内容语义等价或高度相近的记忆同时出现在结果中——浪费下游 prompt 预算。
2. **矛盾事实**：同一属性的两个互斥值（如"住北京"与"搬到上海"）同时出现且无任何冲突信号——下游 Agent 不知道以哪条为准。

### 1.2 现有机制兜不住

当前检索链路的所有去重都是 **id 级或精确字符串级**：

| 位置 | 去重口径 |
|---|---|
| `UnitReader.load` | unit_id 去重 |
| `fuser_impl/layered_merge.py` | 同通道多层命中同 unit_id 取 MaxP |
| RRFFuser 等三个 Fuser | 跨通道同 unit_id 融合 |
| `cross_space.merge` | `content.strip()` 精确匹配 |
| `MultimodalRetriever._rrf_merge` | unit_id |

即：**不同 unit_id、内容相同/相近/矛盾的记忆会同时出现在最终结果里**。

写侧有语义去重（`Evolver._dedup_batch` LLM 判定 ADD/UPDATE/SUPERSEDE/NOOP），但走后台异步、偏保守（"If unsure, prefer add"）、scope 粒度——读侧需要一道快而显式的兜底。

### 1.3 Reranker 当前的局限

Reranker 基类 `rerank(self, query: str, texts: list[str]) -> list[float]` 入参是纯字符串，无 MemoryUnit 上下文。LLM 在做去重/冲突消解时拿不到 `tier / temporal / tags / provenance` 等辅助判据，无法做有效的时间裁决与优先级判断。

## 二、设计目标

1. 基类接口从 `str` 升级为 `MemoryUnit`，让重排实现可消费 unit 的多字段作为辅助判据。
2. 在 reranker 中新增 `llm_reranker.py` 实现，既支持 rerank 打分，也支持语义去重与冲突消解。
3. 全链路调用点同步适配，保持既有降级语义不变——去重/冲突消解是"锦上添花"，任何失败不得影响检索可用性。

## 三、详细设计

### 3.1 基类接口

`jiuwen_memory/common/reranker/base.py`：

```python
class Reranker(Plugin):
    @abstractmethod
    def rerank(self, query: str, units: list[MemoryUnit]) -> list[float]:
        """对每条 MemoryUnit 计算与 query 的相关性得分（顺序与输入一致）。
        实现可消费 unit.content / tier / temporal / tags / entities 等字段。
        排序/截断由调用方完成。"""

    def rerank_texts(self, query: str, texts: list[str]) -> list[float]:
        """str 入参的兼容入口（schema_temporal 等无完整 unit 的路径用）。
        默认把每个 text 包成 MemoryUnit(segments=[Segment(content=t)]) 再调 rerank。"""
        units = [MemoryUnit(segments=[Segment(content=t)]) for t in texts]
        return self.rerank(query, units)

    def resolve(self, query: str, units: list[MemoryUnit]) -> RerankResolution:
        """可选：语义去重 + 冲突消解。默认 keep_all（不丢任何候选）。
        仅 LLMReranker 覆写。任何异常由调用方降级，实现内部不吞。"""
        return RerankResolution.keep_all(len(units))
```

设计要点：

- `rerank` 主接口入参为 `list[MemoryUnit]`，实现可消费 `unit.content / tier / temporal / tags / entities / provenance` 等字段。
- `rerank_texts` 是非抽象默认实现，供 schema_temporal 等无完整 MemoryUnit 的路径使用——内部把每个 text 包成空 MemoryUnit 再调 `rerank`。
- `resolve` 是非抽象默认实现，默认 `keep_all`——非 LLM 实现零改动；调用方统一调 `resolve`，不需要 `hasattr` 判断。

### 3.2 数据结构

放在 `jiuwen_memory/common/reranker/base.py`（与 Reranker 契约同处，符合"工厂随契约"铁律）：

```python
@dataclass
class DuplicateGroup:
    """重复分组：簇内代表 + 被去重的条目（供轨迹统计用）。"""
    representative: int      # 保留的代表下标（输入下标）
    dropped: list[int]        # 被去重的下标

@dataclass
class RerankResolution:
    """去重 + 冲突消解的结果。"""
    kept_indices: list[int]              # 保留的输入下标（有序，子序列）
    duplicates: list[DuplicateGroup]     # 重复分组（轨迹统计用）
    conflict_dropped: list[int]          # 冲突中被丢弃的下标（轨迹统计用）

    @classmethod
    def keep_all(cls, n: int) -> "RerankResolution":
        return cls(kept_indices=list(range(n)), duplicates=[], conflict_dropped=[])
```

设计要点：

- `kept_indices` 是输入下标的**稳定子序列**——不合成新内容，每个保留下标仍指向真源 MemoryUnit，`unit_id` ↔ 真源对应不断裂。
- 平局兜底时 `kept_indices` 包含双方——不悄悄裁决，把冲突暴露在轨迹里。
- `duplicates` 与 `conflict_dropped` 仅供轨迹 detail 计数，不向终端调用方透出结构化冲突标注。

### 3.3 LLMReranker 实现

`jiuwen_memory/common/reranker/reranker_impl/llm_reranker.py`，注册名 `@RerankerProducer.register("llm")`。

#### 依赖注入

- `llm: LlmProducer.dep(config)` —— 引用具名 LLM 实例（与构建侧共用同实例，符合双侧同一铁律）。
- `tokenizer: TokenizerProducer.dep(config, default="whitespace")` —— 兜底打分用（LLM 不可用时降级）。

#### 配置参数

```yaml
reranker:
  default:
    target: llm
    params:
      llm: default                    # 引用具名 LLM
      enable_resolve: true            # 是否启用去重/冲突消解
      llm_timeout: 30
      llm_max_candidates: 30          # 送 LLM 的最大候选数（超过则先按 score 截断）
```

#### rerank 实现（打分）

- 一次 LLM 调用处理全部候选：prompt 里列 query + N 条 `unit.content`，输出 JSON `[score1, score2, ...]`。
- prompt 可携带 `unit.tier` / `unit.tags` 作为辅助判据（如"核心记忆优先"）。
- LLM 异常时降级为 tokenizer 词重叠打分，抛 `BackendError` 让 `pipeline_retriever` 走既有降级路径（S04 §72-74）。

#### resolve 实现（去重 + 冲突消解）

两步走：

**步骤 1：LLM 识别关系**

- prompt 里列所有候选（`{index, content, tier, t_event, t_ingest}`），让 LLM 一次性输出所有嫌疑对的关系标签。
- 输出 JSON：`[{pair: [idx_a, idx_b], relation: "duplicate"|"conflict"|"independent"}]`
- 保守默认：拿不准判 `independent`（漏判代价=没去掉重复，误判代价=丢信息）。
- 候选数超过 `llm_max_candidates` 时，先按 rerank score 截断再送 LLM。
- LLM 异常 / 非法 JSON / 引用不存在下标 → 抛异常让调用方降级（keep_all）。

成本控制靠批处理：一次 prompt 列 N 条候选，输出所有嫌疑对的关系，不是逐对调用。N=30 候选时 prompt 约 2-3K tokens，单次调用延迟 1-2s，成本可控。超量候选先按 rerank score 截断——高分候选更可能进冲突对。

**步骤 2：确定性裁决**

不靠 LLM 裁决，裁决由确定性元数据规则完成——可单测穷举、可审计、可复现。

- **相关性 score 不参与裁决**——冲突双方对 query 同等相关，score 是"与查询的相关度"不是"真伪度"。

裁决阶梯（从上往下，命中即止）：

| 优先级 | 裁决依据 | 语义 |
|---|---|---|
| 1 | `t_event` 可比 | 新者胜（"搬到上海"压过"住北京"） |
| 2 | `t_event` 均缺失 → 回退 `t_ingest` | 系统何时知道它，新者胜 |
| 3 | 仍平局 → tier 更高者 | SEMANTIC 画像 > EPISODIC 原始片段 |
| 4 | 仍平局 → provenance 更完整者胜 | 合成记忆优于单源片段 |
| 5 | 仍平局 → **双保留** | 不悄悄裁决，两条都进 `kept_indices` |

按关系类型分别处理：

- **duplicate**（语义等价，无矛盾）：保留簇内代表——rerank score 高者，平局取 content 更长者。
- **conflict**（互斥事实）：按上述裁决阶梯选赢家，输家进 `conflict_dropped`；平局则双保留。
- **independent**（可并存，如"喜欢爬山"与"喜欢打球"）：各自保留，不合并也不丢。

**为什么不靠 LLM 自由裁决赢家**：
- 不可复现、不可审计，prompt 一漂移即行为漂移。
- 确定性规则可单测穷举，LLM 质量只影响识别召回，不影响裁决正确性。

### 3.4 既有实现适配

| 文件 | 适配方式 |
|---|---|
| `reranker_impl/overlap_reranker.py` | `rerank(self, query, units)` 内部 `texts = [u.content for u in units]`，其余逻辑不变 |
| `reranker_impl/bge_reranker.py` | 同上；`health()` 里 `self.rerank("health check", [MemoryUnit(segments=[Segment(content="health check")])])` |
| `reranker_impl/api_reranker.py` | 同上；空文本过滤逻辑改读 `u.content` |
| `config/routing.py` | `RoutingReranker.rerank(query, units)` 转发；新增 `rerank_texts` / `resolve` 转发（委托 active 实例） |

### 3.5 调用点适配

**主链路 `pipeline_retriever.py`**：

rerank 打分调用：

```python
scores = self._reranker.rerank(parsed.rewritten, [c.unit for c in survivors])
```

rerank 之后、threshold 之前，新增 resolve 调用：

```python
if do_rerank and self._reranker is not None and survivors:
    # rerank 打分 + 排序（既有逻辑）
    # ...
    # 去重/冲突消解
    try:
        resolution = self._reranker.resolve(parsed.rewritten, [c.unit for c in survivors])
        survivors = [survivors[i] for i in resolution.kept_indices]
        record_step(
            "resolve",
            t0,
            n=len(resolution.kept_indices),
            detail={
                "in": pre_count,
                "out": len(resolution.kept_indices),
                "duplicates": len(resolution.duplicates),
                "conflict_dropped": len(resolution.conflict_dropped),
            },
        )
    except Exception as exc:
        # 降级：keep_all，轨迹记 degraded
        record_step(
            "resolve", 0.0, n=len(survivors),
            detail={"skipped": "degraded", "error": safe_error_message(exc)},
        )
```

resolve 的输出是输入候选的稳定子序列（允许冲突对相邻重排），每个保留下标仍指向真源 MemoryUnit。不在读路径上合并/改写内容——内容变更的单一真相源是写侧 Evolver。

**schema_temporal 路径 `searcher.py` 与 `shrink.py`**：

```python
scores = [float(score) for score in reranker.rerank_texts(query, texts)]
```

schema_temporal 的 Property 精排是"同实体不同属性排序"，不存在"重复/冲突"语义，不调 `resolve`。

### 3.6 测试桩适配

参数从 `texts: list[str]` → `units: list[MemoryUnit]`，桩内 `texts = [u.content for u in units]` 即可保持原打分逻辑。示例：

```python
# 适配前
def rerank(self, query: str, texts: list[str]) -> list[float]:
    return [1.0 - i / 100.0 for i in range(len(texts))]

# 适配后
def rerank(self, query: str, units: list[MemoryUnit]) -> list[float]:
    return [1.0 - i / 100.0 for i in range(len(units))]
```

涉及文件：

- `tests/integration/retrieval/test_pipeline_retriever.py`（三处测试桩）
- `tests/unit/retrieval/test_rollup.py`（Mock side_effect）
- `tests/unit/retrieval/test_storage_pipelines.py`（测试桩）
- `tests/unit/common/test_bge_reranker.py`（多处直接调用）
- `tests/unit/common/test_api_reranker.py`（多处直接调用）
- `tests/unit/config/test_late_binding.py`（直接调用）

### 3.7 降级策略

遵循 S04 §72-74 既有降级语义：

| 阶段 | 异常类型 | 行为 |
|---|---|---|
| `rerank` | `BackendError` | 既有路径：未精排候选继续，轨迹记 `skipped=reranker_unavailable` |
| `resolve` | 任何异常 | keep_all（不丢候选），轨迹记 `degraded` + 脱敏 error |
| `resolve` LLM 识别 | 非法 JSON / 引用不存在下标 | 抛异常让上层降级（不静默吞） |

核心原则：去重/冲突消解是"锦上添花"，不是必需品——任何失败不得影响检索可用性。

### 3.8 轨迹记录

`TrajectoryStep.stage` 新增 `resolve`（与 `parse/recall/fuse/rerank/threshold/disclose` 同级），detail 记：

- `in` / `out`：进入/保留候选数
- `duplicates`：重复分组数
- `conflict_dropped`：冲突丢弃数
- `degraded`（或 `skipped` 原因 + 脱敏 error）

## 四、拒绝的方案

### 4.1 让 LLM 自由裁决赢家

不可复现、不可审计，prompt 一漂移行为就漂移。本方案限定 LLM 输出为关系标签（duplicate/conflict/independent），裁决由确定性元数据规则完成——规则可单测穷举，LLM 质量只影响识别召回，不影响裁决正确性。

### 4.2 用相关性 score 裁决冲突

冲突双方对 query 同等相关（都命中同一属性），score 是"与查询的相关度"不是"真伪度"。用 score 裁决等于不可审计的黑箱。本方案相关性 score 不参与裁决。

### 4.3 读路径 LLM 内容合成

让 LLM 现场把几条候选合并改写成一条新文本——断链（合成条目没有真实 `unit_id` 可指）、质量全押 LLM（合成丢细节无回滚余地）、职责错位（内容合并是写侧 Evolver 既有职责）。本方案只做选择（保留子序列），不改写内容。

### 4.4 后处理直接改写真源

读路径写存储，破坏读写分离与检索幂等。同一次查询重复执行会产生不同副作用。修复落盘走写侧演进回流，本方案只做读时治理。

### 4.5 默认开启

LLM 档每次检索 +1 次模型调用（0.5–3s），出厂默认开启会让所有调用方承担延迟。本方案缺省关闭，显式启用。

## 五、文档同步

| 文档 | 动作 | 内容 |
|---|---|---|
| `docs/specs/S07-common.md` | 修订 | rerank 签名改为 `(query, units: list[MemoryUnit])`；新增 `rerank_texts` / `resolve` 契约；`RerankResolution` / `DuplicateGroup` 数据结构 |
| `docs/specs/S04-retrieval.md` | 修订 | 检索链路补 `resolve` 阶段；调用点描述同步；降级语义补 resolve |
| `jiuwen_memory/common/AGENTS.md` | 更新 | reranker 段补"实现可消费 unit 多字段"；新增 `llm_reranker` 实现 |
| `jiuwen_memory/retrieval/AGENTS.md` | 更新 | 检索链路补 `resolve` 步；trajectory stages 补 `resolve` |

## 六、验证

待实现后补全：

- **新增** `tests/unit/common/test_llm_reranker.py`：
  - `rerank` 正常路径：mock LLM 返回合法 JSON 分数，验证顺序与输入一致
  - `rerank` 降级路径：LLM 抛异常，验证降级为 tokenizer 词重叠打分 + 抛 `BackendError`
  - `resolve` 正常路径：mock LLM 返回合法分组 JSON，验证 duplicate 留代表、conflict 按时间规则裁决、independent 保留
  - `resolve` 并列事实回归用例："喜欢爬山"+"喜欢打球"判 independent、两条保留、不进时间裁决
  - `resolve` 降级路径：LLM 抛异常 / 非法 JSON / 引用不存在下标，均回退 keep_all
  - `llm_max_candidates` 截断：超量候选先按 score 截断
- **新增** `tests/unit/retrieval/test_resolve_stage.py`：裁决纯函数分支穷举——`t_event` 新者胜 / `t_event` 缺失回退 `t_ingest` / tier 兜底 / 全平局双保留
- **增补** `tests/integration/retrieval/test_pipeline_retriever.py`：
  - `resolve` 装配注入：轨迹含 `resolve` 步，结果为处理后子序列
  - `resolve` 装配未注入：轨迹 detail 含 `skipped`
  - `resolve` 实现抛异常：降级回原候选，轨迹 detail 含 `degraded`
- **基线**：`pytest -m unit` 全绿（exit 0）；`ruff check` 零告警

## 七、已知遗留

- **冲突回流写侧**：resolve 识别出的 duplicate/conflict 簇可回流为 CONSOLIDATE 演进任务，让修复落盘而非每次检索重复裁决。retrieval 不得直接依赖 control/Scheduler，需经端口/事件由装配层接线——独立特性，首版不做。
- **cross_space / MultimodalRetriever 接线**：接口设计为可复用，但跨空间合并后与多模态三分支融合后的调用点首版不接（各自已有 unit_id/精确内容去重兜底），后续按需补。
- **PromptRegistry 化**：`PromptRegistry` 现住 construction 层，retrieval 直接 import 会引入跨层依赖。首版 prompt 内置于实现、支持 params 覆盖文本；registry 化需先将其下沉 common 或经装配传文本，另行评估。

## 八、实施顺序

1. **基类接口升级**（`base.py` + 数据类）+ 既有实现改签名 + 调用点适配 + 测试桩适配 —— 单独一个 PR，保证全绿
2. **LLMReranker 实现**（`llm_reranker.py`）+ `pipeline_retriever` 接 resolve + 轨迹 + 降级 —— 第二个 PR
3. 两个 PR 都同步文档（specs + AGENTS.md）
