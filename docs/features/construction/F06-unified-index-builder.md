# F06 — 统一存储直写 IndexBuilder（含向量化下传与 _index_ops 抽取）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-31 |
| 影响范围 | `jiuwen_memory/construction/index_builder_impl/`（新增 `_index_ops.py`，重构 fulltext / vector / unified）、`jiuwen_memory/common/type_def/`（memory.py / memory_codec.py / `__init__.py`）、`tests/unit/construction/test_index_builder.py`、`tests/unit/common/test_memory_codec.py`、`jiuwen_memory/construction/AGENTS.md`、`docs/specs/S05-construction.md`、`docs/specs/S07-common.md` |
| 测试基线 | `pytest tests/unit/construction/test_index_builder.py tests/unit/common/test_memory_codec.py`（含 unified 向量化新增 5 项 + codec vector 往返 1 项）；`pytest tests/unit/construction/ tests/unit/common/ tests/unit/retrieval/ tests/unit/control/`（963 passed；2 项 entity_linker 失败为 HEAD 已有测试间干扰，2 项 bge_m3 失败为本地环境缺 torch）；改动文件 `ruff check` 通过 |

## 背景

现有 `IndexBuilder` 实现都把 `MemoryUnit` 投影为向量、全文或两者组合的派生索引。统一存储装配需要一个不依赖 Chunker、Embedder 或具体索引 Store 的实现，将构建生命周期直接委托给 `Storage` 的记忆单元写接口。

F06 初版（2026-08-12）落地的 `UnifiedIndexBuilder` 把 build/update/remove 全权委托给 `Storage` 领域写接口，假定注入的是「一体化后端」（内部自建全部索引）。两个遗留问题：

1. 一体化后端要自建向量索引就必须自己向量化，但 Embedder 是构建/检索两侧必须共用的
   共享插件（S07 铁律：同实例才同向量空间），Storage 层不持有插件——向量化只能发生在
   构建侧，结果需要一条随 `Storage.add/update` 下传的通道，而 `MemoryUnit` 没有该字段。
2. fulltext 与 vector 两个子 builder 各自重复维护一份近乎相同的 `_index_metadata`
   投影、scope 分组与端口解析逻辑。

## 决策

新增注册名为 `unified` 的 `UnifiedIndexBuilder`。它将输入按 `Scope` 分组后：

- `build` 调用 `Storage.add(scope, units, mode=mode)`；
- `update` 调用 `Storage.update(scope, units, mode=mode)`；
- `remove` 调用 `Storage.delete(scope, unit_ids, mode=mode)`；
- `rebuild` 返回 `None`，与现有最小实现的重建语义一致。

按 Scope 分组是必要条件：`Storage` 的写接口要求显式 scope，且会校验每个 `MemoryUnit.scope`
与该参数一致。`mode` 原样透传——能否只补建检索索引（`RETRIEVAL_ONLY`）或只回写本体
（`FORWARD_ONLY`）由该 Storage 实现按能力决定，本类不代它判断（CompositeStorage 下
`RETRIEVAL_ONLY`/`SOFT` 为空操作）。

构建侧仍由本类完成两件事：

1. **`MemoryUnit` 新增 `vectors: list[ChunkVector]` 字段**作为向量下传通道：`ChunkVector`
   携带 `id`/`seq`（与 Chunker 产出的 Chunk 对齐）与 `vector`，构建期由 IndexBuilder
   填充，随本体经 `Storage.add/update` 交给实现；一体化 Storage 消费它自建 chunk 级
   向量索引（record id 沿用 `{unit_id}-{chunk_id}` 约定），`CompositeStorage` 仅随本体
   持久化。codec 按既有「加字段兼容演进」约定序列化（`dumps` 恒写、`loads` 缺省空列表），
   `_v` 不升。
2. **unified 全部写只经 Storage 领域接口**：不触碰 `storage.kv`/`vector`/`fulltext`
   等任何底层端口，`mode` 原样透传，覆盖范围由该实现按能力决定（CompositeStorage 下
   `RETRIEVAL_ONLY`/`SOFT` 为空操作）。本类只做两件构建工作：(a) `vector_enabled=True` 时
   走与 `VectorIndexBuilder` 完全相同的管线——`Chunker.chunk` 切片 → 共享 Embedder
   逐 chunk embed（单元管线抽取为 `_index_ops.vectorize_unit`，两个 builder 共用），
   结果回填 `unit.vectors`；单 unit embed 失败不阻断本体写入（该 unit `vectors` 留空，
   与 VectorIndexBuilder 跳过该 unit 的容错水平一致）。
   `vector_enabled=True` 但缺 chunker/embedder 时构造即抛 `ValueError`（装配期暴露）。
   (b) 把 `FulltextIndexBuilder`/`VectorIndexBuilder` 经 `_index_ops.index_metadata`
   单独投影的索引过滤字段直接补进 `unit.system_metadata`——`content_layer`（恒 `"l2"`，
   L0/L1 分层记录由后端按需覆写）、`t_event`（epoch 毫秒，None 落哨兵 `T_EVENT_UNKNOWN`，
   恒写）、`t_valid`（epoch 毫秒，None 不写）、`t_invalid`（epoch 毫秒，None 落哨兵
   `T_INVALID_OPEN`，恒写）。其余过滤字段（`unit_id`/`tier`/`lifecycle`/`tags`/
   `entities`/`source`）已在 unit 顶层、后端直接读；`seq` 已在 `ChunkVector` 上、
   per-chunk 不重复。哨兵与 `memory_filter._field_value` 投影对称，后置复核与下推不分叉。
   一体化后端从 `system_metadata`/`user_metadata` 直接读取建索引，无需 `index_metadata`
   投影下传（也不再把 storage→construction 的反向依赖引入）。
