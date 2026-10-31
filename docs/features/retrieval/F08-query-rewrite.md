# F08 — 上下文感知 Query Rewrite

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-22 |
| 影响范围 | `jiuwen_memory/common/type_def/context.py`、`jiuwen_memory/common/type_def/retrieval.py`、`jiuwen_memory/api/memory_api.py`、`jiuwen_memory/api/memory_api_impl/`、`jiuwen_memory/retrieval/query_parser_impl/simple_query_parser.py`、`jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`、`docs/specs/S02-memory-api.md`、`docs/specs/S04-retrieval.md`、中英文检索 API 文档 |
| 测试基线 | `uv run pytest tests/unit/retrieval tests/unit/api/test_recall_context.py tests/unit/jiuwen_memory_entry/test_handler.py tests/unit/jiuwen_memory_entry/test_api_contract.py tests/unit/multimodal/test_multimodal_adapter.py tests/integration/retrieval/test_pipeline_retriever.py -q` 通过；`git diff --check` 干净 |
| Refs | #248 |

## 背景

记忆检索的输入经常是口语化、带寒暄或省略主体的请求，例如“它的回滚步骤是什么？”。直接把这类文本交给分词、特征抽取和向量化，会产生两个问题：

1. **格式与索引分布不对齐**：问候语、转述框架和上下文噪声会稀释真正有检索价值的名词、ID、时间和领域词。
2. **意图不自包含**：当代词或被省略的主体只出现在最近对话里时，孤立 query 无法表达要检索的对象，候选召回会依赖运气。

因此，显式开启 rewrite 后，系统先把清洗后的 query 改写为一条简洁、自包含、面向记忆索引的检索短语；调用方也可以显式提供最近上下文，用于消解指代和补全省略主体。改写只增强软召回信号，不生成答案，也不改变权限和硬过滤条件。

## 决策

### 整体链路

```text
Context.extensions["query_context"]
  -> API 边界校验并转换为 tuple[str, ...]
RetrievalQuery(query_context=...)
  -> sanitize_query 清洗当前 query
  -> 规范化最近上下文
  -> system/user prompt 组装
  -> llm.chat(temperature=0)
  -> strict JSON 解析
ParsedQuery.rewritten
  -> tokenizer / feature extractor / embedder
ParsedQuery 软召回信号
  -> 多路召回
  -> 融合
  -> Reranker 使用 parsed.rewritten 精排
```

`Context.extensions["query_context"]` 是唯一的上下文入口。该 key 在 API 边界被解释并移除，不会作为普通 extension 继续透传，也不会进入鉴权、scope、filters 或 `as_of`。

### 上下文契约

上下文是调用方显式筛选后的近期消息或摘要，顺序由调用方保证，最近信息放在尾部。边界规则如下：

- key 不存在、空 `list`、空 `tuple` 均表示无上下文；
- 合法值为 `list[str] | tuple[str, ...]`，API 转换为 `RetrievalQuery.query_context: tuple[str, ...]`；
- key 存在但值为 `None`、非序列，或元素不是字符串时抛 `ValidationError`，要求调用方修正输入；
- Parser 会 strip 每项并删除空白项，只保留最近 5 项，拼接后最多保留末尾 8192 字符；
- 系统不自动读取消息表、KV、session 或其他存储；调用方对上下文的相关性与授权负责；
- 自定义 `QueryParser` 可以读取或忽略 `query_context`，API 不要求所有插件都实现上下文改写。

### Prompt 与输出

`SimpleQueryParser` 使用 system/user 分离的固定任务 prompt。user 消息由 XML-escaped 分区组成：

```text
<current_query>
当前 query
</current_query>

<recent_context>
最近上下文
</recent_context>
```

`<recent_context>` 只在上下文非空时渲染。当前 query 放在最前面，表示当前请求决定检索意图；上下文只用于消解指代、补全省略主体和解释缩写。`&` 与 `<` 会被转义为 `&amp;` 与 `&lt;`，防止输入内容伪造闭合标签或改变任务。

LLM 必须只返回一个 JSON 对象：

```json
{"rewritten": "PaymentClient 到 PaymentGateway 的支付迁移方案 回滚步骤"}
```

如果当前消息明确没有记忆检索意图，例如单纯问候、感谢或确认，则返回：

```json
{"rewritten": null}
```

`null` 是显式契约，不与模型失败或解析失败混用。感谢后接问题、指代无法消解、上下文没有答案、不确定记忆是否存在时，都不能返回 `null`；这些场景应输出保守的非空 query，或由非法输出触发 raw 回退。

### 解析、回退与短路

| 输入/异常 | 行为 | `rewrite_status` |
|---|---|---|
| rewrite 关闭或未注入可用 LLM | `rewritten` 为 sanitized raw | `off` |
| 非空字符串 | 使用改写结果驱动软召回与精排 | `used` |
| 显式 `rewritten: null` | `rewritten=""`，跳过召回 | `no_intent` |
| 缺字段、空串、数组、数字、bool、非法 JSON | 回退 sanitized raw | `fallback` |
| 改写结果超过 `max(512, len(sanitized raw))` | 回退 sanitized raw | `fallback` / `output_too_long` |
| LLM 调用异常 | 回退 sanitized raw | `fallback` / `llm_error` |

解析器接受完整 Markdown code fence 包裹的 JSON，但不从普通文本中恢复散落的 JSON，也不接受数组或额外任务输出。长度上限取 `max(512, sanitized raw 长度)`：短 query 允许必要扩展，长 query 不因固定阈值被误伤。

