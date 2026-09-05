# Storage 双面拆分（StoreManager/DomainStore）、全局唯一 manager、控制面直连 KV 与 EntityStore 纳管

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-04（D 组追加 2026-09-05） |
| 影响范围 | `jiuwen_memory/storage/`（`store_manager.py` / `domain_store.py` / `kv.py` / `store_manager_impl/` / `domain_store_impl/`；删 `storage.py` 与 `storage_impl/`），`jiuwen_memory/config/`（`routing.py` / `defaults.py` / `keys.py`），`jiuwen_memory/retrieval/`、`jiuwen_memory/construction/`、`jiuwen_memory/control/`、`jiuwen_memory/api/` 全部消费者，`docs/specs/S02`–`S06`/`S08`，各 `jiuwen_memory/<subdir>/AGENTS.md`，`docs/design/architecture.md`；
**D 组另涉**：`jiuwen_memory/storage/`（`base.py` / `store_manager.py` / `store_manager_impl/composite_store_manager.py` / `entity_impl/elasticsearch_entity_store.py`）、`jiuwen_memory/config/routing.py`、`jiuwen_memory/construction/index_builder_impl/hybrid_index_builder.py`、`jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py`、`deploy/docker/{local,online}/config.yml`、`docs/features/retrieval/F06-entity-recall-channel.md` |
| 测试基线 | `pytest tests/unit/storage/ tests/unit/control/ tests/unit/construction/test_infer_context_extract.py` 392 passed / 6 skipped；`pytest tests/unit/api/ tests/unit/retrieval/` 421 passed；`pytest tests/integration/retrieval/ tests/unit/construction/` 366 passed；`evaluation/smoke_test` 10/10；`ruff` 改动文件零新增问题 |
| Refs | — |

> **本文由原三份特性文档合并而成**：原 F07（Storage 拆分 StoreManager/DomainStore）、
> 原 F08（全局唯一 StoreManager 与命名实例）、原 F09（控制面真源读写直连 KV 端口）。
> 三者是同一连贯设计变动（运行期持最小接口）的连续落地，按「一次连贯变动归一份文档」
> 规约合并。决策编号沿用原文分 **A（原 F07 决策 1–11）/ B（原 F08 决策 1–8）/
> C（原 F09 决策 1–5）** 三组——代码与文档中既有的「F07 决策 X」「F08 决策 X」
> 「F09 决策 X」引用分别对应本文 A/B/C 部分第 X 条。
>
> **D 组（2026-09-05 追加）**：EntityStore 纳入 StoreManager 成为 `StorageCapability`
> 第七席。它是同一条主线（「所有 XXXStore 获取经 StoreManager」，S06 不变量 30）的收尾
> ——A/B/C 的改造对象限定在原 `Storage` ABC 谱系内，而 EntityStore 走独立 Producer、
> 从未进入该谱系，因此被整个漏掉；故按同一份文档追加而非新开特性。代码与文档中的
> 「F07-D」引用对应本文 D 部分。**勿与既有的「F08」混淆**：后者在本仓库语境下恒指
> 原 F08（全局唯一 manager），即本文 B 组。

## 背景

**第一阶段（A：双面拆分）**。`Storage` ABC 落地（F05-unified-storage-design）后一身二任：

- **管理面（端口管理）**：`capabilities()` / `has_*()` / `kv`·`vector`·`fulltext`·`graph`·`fusion`·`fs` 端口 / `security` / `health()`
- **数据面（领域操作）**：`add` / `update` / `delete` / `get` / `list` / `scopes` / `recall` / `recall_and_get` / `retrieve` / `preferred_retrieval_pipeline` / `bind_recallers`

两类职责混在同一 ABC 里，上游依赖 `Storage` 类型时同时耦合端口管理与领域操作——只需要"取 KV 写本体"的 `ForwardIndexBuilder` 被迫持有 `recall/retrieve`；只需要"调 recall 走首选路径"的 `PipelineRetriever` 被迫持有 `kv_port` 等管理面接口。职责边界模糊让"谁该依赖什么"无法在类型层面表达，只能靠调用方自觉。

