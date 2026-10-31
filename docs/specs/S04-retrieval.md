# S04 — 检索层（Retrieval Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/retrieval/ |
| 最近一次修订日期 | 2026-10-31 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md、docs/features/construction/F04-cc-memory-compat.md、docs/features/construction/F05-construction-spec-multimodal-design.md、docs/features/retrieval/F02-retrieval-threshold-topk-design.md、docs/features/retrieval/F03-metadata-filtering.md、docs/features/retrieval/F04-score-max-fusion.md、docs/features/retrieval/F05-storage-retrieval-pipelines.md、docs/features/retrieval/F07-schema-temporal-entity.md、docs/features/retrieval/F08-query-rewrite.md、docs/features/common/F01-memory-layer.md、docs/features/common/F08-memory-tree.md、docs/features/storage/F06-composite-recaller-assembly.md |

## Metadata 检索契约

FilterExpr 以 `user_metadata.<key>` 表示用户字段，以 `system_metadata.<key>` 表示
内部系统谓词，两者不 fallback。`RetrievedItem` 同时返回两个命名空间；接入层可按
信任边界裁剪 `system_metadata`，但 Core API 的检索结果保留完整字段。

## 当前交付边界

显式层级检索、祖先准入/MaxP、向下展开与共享预算已实现；ensure 和自动修复
未接入。接口默认预设、父优先和已召回后代补位已经接入。结构索引字段为 tree_parent_id/tree_span_start/tree_span_end，模型字段保持原名。

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
- 可选 Schema TemporalEntity 视图：实体/Property 双路选择、实体内裁剪与双时间轴过滤

**不管什么**：
- 不做鉴权（由 `jiuwen_memory/api` 层负责）
- 不做记忆写入/演进/落盘
- 不直接操作存储写入（只做存储读取/检索）
- 不实现 Embedder/Tokenizer/Reranker 等共享插件（消费 `jiuwen_memory/common` 注入的实例）

## 不变量

1. **scope 是独立轴**：`scope: Scope` 作为 `Retriever.retrieve` / `Recaller.recall` 的显式第一入参贯穿全链路，不随 `RetrievalQuery` 携带、也不混进 `filters`。
2. **query 是「找什么」，scope 是「在谁的范围内找」**：两条轴分开传。
3. **接口与实现严格分离**：接口、共享状态及契约辅助逻辑不 import `*_impl/`；具体算法留在实现模块。
4. **通道到物理 Store 非 1:1**：一路可对应一个 Store，也可多路合到一个 Store（如 FusionStore），TEMPORAL 通常是叠加在其他通道上的时间过滤。
5. **读写同一套共享插件**：QueryParser 必须与构建侧使用同一套 Tokenizer/Embedder/FeatureExtractor，保证同词表/同向量空间。
6. **所有算子必须实现 `operator_type()` 和 `health()`**：继承自 `RetrievalOperator`。
7. **scalar_filters 与软召回信号分离**：ParsedQuery 中 `scalar_filters`（硬前置过滤）与 `tokens/keywords/entities/vector`（软召回信号）不能互相折叠。
8. **双时间轴独立**：`as_of`（valid-time 回溯点）与 `time_from/time_to`（event-time 范围）是两条独立时间轴。
9. **召回分数高分优先**：chunk→unit MaxP、分层归并与融合排序统一按「分越大越相关」处理；向量 Recaller（存储层数据面）不接受 L2 等 lower-is-better 度量。
10. **生产过滤先于 top-k**：Milvus / Elasticsearch / pgvector 必须在
    `limit/top_k` 前完整下推 `FilterExpr`；UnitReader 的真源复核只做纵深防御，
    不能补回已被截断的候选。
11. **系统谓词不可被用户逻辑稀释**：lifecycle / valid-time / event-time 谓词与用户
    `filters` 以外层 `AND` 合并，用户表达式内部的 `OR` / `NOT` 不能绕过系统约束。
    系统侧的事件窗以 `OR(AND(GTE from, LT to), EQ T_EVENT_UNKNOWN)` 子树表达
    「窗内命中 OR 未知放行」，整棵 OR 子树作为外层 AND 的一个 child 不摊平——
    安全谓词不被稀释，同时 `t_event=None` 的派生不被窗下推清空（见过滤表达式段）。
12. **rank 只包含 Fuser**：Fuser 在物化候选上做分层归并和跨通道融合；Reranker 保持后续
    独立阶段，不下沉到 DomainStore 的 retrieve 入口。
13. **部分失败显式返回**：部分召回入口失败时继续处理成功候选并返回 `ChannelError`；全部选中
    入口失败抛 `StorageRetrievalError`。显式空 channels 是无效输入。
