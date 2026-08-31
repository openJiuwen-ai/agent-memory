# F07 — 记忆写入入口收敛到 IndexBuilder

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-24 |
| 影响范围 | `jiuwen_memory/construction/index_builder*`、`construction/evolver_impl/`、`control/engine_impl/`、`control/jobs_impl/middle_to_long_job.py`、`control/lifecycle_impl/kv_lifecycle_manager.py`、`storage/types.py`、`storage/storage.py`、`storage/storage_impl/composite_storage.py`、`config/routing.py`、`config/defaults.py`；`docs/specs/S03-control.md`、`S05-construction.md`、`S06-storage.md` |
| 测试基线 | `pytest -m unit` 通过（14 项失败为既存环境问题：WSL python 缺 `cryptography` 包 12 项 + entity_linker 2 项，与本次改动无关）；`pytest -m integration` 全绿；`ruff check`（0.15.17）改动文件零新增告警 |
| Refs | —（issue 号待补） |

---

## 背景

记忆写入此前需要调用方连续调两个组件：先 `Storage.add`/`update` 落记忆本体，再
`IndexBuilder.build`/`update` 建派生索引。全项目共 19 处这样的双写，分布在
`orchestrating_evolver`（6）、`dynamic_evolver`（4）、`in_memory_engine`、`cloud_engine`。

这一形态有三个问题：

1. **底层存储拓扑经调用顺序泄漏到业务层**。调用方必须知道「写记忆 = 落本体 + 建索引」
   这个二段式事实。接入一体化存储平台（`add` 一次即建立全部索引）时，这些调用点全部
   需要改写。
2. **正排索引没有承载者**。倒排有 `FulltextIndexBuilder`、向量有 `VectorIndexBuilder`，
   而正排（记忆本体）却是调用方直接调 `Storage.add` 落的——同为索引形式，待遇不一致。
3. **`IndexBuilder.remove` 语义双关**。8 处调用中，3 处表达「彻底删除」（PURGE /
   purge_space），5 处表达「退出检索、记忆本体保留」（归档 / 遗忘 / 跨 pipeline 迁移）。
   两种相反意图共用一个方法，靠「实现恰好不删本体」维持正确，是巧合而非契约。

`_persist_and_maintain_messages` 另有一处独立问题：原文（`/messages/`）既非 MemoryUnit
真源也非索引，却和记忆本体挤在同一个 KV 端口上，靠 key 前缀区分。

---

## 决策

### 一、正排是一种索引形式，由 `ForwardIndexBuilder` 承载

新增 `ForwardIndexBuilder`：从注入的 `Storage` 取 `storage.kv` 端口，自己完成
`MemoryUnit → KV 记录` 的投影（`memory_key` 定 key、`memory_codec` 定字节）。与
`FulltextIndexBuilder` 写 FulltextStore、`VectorIndexBuilder` 写 VectorStore 同构。

端口在**构造期**解析（`self._kv = storage.kv`）：Storage 无 KV 能力时立即抛
`UnsupportedStorageCapabilityError`，而不是拖到首次写入才以 `AttributeError` 暴露——
正排是真源，缺了它整条读路径都无从谈起。

### 二、`HybridIndexBuilder` 退化为纯编排

```
HybridIndexBuilder(storage, chunker, embedder, ...)
├─ ForwardIndexBuilder(storage)   → storage.kv        正排
├─ FulltextIndexBuilder(storage)  → storage.fulltext  倒排
├─ VectorIndexBuilder(storage)    → storage.vector    向量
└─ EntityIndexBuilder(linker)     → entity_store      实体反向
```

一个子 builder 只负责一种索引形式，端口一律从**同一个** `Storage` 取。这带来一条重要
性质：写侧子 builder 与读侧 recaller 取自同一个 Storage 实例的同一端口，**读写不分叉**。
`KeywordRecaller.__init__` 早就是 `storage.fulltext_port(name)` 这个写法，本次只是让写侧
补齐了同一约定。

Hybrid 自身不再调用任何 Store，也不再调 `Storage.add`/`update`/`delete`。

### 三、记忆写入只经 IndexBuilder

19 处双写全部塌缩为单次调用。两个 engine 的 `Storage` 写调用归零；`Storage.add/update/
delete` 的剩余调用方收窄为 `UnifiedIndexBuilder`（转发给一体化后端）与
`KVLifecycleManager`（状态回写，全部带 `mode=IndexWriteMode.FORWARD_ONLY`）。

### 四、写删意图用语义化枚举表达