**第二阶段（B：全局唯一 manager）**。拆分落地过程中暴露三个结构性问题：

1. **manager 选择分散**：15 处消费者 `params.storage: default` 引用逐个声明所用 manager，同一配置里写漏/写错一处就出现第二套 manager 实例（有状态依赖，新建等于换后端）；`StoreManagerProducer.resolve` 还带匿名兜底构建分支，错误配置被静默吞掉。
2. **端口/数据面无法具名选择**：端口方法 `kv(name)` 签名上有 name，但配置装配只认 `layers_l0/l1` 两个硬编码名，`kv/graph/fusion/fs` 端口完全没有装配路径；`domain_store()` 是唯一无 name 的获取口（单槽）。
3. **纯点读场景过度注入**：`Dedup._load_unit`、`Governor._find`、Schema Evolver 源读、`KeywordRecaller` 实体扩展只做「按 unit_id 点读」，却注入了整个 `DomainStore`（或整个 manager）——类型层面无法表达「运行期只需要的最小接口」。

**第三阶段（C：控制面直连 KV）**。拆分后 control 侧七个消费者注入了 `DomainStore`：两个 Engine、`list_support`、`KVLifecycleManager`、`EvolveJob`/`MiddleToLongJob` 两个 Job Spec、以及 `PipelineRetriever`。逐一审计实际调用方法：

- **Engine×2 / list_support / 两个 Job**：只用 `get`/`list`/`scopes`——零检索适配、零领域写；
- **KVLifecycleManager**：`get`/`list`/`scopes` + `update(mode=FORWARD_ONLY)`——唯一的写调用，而 `CompositeDomainStore.update` 对 `FORWARD_ONLY` 与 `ALL` 行为相同（无投影能力，只落本体）；
- **PipelineRetriever / UnifiedIndexBuilder**：`recall`/`retrieve`/`preferred_retrieval_pipeline` 与带 `mode` 的领域写——DomainStore 的本职消费方。

即 control 侧 6 个文件 5 个类持有的 `DomainStore` 引用实际只用了 KV 等价能力——`CompositeDomainStore` 的 `get`/`list`/`scopes`/`update` 本身就是 `manager._stores[KV]` 的薄包装（loads/dumps + `memory_key`），DomainStore 在这条链路上是纯间接层。B 阶段决策 6 已为纯点读场景立了「运行期持最小接口」的先例，C 阶段把同一原则推进到 control 面。

**第四阶段（D：EntityStore 纳入 manager）**。A–C 三阶段确立的「所有 XXXStore 获取经 StoreManager」（S06 不变量 30）落地时漏掉了 `EntityStore`——A/B/C 的改造对象限定在原 `Storage` ABC 谱系内，而 EntityStore 自诞生起就走独立的 `EntityStoreProducer` + `entity_impl/`，从未进入该谱系，于是被整个绕开：前三阶段的文档、S06 的接口契约段落、AGENTS.md 的铁律条款均未提及它。留下的是三处**既有规约的存量违例**：

| 规约 | EntityStore 的状态 |
|---|---|
| S06 不变量 30「所有 XXXStore 获取经 StoreManager」 | 写入侧 `HybridIndexBuilder._build` 与召回侧 `KeywordRecaller._build` 各自 `EntityStoreProducer.dep` |
| AGENTS.md 本地约束 1「所有 Store 必须实现 `store_type()`」 | `ElasticsearchEntityStore.store_type()` 直接 `return None` |
| AGENTS.md 本地约束 12「Construction/Retrieval/Control 不得直接调 Store Producer」 | 两个消费方恰好分居 construction 与 retrieval |

违例的实际代价：`Factory.dep` 在 `params.entity_store` 缺省时走 `cls.build(default, {}, ctx)`——**匿名新建、不入缓存、params 为空**，读写两侧因此各持一个独立 ES client；空 params 下 `hosts` 只能靠 globals 兜底，否则 builder 静默返 None、entity 链路无声关闭。"读写共享同一实例"完全靠"两端 params 各引用同一具名实例"的配置纪律维持（`deploy/docker/local/config.yml` 的注释已明写"缺一侧则该侧 disabled，不报错，静默降级"），而这正是铁律 4「隔离必须在存储层强制，上层不依赖调用纪律」要消灭的东西。此外 entity 后端故障对 `health()` 完全不可见——写侧 try/except 吞、召回侧 `_expand_by_entities` 吞、health 不看，三层静默，运维只能从召回质量倒推。