14. **结构轴正交**：`ContentLayers`/`DisclosureLevel` 是 unit 内披露，`HierarchyRef` 是跨 unit 结构；CLM/ELM、`MemoryUnit.temporal` 与 `RecallChannel.TEMPORAL` 均不替代 `HierarchyKind.TIME`。
15. **单 kind 层级请求**：一次层级请求只处理一个 `HierarchyKind.TIME|TOPIC|DIRECTORY|CLUSTER|CUSTOM`，不隐式跨 kind。
16. **类型默认与接口预设分离**：内部 SearchOptions/RetrievalQuery 默认 expand_depth=0、rollup=False；
    公开 search 签名预设 TIME/time_span/rollup=True/expand_depth=1，树开关决定是否生效。
    显式传入 0/False 原样生效；关闭开关清除树参数，普通行为保持。
17. **展开顺序与隔离**：Expander 只沿直接 `child_ids` 向下，且必须保持父节点声明的稳定顺序；跨 org/space 引用不可见；同租户内跨 session/user 的子节点按 `child_scopes`（或缺省父 Scope）解析。
18. **展开共用既有 token 预算**：`expand_depth>0` 时选子与主披露级分配消耗同一 `RetrievalQuery.max_tokens`（来自 `context.extensions["max_tokens"]`），不另设独立树预算参数；Discloser 仍只负责单个 unit 的内容塑形。`span_start/span_end` 是结构覆盖区间，与 `as_of` 的 valid-time 回溯及 `time_from/time_to` 的 event-time 范围独立。
19. **可选精排失败可降级**：已配置的 Reranker 在 `rerank` 阶段抛出 `BackendError` 时，
    Retriever 必须继续使用未精排候选，按未校准阈值路径处理，并在轨迹中记录
    `skipped=reranker_unavailable` 与脱敏后的 `error`。其他异常不得被该降级路径吞掉。
20. **TemporalEntity 不是记忆真源**：Schema 时序检索在读取期组装临时实体视图，
    Property/Source 必须先完成点读与有效性复核。正式结果可把每个实体格式化为
    一个 `RetrievedItem`，其 id 是 Entity id；该项不是 synthetic MemoryUnit，
    不支持按 MemoryUnit id 点读。结构化结果仅由
    `SchemaTemporalRetrievalResult.schema_temporal` 返回；普通结果无此字段。
21. **Schema 时序两轴先后有序**：`knowledge_as_of` 仅过滤
    `[t_valid, t_invalid)` 知识版本，`event_at/event_from/event_to` 仅选择
    事件时间。年/月精度通过半开区间运算，不补造 `t_event`。

## 接口契约

### RetrievalOperator（基类，`base.py`）

```python
class RetrievalOperatorType(str, Enum):
    QUERY_PARSER / FUSER / DISCLOSER / RETRIEVER / EXPANDER

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

#### Schema TemporalEntity 选择链路

`globals.schema_temporal_enabled=true` 时，Retriever 在普通 Reranker、阈值、top-k
和 disclosure 完成后执行只读 Schema 时序选择：

```text
最终普通 MemoryUnit 结果提供 Entity seed
→ 独立 schema_entities Entity 路径与直达 Property 路径分别召回
→ 按 schema_entity_key 临时组装 TemporalEntity
→ 实体内 Property 二次选择
→ 直达 Property 晚融合 + 同属性前后相邻历史（default_property 除外）
→ knowledge-time / event-time 选择
→ 每 Entity 一个格式化 RetrievedItem
→ 最终按预算融合必要的 Source fallback
```

Entity 路径的名额不得剪掉已直接命中的 Property；直达属性按 `unit_id`
保留原分数，并可扩展同实体、同属性时间线的前后邻居（`default_property`
除外）。各实体独立裁剪，
不允许因一个实体到达全局上限而中止后续实体。Source fallback 只作为结构化
Property 缺失或不完整时的证据保全通道，不扫描并平铺整个 Scope。

该链路以 `SchemaTemporalRetrievalResult.schema_temporal` 返回结构化时间线，并将
`items` 替换为实体级视图与必要 Source。未启用硬门、未指定
`RetrievalQuery.schema_temporal`/`extensions["schema_temporal"]` 且未命中自动
条件时，必须执行既有链路，普通 `RetrievalResult` 不包含 `schema_temporal`。

### QueryParser / Fuser

| 接口 | 签名 | 语义 |
|---|---|---|
| `QueryParser.parse` | `(query: RetrievalQuery) -> ParsedQuery` | 产生规范化文本、软召回信号、硬过滤条件和时间条件；完整保留层级查询字段 |
| `Fuser.fuse` | `(query: ParsedQuery, candidates: list[list[ScoredUnit]]) -> list[ScoredUnit]` | 按 unit_id 融合多路、多内容层候选并稳定排序 |

单路召回（`Recaller`）不是本层算子：它是 `CompositeDomainStore` 的内部件，契约与实现
在 `storage/domain_store_impl/`，签名见 S06-storage。本层只经数据面的
`recall` / `recall_and_get` / `retrieve` 消费其结果。

`RecallChannel.TEMPORAL` 仅应用 event-time/valid-time 条件，不创建、过滤或展开 `HierarchyKind.TIME` 树。TIME 层级过滤必须来自明确的 hierarchy 字段。

`RecallChannel.SPACE`（F07 随跨空间检索新增）不是召回通道，是「某个空间整体没进结果」的标记位，只出现在 `RetrievalResult.errors` 的 `ChannelError.channel` 上。它不进候选、不参与融合，也不出现在 `ChannelEvidence.channel` 里，因此按通道配置融合权重或按通道打点的消费方不为它配权重。该取值的构造点是本层的 `cross_space.space_error`，由 API 层（判权剔除）与控制层（扇出失败）两侧共用。

`cross_space.py` 只提供取数上界、结果合并与失败编码三个纯函数；跨空间的召回扇出编排落控制层 `control/collective/cross_space_recall.py`，它 import 本模块，本模块不反向依赖控制层。

### Expander（`expander.py` / `expander_impl/default_expander.py`）

```python
class Expander(RetrievalOperator):
    def expand(self, scope: Scope, request: ExpandRequest) -> ExpandResult: ...