写接口（`build`/`update`）用 `IndexWriteMode` 表达写入范围，删除接口（`remove`）用
`IndexRemoveMode` 表达删除语义，两枚举归口 `jiuwen_memory/storage/types.py`：

```python
class IndexWriteMode(str, Enum):
    ALL = "all"                    # 正排（记忆本体）+ 全部检索索引均写入
    FORWARD_ONLY = "forward_only"  # 仅更新正排索引（记忆本体），检索索引不动
    RETRIEVAL_ONLY = "retrieval_only"  # 仅更新检索索引，记忆本体已存在/不动

class IndexRemoveMode(str, Enum):
    SOFT = "soft"  # 软删除：退出检索（search/recall 不可召回），本体保留，get/list 仍可读
    HARD = "hard"  # 硬删除：检索索引 + 记忆本体一并物理删除
```

三值全集对 build/update 都合法，不做「每个方法只暴露一半开关」的冗余裁减——枚举的
自解释性使对称组合（如 `build(FORWARD_ONLY)`、`update(RETRIEVAL_ONLY)`）语义自然成立。
`HybridIndexBuilder` 把 mode 透传给四个子 builder，各子 builder 自行跳过不属于自己的
索引形式（正排在 `RETRIEVAL_ONLY` 跳过、检索子 builder 在 `FORWARD_ONLY` 跳过；
remove 时检索子 builder SOFT/HARD 行为相同、正排 SOFT 保留本体）。

枚举同样下沉到 `Storage.add/update/delete`，`UnifiedIndexBuilder` 原样透传——能否拆分
由该 Storage 实现按自身能力决定，`UnifiedIndexBuilder` 不代它判断；不具备检索索引能力
的实现（如 `CompositeStorage`）在 `RETRIEVAL_ONLY` / `SOFT` 时为空操作。

`IndexRemoveMode` 与 control 层面向用户的 `DeleteMode`（FORGET/ARCHIVE/DOWNWEIGHT/
PURGE）分层：后者是治理语义（决定生命周期状态如何流转），前者是索引/存储层的物理
动作。引擎的非破坏式路径（ARCHIVE/FORGET）= Lifecycle 状态流转 + `remove(mode=SOFT)`；
PURGE = `remove(mode=HARD)`。

### 五、状态判断归上层，IndexBuilder 不解读 `lifecycle`

记忆处于什么状态、因而该对索引做什么，由调用方判定后调对应方法。如遗忘：

```python
for u in targets:
    u.lifecycle = LifecycleState.FORGOTTEN
self._index.update(targets, mode=IndexWriteMode.FORWARD_ONLY)  # 只回写本体新状态
self._index.remove(targets, mode=IndexRemoveMode.SOFT)         # 检索索引移出检索
```

两条指令互不重叠，检索索引不会被先重建再删除。

连带修复：`FulltextIndexBuilder.update` 从 `store.update`（要求文档已存在）改为
**删后重建**（`delete` 契约幂等 + `insert`），与 `VectorIndexBuilder`（删旧 + `self.build()`）
容错水平对齐。不改的话，归档已删除倒排文档，此后再更新该记忆会抛 `NotFoundError`。

### 六、顺序约定：正排最先出现、最后消失

| 方法 | 顺序 | 理由 |
|---|---|---|
| `build` / `update` | 正排在前 | 派生写失败 → 本体在，`list`/`get` 可读；目标是索引可重建，**当前 rebuild 尚未实现，不能宣称已恢复** |
| `remove` | 派生在前、正排最后 | 正排先删 → 孤儿派生索引条目，而删除路径的扫描源正是正排，**再也清不掉** |

同一规律推出两处引擎侧调整：

- **SUPERSEDE**：`build([new])` 移到 `lifecycle.supersede(old)` 之前。反过来的话，若
  `build` 失败，旧版已 SUPERSEDED 而新版尚未存在，这条记忆既退出活跃召回又无新版可读。
- **跨 pipeline OVERWRITE**：由「`remove` 删本体 + `build` 重建」改为
  `update(mode=FORWARD_ONLY)` + `remove(mode=SOFT)` + `build(mode=RETRIEVAL_ONLY)`，
  本体全程在位、只更新不删建。

### 七、原文不是索引形式，走独立的 KVStore 依赖

原文（`/messages/`）不建索引、不参与检索，仅供抽取时做指代消解与语境补全，条数上限由
evolver 维护。它与索引构建是两件事，故**不经 IndexBuilder**，也不占 `Storage` 的领域接口——
evolver 注入一个独立的 `message_store: KVStore` 直接读写。

