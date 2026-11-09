# 分阶段 Retriever 基类与按查询选择检索 pipeline

## 元信息
| 项 | 值 |
|---|---|
| 日期 | 2026-10-31 |
| 状态 | 已实现（验证结果见下文） |
| 影响范围 | jiuwen_memory/retrieval/staged_retriever.py（新增），jiuwen_memory/retrieval/retriever.py，jiuwen_memory/retrieval/retriever_impl/，jiuwen_memory/retrieval/types.py，jiuwen_memory/control/engine_impl/，jiuwen_memory/control/collective/cross_space_recall.py（只读核对），jiuwen_memory/api/memory_api_impl/query_ops.py，docs/specs/S02-memory-api.md，docs/specs/S03-control.md，docs/specs/S04-retrieval.md |
| 测试基线 | 本轮实现验证见「验证」；原稿的改动前全绿记录未在本轮独立复测 |
| Refs | 待确认 |

## 背景

1. **`PipelineRetriever.retrieve` 过长**：约 385 行，校验 → 查询理解 → 谓词 → 通道 → 召回/融合 →
   精排预算 → 重排 → 上卷 → 阈值 → top_k → 披露 → 展开 → Schema TemporalEntity，12 个阶段写在
   同一个函数里，阶段之间靠十几个局部变量和两个嵌套闭包（`record_step` / `step`）传递状态。
   改任何一个阶段都得通读整个函数；要做一条"只换重排策略"的变体，只能复制整个函数。
2. **检索链路在启动时就固定了**：QueryParser / Fuser / Reranker / Discloser 在 `_build`
   工厂里经 `XxxProducer.dep` 解析依赖，存进实例字段，`retrieve` 运行时没有切换入口。调用级覆盖目前
   只有 `RetrievalQuery.channels` / `rerank` 两个，而且 `MemoryAPI.search` 构造
   `RetrievalQuery` 时没有传这两个字段，公开接口实际上碰不到。
3. **需求**：用户希望在配置文件里预先定义多条检索 pipeline（比如"快速""精排""文档"），查询时
   为单次检索选用其中一条。
4. **现有 profile 路由解决不了这个需求**：S03 的 `MemoryPipeline.select_for_recall` 按
   `memory_type` 选 profile，一个 profile 同时绑定 `index_builder` / `retriever` / `evolver`。
   它表达的是"这类数据怎么建、怎么查"，路由值由数据类型决定，不是调用方的检索偏好。拿它表达
   "同一份数据换一种查法"，会把写入侧的绑定一起拖进来。

## 决策

### D1. 引入分阶段基类 `StagedRetriever`，阶段方法在基类声明

新建 `retrieval/staged_retriever.py`，定义 `StagedRetriever(Retriever)`（阶段契约）与
`RetrieveState` / `RetrieveRun`；`retrieval/retriever.py` 只保留入口契约，`Retriever` ABC 本身不变：

```
Retriever (ABC)                    retrieve(scope, query) -> RetrievalResult
├── StagedRetriever (ABC)          声明阶段方法 + RetrieveState / RetrieveRun 契约
│   └── PipelineRetriever          实现全部阶段 + retrieve 编排（现有行为）
│       └── <变体>Retriever         只覆写需要改的阶段，或改写 retrieve 的编排顺序
└── MultimodalRetriever            组合型，不分阶段，保持直接继承 Retriever
```

基类声明的阶段（全部为 `@abstractmethod`，签名统一为 `(self, state: RetrieveState)`），以及
`RetrieveState` 上的三个固定步骤（`cut_to_budget` / `cut_to_top_k` / `build_result`，
是 state 的固定语义辅助方法，不是可替换阶段；不支持覆写，但不声称运行时阻止自定义编排跳过）。表中"写入"列是**完整写集**，含对 `parsed`
字段的就地修改：