```

Expander 接收已物化的根，使用 Retriever 注入的同一个 DomainStore 点读后代。
先校验根的 Scope 和结构，再按深度从浅到深遍历；同父按 `child_ids` 顺序，同层父
分组沿上一层结果顺序。选中的后代交给回调，返回诊断统计而非树容器。
深度 1 表示直接子节点，深度 N 最多遍历 N 条父子边。

边界规则：

- 根由直接召回完成物化与可见性复核，Expander 不重新读根或声明事务快照；根不属于
  传入 `scope` 时抛 `NotFoundError`。
- root 的 kind 与请求 kind 不同或 root 为空层级：抛 `ValidationError`。
- 子 id 在其驻留 Scope（`child_scopes[i]` 或父 unit 完整 Scope）缺失：记录 `ExpandIssue(code="missing_child")`，跳过该分支并置 `complete=false`。
- 子节点 kind 不同：记录 `kind_mismatch` 并跳过；不得转入另一 kind。
- 自环或祖先环记录 `cycle`；普通重复到达去重而不报环。每个完整 Scope + id 至多
  返回一次，跨 session 同名节点可分别返回。
- 越过请求 Scope 的引用在点读前记录 `scope_excluded`，不暴露被排除对象的身份或正文。
- 子结构须有效且 ACTIVE；反向 parent_id/parent_scope 必须对应当前父，子 span 不得
  超出父 span。分别记录 `invalid_structure`、`status_excluded`、`parent_mismatch`
  或 `span_not_covered`。
- 子继承 kind、结构 span、event-time、valid-time、include_archived 及业务/权限过滤，
  只移除 typed 父角色。历史 as_of 可见的 SUPERSEDED 不被额外屏蔽；排除记录
  `visibility_excluded`。通用 filters 内的角色条件不移除。
- 读取失败记录 `read_error`；批次 NotFound 回退逐项，保留健康兄弟。问题码按首次出现
  顺序去重。达到请求深度不是截断；预算或节点上限导致未完成才置 `truncated=true`。

**retrieve 路径**：
```
QueryParser.parse(query) → ParsedQuery
→ 若 ParsedQuery.raw 为空则短路返回空结果
→ 按 DomainStore.preferred_retrieval_pipeline 选择 recall→get、recall_and_get 或 retrieve
→ Fuser 前物化候选并完成 lifecycle/valid-time/event-time/filters 真源复核
→ Fuser.fuse(parsed_query, candidates) → list[ScoredMemoryUnit]
→ 截断精排预算；补位路径保存此时的融合候选顺序
→ 可选 Reranker 以 ParsedQuery.rewritten 精排 → [rollup=True] 祖先准入及 MaxP
→ 相关性阈值过滤（结果数可 < top_k）→ 截断 top_k
→ Discloser 塑形；若 expand_depth>0，内部准备阶段暂不分配预算、不读后代
→ [expand_depth>0] 共享预算准入根；TIME 父角色上卷时复用命中后代，否则 Expander BFS
→ 统一截断父子返回总数到 top_k
→ 组装 RetrievalResult（items + trajectory + errors）
```

Expander 是 Retriever 内部结构遍历算子；展开请求采用已召回后代补位时不调用它。
其余 `expand_depth>0` 请求使用 BFS；**不另设公开 `MemoryAPI.expand`**。
`ExpandRequest.query` 由 recall 内部装配，保留原调用 filters 与解析后 filters 的 AND，
以及生命周期、时间可见性。它不依赖 Parser 理解 expand_depth，不为子另查索引。

### 上卷准入与 MaxP

`SearchOptions.rollup` / `RetrievalQuery.rollup` 默认 False；仅接受 bool，True 要求
显式 kind。父和后代不分两次独立归一化：移除 typed 输出 role 的等值限制，
TIME 指定角色时改为“目标角色及其下层”的 IN 过滤，同时下推索引并复核真源。
例如 TimeSpan 仅召回 TimeSpan/Snapshot，Scene 增加 Scene，Event 包含四层。
仍保留 kind、span、lifecycle、时间窗、原 filters 与 parser filters 的 AND；统一完成
既有融合、精排后，再执行祖先准入与 MaxP，随后才做阈值及 top_k。

- 指定 role：直接命中该角色者保留；其他候选沿 parent_id/parent_scope 上卷到最近
  该角色祖先，可准入从未命中索引的父。中间节点不自动加入结果。
- 省略 role：保留原直接命中，并额外准入其直接父；不把新准入的父再次递归上卷。
- 只开 rollup 不自动展开后代。若结果里有直接命中的叶，它来自普通召回；要保证只
  返回父角色，请显式给 role。expand_depth 控制后代相对最终根的最大父子边距离。

固定算法为 MaxP，不新增 score_propagation 策略键：

```text
parent_score = max(存在的父直接命中分, 各已命中后代的最终分)
```

分数取 max 而非累加；父缺席时直接使用后代分，不虚构父自身检索得分。精排开启时
使用精排分，否则使用融合分。祖先聚合按完整 Scope+id，保留父自身已有 evidence，
新准入父不复制子 evidence 冒充父的索引命中；传播统计在 rollup 轨迹中说明。

上卷只用同源 DomainStore 点读，先查授权 Scope，
再检查本体身份、kind、ACTIVE、生命周期、valid-time、event-time、结构闭区间、
业务/权限过滤、双向边和父 span 覆盖；任一中间节点不可见即停止，不越过它找更高祖先。
查询的 org/space 必须相等，非空 user/agent/session 不可放宽，session 限定请求不能
上卷到更宽的 home Scope。缓存以完整身份为键，不扫描 Scope、不写回或修树。

每条链最多 32 跳，每次 Pipeline 调用共尝试 1000 条父引用（含重复/坏引用），不是
全局跨空间共享限额。各空间先上卷并准备候选，全局选根后再共享预算补入后代；
其他结构展开仍在全局选根后执行 BFS。
缺失、越界、坏边和上限记录为 source=rollup 的 HIERARCHY 错误，
即使不开轨迹也返回；不回显被排除节点身份或后端异常。轨迹记录 algorithm、admitted、
boosted、visited、target_role、complete 和 issues。

**已有边界**：只处理 recall_k / 融合精排 budget_n 内实际得到的候选，不保证全树穷举。
当前 CompositeDomainStore 的裸 id 候选点读使用查询的精确物理 Scope，不枚举 session；
因此宽 Scope 索引命中不能据此保证跨 session 叶被物化。原始候选及 Fuser 的裸 id
归并未在本阶段改造；新增祖先的完整身份隔离不等于原始跨 Scope 同 id 召回已解决。

### 父优先与已召回后代补位

TIME 的 time_span/scene/event 查询同时满足 rollup=True、expand_depth>0 时：

1. 保存真源复核、融合及 budget_n 截断后的候选顺序；发生在可选 Reranker 之前。
2. 既有 Reranker、MaxP、阈值及 top_k 继续决定父节点及其顺序。上卷同时记录后代
   到目标父的归属与实际边距离，沿途校验 Scope、可见性、双向引用及时间覆盖。
3. 最终保留父顺序，再按已保存的融合顺序补入属于选中父且距离不超过 expand_depth
   的已召回后代。Scene/Event 可补多层后代；不要求中间节点本身也被召回或返回。
4. 父子总数达到 top_k 即停止补位。不额外召回、不遍历整棵子树、不再次向量化子正文；
   候选不足时允许少于 top_k，不回退到未命中叶。不按父分配子配额。

普通查询的可选 Reranker 保留；子候选补位顺序不随其精排结果重排。
RetrievedItem.score 仍为根召回分或继承分，不改成子候选融合分。
补位轨迹为 child_select，记录 candidate_count、parent_count、selected_children、ranking_cutoff。
内部上卷参数 child_depth 必须为非负整数且不能为 bool；0 表示不记录后代补位映射。
非法值在上卷读取前抛出 ValidationError。

rollup=False、未指定 TIME 父角色的展开请求仍使用结构 BFS，保持遍历顺序，
不再进行额外子正文 embedding 排序。expand_depth=0 仅返回选中的根。
跨空间扇出通过 defer_expansion=True 返回物化根和已召回后代；全局选根后，
按现有空间处理顺序依次取各空间内部保持融合原序的子候选，只补入属于已选父的后代。
不按异步完成时间排列，也不比较不同空间的融合分数。父子共享一个 top_k、披露预算
和节点上限，后代按完整 Scope+id 去重；空间内的权限与可见性复核保留。
准备结果以 recalled_children_ready 标明后代候选已准备完成，默认 false；
keep_recalled 完成后置为 true，候选为空也视为完成且不增加错误。
窗口有剩余名额时，仅校验提供已选父节点的结果；尚未准备则抛出
UnsupportedCapabilityError。父节点已占满 top_k 或提供方没有已选父节点时不要求该标志。

两条路径均在预算准入后按真源完整正文排除空白根/后代，不退还预算；空白根仍可
带回合法后代。不得用自定义 L0 披露器可能省略的返回 content 判断空白。

### 展开准入与预算

`top_k` 限制选中根数量及最终父子返回总数。展开子节点不改变先前的选根名额，
但必须与根共享最终返回名额。先按最终顺序为所有根分配预算，
再执行已召回后代补位或逐根 BFS，最后截取前 `top_k` 项。补位按融合顺序准入，
达到名额或预算上限即停止；BFS 按根及各根遍历顺序准入，每根预算不足时停止该根。
节点访问、披露预算及轨迹计数反映实际工作，不回退或回填；根自身预算不足时
停止后续根准入，结果可少于 top_k。
跨空间选根沿用既有合并/去重规则，后代阶段按完整 Scope + id 去重。

根与子共用 `max_tokens`，先用注入的 Discloser 塑形，再按实际主字段估算
`max(1, ceil(字符数 / 4))`。固定 L0/L1/L2 不降级；ADAPTIVE 在有限预算下逐节点选
能容纳的最丰富 L2→L1→L0，无预算上限时以 L1 为主。只改变展开请求的准入逻辑，
depth=0 保持既有 Discloser 行为。预算截断记录 `budget_exhausted`。

一次结构 BFS 收尾最多尝试 1000 个子引用（坏引用和重复引用也计数），单批最多 64 个，
跨根、跨空间共享上限；补位路径最多尝试 1000 个去重后的候选。超限记录 `node_limit`。
不实现独立子相关性阈值，不把子 id
塞入父 `RetrievedItem`。全程只读，不修复坏树。

`RetrievedItem` 始终返回 `abstract/overview/content` 全字段，因此这些字段的完整序列化
大小可能超过上述逻辑预算。当前契约不提供严格 wire-size/token-size 投影或上限保证。

多模态 profile 使用 `MultimodalRetriever` 包装基础 Retriever，并行执行原生文本、CLM
和 ELM 三个过滤分支后按 RRF 融合。该包装器不扫描 KV 判断多模态记忆是否存在，也不
依赖具体 Store；没有视频记忆时 CLM/ELM 分支返回空，融合结果由原生分支提供。
该包装器尚未适配层级过滤、上卷后的分支融合及延迟展开；只要请求显式设置
`hierarchy_kind` 就明确抛 `UnsupportedCapabilityError`，而不是让三个分支各自执行一部分
层级语义后返回看似完整的结果，
不能视为基础 PipelineRetriever 的三条存储路径不支持展开。

### QueryParser（`query_parser.py`）

| 方法 | 签名 | 语义 |
|------|------|------|
| `parse` | `(query: RetrievalQuery) -> ParsedQuery` | 将检索请求解析为结构化查询表示 |

**产出**：raw/rewritten/intent/tokens/keywords/entities/vector/scalar_filters/as_of/time_from/time_to/channels/extensions/rewrite_status/rewrite_fallback_reason。

`raw` 表示进入检索链路的规范化 query 文本，不要求逐字等于调用方传入的
`RetrievalQuery.text`。默认 `simple` 实现会先剥除上游包装噪声（如 UTC 时间戳、
`Sender (untrusted metadata)` 元数据行），再基于清洗后的文本产生分词、向量和时间窗。

默认 `simple` 实现的 LLM 改写关闭。显式开启且注入 LLM 时，parser 将 sanitized raw
放入 `<current_query>`，把规范化后的可选 `query_context` 放入 `<recent_context>`，
并要求 `{"rewritten": "..." | null}` 单对象输出。分区内容会转义 `&` 与 `<`；
上下文最多保留最近 5 项、8192 字符尾部。非空字符串驱动软召回信号与 Reranker 精排；
显式 null 表示当前消息明确无检索意图，产出空软信号并在 Retriever 进入 DomainStore
前短路。Reranker 使用 `ParsedQuery.rewritten`；关闭或 fallback 时该值即 sanitized raw。
缺字段、空串、非法 JSON、超长输出或模型异常回退 sanitized raw。
`rewritten` 的字符级上限是 `max(512, len(sanitized raw))`；QueryParser 不设置任务级
`max_tokens`，由 LLM 后端的 `llm_max_tokens` 统一控制。硬过滤、`as_of` 和
event-time 窗仍来自调用方输入与 raw 文本。`parse` 轨迹用
`detail["rewrite"]` 记录 `off/used/fallback/no_intent`，fallback 时用
`detail["fallback_reason"]` 记录原因，不记录 query、上下文或模型原始输出。

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

当前字段如下：

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `text` | str | `""` | 自然语言查询 |
| `filters` | FilterExpr \| None | `None` | scope 之外的硬过滤；支持 AND / OR / NOT 树 |
| `as_of` | datetime \| None | `None` | valid-time 回溯点 |
| `top_k` | int | `10` | 选根与最终父子结果总数上限 |
| `disclosure` | DisclosureLevel | `L0` | 父结果及后代的请求披露级 |
| `max_tokens` | int \| None | `None` | 展开时为根与后代共享的主字段预算 |
| `with_trajectory` | bool | `False` | 是否返回轨迹 |
| `channels` | list[RecallChannel] \| None | `None` | 覆盖召回通道 |
| `rerank` | bool \| None | `None` | 覆盖重排开关 |
| `include_archived` | bool | `False` | 是否纳入归档 unit |
| `schema_temporal` | SchemaTemporalQuery \| None | `None` | 强类型 Schema 时序查询；未设置时可按配置自动识别时间意图 |
| `extensions` | dict[str, Any] | `{}` | 调用级透传配置；`schema_temporal` 是内核保留键，本地调用可携带运行时对象 |
| `hierarchy_kind` | HierarchyKind \| None | `None` | 单一结构 kind |
| `hierarchy_role` | HierarchyRole \| None | `None` | 父层角色过滤 |
| `span_start` | datetime \| None | `None` | 结构区间起点 |
| `span_end` | datetime \| None | `None` | 结构区间终点 |
| `expand_depth` | int | `0` | 后代最大边深度；0 不展开 |
| `rollup` | bool | `False` | 是否启用后代分数向父传播 |
| `query_context` | tuple[str, ...] \| None | `None` | 可选最近上下文；由 API 从 `Context.extensions["query_context"]` 校验并转换，仅上下文感知改写 parser 消费 |

### ParsedQuery

| 字段 | 类型 | 语义 |
|------|------|------|
| `raw` | str | 进入检索链路的规范化 query；默认实现会先做保守去噪 |
| `rewritten` | str | LLM 改写后的 query；`no_intent` 时为空 |
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
| `extensions` | dict[str, Any] | 透传配置 |
| `rewrite_status` | str | 改写状态：`off` / `used` / `fallback` / `no_intent` |
| `rewrite_fallback_reason` | str | 改写回退原因；成功、关闭或 no-intent 时为空 |

1. `top_k > 0`，`expand_depth >= 0`；非空 `max_tokens` 必须大于 0。
2. `hierarchy_role`、任一 span、`expand_depth > 0` 或 `rollup=true` 都要求显式 `hierarchy_kind`。
3. span 必须成对出现且 `span_start <= span_end`。
4. 区间采用闭区间相交：节点满足 `node.span_start <= query.span_end AND node.span_end >= query.span_start`；端点相等算相交。没有 span 的节点不匹配有 span 的查询。
5. `hierarchy_kind=HierarchyKind.TIME` 的查询可以省略 query span，此时查询已有 TIME 结构的全部范围；但每个匹配节点自身必须具有有效 span。阻塞 ensure 仍要求 query span 有界。这不改变对 `MemoryUnit.temporal.t_event` 的普通时间过滤。
6. hierarchy 功能关闭时，API 将本次层级类型、角色、结构区间清为空，展开深度归零、上卷关闭；显式树参数同样忽略，再执行普通召回，普通参数与权限约束保持原语义。

| 类型 | 关键字段 |
|------|----------|
| `ScoredUnit` | unit_id / score / channel / evidence: list[ChannelEvidence] |
| `ChannelEvidence` | channel / rank / score / weight / contribution |
| `RetrievedItem` | unit_id / score / content / user_metadata / system_metadata / parent_id / level: DisclosureLevel |
| `TrajectoryStep` | stage / channel / candidate_count / cost_ms / detail |
| `ScoredMemoryUnit` | unit: MemoryUnit / score / channel / evidence |
| `ChannelError` | channel / source / error_type / message |
| `RetrievalResult` | items / trajectory / errors: list[ChannelError] |

### ParsedQuery

`ParsedQuery` 保留既有 `raw/rewritten/intent/tokens/keywords/entities/vector/scalar_filters/as_of/time_from/time_to/channels/extensions`，已包含与 `RetrievalQuery` 同名的 kind/role/span 字段。Parser 不把 hierarchy span 改写成 event-time，也不从 TEMPORAL 通道推导 TIME kind。

`ParsedQuery.extensions["schema_temporal"]` 的规范字典字段如下：

| 字段 | 语义 |
|---|---|
| `mode` | `latest` / `snapshot` / `history` / `range` |
| `event_at` | snapshot 的事件时间点 |
| `event_from` / `event_to` | range 的事件时间半开边界 |
| `event_precision` | `unknown` / `year` / `month` / `day` / `datetime` |
| `knowledge_as_of` | 按 `[t_valid, t_invalid)` 回溯知识版本 |
| `property_names` | 可选的属性白名单 |
| `include_undated` | 是否保留没有 event-time 的属性 |
| `include_archived` | 是否允许归档属性参与 knowledge-time 选择 |
| `fallback_policy` | snapshot/range 选择为空时的 `none` / `full_timeline` 策略 |
| `entity_limit` / `per_entity_limit` | 实体数与单实体 Property 数上限 |

为兼容简单调用，值 `true` 表示使用 `latest`，非空字符串表示相应
`mode`。强类型 `RetrievalQuery.schema_temporal` 以及字典值未显式给出
`knowledge_as_of/include_archived` 时，继承查询的 `as_of/include_archived`。
`latest/history` 默认保留无 event-time 属性，
`snapshot/range` 默认不保留。
扩展解析或时序选择异常时，Retriever 返回 TEMPORAL `ChannelError`，
并使用未经时序选择的已融合候选继续后续链路。

### ExpandRequest / ExpandIssue / ExpandResult（内部契约）

```python
@dataclass
class ExpandRequest:
    root: MemoryUnit
    query: ParsedQuery
    depth: int
    select: Callable[[MemoryUnit, int], bool]  # 披露与共享预算准入
    seen: set[NodeKey] = field(default_factory=set)
    node_limit: int = 1000

