# S06 — 存储层（Storage Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/storage/ |
| 最近一次修订日期 | 2026-11-12 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/construction/F07-memory-write-entry.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F05-cloud-engine-design.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/retrieval/F05-storage-retrieval-pipelines.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F08-memory-tree.md，docs/features/common/F04-security-interfaces-and-encryption.md，docs/features/storage/F03-postgres-backend.md，docs/features/storage/F04-storage-ssl.md，docs/features/storage/F05-unified-storage-design.md，docs/features/storage/F06-composite-recaller-assembly.md，docs/features/storage/F07-storage-manager-domain-store-split.md，docs/features/storage/F08-document-memory.md |
## Markdown 块更新契约

`MarkdownStore.replace_content(scope, md_filename, old_content, new_content, *,
new_title=None, old_title=None) -> bool` 定位并替换首个符合条件的块。
`new_title=None` 保留原标题，空串清空标题，非空标题归一为单行。
`old_title` 非 None 时联合匹配标题与正文；标题漂移时仅在可替换块正文唯一时
退化匹配，重复正文不猜测目标。候选统计与实际替换共用首个换行拆分口径，
无换行的裸正文块不参与；未提供旧标题时按正文首个命中替换。
未命中返回 False，`remove_content` 仍按正文删除首个命中。

`edit_content` 要求 old_text 在文件全文中唯一，支持任意子串及跨块编辑。
USER.md/MEMORY.md 单行正文变化时，可同步替换未显式编辑标题中的唯一旧事实片段；
概括性标题、daily 团队标题及跨块编辑标题不自动改写。
文件编辑后标题同步到 shadow，重复正文结合块位置和旧标题定位，不仅按哈希覆盖。
标题与正文同次文件写入，shadow 更新失败时恢复文件快照并补偿索引。

## 显式明文维护增量能力

KVStore 增量提供非抽象 `compare_exchange(scope, key, expected, replacement) -> bool`：
完整 Scope/key/value 原子匹配，保留原绝对到期时间；replacement=None 为比较删除。
缺失、过期、变化返回 false，不创建记录。无法提供原子性时明确报不支持，禁止读后
普通 update/delete 兜底。`migration_identity()` 返回仅供维护去重的物理位置身份，
不能输出连接秘密。既有 ABC 抽象方法集合不变。
SQLite 的 insert 在数据库写事务中判断缺失并以唯一主键插入，不能用 REPLACE 覆盖竞争
写入；CAS 的有效期时钟在实例锁与 SQLite 写锁取得后采样，保留原绝对到期时间。

受保护端口的可选 PlaintextMigrationTarget（storage/migration.py）契约包括
migration_validate_scope/migration_identity/migration_scopes/migration_scan_supported/
migration_records(scope, *, after="", max_value_bytes=...)/
migration_key(unit_id)/migrate_plaintext(scope, key, expected, *, operation,
existing_only=False)。记录扫描仅在已判权 Scope 内，密文仍需认证；迁移通过公开 provider
加密，写前及持久化回读校验，现有记录以 CAS 保留 TTL。existing_only=true 禁止把扫描后
消失或到期的记录重新插入。显式文件导入可插入缺失目标；clear 不删除密文。
MigrationVerificationError.committed=true 标识提交后的回读失败，不自动回滚为明文。

有界扫描须显式声明支持；KV 默认拒绝，不允许以全量 scan 兼容。migration_records 返回
MigrationRecord(key, value, cursor) 迭代器，cursor 表示该条之后的位置；value=None 表示
超过单条读取上限，后端须在传回 Python 前拒绝物化大值，工具报 record_too_large。
KV 的可选 migration_read(scope,key,*,max_value_bytes) 同样在后端限值；None 表示超大，
缺失抛 NotFoundError。转换前比较及提交后校验均须使用有界读取，不能退回普通 get。
内存/SQLite/PG 按至多 64 个 key 分页并逐条有界读取；Redis 用 SCAN 游标及未处理 key
列表保留页内进度，Lua STRLEN/GET 在一次原子调用中限制返回体。Redis SCAN 可重复键，
已有转换须保持幂等；同一批预算耗尽时应返回续跑位置，不能丢掉未处理页内键。

位置和保护依赖来自实际 manager 清单与公开插件能力，未知枚举/CAS 能力须明确失败。
FS 资产、索引投影和治理记录不能按 MemoryUnit 猜测迁移；索引维持部署加密契约。
非信封输入的专用 PlaintextDataError 仍在底层终止读取，沿原 BackendError 异常契约传播；
存储不得返回原明文、错误对象冒充 bytes 或空内容。

## Metadata 物理存储契约

索引记录保留 `system_metadata.<key>` 和 `user_metadata.<key>` 的逻辑路径。Milvus 与
PostgreSQL JSONB 使用完整路径作 key；Elasticsearch 写入时展开为对象层级，使
`metadata.user_metadata.<key>` 等 DSL 路径可下推。Store record 自身的 `metadata` 名称不变。
## 范围 / 边界

**管什么**：
- 管理面 `StoreManager` 契约（能力发现、命名端口暴露、统一授权代理、健康聚合）与数据面 `DomainStore` 契约（MemoryUnit 领域 CRUD 与检索适配），分处 `store_manager.py` / `domain_store.py` 两个独立 ABC
- `CompositeStoreManager` / `CompositeDomainStore` 默认组合实现
- `StoreManagerProducer` 注册与 `store_manager` 配置命名空间（全局唯一 manager，经 `globals.store_manager` 指名）
- MemoryUnit 领域 add/update/delete/get/list
- StorageSecurity 数据面授权和 StoreSecurity 数据保护能力边界
- 可配置真源（文档/结构化）的 KV 存储抽象
- KV 加密装饰器（EncryptedKVStore）
- 多后端索引存储抽象：向量（VectorStore）、全文（FulltextStore）、图（GraphStore）、融合（FusionStore）、文件系统（FSStore）、实体反向（EntityStore）、markdown 视图（MarkdownStore）、文档影子索引（DocumentShadowIndex）
- 统一 CRUD 动词（insert / delete / update / get）
- 检索型存储的 search 查询
- scope 原生隔离（scope 为显式第一入参，物理约束在该 scope 内）

**不管什么**：
- 不管理 grant/revoke、授权策略生命周期或业务权限模型
- 不做检索编排（由 `jiuwen_memory/retrieval` 层负责）
- 不做索引构建逻辑（由 `jiuwen_memory/construction` 层负责）
- 不实现具体后端（实现在 `*_impl/` 下，通过 Producer 注册）
- 不解释或维护父子业务语义；通用 CRUD 不执行 hierarchy 级联

## 不变量

1. **scope 原生隔离**：`scope: Scope` 为每个 Store 方法的显式第一入参，不放进记录/查询结构体、也不编进 `metadata` / `filters`。
2. **记录 id 在 scope 内唯一**：`insert` 冲突 / `update` 缺失按 `(scope, id)` 判定；后端可用 `scope + id` 生成物理主键，保证同一逻辑 id 在不同 space 下互不冲突。
3. **统一 CRUD 动词**：insert（增）/ delete（删）/ update（改）/ get（查），各存储接口保持同一命名。
4. **检索型存储额外提供 search**：fulltext / vector / graph / fusion 在 CRUD 之上再提供 `search` 查询。
5. **vector 的 recall 为可选能力**：`VectorStore.recall` 在 `search` 之上按需回带命中行 payload（`metadata`），基类默认抛 `NotImplementedError`，**不强制**每个后端实现；未实现者由调用方（`VectorRecaller`）捕获后回退 `search + get` 两段式，功能不退化。`search`/`get` 返回 `ScoredID`/`VectorRecord` 的契约不变。
6. **kv 区分 list 与 scan**：`list` 是 `/memory/` MemoryUnit 的过滤、计数、排序和分页查询；`scan` 是无业务语义的 scope 内原始 key-value 扫描；`scopes` 枚举已有 scope。`mget` 是 `get` 的批量互补：一次召回多条、省逐条 `get` 的接口往返。返回与 `keys` 下标一一对应的 `list[bytes]`，**不去重、按位置返回**（调用方可传重复 key、各下标独立返回，语义同 Redis `MGET`）；缺失语义与 `get` 一致——任一 key 不存在即抛 `NotFoundError`，不静默省略（「索引↔真源短暂不一致」的兜底由调用方负责）。重复 key 的去重亦由调用方（如 `UnitReader.load`）负责，不下沉到本接口。
7. **fs 提供 stat**：stat（文件元信息查询）。
8. **scope 对 key/路径做命名空间隔离**：kv / fs 是通用原语，`scope` 入参用于对 key / 路径做命名空间隔离（同一逻辑 key 在不同 scope 下是相互隔离的不同物理键）。
9. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
10. **生产过滤先于截断**：生产检索后端必须完整编译所支持的 `FilterExpr`，并在
   `limit/top_k` 前执行；不允许依赖检索层后置过滤替代生产下推。
11. **metadata 原生类型入库**：Document / VectorRecord 的 metadata 保留 JSON 标量
    原生类型，不统一字符串化；不同类型之间不做隐式比较转换。
12. **metadata 过滤区分标量与数组**：`EQ` / `IN` 的正向匹配只命中标量，
    `CONTAINS` 只命中数组成员；`NE` / `NOT_IN` 是对应正向谓词的逻辑否定；范围算子
    只作用于标量，数组字段不按「任一成员命中」判定。后端若原生不保留单值/数组形态，
    必须用内部派生字段恢复语义。
13. **所有 Store 必须实现 `store_type()` 和 `health()`**：继承自 `BaseStore`。
14. **多租户隔离默认依赖逻辑 scope 边界**：当前不要求物理分库/分 collection，但要求同一逻辑 key/id 在不同 scope 下严格命名空间隔离。
15. **EncryptedKVStore 只装饰 KV，不实现算法**：写前加密、读后解密通过注入的 `CryptographyProvider` 完成；`list` 在解密后执行 MemoryUnit 过滤，不能把过滤下推到密文 raw KV。
16. **space 是 scope 的硬分区维度**：`scope_segments(scope)` 使用 `org/space/user/agent/session` 五段；`scope_dims(scope)` 在 `org` 非空时即使 `space==""` 也下推 `space == ""`，避免空 space 查询跨到非空 space。
17. **标识唯一性分层**：非空 Space id 在 Space 资源注册表中全局唯一；MemoryUnit 与各 Store 记录 id 只要求在完整 Scope 内唯一。
18. **SSL 声明即生效**：接外部后端的实现统一接受 `ssl_verify` / `ssl_ca_cert` 两个装配参数（默认关闭）。`ssl_verify` 只表示**是否校验服务端证书**，不负责开启加密——加密开关落在连接串上（`rediss://` / `https://` / `sslmode=`）。开启后不得静默降级：缺证书、连接串仍为明文、或连接串自带会覆盖本设置的 TLS 参数，一律在**装配阶段**报错。
19. **KV 是层级真源**（已实现）：序列化 `MemoryUnit.hierarchy` 与 unit
    一同存入 KV。当前契约不新增 hierarchy Store，也不把父子包含边双写到 GraphStore；
    若未来迁移到独立边存储，必须先修订本 spec 和 S07 的数据模型契约。
20. **层级索引是派生物**：VectorRecord/Document 的 hierarchy metadata 必须能够从 KV
    中的 `MemoryUnit` 全量重建；索引丢失或不一致时以 KV 为准。
21. **GraphStore 边界明确**：GraphStore 表示关联和多跳关系，不表示 hierarchy containment；
    `HierarchyRef.parent_id/child_ids` 不投影为图边。
22. **CRUD 不级联层级关系**：KVStore 的 insert/update/delete 只作用于指定 key。删除父或子
    不会自动改写其他 unit；父子双向边维护、剪枝与修复由 construction/control 调用显式
    CRUD 完成。GraphStore 删除节点时清理关联图边的既有语义不适用于 hierarchy。