`no_intent` 产物不调用 tokenizer、embedder、feature extractor 或时间规则解析器；tokens、keywords、entities、vector、channels 与时间窗均为空，raw、filters、`as_of` 和 extensions 保留。`PipelineRetriever` 在 DomainStore 检索前短路，返回空结果，并在轨迹记录：

```json
{"rewrite": "no_intent", "skipped": "no_retrieval_intent"}
```

### 与召回、精排和硬约束的边界

- `rewritten` 驱动 tokenizer、keywords、entities、vector 与 Reranker；`raw` 保留 sanitized 原文，用于行为可观测与回退语义。
- Reranker 使用 `ParsedQuery.rewritten`。关闭或 fallback 时该值就是 sanitized raw，因此不额外引入第二套精排输入。
- `filters`、`as_of`、scope 与权限完全来自可信调用参数；LLM 与最近上下文不能生成或修改这些硬约束。
- event-time 窗仍由规则解析器从 sanitized raw 中解析，不由改写文本引入新的时间事实。
- rewrite 与实体抽取不互斥：实体抽取在改写结果上运行，得到的实体只作为图召回种子，不参与权限判定。
- 跨空间与多模态路径会多次进入基础检索分支；每次 `parser.parse` 至多一次 LLM 改写调用，但不承诺一个外部请求全局只调用一次。
- QueryParser 不设置任务级 `max_tokens`，也不新增应用层重试；`llm_max_tokens`、`llm_max_retries` 继续由 OpenAI/DashScope 等共享 LLM 后端统一配置。

### 可观测性

`ParsedQuery` 携带：

| 字段 | 语义 |
|---|---|
| `rewrite_status` | `off` / `used` / `fallback` / `no_intent` |
| `rewrite_fallback_reason` | fallback 原因；成功、关闭或 no-intent 时为空 |

parse 轨迹记录 rewrite 状态和 fallback 原因；rerank 轨迹记录 `query_source=parsed.rewritten` 与 rewrite 状态。轨迹和生产日志不记录 raw query、上下文、改写结果或模型原始输出，避免调试数据变成敏感文本外泄面。

## 示例

### 无上下文去噪

输入：

```text
do you remember where alice likes coffee?
```

输出：

```json
{"rewritten": "alice coffee"}
```

`alice coffee` 进入分词、关键词、实体和向量化；原始寒暄与转述框架不再参与软召回。

### 上下文消解指代

输入：

```python
query = "之前那个迁移方案的回滚步骤是什么？"
context = Context(
    scope=scope,
    extensions={
        EXT_QUERY_CONTEXT: (
            "用户：我们之前讨论过支付服务迁移。",
            "助手：倾向于从 PaymentClient 切换到 PaymentGateway。",
        )
    },
)
```

期望输出：

```json
{"rewritten": "PaymentClient 到 PaymentGateway 的支付迁移方案 回滚步骤"}
```

### 无检索意图

输入：

```python
query = "谢谢，明白了"
context = Context(
    scope=scope,
    extensions={EXT_QUERY_CONTEXT: ("用户：刚询问并获得支付迁移方案。",)},
)
```

期望输出：

```json
{"rewritten": null}
```

系统返回空结果，不把历史查询重放为新检索。

## 拒绝的方案

| 方案 | 结论 | 依据 |
|---|---|---|
| `llm.generate(text)` 自由文本改写 | 否决 | 没有任务边界和可解析契约，模型回答、Markdown 或说明文字会直接污染召回信号 |
| 自动读取 session 历史 | 否决 | 扩大敏感数据暴露面；调用方最清楚哪些上下文相关且已获授权 |
| 单个长字符串上下文 | 否决 | 调用方需自行处理换行与顺序，Parser 难以稳定截断 |
| 带角色/时间戳的完整消息协议 | 暂缓 | 当前只需要近期上下文片段，完整协议是更大的公共契约变更 |
| 一对多 query fan-out | 暂缓 | 会改变 `ParsedQuery` 与召回契约；先用一对一上下文补全验证收益 |
| 空字符串表示无意图 | 否决 | 与非法输出、模型失败语义混淆；显式 JSON null 才可观测 |
| LLM 生成 filters、scope 或时间窗 | 否决 | 混淆软召回与硬约束，扩大注入和越权面 |
| Parser 内新增应用层重试 | 否决 | 检索在用户请求路径上，失败回退 sanitized raw 比阻塞等待更符合延迟预算 |
| 任务级硬编码 `max_tokens=128` | 否决 | reasoning 模型可能把思考 token 计入输出预算，固定小值容易截断有效 JSON；模型差异由共享 LLM 后端配置承担 |

## 验证

- **Parser 单测**：覆盖无上下文改写、上下文消解、prompt 转义、上下文截断、strict JSON、显式 null、非法 JSON、空输出、超长输出、LLM 异常、rewrite 关闭与空 query。
- **API 边界测试**：覆盖 `Context.extensions["query_context"]` 的 list/tuple/空值/非法值、top-level 误传拒绝、key 移除和 typed tuple 下传。
- **Pipeline 集成测试**：覆盖 rewritten 驱动软召回、Reranker 使用 `parsed.rewritten`、no-intent 在所有 DomainStore 检索路径前短路、fallback 状态与轨迹。
- **入口适配测试**：覆盖 HTTP/CLI/MCP 上下文透传、跨空间检索与多模态基础检索路径。

## 已知遗留

- strict prompt、512 字符基线、最近 5 条上下文和 8192 字符上下文上限是当前实现常量，暂不暴露配置；待部署反馈后再决定是否需要配置面。
- 真实模型的改写质量、命中率收益与延迟开销需要部署侧基准验证；本特性测试不依赖外部模型。
- 自定义 `QueryParser` 未被强制要求消费上下文；是否支持上下文改写由各实现声明并测试。