## 决策

### A. 双面拆分（原 F07）

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

### B. 全局唯一 manager 与命名实例（原 F08）

1. **全局唯一 manager，`globals.store_manager` 指名**：配置顶层段 `storage:` 更名 `store_manager:`（`StoreManagerProducer.TOP_NAME = "store_manager"`）；globals 加 `"store_manager": "default"` 键。`StoreManagerProducer.resolve` 重写为三级链——params 显式覆盖 → `globals.store_manager` → `"default"`；**删除匿名兜底构建分支与 `default_target` 参数**，未声明实例名抛 `ValidationError`。defaults 清理 15 处消费者 `params.storage` 引用与 `ROOT_PARAMS.storage`。
2. **`domain_store(name)` 命名数据面（同 manager 多套）**：一个 manager 持 `dict[str, DomainStore]`，多套命名数据面**共享同一物理 Store 集**，差异仅在检索 profile。ABC 加 `domain_store(name="default")` + `has_domain_store(name)`；配置段 `store_manager.<inst>.params.domain_stores: {<name>: {覆盖键}}` 逐项构建，段内 `"default"` 键拒绝。`RoutingDomainStore(router, name)` per-name 惰性缓存（同名身份稳定 + active 跟随）。
3. **六类命名端口全量自动（声明即端口）**：`_named_ports` 从硬编码 `layers_l0/l1` 改为遍历六类 `*_store` 命名空间下所有非 default 名；`kv/graph/fusion/fs` 端口装配路径补齐。encrypted KV 的明文 raw 若以具名声明会随之暴露为端口——配置写法问题而非机制缺陷，约定 raw 推荐 inline 声明。
4. **消费者具名选择键（manager + name 构造形态）**：消费者构造函数保持收 `StoreManager`，追加 name 参数；`_build` 工厂经 `resolve_name(config, key)` 统一读取——params 直读**不回退 globals**（端口选择是实例级决策）、值必须是名字字符串（inline dict 拒绝）。数据面消费者工厂读 `params.domain_store` 键 → `manager.domain_store(name)`。
5. **Recaller 端口显式覆盖优先**：`vector_recaller`/`keyword_recaller`/`graph_recaller` 的 params 支持 `vector_store`/`fulltext_store`/`graph_store` 键，优先于 layer 推导（缺省 l2→default；l0/l1→`layers_l0/l1`）。
6. **四处纯点读切 KVStore（修订 A-10）**：`storage/kv.py` 新增模块函数 `load_units(kv, scope, unit_ids)`（`memory_key` + `get` + `loads`；缺失省略、保序、不去重、零过滤）。四处切换：Dedup 基类（`__init__(kv: KVStore)`）、`InMemoryGovernor._find`、SchemaOrchestratingEvolver 源读、KeywordRecaller 实体扩展（**删除其 manager 字段**——运行期持最小接口）。
7. **Recaller 端口可选（store None → recall 返空）**：KeywordRecaller 的 kv 端口与 GraphRecaller 的 graph 端口改为可选，与既有 store None 约定对齐。
8. **`_Kernel.kv` 与 ingest_job 任务 KV 统一走 manager 端口**：`_Kernel.kv = manager.kv(resolve_name(root, "kv_store"))`（`ROOT_PARAMS` 既有 `kv_store` 键复用为端口名）；与 `kv_store.default` 具名实例同源（外部注入 kv 经 `KvProducer.put` 预置缓存后 `dep` 命中同一实例）。

