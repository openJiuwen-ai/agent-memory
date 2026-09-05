# F05 — Storage 能力适配的 Retrieval Pipeline 设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-06 |
| 影响范围 | 规划中的 `jiuwen_memory/retrieval` pipeline、共享检索类型，以及统一 Storage 的检索入口 |
| 测试基线 | `tests/unit/retrieval/test_storage_pipelines.py`；retrieval 单测与集成测试通过 |
| Refs | [F05-unified-storage-design.md](../storage/F05-unified-storage-design.md) |

> 本文档记录 Retrieval pipeline 设计及首版实现。当前实现已经把 Storage 能力差异
> 收敛为三条 `recall/get/rank` pipeline，并保持 Reranker 在三步内核之外。

---

## 背景

当前 Retrieval 只有一条固定链路：多个 Recaller 先返回 MemoryUnit id，Fuser 在 id 候选上
融合排序，再从 KVStore 点读 MemoryUnit。该链路适合索引与真源分离的本地组合，但不能利用
下列后端能力：

- 召回请求能够直接回带 MemoryUnit，省去按 id 再取一次真源；
- 一体化平台能够在一个 Storage 实现中完成召回、取数和融合；
- 不同召回通道可分别选择原生回带或 `recall + get`，不应被能力最弱的通道统一拖回最慢路径。

Retrieval 需要保留完整编排权，同时根据 Storage 的首选 pipeline 使用不同入口。三条路径
必须保持 Scope、过滤、时态、候选证据和 Fuser 结果等价。

## 目标

1. 只抽象 `recall`、`get`、`rank` 三个步骤；`rank` 仅指 Fuser。
2. 支持 `recall -> get -> rank`、`recall_and_get -> rank`、`retrieve` 三条 pipeline。
3. QueryParser、Reranker、阈值、最终 top-k 和 Discloser 继续由 Retriever 编排。
4. 支持指定召回通道；未指定时使用全部已配置通道。
5. 多通道部分失败时返回成功候选和结构化错误；全部失败才抛异常。
6. 当前阶段保持同步接口，异步化作为后续整体改造。

## 非目标

- 不把 QueryParser、Reranker 或 Discloser 下沉到 Storage。
- 不把 L0/L1/L2 设计成新的 RecallChannel。
- 不在本文确定 Memory API 与 Store 方法改名。
- 不允许不同 pipeline 弱化 Scope、FilterExpr、lifecycle 或双时间语义。

---

## 决策

### 一、三步边界

本文中的检索内核只包含：

| 步骤 | 输入 | 输出 | 责任 |
|---|---|---|---|
| `recall` | ParsedQuery、通道、每通道召回宽度 | 分通道 ScoredUnit | 从索引召回 id、分数和证据 |
| `get` | 去重后的 MemoryUnit id | MemoryUnit | 批量读取真源并完成真源复核 |
| `rank` | 分通道 ScoredMemoryUnit | ScoredMemoryUnit | Fuser 做层内归并、跨通道融合与排序 |

`rank` 不包含 Reranker。完整 Retriever 在三步内核之后继续执行 Reranker、相关性阈值、最终
`top_k` 和 Discloser。

### 二、三条 Pipeline

#### Pipeline 1：RECALL_GET_RANK

```text
Storage.recall
  -> 保留全部分通道候选
  -> 按 unit_id 生成唯一读取列表
  -> Storage.get + 真源复核
  -> 用读取结果恢复各通道候选
  -> Fuser
```

读取前的去重只减少真源读取次数，不能合并召回证据。例如同一 MemoryUnit 同时被 Vector 和
Keyword 命中，只读取一次 MemoryUnit，但必须恢复为两条带各自 score、channel、evidence 的
候选再交给 Fuser。

#### Pipeline 2：RECALL_AND_GET_RANK

```text
Storage.recall_and_get
  -> 对物化候选做真源复核
  -> Fuser
```

CompositeStorage 可以对不同通道采用混合实现：支持原生回带的底层 Store 直接返回物化候选，
其余通道内部使用 `recall + get`。Retriever 只消费统一的分通道结果。

#### Pipeline 3：RETRIEVE

```text
Storage.retrieve(parsed_query, fuser)
  -> Storage 取得分通道物化候选并完成真源复核
  -> Storage 在本地调用传入的 Fuser
  -> 返回融合候选和通道错误
```

`retrieve` 必须调用传入的 Fuser，不能忽略参数改用平台自己的融合算法。一体化远程平台负责
返回分通道物化候选，Storage 适配器在本地调用 Fuser，因此该路径不要求把 Python Fuser
对象序列化到远端。

### 三、通过首选 Pipeline 选择路径

Storage 的通用 capability 只描述 KV、Vector、Fulltext、Graph、Fusion、FS 等底层端口，
不加入 `RECALL`、`RECALL_AND_GET`、`RETRIEVE`。

每个 Storage 实现提供一个全局、稳定的首选 pipeline：