| 步骤 | 前置条件 | 读取 state | 写入 state | 说明 |
|---|---|---|---|---|
| `validate` | — | query | hierarchy | 入参校验，非法直接抛 `ValidationError` / `UnsupportedCapabilityError` |
| `parse` | hierarchy 已就绪 | query, hierarchy | parsed（含 `hierarchy_kind/role`、`span_*`、`extensions`、rollup 时的 `as_of`）；返回 `bool`（False = 短路） | 空 query、清洗后为空、no_intent 三种短路；短路时 parsed 可能为 None。rollup 请求下 `parsed.hierarchy_role` 置为 None（候选不限目标角色） |
| `build_filters` | parse 返回 True | query, hierarchy, parsed | parsed.scalar_filters / recheck_filters / include_archived，user_filters | 系统谓词以外层 AND 合并（铁律 9） |
| `select_channels` | 同上 | query, parsed | channels | 调用级覆盖、doc_mode 补 DOCUMENT；显式空列表抛 `ValidationError` |
| `plan_budget` | 同上 | query | recall_k, budget_n | 召回超采样与精排预算 |
| `recall_and_fuse` | channels / recall_k / budget_n 已就绪 | scope, parsed, channels, recall_k, budget_n | candidates（融合序）, errors | 三条 DomainStore 首选路径 + Fuser + recall/fuse 轨迹 |
| `state.cut_to_budget()` | recall_and_fuse 之后、rerank 之前 | candidates, budget_n, query | candidates（截到 budget_n）, recalled_children | 记 `recheck` 轨迹。仅 `uses_recalled_children(query)` 为真时把**截断后、精排前**的融合序候选快照进 `recalled_children`，否则置空列表——供父子补位按原融合序使用，精排不得改写该快照 |
| `rerank` | cut_to_budget 之后 | query, parsed, candidates | candidates, calibrated | 可选重排；`BackendError` 降级（calibrated=False，候选保持原序） |
| `rollup` | `query.rollup` 为真；rerank 之后、threshold 之前 | scope, query, hierarchy, parsed, candidates | candidates, rolled, errors（追加 HIERARCHY 诊断）, **parsed.hierarchy_role**（恢复为目标角色） | 编排只在 `query.rollup` 时调用；未调用时 rolled 保持 None |
| `threshold` | — | query, candidates, calibrated | candidates | 统一相关性阈值 + `min_results` 回填 |
| `state.cut_to_top_k()` | threshold 之后 | candidates, query | final | 展开请求记 `parent_recall` 轨迹 |
| `disclose` | final 已就绪 | query, parsed, final | items | 纯内容塑形（铁律 10）；展开请求固定 L0、不限预算 |
| `state.build_result()` | disclose 之后 | items, errors, run | result（普通 `RetrievalResult`） | 记 `retrieve done` 日志；**result 只在此处创建** |
| `expand` | `query.expand_depth > 0`；result 已创建 | scope, query, parsed, final, result, rolled, recalled_children | result | 见下方"展开的结果类型转换" |
| `finalize` | result 为普通 `RetrievalResult` | scope, query, parsed, final, user_filters, result | 返回 `RetrievalResult` | Schema TemporalEntity 等后处理；延迟展开时不调用 |

**展开的结果类型转换**（`expand` 的写集约束）：

- 先用 `prepare_expansion(result, source, final)` 把普通结果转成内部的
  `PreparedRetrievalResult`；`uses_recalled_children(query)` 为真且 `rolled` 不为 None 时，
  调用 `keep_recalled(recalled_children, rolled.recalled_parents)`，即使快照为空也标记准备完成。
- `query.defer_expansion` 为真（跨空间先全局选根）时，`state.result` 写为该
  `PreparedRetrievalResult`，编排**直接返回**，不进 `finalize`；由跨空间层统一 `complete_expansion`。
- 否则写为 `complete_expansion(result, [prepared], query)` 的普通 `RetrievalResult`，再进 `finalize`。
- `PreparedRetrievalResult` 只在内部传递，不序列化、不返回给公开调用方（现有约束不变）。

