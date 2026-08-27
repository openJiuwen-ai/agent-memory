# S04 — 检索层（Retrieval Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/retrieval/ |
| 最近一次修订日期 | 2026-08-27 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md、docs/features/construction/F04-cc-memory-compat.md、docs/features/construction/F05-construction-spec-multimodal-design.md、docs/features/retrieval/F02-retrieval-threshold-topk-design.md、docs/features/retrieval/F03-metadata-filtering.md、docs/features/retrieval/F04-score-max-fusion.md、docs/features/retrieval/F05-storage-retrieval-pipelines.md、docs/features/common/F01-memory-layer.md、docs/features/common/F08-memory-tree.md |

## Metadata 检索契约

FilterExpr 以 `user_metadata.<key>` 表示用户字段，以 `system_metadata.<key>` 表示
内部系统谓词，两者不 fallback。`RetrievedItem` 返回 `user_metadata`，普通搜索结果
不暴露 `system_metadata`。

## 范围 / 边界

**管什么**：
- 混合检索的完整链路编排（查询理解 → 并行多路召回 → 融合 → 精排 → 相关性阈值 → 渐进式披露 → 返回 + 检索轨迹）
- 查询理解：去噪/改写/分词/实体/向量化/时间解析
- 多路召回：按配置启用的通道并行检索（向量/关键词/图/文档/时序）
- 融合：多路候选合并去重、归一化打分、取最大值/RRF/加权排序
- 精排（可选）：调用 Reranker 做 cross-encoder 精排
- 相关性阈值：绝对/相对阈值裁剪低相关候选（结果数可 < top_k），min_results 兜底回填
- 渐进式披露：L0 摘要/L1 片段/L2 全文 按需加载
- 检索轨迹：可观测的非黑盒调试信息

**不管什么**：
- 不做鉴权（由 `jiuwen_memory/api` 层负责）
- 不做记忆写入/演进/落盘
- 不直接操作存储写入（只做存储读取/检索）
- 不实现 Embedder/Tokenizer/Reranker 等共享插件（消费 `jiuwen_memory/common` 注入的实例）

## 不变量

1. **scope 是独立轴**：`scope: Scope` 作为 `Retriever.retrieve` / `Recaller.recall` 的显式第一入参贯穿全链路，不随 `RetrievalQuery` 携带、也不混进 `filters`。
2. **query 是「找什么」，scope 是「在谁的范围内找」**：两条轴分开传。
3. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
4. **通道到物理 Store 非 1:1**：一路可对应一个 Store，也可多路合到一个 Store（如 FusionStore），TEMPORAL 通常是叠加在其他通道上的时间过滤。
5. **读写同一套共享插件**：QueryParser 必须与构建侧使用同一套 Tokenizer/Embedder/FeatureExtractor，保证同词表/同向量空间。
6. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `RetrievalOperator`。
7. **scalar_filters 与软召回信号分离**：ParsedQuery 中 `scalar_filters`（硬前置过滤）与 `tokens/keywords/entities/vector`（软召回信号）不能互相折叠。
8. **双时间轴独立**：`as_of`（valid-time 回溯点）与 `time_from/time_to`（event-time 范围）是两条独立时间轴。
9. **召回分数高分优先**：chunk→unit MaxP、分层归并与融合排序统一按「分越大越相关」处理；向量 Recaller 不接受 L2 等 lower-is-better 度量。
10. **生产过滤先于 top-k**：Milvus / Elasticsearch / pgvector 必须在
    `limit/top_k` 前完整下推 `FilterExpr`；UnitReader 的真源复核只做纵深防御，
    不能补回已被截断的候选。
11. **系统谓词不可被用户逻辑稀释**：lifecycle / valid-time / event-time 谓词与用户
    `filters` 以外层 `AND` 合并，用户表达式内部的 `OR` / `NOT` 不能绕过系统约束。
    系统侧的事件窗以 `OR(AND(GTE from, LT to), EQ T_EVENT_UNKNOWN)` 子树表达
    「窗内命中 OR 未知放行」，整棵 OR 子树作为外层 AND 的一个 child 不摊平——
    安全谓词不被稀释，同时 `t_event=None` 的派生不被窗下推清空（见过滤表达式段）。
