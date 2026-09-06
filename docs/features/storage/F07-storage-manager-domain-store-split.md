# Storage 双面拆分（StoreManager/DomainStore）、全局唯一 manager、控制面直连 KV 与 EntityStore 纳管

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-06 |
| 影响范围 | `jiuwen_memory/storage/`（`base.py` / `store_manager.py` / `domain_store.py` / `kv.py` / `store_manager_impl/` / `domain_store_impl/` / `entity_impl/`；删 `storage.py` 与 `storage_impl/`），`jiuwen_memory/config/`（`routing.py` / `defaults.py` / `keys.py`），`jiuwen_memory/retrieval/`、`jiuwen_memory/construction/`、`jiuwen_memory/control/`、`jiuwen_memory/api/` 全部消费者，`deploy/docker/{local,online}/config.yml`，`docs/specs/S02`–`S06`/`S08`，`docs/features/retrieval/F06-entity-recall-channel.md`，各 `jiuwen_memory/<subdir>/AGENTS.md`，`docs/design/architecture.md`；E 组另涉 `jiuwen_memory/retrieval/`（`recaller.py` 与 `recaller_impl/` 整体迁出、`base.py` 删 `RECALLER` 枚举值）与 `docs/zh|en/API文档/{retrieval,storage}.md` |
| 测试基线 | `pytest tests/unit/` 1978 passed / 8 skipped；`pytest tests/integration/` 48 passed / 61 skipped；`ruff check jiuwen_memory/ tests/` 165（低于改动前基线 169，详见「验证」） |
| Refs | — |

本文覆盖同一条主线（**运行期持最小接口** + **所有 XXXStore 获取经 StoreManager**
+ **每个组件的配置住在它自己那一层**）上的五组连续变动，按 A/B/C/D/E 分组编号；
正文内的「A-10」「B-6」这类记法指本文对应分组下的第 N 条决策。

## 背景

**A. 双面拆分**。`Storage` ABC 落地（F05-unified-storage-design）后一身二任：

- **管理面（端口管理）**：`capabilities()` / `has_*()` / `kv`·`vector`·`fulltext`·`graph`·`fusion`·`fs` 端口 / `security` / `health()`
- **数据面（领域操作）**：`add` / `update` / `delete` / `get` / `list` / `scopes` / `recall` / `recall_and_get` / `retrieve` / `preferred_retrieval_pipeline` / `bind_recallers`

两类职责混在同一 ABC 里，上游依赖 `Storage` 类型时同时耦合端口管理与领域操作——只需要"取 KV 写本体"的 `ForwardIndexBuilder` 被迫持有 `recall/retrieve`；只需要"调 recall 走首选路径"的 `PipelineRetriever` 被迫持有 `kv_port` 等管理面接口。职责边界模糊让"谁该依赖什么"无法在类型层面表达，只能靠调用方自觉。

**B. 全局唯一 manager**。拆分落地过程中暴露三个结构性问题：

1. **manager 选择分散**：15 处消费者 `params.storage: default` 引用逐个声明所用 manager，同一配置里写漏/写错一处就出现第二套 manager 实例（有状态依赖，新建等于换后端）；`StoreManagerProducer.resolve` 还带匿名兜底构建分支，错误配置被静默吞掉。
2. **端口/数据面无法具名选择**：端口方法 `kv(name)` 签名上有 name，但配置装配只认 `layers_l0/l1` 两个硬编码名，`kv/graph/fusion/fs` 端口完全没有装配路径；`domain_store()` 是唯一无 name 的获取口（单槽）。
3. **纯点读场景过度注入**：`Dedup._load_unit`、`Governor._find`、Schema Evolver 源读、`KeywordRecaller` 实体扩展只做「按 unit_id 点读」，却注入了整个 `DomainStore`（或整个 manager）——类型层面无法表达「运行期只需要的最小接口」。

**C. 控制面直连 KV**。拆分后 control 侧七个消费者注入了 `DomainStore`：两个 Engine、`list_support`、`KVLifecycleManager`、`EvolveJob`/`MiddleToLongJob` 两个 Job Spec、以及 `PipelineRetriever`。逐一审计实际调用方法：

- **Engine×2 / list_support / 两个 Job**：只用 `get`/`list`/`scopes`——零检索适配、零领域写；
- **KVLifecycleManager**：`get`/`list`/`scopes` + `update(mode=FORWARD_ONLY)`——唯一的写调用，而 `CompositeDomainStore.update` 对 `FORWARD_ONLY` 与 `ALL` 行为相同（无投影能力，只落本体）；
- **PipelineRetriever / UnifiedIndexBuilder**：`recall`/`retrieve`/`preferred_retrieval_pipeline` 与带 `mode` 的领域写——DomainStore 的本职消费方。

即 control 侧 6 个文件 5 个类持有的 `DomainStore` 引用实际只用了 KV 等价能力——`CompositeDomainStore` 的 `get`/`list`/`scopes`/`update` 本身就是 `manager._stores[KV]` 的薄包装（loads/dumps + `memory_key`），DomainStore 在这条链路上是纯间接层。B-6 已为纯点读场景立了「运行期持最小接口」的先例，C 组把同一原则推进到 control 面。

**D. EntityStore 纳入 manager**。A–C 三组确立的「所有 XXXStore 获取经 StoreManager」（S06 不变量 30）落地时漏掉了 `EntityStore`——A/B/C 的改造对象限定在原 `Storage` ABC 谱系内，而 EntityStore 自诞生起就走独立的 `EntityStoreProducer` + `entity_impl/`，从未进入该谱系，于是被整个绕开：前三组的决策、S06 的接口契约段落、AGENTS.md 的铁律条款均未提及它。留下的是三处**既有规约的存量违例**：

| 规约 | EntityStore 的状态 |
|---|---|
| S06 不变量 30「所有 XXXStore 获取经 StoreManager」 | 写入侧 `HybridIndexBuilder._build` 与召回侧 `KeywordRecaller._build` 各自 `EntityStoreProducer.dep` |
| AGENTS.md 本地约束 1「所有 Store 必须实现 `store_type()`」 | `ElasticsearchEntityStore.store_type()` 直接 `return None` |
| AGENTS.md 本地约束 12「Construction/Retrieval/Control 不得直接调 Store Producer」 | 两个消费方恰好分居 construction 与 retrieval |

违例的实际代价：`Factory.dep` 在 `params.entity_store` 缺省时走 `cls.build(default, {}, ctx)`——**匿名新建、不入缓存、params 为空**，读写两侧因此各持一个独立 ES client；空 params 下 `hosts` 只能靠 globals 兜底，否则 builder 静默返 None、entity 链路无声关闭。"读写共享同一实例"完全靠"两端 params 各引用同一具名实例"的配置纪律维持（`deploy/docker/local/config.yml` 的注释已明写"缺一侧则该侧 disabled，不报错，静默降级"），而这正是铁律 4「隔离必须在存储层强制，上层不依赖调用纪律」要消灭的东西。此外 entity 后端故障对 `health()` 完全不可见——写侧 try/except 吞、召回侧 `_expand_by_entities` 吞、health 不看，三层静默，运维只能从召回质量倒推。