| 首选值 | 适用实现 |
|---|---|
| `RECALL_GET_RANK` | 只能先返回 id、再批量读取 MemoryUnit 的实现 |
| `RECALL_AND_GET_RANK` | 能原生回带内容，或适合在 Storage 内混合原生回带与批量读取的组合实现 |
| `RETRIEVE` | 一体化取得分通道物化候选并在 Storage 入口内调用 Fuser 的实现 |

首选值是实现级静态选择，不随单次 query 或健康状态变化。BackendError 不触发另一条 pipeline
的静默重试，避免重复远程请求和结果语义漂移；部分通道失败按本文错误模型降级。

### 四、Storage 不负责 QueryParser

Retriever 先执行 QueryParser，再把 ParsedQuery 交给 Storage。`Scope` 仍是独立参数，不放入
查询对象；Storage 不解析原始文本，也不决定用户意图。

Retriever 负责把调用级 `RetrievalQuery.channels` 与 parser 建议通道解析为最终通道列表：

- 未指定通道时调用全部已配置通道；
- 指定非空通道列表时只调用选中通道；
- 显式空列表是无效输入，抛 ValidationError；
- 当前同步接口可在实现内部使用线程池并发远程通道，但并发不是接口语义。

### 五、共享类型下沉到 common

Storage 不能反向依赖 Retrieval 实现模块。双方共同使用的契约类型下沉到
`jiuwen_memory/common/type_def/`，至少包括：

- `ParsedQuery`
- `RecallChannel`
- `ChannelEvidence`
- `ScoredUnit`
- `ScoredMemoryUnit`
- `RecallBatch`
- `ChannelError`
- Fuser 所需的最小协议

QueryParser、Fuser、Retriever 的接口和具体实现继续放在 `jiuwen_memory/retrieval`。公共目录只承载数据
契约和协议，不承载查询解析或融合算法。

物化候选包含完整 MemoryUnit 及其召回上下文：

| 字段 | 语义 |
|---|---|
| `unit` | 已从真源取得的 MemoryUnit |
| `score` | 当前召回入口的原始分数 |
| `channel` | 逻辑召回通道 |
| `evidence` | 通道名次、原始分、权重和贡献等证据 |

`RecallBatch` 显式保留一个物理召回入口的候选列表、逻辑 channel 和 source。source 可区分
`vector_l0`、`vector_l1`、`vector_l2` 等入口，Fuser 仍把它们视为同一 VECTOR 通道。

### 六、分层索引不是独立通道

L0 摘要、L1 片段和 L2 全文可以分别建立 Vector 或 Fulltext 索引，但它们是同一逻辑通道的
多个物理入口：

```text
VECTOR  -> vector_l0 / vector_l1 / vector_l2
KEYWORD -> fulltext_l0 / fulltext_l1 / fulltext_l2
```

Fuser 在跨通道融合前先做分层归并：同一 channel、同一 unit 的多层命中取最高分 MaxP，避免
索引覆盖更完整的 MemoryUnit 因多次命中而被重复加权。

### 七、Fuser 改为处理物化候选

Fuser 从“融合 ScoredUnit id”调整为“融合 ScoredMemoryUnit”。它仍接收按召回入口分组的候选，
先执行分层 MaxP，再执行 RRF、Weighted RRF 或 Score Max 等跨通道融合。

Fuser 只使用 MemoryUnit 标识、分数、channel 和 evidence 做融合，不执行 Reranker，不读取
Storage，也不负责最终披露。

### 八、真源复核属于 get 阶段

三条 pipeline 在 Fuser 前必须执行同等真源复核：

- lifecycle；
- valid-time `as_of`；
- event-time `time_from/time_to`；
- 完整 FilterExpr；
- archived 是否可见。

Pipeline 1 和 2 由 Retriever 在物化后调用公共复核逻辑；Pipeline 3 由 Storage.retrieve
调用同一纯函数。复核函数下沉到 common，避免 Storage 反向依赖 Retrieval 实现。

索引过滤仍必须发生在每通道截断之前。真源复核只做纵深防御，不能补回被错误 top-k 截掉的
候选。

### 九、分别定义召回宽度和融合预算

| 参数 | 语义 |
|---|---|
| `recall_limit` | 每个物理召回入口最多返回的候选数，包含 Retriever 的超采样策略 |
| `rank_limit` | Fuser 后最多保留的候选数，用于限制后续 Reranker 成本 |
| `RetrievalQuery.top_k` | Reranker、阈值处理后的最终返回上限 |

Storage 的 `recall`、`recall_and_get` 和 `retrieve` 消费 `recall_limit`；执行 Fuser 的路径同时
消费 `rank_limit`。最终 `top_k` 不下沉到 Storage。

### 十、部分失败返回候选和错误

每个 Storage 检索入口返回候选与结构化通道错误：