@dataclass
class ExpandIssue:
    code: str

@dataclass
class ExpandResult:
    actual_depth: int = 0
    selected_count: int = 0
    visited_count: int = 0
    truncated: bool = False
    complete: bool = True
    issues: list[ExpandIssue] = field(default_factory=list)
```

以上类型不暴露为公开 MemoryAPI 方法。depth 为正整数、node_limit 为非负整数。
actual_depth 是准入后代的最大边深度；空结果为 0。complete=true 表示请求深度内
遍历处理无 issue、无预算/节点上限截断，不保证全库一致性或并发快照。
NodeKey 为 `(org, space, user, agent, session, id)`。issues 不包含被排除子节点的详情。

### 既有结果结构

| 类型 | 精确字段 |
|---|---|
| `ScoredUnit` | `unit_id` / `score` / `channel` / `evidence` |
| `ChannelEvidence` | `channel` / `rank` / `score` / `weight` / `contribution` |
| `RetrievedItem` | `unit_id` / `score` / `abstract` / `overview` / `content` / `user_metadata` / `system_metadata` / `level` |
| `TrajectoryStep` | `stage` / `channel` / `candidate_count` / `cost_ms` / `detail` |
| `RetrievalResult` | `items` / `trajectory` / `errors` |
| `SchemaTemporalRetrievalResult` | 继承 `RetrievalResult`，仅在有结构化结果时增加 `schema_temporal` |

不得向既有 `RetrievedItem` 增加 `child_ids` 或把 `content` 改作树容器。

### 枚举

| 枚举 | 值 |
|------|------|
| `DisclosureLevel` | L0 / L1 / L2 / ADAPTIVE |
| `RecallChannel` | DOCUMENT / KEYWORD / VECTOR / GRAPH / TEMPORAL |

### 轨迹

普通链路沿用 `parse/recall/fuse/rerank/threshold/disclose`。Schema 时序选择命中时
在 `disclose` 之后增加 `schema_temporal` 轨迹，记录组装后的实体数；
执行失败时记录降级原因并继续使用已融合候选。
层级召回额外使用：

- `parent_recall`：`detail` 至少记录 `kind`、`role`、span、父候选数。
- `expand`：每个 root 一步，`detail` 至少记录 `root_id`、`kind`、`requested_depth`、`actual_depth`、`item_count`、`truncated` 和截断原因。

`with_trajectory=false` 时 `RetrievalResult.trajectory=[]`；公开 `expand` 的 `with_trajectory` 独立控制 `ExpandResult.trajectory`。

## 错误语义

| 异常 | 场景 |
|---|---|
| `ValidationError` | 深度、预算、span 或 kind/role 组合非法；root kind 不匹配 |
| `NotFoundError` | 展开 root 不存在或不在请求 scope |
| 树开关关闭 | API 忽略树参数并执行普通召回；不因传入合法树参数抛 PolicyError |
| `BackendError` | 召回或点读后端失败，且不能按 issue 规则局部处理 |

## 实现注册机制

```
jiuwen_memory/retrieval/<算子>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`QueryParserProducer` / `FuserProducer` / `DiscloserProducer` / `RetrieverProducer`。
注册由 `retrieval.bootstrap.register_operators` 统一触发。`RecallerProducer` 归存储层
（`storage/domain_store_impl/recaller.py`，注册随 `storage.bootstrap.register_backends`
触发）；YAML 命名空间仍是 `recaller`，配置写法不变。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S02-memory_api | MemoryAPI.search → Engine → 本层 Retriever |
| S03-control | Engine.recall 委托本层 Retriever |
| S05-construction | 本层消费构建层产出的内容索引、Schema 时间字段和 Entity → Property 反向索引 |
| S06-storage | Retriever 经 `StoreManagerProducer.resolve` 取全局 manager 并持其 `domain_store()`；`Recaller` 契约、实现与装配全在存储层数据面（`domain_stores.<name>` 的选择键 → `for_manager` 组装），本层不持有召回路 |
| S07-common | 复用 Tokenizer/Embedder/FeatureExtractor/LLM/Reranker |
| S08-config | Schema TemporalEntity 装配门、自动启用及调优参数；其他能力开关与 rerank/embedder 晚绑定经 ConfigSource |
| architecture.md §8 | 检索链路设计 |