**E. Recaller 归属与数据面参数归位**。三条线索指向同一个结论——召回路与数据面的
装配参数都寄居在错误的位置。

其一，Recaller 早已是数据面的内部件却还住在 `retrieval/`。F06 把召回路装配内收到
manager 工厂、`PipelineRetriever` 不再持有 recaller 之后，生产链路里 Recaller 实例的
唯一消费方就是 `CompositeDomainStore`。位置没跟上带来两处味道：

```
composite_domain_store.py   from jiuwen_memory.retrieval.recaller import RecallerProducer   ← 函数内延迟导入，绕 storage→retrieval→storage 循环
composite_store_manager.py  from ...composite_domain_store import _assemble_recallers      ← 跨模块导入私有函数
```

而 Recaller 对检索层的真实依赖只有 `RetrievalOperator` 一个基类——三个实现从
`retrieval.types` 取的 `ParsedQuery` / `RecallChannel` / `ScoredUnit` 全是
`common.type_def` 的再导出。

其二，D-6 全量聚合落地后，`store_manager.<inst>.params` 里**一个 manager 自己的键都
没有了**。`composite_store_manager.py` 全文只读 `domain_store_target` /
`preferred_retrieval_pipeline` / `kv_store` / `domain_stores` / 经 overlay 传给
`_assemble_recallers` 的 `*_recaller`——全是数据面的；七类端口由 `_collect_ports` 扫命名
空间得来，与 params 无关。`defaults.py` 里的 `vector_store` / `fulltext_store` /
`graph_store` 三个键因此成了零读者的死键（真正的读者都在各消费方自己的 params 里）。

其三，参数寄居直接导致继承语义分三套。`preferred_retrieval_pipeline` 是唯一不继承
manager params 的键，且默认数据面与命名数据面对 params/globals 的读法**正好相反**：

| 声明位置 | 默认数据面 | 命名数据面 |
|---|---|---|
| `domain_stores.<name>` entry | 不适用 | ✓ |
| `store_manager.<inst>.params` | ✓ | ✗ 丢失 |
| `globals` | ✗ 忽略（被 `in config.params` guard 挡掉） | ✓ |

`defaults.py` 的值与 Producer 兜底恰好同为 `recall_get_rank`，默认配置下不可见；且
`domain_stores` 段**全仓零测试覆盖**，所以从未暴露。同一文件里 `_build_domain_store` 的
非 composite 分支专门写注释处理了「数据面新造的 config 读不到 manager params」这个坑，
`_named_domain_stores` 漏了——是漏写不是设计。

## 决策

### A. 双面拆分

1. **删 `Storage` ABC，拆为两个独立 ABC（分处不同文件）**：`store_manager.py` 的 `StoreManager`（管理面 ABC）+ `StoreManagerProducer` + `StorageCapability`；`domain_store.py` 的 `DomainStore`（数据面 ABC）+ `DomainStoreProducer`。
2. **端口接口统一为单一入口（消除双写法）**：原 F05 的 property 快捷方式与 `*_port(name)` 后缀接口功能重复（24 个端口成员里一半是另一半的特例）。统一为每 capability 一对带 name 参数的短名方法：`kv(name="default")` / `has_kv(name="default")`（其余五类同理），删 property 与 `*_port`/`has_*_port` 名。`RoutingStoreManager` 按 `(capability, name)` 缓存 `_LazyStorePort`，同键代理身份稳定。
3. **`CompositeStorage` 拆为两个独立实现类（impl 目录同步拆分）**：`CompositeStoreManager` 持六类 Store + capabilities + security + 授权代理表，实现端口方法/健康聚合，`domain_store()` 返回已绑定实例；`CompositeDomainStore` 构造注入 `manager` + `preferred_pipeline`，实现领域方法与首选路径，`security` 委托 manager。实现类随 ABC 改名（`CompositeStorageManager` → `CompositeStoreManager`；`RoutingStorageManager` → `RoutingStoreManager`）。
4. **`DomainStoreProducer` 支持 Factory 装配，但 manager 仍是唯一装配入口**：domain_store 不是平级 YAML 入口；装配链路为 manager `_build` → 预注册（打破循环依赖）→ `DomainStoreProducer.build(..., {"store_manager": <引用>, ...})` → domain builder 内 `StoreManagerProducer.dep` 回取 manager（命中预注册缓存）→ 构造 `CompositeDomainStore` → `bind_domain_store`。domain builder 的 `store_manager` 引用**必填**——独立构建会触发 manager 匿名重建的无限递归，缺引用 fail-fast；`preferred_retrieval_pipeline` 从 manager config 显式透传。
5. **召回路装配的内收设计保留**（沿用 F06）：`_assemble_recallers` 移至 `domain_store_impl`（recallers 是数据面资源），**调用时机留在 manager `_build` 末尾**（开关键在 globals/manager params，domain 的新造 config 读不全）；isinstance 守卫后 `bind_recallers`。
6. **去惰性物化**：装配期同步构建 recallers；两个实现类的构造函数均不接收 `recallers` 参数。
7. **`bind_recallers` 仅落 `CompositeDomainStore`，不下沉 `DomainStore` ABC**：`RoutingDomainStore` 不实现——active 切换语义要求各预装实例装配期各自绑定，对外只读委托。
8. **`RoutingStorage` 同步拆为两个独立类**：`RoutingStoreManager`（内部 `ActiveRouter[StoreManager]`，端口方法返回按 `(capability, name)` 缓存的惰性代理）+ `RoutingDomainStore`（每次方法调用委托当前 active 实例的 `domain_store()`；不实现 `bind_recallers`）。
9. **`PipelineRetriever` 只持有 `DomainStore`**：构造签名改 keyword-only 必填 `domain_store:`；`storage` property 返回类型改 `DomainStore`（名暂保留）；`_build` 工厂经 `StoreManagerProducer.resolve` 取 manager、取 `domain_store()`、装配期用 `manager.kv()` 构造 `UnitReader`，运行期只持数据面。
10. **Recaller 持 manager，点读走 `domain_store()`**：Vector/Graph/Keyword Recaller 装配期取端口；KeywordRecaller 运行期实体扩展点读走数据面接口。（**已被 B-6 修订**：点读改走 KV 端口 + `load_units`，Recaller 不再持 manager 字段。）
11. **上游消费者按职责面切分依赖**：管理面消费者（IndexBuilder*/Dedup*/KvSpaceManager/OrchestratingEvolver 等）持 `StoreManager`；数据面消费者（Engine×2/ListSupport/Jobs/Governor/Lifecycle/UnifiedIndexBuilder 等）持 `DomainStore`；装配层 `_Kernel.storage: StoreManager`，按面注入。

### B. 全局唯一 manager 与命名实例

