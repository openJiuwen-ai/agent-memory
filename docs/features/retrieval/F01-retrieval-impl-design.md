# F01 — 检索层实现规约（src/retrieval/*_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-06-24 |
| 影响范围 | src/retrieval/{query_parser,recaller,fuser,discloser,retriever}_impl/，docs/specs/S04-retrieval.md |
| 测试基线 | `pytest tests/unit/retrieval tests/integration/retrieval` 全绿（exit 0；真实后端未连通时按约定 skip） |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档**检索层各实现的实现规约**：每个 `*_impl/` 实现对应哪个接口契约、注册名（`target`）、依赖、关键语义与各自取舍。接口契约本身（方法签名、错误语义、披露层级语义、不变量）归 `docs/specs/S04-retrieval.md`；本文聚焦「当前有哪几种实现、各自怎么落地、为什么这样选」。

---

## 背景

检索层（架构 §7，Read 路径）把一条 `RetrievalQuery` 经「查询理解 → 多路召回 → 融合 → 点读复核 → 重排 → 渐进披露」转成 `RetrievalResult`。算子按职责拆分，由 `Retriever` 编排，各算子只做单一职责、彼此不感知：

| 角色 | 接口 / TOP_NAME | 职责 |
|---|---|---|
| **QueryParser** | `query_parser.py` · `query_parser` | `RetrievalQuery` → `ParsedQuery`：分词、可选改写/向量化、时间约束解析、过滤透传 |
| **Recaller** | `recaller.py` · `recaller` | 单通道召回（keyword / vector / graph 各一具名实例），产出 unit 粒度候选 |
| **Fuser** | `fuser.py` · `fuser` | 多路候选跨通道融合排序 |
| **Discloser** | `discloser.py` · `discloser` | 按披露层级（L0/L1/L2/ADAPTIVE）塑形结果内容 |
| **Retriever** | `retriever.py` · `retriever` | 编排上述算子的完整链路；持有 `scope` 显式下推 |

重排算子 **Reranker** 归 `common/reranker`（`TOP_NAME=reranker`，实现 overlap / api / bge_reranker），由 `Retriever` 经 `dep` 注入、在融合后对候选重排——其实现规约见 common 层文档，本文只记其在检索链路中的接入点。

### 注册铁律

- **走工厂的算子**：`query_parser` / `recaller` / `fuser` / `discloser` / `retriever` 各为一个 `Producer`（声明 `TOP_NAME`）；实现文件尾部 `@XProducer.register("target")` 自注册，`*_impl/__init__.py` import 触发，`retrieval.bootstrap.register_operators()` 在装配前统一调用（幂等）。
- **`recaller` 是多具名实例**：同一 `Producer` 下注册 `keyword` / `vector` / `graph` 三种 `target`；`Retriever` 经 `RecallerProducer.dep(config, "<field>", default=...)` 按字段名引用各通道，依据 `globals.vector_enabled` / `graph_enabled` 决定是否接入对应通道。
- **不走工厂的支撑件**：`predicate_builder` / `unit_reader` / `unit_aggregation` / `time_parse` 是纯函数 / 轻量类，由 `PipelineRetriever` 直接构造或调用——它们不是可替换的装配算子，不进两级命名空间依赖图。

---

## 决策：各实现规约

### QueryParser（`query_parser.py` · `QueryParserProducer` · TOP_NAME=`query_parser`）

| target | 类 | 依赖 | 产出 | 关键语义 |
|---|---|---|---|---|
| `simple` | `SimpleQueryParser` | `tokenizer`（`dep`，缺省 `whitespace`）；`embedder`（`dep`，缺省 `hashing`，仅 `vector_enabled` 时接入）；`llm`（`dep`，缺省 `echo`） | `ParsedQuery` | 用注入的 `Tokenizer` 分词（与索引侧同实例 = 同词表）；`llm` 非 `echo` 时改写 query（`rewritten`）；接了 `embedder` 时对 query 向量化（同向量空间）并启用 VECTOR 通道；`filters` 透传为硬过滤、`as_of` 透传为 valid-time 回溯点；文本时间约束经 `time_parse` 规则解析为 event-time 窗 |

**`time_parse.py`（子模块）**：规则版自然语言时间解析——覆盖常见中文相对时间（今天/昨天/本周/上月/最近 N 天…），命中返回 event-time 起止 `datetime`，未命中返回 `(None, None)`；`parse_time` 留可选 `llm` 钩子供规则难穷举的表述按需委托，默认不注入。

### Recaller（`recaller.py` · `RecallerProducer` · TOP_NAME=`recaller`，三具名实例）