**编排归属**：`StagedRetriever.retrieve` 保持抽象，编排写在 `PipelineRetriever.retrieve`（按需求
"子类写 retrieve 的具体实现"）。变体通常继承 `PipelineRetriever`，**直接复用它的 retrieve**，
只覆写需要改的阶段；只有确实要调整阶段顺序时，才覆写 `retrieve`。`PipelineRetriever.retrieve`
缩成约 40 行的阶段调用序列，顺序与现状一致：

```python
def retrieve(self, scope, query):
    state = RetrieveState.start(scope, query)
    self.validate(state)
    state.log_start()                   # 校验成功后才格式化并记录开始日志
    if not self.parse(state):
        return state.empty_result()
    self.build_filters(state)
    self.select_channels(state)
    self.plan_budget(state)
    self.recall_and_fuse(state)
    state.cut_to_budget()               # 截到 budget_n + recalled_children 快照 + recheck 轨迹
    self.rerank(state)
    if state.query.rollup:
        self.rollup(state)
    self.threshold(state)
    state.cut_to_top_k()
    self.disclose(state)
    state.build_result()
    if state.query.expand_depth:
        self.expand(state)
        if state.query.defer_expansion:
            return state.result         # PreparedRetrievalResult，交跨空间层完成展开
    return self.finalize(state)
```

**阶段间状态 `RetrieveState`**（`retrieval/staged_retriever.py`，内部调用态，属于子类扩展契约但不进入 API 序列化）：

- 输入引用：`scope`、`query`、`run`，由 `RetrieveState.start` 创建；各阶段按契约只读使用输入，不作深冻结。
- 其余字段初始为 None / 空列表（`errors=[]`、`recalled_children=[]`、`calibrated=False`、
  `rolled=None`），只能由上表"写入"列所列的步骤写入，其他步骤只读。
- 写集约定写进 S04；单测按阶段校验写集与前置条件。

**单次调用运行态 `RetrieveRun`**：收拢 `trace_id`、`scope_dims`、`started_at`、`trajectory`，
提供 `record()` / `step()` / `log_done()`，替代现有的嵌套闭包；
`SchemaTemporalPipelineExtension.apply(record_step=run.step)` 签名不变。
`RetrieveState.start` 只创建状态（用 `_scope_log_dims` 提取维度名）；validate 成功后才调用
`state.log_start()`，通过 `_format_channels` 格式化通道、以 disclosure.value 记录披露级别。
非法输入保持校验异常，不被日志格式化错误抢先覆盖。日志辅助测试直接覆盖
生产使用的函数，并验证 start 日志不输出 scope 值或 query 正文。错误脱敏复用
`common.errors.safe_error_message`，不再保留无调用方的 `_safe_error` 包装。

### D2. 多条 pipeline = 多个具名 retriever 实例，公开名单独声明

**pipeline 由 retriever 具名实例表达**，复用现有 Factory 具名实例机制，不新增顶层算子命名空间：

```yaml
# 被引用的算子先声明为具名实例：字符串引用走 build_named，未声明即装配失败
fuser:
  default: rrf
  score_max: score_max
reranker:
  bge:
    target: bge_reranker
    params: { ... }             # 模型路径等

retriever:
  default:                      # 现状，行为不变
    target: pipeline
    params: { query_parser: default, fuser: default, reranker: default, discloser: default, ... }
  fast:                         # 不精排、取通道最大分
    target: pipeline
    params:
      query_parser: default     # 必写：与索引侧同一 embedder / tokenizer
      fuser: score_max
      discloser: default
      rerank_enabled: false
      rerank_max: 20
  precise:                      # 交叉编码器精排 + 校准阈值
    target: pipeline
    params:
      query_parser: default
      fuser: default
      discloser: default
      reranker: bge
      rerank_max: 100
      min_score: 0.3
  doc_first:                    # 变体子类：覆写部分阶段
    target: doc_first_pipeline
    params: { query_parser: default, ... }

engine:
  default:
    params:
      retriever: default                # 未指定 pipeline 时的默认
      retrieval_pipelines:              # 对外可选名单：公开名 → retriever 实例名
        default: default
        fast: fast
        precise: precise
```