1. **全局唯一 manager，`globals.store_manager` 指名**：配置顶层段 `storage:` 更名 `store_manager:`（`StoreManagerProducer.TOP_NAME = "store_manager"`）；globals 加 `"store_manager": "default"` 键。`StoreManagerProducer.resolve` 重写为三级链——params 显式覆盖 → `globals.store_manager` → `"default"`；**删除匿名兜底构建分支与 `default_target` 参数**，未声明实例名抛 `ValidationError`。defaults 清理 15 处消费者 `params.storage` 引用与 `ROOT_PARAMS.storage`。
2. **`domain_store(name)` 命名数据面（同 manager 多套）**：一个 manager 持 `dict[str, DomainStore]`，多套命名数据面**共享同一物理 Store 集**，差异仅在检索 profile。ABC 加 `domain_store(name="default")` + `has_domain_store(name)`；配置段 `store_manager.<inst>.params.domain_stores: {<name>: {覆盖键}}` 逐项构建，段内 `"default"` 键拒绝。`RoutingDomainStore(router, name)` per-name 惰性缓存（同名身份稳定 + active 跟随）。
3. **六类命名端口全量自动（声明即端口）**：`_named_ports` 从硬编码 `layers_l0/l1` 改为遍历六类 `*_store` 命名空间下所有非 default 名；`kv/graph/fusion/fs` 端口装配路径补齐。encrypted KV 的明文 raw 若以具名声明会随之暴露为端口——配置写法问题而非机制缺陷，约定 raw 推荐 inline 声明。
4. **消费者具名选择键（manager + name 构造形态）**：消费者构造函数保持收 `StoreManager`，追加 name 参数；`_build` 工厂经 `resolve_name(config, key)` 统一读取——params 直读**不回退 globals**（端口选择是实例级决策）、值必须是名字字符串（inline dict 拒绝）。数据面消费者工厂读 `params.domain_store` 键 → `manager.domain_store(name)`。
5. **Recaller 端口显式覆盖优先**：`vector_recaller`/`keyword_recaller`/`graph_recaller` 的 params 支持 `vector_store`/`fulltext_store`/`graph_store` 键，优先于 layer 推导（缺省 l2→default；l0/l1→`layers_l0/l1`）。
6. **四处纯点读切 KVStore（修订 A-10）**：`storage/kv.py` 新增模块函数 `load_units(kv, scope, unit_ids)`（`memory_key` + `get` + `loads`；缺失省略、保序、不去重、零过滤）。四处切换：Dedup 基类（`__init__(kv: KVStore)`）、`InMemoryGovernor._find`、SchemaOrchestratingEvolver 源读、KeywordRecaller 实体扩展（**删除其 manager 字段**——运行期持最小接口）。
7. **Recaller 端口可选（store None → recall 返空）**：KeywordRecaller 的 kv 端口与 GraphRecaller 的 graph 端口改为可选，与既有 store None 约定对齐。
8. **`_Kernel.kv` 与 ingest_job 任务 KV 统一走 manager 端口**：`_Kernel.kv = manager.kv(resolve_name(root, "kv_store"))`（`ROOT_PARAMS` 既有 `kv_store` 键复用为端口名）；与 `kv_store.default` 具名实例同源（外部注入 kv 经 `KvProducer.put` 预置缓存后 `dep` 命中同一实例）。

### C. 控制面真源读写直连 KV

1. **control 侧真源读写全部直连 KV 端口**：两个 Engine、`list_support`、`KVLifecycleManager`、`EvolveJob`/`MiddleToLongJob` 的构造参数 `domain_store: DomainStore` 统一改为 `kv: KVStore`。读：点读走 `load_units`、列表/分页走 `list_units`、跨 scope 枚举走 `kv.scopes()`；写（仅 lifecycle 的非破坏式回写）：`kv.update(scope, memory_key(unit.id), dumps(unit))`——即 `ForwardIndexBuilder` 的写侧模式，回写对象是正排本体本身，无检索索引需要拆分。
2. **`storage/kv.py` 新增 `list_units` helper**：`list_units(kv, scope, *, offset, limit, memory_types, filters, extensions) -> tuple[list[MemoryUnit], int]`——与 `load_units` 对称的列表读 helper：`kv.list` + 逐条 `loads`（非 MemoryUnit 记录自然过滤），返回 `(items, count)`。过滤/计数/分页语义全部由 `KVStore.list` 契约承担，helper 不做二次过滤。承载 Engine 全量扫描、`list_page` 分页、Lifecycle sweep、两个 Job 的候选拉取。
3. **装配键复用 `params.kv_store`**：五处 `_build`/Spec builder（cloud/in_memory 两个 Engine、evolve/middle 两个 Job Spec、lifecycle）从 `resolve(config).domain_store(resolve_name(config, "domain_store"))` 改为 `.kv(resolve_name(config, "kv_store"))`。`kv_store` 是 `ROOT_PARAMS` 既有键（B-8 已用），默认值 `"default"`——默认拓扑与既有配置零兼容影响。
4. **DomainStore 消费方收敛为两类**：检索路径（`PipelineRetriever` 持 `domain_store(name)`）与一体化写路径（`UnifiedIndexBuilder` 领域写 + `mode` 透传）。control 面不再持有 DomainStore 引用。
5. **删除两个 Engine 中的死代码 `_write_middle_to_kv` / `_write_default_to_kv`**：全库零调用点的历史遗留，且是 engine 内仅存的 `DomainStore.add` 写调用——与「记忆本体的写入一律经 IndexBuilder」铁律冲突的潜在入口，删除而非移植。

### D. EntityStore 纳入 manager 成为第七 capability