12. **rank 只包含 Fuser**：Fuser 在物化候选上做分层归并和跨通道融合；Reranker 保持后续
    独立阶段，不下沉到 Storage 的 retrieve 入口。
13. **部分失败显式返回**：部分召回入口失败时继续处理成功候选并返回 `ChannelError`；全部选中
    入口失败抛 `StorageRetrievalError`。显式空 channels 是无效输入。
14. **结构轴正交**：`ContentLayers`/`DisclosureLevel` 是 unit 内披露，`HierarchyRef` 是跨 unit 结构；CLM/ELM、`MemoryUnit.temporal` 与 `RecallChannel.TEMPORAL` 均不替代 `HierarchyKind.TIME`。
15. **单 kind 层级请求**：一次层级请求只处理一个 `HierarchyKind.TIME|TOPIC|DIRECTORY|CLUSTER|CUSTOM`，不隐式跨 kind。
16. **层级默认保守**：`expand_depth=0`、`rollup=false`；只返回直接召回命中的节点，不遍历子节点，也不传播后代分数；父优先由显式 `hierarchy_role` 父侧角色过滤实现。
17. **展开顺序与隔离**：Expander 只沿直接 `child_ids` 向下，且必须保持父节点声明的稳定顺序；跨 org/space 引用不可见；同租户内跨 session/user 的子节点按 `child_scopes`（或缺省父 Scope）解析。
18. **展开共用既有 token 预算**：`expand_depth>0` 时选子与主披露级分配消耗同一 `RetrievalQuery.max_tokens`（来自 `context.extensions["max_tokens"]`），不另设独立树预算参数；Discloser 仍只负责单个 unit 的内容塑形。`span_start/span_end` 是结构覆盖区间，与 `as_of` 的 valid-time 回溯及 `time_from/time_to` 的 event-time 范围独立。

## 接口契约

### RetrievalOperator（基类，`base.py`）

```python
class RetrievalOperatorType(str, Enum):
    QUERY_PARSER / RECALLER / FUSER / DISCLOSER / RETRIEVER

class RetrievalOperator(ABC):
    def operator_type(self) -> RetrievalOperatorType  # 自描述
    def health(self) -> None                          # 存活探测
```

### Retriever（`retriever.py`）

检索层入口，编排完整链路。

| 方法 | 签名 | 语义 |
|---|---|---|
| `retrieve` | `(scope: Scope, query: RetrievalQuery) -> RetrievalResult` | 在 scope 内执行完整检索链路；层级字段为空时执行既有链路 |

目标父优先链路固定为：

```text
QueryParser
→ hierarchy kind/role/span 硬过滤
→ 既有 L0/L1/L2 内容层多路召回
→ fusion → rerank → threshold → top_k
→ 可选 Expand（expand_depth > 0）
→ tree score / convergence / tree token budget
→ 对每个保留 unit 调用 Discloser
→ RetrievalResult
```

`hierarchy_kind`、`hierarchy_role` 与结构 span 先于内容层召回生效；显式层级召回只接受
`HierarchyStatus.ACTIVE` 的节点。生命周期过滤与普通 recall 相同：
FORGOTTEN/SUPERSEDED 不可见，ARCHIVED 仅在 `include_archived=true` 时可见。
指定父侧 `hierarchy_role` 时，召回集合只包含该父角色；省略 role 时，同 kind 下所有
可见活动角色均可参与。过滤后的候选仍走既有融合、重排和阈值链路，因此层级父节点
不是一条绕过相关性判断的特殊结果通道。默认不展开。

### QueryParser / Recaller / Fuser