### 时间边界解释

查询时间边界与索引字段共用 S07 的时间口径：无时区 datetime 按 UTC 解释，
有时区 datetime 保持实际时刻；不得通过服务器本地时区生成 epoch 值。
valid-time 真源复核及有事件时间的边界比较也使用该规则，避免混合时间表示抛错。
保留既有 `in_event_window` 对未知/无时区事件时间的宽松兜底，索引前置过滤与显式
FilterExpr 复核仍按统一时间投影判定；不改变未知时间哨兵和半开区间边界。


## 普通召回与生成父节点

普通召回在索引 top-k 前排除 TIME 的 time_span/scene/event，并在真源物化时
独立复核。Snapshot 与无结构记忆保持原有召回行为；get/list 点读能力不受此规则影响。
显式 kind 查询使用活动结构、可选角色及结构区间条件替代普通父隔离，仍复核本体。

结构查询的真源复核保留微秒精度，索引毫秒投影只负责前置筛选。
valid-time 起点为空表示无下界：前置使用 NOT(t_valid > as_of)，真源按半开区间复核。


## 分阶段检索与可选 pipeline 契约

`Retriever.retrieve(scope, query)` 保持不变。`StagedRetriever` 是可选的中间 ABC，
声明下表阶段，`retrieve` 仍抽象，由实现提供编排。非分阶段、组合型插件无需实现这些阶段。
阶段签名为 `(self, state: RetrieveState)`；除 `parse -> bool`、
`finalize -> RetrievalResult` 外均返回 None。state 是插件扩展契约中的内部调用态，
不进入 API 序列化；每次调用独立创建，不保存在共享 retriever 上。