装配上缺省复用 `kv_store.default` 具名实例（与正排 KV 同实例，`/messages/` 与 `/memory/`
靠 key 前缀分离），声明另一个 `kv_store` 具名实例即可物理拆开。

取解析必须用 `_resolve_message_store` 而非 `KvProducer.dep(..., default="memory")`：后者的
`default` 分支是**匿名新建**，会造出与配置后端无关的进程内 KV。而字段缺失是常态——
`AssemblyContext.merged` 按具名实例整体覆盖，用户一旦声明 `evolver.default`（如
`examples/config_template.yml` 里启用 dynamic 的写法），内置 params 里的 `message_store`
就随之消失。兜底逻辑与 `StorageProducer.resolve` 同款：两者都是**有状态**依赖，新建一个
等于换后端。

---

## 拒绝的方案

### 1. `Storage.add_covers()` 运行时能力协商

让 `Storage` 声明「`add` 已覆盖哪些索引形式」，`IndexBuilder` 按差集补齐。

**拒绝原因**：为解决装配匹配问题而增加接口硬约束。装配期约定（组合裸 Store 配 `hybrid`、
一体化后端配 `unified`）已能表达同一件事，配错组合的风险由联调定位承担。

### 2. `Storage` 增加 `add_messages`/`list_messages`/`delete_messages` 领域接口

曾经落地过一版：把原文读写收进 `Storage`，由 `CompositeStorage` 实现。

**拒绝原因**：原文既非 MemoryUnit 真源也非索引，把它塞进 `Storage` 的领域接口，等于承认
它是一种存储领域概念。它其实只是 evolver 的一项私有状态，用一个独立注入的 `KVStore`
表达更准确，也让 evolver 不必为原文而依赖 `Storage` 抽象。

### 3. `HybridIndexBuilder.update` 按 `unit.lifecycle` 分流派生索引

曾经落地过一版：`_UNINDEXED_LIFECYCLES = {FORGOTTEN, ARCHIVED}`，`update` 内部按状态决定
派生索引是更新还是移除，使派生索引成为记忆状态的函数。

**拒绝原因**：把状态语义放进了构建算子。IndexBuilder 该只执行被要求的索引操作，"记忆处于
什么状态、因而该做什么"属于上层判断。且该分流会遮蔽 `FulltextIndexBuilder.update` 非幂等
这个真实缺陷——撤掉分流后必须正面修复它（见决策五）。

### 4. 写删意图保留布尔开关（`include_forward` / `only_forward`）

**拒绝原因**：布尔不表意——`remove(include_forward=False)` 读不出「软删除」语义；且三个
方法各暴露一半开关、命名不对称，调用方要背一张「哪个布尔在哪个方法上是什么意思」的表。
枚举的自解释性消除了当初「不做对称冗余」的顾虑。

### 5. 枚举取值用物理命名 `FORWARD` / `DERIVED`

**拒绝原因**：「正排/派生」是实现侧术语。接口参数应表达调用方的逻辑意图——仅检索索引、
仅正排本体、全部，故定为 `RETRIEVAL_ONLY` / `FORWARD_ONLY` / `ALL`。

### 6. `remove` 复用 `IndexWriteMode`（`RETRIEVAL_ONLY` 即软删）

**拒绝原因**：删除是独立的语义轴——软/硬决定本体去留，与写入范围不是同一维度。混用同一
枚举会让 `remove(mode=FORWARD_ONLY)` 这类无意义组合合法化。

### 7. 枚举归口 `common.type_def`

**拒绝原因**：`memory_key`/`memory_codec` 归口 common 是因为读写两侧分居两层、无共同下层
可放；本枚举的两个消费层（construction/storage）有共同下层 storage，归口
`storage/types.py` 即可，不污染全局命名空间。

### 8. 让 `remove` 无条件删除记忆本体，归档路径改用其他方式

**拒绝原因**：归档 / 遗忘 / 跨 pipeline 迁移共 4 处依赖「本体保留」，若 `remove` 无条件删
本体，这些路径将静默丢数据。

### 9. `remove` 的去留由 `lifecycle` 推导，取消 `mode` 参数

分析过：软删除调用点的 `lifecycle` 恰好全落在 `{ARCHIVED, FORGOTTEN}`，硬删除的全不在，
看上去参数完全冗余。

**拒绝原因**：PURGE **不按 lifecycle 过滤**——对一条已归档的记忆执行合规硬删除时，其
lifecycle 是 ARCHIVED，纯状态推导会判「保留本体」，合规删除失效。`lifecycle` 表达的是
记忆的状态，删不删本体表达的是**本次操作的性质**，两者不是一回事。