- **新增实例不继承 `retriever.default` 的配置**：每个具名实例的 params 是独立的整份配置。
  依赖字段缺省时，`Factory.dep` 会用 `default=` 实现**匿名新建**一个不共享的实例，不会引用
  同名的 `default` 实例。对 `query_parser` 来说，这意味着会得到一个 embedder 为 `hashing`、
  tokenizer 为 `whitespace` 的新 parser，部署若用了其他 embedding 模型，召回向量就和已有索引
  不在同一向量空间（违反铁律 2），而且不报错。因此：
  - 每条 pipeline **必须**显式写 `query_parser: <共享实例名>`；`fuser` / `discloser` /
    `reranker` 同理，要么引用具名实例，要么有意内联。
  - engine 装配显式非空白名单时，经 `Retriever.query_parsers()` 公共可选声明校验：
    默认与白名单 retriever 必须返回非空 parser 集合，且全部是**同一实例**，否则启动失败。
    不读取 `_parser`，不识别具体插件类型；MultimodalRetriever 转交其基础 retriever 声明。
    旧插件默认返回 None，未启用白名单时保留原行为；要进入白名单须实现声明。
    默认 parser 与构建侧共享依赖仍由现有拓扑保证，不能仅凭查询侧身份检查推导索引兼容。
    不同 parser 的场景本期不支持，见「已知遗留」。
  - 其余数值参数（`over_fetch_*`、`recall_max`、阈值等）缺省时回退到出厂默认，而非
    `retriever.default` 中的值。

- **两层变化分别对应两种手段**：只换算子或调参，新增一个 `target: pipeline` 的实例即可，
  不写代码；要改阶段行为或阶段顺序，写一个 `PipelineRetriever` 子类，覆写对应的阶段方法或
  `retrieve`，再用 `@RetrieverProducer.register` 注册新 target。
- **公开名与实例名解耦**：`retrieval_pipelines` 是白名单，`retriever` 命名空间里给 profile 或
  `MultimodalRetriever` 内部用的实例不会自动暴露给调用方。
- **装配期全量构建**：engine 装配时把白名单里的实例全部构建好，存成 `dict[str, Retriever]`。
  引用和共享关系错误在启动时暴露，查询路径上不临时构建 retriever 或算子实例。
  模型权重的加载时机仍由插件负责（例如 BGE 既有懒加载），不把实例构建等同模型预热。
  共享插件**以字符串引用同一具名实例且未设 new_instance 时**，由 Factory 缓存保证同一实例；
  缺省或内联都会得到不共享的新实例（见上一条）。
- 未配置 `retrieval_pipelines` 时，等价于只有 `{default: <engine.retriever>}`，不重复构建默认实例。
  显式空映射公开零个名称，但无名称请求仍走 profile/default。defaults.py 不自动注入白名单，
  避免对未启用该能力的旧插件强制 parser 声明。名称与引用须是非空字符串。
- 三种 Engine 共用 engine_impl/retrieval_pipelines.py，健康检查按对象身份去重聚合；
  新实例的资源所有权继续由 Factory 观察和现有装配根管理，不引入第二套生命周期。

### D3. 查询侧选择：保留扩展键 `retrieval_pipeline` → typed `RetrievalQuery.pipeline`

- 调用方经 `context.extensions["retrieval_pipeline"] = "precise"` 指定 pipeline。
  `query_ops.search` 在 API 边界把它 pop 出来，写入 typed 字段
  `RetrievalQuery.pipeline: str | None`，不再随 extensions 透传给自定义模块。处理方式与已有的
  `max_tokens` / `spaces` / `query_context` 一致。CLI / HTTP / MCP / SDK 几种接入形态都经
  extensions 透传，无需逐个改签名。
- **选择优先级**（engine 的 `recall` 中解析，单空间与跨空间两条路径共用）：
  1. `query.pipeline` 显式指定 → 在 `retrieval_pipelines` 中查找；
  2. 否则，若启用了 profile 路由 → 使用 `binding.retriever`（现状）；
  3. 否则 → engine 默认 retriever。