| 接口 | 签名 | 语义 |
|---|---|---|
| `QueryParser.parse` | `(query: RetrievalQuery) -> ParsedQuery` | 产生规范化文本、软召回信号、硬过滤条件和时间条件；完整保留层级查询字段 |
| `Recaller.recall` | `(scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]` | 在 scope 和硬过滤约束内执行单路召回 |
| `Fuser.fuse` | `(query: ParsedQuery, candidates: list[list[ScoredUnit]]) -> list[ScoredUnit]` | 按 unit_id 融合多路、多内容层候选并稳定排序 |

`RecallChannel.TEMPORAL` 仅应用 event-time/valid-time 条件，不创建、过滤或展开 `HierarchyKind.TIME` 树。TIME 层级过滤必须来自明确的 hierarchy 字段。

### Expander（目标契约，尚未实现）

```python
class Expander(RetrievalOperator):
    def expand(self, scope: Scope, request: ExpandRequest) -> ExpandResult: ...
```

Expander 先校验 root，再按深度从浅到深遍历；同一父的子顺序与 `child_ids` 一致，同层父分组沿上一层结果顺序。返回项使用相同顺序，不包含 root，只包含实际选中的后代。深度 1 表示直接子节点，深度 N 最多遍历 N 条父子边。

边界规则：

- root 不存在或不属于传入 `scope`：抛 `NotFoundError`，不得泄漏其他 scope 是否存在同 id。
- root 的 kind 与请求 kind 不同或 root 为空层级：抛 `ValidationError`。
- 子 id 在其驻留 Scope（`child_scopes[i]` 或父 unit 完整 Scope）缺失：记录 `ExpandIssue(code="missing_child")`，跳过该分支并置 `complete=false`。
- 子节点 kind 不同：记录 `kind_mismatch` 并跳过；不得转入另一 kind。
- 检测到自环、祖先环或重复到达：记录 `cycle`，首次出现之后不再访问该节点；结果中每个 id 至多一次。
- 子引用解析到其他 scope：按 `missing_child` 处理，不返回或描述外部对象。
- `HierarchyStatus` 非 ACTIVE：记录 `status_excluded` 并跳过该分支。FORGOTTEN/SUPERSEDED 同样不可展开；ARCHIVED 仅在 recall 的 `include_archived=true` 时可见。生命周期排除记录 `lifecycle_excluded`。
- 达到深度不是截断；`max_tokens` 耗尽、top-M 或节点上限导致未遍历完才是截断。

**retrieve 路径**：
```
QueryParser.parse(query) → ParsedQuery
→ 若 ParsedQuery.raw 为空则短路返回空结果
→ 按 Storage.preferred_retrieval_pipeline 选择 recall→get、recall_and_get 或 retrieve
→ Fuser 前物化候选并完成 lifecycle/valid-time/event-time/filters 真源复核
→ Fuser.fuse(parsed_query, candidates) → list[ScoredMemoryUnit]
→ 截断精排预算
→ 可选 Reranker 精排 → 相关性阈值过滤（结果数可 < top_k）→ 截断 top_k
→ [目标] 若 expand_depth>0：Expander 沿命中父节点展开（共用 max_tokens）
→ Discloser.disclose(parsed_query, candidates, units, level, max_tokens) → list[RetrievedItem]
→ 组装 RetrievalResult（items + trajectory + errors）
```

Expander 是 Retriever 内部算子，仅由 `search(..., expand_depth>0)` 触发；**不另设公开 `MemoryAPI.expand`**。
`ExpandRequest.include_archived` 与 `query` 由 recall 内部装配：前者继承检索查询的生命周期
可见性，后者为 MaxP 提供已规范化的 query。

### 分数传播、收敛与展开预算（目标契约，尚未实现）

父层召回的默认分数保持不变。`rollup=true` 时，检索层增加一条同 query、kind、span
和 lifecycle 可见性约束下的后代节点召回，不套用目标父角色过滤；命中后沿
`parent_id` 上卷：
`hierarchy_role` 非空时取满足该 role 的最近祖先，role 为空时只取直接父节点，再与
父节点自身召回分融合。该路径不改变输出展开深度，也不把后代自动加入结果。
默认传播算法是 MaxP：