3. **公共逻辑抽取为 `_index_ops.py`**：metadata 投影（合并 fulltext/vector 两份
   `_index_metadata` 的并集，`seq` 可选；vector 投影自此多写 `entities` 字段，纯增量）、
   scope 分组、端口解析、全文文档构造、向量切片-embed-写库流水线（`vectorize_unit` /
   `vectorize_units` / `write_vector_index` / chunk 跟踪读写）、L0/L1 分层建删。
   fulltext / vector 两个 builder 改为调用共享函数，行为不变；unified 复用其
   scope 分组与 `vectorize_unit`，过滤投影走 `system_metadata` 补齐而非 `index_metadata`。

## 拒绝的方案

- 逐条调用 Storage：能够满足接口，但放弃同 Scope 批量写能力，也与其他批量 Builder 的行为不一致。
- 让 unified 组合既有 HybridIndexBuilder：这会引入 Chunker、Embedder 和索引 Store 依赖，违背统一存储直写模式的目标。
- 在 `IndexBuilder` 抽象接口中新增 Storage 专用方法：四个既有生命周期方法已足以表达需求，扩展接口会扩大所有 Builder 的适配面。
- **unified 经底层端口自建全文/向量检索索引**（向量化下传第一稿）：违背了「统一存储直写」
  模式的初衷——索引形式应由 Storage 实现按自身能力落地，builder 经端口直写会让
  unified 退化为 hybrid 的平行编排层，且在一体化后端上会与其内部索引重复构建。
- **向量挂 `system_metadata` 约定 key**：`MetadataValueType` 不允许 `list[float]`，
  塞进去会撑破 metadata 的过滤语义与双命名空间契约；新增独立字段更干净。
- **content 整段单向量（`vector: list[float] | None`，向量化下传中间稿）**：与
  VectorIndexBuilder 的 chunk 级管线不一致——同一内容两条向量化路径会产生不同粒度
  的向量，一体化后端拿到的投影与组合后端的向量索引不可对账；改为 chunk 级
  `ChunkVector` 列表，与 vector builder 完全同管线。
- **unified 接 entity 索引**：本次未要求，保持 lean。

## 验证

- `pytest tests/unit/construction/test_index_builder.py`：覆盖跨 Scope 的 build、update、remove、
  `unified` 工厂装配，以及向量化下传新增 5 项（向量化随本体 codec 往返保留、
  `vector_enabled=False` 不向量化、embed 失败不阻断本体写入、update 重新向量化、
  缺 embedder 装配期报错）；工厂按 `globals.vector_enabled` 两种形态装配。
- `pytest tests/unit/common/test_memory_codec.py`：codec vector 往返新增 1 项通过。
- `pytest tests/unit/construction/ tests/unit/common/ tests/unit/retrieval/ tests/unit/control/`：
  963 passed；`test_entity_linker.py` 2 项失败在干净 HEAD 检出上同样复现（既有测试间干扰），
  `test_bge_m3_embedder.py` 2 项失败为本地 venv 未装 torch，均与本次改动无关。
- `ruff check` 本次改动的全部文件通过。

## 已知遗留

- `rebuild()` 仍为 no-op，与全部既有实现一致（S05 不变量 2 的目标契约缺口未补）。
- 一体化 Storage 实现尚未存在（storage_impl 仅 CompositeStorage）：`MemoryUnit.vectors`
  目前只随本体落盘，暂无消费方；首个一体化实现落地时应消费该字段、按
  `{unit_id}-{chunk_id}` 约定自建 chunk 级向量索引，并从 `system_metadata` 读取
  补齐的过滤字段（`content_layer`/`t_event`/`t_invalid`/`t_valid`）建索引 metadata。
- 补齐的过滤字段随本体经 `dumps` 持久化进 KV（`system_metadata` 非 TRANSIENT）。
  这使 `system_metadata` 混入派生投影——派生单元经 Extractor 抽取时，S05 铁律 0 的
  `system_metadata` 等值交集合并会把源 unit 的 `t_event=0` 等哨兵带进派生 unit，
  与派生 unit 自身 `temporal` 派生的哨兵不一致。当前由 Evolver 在派生时按
  派生 unit 的 `temporal` 重算覆盖；若后续 Evolver 路径不重算，需在补齐前
  区分「源 system_metadata」与「索引投影」或补齐改为 transient 不落盘。
- unified 不建立派生检索索引（`CompositeStorage` 无投影能力）。其检索能力取决于所注入
  `Storage` 自身支持的 recall/retrieve 管线，不由此 Builder 提供。
- engine 链路冲突仍未解：InMemoryEngine/CloudEngine 的标准写链路会先调 Storage 写接口
  再调 IndexBuilder，直接换 `unified` 会重复写本体；启用需 control 路由配合。
- unified 不接 entity 索引；L0/L1 分层文本不向量化（`ChunkVector` 只承载 content
  chunks，分层向量的 record id 约定与 metadata 投影不同，需要时另行扩展）。