- **未知名称直接拒绝**：抛 `ValidationError`，错误信息带上可选名单，不静默回退到 default。
  调用方显式选了一条不存在的路径，静默回退会让"我以为用了精排"的问题在线上很难排查。
- **名称校验在扇出之前完成，两处都要校验**：
  - **API 边界（主校验点）**：`query_ops.search` 在 pop 出 `retrieval_pipeline` 后、
    **在 `spaces` 分流（单空间 / 跨空间）之前**，经 MemoryQueryService 转交到 engine 的
    公共 `validate_retrieval_pipeline()`，由其使用 `retrieval_pipeline_names()` 校验。
    API 不复制规则或访问私有白名单；未知名称立即抛 `ValidationError`。
    这是必须的：跨空间路径 `cross_space_recall` 用 `gather(return_exceptions=True)` 把每个
    空间的异常转成空间级 `ChannelError`；候选空间为零时会在扇出前直接返回空结果，
    根本不调 `recall`。只在 `engine.recall` 内校验，会让未知名称在跨空间下变成"空结果 + 错误
    列表"或静默空结果，违背上一条语义。
  - **engine.recall（兜底）**：不经 API、直接调用 `MemoryEngine.recall` 的调用方仍在 `recall`
    内解析并校验，未知名称抛同样的 `ValidationError`；API 校验通过后这里不会再失败。
    直接持有 retriever 实例调用 `retrieve` 的内部组件（如 `control/jobs_impl/candidate_resolver`）
    不经过选择逻辑，`query.pipeline` 对其无效，保持装配时注入的 retriever。
- **可观测**：轨迹新增首个 step `pipeline`，detail 为 `{"name": <公开名>, "source": "query|profile|default"}`；
  Engine 记录实际选择的 name/source；阶段运行态 start 日志记录 query.pipeline。
  profile 分支的 name 是 profile 名，default 分支是路由标识 default，不声称它们必在白名单中。

### D4. 不变量不随 pipeline 变化

内置 pipeline 经全局 StoreManager 解析配置所指的 DomainStore；同一默认引用共享实例，
不额外禁止已有命名数据面配置。共享存储本身不能保证以下约束；它们是每个 Retriever 插件
必须遵守的契约，由阶段实现、review 与契约测试验证：

- 系统谓词与用户 FilterExpr 在 top-k 之前下推（铁律 7、9）；
- 真源复核在 Fuser 之前完成（本地约束 3、6）；
- Discloser 只做内容塑形（铁律 10）；
- scope 作显式首参贯穿全链路（铁律 1）。

变体子类覆写 `build_filters` / `recall_and_fuse` 时不得跳过上述约束。共享契约测试套件对
内置实现与测试变体执行隔离、过滤和生命周期用例；新增第三方 target 需提供满足 S09 的契约测试，
不自动实例化所有已注册插件（第三方插件可能要求外部资源）。

### 相关规约同步

S03 同步明确「数据操作异步、能力查询和无 I/O 校验同步」，对应新增的名称查询与校验方法；
同时按现有 PEP 代码修正授权入口为 Authorizer，旧 PermissionManager 仅保留历史兼容。
这些修订更新文档事实，不改变授权行为。

## 拒绝的方案

1. **把阶段方法直接加到 `Retriever` ABC**：`MultimodalRetriever` 是组合型 retriever，
   它的 `retrieve` 是对多个基础 retriever 的扇出与 RRF 融合，没有 parse / rerank / disclose
   这些阶段，强行实现只能写一堆 `NotImplementedError`。所以中间加一层 `StagedRetriever`，
   不分阶段的 retriever 继续直接实现 `Retriever`。
2. **阶段方法用显式长参数列表，不用 `RetrieveState`**：现状重构时每个阶段要 5～7 个参数，
   后续给某个阶段加一个输入，所有覆写它的子类都得跟着改签名。用 state 对象 + 文档化的
   读写集，签名稳定，代价是写集靠约定和测试保证，而不是靠类型系统。