```text
parent_score = max(parent_recall_score, selected_descendant_scores)
```

后代相关性分数使用与父召回相同的规范化 query 评分口径。传播只在请求 kind 内进行，不改变子项自身分数。

每个父节点最多保留策略 `hierarchy.expand_top_m` 指定的高分直接子节点；同分按 `child_ids` 顺序。某层最高剩余分不超过检索阈值时停止向下，形成确定性收敛。top-M 为空表示不额外裁剪，但仍受深度与 `max_tokens` 约束。

展开选子与父命中共用同一 `max_tokens` 池（不另设 `expand_budget_tokens`）。分配顺序为父命中顺序、深度从浅到深、同父 `child_ids` 顺序；预算估算决定某节点是否入选以及其主 `level`。不足时停止后续选择，`truncated=true`、`complete=false`，并记录 `budget_exhausted`。选定节点与披露级别后，逐 unit 调用 Discloser；Expander 不把子 id 塞入父 `RetrievedItem`。

`RetrievedItem` 始终返回 `abstract/overview/content` 全字段，因此这些字段的完整序列化
大小可能超过上述逻辑预算。当前契约不提供严格 wire-size/token-size 投影或上限保证。

多模态 profile 使用 `MultimodalRetriever` 包装基础 Retriever，并行执行原生文本、CLM
和 ELM 三个过滤分支后按 RRF 融合。该包装器不扫描 KV 判断多模态记忆是否存在，也不
依赖具体 Store；没有视频记忆时 CLM/ELM 分支返回空，融合结果由原生分支提供。

### QueryParser（`query_parser.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `parse` | `(query: RetrievalQuery) -> ParsedQuery` | 将检索请求解析为结构化查询表示 |

**产出**：raw/rewritten/intent/tokens/keywords/entities/vector/scalar_filters/as_of/time_from/time_to/channels/extensions。

`raw` 表示进入检索链路的规范化 query 文本，不要求逐字等于调用方传入的
`RetrievalQuery.text`。默认 `simple` 实现会先剥除上游包装噪声（如 UTC 时间戳、
`Sender (untrusted metadata)` 元数据行），再基于清洗后的文本产生分词、向量和时间窗。

### 过滤表达式

`RetrievalQuery.filters` 的内核类型是 `FilterExpr | None`：

- `FilterClause(field, op, value)` 表示叶子谓词；
- `FilterGroup(logic, children)` 表示可嵌套的 `AND` / `OR` / `NOT`；
- 旧 `list[FilterClause]` 在查询对象边界规范化为 `AND`；
- dict DSL 仅作为 API / SDK 兼容输入，在进入检索内核前转换为 `FilterExpr`；
- scope 字段不得进入 filters，隔离仍由 `scope: Scope` 专用入参保证。

metadata 比较保留 JSON 原生类型。查询侧不做 string / number / boolean 隐式互转；
范围算子只接受有限 `int` / `float`，同一业务 key 的类型稳定性由调用方负责。
字段形态同样属于比较语义：`EQ` / `IN` 的正向匹配只命中标量，`CONTAINS` 只命中
数组成员；`NE` / `NOT_IN` 分别按对应正向谓词取反。标量 `CONTAINS` 不退化为等值或
字符串子串，数组 `EQ` / `IN` 也不退化为成员匹配。

历史 `as_of` 查询追加 `lifecycle != forgotten`、`t_valid <= as_of`、
`t_invalid > as_of`。开放有效期在索引中投影为 `T_INVALID_OPEN`，真源仍保持
`t_invalid=None`；UnitReader 按真源 `[t_valid, t_invalid)` 区间复核。