| 情况 | 行为 |
|---|---|
| 部分通道失败 | 保留成功通道候选，返回 ChannelError，继续 get/Fuser，并记录 warning |
| 通道成功但无命中 | 返回空 batch，不记错误 |
| 索引命中但真源缺失 | 丢弃该候选并返回数据不一致错误，不拖垮其他候选 |
| 全部选中通道失败 | 抛 StorageRetrievalError，携带所有 ChannelError |
| Fuser 失败 | 整体失败，不按通道降级 |

最终 RetrievalResult 增加始终可见的 errors，不依赖 `with_trajectory`。trajectory 继续记录各步骤
耗时和降级细节，但不是错误的唯一出口。

### 十一、Pipeline 结果等价性

三条 pipeline 必须共同满足：

1. Scope 原生隔离，MemoryUnit id 只在显式 Scope 内解释。
2. 系统谓词与用户 FilterExpr 在每通道截断前下推。
3. Fuser 前完成一致的真源复核。
4. 所有召回分数统一为越大越相关。
5. 同一物理入口内稳定排序，分层入口按 channel+unit 做 MaxP。
6. 多通道证据在读取去重后完整恢复。
7. 使用同一个 Fuser 实例和相同 `rank_limit` 时，融合语义一致。
8. Reranker、阈值、最终 top-k 和 Discloser 位于三条路径之后，执行顺序一致。

---

## 拒绝的方案

### 把 Retrieval pipeline 放进 Storage capability

KV、Vector 等 capability 描述“暴露哪些标准底层端口”；pipeline preference 描述 Retriever
应该调用哪个入口。混在同一集合会让组合能力和底层能力含义不一致，因此分开建模。

### 在 get 前直接按 unit_id 合并候选

该方案虽然减少读取，但会丢失同一 MemoryUnit 的多通道分数和 evidence。只允许对读取 id 去重，
读取后必须恢复原候选结构。

### 把分层索引建成独立 RecallChannel

L0/L1/L2 是索引覆盖差异，不是独立相关性信号。作为独立通道会让拥有更多层级的 MemoryUnit
获得额外权重，因此继续按同通道 MaxP。

### 部分失败直接抛带 partial_result 的异常

调用方必须捕获异常才能拿到成功结果，容易把可用候选整体丢弃。部分失败采用正常结果加
errors；只有全部通道失败才抛异常。

### 在 Storage 内执行 QueryParser 或 Reranker

QueryParser 属于查询理解，Reranker 属于融合后的精排；二者不属于 Storage 对底层能力的适配，
下沉会造成 Storage 反向持有完整 Retrieval pipeline。

### 当前阶段直接改为异步接口

现有 Store 和 Retriever 主体为同步接口。先完成行为重构，再整体设计 async 边界，避免同时
改变执行模型和结果契约。

---

## 验证计划

实现阶段至少需要覆盖：

1. 三种首选 pipeline 分别执行对应路径，不发生隐式路径切换。
2. 指定单通道、指定多通道、未指定通道和空通道输入语义正确。
3. Pipeline 1 对读取 id 去重，但保留重复 id 的多通道候选和 evidence。
4. Pipeline 2 混合原生回带与 `recall + get` 时保持分组和顺序。
5. Pipeline 3 实际调用传入的 Fuser，并在调用前完成真源复核。
6. 三条 pipeline 对 Scope、FilterExpr、双时间和 lifecycle 产出等价结果。
7. L0/L1/L2 同通道多层命中按 unit_id MaxP，不重复加权。
8. 部分通道失败返回成功结果和 errors；全部通道失败抛 StorageRetrievalError。
9. Fuser 失败整体报错，不能被误判为单通道失败。
10. recall_limit、rank_limit 和最终 top_k 在各自阶段生效。
11. Reranker、阈值和 Discloser 在三条 pipeline 后保持现有顺序。

## 已知遗留

- 当前设计保持同步接口；远程多通道的 async 化后续统一设计。
- 首版 `CompositeStorage.recall_and_get` 使用组合实现；原生回带 MemoryUnit 的通道适配器
  与不暴露标准 Store 端口的一体化实现尚未落地。
- 三条 pipeline 已覆盖路径选择、读取去重、物化 Fuser、部分/全部失败；跨三条路径的复杂
  FilterExpr、双时间和 archived 组合等价性还需要增加参数化测试矩阵。
- Memory API 与 Store 方法改名、兼容别名和 deprecation 周期另行归档，不混入本特性。

## 后续演进

- [F07（storage 拆分，合并原 F07/F08/F09）](../storage/F07-storage-manager-domain-store-split.md)：本文的三条检索
  pipeline 语义不变，载体从统一 `Storage` 拆到 `DomainStore`（recall/recall_and_get/retrieve/
  preferred_retrieval_pipeline）；Retriever 生产装配经 `StoreManagerProducer.resolve` 取全局
  manager 并持其 `domain_store()`，首选路径可为每套命名数据面单独声明（`domain_stores` 段）。