| 步骤 | 前置条件 | 完整业务写集 |
|---|---|---|
| validate | 输入已创建 | hierarchy |
| parse | hierarchy 就绪 | parsed（kind/role/span/extensions/as_of）；无意图返回 False |
| build_filters | parse=True | parsed.scalar_filters/recheck_filters/include_archived，user_filters |
| select_channels | parse=True | channels |
| plan_budget | 校验通过 | recall_k，budget_n |
| recall_and_fuse | 过滤、通道、预算就绪 | candidates（融合序），errors |
| cut_to_budget（state 辅助） | 融合之后、精排之前 | candidates，recalled_children；run.fused_count |
| rerank | 预算截断完成 | candidates，calibrated |
| rollup | query.rollup=True；精排后阈值前 | candidates，rolled，errors，parsed.hierarchy_role |
| threshold | 最终候选分已生成 | candidates |
| cut_to_top_k（state 辅助） | 阈值之后 | final |
| disclose | final 就绪 | items |
| build_result（state 辅助） | 披露完成 | result |
| expand | query.expand_depth>0；result 就绪 | result |
| finalize | 普通 result 就绪 | 返回结果，不修改 state 字段 |

所有阶段可向 run 追加轨迹；输入 scope/query/run 引用只读使用。
状态创建不输出 start 日志；validate 成功后才调用 log_start，disclosure 使用枚举 value，
非法输入不得因日志格式化而改变校验异常。三个 state 辅助方法是固定
语义，不是可替换算子；自定义编排仍须满足前置条件，由契约测试验证，不声称运行时禁止跳过。
parse 短路时 parsed 可为空，返回带已有轨迹的空结果。其余产物初始为 None、空列表或零值；
calibrated=False、rolled=None。RollupResult 是共享阶段数据，不依赖具体上卷实现。