### C. 控制面真源读写直连 KV（原 F09）

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
6. **ENTITY 默认端口用三级兜底解析**（`_entity_store`，与 `_manager_kv` 同构）：`params` 显式引用 → `entity_store.default` 具名实例 → 无该能力。不像 vector/graph/fusion/fs 那样只认 params 引用键，因为配置合并是**实例级整体覆盖**（`AssemblyContext.merged` 按实例名 update 整个 `RawSpec`，无 params 深合并）——强制 params 引用等于要求每个既有部署在自己的 config.yml 里全量抄写 `defaults.py` 的 `store_manager.default.params`（12 个键，含 7 个 `*_recaller`），漏抄一个 recaller 键 = 一路召回静默消失。受管成员本就并非都由 params 引用键驱动（`domain_store` 由 manager 工厂内部构建 + `bind_domain_store` 注入），这个不一致有先例且换来的是零迁移成本。
7. **`_named_ports` 统一过滤 builder 返回的 None**：端口表的值非 None 是 `CompositeStoreManager` 的不变量（`health()` 直接 `store.security` / `store.health()`，代理也假定非 None）。增强层后端约定「必填连接参数未配即返 None 表示降级关闭」，在这个**唯一的**「命名空间 → 端口」构造点统一过滤，而不是把 None 判断散进 health 与代理。对六类 Store 是 no-op（它们缺必填参一律 `require_param` 抛错，不返 None）。
8. **entity 纳入 health 聚合，不开豁免**：降级的正确位置是**装配期**（builder 返 None → 无 capability → 消费方跳过），不是探活期。行为改变的只有"声明了、构造成功了、但后端挂了"这一种情况——这本就该报出来，它补的正是背景里那个三层静默的可观测性缺口。运行期容错不受影响（两侧 try/except 仍在）；fusion/fs 同为可选能力且声明即参与 health，entity 无理由特殊。
9. **`entity_enabled` 与 `has_entity()` 并存，前者优先短路**：二者回答不同问题、住在不同层——`entity_enabled` 是跨切面意图开关（globals，`config.get` 回退），`has_entity(name)` 是能力事实（实例级，`resolve_name` params 直读不回退）。读法不对称是有意的。`entity_enabled=False` 时直接跳过、不查询 manager；`True` 但端口未装配时降级并留日志（替代此前的完全静默）。同时**删掉消费方的旧 try/except**：它包住的是 `EntityStoreProducer.dep`（Producer 解析 + 客户端构造），改造后端口构造已搬到 manager 装配期，这层兜不住；剩下的 `manager.entity()`（字典查表）与对象构造都不做 IO，留着只会把配置错误静默吞成"entity 关闭"。

## 拒绝的方案

### A 阶段（原 F07）

- **只拆接口不拆实例（一个类同时实现两个 ABC）**：等于没拆，类型层面依赖仍模糊。
- **保留 `Storage` 联合 ABC 渐进迁移**：留退路等于迁移被无限期推迟，一次性切、不留兼容期。
- **数据面逻辑搬到调用方（`PipelineRetriever` 自己实现 recall 编排）**：破坏 `DomainStore` 抽象边界；多路召回 + 融合 + 复核逻辑集中在 `CompositeDomainStore` 更可维护。
- **`bind_recallers` 下沉到 `DomainStore` ABC**：`RoutingDomainStore` 不能 bind，下沉会强制实现一个永远不该调的方法。
- **Recaller 运行期持有 manager / `PipelineRetriever` 同时持 manager + domain_store**：运行期实际只需要点读真源/数据面；持续持有 manager 让「装配期需要 manager、运行期需要 domain_store」的边界在类型层面无法表达。
- **删 `bind_recallers`、完全靠工厂装配**：手工接线（测试、`make_world` fixture）仍需要它；作为「手工/测试接线口」保留。
- **保留端口双入口（property + `*_port` 并存）**：双入口让「获取 Store 的唯一路径」无法成立；合并后成员 24 → 12。
- **仅保留 `*_port` 长名**：语义等价但写法变长，且与 `domain_store()` 命名风格不一致。
- **DomainStore 走 YAML 平级独立段装配**：弱化「所有存储类从 StoreManager 获取」原则，且 recaller 装配要读 manager 侧开关键，平级段会把装配链路撕成两半。改为 manager 工厂内构建（保留 `domain_store_target` 可换实现）。

### B 阶段（原 F08）