### 10. engine 与 job 的治理路径改用 `update` 统一表达

**拒绝原因**：`lifecycle.transition` 已完成本体状态变更（含状态机校验），再调 `update`
会重复写入本体。这三处保留 `remove(mode=SOFT)`——本体由专职组件处理，
IndexBuilder 只补检索索引那一半。

---

## 验证

行为基线（端到端逐条实测 forward / lifecycle / fulltext / vector 四项状态）：

| 场景 | 记忆本体 | 派生索引 |
|---|---|---|
| ARCHIVE | 保留，lifecycle=archived | 移除 |
| FORGET | 保留，lifecycle=forgotten | 移除 |
| PURGE / purge_space | 物理删除 | 移除 |
| DOWNWEIGHT | 保留，lifecycle=active | 保留 |
| SUPERSEDE | 旧版保留，lifecycle=superseded | 保留（支持 `as_of` 回溯） |
| 归档后再更新 | 更新成功 | 保持移除 |
| evolver FORGET | 保留，lifecycle=forgotten | 移除 |
| 原文 FIFO | 写 13 条保留最新 10 条 | 不适用（原文不建索引） |

失败语义（注入必抛的子步骤实测）：

- SUPERSEDE 的 `build([new])` 失败 → 旧版仍为 `active`、内容可读，重试即可
- `build` 中检索索引失败 → 本体在，可用 `build(mode=RETRIEVAL_ONLY)` 补建检索索引
- `remove` 中检索索引失败 → 本体仍在，可重试补删

行为矩阵单测（`tests/unit/construction/test_index_builder.py` T-I-16 组）：

- `build(mode=RETRIEVAL_ONLY)` → 正排不写、检索索引可召回
- `build(mode=FORWARD_ONLY)` → 只交付本体、检索索引不写
- `update(mode=FORWARD_ONLY)` → 本体回写新内容、检索索引保持旧内容
- `remove(mode=SOFT)` → fulltext/vector 均不可召回，KV 本体保留、get/list 可读
- `remove(mode=HARD)` → 检索索引与本体一并物理删除
- `unified` 透传：SOFT 在 CompositeStorage 上为空操作（本体保留），HARD 删本体

装配（实测 `message_store` 与正排 KV 的实例关系）：

| 配置 | 结果 |
|---|---|
| 默认 | 复用 `kv_store.default` |
| 只写 `evolver.default.target=dynamic` | 复用 `kv_store.default` |
| `evolver` 带 params 但未写 `message_store` | 复用 `kv_store.default` |

---

## 已知遗留

1. **顶层 target 选 `fulltext` / `vector` 时记忆本体不落盘**。这两个实现只建派生索引，却
   注册为可被 `constructor.default.target` 选中的顶层实现，此时 `add` 返回成功但
   `list`/`get` 读不到。已决定不加装配期守卫，靠运行时报错暴露。
2. **`ib_default` 在非标准配置下会让组件拿到不同 IndexBuilder 实例**。
   `ib_default = "hybrid" if vector_on else "fulltext"` 在标准配置下是死代码（`defaults.py`
   恒声明 `index_builder`，`dep` 走引用分支）；但用户整体替换 `engine.default` 的 params
   且不写该字段时会复活，此时 engine 拿到匿名 `FulltextIndexBuilder` 而 evolver 仍是具名
   Hybrid，写入与去重检索的不是同一份索引。
3. **图索引仍在 evolver 内构建**。`_persist_graph` 直接调用 `storage.graph`，五个
   IndexBuilder 实现均不涉及图。按决策一，该逻辑应成为第五个子 builder。
4. **`KVLifecycleManager.sweep()` 不同步派生索引**。它只持有 `storage`、不持有 index，把
   单元转成 ARCHIVED/FORGOTTEN 后无人移除其派生索引。`sweep()` 目前无生产调用方，接线前
   必须补上。
5. **原文淘汰的容错等级下降**。整批一个 `try`，首条删除失败即中断剩余淘汰；失败告警报的是
   意图条数而非实际失败条数。影响有限：`/messages/` 短暂超出上限，下一轮取全量重排后自愈。
6. **`HybridIndexBuilder.remove_with_scope` 不删正排**，与 `remove` 语义不一致；该方法目前
   无生产调用方。
7. **原文侧持有裸 `KVStore` 而非 `_AuthorizedStoreProxy`**，`/messages/` 读写绕过
   `StorageSecurity`；正排写入的鉴权 resource 也由 `memory_unit` 变为 `kv`。当前仓库只有
   `AllowAllStorageSecurity`，暂无实际影响。