23. **Storage capability 唯一来源**：能力集合只包含 KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS/ENTITY/MARKDOWN/DOCUMENT_SHADOW
    （ENTITY 为 F07 新增第七席；MARKDOWN/DOCUMENT_SHADOW 为 F08 新增第八、第九席，仅文档模式
    `write_document=true` 装配，见 F08-document-memory.md）。capability 与 `has_*()` **同源于端口表**——某类存储只要有
    任一端口可用即拥有该 capability，二者不会分叉；
    `has_*(name)` 由集合/命名端口表推导，未声明端口访问抛 `UnsupportedStorageCapabilityError`。
24. **端口单一入口且全量自动**：每个 capability 一对 `xxx(name="default")` / `has_xxx(name="default")`
    方法（无 property 快捷方式、无 `*_port` 后缀双入口）；九类 `*_store` 命名空间下**所有**
    具名实例（含 `default`）**声明即端口**，端口名即实例名（encrypted KV 的明文 raw 若以具名
    声明会随之暴露，raw 推荐在 kv 实例 params 内 inline 声明）；上层不得绕过
    `StoreManagerProducer` 直接解析 Store 具名实例。
    **manager params 不再用 `<ns>_store` 引用键决定默认端口**：`default` 只是端口表中名为
    `"default"` 的普通实例。理由是配置合并为实例级整体覆盖（`AssemblyContext.merged` 按实例名
    update 整个 `RawSpec`，无 params 深合并），部署一旦覆写 `store_manager.default.params`
    就须全量抄写其全部键，漏一个即静默丢能力。**代价**：manager params 里 inline 声明 Store
    （`kv_store: {target: ...}`）的写法失效，后端一律经命名空间声明。
    端口表的值必须非 None——builder 返 None（增强层未配即降级）的实例在
    `_collect_ports` 统一丢弃。管理面**不校验任何 capability 的必需性**——它只如实报告
    装配出的能力，「哪类存储不可或缺」是消费方的约束（数据面缺 KV 真源时，其领域方法自会
    抛 `UnsupportedStorageCapabilityError` 并指明缺失端口）；两条入口据此行为一致。
25. **检索路径独立于 capability**：`DomainStore` 提供 recall/recall_and_get/retrieve，并以
    实例级稳定的 `preferred_retrieval_pipeline()` 选择首选入口；路径值不加入 capability。
26. **统一授权不可绕过**：`DomainStore` 领域接口和 `StoreManager` 暴露的 Store 代理端口都先执行
    `StorageSecurity.authorize`；默认 AllowAll 可省略 access。Store 自身 `security` 表示数据保护。
    **ENTITY 端口的授权适配**（`_AuthorizedEntityStoreProxy`，与通用代理分开实现）：
    `find_by_entity_text_hash` / `find_by_linked_memory_id` → `SEARCH`；`execute_operations`
    → 按 batch 内 op 类型派生动作集逐个授权（`INSERT`→ADD、`LINK`/`UNLINK`/`UNLINK_UPDATE`→UPDATE、
    `DELETE_IF_EMPTY`/`DELETE`→DELETE；空 batch 零授权，因为它不执行任何动作）；整批先授权、
    全部通过才执行；`ensure_index` → `ADMIN`。交给 `authorize` 的 Scope 是方法首参的**真实
    Scope**（契约 v2 起，不再由 `space_id` 有损近似）。
27. **写接口覆盖范围由实现决定**：`DomainStore.add`/`update`/`delete` 落成哪些索引形式取决于该
    实现的能力，调用方不得假定「只写记忆本体」。`IndexWriteMode` / `IndexRemoveMode` 表达调用方
    意图，能否拆分由实现按自身能力决定——不具备检索索引能力的实现在 `RETRIEVAL_ONLY` /
    `SOFT` 时为空操作。差额由 IndexBuilder 补齐，匹配关系由装配期约定保证。
28. **正排的 key 方案与编解码是跨层共享契约**：`memory_key` 与 `memory_codec` 归口
    `common.type_def`，写侧在 `ForwardIndexBuilder` 与 `KVLifecycleManager` 回写、读侧在
    `DomainStore.get`/`list` 与 `load_units`/`list_units` helper，`KVStore.list`
    本身也按 `MEMORY_KEY_PREFIX` 扫描。这是正排作为唯一需要**两向投影**的索引形式的固有代价：
    实现分居两层，靠这对共享契约对齐。
29. **命名数据面使用受管 Store 集**：`manager.domain_store(name)` 的多套命名数据面可分别选择
    受管真源 KV 端口与检索 profile（`preferred_retrieval_pipeline` + recallers 组合）；同名多次调用返回同一实例。
    跨 Store 集的整栈切换走 F02 Routing（`store_manager.active`），不属命名数据面语义。
30. **所有 XXXStore 获取经 StoreManager**：消费者不直接调 Store Producer 解析具名后端
    （F07 起 `EntityStore` 一并纳入——写入侧 `HybridIndexBuilder` 与召回侧 `KeywordRecaller`
    改经 `manager.entity(name)`，`EntityStoreProducer.dep` 旁路移除，读写共享同一实例由
    manager 保证而非配置纪律）；
    端口选择键（`params.kv_store` / `vector_store` / `entity_store` / ... / `domain_store`）的值是 manager 端口/
    数据面名（仅字符串，inline dict 拒绝）。`CloudEngine` 的 MemoryUnit 点读、列表/全量扫描
    与 Scope 枚举注入 DomainStore，并通过 `get` / `list` / `scopes` 访问；`InMemoryEngine`、
    原始消息、LifecycleManager、EvolveJob、MiddleToLongJob 等 KV 专用场景仍直接使用
    KVStore 端口。DomainStore 同时服务 CloudEngine 的 MemoryUnit 读取、检索路径
    （PipelineRetriever）与一体化写路径（UnifiedIndexBuilder）。
31. **health 聚合覆盖全部已声明端口**：增强层的降级发生在**装配期**（builder 返 None →
    无该 capability → 消费方跳过），不在探活期；端口一旦声明且构造成功，后端不可达就应让
    `manager.health()` 报错，不给任何 capability 开健康豁免。运行期容错仍由消费方各自的
    try/except 承担（entity 后端故障不影响正排/倒排/向量的写入与召回）。
    **部署注意**：把 `health()` 当 liveness probe 的部署，增强层后端抖动会触发重启；
    建议用作 readiness，或在部署侧区分 required/optional 探活。

## 接口契约

### 部署依赖加载时机

配置选中的 ES 全文、ES 实体、Milvus 向量及 `milvus_graph` 融合后端在同步装配阶段
预加载 ES/Milvus 客户端依赖（融合后端的图依赖仍惰性加载），
避免长命服务首次请求在线程池中冷加载原生库；未选中的后端不因此引入部署依赖。
预加载只导入模块，不建连、不建索引/集合；客户端连接及索引初始化仍为首次使用时执行。
依赖导入或原生库加载失败在装配阶段以 `BackendError` 报错并保留原始异常链。
ES 实体未配置 hosts/endpoint 时仍关闭该端口，不要求安装客户端。
直接构造 Store 的手工接线仍保留惰性导入语义。
这是将运行期冷加载前移到启动期的缓解措施，不代表已定位或修复 Windows loader
底层慢加载机制；部署应为启动依赖加载保留时间。

### StoreManager（管理面，`store_manager.py`）

| 类别 | 接口 | 语义 |
|---|---|---|
| 端口 | `kv/vector/fulltext/graph/fusion/fs/entity/markdown/shadow_index(name="default")` 及 `has_*(name="default")` | 暴露经过统一授权代理的完整 Store 契约；单一入口（无 property、无 `*_port` 后缀）；未声明端口抛 `UnsupportedStorageCapabilityError` |
| 数据面 | `domain_store(name="default")` / `has_domain_store(name="default")` | 取命名数据面实例；实现需缓存（同名多次返回同一实例） |
| KV 真源领域门面 | `domain_store_for_kv(name="default") -> DomainStore` | 绑定指定受管 KV 端口及 MemoryUnit codec，保留领域与端口授权；不继承默认数据面的 KV 选择、文档模式或召回配置，不注册/替换具名数据面，不承诺实例缓存；未实现时抛 UnsupportedStorageCapabilityError，公共基类不选择具体数据面实现 |
| 能力 | `capabilities()` | 返回不可变标准端口能力集合 |
| 横切 | `security` / `health()` | 统一授权入口并聚合全部命名端口的健康检查 |

`CompositeStoreManager` 是默认实现（组合九类 Store + 端口表 + `_AuthorizedStoreProxy`，
ENTITY 端口用 `_AuthorizedEntityStoreProxy`），提供两条入口：`__init__` 收**已构造的 Store
实例**（手工接线与测试路径，每类接单实例或 `{name: store}` 端口表）；`from_config(config)`
从装配配置内部构造全部 Store 与全部命名数据面（装配路径，注册的 `_build` 即其一行封装）。
数据面在 manager 自身就绪后逐套构造——`domain_store_target` 为 `composite`（默认）时经
`CompositeDomainStore.for_manager(manager, ds_config)` 直接构造，该方法内部一次完成
profile 派生、召回路组装与绑定；其余 target 走 `DomainStoreProducer`（自带检索路径，
不装召回路）。两种情形都经 `bind_domain_store(ds, name)` 注入。
数据面的真源读写与 control 面走同一条路径——经 `manager.kv(name)` 端口（授权代理在内）：
领域方法先按 `memory_unit` 授权、端口再按 `kv` 授权，两层 resource 不同，分层授权是预期
语义；管理面不提供绕过代理的原始端口出口。该 `name` 由装配期 `resolve_name(ds_config,
"kv_store")` 指名（与其余消费方同一套选择键机制），不在运行期硬编码 `"default"`；
该 `name` 与其余数据面参数一样声明在 `domain_stores.<name>` 里。`StoreManagerProducer.TOP_NAME = "store_manager"`（F07 起从 `storage` 更名）；统一
manager 以 target 自注册，默认 `composite`。

**全局唯一 manager（F07）**：进程内共享一个 StoreManager，由 `globals.store_manager` 指名
（值 = store_manager 命名空间下的实例名）。`StoreManagerProducer.resolve(config)` 按
params 显式覆盖 → `globals.store_manager` → `"default"` 三级链解析；未声明实例名抛
`ValidationError`，不做匿名兜底构建。消费者不再逐个声明 `storage` 引用。

**数据面装配参数归位（F07）**：`store_manager.<inst>.params` **不承载任何数据面配置**——
九类端口由命名空间全量聚合（声明即端口），与 params 无关。数据面的全部装配参数住在
`store_manager.<inst>.params.domain_stores: {<name>: {...}}`：`preferred_retrieval_pipeline`
/ `kv_store` / `domain_store_target` / 七个 `*_recaller` 选择键。`default` 可显式声明且
**必建**（整段缺省时按全默认构造）。

**继承规则只有一条，对所有键统一生效**：`default` entry 是 base，命名 entry overlay 在其
上，再经 `ComponentConfig.get` 回退 `globals`（`vector_enabled` / `graph_enabled` /
`layers_index_enabled` 等跨切面开关走这条）。base 是必需而非便利——`Factory.dep` 读
`config.params` 直读不回退 globals，命名 entry 漏掉 `*_recaller` 键会让 `dep` 落到
`cls.build(default, {}, ctx)` 匿名新建一套不共享的 recaller，装配不报错、退化全静默。

**消费者具名选择键**：`resolve_name(config, key)` 统一读取——键名与命名空间一致
（`kv_store` / `vector_store` / `fulltext_store` / `graph_store` / `fusion_store` / `fs_store` /
`entity_store` / `domain_store`），值必须是 manager 端口/数据面名字符串（params 直读不回退
globals，inline dict 拒绝），缺省 `"default"`。注意与跨切面开关的读法不对称：`entity_enabled`
走 `config.get`（回退 globals，表达"要不要用"），端口选择键走 `resolve_name`（params 直读，
表达"用哪个端口"）；二者是 AND 关系且 `entity_enabled` 优先短路。

### DomainStore（数据面，`domain_store.py`）