1. **完整第七能力席位，而非独立表**：`StorageCapability` 与 `StoreType` 各加 `ENTITY`，`StoreManager` 加 `entity(name)` 抽象方法 + `has_entity(name)` 默认实现（由 capability 集合推导，与既有六个逐字同构），纳入命名端口全量自动、授权代理、health 聚合。选完整席位而非仿 `domain_store` 的独立表：EntityStore 是货真价实的后端 Store（有 Producer、有 `*_impl/`、有连接参数与 SSL），与 kv/vector 同类；`domain_store` 是数据面编排对象，不是后端。
2. **冻结 `space_id: str` 首入参，代价由授权代理承担**：不把四个方法签名改成 `scope: Scope`。entity 索引的隔离维度（`space_id` routing + `actor_id` 单段 term，agent/session 不作维度）与 Scope 五段模型不同构，强行套用会丢掉 routing 语义；这是 BaseStore「scope 显式第一入参」的**唯一例外**，在 `base.py` docstring、S06 不变量 1 与 AGENTS.md 铁律 1 三处显式记录。
3. **独立的 `_AuthorizedEntityStoreProxy`，不在通用代理里加分支**：通用代理的 `args[0] is Scope` 假设是 BaseStore 的文档化不变量，让六个端口的每次属性访问为第七个的例外买单不划算；且 `_action_for_store_method(name)` 只收方法名，无法表达 `execute_operations` 按 op 类型派生——改签名要动六端口的调用点。
4. **action 映射按语义而非省事**：`find_by_entity_text_hash` / `find_by_linked_memory_id` → `SEARCH`；`execute_operations` → 按 batch 内 op 类型派生动作集逐个授权（`INSERT`→ADD、`LINK`/`UNLINK_UPDATE`→UPDATE、`DELETE`→DELETE），空 batch 零授权；`ensure_index` → `ADMIN`。不适配的话四个方法全落 ADMIN，写入链路要跑通就得授 ADMIN，而 ADMIN 同时解锁全部 Store 的所有未映射方法——实质性权限放大。
5. **授权 scope 是有损近似，并明确其边界**：代理交给 `authorize` 的是 `Scope(space=space_id, user=filters.actor_id)`。`space_id_from_scope` 是 space → org → 字面量 `"default"` 的三级降级，**无法无损反推**；`org`/`agent`/`session` 恒空；`execute_operations` 无 `filters` 参数故写入侧 `user` 也恒空。自定义 `StorageSecurity` 不得对 `resource == "entity"` 按 org/agent/session 判定，应把 `(space, user)` 当作不透明的 routing/actor 二元组。写入侧的 actor 隔离由 `EntityRecord.filters` 记录内字段承担，不由授权入参承担。
6. **端口全量聚合，取消 `<ns>_store` 引用键与 default/ports 二分**（七类统一规则，非 ENTITY 特例）：`_collect_ports` 收命名空间下**所有**实例（含 `default`），端口名即实例名；`default` 只是端口表中名为 `"default"` 的普通键，manager params 不再用引用键决定默认端口。构造入参也随之从 `kv=` + `kv_ports=` 两个合并为一个（接单实例或 `{name: store}` 表）。理由适用于全部七类：配置合并是**实例级整体覆盖**（`AssemblyContext.merged` 按实例名 update 整个 `RawSpec`，无 params 深合并），要求 params 引用等于强制既有部署全量抄写 `store_manager.default.params`（12 个键，含 7 个 `*_recaller`），漏抄一个即静默丢能力。**代价**：manager params 里 inline 声明 Store（`kv_store: {target: "memory"}`）的写法失效，后端一律经命名空间声明；给实例起的名字若不是 `"default"`，则该类无默认端口（消费方需经 `params.<ns>_store` 选择键指名）。
7. **capability 与 `has_*()` 同源于端口表**：改前 capability 由「default 端口是否存在」推导、`has_*()` 查端口表，只声明具名端口时会出现「`capabilities()` 说没有、`has_xxx(name)` 说有且端口可用」的矛盾（违反 S06 不变量 23 的字面表述）。现统一为「某类只要有任一端口即拥有该 capability」。KV 作为必需能力单独校验：命名空间一个实例都没有时装配期 fail-fast。
8. **`_collect_ports` 统一过滤 builder 返回的 None**：端口表的值非 None 是 `CompositeStoreManager` 的不变量（`health()` 直接 `store.security` / `store.health()`，代理也假定非 None）。增强层后端约定「必填连接参数未配即返 None 表示降级关闭」，在这个**唯一的**「命名空间 → 端口」构造点统一过滤，而不是把 None 判断散进 health 与代理。对其余六类是 no-op（它们缺必填参一律 `require_param` 抛错，不返 None）。
9. **manager 双入口，装配逻辑内收**：`__init__` 只收已构造实例（手工接线与 60+ 处测试路径不变），新增 `from_config(config)` 承担装配——依次全量聚合七类 Store → 构造 manager → 预注册打破循环 → 构造并注入默认数据面 → 命名数据面；注册的 `_build` 退化为一行封装。数据面构造顺序是刚性的：它持有 manager 引用，必须在 manager 自身就绪后才能构造。管理面**不校验 capability 必需性**：manager 只如实报告装配出的能力，「哪类存储不可或缺」是消费方的约束——数据面缺 KV 时其领域方法自会抛 `UnsupportedStorageCapabilityError` 且指明缺失端口，信息并不更差；保留校验反而让 `from_config` 与 `__init__` 行为分叉（后者本就允许构造无 KV 的 manager，分层索引专用装配即如此）。
10. **`CompositeDomainStore.for_manager(manager)` 直接构造口**：`domain_store_target` 为默认 `composite` 时，manager 手里已有自身引用，无须再经 `DomainStoreProducer` 按具名引用绕回来解析一次；非默认 target 仍走 Producer，可换实现的能力保留。非法 `preferred_retrieval_pipeline` 在两条路径上抛同一个 `ValidationError`。
11. **数据面真源读写统一走 `manager.kv(kv_name)` 具名端口**：`CompositeDomainStore._raw_kv()` 此前直接读 `manager._stores[KV]` 以绕开授权代理（F07 A 组遗留中记录的跨类私有访问权宜），现改为与 control 面同一条路径。「避免双重授权」不成立为保留 raw 出口的理由——领域方法授权 `memory_unit`、端口授权 `kv`，两层 resource 不同，分层授权是预期语义；而 C 组已让 control 面直连 KV 端口（走代理），数据面走 raw 反倒是同一份真源两条授权路径的不一致。一并消除 A 组的那条已知遗留，且管理面不必新增绕过代理的公开出口。
    端口名由装配期 `resolve_name(config, "kv_store")` 指名并存进 `CompositeDomainStore.__init__`
    的 `kv_name`，与 `HybridIndexBuilder` / `KeywordRecaller` 等消费方同一套选择键机制——
    数据面此前硬编码 `"default"`，是这批消费方里唯一的例外。（该键的声明位置随后由
    E-5 归位到 `domain_stores.<name>`。）
12. **entity 纳入 health 聚合，不开豁免**：降级的正确位置是**装配期**（builder 返 None → 无 capability → 消费方跳过），不是探活期。行为改变的只有"声明了、构造成功了、但后端挂了"这一种情况——这本就该报出来，它补的正是背景里那个三层静默的可观测性缺口。运行期容错不受影响（两侧 try/except 仍在）；fusion/fs 同为可选能力且声明即参与 health，entity 无理由特殊。
13. **`entity_enabled` 与 `has_entity()` 并存，前者优先短路**：二者回答不同问题、住在不同层——`entity_enabled` 是跨切面意图开关（globals，`config.get` 回退），`has_entity(name)` 是能力事实（实例级，`resolve_name` params 直读不回退）。读法不对称是有意的。`entity_enabled=False` 时直接跳过、不查询 manager；`True` 但端口未装配时降级并留日志（替代此前的完全静默）。同时**删掉消费方的旧 try/except**：它包住的是 `EntityStoreProducer.dep`（Producer 解析 + 客户端构造），改造后端口构造已搬到 manager 装配期，这层兜不住；剩下的 `manager.entity()`（字典查表）与对象构造都不做 IO，留着只会把配置错误静默吞成"entity 关闭"。

### E. Recaller 归入数据面 + 数据面装配参数归位

1. **`Recaller` 契约与三个实现整体移入 `storage/domain_store_impl/`**：`recaller.py`（契约）
   与 `keyword_recaller.py` / `vector_recaller.py` / `graph_recaller.py` / `unit_aggregation.py`
   与 `composite_domain_store.py` 平铺同目录，不另起 `storage/recaller.py` + `recaller_impl/`
   的平级二元。理由是归属而非文件数：Recaller 不是与七类 Store 并列的第八类后端，而是
   `CompositeDomainStore` 的构件——放进数据面实现目录，「谁拥有它」在目录结构上就是自明的。
   `RecallerProducer.TOP_NAME` 仍是 `recaller`，**YAML 命名空间与注册名逐字不变**，配置零迁移。