事件时间窗 `[time_from, time_to)` 下推为 `OR(AND(GTE from, LT to), EQ 0)` 子树：
`AND` 子组放行窗内已知事件时间 unit，`EQ 0` 分支放行 `t_event=None` 的派生
（F07 净化后此类派生常见）。真源 `t_event=None` 在索引投影与 `memory_filter`
后置复核两侧都投影为哨兵 `T_EVENT_UNKNOWN=0`，使下推与复核语义不分叉。
`in_event_window` 后置仍读真源 datetime、对 None / naive 放行，与下推的
「窗内 OR 未知放行」意图对齐。半开边界与原扁平 GTE+LT 一致，不引入 LTE。

属性问（多大/几岁/爱好/是谁/住址/名字/生日/年龄…）即便含时间词也清空
`time_from/to` 不下推——属性问不是事件时间检索，误下推会放大 `t_event=None`
派生的误伤。`time_parse` 入口对此类 query 直接返回 `(None, None)`。

### Recaller（`recaller.py`）

单路召回算子。一个 Recaller 对应一条召回通道。

| 方法 | 签名 | 语义 |
|------|------|------|
| `channel` | `() -> RecallChannel` | 返回本召回路对应的通道 |
| `recall` | `(scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]` | 在 scope 范围内本通道内召回 top-k 候选 |

### Fuser（`fuser.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `fuse` | `(query: ParsedQuery, candidates: list[list[ScoredMemoryUnit]]) -> list[ScoredMemoryUnit]` | 融合已物化的分入口候选，保持 MemoryUnit 与 evidence |

### Discloser（`discloser.py`）

| 方法 | 签名 | 语义 |
|---|---|---|
| `disclose` | `(query, candidates, units, level, max_tokens=None) -> list[RetrievedItem]` | 为已选中的单个 unit 候选填充 L0/L1/L2 内容和实际主披露级 |

`RetrievedItem` 始终一次性具有 `abstract`、`overview`、`content` 三个字段；`level` 表示本次主披露级。已有行为必须准确区分：

- `StructuredDiscloser` 的 `ADAPTIVE` 会先给所有候选 L0；无 `max_tokens` 时尝试把首项提升到 L1；有预算时按预算尝试首项 L1、满足置信差时首项 L2，再依次提升其余项到 L1。
- 默认 `TruncatingDiscloser` 不实现自适应升级；收到 `ADAPTIVE` 时确定性降为 L0。其 `max_tokens` 不改变该行为。


**参数说明**：
- `query: ParsedQuery` — 提供改写后查询与关键词（L1 据此挑最相关片段）
- `candidates: list[ScoredUnit]` — 最终顺序的候选列表（已融合/重排）
- `units: dict[str, MemoryUnit]` — unit_id → MemoryUnit 的内容查找表
- `level: DisclosureLevel` — L0/L1/L2/ADAPTIVE
- `max_tokens: int | None` — 自适应披露预算

以上是单 unit 披露行为，不承担选子、遍历或树预算。

## 数据结构

### RetrievalQuery

既有字段保持兼容，目标新增字段标为“目标”：

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `text` | str | `""` | 自然语言查询 |
| `filters` | FilterExpr \| None | `None` | scope 之外的硬过滤；支持 AND / OR / NOT 树 |
| `as_of` | datetime \| None | `None` | valid-time 回溯点 |
| `top_k` | int | `10` | 父层结果上限 |
| `disclosure` | DisclosureLevel | `L0` | 父结果及后代的请求披露级 |
| `max_tokens` | int \| None | `None` | 既有单 unit 自适应披露预算 |
| `with_trajectory` | bool | `False` | 是否返回轨迹 |
| `channels` | list[RecallChannel] \| None | `None` | 覆盖召回通道 |
| `rerank` | bool \| None | `None` | 覆盖重排开关 |
| `include_archived` | bool | `False` | 是否纳入归档 unit |
| `extensions` | dict[str, str] | `{}` | 调用级透传配置 |
| `hierarchy_kind`（目标） | HierarchyKind \| None | `None` | 单一结构 kind |
| `hierarchy_role`（目标） | HierarchyRole \| None | `None` | 父层角色过滤 |
| `span_start`（目标） | datetime \| None | `None` | 结构区间起点 |
| `span_end`（目标） | datetime \| None | `None` | 结构区间终点 |
| `expand_depth`（目标） | int | `0` | 后代最大边深度；0 不展开 |
| `rollup`（目标） | bool | `False` | 是否启用后代分数向父传播 |

