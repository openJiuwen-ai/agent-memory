# F08 — 保守型 LLM Query Rewrite

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-13 |
| 影响范围 | `jiuwen_memory/common/type_def/retrieval.py`、`jiuwen_memory/retrieval/query_parser_impl/simple_query_parser.py`、`jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`、`docs/specs/S04-retrieval.md`、中英文检索 API 文档 |
| 测试基线 | `uv run pytest tests/unit/retrieval/test_query_parser.py -q` 17 passed；`uv run pytest tests/integration/retrieval/test_pipeline_retriever.py -q` 35 passed；`uv run pytest tests/unit/retrieval tests/integration/retrieval -q` 213 passed；`uv run ruff check` 干净 |
| Refs | #248 |

---

## 背景

`SimpleQueryParser` 原先开启改写时直接调用 `llm.generate(text)`，把自由文本回复当作
`ParsedQuery.rewritten`。该路径没有任务 prompt、输出契约、长度边界和失败观测：模型一旦
回答问题、输出 Markdown 或注入额外说明，会直接污染分词、向量和特征抽取。

## 决策

采用保守的单 query 改写：

```text
raw query
  -> sanitize_query
sanitized raw query
  -> strict LLM rewrite
rewritten query
  -> tokenizer / feature extractor / embedder
ParsedQuery soft recall signals
```

### 任务 prompt 与输出契约

rewrite prompt 作为模块级常量放在 `simple_query_parser.py`，与 construction 算子的
stable prompt 组织方式一致。`PromptRegistry` 面向构建阶段动态 prompt key，不承载本任务。

调用使用既有 `LLM.chat()`：

```python
messages = [
    ChatMessage(role="system", content=_REWRITE_SYSTEM_PROMPT),
    ChatMessage(role="user", content=f"Query: {text}"),
]
response = llm.chat(messages, temperature=0)
```

LLM 必须返回单个 JSON 对象：

```json
{"rewritten": "alice coffee"}
```

解析器只读取非空字符串 `rewritten`，接受完整 Markdown code fence，忽略其他字段。
字符级上限是 `max(512, len(sanitized raw))`：短 query 允许必要扩展，长 query 只要求
改写结果不劣于 raw 长度。不恢复散落在普通文本里的 JSON，不接收数组或多余任务输出。

QueryParser 不设置任务级 `max_tokens`。OpenAI / DashScope 后端继续使用统一的
`llm_max_tokens` 配置，避免检索层理解各模型的 token 计费、reasoning token 和
`max_completion_tokens` 差异。

### 回退与可观测

| 场景 | 结果 | 原因 |
|---|---|---|
| rewrite 关闭或未注入可用 LLM | `rewritten=raw` | `""` |
| LLM 调用异常 | 回退 raw | `llm_error` |
| JSON / 类型不符合契约 | 回退 raw | `invalid_json` |
| `rewritten` 空白 | 回退 raw | `empty_output` |
| `rewritten` 超过 `max(512, len(raw))` | 回退 raw | `output_too_long` |

`ParsedQuery.rewrite_status` 取 `off/used/fallback`；`rewrite_fallback_reason` 只在
fallback 时非空。`PipelineRetriever` 的 parse 轨迹写入：

```python
{"rewrite": "fallback", "fallback_reason": "invalid_json"}
```

轨迹不携带 raw query、rewritten query、模型原始输出或 system prompt，避免调试数据
变成敏感文本外泄面。

### 边界

- 默认 `rewrite_enabled=false`，出厂检索路径不增加 LLM 调用。
- LLM 只影响软召回信号；`scalar_filters`、`as_of` 透传，event-time 由规则解析 raw 文本。
- Reranker 继续使用 `ParsedQuery.raw`，不用 rewritten。
- 不新增应用层重试；`llm_max_retries` 继续由 OpenAI/DashScope 后端的出站策略控制。
- 不新增 QueryParser 专属 token 上限；`llm_max_tokens` 继续由 LLM 后端统一配置。
- 不新增 HTTP / SDK / MCP 对外 API，也不新增配置项。

## 拒绝的方案

| 方案 | 结论 | 依据 |
|---|---|---|
| 继续使用 `llm.generate(text)` | 否决 | 无 system 任务边界和可解析契约，模型自由文本会直接进入召回信号 |
| 复用 construction 的 JSON 恢复器 | 否决 | 该实现为多对象抽取结果恢复第一个 JSON，容错偏宽；跨层引用也会破坏边界 |
| parser 内新增应用层重试 | 否决 | 检索是用户请求路径，失败后回退 raw 比阻塞等待更符合延迟预算 |
| QueryParser 硬编码任务级 `max_tokens=128` | 否决 | reasoning 模型会把思考 token 计入输出预算，固定小值容易截断有效 JSON；模型差异应由 LLM 后端配置承担 |
| session-aware expansion / 多 query fan-out | 暂缓 | 需要会话上下文和更大的 `ParsedQuery`/召回契约变更，不属于最小可用闭环 |
| LLM 生成 filters、scope 或时间窗 | 否决 | 混淆软召回与硬约束，扩大注入和越权面 |

## 示例

开启后输入：

```text
do you remember where alice likes coffee?
```

LLM 输出：

```json
{"rewritten": "alice coffee"}
```

`ParsedQuery` 用 `alice coffee` 生成 tokens/keywords/vector，raw 仍是清洗后的原 query；
reranker 与硬约束继续使用 raw 和调用方输入。

LLM 返回非法 JSON `not json` 时，`rewritten` 回退 raw，状态为 `fallback`，
轨迹记录 `invalid_json`。

## 已知遗留

- strict prompt 与 512 字符基线上限是当前实现的常量，暂不暴露配置；待部署反馈后再决定是否需要配置面。
- 未做多轮上下文、session-aware expansion 和多 query fan-out。
- 真实模型命中率与延迟收益需部署侧基准验证；本特性测试不依赖外部模型。