- **manager params 显式端口白名单（`ports: {kv: [aux]}`）**：与全量自动语义等价但多一层配置；「声明即端口」更简单。
- **`domain_store(name)` 做跨配置栈选择**：需要 manager 实例跨进全局命名空间，破坏封装；整栈切换已有路径（消费者 params 引用不同 manager 名 / F02 Routing）。
- **`StoreManagerProducer.TOP_NAME` 保留 `"storage"`**（A 阶段原选择）：配置段名与类名分裂易混淆；接受 YAML 兼容性破坏（`storage:` 段与 `storage.active` 键需改写），一次性切换。
- **消费者构造收解析好的实例（实例注入形态）**：测试可直接传 fake Store，但改动面更大（约 10 个构造签名重排）；选 manager + name 参数形态。
- **端口选择键回退 globals**：端口选择是消费者实例级决策，回退 globals 会让全局键静默覆盖实例级选择。
- **保留 `params.storage` 引用语义兼容**：留着等于给「第二套 manager」留后门，与全局唯一语义矛盾。
- **Recaller 端口名仅由 layer 推导**：自定义分表/多向量空间场景需要显式指名；显式覆盖优先、缺省推导，两层并存。

### C 阶段（原 F09）

- **保留 DomainStore 依赖（行为等价，不动）**：`CompositeDomainStore` 下行为确实等价，但依赖面更大——未来 DomainStore 获得非 KV 语义（如一体化后端自带索引投影）时，control 面会静默继承它未声明消费的能力。
- **消费者持 manager、调时再取端口**：违反「运行期持最小接口」——manager 是装配期对象，端口应在构造期固化（`kv(name)` 返回的 `_LazyStorePort` 代理已保证 active 切换时跟随重解析）。
- **lifecycle 回写仍走 `DomainStore.update(FORWARD_ONLY)`**：CompositeDomainStore 对 FORWARD_ONLY/ALL 行为相同，调用实际是裸 KV 往返 + 授权代理二跳；且 control 面若为此单独保留 DomainStore 注入点，决策 C-1 的收窄就不彻底。
- **在 DomainStore ABC 上加纯 KV 便捷方法（`load_units`/`list_units` 成员）**：会把「持最小接口」退化回「持 DomainStore」；模块级函数收 `KVStore` 入参，端口消费者与数据面消费者都可复用。

### D 阶段（EntityStore 纳管）

- **只收敛实例唯一性，不给 capability 席位**（在 manager `_build` 里统一建一次再注入两个消费方）：能消除实例分裂，但 EntityStore 仍拿不到授权代理与 health 聚合，三处存量违例只消除一处；且"受 manager 管理却不在 capability 里"会新造一种形态。
- **仿 `domain_store` 的独立实例表**（`entity(name)`/`has_entity(name)` + 独立 `_entity_stores`，不进 `StorageCapability`）：`domain_store` 是数据面编排对象，EntityStore 是后端 Store，二者不同类；走这条路 `has_entity` 无法由 capability 集合推导，与另六个 `has_*` 的写法分叉。
- **把四个方法首参改成 `scope: Scope`**（`space_id_from_scope` 与 `EntityStoreFilters.from_scope` 下沉实现内部）：接口最统一、代理无需特例，但会抹掉 entity 索引 `space_id` routing 与 Scope 五段的语义差异，且改动面扩到 ES 实现 + 2 个消费方 + 3 个测试桩。签名冻结、代价由代理承担是更小的切口。
- **在通用 `_AuthorizedStoreProxy` / `_action_for_store_method` 里加 entity 分支**：见决策 D-3。
- **`execute_operations` 统一映射 `ADMIN`**：授 ADMIN 等于解锁全部 Store 的所有未映射方法，写入是常规数据面动作，不该要管理员权限。
- **`execute_operations` 用固定的 `{ADD, UPDATE, DELETE}` 并集**：纯 INSERT 的 batch 会被迫要求 DELETE 权限，`DenyWritesSecurity` 这类策略对混合 batch 的判定也不精确。
- **`find_*` 映射 `GET`**：既有 GET 是「按 id 点查 / 枚举」，把反向索引的批量反查归进去，会让只授了「读自己记录」的身份意外获得全库反查能力。
- **deploy 全量抄写 `store_manager.default.params` 后再加 `entity_store` 键**：把最坏的配置陷阱引入全栈拓扑的根——两份 config.yml × 12 个键需与 defaults.py 手工同步，漏抄 `keyword_l0_recaller` 之类不会报错、只会静默丢一路召回。
- **`defaults.py` 加 `entity_store: _D` 与顶层 `{_D: "elasticsearch"}` 默认段**：在纯内存默认栈里声明一个永远连不上的 ES（决策 D 不做内存 entity 实现，没有合法后端可指），且每次装配、每个单测都刷一条 `hosts not configured` warning；消噪的两条路——降 debug 会丢掉"开了 entity_enabled 却忘配 hosts"的真实信号，让 ES builder 读 `entity_enabled` 则是存储后端反向依赖上层开关的层级倒置。
- **纯「命名空间声明即端口」（只读 `ctx.namespaces` 不看 params）**：是决策 D-6 的子集，但堵死了把 ENTITY 端口指到 `entity_store.aux` 的自由度。
- **新建 `InMemoryEntityStore` 生产实现**：本次不做。storage 层测试用手工构造的 spy 覆盖端口契约，装配链路用 ES 实现验证（构造期零 IO，`client` 是惰性 property）。代价见「已知遗留」。
- **给 entity 开 health 豁免**：见决策 D-8。