| 类别 | 接口 | 语义 |
|---|---|---|
| 领域操作 | `add/update(..., mode: IndexWriteMode = ALL)` / `delete(..., mode: IndexRemoveMode = HARD)` / `get` / `list` / `scopes` | 操作或枚举 MemoryUnit 真源；get 保序并省略缺失，list 返回 items 与 count |
| 检索适配 | `preferred_retrieval_pipeline()` / `recall` / `recall_and_get` / `retrieve` | 供 Retriever 选择 recall/get/rank 三步的组合位置 |
| 横切 | `security` / `health()` | 委托 manager 的统一授权与聚合健康检查 |

`CompositeDomainStore` 是默认实现。一体化实现可以只实现领域和首选检索入口；只有完整提供
某个标准 Store 契约时才在对应 manager 上声明 capability。

`bind_recallers` 仅落 `CompositeDomainStore`（手工/测试接线口；装配路径由 `for_manager` 内部调用），不下沉 `DomainStore` ABC；
`RoutingDomainStore` 不实现（active 切换语义要求各预装实例装配期各自绑定）。

**写接口的覆盖范围是实现相关的**：`add`/`update`/`delete` 的语义是「按该实现的能力落地」，
而非「只写记忆本体」。`CompositeDomainStore` 不持有 Chunker/Embedder，无投影能力，故只落本体；
一体化平台可在一次 `add` 内建立全部索引形式。差额由 `IndexBuilder` 补齐，两者的匹配由
**装配期约定**保证（见 S05 不变量 15），不引入运行时能力协商。

两个枚举把调用方意图透传到实现：`IndexWriteMode`（`ALL` / `FORWARD_ONLY` /
`RETRIEVAL_ONLY`）表达写入范围，`IndexRemoveMode`（`SOFT` / `HARD`）表达删除语义——
`SOFT` 软删除只移出检索索引（search/recall 不再召回），本体保留、get/list 仍可读。
`UnifiedIndexBuilder` 原样下传，不代实现判断；不具备检索索引能力的实现在
`RETRIEVAL_ONLY` / `SOFT` 时为空操作，而 `FORWARD_ONLY` 时应**至少保证本体被写到**——
多刷新一次检索索引无害，漏写本体则丢数据。

**原文**（对话消息）不属于存储层的领域范围：它既非 MemoryUnit 真源也非索引，不建索引、
不参与检索，仅供构建层做指代消解与语境补全，条数上限由写入方维护。故不占存储层接口——
构建层注入一个独立的 `KVStore` 直接读写（见 [F07](../features/construction/F07-memory-write-entry.md)）。

### Recaller（数据面检索适配器，`domain_store_impl/recaller.py`）

单路召回算子。一个 Recaller 对应一条召回通道，消费 `ParsedQuery` 中本通道需要的字段，
经 `StoreManager` 的对应命名端口召回候选。

| 方法 | 签名 | 语义 |
|------|------|------|
| `channel` | `() -> RecallChannel` | 返回本召回路对应的通道 |
| `recall` | `(scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]` | 在 scope 范围内本通道内召回 top-k 候选 |
| `health` | `() -> None` | 存活探测；健康返回 None，否则抛异常 |

**归属在存储层而非检索层（F07）**：生产链路里 Recaller 实例的唯一消费方是
`CompositeDomainStore`，`PipelineRetriever` 只按 `preferred_retrieval_pipeline()` 委托数据面的
`recall` / `recall_and_get` / `retrieve`，不持有召回路。故契约与实现都在
`storage/domain_store_impl/`，且**不继承** `RetrievalOperator`——自描述的 `operator_type()`
对一个不进检索算子表的组件没有意义，`RetrievalOperatorType` 相应不含 `RECALLER`。
`RecallerProducer.TOP_NAME` 仍是 `recaller`：YAML 命名空间与注册名不变，注册随
`storage.bootstrap.register_backends` 触发。

**装配**：`CompositeDomainStore.for_manager(manager, ds_config)` 内按能力开关
（`vector_enabled` / `graph_enabled` / `layers_index_enabled`，`config.get` 回退 globals）与
`domain_stores.<name>` 的七个 `*_recaller` 选择键同步组装，构建期 fail-fast。手工/测试接线
走 `bind_recallers`（同一实例不允许绑定两套不同 recaller）。

`scope` 是 `Recaller.recall` 的显式第一入参，不随查询对象携带、不混进 `filters`——与
S04 的「scope 是独立轴」同一条铁律。召回分数统一「分越大越相关」，chunk→unit 归并由
同目录 `unit_aggregation.aggregate_to_units` 按 `metadata['unit_id']` 取 MaxP。


### 共享读 helper（`kv.py::load_memory_unit` / `load_units` / `list_units`）

`load_memory_unit(raw, scope=..., key=...)` 是字节级损坏的隔离口：`loads` 保持严格 UTF-8，`UnicodeDecodeError` 记 warning（含脱敏 scope、可见 MemoryUnit id、字节数和异常类型）并返回 `None`，调用方同其他非 MemoryUnit 记录一样过滤。`load_units` / `list_units`、DomainStore `get` / `list`、`UnitReader` 与 KV list 兼容实现都经它读取；get/list 对存在但不可解码的记录按缺失省略，search/recall 不因单条坏记录中断。

`load_units(kv, scope, unit_ids) -> list[MemoryUnit]`：按 unit_id 从 KV 真源点读
MemoryUnit 列表（`memory_key` + `loads`）——缺失省略、按输入顺序返回、重复 id 各自返回、
不做任何过滤/复核（lifecycle/valid-time 判定属调用方职责）。

`list_units(kv, scope, *, offset, limit, memory_types, filters, extensions) ->
tuple[list[MemoryUnit], int]`：列表读的对称件——`kv.list` + 逐条 `loads` 反序列化（非
MemoryUnit 记录自然过滤），返回 `(items, count)`；过滤/计数/分页语义全部由 `KVStore.list`
契约承担，helper 不做二次过滤。

两者供 InMemoryEngine、Dedup._load_unit / Governor._find / Evolver 源读 / KeywordRecaller
实体扩展等纯按 id 点读场景，以及 LifecycleManager sweep、EvolveJob/MiddleToLongJob
候选拉取等 KV 专用列表场景共用。CloudEngine 的 MemoryUnit 点读、全量扫描和分页 list
改经 DomainStore 的 `get` / `list` / `scopes`，不再调用这些 helper。

### BaseStore（基类，`base.py`）

```python
class StoreType(str, Enum):
    KV / FULLTEXT / VECTOR / GRAPH / FUSION / FS / ENTITY / MARKDOWN / DOCUMENT_SHADOW / PRIVACY

class BaseStore(ABC):
    def store_type(self) -> StoreType  # 自描述
    def health(self) -> None            # 存活探测：健康返回 None，否则抛 HealthCheckError
    @property
    def security(self) -> StoreSecurity # 后端数据保护模块；默认 passthrough
```

### KVStore（`kv.py`）

键值存储，统一 CRUD + MemoryUnit 列表查询 + 范围枚举。

| 方法 | 签名 | 语义 |
|------|------|------|
| `insert` | `(scope, key, value: bytes, ttl=0.0) -> None` | 在 scope 下新建 key；已存在时报冲突 |
| `update` | `(scope, key, value: bytes, ttl=0.0) -> None` | 覆写 scope 下已有 key；不存在时报缺失 |
| `delete` | `(scope, key) -> None` | 删除 scope 下的 key（幂等） |
| `get` | `(scope, key) -> bytes` | 读取 scope 下 key 的值；不存在时报缺失 |
| `mget` | `(scope, keys: list[str]) -> list[bytes]` | 批量读取 scope 下多个 key 的值；返回与 `keys` **按下标一一对应**。缺失语义与 `get` 一致：任一 key 不存在即抛 `NotFoundError`，不静默省略。一次召回省去逐条 `get` 的接口往返。**不去重、按位置返回**：调用方可传重复 key，各下标独立返回该 key 的值（语义同 Redis `MGET`）；重复 key 的去重由调用方负责，本接口不做 |
| `exists` | `(scope, key) -> bool` | 返回 scope 下 key 是否存在 |
| `list` | `(scope, *, offset=0, limit=100, memory_types=None, filters=None, extensions=None) -> KVMemoryListResult` | 查询 `/memory/` MemoryUnit；先执行 `memory_types AND filters`，再精确计数、稳定排序和分页 |
| `scan` | `(scope, prefix="") -> list[tuple[str, bytes]]` | 扫描 scope 下的全部 (key, value)（可选只取 prefix 开头的 key）；顺序由实现定义 |
| `scopes` | `() -> list[Scope]` | 枚举本存储中已用过的全部 scope |
| `supports_compare_and_swap` | `() -> bool` | 可选原子条件写能力；默认 False，不增加第三方插件的必填抽象方法 |
| `compare_and_swap` | `(scope, key, expected: bytes \| None, value: bytes \| None) -> bool` | 原子比较并替换；expected=None 表示不存在，value=None 表示删除；不匹配返回 False，后端失败抛异常 |

**ttl** 单位为秒（float），`0` 表示永不过期。

条件写按完整 Scope 隔离，过期值等同不存在，空 bytes 不等同不存在；成功替换清除
TTL。未声明能力的后端拒绝调用，禁止用无锁 get/update 模拟原子性。加密包装层比较
逻辑明文并在底层对原密文执行条件写，不得只比较明文后无条件覆盖。隐私业务 KV
包装层拒绝对 `/memory/`、`/messages/` 使用此方法绕过正常保护流程；其他键透传。

#### EncryptedKVStore（`kv_impl/encrypted_kv_store.py`）

`encrypted` 是 KVStore 装饰器实现，用于把加密能力透明套到任意 raw KV 后端之上。

| 方法 | 行为 |
|------|------|
| `insert` / `update` | 构造 `CryptoContext(scope, purpose, object_id, format_version)` 与 AAD，调用 `CryptographyProvider.encrypt` 后写入 raw KV |
| `get` / `scan` / `mget` | 从 raw KV 读取密文字节，调用 `CryptographyProvider.decrypt` 后返回明文字节；任一解密失败抛 `BackendError`，不跳过坏数据。`mget` 委托 raw 一次性批量取密文（raw 缺失即抛 `NotFoundError`）后**逐项解密**——AAD 绑定 scope+key+purpose，各 key AAD 不同，不能批量统一解密 |
| `list` | 扫描目标 Scope 的 `/memory/` 密文并逐条解密，再执行统一过滤、计数、排序和分页；不调用 raw KV 的 `list` |
| `exists` / `delete` / `scopes` | 直接委托 raw KV，不读取或改写 value |

装配参数：

| 参数 | 语义 |
|------|------|
| `raw_kv_store` | 必填，指向被装饰的 raw KVStore 具名实例或内联配置；不得指向当前 encrypted 实例自身 |
| `cryptography` | 必填，指向 `common.security.cryptography.CryptographyProvider` 具名实例或内联配置 |

AAD 版本当前为 `1`，绑定 `scope(org/space/user/agent/session)`、KV `key` 与 `purpose`。`purpose` 由 key 前缀推导：`/memory/` 为 `memory_unit`，`/messages/` 为 `raw_message`，其他为 `kv_value`。

### FulltextStore（`fulltext.py`）

全文倒排索引存储，统一 CRUD + 关键词检索。

| 方法 | 签名 | 语义 |
|------|------|------|
| `insert` | `(scope, docs: list[Document]) -> None` | 在 scope 下索引新文档；id 已存在时报冲突 |
| `update` | `(scope, docs: list[Document]) -> None` | 重建已有文档的索引；id 不存在时报缺失 |
| `delete` | `(scope, ids: list[str]) -> None` | 在 scope 内按 id 删除文档（幂等） |
| `get` | `(scope, ids: list[str]) -> list[Document]` | 在 scope 内按 id 点查文档；缺失的 id 从结果中省略 |
| `search` | `(scope, query: TextQuery) -> list[ScoredID]` | 在 scope 内做关键词检索（BM25 等），按相关性返回 top-k |

### VectorStore（`vector.py`）

向量存储，统一 CRUD + ANN 检索。