3. **在 YAML 里用 `stages: [parse, recall, rerank, ...]` 声明阶段序列（DSL）**：阶段之间有硬
   顺序约束（过滤先于 top-k、上卷在精排后阈值前、Schema Temporal 在披露之后），配置化的序列
   很容易拼出违反铁律的组合，还要额外写一套校验器。顺序变化不频繁，交给子类代码表达，由
   review 和契约测试把关。
4. **查询时按算子名自由组合**（`RetrievalQuery.fuser="score_max"`、`reranker="bge"`…）：
   组合空间不可控，阈值、`rerank_max` 等参数和算子是耦合的（校准分阈值只对校准过的 reranker
   有意义），自由组合容易产出无意义的结果；按名字取算子还会把算子实例名变成公开 API。
   改为预先定义好、经过验证的完整 pipeline。
5. **复用 S03 profile 路由，新增一个路由键**：profile 同时绑定写入侧（`index_builder` /
   `evolver`），选 profile 的语义是"这类数据"，不是"这种查法"；而且一个 profile 只能绑一个
   retriever，表达不了"同一类数据有多种查法"。两者保持正交：profile 决定默认的查法，
   `query.pipeline` 显式覆盖。
6. **typed 参数 `MemoryAPI.search(..., pipeline=...)`**：可发现性更好，但要同步改 SDK / HTTP
   schema / MCP tool schema / CLI 四种接入形态，也和现有 `spaces` / `query_context` 等扩展键的
   约定不一致。先用保留扩展键；如果以后需要提升为 typed 参数，可以平滑迁移（边界处两者都接受）。
7. **由 `StagedRetriever` 提供默认编排（具体的 `retrieve`），子类只实现阶段**：与需求"子类写
   retrieve 的具体实现"不符。编排复用的目标已经通过"变体继承 `PipelineRetriever`、直接复用其
   retrieve"达到。两种做法都约束不了子类覆写后的执行顺序，只是把默认编排放在不同层；顺序
   约束由轨迹快照测试与契约测试兜底。如果以后出现第二个不继承 `PipelineRetriever` 的分阶段
   实现，需要复用同一编排，再把 `PipelineRetriever.retrieve` 上提到基类，这一步迁移不改阶段签名。
8. **只在 `engine.recall` 内校验 pipeline 名称**：跨空间扇出会把异常转成空间级 `ChannelError`，
   零目标空间时不会调用 recall，未知名称会表现为空结果，而不是 `ValidationError`（见 D3）。

## 验证

实现验证范围（实际执行结果列于本节末尾）：

1. **纯重构回归**：运行 `tests/unit/retrieval` 与 `tests/integration`，不改动已有断言；
   环境依赖导致的失败或跳过单独报告，不宣称全绿。
2. **轨迹快照**：新增 `tests/unit/retrieval/test_pipeline_stages.py`，固定
   `with_trajectory=True` 时三条 DomainStore 首选路径、rerank 降级、rollup、expand 等场景下的
   stage 序列与关键 detail，防止日后拆改把阶段顺序弄乱。
3. **阶段可覆写**：测试子类只覆写 `rerank`，其余阶段走父类实现，结果与预期一致。
4. **阶段写集与展开**：
   - 精排改变候选顺序后，`expand` 补入的后代仍按 `cut_to_budget` 快照的**原融合序**排列；
   - `defer_expansion=True` 时返回 `PreparedRetrievalResult` 且不进 `finalize`，跨空间层
     `complete_expansion` 后的结果与重构前一致；
   - rollup 之后 `parsed.hierarchy_role` 恢复为目标角色。
5. **pipeline 选择**：`tests/unit/control/` 下覆盖优先级（query > profile > default）、
   未配置白名单时行为不变。
6. **未知名称**：单空间、跨空间（多目标）、跨空间零目标空间三种情况下都抛 `ValidationError`，
   且都在 recall 之前；直调 `engine.recall` 时同样抛出。
7. **真实装配**：用含 `retrieval_pipelines` 的 YAML 走完整 `build_kernel` 装配，覆盖三种 Engine，断言白名单内
   各 retriever 的 QueryParser（及其 embedder / tokenizer）与默认 retriever 是同一实例，与索引侧
   embedder 也是同一实例；缺 `query_parser` 引用时启动失败。