2. **`Recaller` 不再继承 `RetrievalOperator`，`RetrievalOperatorType` 删掉 `RECALLER`**：只保留
   `channel()` / `recall()` / `health()` 三个抽象方法。`operator_type()` 对一个不进检索算子表的
   组件没有意义，全仓也确实无人读取（只有定义，没有消费点）。留着枚举值会让「能力声明必须
   真实」（S09 不变量 6）落空——一个没有任何实现者能返回的取值不是稳定契约而是死枝。
   删除后 `storage → retrieval` 的导入边彻底消失，F06 决策 5 的延迟导入连同它绕开的循环一起去掉。
3. **彻底移除 `jiuwen_memory.retrieval.recaller` 旧路径，不留兼容 re-export**：`retrieval/__init__.py`
   去掉 `Recaller` 导出，两份 API 文档的自定义 Recaller 示例同步改导入路径。留一层薄转发看似
   便宜，实际是让「Recaller 属于哪一层」长期保持两个答案；本仓库不承诺第三方插件路径的向后
   兼容（S09 靠 target 名而非模块路径稳定），而 target 名恰恰没变。
4. **`for_manager(manager, config)` 收口**：profile 派生（`preferred_retrieval_pipeline` /
   `kv_store`）、召回路组装与 `bind_recallers` 绑定三件事在该方法内一次完成。此前它只做前两件、
   把绑定留给调用方，于是 manager 侧必须跨模块 import 私有的 `_assemble_recallers`，并留出
   「构造完但还没绑召回路」的半成品状态。`bind_recallers` **保留为公开的手工/测试接线口**
   （F06 决策 4 不变，重绑守卫不变），只是装配路径改由 `for_manager` 内部调用。
   非法 `preferred_retrieval_pipeline` 的解析抽成模块级 `_parse_pipeline`，直构与 Producer
   两条路径共用，错误契约因此逐字一致（此前是两份重复代码）。
5. **数据面装配参数全部归位到 `params.domain_stores.<name>`**：`preferred_retrieval_pipeline` /
   `kv_store` / `domain_store_target` / 七个 `*_recaller` 选择键从 `store_manager.<inst>.params`
   下移一层。manager 段自此不承载任何数据面配置——它只是数据面声明的容器。`vector_store` /
   `fulltext_store` / `graph_store` 三个死键一并删除（D-6 之后零读者，删除零行为变化）。
   `*_enabled` 三个开关**留在 globals 不动**：构建侧与检索侧共读，是真跨切面。
6. **`domain_stores` 允许并要求 `default`，「段内不得声明 default」的规则反转**：`default` entry
   是数据面参数的新家，也是命名实例的 overlay base；整段缺省时按空 base 建出全默认的 default
   数据面。原规则的理由（「default 由装配自动构建，显式声明产生歧义」）在参数归位后失效——
   歧义来自「default 的参数写在别处」，归位后它和命名实例是同一种东西。
   `_build_domain_store` 与 `_named_domain_stores` 两个函数相应合并为单一 `_build_domain_stores`，
   default 与命名走同一条代码路径，E 组背景里那张读法相反的表从结构上消失。
7. **继承规则只有一条，对所有键统一生效**：`ds_params(name) = {**default_entry, **entry}`，
   再经 `ComponentConfig.get` 回退 `globals`。**base 是必需而非便利**：`Factory.dep` 读的是
   `config.params`（params 直读，不回退 globals），命名 entry 若拿不到 `*_recaller` 键，`dep`
   会落到 `cls.build(default, {}, ctx)` **匿名新建**一套不共享、params 为空的 recaller——装配
   不报错，读写各用一套实例，退化完全静默。故新增用例断言的是**对象身份**（`is`）而非通道名：
   通道名在退化前后都相同，只有身份能分辨。
8. **非 composite target 不再补绑 recaller**：原 `isinstance(ds, CompositeDomainStore)` 兜底移除。
   「非 Composite 实现自带检索路径、无需 Recaller」本就是 F06 决策 3 的前提；该分支只在
   「有人把 `CompositeDomainStore` 以别的 target 名注册」时才可能命中，此时召回路应由注册方
   自己负责（可直接调 `for_manager`）。仓库内只注册了 `composite` 一个 target。

## 拒绝的方案

### A. 双面拆分

- **只拆接口不拆实例（一个类同时实现两个 ABC）**：等于没拆，类型层面依赖仍模糊。
- **保留 `Storage` 联合 ABC 渐进迁移**：留退路等于迁移被无限期推迟，一次性切、不留兼容期。
- **数据面逻辑搬到调用方（`PipelineRetriever` 自己实现 recall 编排）**：破坏 `DomainStore` 抽象边界；多路召回 + 融合 + 复核逻辑集中在 `CompositeDomainStore` 更可维护。
- **`bind_recallers` 下沉到 `DomainStore` ABC**：`RoutingDomainStore` 不能 bind，下沉会强制实现一个永远不该调的方法。
- **Recaller 运行期持有 manager / `PipelineRetriever` 同时持 manager + domain_store**：运行期实际只需要点读真源/数据面；持续持有 manager 让「装配期需要 manager、运行期需要 domain_store」的边界在类型层面无法表达。
- **删 `bind_recallers`、完全靠工厂装配**：手工接线（测试、`make_world` fixture）仍需要它；作为「手工/测试接线口」保留。
- **保留端口双入口（property + `*_port` 并存）**：双入口让「获取 Store 的唯一路径」无法成立；合并后成员 24 → 12。
- **仅保留 `*_port` 长名**：语义等价但写法变长，且与 `domain_store()` 命名风格不一致。
- **DomainStore 走 YAML 平级独立段装配**：弱化「所有存储类从 StoreManager 获取」原则，且 recaller 装配要读 manager 侧开关键，平级段会把装配链路撕成两半。改为 manager 工厂内构建（保留 `domain_store_target` 可换实现）。

### B. 全局唯一 manager 与命名实例

- **manager params 显式端口白名单（`ports: {kv: [aux]}`）**：与全量自动语义等价但多一层配置；「声明即端口」更简单。
- **`domain_store(name)` 做跨配置栈选择**：需要 manager 实例跨进全局命名空间，破坏封装；整栈切换已有路径（消费者 params 引用不同 manager 名 / F02 Routing）。
- **`StoreManagerProducer.TOP_NAME` 保留 `"storage"`**（A 组的原选择）：配置段名与类名分裂易混淆；接受 YAML 兼容性破坏（`storage:` 段与 `storage.active` 键需改写），一次性切换。
- **消费者构造收解析好的实例（实例注入形态）**：测试可直接传 fake Store，但改动面更大（约 10 个构造签名重排）；选 manager + name 参数形态。
- **端口选择键回退 globals**：端口选择是消费者实例级决策，回退 globals 会让全局键静默覆盖实例级选择。
- **保留 `params.storage` 引用语义兼容**：留着等于给「第二套 manager」留后门，与全局唯一语义矛盾。
- **Recaller 端口名仅由 layer 推导**：自定义分表/多向量空间场景需要显式指名；显式覆盖优先、缺省推导，两层并存。