### ParsedQuery

| 字段 | 类型 | 语义 |
|------|------|------|
| `raw` | str | 进入检索链路的规范化 query；默认实现会先做保守去噪 |
| `rewritten` | str | LLM 改写后的 query |
| `intent` | str | 意图标签 |
| `tokens` | list[str] | 分词结果 |
| `keywords` | list[str] | 抽取的关键词 |
| `entities` | list[Entity] | 实体（FeatureExtractor NER 抽取，graph 通道召回读本字段做实体扩展；实体反向索引召回**不读本字段**——它读 fulltext L2 文档 `metadata['entities']` 明文，见 [F06](../features/retrieval/F06-entity-recall-channel.md)） |
| `vector` | list[float] | query 向量 |
| `scalar_filters` | FilterExpr \| None | 已规范化的硬前置过滤谓词 |
| `recheck_filters` | FilterExpr \| None | 用户原始硬过滤谓词，供物化后的真源复核 |
| `as_of` | datetime \| None | valid-time 回溯 |
| `time_from` | datetime \| None | event-time 下界 |
| `time_to` | datetime \| None | event-time 上界 |
| `channels` | list[RecallChannel] | 建议启用的通道 |
| `include_archived` | bool | 当前态真源复核是否允许 archived |
| `extensions` | dict[str, str] | 透传配置 |

1. `top_k > 0`，`expand_depth >= 0`；非空 `max_tokens` 必须大于 0。
2. `hierarchy_role`、任一 span、`expand_depth > 0` 或 `rollup=true` 都要求显式 `hierarchy_kind`。
3. span 必须成对出现且 `span_start <= span_end`。
4. 区间采用闭区间相交：节点满足 `node.span_start <= query.span_end AND node.span_end >= query.span_start`；端点相等算相交。没有 span 的节点不匹配有 span 的查询。
5. `hierarchy_kind=HierarchyKind.TIME` 的查询可以省略 query span，此时查询已有 TIME 结构的全部范围；但每个匹配节点自身必须具有有效 span。阻塞 ensure 仍要求 query span 有界。这不改变对 `MemoryUnit.temporal.t_event` 的普通时间过滤。
6. hierarchy 功能关闭时，任何显式层级字段、非零展开深度或 `rollup=true` 都抛 `PolicyError`；没有层级请求的召回不受影响。

| 类型 | 关键字段 |
|------|----------|
| `ScoredUnit` | unit_id / score / channel / evidence: list[ChannelEvidence] |
| `ChannelEvidence` | channel / rank / score / weight / contribution |
| `RetrievedItem` | unit_id / score / content / level: DisclosureLevel |
| `TrajectoryStep` | stage / channel / candidate_count / cost_ms / detail |
| `ScoredMemoryUnit` | unit: MemoryUnit / score / channel / evidence |
| `ChannelError` | channel / source / error_type / message |
| `RetrievalResult` | items / trajectory / errors: list[ChannelError] |

### ParsedQuery

`ParsedQuery` 保留既有 `raw/rewritten/intent/tokens/keywords/entities/vector/scalar_filters/as_of/time_from/time_to/channels/extensions`，目标增加与 `RetrievalQuery` 同名的 hierarchy 字段。Parser 不把 hierarchy span 改写成 event-time，也不从 TEMPORAL 通道推导 TIME kind。

### ExpandRequest / ExpandIssue / ExpandResult（目标契约，尚未实现）