## 验证

- **拆分冒烟（A）**：手工构造 manager → 端口访问、代理身份稳定、未声明端口抛 `UnsupportedStorageCapabilityError`、`domain_store()` 未绑定报错/绑定后缓存稳定、领域 CRUD 正确；完整装配链路 7 路 recaller 同步装配、选择键指向未注册实现 fail-fast；端到端 add → retrieve 命中；Routing 委托链路与 active 切换计数验证。
- **具名键全链路（B）**：`dedup.params.{kv_store: aux, vector_store: my_idx}` 断言实例身份 = `manager.kv("aux")` / `manager.vector("my_idx")`；`engine.params.domain_store: fast` 的 profile 与 default 互异、物理 KV 互通；未声明名 fail-fast；globals 三态（default/自定义名/未声明报错）；`domain_stores` 段含 `"default"` 键拒绝；inline dict 端口键拒绝；四处点读回归；recaller 覆盖优先。
- **控制面直连（C）**：新增 `tests/unit/storage/test_kv_helpers.py`（`list_units` 反序列化/非 unit 过滤/memory_types+filters 透传/scope 隔离；`load_units` 保序/缺失省略/重复 id/空入参）；10 个 control 测试文件约 30 处构造点从 `domain_store=make_storage(kv=...).domain_store()` 简化为 `kv=kv`；三个测试替身的交付通道同步改为 KV 直写（`memory_key` + `dumps`），与真实 `ForwardIndexBuilder` 行为一致。
- **EntityStore 纳管（D）**：新增 `tests/unit/storage/test_entity_port.py`（17 例）——能力发现与端口暴露（含代理身份稳定、`store_type()` 返 ENTITY）、缺失时抛 `UnsupportedStorageCapabilityError`、命名端口真值表、`entity_ports` 传 None 被丢弃且 health 不炸、两个 `find_*` → SEARCH 且 scope 近似为 `Scope(space, user)`、`ensure_index` → ADMIN、混合 batch 派生 `{ADD,UPDATE,DELETE}`（LINK/UNLINK_UPDATE 去重为 3 次授权）、空 batch 零授权但仍委托、`DenyWritesSecurity` 下拒写放行读且**授权先于委托**（被拒调用不触达后端）、health 聚合与 `id()` 去重、从 `entity_store.default` 命名空间装出端口、无 hosts 装配期降级、命名端口无 hosts 被丢弃、params 引用优先于 default 兜底（判别式：default 缺 hosts 而 aux 有）、deploy 形状配置能装出端口。装配用例走完整 `AssemblyContext → build_named → has_entity()` 链路而**不连真实 ES**（`ElasticsearchEntityStore.__init__` 只存字段，`client` 是惰性 property，构造期零 IO；这类用例不调 `health()`——那会 ping）。
  `tests/unit/construction/test_hybrid_entity_wiring.py` 新增 4 例装配侧覆盖，核心是 **`test_builder_and_recaller_share_the_same_entity_port`**：预置含 ENTITY 端口的 manager，分别 build `hybrid` 与 `keyword` 两个 producer，写入侧 `build()` 后清空调用记录、再走真实 `recall()`，断言召回侧的 `find_by_entity_text_hash` 落在**同一个** store 实例上——这正是 D 阶段要拿到的收益，改造前两侧各自 `dep` 会各建一个匿名实例。另加"端口未装配时降级不抛"与"`entity_enabled=false` 时不查询端口"两例。
  `tests/unit/config/test_storage_routing.py` 加 `test_entity_port_follows_active`（ENTITY 端口随 `store_manager.active` 切换，构造期缓存的惰性端口在切换后解析到新实例；`_LazyStorePort` 对 `space_id` 首参透明转发）。三个测试 fake 同步：`UnitOnlyStoreManager` 实现 `entity()`（不实现则抽象方法致收集期 TypeError）、两个内存 entity 桩改继承 `EntityStore` 并返 `StoreType.ENTITY`。