### C. 控制面真源读写直连 KV

- **保留 DomainStore 依赖（行为等价，不动）**：`CompositeDomainStore` 下行为确实等价，但依赖面更大——未来 DomainStore 获得非 KV 语义（如一体化后端自带索引投影）时，control 面会静默继承它未声明消费的能力。
- **消费者持 manager、调时再取端口**：违反「运行期持最小接口」——manager 是装配期对象，端口应在构造期固化（`kv(name)` 返回的 `_LazyStorePort` 代理已保证 active 切换时跟随重解析）。
- **lifecycle 回写仍走 `DomainStore.update(FORWARD_ONLY)`**：CompositeDomainStore 对 FORWARD_ONLY/ALL 行为相同，调用实际是裸 KV 往返 + 授权代理二跳；且 control 面若为此单独保留 DomainStore 注入点，决策 C-1 的收窄就不彻底。
- **在 DomainStore ABC 上加纯 KV 便捷方法（`load_units`/`list_units` 成员）**：会把「持最小接口」退化回「持 DomainStore」；模块级函数收 `KVStore` 入参，端口消费者与数据面消费者都可复用。

### D. EntityStore 纳管

- **只收敛实例唯一性，不给 capability 席位**（在 manager `_build` 里统一建一次再注入两个消费方）：能消除实例分裂，但 EntityStore 仍拿不到授权代理与 health 聚合，三处存量违例只消除一处；且"受 manager 管理却不在 capability 里"会新造一种形态。
- **仿 `domain_store` 的独立实例表**（`entity(name)`/`has_entity(name)` + 独立 `_entity_stores`，不进 `StorageCapability`）：`domain_store` 是数据面编排对象，EntityStore 是后端 Store，二者不同类；走这条路 `has_entity` 无法由 capability 集合推导，与另六个 `has_*` 的写法分叉。
- **把四个方法首参改成 `scope: Scope`**（`space_id_from_scope` 与 `EntityStoreFilters.from_scope` 下沉实现内部）：接口最统一、代理无需特例，但会抹掉 entity 索引 `space_id` routing 与 Scope 五段的语义差异，且改动面扩到 ES 实现 + 2 个消费方 + 3 个测试桩。签名冻结、代价由代理承担是更小的切口。
- **在通用 `_AuthorizedStoreProxy` / `_action_for_store_method` 里加 entity 分支**：见决策 D-3。
- **`execute_operations` 统一映射 `ADMIN`**：授 ADMIN 等于解锁全部 Store 的所有未映射方法，写入是常规数据面动作，不该要管理员权限。
- **`execute_operations` 用固定的 `{ADD, UPDATE, DELETE}` 并集**：纯 INSERT 的 batch 会被迫要求 DELETE 权限，`DenyWritesSecurity` 这类策略对混合 batch 的判定也不精确。
- **`find_*` 映射 `GET`**：既有 GET 是「按 id 点查 / 枚举」，把反向索引的批量反查归进去，会让只授了「读自己记录」的身份意外获得全库反查能力。
- **deploy 全量抄写 `store_manager.default.params` 后再加 `entity_store` 键**：把最坏的配置陷阱引入全栈拓扑的根——两份 config.yml × 12 个键需与 defaults.py 手工同步，漏抄 `keyword_l0_recaller` 之类不会报错、只会静默丢一路召回。
- **`defaults.py` 加 `entity_store: _D` 与顶层 `{_D: "elasticsearch"}` 默认段**：在纯内存默认栈里声明一个永远连不上的 ES（决策 D 不做内存 entity 实现，没有合法后端可指），且每次装配、每个单测都刷一条 `hosts not configured` warning；消噪的两条路——降 debug 会丢掉"开了 entity_enabled 却忘配 hosts"的真实信号，让 ES builder 读 `entity_enabled` 则是存储后端反向依赖上层开关的层级倒置。
- **只给 ENTITY 单设兜底解析、其余六类维持 params 引用键**：能以最小改动接入第七能力，但把「命名空间 → 端口」的推导规则一分为二，第七席成为特例；且六类各自的「params 漏写引用键 → 能力静默消失」陷阱原样留着。故取七类统一的全量聚合（决策 D-6）。
- **`__init__` 直接接 `config` 内部构造全部 Store（单入口）**：入参最简洁，但 60+ 处手工构造点须从「传内存 Store 实例」改写为「先造 `AssemblyContext` 再装配」，测试可读性与调试难度显著变差；改用双入口（决策 D-9）在收内聚装配逻辑的同时保住手工路径。
- **引入 `*_store_list` 端口白名单**：能根治 encrypted 明文 raw 以具名声明时随之暴露的问题，但漏列会静默丢端口（与漏抄 recaller 键同类陷阱），且每个部署多一项配置负担。维持「声明即端口」，raw 仍推荐在 kv 实例 params 内 inline 声明规避。
- **新建 `InMemoryEntityStore` 生产实现**：本次不做。storage 层测试用手工构造的 spy 覆盖端口契约，装配链路用 ES 实现验证（构造期零 IO，`client` 是惰性 property）。代价见「已知遗留」。
- **给 entity 开 health 豁免**：见决策 D-8。

### E. Recaller 归属与参数归位

- **`storage/recaller.py` + `storage/recaller_impl/` 平级二元**：形式上与 `kv.py` + `kv_impl/`
  同构，但那套约定是给「与七类端口并列的后端」用的。Recaller 不是后端也不是端口，它是数据面
  的构件；平级放置会让人以为它是第八类 Store，恰好抹掉本次要确立的归属。
- **保留 `Recaller(RetrievalOperator)` 继承**：`retrieval/base.py` 是零依赖叶子模块（只 import
  `abc` + `enum`），storage 导入它确实不会成环，改动面也最小。但这样 `storage → retrieval`
  的导入箭头会永久留在纸面上，且 `RetrievalOperatorType.RECALLER` 成为无实现者的死枚举值。
  为省几行改动保留一条反向依赖不划算。
- **保留 `jiuwen_memory.retrieval.recaller` 的兼容 re-export**：见 E-3。
- **只把 `preferred_retrieval_pipeline` 挪到 globals**：改动最小，但只解决了四分之一——
  `kv_store` / `domain_store_target` / 七个 `*_recaller` 仍寄居 manager 段，继承语义照旧分三套；
  且 pipeline 是数据面的 per-instance 参数，不是跨切面开关，放 globals 语义也不对。
- **顶层平级 `domain_store:` 命名空间**（本文 A 组曾以另一个理由拒过）：与七类 Store 命名空间
  同构、`_collect_ports` 可直接复用，是最"整齐"的形态。仍不采纳：`domain_store.<name>.params`
  必须手写 `store_manager: <name>` 引用（`DomainStoreProducer._build` 无 default，漏写会触发
  manager 匿名重建的无限递归），而嵌套段是由装配自动注入该引用的；把一个装配期不变量交给
  部署方手写，换来的只是目录形态的整齐。