| target | 类 | 依赖（缺省） | 召回源 | 关键语义 |
|---|---|---|---|---|
| `keyword` | `KeywordRecaller` | `fulltext_store`（`memory`） | 全文倒排 | 组 `TextQuery`（`scope` 走查询专用入参做原生隔离、`scalar_filters` 落硬过滤），经 `FulltextStore` 召回，按记录 `metadata['unit_id']` 归并到 unit 粒度 |
| `vector` | `VectorRecaller` | `vector_store`（`memory`） | 向量 ANN | 消费 `ParsedQuery.vector`，组 `VectorQuery` 做 ANN 近邻召回；向量索引按 **chunk** 建，命中后按 `metadata['unit_id']` 折叠到 unit（同 unit 多 chunk 取 MaxP）；query 无向量时返回空（该通道不参与） |
| `graph` | `GraphRecaller` | `graph_store`（`memory`） | 属性图多跳 | 按 query 关键词在图里找种子节点（`seed_ids`），BFS 扩展邻居，把关联到的 unit 作为候选——补关键词/向量直接命中之外、靠关系「连点成线」找到的相关记忆；图为空（尚未 ASSOCIATE）时返回空 |

> 共性：各通道只产候选、不感知其他通道，多路并行与合并归 `Retriever` + `Fuser`；chunk → unit 的归并统一经 `unit_aggregation`（MaxP 聚合），使各通道回传 `unit.id`、可被点读与跨通道合并。

### Fuser（`fuser.py` · `FuserProducer` · TOP_NAME=`fuser`）

| target | 类 | 参数（默认） | 关键语义 |
|---|---|---|---|
| `rrf` | `RRFFuser` | `k`（60） | RRF 倒数排名融合：同一 unit 在每路按名次 `r` 贡献 `1/(k+r+1)`，跨路累加得融合分降序；与各路得分量纲无关，单路退化为按名次排序 |
| `weighted_rrf` | `WeightedRRFFuser` | `fusion_rrf_k`（60）、`fusion_channel_weights`（`{}`） | 带通道权重的 RRF：各通道贡献按 `channel_weights` 加权；产出融合证据（每路名次与对最终分的 `contribution`），供可解释排序 |

### Discloser（`discloser.py` · `DiscloserProducer` · TOP_NAME=`discloser`）

| target | 类 | 关键语义 |
|---|---|---|
| `truncating` | `TruncatingDiscloser` | 渐进式披露的**纯内容塑形**：L0 摘要（截断）/ L1 围绕 query 关键词的片段窗口 / L2 全文。Option B 下候选已由 `Retriever` 点读、过滤、（可选）重排，本算子只按 `level` 截/取 `unit.content`，不再点读/过滤/重排 |
| `structured` | `StructuredDiscloser` | 面向 Agent 消费的结构化渐进披露（L0 ≤ 120 / L1 ≤ 260 字符等档位），在内容塑形之上附结构化字段 |

### Retriever（`retriever.py` · `RetrieverProducer` · TOP_NAME=`retriever`）

| target | 类 | 依赖（缺省） | 参数（默认） | 关键语义 |
|---|---|---|---|---|
| `pipeline` | `PipelineRetriever` | `query_parser`（`simple`）；`recaller` 三路 `keyword_recaller`/`vector_recaller`/`graph_recaller`（后两路按 `vector_enabled`/`graph_enabled` 开关接入）；`fuser`（`rrf`）；`discloser`（`truncating`）；`unit_reader` ← `kv_store`（`memory`）；`reranker`（common，`overlap`，仅 `rerank_enabled` 接入） | `rerank_top_m`（50） | 编排完整 Read 链路（见下「编排顺序」），`scope` 作显式首参贯穿下推到各召回路；本类不含召回/打分逻辑，全由注入算子完成 |

### 非工厂支撑件（由 `PipelineRetriever` 直接构造/调用，不进依赖图）

| 件 | 文件 | 职责 |
|---|---|---|
| `build_system_filters` | `predicate_builder.py` | 把检索策略（lifecycle × as_of / event-time 窗 / include_archived）翻译成 `FilterClause`，召回前并入 `ParsedQuery.scalar_filters` 下推到各 Store，使「先排除什么」在索引级生效 |
| `UnitReader` + `passes` / `in_event_window` / `matches_filters` | `unit_reader.py` | 融合截断后把候选 id 点读真源物化为 `MemoryUnit`，再做三道**后置过滤**（有效性 lifecycle×as_of / event-time 窗 / 调用方 `filters`）；序列化反序列化（`loads`）只在此处发生 |
| chunk → unit MaxP 归并 | `unit_aggregation.py` | 各通道命中按 `metadata['unit_id']` 折叠到 unit 粒度取最高分；`metadata` 缺 `unit_id` 时回退记录 id |
| `parse_time` | `time_parse.py` | 文本时间约束 → event-time 窗（规则版，LLM 钩子可选） |