| 方法 | 签名 | 语义 |
|------|------|------|
| `insert` | `(scope, records: list[VectorRecord]) -> None` | 在 scope 下新建向量行；id 已存在时报冲突 |
| `update` | `(scope, records: list[VectorRecord]) -> None` | 替换已有向量行；id 不存在时报缺失 |
| `delete` | `(scope, ids: list[str]) -> None` | 在 scope 内按 id 删除向量行（幂等） |
| `get` | `(scope, ids: list[str]) -> list[VectorRecord]` | 在 scope 内按 id 点查向量行；缺失的 id 从结果中省略 |
| `search` | `(scope, query: VectorQuery) -> list[ScoredID]` | 在 scope 内做 ANN 近邻检索，按相似度返回 top-k |
| `recall` | `(scope, query: VectorQuery, output_fields: list[str]\|None=None) -> list[ScoredHit]` | 在 scope 内做 ANN 近邻检索，并按需在同一次请求内回带命中行 payload（当前仅认 `metadata`）；**可选能力**，基类默认抛 `NotImplementedError`，子类按需 override；未实现时调用方回退 `search + get` |

### GraphStore（`graph.py`）

属性图存储，节点与边统一 CRUD + 邻域遍历。

| 方法 | 签名 | 语义 |
|------|------|------|
| `seed_ids` | `(scope, tokens: set[str], *, extensions=None) -> list[str]` | 在 scope 内按关键词/词项定位种子节点 id；`extensions["shards"]` 供共享图在截断前过滤，调用方不分片时不传该参数（兼容旧签名插件） |
| `insert` | `(scope, nodes=None, edges=None) -> None` | 在 scope 下新建节点/边；id 已存在时报冲突 |
| `update` | `(scope, nodes=None, edges=None) -> None` | 更新已有节点/边；id 不存在时报缺失 |
| `delete` | `(scope, node_ids=None, edge_ids=None) -> None` | 在 scope 内按 id 删除节点（连带其关联边）/ 边（幂等） |
| `get` | `(scope, node_ids: list[str]) -> list[Node]` | 在 scope 内按 id 点查节点；缺失的 id 从结果中省略 |
| `search` | `(scope, query: GraphQuery) -> list[Node]` | 在 scope 内从 query.start_id 出发扩展邻域/子图（多跳遍历） |
| `incident_edges` | `(scope, node_id, *, limit=100) -> list[Edge]` | 返回保留 source/target 方向的有界入出边；`supports_incident_edges` 为真 |
| `project_edge` | `(scope, edge, *, revision, nodes=None) -> None` | 按修订条件投影（见「接续管理与证据图能力」）；`supports_conditional_projection` 为真 |
| `unproject_edge` | `(scope, edge_id, *, revision) -> None` | 按修订条件撤投影；`supports_conditional_projection` 为真 |

GraphStore 只承载 `ASSOCIATE` 等路径产生的语义关联、共指、因果或引用关系。父子包含
关系的读取与遍历以 KV 中 `MemoryUnit.hierarchy` 为准，不通过 GraphStore 搜索或修复。

### FusionStore（`fusion.py`）

向量·倒排·正排融合存储。

| 方法 | 签名 | 语义 |
|------|------|------|
| `insert` | `(scope, records: list[FusionRecord]) -> None` | 在 scope 下新建融合行（向量/文本/标量/正排值一次写入）；id 已存在时报冲突 |
| `update` | `(scope, records: list[FusionRecord]) -> None` | 替换已有融合行；id 不存在时报缺失 |
| `delete` | `(scope, ids: list[str]) -> None` | 在 scope 内按 id 删除融合行（幂等） |
| `get` | `(scope, ids: list[str]) -> list[FusionRecord]` | 在 scope 内正排点查：按 id 读取完整融合行；缺失的 id 从结果中省略 |
| `search` | `(scope, query: FusionQuery) -> list[ScoredID]` | 在 scope 内做融合检索：向量 ANN 受 scalar_filters 约束，可选与文本相关性按 vector_weight 混合，返回 top-k |

### FSStore（`fs.py`）

本地文件系统存储（原始负载/二进制资产）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `insert` | `(scope, key, data: BinaryIO) -> str` | 在 scope 下的 key 写入新文件，返回规范引用 ref；已存在时报冲突 |
| `update` | `(scope, ref, data: BinaryIO) -> str` | 覆写 scope 下 ref 处的文件，返回（可能更新的）ref；不存在时报缺失 |
| `delete` | `(scope, ref) -> None` | 删除 scope 下 ref 处的文件（幂等） |
| `get` | `(scope, ref) -> BinaryIO` | 打开 scope 下 ref 处的文件用于读取，由调用方负责关闭 |
| `stat` | `(scope, ref) -> FileStat` | 返回 scope 下 ref 处文件的元信息 |
| `object_id` | `(scope, key_or_ref, *, for_insert=False) -> str` | 可选能力；返回稳定对象标识，默认抛 UnsupportedCapabilityError；insert key、返回 ref、别名及 update 均须指向同一标识 |

#### 加密 FS 装饰器契约（PR1-1）

`encrypted` FS target 属于 PR1-1；inner 必须提供 object_id 能力，不能假定其返回 key。
未提供该能力时 encrypted 装配拒绝，raw 后端不受影响。update 可以返回新引用字符串，
但 object_id 必须保持不变，违反时抛 BackendError。LocalFS 规范化别名，InMemoryFS
将 insert key 转为规范 fs:// 引用；AAD 格式版本为 1，绑定完整 Scope、稳定对象标识与 fs_object 用途。

`encrypted` FS target 包装任意 inner FSStore，写入时整体加密文件内容，读取时有界读完整
密文后整体解密；`delete` 与 `stat` 委托 inner。`ref` 与 Scope 保持可寻址的明文，
完整 Scope 与 ref 所指的稳定对象标识必须进入 AAD。`FileStat.size` 表示 inner 中的密文长度，
不承诺等于明文长度。

装配参数：

| 参数 | 语义 |
|------|------|
| `inner` | 必填，指向被装饰的 FSStore 具名实例或内联配置；不得指向当前实例自身 |
| `cryptography` | 必填，指向 CryptographyProvider 具名实例或内联配置 |
| `max_plaintext_bytes` | 默认 64 MiB，单文件明文硬上限，必须大于等于 1；bool/浮点数非法 |
| `max_ciphertext_bytes` | 默认 0，密文读写上限；`0` 表示明文上限加 4 KiB 安全余量，负数/bool/浮点数非法 |

大小检查必须同时覆盖：写入时循环有界读取明文、落盘前检查生成密文、读取前按 `stat` 快速早拒、真正读取时
循环有界读取密文，以及解密后再次校验明文。`stat` 不能作为唯一边界，因为它与随后
`get` 之间存在 TOCTOU 窗口。

#### 全后端静态加密装配

标准 API/StoreManager 装配以 globals.storage_encryption_enabled 为统一开关，默认 false。
开启后全部具名 KV/FS（含注入 KV 与 public raw 端口）统一保护，并更新 Producer 共享缓存；
既有显式 encrypted 不双包，同 raw 端口沿用其既有 provider。关闭时不拆除显式 encrypted。
应用字节加密仍保留 key/Scope/路径等寻址元数据；完整落盘文件的保护由加密卷/后端承担。

全文、向量、图、融合、实体、Markdown 与文件型影子索引/audit/permission SQLite
的静态加密采用部署卷/后端。
启用时逐具名实例核查 params.at_rest 声明与位置绑定，契约见 S08 和 common/F04。内存后端及
SQLite :memory: 无落盘要求，但仍必须固定连接位置，拒绝初始覆盖及运行时切盘（S08）；
声明是运营方核查责任，不能冒充应用验证了远端磁盘。
StoreSecurity.enabled() 仍表达 Store 内部保护，索引的外部保护通过部署声明与宿主检查表达。
查询接口、BM25/ANN/原生过滤不变；本 PR 不新增密态检索或应用层全量解密检索路径。

Linux 管理加密卷须为完整块设备路径受 dm-crypt 保护的 ext4，具有 has_journal 特性及
可核查的非零内部 journal inode；非 ext4、无 journal、外部或无法核查的 journal 均拒绝。
初始化只接受新建镜像，须显式选择 ext4 与 journal，并在报告成功前核查实际文件系统
类型及内部 journal。格式化命令返回成功不能替代产物核查；失败须关闭已打开的临时
mapper、保留镜像供排查，不自动覆盖或修复已有卷。挂载后仍独立验证同一契约。

`StoreSecurity.encryption_binding` 是公开的可选装饰依赖契约，返回
`StoreEncryptionBinding(inner, provider, format)`；原生保护实现可返回 None。
绑定须指向实际原始 Store、实际 CryptographyProvider 及非空的兼容格式标识。
同 raw 的 provider/format 冲突必须拒绝；自动保护不得依赖具体装饰器类或私有字段。
自动包装由对应 Producer 选择配置的 target，显式注入原始具名引用与共享 cryptography；
返回保护对象必须声明相同依赖并通过健康检查。已保护的显式端口及 raw 别名复用现有对象。

`StoreManager.encryption_inventory() -> dict[StorageCapability, dict[str, object]]`
返回所有数据面实际使用的完整端口快照，capability 与 `capabilities()` 一致；每个对象须与
对应公开端口方法返回的对象一致，且不得隐藏领域数据面使用的可写 raw。统一开关开启时，
`StoreManagerProducer.resolve` 在公共边界验证全部 manager 插件的清单：配置的 KV/FS
具名端口不得遗漏，其保护须启用且健康；额外未配置的索引端口须声明内部保护。
配置的索引端口必须实际对应同名配置及插件声明的持久化位置，不得冒用内存配置。
缺失或不完整的保护能力抛 ValidationError，关闭开关时不要求旧插件实现该可选契约。

### EntityStore（`entity_store.py`）

实体反向索引（实体 → 关联的 memory）。F07 起是 `StorageCapability` 第七席 ENTITY，经
`manager.entity(name)` 取用，与其他能力同构。

| 方法 | 签名 | 语义 |
|------|------|------|
| `ensure_index` | `() -> None` | 确保索引已创建并就绪；实现可懒触发（首次读写时） |
| `find_by_entity_text_hash` | `(scope, entity_text_hashes, *, filters, shards=None, limit=500) -> list[EntityRecord]` | 按 `entity_text_hash` keyword term 精确查询（不做向量归并）；`shards` 非空时只返回选中分片的关联，空记录不返回 |
| `find_by_linked_memory_id` | `(scope, memory_id, *, filters, shards=None) -> list[EntityRecord]` | 反查：哪些实体关联了该 memory（unlink 用） |
| `execute_operations` | `(scope, operations) -> EntityBatchResult` | bulk 变更（INSERT/LINK/UNLINK/DELETE_IF_EMPTY/DELETE，UNLINK_UPDATE 仅兼容） |
| `search` | `(scope, query_vector, *, top_k, filters, shards=None) -> list[EntitySearchResult]` | 实体向量 kNN 检索；`shards` 非空时作为前置过滤 |

**LINK 的 `record` 是可选的**：带 `record` 时，目标文档缺失则用它创建（upsert）；不带时只对
已存在的文档增量追加，文档缺失则该条失败。分片包装层在 `split` 模式下保持同一语义——首次关联
到某个分片（该分片还没有这个实体的物理文档）且不带 `record` 时该条失败，已有物理文档时照常追加。
`UNLINK` 只增量移除、从不删除记录；`DELETE_IF_EMPTY` 在存储内原子判空并删除。

**隔离模型**：第一入参是 `scope: Scope`（契约 v2 起与其它 Store 对齐）；后端内部用
`space_id_from_scope(scope)`（`scope.space` → `scope.org` → 字面量 `"default"` 三级降级）
计算 routing；额外的隔离维度由 `EntityStoreFilters` 中非空的 `actor_id` / `assistant_id` /
`session_id` 分别生成 term。`from_scope` 将 user / agent / session 映射到这三项；空值
不生成该维过滤。后端按显式 filters 决定范围，不承诺同 user 下必然跨 agent/session 共享。
隔离仍由存储层强制（不变量 1）；授权按真实 Scope 判定（不变量 26）。