- **命名 entry 不继承 `default`，各自写全**：显式当然更"无歧义"，但 E-7 那条静默退化正是它的
  代价——漏抄 `*_recaller` 不报错，只是读写悄悄用上两套实例。让配置在**装配期**就无法表达
  错误状态，优于让它可表达但靠纪律避免。

## 验证

- **拆分冒烟（A）**：手工构造 manager → 端口访问、代理身份稳定、未声明端口抛 `UnsupportedStorageCapabilityError`、`domain_store()` 未绑定报错/绑定后缓存稳定、领域 CRUD 正确；完整装配链路 7 路 recaller 同步装配、选择键指向未注册实现 fail-fast；端到端 add → retrieve 命中；Routing 委托链路与 active 切换计数验证。
- **具名键全链路（B）**：`dedup.params.{kv_store: aux, vector_store: my_idx}` 断言实例身份 = `manager.kv("aux")` / `manager.vector("my_idx")`；`engine.params.domain_store: fast` 的 profile 与 default 互异、物理 KV 互通；未声明名 fail-fast；globals 三态（default/自定义名/未声明报错）；inline dict 端口键拒绝；四处点读回归；recaller 覆盖优先。
- **控制面直连（C）**：新增 `tests/unit/storage/test_kv_helpers.py`（`list_units` 反序列化/非 unit 过滤/memory_types+filters 透传/scope 隔离；`load_units` 保序/缺失省略/重复 id/空入参）；10 个 control 测试文件约 30 处构造点从 `domain_store=make_storage(kv=...).domain_store()` 简化为 `kv=kv`；三个测试替身的交付通道同步改为 KV 直写（`memory_key` + `dumps`），与真实 `ForwardIndexBuilder` 行为一致。
- **EntityStore 纳管（D）**：新增 `tests/unit/storage/test_entity_port.py`（18 例）——能力发现与端口暴露（含代理身份稳定、`store_type()` 返 ENTITY）、缺失时抛 `UnsupportedStorageCapabilityError`、命名端口真值表、端口表丢弃 None 值且 health 不炸、两个 `find_*` → SEARCH 且 scope 近似为 `Scope(space, user)`、`ensure_index` → ADMIN、混合 batch 派生 `{ADD,UPDATE,DELETE}`（LINK/UNLINK_UPDATE 去重为 3 次授权）、空 batch 零授权但仍委托、`DenyWritesSecurity` 下拒写放行读且**授权先于委托**（被拒调用不触达后端）、health 聚合与 `id()` 去重、命名空间全量聚合成端口、只声明具名实例时 capability 成立而 default 端口不可用、无 hosts 装配期降级、命名端口无 hosts 被丢弃、deploy 形状配置能装出端口。装配用例走完整 `AssemblyContext → build_named → has_entity()` 链路而**不连真实 ES**（`ElasticsearchEntityStore.__init__` 只存字段，`client` 是惰性 property，构造期零 IO；这类用例不调 `health()`——那会 ping）。
  `tests/unit/construction/test_hybrid_entity_wiring.py` 新增 4 例装配侧覆盖，核心是 **`test_builder_and_recaller_share_the_same_entity_port`**：预置含 ENTITY 端口的 manager，分别 build `hybrid` 与 `keyword` 两个 producer，写入侧 `build()` 后清空调用记录、再走真实 `recall()`，断言召回侧的 `find_by_entity_text_hash` 落在**同一个** store 实例上——这正是 D 组要拿到的收益，改造前两侧各自 `dep` 会各建一个匿名实例。另加"端口未装配时降级不抛"与"`entity_enabled=false` 时不查询端口"两例。
  `tests/unit/config/test_storage_routing.py` 加 `test_entity_port_follows_active`（ENTITY 端口随 `store_manager.active` 切换，构造期缓存的惰性端口在切换后解析到新实例；`_LazyStorePort` 对 `space_id` 首参透明转发）。三个测试 fake 同步：`UnitOnlyStoreManager` 实现 `entity()`（不实现则抽象方法致收集期 TypeError）、两个内存 entity 桩改继承 `EntityStore` 并返 `StoreType.ENTITY`。
- **Recaller 归属与参数归位（E）**：新增 `tests/unit/storage/test_named_domain_stores.py`（11 例）——`domain_stores` 段此前**全仓零覆盖**，背景里那个继承分叉正因此从未暴露。覆盖：default entry 的 pipeline/kv_store/recaller 三类键生效（kv 用判别式断言写入只落 `truth`、`default` 端口保持空）、整段缺省仍建出 default 数据面、显式声明 `default` 被接受（原抛 `ValidationError`）、命名 entry 只覆盖声明的键、命名 entry 可覆盖 recaller 选择、命名 entry 继承 default 的 kv 端口、两条映射校验、非法 pipeline 装配期 fail-fast，以及「manager params 不含 `<ns>_store` 引用键时七类端口照样齐」——锁死 D-6 之后那三个键确已成死键，防止有人顺手补回制造 manager 段承载端口配置的假象。
  核心是 **`test_named_entry_shares_recaller_instance_with_default`**：命名 entry 只覆盖 pipeline 时，其 keyword recaller 与默认数据面是**同一个对象**（`is` 断言）。反证做过——把 `_build_domain_stores` 的 `{**base, **entry}` 改回 `entry`，该例与 `test_named_entry_inherits_default_kv_port` 立刻转红，恢复即绿；这证明它们确实锁住了 E-7 描述的静默退化，而非在断言一个恒真的通道名。
  `tests/unit/retrieval/test_recallers.py` 随源码镜像迁到 `tests/unit/storage/`；`conftest.py` 与 4 个测试文件改导入路径，2 个文件删掉 4 处 recaller 桩的 `operator_type()`；3 处配置键（`test_storage_factory_wiring` 的 recaller 覆盖键、`test_composite_storage` 的 pipeline 与 kv_store）下移到 `domain_stores.default`。
- **基线**：`pytest tests/unit/` 全量 **1978 passed / 8 skipped**（零失败；1967 基线 + E 组新增 11）；`pytest tests/integration/` **48 passed / 61 skipped**（skip 为缺 asyncpg 等外部依赖的既有跳过）。`ruff check jiuwen_memory/ tests/` 改造前 169 → 改造后 **165**：E 组自身零新增（引入的 1×F401 + 4×I001 已修），另顺带清掉 4 处存量。默认配置装配冒烟：`StoreManagerProducer.build_named("default", ctx)` 装出七类端口与 7 路 recaller，pipeline/kv_name 均从 `domain_stores.default` 正确派生。

## 已知遗留