```python
@dataclass
class ExpandRequest:
    root_id: str
    kind: HierarchyKind
    depth: int = 1
    disclosure: DisclosureLevel = DisclosureLevel.L1
    max_tokens: int | None = None  # 与父命中共用同一池；由 recall 传入剩余/总预算
    with_trajectory: bool = True
    include_archived: bool = False
    query: ParsedQuery | None = None

@dataclass
class ExpandIssue:
    unit_id: str
    code: str
    message: str

@dataclass
class ExpandResult:
    root_id: str
    kind: HierarchyKind
    items: list[RetrievedItem]
    actual_depth: int
    truncated: bool
    complete: bool
    issues: list[ExpandIssue]
    trajectory: list[TrajectoryStep]
```

以上类型仅供 Retriever 内部装配，不暴露为公开 `MemoryAPI` 方法。`depth >= 1`，非空 `max_tokens > 0`。`actual_depth` 是返回项中离 root 的最大边数；空结果为 0。`complete=true` 当且仅当请求深度内所有可见、同 kind、有效的后代都完成处理，且没有 issue 或 `max_tokens`/top-M/节点上限截断。issues 按首次遇到顺序稳定排列。

### 既有结果结构

| 类型 | 精确字段 |
|---|---|
| `ScoredUnit` | `unit_id` / `score` / `channel` / `evidence` |
| `ChannelEvidence` | `channel` / `rank` / `score` / `weight` / `contribution` |
| `RetrievedItem` | `unit_id` / `score` / `abstract` / `overview` / `content` / `level` |
| `TrajectoryStep` | `stage` / `channel` / `candidate_count` / `cost_ms` / `detail` |
| `RetrievalResult` | `items` / `trajectory` |

不得向既有 `RetrievedItem` 增加 `child_ids` 或把 `content` 改作树容器。

### 枚举

| 枚举 | 值 |
|------|------|
| `DisclosureLevel` | L0 / L1 / L2 / ADAPTIVE |
| `RecallChannel` | DOCUMENT / KEYWORD / VECTOR / GRAPH / TEMPORAL |

### 轨迹

普通链路沿用 `parse/recall/fuse/rerank/threshold/disclose`。层级召回额外使用：

- `parent_recall`：`detail` 至少记录 `kind`、`role`、span、父候选数。
- `expand`：每个 root 一步，`detail` 至少记录 `root_id`、`kind`、`requested_depth`、`actual_depth`、`item_count`、`truncated` 和截断原因。

`with_trajectory=false` 时 `RetrievalResult.trajectory=[]`；公开 `expand` 的 `with_trajectory` 独立控制 `ExpandResult.trajectory`。

## 错误语义

| 异常 | 场景 |
|---|---|
| `ValidationError` | 深度、预算、span 或 kind/role 组合非法；root kind 不匹配 |
| `NotFoundError` | 展开 root 不存在或不在请求 scope |
| `PolicyError` | 显式层级召回或展开在 hierarchy 关闭时发起 |
| `BackendError` | 召回或点读后端失败，且不能按 issue 规则局部处理 |

## 实现注册机制

```
jiuwen_memory/retrieval/<算子>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`QueryParserProducer` / `RecallerProducer` / `FuserProducer` / `DiscloserProducer` / `RetrieverProducer`。
注册由 `retrieval.bootstrap.register_operators` 统一触发。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | MemoryAPI.search → Engine → 本层 Retriever |
| S03-control | Engine.recall 委托本层 Retriever |
| S05-construction | 本层消费构建层产出的索引（向量/全文/图） |
| S06-storage | Retriever 经 StorageProducer 获取统一 Storage；现有 Recaller 作为 CompositeStorage 的兼容检索适配器 |
| S07-common | 复用 Tokenizer/Embedder/FeatureExtractor/LLM/Reranker |
| S08-config | 能力开关与 rerank/embedder 晚绑定经 ConfigSource |
| architecture.md §8 | 检索链路设计 |