**partial failure 语义**：`execute_operations` 是 per-item 粒度——一条失败不影响其他，
失败 id 经 `EntityBatchResult.failed_ids` 回传，**不抛异常**（与主链路 `BulkWriteError`
的 all-or-nothing 相反）。

**显式候选 ID 过滤**：文档全文与向量召回接受 `unit_id`（或 `id`）条件时，
须在结果数量截取前应用候选范围及可下推的系统、Scope 条件。向量不能只先取全库
近邻再过滤候选 ID，否则高分无关记录可能占满候选窗口。SQLite 先按 ID 和其他可下推
条件筛选候选，再对候选计算距离。参数超过当前连接的上限时返回明确错误。普通无候选
ID 限制的向量召回路径保持不变。

**装配期降级**：entity 是增强层，builder 在必填连接参数（`hosts`）未配时返回 `None` 而
非抛错 → 无 ENTITY 能力 → 两侧消费方跳过（不变量 24 的端口表 None 过滤、不变量 31）。
这是可选增强层的降级策略；其他连接型后端缺必填参数时通常由 `require_param` 拒绝构建。

### MarkdownStore / DocumentShadowIndex（文档记忆真源，F08）

文档模式（`globals.write_document=true`）的真源形态：`MarkdownStore` 是人类可读 markdown
视图（`USER.md` / `MEMORY.md` / `daily_memory/日期.md`，按 `memory_class` + `project` 分流落盘），
`DocumentShadowIndex` 是机器真源（单 sqlite 文件承载 `memory_unit` 全量 + `memory_fts` 倒排
+ `memory_vec` 向量三表同库，靠隐式 rowid 关联）。二者为 F08 新增的第八、第九 capability
（MARKDOWN / DOCUMENT_SHADOW），仅文档模式装配，经 `manager.markdown(name)` / `manager.shadow_index(name)`
取用，与其余七类同构暴露。`StorageCapability.namespace` / `port_method` 公开能力对应的
配置命名空间与端口方法，其中 DOCUMENT_SHADOW 对应 shadow_index（非 document_shadow_store）。
统一静态加密启用时，Markdown root 与影子 SQLite db_path 由插件声明并绑定部署保护；
两者要求显式位置，SQLite :memory: 仅免部署证明，位置仍固定，详见 common/F04。

两端口是文档记忆的双写对——`CompositeDomainStore.add/update/delete` 文档分支同步写 md 块 +
shadow 索引。同进程内，写入与文件监听按解析后的文档根目录共用写窗口。监听器写回索引前
须校验该目录的版本。版本变化时重新读取和计算，提交时只与该目录的写入互斥。
正常写入等候超过上限时返回后端错误。
文件同步只对有效记录执行删除，保留纠正历史、遗忘和归档记录。这些记录的正文留在文件
时，同步不会将其重新写为有效记忆。写失败时尽力恢复。完整算子
契约见 F08-document-memory.md（§2 MarkdownStore / §3 影子索引 / §4 写入路径分流 / 决策四写失败
补偿）。**scope 原生隔离**：影子索引 `memory_unit` 表加 org/space/user/agent/session 五列
（对齐 KV 五段等值 WHERE），与 project+category 列隔离正交——project 是收窄维
（`IN ['', value]`），scope 是身份隔离维（严格等值）。`list_units_by_md`（看门狗跨 scope
诊断）/`delete_units`（幂等按 unit_id）例外不限 scope。看门狗建新 unit 时按
`latest_scope_by_md`（同 `list_units_by_md` 跨 scope 不限 WHERE 例外，`rowid DESC LIMIT 1`）
继承该 md 文件最新一条 unit 的 scope，无历史返 None 落空 Scope——修正补登 unit 落空 scope
列、真实 scope 召回不可见的遗留。**召回放宽 session**：`search_fulltext`/`search_vector`
的 scope WHERE 只对 org/space/user/agent 四段严格等值，不做 session 等值（记忆跨 session
共享）；`get_units`/`list_units` 仍五段严格等值（点查/列表不放宽）——召回与点查/列表口径
不一致是有意为之的已知边界。

**装配期降级**：embedder 未注入或 sqlite-vec 不可用时，shadow 降级为两表（`memory_unit` +
`memory_fts`）模式——向量召回返空、倒排与全量存储照常（与 EntityStore 同属增强层降级，但
shadow 是文档模式必需项而非可选增强，`write_document=true` 下不装配即数据面抛
`UnsupportedStorageCapabilityError`）。

## 数据结构

### KV（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `KVMemoryListResult` | entries: list[tuple[str, bytes]] / count: int |

### 向量（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `VectorRecord` | id / vector: list[float] / metadata |
| `VectorQuery` | vector: list[float] / top_k / filters: FilterExpr \| None / extensions: dict[str, Any] |

层级索引 metadata 在既有 `unit_id`、`content_layer`、`tier`、`lifecycle`、`seq`
基础上增加：

| 键 | 表示 |
|---|---|
| `hierarchy_kind` | kind 的字符串值；空 hierarchy 时缺省 |
| `hierarchy_role` | role 的字符串值；空 hierarchy 时缺省 |
| `hierarchy_status` | 非空结构恒写 active/dismissed |
| `tree_parent_id` | 直接父 id；根或未挂接时为空串 |
| `tree_span_start` | UTC epoch 毫秒区间起点；未声明区间时缺省 |
| `tree_span_end` | UTC epoch 毫秒区间终点；未声明区间时缺省 |

三个 tree_ 字段是索引/过滤名；HierarchyRef 内部字段及序列化键仍为 parent_id/span_start/span_end。
这些通用名称不再属于树保留键，system_metadata 同名业务值继续接受；不提供旧树字段别名。
裸通用过滤键按原有规则归 user_metadata，显式 system_metadata 路径仍读取系统业务值。

同一 unit 的 L0/L1/L2 VectorRecord 必须携带相同的 hierarchy metadata；现有记录 id
格式保持不变。

### 全文（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `Document` | id / text / metadata |
| `TextQuery` | text / top_k / filters: FilterExpr \| None / extensions: dict[str, Any] |

Document 使用与 VectorRecord 相同的六个 hierarchy metadata 键，并保留既有
`content_layer`。L0/L1/L2 文档的当前 id 规则保持不变；增加 metadata 不改变主键。

### 层级过滤与区间表示

层级过滤继续使用现有 `FilterClause(field, op, value)`，不新增查询结构：

- kind/role/parent 精确过滤使用 `EQ`，例如
  `field="hierarchy_kind"`、`field="tree_parent_id"`。
- 区间相交 `[query_start, query_end]` 表示为
  `tree_span_start <= query_end AND tree_span_end >= query_start`，即分别使用 `LTE` 与 `GTE`。
- 树区间与普通 temporal 字段的时间投影统一写为 UTC epoch 毫秒，朴素 datetime 按 UTC
  解释；带时区值保留实际时刻，不使用服务器本地时区。真源保留原时间表示；
  没有区间时不落时间哨兵，普通字段的既有缺值与哨兵规则保持不变。
- filters 只承载 scope 之外的谓词，scope 仍是 Store 方法的显式第一参数。

后端若不能原生执行区间谓词，可以在同 scope 候选上做等价后过滤，但不得放宽结果语义。
裸结构字段只读 `HierarchyRef` 真源，用户同名 metadata 不覆盖它。索引更新先清旧结构投影，再生成当前值；Unified 写到 system_metadata 的结构副本不得被其他 builder 当成旧值再次索引。
索引重建必须枚举 KV 真源的 MemoryUnit，重新生成内容层与 hierarchy metadata；不得从
旧索引反推 hierarchy。

### 图（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `Node` | id / label / properties |
| `Edge` | id / source / target / relation / properties |
| `GraphQuery` | start_id / relation / depth / limit / extensions: dict[str, Any] |

### 融合（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `FusionRecord` | id / vector / text / scalars / value: bytes |
| `FusionQuery` | vector / text / scalar_filters: FilterExpr \| None / top_k / vector_weight / extensions: dict[str, Any] |

### 文件系统（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `FileStat` | ref / size / content_type / created_at / updated_at |

### 通用（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `ScoredID` | id / score |
| `ScoredHit` | id / score / metadata |

**注**：所有 `metadata` / `filters` / `scalar_filters` 只承载 scope 之外的额外谓词，scope 作为显式第一入参，不混进这些结构体。

## 文档后台组件生命周期

Watchdog 不属于存储 capability，由装配根持有并显式启动。start 必须在事件循环
就绪后调用，且该循环须持续运行到 Watchdog 停止；stop 幂等地停止事件/任务准入、
取消定时器并等待已进入数据面的同步工作，
返回后不得再访问 shadow。此等待不得依赖调用方事件循环继续执行。
排队但尚未进入数据面的工作在停止后不得访问依赖，旧代工作不得因重新启动而复活。
装配根在 watcher、摄入和调度使用者停止后，按共享所有权去重关闭 shadow 等连接；
失败装配只回滚本次新建资源，不关闭借用对象。

## 实现注册机制

```
jiuwen_memory/storage/<store>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")

jiuwen_memory/storage/store_manager_impl/   # 管理面实现（CompositeStoreManager）
jiuwen_memory/storage/domain_store_impl/    # 数据面实现（CompositeDomainStore + Recaller 全家）
```

各 Producer：`StoreManagerProducer` / `DomainStoreProducer` / `RecallerProducer` / `KvProducer` /
`FulltextProducer` / `VectorProducer` / `GraphProducer` / `FusionProducer` / `FsProducer` /
`EntityStoreProducer` / `MarkdownProducer` / `ShadowIndexProducer`；文档看门狗使用
`WatchdogProducer`。
注册由 `storage.bootstrap.register_backends` 统一触发。`DomainStoreProducer` 不是平级
YAML 入口——domain_store 由 manager 工厂内构建（manager 是唯一装配入口）。

具体 Store target 名与实现文件列表归 `jiuwen_memory/storage/AGENTS.md` 维护；本 spec 只固化
Store 抽象、跨后端不变量与注册机制。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S03-control | InMemoryEngine 继续使用 KVStore 读取；CloudEngine 的 MemoryUnit 真源读取经注入的 DomainStore（点读 `get` / 列表 `list` / 枚举 `scopes()`）；目标生命周期/治理操作按显式 Scope 定位，全局 sweep/offboarding 才跨 Scope 枚举 |
| S04-retrieval | Retriever 经 `StoreManagerProducer.resolve` 取全局 manager 并持其 `domain_store()`，不持有召回路；`Recaller` 契约与实现归本层数据面，由 `for_manager` 按 `domain_stores.<name>` 的选择键在构建期同步组装（具名构建用 `config.name` 预注册、匿名构建用合成名预注册打破循环） |
| S05-construction | 构建层通过本层抽象做真源与索引持久化 |
| S07-common | 定义 `MemoryUnit.hierarchy`、`HierarchyKind`、`HierarchyRole` 与 `FilterClause` |
| S08-config | Store 连接参数与 `*.active` 可由 ConfigSource 晚绑定；切换后端不包含数据迁移 |
| architecture.md §5 | 可配置真源形态（文档/结构化）与多后端 |


## 接续管理与证据图能力

`KVStore.persistent_atomic_records` 默认 false，表示固定持久命名空间和单记录原子替换，不代表 CAS。`claim_single_writer(namespace)` 默认不支持；支持者持有管理写者排他声明直到 close，第二写者必须失败，不能把单进程 RLock 当跨进程互斥。Registry 显式使用具名端口，测试 ephemeral 例外不得用于生产。

`GraphStore.supports_incident_edges` 默认 false；`incident_edges(scope, node_id, *, limit)` 返回保留 source/target/id/relation/properties 的有界入出边，不跨 Scope。角色图装配要求该能力；不能用无向邻居列表冒充有向关系。节点和边属于可重建投影，关系真源在 Registry KV 的关系头与修订记录；投影 ID 使用专用前缀，后端更新邻接后不得因旧邻接删除已移到其他端点的边。