- **基线（D）**：`pytest tests/unit/storage/ tests/unit/config/` 225 passed / 1 skipped；`pytest tests/unit/construction/ tests/unit/retrieval/` 485 passed；`pytest tests/unit/` 全量 **1965 passed / 8 skipped**（零失败）；`pytest tests/integration/` 通过。`ruff check jiuwen_memory/` 改造前后同为 86 errors——零新增（改动文件的残留问题逐文件与 HEAD 比对确认为存量）。
- **基线（A/B/C）**：`pytest tests/unit/storage/ tests/unit/control/ tests/unit/construction/test_infer_context_extract.py` 392 passed / 6 skipped；`tests/unit/api/` + `tests/unit/retrieval/` 421 passed；`tests/integration/retrieval/` + `tests/unit/construction/` 366 passed；`evaluation/smoke_test` 10/10；`ruff` 改动文件零新增问题（顺带修复 6 处存量：4×I001、1×E501、1×F821）。

## 已知遗留

- **「本体不落 KV」的一体化 DomainStore 会破坏 C 阶段前提**：直连 KV 的等价性依据是 S06 不变量 28（真源恒为 KV `/memory/` 前缀）+ 当前唯一注册的 domain_store 实现是 composite。未来 F05 愿景中「本体进一体化后端、不落独立 KV」的实现落地时，control 面直连 KV 会读不到真源，届时需重新评估（Engine/Jobs/Lifecycle 应改持 DomainStore 或由装配注入统一读端口）。
- **授权语义变化（B-6 / C-1）**：点读与列表读的授权 resource 从 `DomainStore` 领域动作（`memory_unit` 等）变为 KV 端口代理动作（`kv`，GET/LIST/UPDATE/ADMIN 标签）；且 `_AuthorizedStoreProxy` 对 `load_units` 是每 key 一次授权事件（原 `DomainStore.get/list` 每调用一次），审计事件数量级变大。默认 `AllowAllStorageSecurity` 下零感知，自定义 security 策略需按新 resource/动作名调整。这是收紧而非放松。
- **YAML 兼容性破坏（B-1）**：用户配置的 `storage:` 顶层段与 `storage.active` 键需改写为 `store_manager:` / `store_manager.active`；旧段装配期明确报错（fail-fast），旧 `params.storage` 键静默无效。
- **`resolve` 不再匿名兜底**：空 `AssemblyContext`（无 `store_manager` 段）下调用 resolve 报错——手工装配场景需显式声明或 `put` 预置。
- **`CompositeDomainStore._raw_kv()` 直接访问 `manager._stores`**（私有属性，跨类）：避免领域方法与授权代理的双重授权；同包实现协作的权宜。若未来引入第三方 DomainStore 实现，需评估在 StoreManager 上提供受控的 raw 端口访问。
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
- **顺带修复**（D）：两份 deploy config 的 `constructor` / `recaller.keyword` 段仍写着 B 阶段已废的死键 `storage: default`（`StoreManagerProducer.resolve` 读的是 `store_manager`，且 globals 已指名），一并删除。