8. **API 边界**：`retrieval_pipeline` 扩展键被 pop 后写入 typed 字段，不出现在
   `ParsedQuery.extensions` 中。
9. **契约测试**：内置分阶段实现与仅覆写 rerank 的变体覆盖 scope 隔离、过滤先于 top-k、
   lifecycle 复核。第三方新增 target 自带契约测试，不在启动时自动跑测试。
10. 变更文件 `ruff check --fix` 后再 `ruff check` 验证；仓库已有 lint 问题单独列出。

本轮执行结果（2026-10-31）：

- 现有 `tests/unit/retrieval tests/unit/control`：964 passed，5 skipped。
- 新增用例：control 的具名装配/选择 20 passed，retrieval 的阶段契约 10 passed，
  API 的边界选择 20 passed（API 20 项也包含在下一条完整回归中）。
- `tests/unit/api tests/integration`：1233 passed，74 skipped，3 failed。
  三个失败均为 watchdog 未安装导致的既有生命周期测试：
  `test_server_facade_exposes_upstream_start_without_raw_ports`、
  `test_sync_start_boots_watchdog_in_loop_thread_and_close_stops_it`、
  `test_async_start_background_boots_watchdog_in_current_loop`。
  初次沙箱执行的 HTTP 监听限制已在沙箱外重跑排除；跳过的外部后端未宣称验证。
- 既有检索行为断言保留；日志测试的导入随生产辅助函数迁移，脱敏测试直接调用公共实现。
  父子原融合序、跨空间延迟展开沿用已有回归，加测阶段结果转换。
- 日志回归覆盖 3 个 start 格式化用例与 3 个非法参数先校验用例；捕获 handler 直接
  挂载目标 logger 并恢复配置，不依赖先前装配后的父 logger 传播状态。
  已复现「先装配、后日志测试」导致旧用例失败，修复后同序回归通过；
  `tests/unit/api/test_build_kernel_config.py tests/unit/retrieval` 合计 528 passed。
- 所有变更 Python 文件的 ruff 检查通过，git diff --check 通过；全仓 ruff 仍有
  23 个既有问题，均在本轮未修改的文件中，未扩展本次实现范围处理。


## 已知遗留

1. **profile 与 pipeline 的兼容性没有校验**：显式指定的 pipeline 会覆盖 profile 的
   retriever；如果某个 profile 的 `index_builder` 建出的索引布局（比如没建 L0/L1 分层表）与所选
   pipeline 的召回假设不一致，召回会变弱，但不会报错。后续可以在 profile 上声明
   `allowed_pipelines` 做约束。
2. **白名单没有按调用方分权**：目前所有调用方都能选任意公开 pipeline。如果 `precise` 这类
   高成本 pipeline 需要限流或按租户开放，需要接入 Policy / Governor，本期不做。
3. **`MultimodalRetriever` 的基础 retriever 仍是装配期固定的**：多模态检索里选 pipeline 只能
   选整个 multimodal 实例，不能只替换它内部的 native 分支。
4. **`recheck` 轨迹 step 的 `dropped` 恒为 "0"**：现状遗留（复核已前移到 Fuser 之前），
   重构时保留这个 step 以兼容轨迹消费方，同时删除恒不触发的 debug 分支；以后可以考虑移除该 step。
5. **不支持可选 pipeline 使用不同的 QueryParser**：显式非空白名单要求共享默认 parser；
   旧 profile 路由未纳入此次检查。
   确实需要不同 parser 配置（比如只开 LLM 改写）的场景，需要先区分"必须共享的部分"
   （embedder / tokenizer）和"可按 pipeline 变化的部分"（改写、去噪开关），再放开校验。
6. **内部直接持有 retriever 的组件不受 `query.pipeline` 影响**（如 candidate_resolver），
   仍使用装配时注入的 retriever。

## 待提交确认

1. Refs issue 号。