- **「本体不落 KV」的一体化 DomainStore 会破坏 C 组前提**：直连 KV 的等价性依据是 S06 不变量 28（真源恒为 KV `/memory/` 前缀）+ 当前唯一注册的 domain_store 实现是 composite。未来 F05 愿景中「本体进一体化后端、不落独立 KV」的实现落地时，control 面直连 KV 会读不到真源，届时需重新评估（Engine/Jobs/Lifecycle 应改持 DomainStore 或由装配注入统一读端口）。
- **授权语义变化（B-6 / C-1）**：点读与列表读的授权 resource 从 `DomainStore` 领域动作（`memory_unit` 等）变为 KV 端口代理动作（`kv`，GET/LIST/UPDATE/ADMIN 标签）；且 `_AuthorizedStoreProxy` 对 `load_units` 是每 key 一次授权事件（原 `DomainStore.get/list` 每调用一次），审计事件数量级变大。默认 `AllowAllStorageSecurity` 下零感知，自定义 security 策略需按新 resource/动作名调整。这是收紧而非放松。
- **YAML 兼容性破坏（B-1）**：用户配置的 `storage:` 顶层段与 `storage.active` 键需改写为 `store_manager:` / `store_manager.active`；旧段装配期明确报错（fail-fast），旧 `params.storage` 键静默无效。
- **`resolve` 不再匿名兜底**：空 `AssemblyContext`（无 `store_manager` 段）下调用 resolve 报错——手工装配场景需显式声明或 `put` 预置。
- **`CompositeDomainStore._validate_units` 前置校验在直连 KV 路径不再被执行**（C）：`unit.scope != scope` 的防御性校验丢失。lifecycle 写对象全部从同一 scope 点读而来（一致 by construction），CloudEngine 保留 `_ensure_unit_scope` 读后校验，风险面未扩大；外部构造的 unit 直接调 KV 回写时该不变量由调用方负责。
- **`pipeline_retriever.py` 的 `storage` property 名保留**（返回类型已是 `DomainStore`），后续可重命名为 `domain_store`。
- **`bind_recallers` 在 `RoutingDomainStore` 上不可用**：手工接线始终作用于 `CompositeDomainStore` 实例。
- **`domain_store(name)` 急切构建**：`_named_ports` 与 `domain_stores` 段在 manager 装配期构建全部声明实例，指向外部服务的具名 store 即使无人使用也会被构建，装配失败面变大（设计代价）。
- **本次不实现除 `CompositeStoreManager` + `CompositeDomainStore` 之外的其他实现**（如一体化 `IntegratedDomainStore`），待后续按需经 `domain_store_target` 注册新 target。
- **`execute_operations` 无 `filters` 参数，写入侧无法做 actor 级授权**（D）：授权入参的 `user` 段在写入路径恒空，只能按 `(space, action)` 判定。写入侧的 actor 隔离由 `EntityRecord.filters` 记录内字段承担。需要写入侧 actor 级授权的部署，应在 `EntityLinkService` 之上做，或未来给 `execute_operations` 加 `filters` 参数（签名变更，本次冻结）。
- **ENTITY 端口的授权 scope 是有损近似**（D）：`org`/`agent`/`session` 恒空，`space` 可能实际是 org id 或字面量 `"default"`。自定义 `StorageSecurity` 若按这三段判定 `resource == "entity"` 的调用会得到错误结果——这是契约层的约定，已在 S06 不变量 26、AGENTS.md 铁律 12 与代理类 docstring 三处记录。
- **无 `RoutingEntityStore`**（D）：Store 级 routing（`config.routing.Routing*Store`）只覆盖六类，没有配置路径会构造 entity 版本。`RoutingStoreManager` 的 `entity()` 走既有 `_lazy_port`，整颗 manager 的 active 切换正常生效；只是缺"单独给 entity 做 Store 级路由"的能力，需要时另开特性。
- **entity 纳入 health 后，把 `health()` 当 liveness probe 的部署有新的重启风险**（D）：entity ES 抖动会让探活失败。仓库 `jiuwen_memory/api/` 下未发现 health 端点调用点，实际影响面限于测试与显式调用方；缓解建议是拆 required/optional 两级探活，或部署侧改用 readiness——本次不实现。
- **`entity_impl/` 无内存实现，默认栈永远无 ENTITY 能力**（D）：`defaults.py` 不声明 `entity_store` 段（也无合法的内存后端可指），故默认装配下 `has_entity()` 恒 False。storage 层测试靠手工构造的 spy 覆盖端口契约，装配链路靠 ES 实现（构造期零 IO）验证；补内存实现后可让默认栈也具备该能力，届时 `_entity_store` 的第一级 params 解析自然命中，兜底退居后备。
- **`params.entity_store` 的语义变化**（D）：该键从 Producer 依赖引用（接受 inline dict）变为 manager 端口选择键（`resolve_name` 只接受端口名字符串，inline dict 装配期报错）。仓库内无 inline 写法实例，两份 deploy config 的 `entity_store: default` 字面不变、语义已改。
- **manager params 内 inline 声明 Store 的写法失效**（D-6 的代价）：`store_manager.<inst>.params.kv_store: {target: "memory"}` 这类内联声明不再被读取，后端一律经 `<ns>_store` 命名空间声明。生产配置（defaults.py 用字符串引用、两份 deploy config 无 store_manager 段）不受影响，仓库内仅 3 处测试用过该写法，已改写为命名空间声明。注意 encrypted KV 的 `raw_kv_store` 是 **kv 实例自身 params** 内的 inline，由 `KvProducer.dep` 解析，不经 manager，**不受本变更影响**。
- **默认端口必须恰好命名为 `"default"`**（D-6 的代价）：给实例起别的名字（如 `kv_store.truth`）后该类就没有默认端口，消费方须经 `params.<ns>_store` 选择键指名。此前可用 manager params 引用键把任意具名实例指定为默认端口，该能力已放弃（实测生产配置未使用）。
- **顺带修复**（D）：两份 deploy config 的 `constructor` / `recaller.keyword` 段仍写着 B 组已废的死键 `storage: default`（`StoreManagerProducer.resolve` 读的是 `store_manager`，且 globals 已指名），一并删除。
- **`RetrievalOperatorType.RECALLER` 删除是公开枚举的破坏性变更**（E-2）：该值移走后零实现者，但枚举本身经 `retrieval/__init__.py` 对外导出。自定义 Recaller 已因 E-3 的路径删除必须改代码，故不额外增加迁移成本。
- **`jiuwen_memory.retrieval.recaller` 路径删除**（E-3）：第三方自定义 recaller 需改为 `from jiuwen_memory.storage.domain_store_impl.recaller import Recaller`。两份 API 文档（`docs/zh|en/API文档/`）已同步——`retrieval.md` §5 收缩为指路小节（保留编号，不动其后 12 节），完整契约落在 `storage.md` §20。注册用的 target 名与 YAML 命名空间不变。
- **用户配置需下移一层**（E-5）：写在 `store_manager.<inst>.params` 的 `*_recaller` / `preferred_retrieval_pipeline` / `kv_store` 要移进 `params.domain_stores.default`。`defaults.py` 与两份 deploy config 已改（deploy 本就不覆写 `store_manager` 段，实际迁移面限于用户自定义配置）；旧位置的键**静默失效**，不报错——因为 manager 段允许任意 params 键，无法与合法的自定义键区分。
- **`AssemblyContext.merged` 仍是实例级整体覆盖**（E-5 未改变）：覆写 `store_manager.<inst>.params` 要写全整个 `domain_stores` map。归位后这一点更显眼（单一嵌套键，而非散落的十来个平级键），但陷阱性质不变，两份 deploy config 的告警注释保留并已按新形态改写。