**条件投影（可选能力，已实现 `kv` / `memory` / `neo4j`）**：`GraphStore.supports_conditional_projection` 默认 false；支持者提供 `project_edge(scope, edge, *, revision, nodes=None)` 与 `unproject_edge(scope, edge_id, *, revision)`。`project_edge` 在单个原子操作（事务或后端写锁）内按边 ID 读取当前边：不存在且无更高撤回墓碑则创建（节点按 ID 创建或复用、从不删除）；当前修订小于 `revision` 则原地更新端点与属性；相等且内容一致视为已完成、不做改动，相等但内容不同报 `ConflictError`；大于则拒绝并报告已有更新版本。`unproject_edge` 仅当当前边修订不大于 `revision` 时删除该边，不删除任何节点，并记录撤回墓碑使更旧修订的重放无法复活已被撤回的边。普通 `insert` 仍保持"重复 ID 报冲突"。关系投影器依赖此能力实现确认丢失后的安全重放，不能以"先删后插"代替。

### 派生构建与借用资源

`DomainStore.supports_exact_metadata_lookup` 默认 false；声明 true 必须保证系统元数据等值过滤查询从真源读取，写成功后立即可查，不能依赖异步索引。`KVStore.release_single_writer(namespace)` 只释放该命名空间写者声明，不关闭数据库；默认不支持。使用方释放自己的 claim，资源所有者负责 close，借用 KV 的图实现不关闭 KV。

### 角色记忆的条件更新