### 编排顺序（`PipelineRetriever.retrieve`，Option B）

1. **查询理解**：`query_parser.parse` → `ParsedQuery`（tokens / 可选 rewritten / 可选 vector / 时间窗 / filters）。
2. **前置谓词**：`build_system_filters` → 并入 `scalar_filters` 下推。
3. **并行多路召回**：每路超采样 `recall_k = max(top_k, rerank_top_m)`，补偿后续过滤/重排的损耗。
4. **融合**：`fuser` 跨路合并排序。
5. **截断重排预算**：取融合结果前 `rerank_top_m`（50）作候选预算。
6. **点读 + 后置过滤**：`UnitReader` 物化真源 `MemoryUnit`，`passes` / `in_event_window` / `matches_filters` 做纵深防御过滤。
7. **（可选）重排**：注入 `reranker` 时对存活候选 `rerank`。
8. **截断 `top_k`**。
9. **渐进披露**：`discloser.disclose`，按 `disclosure` 层级与 `max_tokens` 塑形（`ADAPTIVE` 按预算自选 L0/L1/L2）。
10. **返回** `RetrievalResult`（`items` + 可选 `trajectory`，`with_trajectory` 时逐阶段记录 stage/channel/候选数/耗时）。

---

## 拒绝的方案

- **QueryParser 默认用对话 LLM 改写 query**：被拒。缺省 `llm=echo`（不改写）——对话模型常把 query「答」成长文，造成语义漂移并引入显著延迟；改写能力需调用方显式注入并自担风险（部署配置即把 query_parser 的 llm 内联为 `echo`）。
- **Discloser 内做点读 / 有效性过滤 / 重排（Option A）**：被拒，改 Option B。点读、复核、重排上移到 `Retriever` 阶段，`Discloser` 退化为纯内容塑形（只按 `level` 截取）——职责单一、可独立替换，且重排只作用于已点读的存活候选。
- **只靠谓词下推 或 只靠后置过滤**：被拒，两者并存 = 纵深防御。能下推的（lifecycle/temporal）在索引级先排除以减少召回浪费；索引未写齐相应字段或异步滞后时，`passes` / `in_event_window` 后置兜底正确性。
- **`time_parse` 默认注入 LLM**：被拒。规则版覆盖常见中文相对时间且可控；默认不接 LLM，避免规则确定性被弱模型噪声污染，复杂表述留可选钩子按需注入。
- **召回返回 chunk 粒度**：被拒，统一归并到 unit 粒度（MaxP）。`UnitReader` 需按 unit 点读真源、`Fuser` 需跨通道合并同一 unit，chunk 复合 id 无法承担这两件事。
- **全量重排**：被拒。按 `rerank_top_m`（50）截断重排预算，并令各召回路超采样到该预算，平衡召回率与重排成本。

---

## 验证

- `pytest tests/unit/retrieval tests/integration/retrieval` 全绿（exit 0）。
- 单测覆盖：`query_parser` / `time_parse` / `recallers` / `fuser` / `discloser` / `predicate_builder` / `unit_reader`；集成覆盖：`pipeline_retriever` 全链路与 `index_contract`。
- 内存实现（memory store + hashing embedder + whitespace 分词 + echo LLM）无外部服务即可全程运行，是 CI 常驻覆盖面；真实后端（milvus/es/redis）按 integration 约定未连通时自动 skip。

---

## 已知遗留

- **LLM 改写 / 时间解析默认关闭**：缺省走 `echo` + 规则版，真正的 query 改写与复杂时间解析需显式注入 LLM 并承担延迟/漂移。
- **InMemory 召回为近似计分**：内存向量走暴力余弦、内存全文走词重叠比值近似 BM25，仅供离线/测试；生产召回走 milvus / es。
- **图召回依赖已建图**：`graph` 通道依赖构建层 ASSOCIATE 已建关联图，图为空时该路无产出。
- **FusionStore 未接入 pipeline**：`PipelineRetriever` 走分离的多路 `recaller` + `Fuser`；向量·倒排·正排合一的 `fusion_store` 形态（见存储层规约）是另一条尚未编排进检索链路的路径。
- **`structured_discloser` 字段约定待打磨**：结构化输出面向特定 Agent 消费约定，通用性与稳定性仍需迭代。