预算截断后、精排前保存 recalled_children 快照；精排不得原地修改快照中的候选。
TIME 父角色上卷展开时，即使快照为空也须调用 keep_recalled 标记已准备完成。
expand 先创建 PreparedRetrievalResult；defer_expansion=True 时直接交跨空间层完成展开，
不调用 finalize。否则完成展开，恢复普通结果后才进行 Schema 等后处理。

`RetrievalQuery.pipeline: str | None` 是控制层选择键，与 DomainStore 的
`RetrievalPipeline` 三种物理执行路径无关。直接调用 Retriever.retrieve 不解析该键，
仍使用当前实例；选择发生在 Engine。API 消费保留扩展键，不传入 ParsedQuery.extensions。

`Retriever.query_parsers() -> tuple[QueryParser, ...] | None` 是可选依赖声明：
None 表示未声明；声明时返回稳定、真实的已注入 parser 实例，不得在查询中构建依赖。
组合实现经子组件公共方法汇总，不得读取私有字段。显式配置非空可选 pipeline 白名单时，
默认 retriever 和每个白名单成员必须声明非空 parser 集合，且所有成员均为同一个实例；
不满足则装配失败。未启用白名单的旧插件不因缺少声明被拒绝。
这项检查只证明查询侧共享，默认 parser 与构建侧 tokenizer/embedder 的一致性仍由既有装配契约负责。

选择器在 with_trajectory=True 时加入首个 pipeline step，detail 为 name/source。
source=query 时 name 为白名单公开名，source=profile 时为 profile 名，source=default 时为
默认路由标识 default；后两种不是白名单存在性声明。检索直接调用的阶段轨迹不增加此 step。