派生 `updated` 推迟到后续版本（见 [F10 第 12 节](../features/construction/F10-role-memory.md#12-已知遗留与外部依赖)）。Registry 并发依赖单写者声明与 `expected_revision`；隐私 KV 包装保留后端的资源绑定和单写者声明。

Neo4j 的 ssl_verify=true 要求 bolt+s:// 或 neo4j+s:// 及 ssl_ca_cert，动态 URI 重绑定不得绕过校验。自定义 CA 通过驱动显式 encrypted=True 与 trusted_certificates 传递（驱动侧 URI 转为对应无后缀 scheme，仅作为加密参数表达方式，连接仍强制 TLS）。健康检查不得执行建约束 DDL，首次写入前须完成所需唯一约束。

## 敏感片段与脱敏真源

隐私能力经独立的 `PrivacyStore` 维护私有状态，复用 BaseStore、Producer、StoreSecurity
和加密装饰器契约；不注册为 StoreManager 普通端口，不由普通 KV、DomainStore 或
召回接口返回原文。片段值与引用清单主体加密；用于定位的 Scope、对象 ID、等级及修订
标识属于控制元数据，不应被描述为整个私有数据库均已加密。部署须保护状态路径和备份。

### 私有 Store 契约

`PrivacyStoreProducer.TOP_NAME = "privacy_store"`；依赖由隐私配置显式选择。
受保护 Store 必须声明有效 StoreSecurity；业务保护层不直接执行 SQL 或调用密码学。
加密包装层调用既有 CryptographyProvider，原始后端负责持久化与事务。

私有状态可以经适配层依赖独立 KVStore。适配层须将同一完整 Scope 的片段、当前
清单、引用及恢复候选作为一个有界状态，使用原生条件写原子发布；冲突抛 ConflictError，
不得覆盖其他发布者。嵌套事务失败回滚到保存点。该事务不扩展为跨业务真源的事务。
`supports_compare_and_swap` / `compare_and_swap` 提供此处需要的条件创建、替换和删除，
替换值不设 TTL。加密维护的 `compare_exchange` 仅更新或删除现有记录，保留绝对过期时间。
隐私保护端口不提供明文迁移能力，受保护记忆的写入仍经隐私真源边界。
私有 KV 及其递归 KV 依赖必须内联创建，不得引用普通具名 KV 端口，也不注册为普通
端口；连接参数在装配时固定，不跟随业务 KV 的 ConfigSource 热切换。后端切换不迁移
数据；旧专用私有库与 KV 状态格式须显式区分。

| 操作 | 契约 |
|---|---|
| `stage_fragment(scope, token, value, level)` | 对本次原值生成不透明受保护引用，不落盘；上层按 TTL/容量管理暂存 |
| `reference(scope, token)` | 点读不透明引用，不解密敏感值；来源绑定随合法路由保留 |
| `get_fragment(scope, token)` | 点读并透明解密敏感值，调用方须已完成二次认证及资源判权 |
| `get(scope, unit)` | 读取并认证当前关联清单；缺失抛 NotFoundError |
| `search(scope, units, *, revision=None)` | 按记录 ID 与可选修订号定位当前/恢复清单；不检索敏感原文 |
| `insert/update(scope, snapshot, fragments)` | 同一私有事务内保存清单、片段及引用；插入冲突/更新缺失分别报错 |
| `checkpoint/confirm/reconcile` | 保留旧引用、确认外部写入、按实际存续版本恢复；未知结果保留候选 |
| `delete(scope, units)` | 删除精确 Scope 内指定记录及恢复引用；units=None 清理该完整 Scope，返回被撤销的 token |
| `transaction(scope)` | 私有后端事务，可嵌套；不承诺与业务 Store 的全局原子性 |

数据方法显式接受完整 Scope；读取、检索和删除不得跨 Scope。维护枚举 `scopes()` 不
读取敏感值；上层处理前缀清理时先枚举匹配，再逐个按精确 Scope 删除。
清单读取可以用于正常处理中的关联认证，不能因此解密敏感片段。普通记忆读取仍走
原来的脱敏真源 Store；加密装饰器不能替代二次认证、当前资源判权及修订校验。

### 关联与密码学绑定

| 对象 | 绑定与约束 |
|---|---|
| 脱敏占位符 | `[JW-PRIVATE:<48 位小写十六进制随机标识>]`，不编码原值，不构成访问凭据 |
| 敏感片段 | AEAD 绑定来源完整 Scope、token、等级、用途 `privacy-fragment`；加密上下文版本为 1 |
| 引用清单 | AEAD 绑定目标完整 Scope、`<memory/message>/<unit_id>:<revision>`、用途 `privacy-manifest`；主体包含 token 集合、指纹与段位置 |
| 修订标识 | `privacy_revision` 与 `privacy_message_revision` 分别服务正式记忆和原始消息，由服务端生成 |
| 内容指纹 | 覆盖段正文、L0/L1、标签、实体、用户元数据及事实文本；不含索引构建追加的技术性 system_metadata |

非保留系统文本元数据仍需保护，其 token 纳入认证清单。相同指纹和 token 集合可以复用
已认证修订号；未知、过期、无来源或损坏的引用拒绝写入。正常构建可认证仅含引用的清单，
不能解密敏感片段。合法同组织路由保留片段的来源加密绑定，并密封目标清单。
privacy_rule_ids / privacy_categories 只保存有限分类依据，不包含原值，且不参与
保护正文指纹；它们不能替代关联和授权检查。修订号表示记忆/消息保护快照，不是独立的规则版本；当前不持久化策略版本历史，配置变更
通过重建运行时及既有内容校验生效。

### 提交、失败和清理

正文、索引及直接 `/memory/`、`/messages/` 写入前均保护。
原文消息写入保护反序列化副本，不修改当前抽取输入；持久化历史读取保持脱敏，不自动
解密。普通正文及派生索引在索引构建入口统一保护，向量索引从脱敏内容生成。嵌套加密 KV 只在同一次
精确 Scope/key 操作内跳过重复编码，不豁免其他键；StoreSecurity 能力保持透传。
私有关联的提交和回收由 KV 真源边界维护；索引包装层只复检投影并同步已提交修订号。
委托写入前持久保留旧清单与片段的密文恢复引用。插入冲突或其他写入异常时回读真源：
旧值仍在则恢复旧关联，新值已写则保留新关联，确认不存在才清理。回读失败时保留候选
快照及引用；下一次成功写入或硬删再回收，不将其开放为普通历史查询。
实例内写入和删除共享互斥边界；各后端不共享全局事务。缺失、版本不符或无法认证的
关联仍拒绝还原，业务真源中可留下的内容必须是脱敏表示。
多个 Runtime 共用后端时，同一 Scope 的业务写入须串行路由；私有状态条件写不构成
整个业务提交协议的分布式锁。

原始消息和正式记忆分通道计引用。显式硬删、回滚删除及空间删除撤销相应关联，最后
一个引用消失后清理片段。先完成真源删除再回收对应私有状态，删除异常保留未确认的引用；
仅删除检索投影不回收私有状态。来源删除不自动推断为删除全部派生记忆；软删状态不允许人的
原文展示，分级自动硬删仍是 TODO。指纹和清单校验不替代查看时的正常资源判权。
清理针对逻辑记录和引用，不声明备份、数据库日志或存储介质已经完成可验证擦除。

原文精确检索、旧库自动迁移、文档/文件监听路径及分级 TTL/cloud/share 执行未提供。
完整披露契约见 [S10](S10-security.md#隐私保护与用户访问控制)。

## 逻辑分片能力协议（第一阶段已实现）

本节是 F09 的契约。**第一阶段已落地**：`ShardResolver`（`rule_based`）、`ShardLocator`、
`ConditionalKV`（内存 / PostgreSQL / Redis）、KV / 向量 / 全文 / 实体四类 `shard_routed`
包装层与关联图 `shard_routed` 包装层（证据图不分片）、`DomainStore.get_in_shards`、拓扑登记与对账、
`--relocate-shard` 数据搬迁与归属存储的 `encrypted` 条件 KV 转发、`resource_descriptor()` 与启动期自动拓扑登记。
实现按 S09 注册与装配；决策与失败恢复流程见 [F09](../features/storage/F09-logical-sharding.md)。

实现映射：Store 级能力由 `storage/capability.py` 的 `StoreCapability` 声明（`BaseStore.capabilities()`），
而不是本节早期草案里的独立能力对象；能力集装配期固定。`SHARD_ROUTED` 标识分片路由包装层，
装配器据此判定是否处于分片部署（不再从某个内层的 `SHARD_PREFILTER` 推断）；声明该能力即承诺实现
`storage/shard_port.py` 的 `ShardRoutedPort` 协议（`shards` / `locator_name` / `shard_descriptors`），
需要外部定位端口的实现同时实现 `ShardLocatorConsumer`（`bind_shard_locator`），持有归属台账与拓扑
登记的实现额外声明 `SHARD_PLACEMENT`（`ShardPlacementOwner`：`register_topology` / `read_topology`）。
装配器只按能力取端口、按协议访问，不 `getattr` 探测方法。`ConditionalKV` 是 `KVStore` 之外的
独立 ABC（`storage/kv.py`），归属存储必须同时实现 `KVStore` 与它；当前只有 PostgreSQL / Redis /
内存实现声明该能力，SQLite 等单写者后端不得作归属存储（仅限单机测试）。

### 能力发现与代理

Store 提供稳定的能力描述和按能力标识取得的类型化端口；描述包含协议版本，缺失或版本不兼容返回 `UnsupportedStorageCapabilityError`。能力对象不单独拥有连接资源，归提供 Store 管理。授权与加密代理显式实现/转发能力，保持 Scope、安全和资源身份；禁止通过私有属性找回原始 Store。暂时故障为 BackendError，不改变能力集合。

加密代理**不得包装 `SHARD_ROUTED` 端口**：加密只作用于物理分片表 / `passthrough` / 归属存储（每分片独立密钥），
外层再包一次会双重加密并丢掉分片粒度，因此该组合在装配期（含显式 `encrypted` 配置）直接拒绝，
也不得靠方法名转发去"保持"分片能力。

归属存储的部署形态属于该能力的前提：Redis 后端使用单节点客户端，只支持单机 / 主从，**不支持
Redis Cluster**（需要 `redis.RedisCluster` 客户端与真实集群集成测试，当前未实现）。

| 能力端口 | 输入 / 输出与约束 |
|---|---|
| ShardResolver | 创建时 MemoryUnit → 合法 shard；公开只读 shards/default_shard；纯函数，不访问存储 |
| ShardLocator | Scope + unit_ids → active 的 id→shard 映射及未定位 id；批量部分结果，故障不得冒充未定位 |
| ConditionalKV | Scope + key/value → 原子 create、按完整期望值 replace/delete；多 key 部分读取返回已存在映射；存储侧当前时间；比较失败与后端故障区分 |
| ResourceDescriptor | 无凭据的规范化持久资源身份、端口类型、索引兼容描述；代理不改变身份；内存资源身份限定装配上下文。当前 PG / pgvector / Redis / ES / SQLite / Milvus（`uri/collection`）/ nano-graphrag（工作目录）/ Neo4j（`uri/database`）/ KV 图声明；split 包装层要求各分片的非空描述互不相同 |
| ShardPrefilter | Scope + 查询 + shard 集合 → 截断前过滤的候选；系统/授权/用户谓词均 AND；标签由存储写入，不信任用户输入。当前由 pgvector / Milvus / Elasticsearch，以及图后端内存 / nano-graphrag / Neo4j 声明（图：遍历可经过任意节点，只在收集结果时按标签过滤） |
| GroupedMemoryLinks | Scope + 记录身份 + 按 shard 分组引用 + 增量操作 → 逐项结果；原子 UNLINK、DELETE_IF_EMPTY；查询仅投影选中引用 |


### 逻辑视图与模式

KV 以归属记录控制可见性，记忆创建后归属固定；scan 覆盖全部逻辑可见键，scopes 去重，不暴露控制键及迁移旧副本。向量、全文、实体、图独立选择 shared/split。shared 前置过滤；split 查询所选物理资源；两者均在返回前验证归属，并从当前真源读取返回内容。

DomainStore 新增 `get_in_shards(scope, unit_ids, shards)`，返回 found/missing_ids/excluded_ids；`shards` 为空或含未配置的分片名时返回 `ValidationError`，不得把拼写错误表现为 excluded_ids；不得因单个缺失整批失败，也不得借 excluded_ids 绕过授权披露跨 Scope 存在性。普通 get 保持旧契约。Schema 属性与层级扩展采用相同约束。

`extensions["shards"]` 的语义由数据面拥有：`DomainStore` 提供 `requested_shards(extensions) -> frozenset[str] | None`，
检索层只透传扩展参数、不解析存储内部 key。默认实现只做形状判定（缺键 → `None`；`[]` / `None` / 非集合 /
空字符串成员 → `ValidationError`），启用分片的实现额外校验分片名已配置；启用的通道不支持分片时报
`UnsupportedStorageCapabilityError`，不得静默退回全量。

实体方法首参迁到 Scope；旧签名经有期限适配器，不能宣称具备分片或原子能力。`EntityStore.supports_complete_linked_enumeration` 显式声明能否完整枚举关联记忆（默认 false，第三方插件不会被误传 `complete=True`），包装层只在该能力为真时声明 `SHARD_RELOCATABLE`。实体契约以
`EntityStore.CONTRACT_VERSION` 版本化（v2 = Scope 首参 + 增量 `UNLINK` / `DELETE_IF_EMPTY`）：旧签名实现
不继承该属性，由适配器按版本识别，不靠方法签名自省。适配器无法履约的操作（如旧签名下的
`DELETE_IF_EMPTY`）必须如实计入失败结果，不得静默当作成功。

共享索引（向量 / 全文）由包装层写入的分片标签是 `system_metadata.storage_shard`：它落在既有
metadata 命名空间内，各后端沿用既有 metadata 索引与过滤路径，公共层不需要为 storage 增加内置字段。
该字段由 storage 覆盖调用方同名输入，用户 filters 只能与它取 AND。

### 图分片协议（已实现）

关联图节点 id 就是记忆 `unit_id`，节点属性不要求 `MemoryRef`。`GraphStore.seed_ids` 增加可选
`extensions`，`GraphRecaller` 把 `extensions["shards"]` 同时传给 `seed_ids` 与 `GraphQuery`；
未指定时保持原行为，普通后端不支持分片时明确报 `UnsupportedStorageCapabilityError`。

- **shared**：单图，包装层按节点 id 经 `ShardLocator` 查归属，在
  `Node.properties["storage_shard"]` 写标签；关联图节点就是记忆，**不存在公共节点**，查不到归属的写入直接 `ValidationError`。
  内层后端须声明 `SHARD_PREFILTER`，在**截断 `limit` 之前**按 `storage_shard` /
  `storage_shard_next` 过滤；遍历可以经过其它分片节点，只在返回结果时过滤（三个后端统一）。
  已删除记忆留下的无标签旧节点在带 `shards` 的查询中不返回。
- **split**：每个分片一个图存储，各分片的 `resource_descriptor()` 必须互不相同
  （Milvus 以 `uri/collection` 为向量物理身份）；节点归属缺失同样报 `ValidationError`，边只有两端落在同一分片才写入，
  跨分片的边丢弃（接受跨分片多跳丢失）；`search` / `seed_ids` 只访问选中分片，`get` 扇出全部图。
- **缺归属**：两种模式写入缺归属节点抛 `MissingShardPlacementError`（`ValidationError` 子类），后台构建可只跳过这一类可预期缺失。
- **搬迁中的写入**：`insert` / `update` 命中 `moving` 节点（含边端点）抛 `RelocationInProgressError`，
  不能覆盖 `storage_shard_next` 过渡标签；`delete` 同样拒绝 `moving` 节点，避免切换后目标副本"复活"。
- **搬迁（D16）**：shared 只改标签（`storage_shard_next` 过渡，`cleanup_unit` 转正或回滚），
  split 复制节点到目标图后删除源节点及其关联边；跟随开启搬迁的 KV 的图端口必须声明
  `SHARD_RELOCATABLE`。

**证据图不分片**：它的节点引用其他 Scope 的记忆，角色视图按 `incident_edges` 读取，不经过
`search`。`EvidenceGraphBuilder` 要求 `supports_incident_edges` 与
`supports_conditional_projection`；`shard_routed` 图包装层不声明这两项，配成证据图时构建失败。
不引入控制 KV、逻辑目录、每 Scope 写入占位、操作日志、提交代次或多引用节点 / split 占位节点。

Schema watermark 与派生索引均纳入迁移/备份/重建。图 split 不承诺跨物理图多跳完整；共享模式不构成独立资源隔离。未完成真实后端认证不得声明支持。

### 归属记录协议

KV 是分片归属的唯一事实来源：只有分片 KV 在 `insert` 新的 `/memory/` 记录时按 `ShardResolver` 决定分片，此后归属固定（粘性归属）；同 id 原地写（分片 KV 的 `update`）修改 `tier`、显式分片键等字段照常更新，记忆留在原分片。生成新 id 的写入（如 `SUPERSEDE`）是一次新的 `insert`，按新内容落位。向量 / 全文 / 实体经 `ShardLocator` 读取归属并跟随，不自行按规则判定。非 `/memory/` 的 key 写 `passthrough`。

**归属记录**：每条记忆在归属存储中有一条记录，key 为 `(scope, unit_id)`，值为 `{分片, 状态, 操作 token, 创建时间}`。

| 字段 | 约束 |
|---|---|
| 状态 | `pending`（插入进行中）/ `active`（已生效）/ `cleaning`（删除或对账清理进行中） |
| 操作 token | 每次创建归属时生成的随机值，只存在于归属记录中，用于区分不同的创建尝试；不扩散到数据 key 与索引 |
| 创建时间 | 存储侧时间（`ConditionalKV.now()`），只用于对账判断超期，不用于识别操作 |

数据 key 保持 `/memory/{unit_id}`，不带归属或代次信息。归属存储须声明 `CONDITIONAL_KV`；条件替换 / 删除比较整条归属值。

**可见性**：一条记忆可见，当且仅当其归属为 `active`，且归属指向的分片中存在 `/memory/{unit_id}`。分片表中没有 `active` 归属指向的数据（无主数据）、归属指向别的分片的数据（错放副本）对任何读取都不可见。

**协调规则**：

1. 归属只能原子创建；同一 id 同时只能存在一条归属，持有归属的 insert 至多一个。
2. 数据只用原子创建写入，遇到已有数据即中止，不覆写。
3. 删除与对账先持有自己的 `cleaning` 归属，持有期间删除数据，最后按自己的 token 条件删除归属。
4. 遗留的 `cleaning` 只能由创建它的操作完成，不在线接管；只在维护窗口、排空在途请求后由对账完成。
5. 超期 `pending` 由对账按完整值条件清理，不允许新的 insert 接管它的 id。
6. update 不参与协调：读到 `active` 后直接对分片表按 key 盲写，语义与单表 `KVStore.update` 相同。
7. 协调全部依赖存储侧原子条件操作，不使用锁。

| 操作 | 步骤 | 并发与失败 |
|---|---|---|
| `insert` | ① 求分片 S、生成 token；② 原子创建 `pending`；③ 在 S 中原子创建数据；④ 条件替换为 `active`，成功才返回成功 | ② 已有任何状态的归属 → `ConflictError`；③ 已有数据或后端失败 → 条件删除自己的 `pending` 后报错；④ 失败（归属已被删除或清理）→ insert 失败，已写数据成为不可见的无主数据 |
| `update` | 读归属，须为 `active`；对 S 中数据执行 `update` | 无归属或非 `active` → `NotFoundError`；数据已被并发删除 → 内层 `NotFoundError` |
| `delete` | ① 读归属；② 条件替换为自己的 `cleaning`；③ 删除数据；④ 按 token 条件删除归属 | 无归属 → 幂等成功；② 失败重读重试；读到他人的 `cleaning` → 视为删除已在进行并返回成功；对 `pending` 同样生效，进行中的 insert 在 ④ 失败 |
| `get` / `exists` / `mget` | 读归属，为 `active` 才读 S | `pending`、`cleaning`、无归属、数据缺失都按不存在处理；`mget` 保持任一缺失整批 `NotFoundError` |

**保证**：同一 id 同时至多一次 insert 成功；insert 返回成功的记忆在被删除前一直可见，内容为该次 insert 或其后某次 update 的内容；delete 返回成功后该记忆不可见；失效的 insert 不能提交归属，也不能覆盖已有数据，其晚到写入的内容不会可见。

**不保证**：update 不拒绝跨「删除后同 id 重新插入」的陈旧写入（与单表相同）；失效在途 insert 的晚到写入会占住数据位置，使同 id 的下一次 insert 失败直到对账清理；遗留 `cleaning` 在维护窗口清理前该 id 无法重新插入；并发 insert 同一 id 时，赢家在提交前失败会使两者都失败。insert 不是幂等的：响应丢失后重试，若首次已成功返回 `ConflictError`。删除完成后允许以同一 id 重新插入，可能落到不同分片。

**派生索引写入契约**（向量 / 全文 / 实体）：

| 操作 | 约束 |
|---|---|
| insert / update | 记录 id 必须由 `metadata.unit_id` 派生（等于 `unit_id`，或以 `unit_id` 加 `-` / `:` 开头），否则拒绝；按 `active` 归属写入，定位不到的记录失败，不落默认分片；不到其它分片删除同 id 记录 |
| delete | 按 id 在各分片幂等删除 |
| get | 按 id 扇出到各分片，缺失语义同内层实现 |
| 批量 | 按分片分组写入；某组失败不回滚成功组，返回失败记录列表 |

检索返回的内容一律回读当前真源，索引记录只用于召回与排序。派生索引中归属分片与所在表不一致的残留记录、KV 错放副本不在在线对账范围内，只在维护窗口处理。

### 改路由字段后的搬迁协议（已实现）

本小节是 [F09 D16](../features/storage/F09-logical-sharding.md#d16-改路由字段后的自动搬迁已实现) 的契约。开关 `kv_store.<name>.params.relocate_on_update`（布尔，默认 `false`）；为 `false` 时归属记录序列化与上文完全一致，下列状态与字段都不出现。

**归属记录扩展**

| 字段 / 状态 | 约束 |
|---|---|
| 状态 `moving` | 搬迁进行中；记录源分片 `source` 与目标分片 `relocate_to`；`shard` 仍为源分片 |
| `relocate_to` | `active` 上表示「待搬迁到该分片」；`moving` 上表示搬迁目标 |
| `step` | 仅 `moving` 使用，取 `copy_kv` / `copy_derived` / `switch` / `rollback`；每步开始前持久化。切换后进入 `active(cleanup_from)`，清理进度即 `cleanup_from` 是否存在 |
| `cleanup_from` | 仅 `active` 使用，表示已切换、源分片仍待清理 |
| `failures` | 连续搬迁失败次数；达到 `relocate_max_failures` 时清除标记 |
| `mark_digest` | 打标记时写入正文的 sha256；搬迁开始后与源分片当前正文比对，不一致即按当前真源重新裁决 |
| `claim` / `claimed_at` | 仅 `active(cleanup_from)` 使用：清理执行权 token 与领取时间，切换时随同一次条件替换写入 |

可选字段为空时不序列化；条件替换 / 删除仍比较整条归属值。打标记只改 `relocate_to` / `mark_digest` / `failures`，清理只改 `cleanup_from` / `claim` / `claimed_at`，两者都基于最新记录做字段替换，不得覆盖对方字段。

**可见性**：一条记忆可见，当且仅当其归属为 `active` 或 `moving`，且归属的 `shard` 指向的分片中存在数据。`moving` 期间以源分片为准；目标分片中的副本在切换前视为错放副本。

**定位**：`ShardLocator.locate` 把 `moving` 的 id 计入 `found`（值为源分片），同时放入结果的 `moving` 集合；派生写入遇到 `moving` 中的 id，该条以 `RelocationInProgressError` 失败。

**触发**：`ShardResolver.resolve_on_update(old: MemoryUnit, new: MemoryUnit, current: str) -> str | None`，返回需要搬去的分片，`None` 表示不搬。默认实现：

1. `shard_from` 字段在 `new` 中有值 → 以该值为目标；
2. `shard_from` 字段在 `old` 中有值而在 `new` 中缺失 / `None` / 空串 → `None`；
3. 其余按 `resolve(new)` 求值；
4. 目标等于 `current` → `None`；目标为 `retiring` 分片时由分片 KV 在打标记前拒绝并告警（`resolve_on_update` 本身不读拓扑）。

只在分片 KV 的 `update` 写入成功后求值；不同于当前标记（目标或 `mark_digest`）时以条件替换写入 `relocate_to` 与本次写入正文的 `mark_digest`，求得 `None` 时清除已有标记。条件替换失败不影响 update 本身的结果。**清除标记也要绑定数据版本**：只有本次写入的正文仍是源分片当前内容时才清除；CAS 重试读到的记录已被更晚的写入标记过（正文摘要不同）时保留该标记，旧请求不能取消新意图。

**操作语义**

| 操作 | 归属为 `active(relocate_to)` | 归属为 `moving` | 归属为 `active(cleanup_from)` |
|---|---|---|---|
| `get` / `exists` / `mget` / `scan` / `list` | 读 `shard` | 读源分片 | 读 `shard`（目标分片） |
| `update` | 正常，可改写或清除标记 | `RelocationInProgressError` | 正常 |
| `delete` | 正常，标记随归属删除 | `RelocationInProgressError` | 存在 `claim` 时 `RelocationInProgressError`；否则正常，持有 `cleaning` 期间一并删除 `cleanup_from` 分片中的数据 |
| 同 id `insert` | `ConflictError`（与现有一致） | `ConflictError` | `ConflictError` |

`RelocationInProgressError` 是 `ConflictError` 的子类，HTTP 映射 409，表示可在稍后重试；消息不暴露源 / 目标分片之外的信息。

**待搬迁索引**：有搬迁工作的记忆（带 `relocate_to` / `cleanup_from` 或处于 `moving`）另在归属存储同 Scope 写一条 `/__shard_placement/relocation/<unit_id>`，值为写入时间与随机串。先写索引、再写标记；索引刷新遇到 CAS 冲突必须重读后重试创建 / 替换，不能假定「另一个写者已刷新」——旧清理任务可能刚按旧值删除了它；重试仍失败时不写标记，避免产生没有索引、永不被处理的搬迁。`relocate_pending` 只扫描这些条目。索引不是权威状态：执行者先读索引值、再读归属记录；指向的记录已无工作且索引超过 `stale_after` 时，按读到的值条件删除；搬迁完成后同样按**本轮读到的值**条件删除，期间被新标记刷新过的索引必须保留。对账、退役校验与外部扫描跳过该子前缀。

**update 写后复核**（开关打开时）：分片 KV 的 `update` 写入数据后重读归属；若与写前读到的不是同一次生效占有（状态非 `active`，或 token / 分片变化），返回 `RelocationInProgressError`，由调用方重试。写源分片时数据已不存在且归属已换主，同样返回该错误而不是 `NotFoundError`。打标记的条件替换失败时重读一次：同一占有只是标记变化则按最新记录重试，已换主则返回该错误。

**执行**（`relocate_pending(*, scope=None, limit=100, min_age=None, recover_stale=False, stale_after=None)`，由分片 KV 的控制面提供，非 MemoryAPI 方法；`min_age` 缺省取装配参数 `relocate_min_age`，默认 30 秒）：

| 步骤 | 动作 | 失败处理 |
|---|---|---|
| 开始 | 对 `relocate_to` 标记已超过 `min_age`、且不带 `cleanup_from` 的 `active` 记录，条件替换为 `moving(step=copy_kv)` 并生成新 token | 条件替换失败 → 跳过（已被并发修改或由其他执行者处理） |
| 校验标记 | 持有 `moving` 读源正文，与 `mark_digest` 比对 | 不一致 → 按当前正文 `resolve_on_update(new, new, 源)` 重新求目标，条件替换回 `active(源)` 并重新排队（目标为空则清除标记），不计失败 |
| `copy_kv` | 读源数据与剩余 TTL，在目标分片原子创建 | TTL 无法给出、目标已有不同内容 → 回滚 |
| `copy_derived` | 对每个声明 `SHARD_RELOCATABLE` 的派生端口调用 `relocate_unit` | 任一失败 → 回滚 |
| `switch` | 先确认源数据仍等于复制时的版本、且目标 KV 副本存在并与源一致，再条件替换为 `active(shard=目标, cleanup_from=源, claim=新 token)` | 源数据已被改写 → 回滚（不计失败，下一轮按新内容重新裁决）；目标副本缺失 / 不一致 → 回滚（绝不切换）；条件替换失败 → 保持 `moving`，按遗留处理 |
| `cleanup` | 持有 `claim`：各端口 `cleanup_unit(源)`，删除源分片数据，仅当记录的 `claim` 仍为本执行者时去掉 `cleanup_from` / `claim` | 失败 → 释放 `claim`、保留 `cleanup_from`，下次重新领取后继续；`claim` 未释放（执行者崩溃）且超过 `stale_after` 时只在 `recover_stale=True` 接管 |

回滚：**开始前先把 `step` 条件替换为 `rollback`**，再各端口 `cleanup_unit(目标)`、最后删除目标分片 KV 副本（派生清理失败时保留目标副本，维护恢复仍可复核），条件替换回 `active(shard=源, relocate_to=目标, failures+1)`，标记时间重置。`failures` 达到 `relocate_max_failures`（默认 5）时改为清除标记与索引（报告计入 `abandoned`）。回滚失败时保持 `moving`。

重新分区（`repartition_unit`）在开关打开时复用同一状态机：先打标记再执行，未完成则抛错且归属保持在源分片，不会出现 KV 已切换、派生记录仍在旧分片的状态；开关关闭时保持只搬 KV 的旧行为。

遗留 `moving`（`now - created_at > stale_after`）只能由创建它的执行者完成；在线执行只计数，`recover_stale=True` 仅在维护窗口、排空在途操作后使用：`step` 早于 `switch` 回滚。`step=switch` 前滚前必须复核目标 KV 副本存在且（源仍在时）与源一致，不满足就按回滚处理（防止目标副本写入丢失或内容不一致时切到空目标并删除唯一有效数据）；回滚一开始就持久化 `step=rollback`，因此不会出现「回滚做了一半却停在 `step=switch`」。`step=rollback` 的记录一律继续回滚、绝不前滚：此时目标派生记录可能已被删除，前滚会清理源分片并永久丢掉唯一派生索引。在线对账跳过 `moving`。

复制 KV 必须保留剩余 TTL；`remaining_ttl` 返回 `None` 时不得写成永久记录。目标分片为 `retiring` 时不开始搬迁。

**派生端口能力**

| 能力 | 方法 | 约束 |
|---|---|---|
| `UNIT_RECORD_LOOKUP`（后端） | `record_ids_for_units(scope, unit_ids) -> dict[str, list[str]]` | 按记录 `metadata.unit_id` 精确匹配，Scope 五维精确；覆盖分块与 L0 / L1 记录；不存在的 unit_id 不出现在结果中；**必须读到写入后已返回成功的全部记录**（近实时后端先刷新、弱一致后端用强一致查询）且不得静默截断（超出后端单次上限时报错）；未声明时调用抛 `UnsupportedStorageCapabilityError` |
| `SHARD_RELOCATABLE`（分片包装层） | `relocate_unit(scope, unit_id, source, target) -> None`；`cleanup_unit(scope, unit_id, shard) -> None` | 二者幂等；`relocate_unit` 读取源记录必须覆盖 ID 枚举的全集（强一致读 / 完整枚举），少任何一条即抛 `BackendError` 并拒绝搬迁；`relocate_unit` 原样复制记录（含 embedding、正文与 metadata），不删除、不改写源分片上的记录与关联：共享模式只写过渡标签 `system_metadata.storage_shard_next=目标`，实体只追加目标关联（共享模式追加目标分组）；`cleanup_unit(源)` 才删除源分片记录、把共享记录的分片标签改为目标并去掉过渡标签、按分组解除源关联；`cleanup_unit(目标)`（回滚）只去掉指向目标的过渡标签 / 目标分组；不跨分片盲删；批量写入结果中 `failed_ids` 非空时抛 `BackendError`；内层后端须声明 `UNIT_RECORD_LOOKUP` 才可声明本能力。搬迁读取用 `VectorStore.get_for_relocation`、写入用 `insert_for_relocation`（默认等价 `get` / `insert`，强一致后端覆写为强一致读与强一致冲突预检），读回记录必须覆盖全部 id，否则拒绝；实体包装层只在内层 `supports_complete_linked_enumeration` 为真时声明本能力 |

**共享模式检索过滤**：开关打开时，共享派生端口的分片过滤为「`system_metadata.storage_shard` 命中或 `system_metadata.storage_shard_next` 命中」，再由归属校验决定可见性，保证切换前后记录在当前归属分片上始终可被检索到。后端须对 `storage_shard_next` 建与 `storage_shard` 相同的过滤索引。

**分组关联**（`GROUPED_LINKS`）：`UNLINK` 携带 `memory_shards` 时只从对应分组移除该记忆；记忆仍属于其它分组时保留在 `linked_memory_ids` 中。

**装配**：派生端口按 `locator_name()` 绑定到对应的分片 KV，同一端口经别名重复出现只绑定一次。某个分片 KV 的 `relocate_on_update=true` 时，跟随它的 `shard_routed` 派生端口必须声明 `SHARD_RELOCATABLE`，否则启动失败；未开启的 KV 不绑定派生端口。新方法不加入 `KVStore` / `VectorStore` / `FulltextStore` / `EntityStore` 的抽象方法，只通过能力声明提供。

**保证**：同一记忆任一时刻只有一个可见副本；`relocate_pending` 返回 moved 的记忆，此后读取与按分片检索都以目标分片为准；搬迁不改变记忆内容与剩余 TTL；被 `RelocationInProgressError` 拒绝的写入未生效。

**不保证**：改字段后到新分片可见的延迟；派生写入若在打标记前已定位到源分片、却在复制之后才落地，目标分片的派生记录可能是复制时的版本（只影响召回与排序，返回内容回读真源）；遗留 `moving` 在维护窗口恢复前该记忆保持只读；遗留 `claim` 在维护窗口恢复前该记忆不能 delete、不能开始新搬迁。
