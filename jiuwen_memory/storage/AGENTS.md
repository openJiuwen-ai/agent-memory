# Agent Memory Storage（存储层）

**规约文档**：[S06-storage.md](../../docs/specs/S06-storage.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

存储层分**管理面**（`StoreManager`：能力发现、命名端口暴露、统一授权代理、健康聚合）
与**数据面**（`DomainStore`：MemoryUnit 领域 CRUD 与检索适配）两个独立 ABC；默认实现
`CompositeStoreManager` + `CompositeDomainStore`。全局唯一 manager 由 `globals.store_manager`
指名（F08）。管理面暴露七类 Store 端口（KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS/ENTITY）与
命名数据面，二者是同层级的受管成员（同构的 `xxx(name)` / `has_xxx(name)` 入口）。
底层 Store 统一 CRUD 动词（insert/delete/update/get）和检索型 `search`。
**scope 隔离是存储层的原生职责**（ENTITY 端口以 space_id + actor 表达，见铁律 1）。

## 模块地图

| 文件 | 职责 |
|---|---|
| `store_manager.py` | 管理面契约 `StoreManager`（端口单一入口 `kv(name)`/`has_kv(name)` 等、`domain_store(name)`/`has_domain_store(name)`、能力/健康聚合）+ `StoreManagerProducer`（TOP_NAME=`store_manager`；`resolve` 按 params → `globals.store_manager` → default 三级链解析全局唯一 manager，不做匿名兜底）+ `resolve_name`（消费者具名端口/数据面选择键统一读取：params 直读、拒 inline dict）+ `StorageCapability` |
| `domain_store.py` | 数据面契约 `DomainStore`（MemoryUnit 领域操作：`add`/`update` 带 `IndexWriteMode`、`delete` 带 `IndexRemoveMode`、get/list/scopes、recall/recall_and_get/retrieve/preferred_retrieval_pipeline）+ `DomainStoreProducer`（非平级 YAML 入口，由 manager 工厂内构建）。原文不在此列——由构建层注入独立 `KVStore` 自行读写 |
| `security.py` | StorageSecurity 通用授权与 StoreSecurity 数据保护能力标识 |
| `base.py` | BaseStore 基类：所有存储后端的自描述契约（store_type / health） |
| `types.py` | 存储层数据类型：`IndexWriteMode`/`IndexRemoveMode` 写删语义枚举、KVMemoryListResult/VectorRecord/Document/Node/Edge/FusionRecord/FileStat 等 |
| `kv.py` | KVStore 接口：键值存储，统一 CRUD + MemoryUnit 列表查询 + 范围枚举；共享读 helper 两件——`load_units(kv, scope, unit_ids)` 点读（缺失省略/保序/不去重/零过滤）与 `list_units(kv, scope, **list kwargs) -> (items, count)` 列表读（`kv.list` + 反序列化，过滤/计数/分页语义由 `KVStore.list` 契约承担） |
| `vector.py` | VectorStore 接口：向量存储，统一 CRUD + ANN 检索 |
| `graph.py` | GraphStore 接口：属性图存储，节点与边统一 CRUD + 邻域遍历 |
| `fulltext.py` | FulltextStore 接口：全文倒排索引存储，统一 CRUD + 关键词检索（BM25） |
| `fusion.py` | FusionStore 接口：融合存储（向量+倒排+正排一体） |
| `fs.py` | FSStore 接口：文件系统存储（原始负载/二进制资产） |
| `entity_store.py` | EntityStore 端口契约（**StorageCapability 第七席位 ENTITY**）：以 `space_id` routing + `EntityStoreFilters.actor_id` 隔离的实体反向索引；`space_id: str` 作显式第一入参——BaseStore「scope 第一入参」的**唯一例外**，授权由 `_AuthorizedEntityStoreProxy` 专门适配 |
| `_support.py` | 后端实现共用：异常归一（`wrap_backend`）、scope 派生（`scope_dims`/`scope_segments`）、SSL 配置读取（`read_ssl_config`）；`SslConfig` 与 scheme 校验复用 `common._support` |
| `_pg.py` | PostgreSQL 后端共享基础：asyncpg 惰性连接池（专职事件循环线程桥接同步调用）、schema 工具与 FilterExpr SQL 编译；`dsn` 支持 ConfigSource 晚绑定 |
| `kv_impl/` | KVStore 实现目录（memory / sqlite / redis / encrypted / postgres）及共用的 `memory_list.py` 兼容逻辑；连接型后端支持 `kv_store.*` 晚绑定 |
| `vector_impl/` | VectorStore 实现目录（memory / milvus / pgvector）；`uri`/`dsn` 晚绑定 |
| `graph_impl/` | GraphStore 实现目录（memory / nano_graphrag）；`working_dir` 晚绑定 |
| `fulltext_impl/` | FulltextStore 实现目录（memory / elasticsearch）；`hosts` 晚绑定 |
| `fusion_impl/` | FusionStore 实现目录（memory / milvus_graph）；`uri`/`working_dir` 晚绑定 |
| `fs_impl/` | FSStore 实现目录（local）；`root` 晚绑定 |
| `entity_impl/` | EntityStore 实现目录（elasticsearch）；经 manager 装配为 ENTITY 端口（params 引用 → `entity_store.default` 兜底）；`hosts` 未配时 builder 返 None → 无 ENTITY 能力（增强层装配期降级，不报错）；`hosts` 晚绑定 |
| `store_manager_impl/` | 管理面实现目录；`CompositeStoreManager` 以 `composite` target 自注册（`_AuthorizedStoreProxy` + ENTITY 专用的 `_AuthorizedEntityStoreProxy` 授权代理、七类命名端口全量自动、`domain_stores` 配置段装配、`bind_domain_store(ds, name)`） |
| `domain_store_impl/` | 数据面实现目录；`CompositeDomainStore`（`recallers` property + `bind_recallers` 手工接线口）与 `_assemble_recallers`（F06 内收设计，调用时机在 manager `_build` 末尾） |
| `bootstrap.py` | 统一触发七类 Store 后端与 Storage 实现注册 |

## 统一 CRUD 动词

| 动词 | 含义 |
|------|------|
| `insert` | 增：新建记录（id 已存在时抛 ConflictError） |
| `delete` | 删：按 id 删除（幂等） |
| `update` | 改：修改已有记录（id 不存在时抛 NotFoundError） |
| `get` | 查：按 id 点查（点查单条不存在时抛 NotFoundError；批量查缺失的 id 省略） |

检索型存储额外提供 `search` 查询；kv 提供 MemoryUnit 专用 `list` 和通用 `mget` /
`exists` / `scan` / `scopes`；fs 提供 `stat`。

## 行为铁律

0. **metadata 索引保留逻辑路径**：索引记录区分 `system_metadata.<key>` 与
   `user_metadata.<key>`；各后端可用完整 JSON key 或对象层级实现，但 FilterExpr 语义必须一致。

1. **scope 原生隔离**  
   `scope: Scope` 为每个 Store 方法的显式第一入参，物理约束在该 scope 内。写入按 scope 落库，检索/点查/删除绝不跨 scope 返回或影响。`org/space/user/agent/session` 五段 scope 必须参与命名空间或过滤；空 `space` 只匹配空 space 兼容域。隔离必须在存储层强制，上层不依赖调用纪律。
   **唯一例外是 ENTITY 端口**：`EntityStore` 四方法以 `space_id: str`（`space_id_from_scope` 的算值，走后端 routing）+ `EntityStoreFilters.actor_id` 承担隔离——entity 索引的隔离维度与 Scope 五段模型不同构（agent/session 不作隔离维度，实体是 user 级知识）。隔离仍在存储层强制，只是维度表达不同；该端口的授权由 `_AuthorizedEntityStoreProxy` 适配。

2. **记录 id 在 scope 内唯一**
   `insert` 冲突 / `update` 缺失按 `(scope, id)` 判定。同一 id 在不同 scope 下物理隔离（kv/fs 通过五段命名空间，检索型 Store 可用 scope+id 物理主键和 scope 字段）。

3. **scope 不在记录结构体里**  
   `VectorRecord` / `Document` / `Node` / `FusionRecord` 等结构体不含 `scope` 字段（scope 是方法入参，不混进记录/查询结构体，也不编进 `metadata` / `filters`）。

4. **kv/fs 对 key/路径做命名空间隔离**  
   同一逻辑 key 在不同 scope 下是相互隔离的不同物理键。`KVStore.scan(scope, prefix)` 物理约束在该 scope 内，不跨 scope。

5. **KV list 先过滤再分页**
   `KVStore.list` 只查询 `/memory/` MemoryUnit；在完整 Scope 内依次执行
   `memory_types AND filters`、精确计数、稳定排序和分页。`count` 不受 offset/limit 影响。

6. **检索型 Store 的 search 物理约束在 scope 内**
   `FulltextStore.search(scope, query)` / `VectorStore.search(scope, query)` / `GraphStore.search(scope, query)` 绝不跨 scope 返回。

7. **后端不可用统一抛 BackendError**
   连接失败/超时/服务不可用等非预期失败统一抛 `BackendError`（不抛泛化的 Exception）。

8. **EncryptedKVStore 只做装饰，不做算法**
   `encrypted` KV target 必须显式包装一个 raw KVStore，并调用 `common.security.SecurityProvider`
   做 value 加解密；`list` 必须在解密后执行 MemoryUnit 过滤，不能把过滤下推到密文 raw KV。
   真实加密算法不放在 storage 层。

9. **过滤保持 metadata 形态语义**
   `EQ` / `IN` 的正向匹配只命中标量，`CONTAINS` 只命中数组成员；`NE` / `NOT_IN`
   分别是前两者的逻辑否定；范围算子只作用于标量。后端原生字段若不区分单值与数组，
   必须写入内部派生标记恢复该语义，不得把 `EQ` 与 `CONTAINS` 编译成无差别查询，
   也不得让数组字段被范围谓词按「任一成员命中」选中。

10. **SSL 开启后不得静默降级**
   `ssl_verify=true` 意味着实际必须校验服务端证书。缺 `ssl_ca_cert`、连接串仍为明文
   scheme、或连接串自带会覆盖本设置的 TLS 参数，一律在**装配阶段**报错，不得放行到
   运行期——调用方以为受保护而实际未校验，比明文更危险。

11. **Storage capability 是端口能力的唯一事实来源**
   capability 只包含 KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS/ENTITY（ENTITY 为 F07-D 新增第七席）。
   `has_*()` 必须由不可变集合推导；
   未声明端口直接访问时抛 `UnsupportedStorageCapabilityError`。

12. **统一授权覆盖领域接口和暴露端口**
   Storage 顶层操作与 `storage.vector.get()` 等端口调用先经 `StorageSecurity`；默认
   `AllowAllStorageSecurity` 允许省略 access。各 Store 同时暴露自身 `security`，未启用数据
   保护时返回 passthrough，`EncryptedKVStore` 明确声明已启用。

   **ENTITY 端口的 action 映射**（`_AuthorizedEntityStoreProxy`，与通用 proxy 分开实现）：
   `find_by_entity_text_hash` / `find_by_linked_memory_id` → `SEARCH`（不归 GET——既有
   GET 是「按 id 点查/枚举」，把反向索引的批量反查归进去会让只授读的身份获得全库反查
   能力）；`execute_operations` → 按 batch 内 op 类型派生动作集逐个授权
   （`INSERT`→ADD、`LINK`/`UNLINK_UPDATE`→UPDATE、`DELETE`→DELETE；空 batch 零授权），
   不归 ADMIN 也不用固定并集；`ensure_index` → `ADMIN`（DDL）。
   **告诫**：该端口交给 `authorize` 的 Scope 是**有损近似**（`Scope(space=space_id,
   user=filters.actor_id)`）——`space` 可能实际是 org id 或字面量 `"default"`，
   `org`/`agent`/`session` 恒空，`execute_operations` 无 `filters` 故 `user` 也恒空。
   自定义 `StorageSecurity` **不得**对 `resource == "entity"` 按 org/agent/session 判定。

13. **已声明端口一律参与 health 聚合**
   增强层的降级发生在**装配期**（builder 返 None → 无该 capability → 消费方跳过），不在
   探活期。端口一旦声明且构造成功，后端不可达就应让 `manager.health()` 报错——不给任何
   capability 开健康豁免。推论：端口表的值必须非 None（`_named_ports` 在唯一的
   「命名空间→端口」构造点统一过滤 builder 返回的 None）。ENTITY 端口纳管的完整决策见
   [F07 D 组](../../docs/features/storage/F07-storage-manager-domain-store-split.md)。

## 与其他子目录的边界

**本模块管**：
- 可配置真源（KVStore）
- 多后端索引存储（Vector/Fulltext/Graph/Fusion/Entity）
- 文件系统存储（FSStore）
- MemoryUnit 领域 CRUD/list、原文读写、能力发现和底层 Store 端口统一暴露
- Storage 数据面授权与 Store 数据保护能力边界
- 统一 CRUD 动词
- scope 原生隔离

**不管**：
- grant/revoke、授权策略生命周期与业务权限模型
- 检索编排（归 `retrieval`）
- 索引构建逻辑（归 `construction`）
- 具体后端选型决策（由装配层配置）

## 本地约束

1. 所有 Store 必须实现 `store_type()` 和 `health()`（继承自 `BaseStore`）。
2. 实现通过 `@XxxProducer.register("name")` 自注册。
3. 检索型 Store 的 `get` 批量查询时，缺失的 id 从结果中省略（不抛异常）。
4. KVStore 的 `ttl` 单位为秒（float），`0` 表示永不过期。
5. GraphStore 的 `seed_ids` 用于图召回时定位入口节点，匹配语义由后端定义（允许实现差异）。
6. FusionStore 的 `FusionRecord` 可部分字段为 None（如只写向量不写文本）。
7. `EncryptedKVStore` 的 `raw_kv_store` 不能指向自身；未配置 raw 依赖时必须在装配阶段报错。
8. `KVStore.mget` 是 `get` 的批量互补：返回与 `keys` 下标一一对应的 `list[bytes]`、任一 key 缺失即抛 `NotFoundError`（与 `get` 一致，不静默省略）、**不去重**、支持重复 key（各下标独立返回，语义同 Redis `MGET`，重复 key 去重由调用方如 `UnitReader.load` 负责，不下沉到本接口）；`encrypted` 的 `mget` 委托 raw 取密文（raw 缺失即抛 `NotFoundError`）后须逐项解密（AAD 绑 key，不可批量统一解密）。
9. 接外部后端的实现统一接受 `ssl_verify` / `ssl_ca_cert`（默认关闭），经 `_support.read_ssl_config`
   读取后由各 builder 自行翻译为客户端参数：redis `ssl_ca_certs`、elasticsearch `ca_certs`、
   postgres/pgvector `SSLContext`（`CERT_REQUIRED` + `check_hostname`）、milvus `server_pem_path`（配
   `secure=True`）。不做跨后端的 TLS 参数抽象层——各客户端语义切分不同，详见
   [F04-storage-ssl.md](../../docs/features/storage/F04-storage-ssl.md)。
   `SslConfig`、归一（`build_ssl_config`）与 scheme 校验（`require_tls_scheme`）住在
   `common._support`，与出站客户端共用；storage 侧只保留缺证书即报错这条自有策略。
10. `CompositeDomainStore` 的默认首选检索路径是 `RECALL_GET_RANK`；首选路径是实例级稳定值，
    不随请求或健康状态切换，也不加入 Store capability 集合。
11. `StoreManagerProducer.TOP_NAME` 固定为 `store_manager`（F08 从 `storage` 更名）；全局唯一
    manager 由 `globals.store_manager` 指名，`resolve` 三级链（params 显式覆盖 → globals →
    default）解析，未声明实例名抛 `ValidationError`、不做匿名兜底。`CompositeStoreManager`
    只装配配置中声明的 Store 端口；其中 `kv_store` 引用缺键时共享 `kv_store.default` 具名
    实例（`build_named` 命中具名缓存，未声明则由 `build_named` 抛 `ValidationError`）——
    禁止匿名新建（会与具名实例静默分裂成两套真源）；其余五类端口未声明即无该能力。兼容 Recaller 由 manager 工厂按配置（`vector_enabled`/
    `graph_enabled`/`layers_index_enabled` 与 `*_recaller` 选择键）在构建期同步组装，装配错误
    fail-fast。recaller builder 会经 `StoreManagerProducer.resolve` 回取本 manager，故工厂先
    预注册再组装打破循环依赖：具名构建用 `config.name` 预注册（recaller 命名空间下具名实例的
    `store_manager` 引用走 `build_named` 命中缓存），匿名构建无缓存键用合成名
    （`__anon_store_manager_{id}__`）预注册并把 manager 引用注入 recaller params（让 builder 内
    `resolve` 走 `cls.dep` 第一分支命中合成名缓存，不触发递归）；模块层面 storage 不导入
    retrieval（工厂内函数级惰性 import）。详见
    [F06-composite-recaller-assembly.md](../../docs/features/storage/F06-composite-recaller-assembly.md)。
12. `DomainStore.scopes()` 枚举 MemoryUnit 真源已有 Scope；分层索引经 `has_*(name)` /
    `xxx(name)` 访问命名端口，Construction、Retrieval、Control 不得直接调用 Store Producer
    解析具名后端（含 `EntityStoreProducer`——F07-D 起 entity 也经 `manager.entity(name)` 取）。
    七类 `*_store` 命名空间下所有非 `default` 具名实例**声明即端口**（全量自动，
    F08）；消费者经 `resolve_name` 读 `params.<ns>_store` 键指名端口（值必须是端口名字符串），
    `params.domain_store` 键指名数据面。ENTITY 的 default 端口额外接受
    「`entity_store.default` 声明即端口」的兜底解析：配置合并是实例级整体覆盖，若强制
    params 引用，既有部署就得在自己的 config.yml 里全量抄写 `store_manager.default.params`
    （含全部 `*_recaller` 键），漏抄即静默丢一路召回。
13. 连接型后端须支持 ConfigSource 晚绑定（S08 / F01 §2.1.4）：在取客户端/连接路径
    `fetch` 对应 key，值变化则重建连接（同实现换 Redis URL / db_path 走此路径，不必多实例）。
    异质 Store 级 `*.active` 切换由 `config.routing.Routing*Store` 承担（F01）；
    多套完整 `StoreManager` 实例的动态选用由 `config.routing.RoutingStoreManager` +
    `store_manager.active` 承担（F02/F08）。二者均为产品手工注入（方案 A），不改默认拓扑、
    不注册内置 `target: routing`。

14. **上层握共享 manager 入口，勿构造期握死某一实例的裸端口**
    Engine / Retriever / IndexBuilder / Recaller 共享的全局 manager 可以是
    `RoutingStoreManager`。`RoutingStoreManager` 的 `kv(name)`/`vector(name)`/… 返回按
    `(capability, name)` 缓存的惰性代理（`_LazyStorePort`），使构造期 `self._vector =
    manager.vector()` 仍随 `store_manager.active` 重解析；`domain_store(name)` 返回按 name
    惰性缓存的 `RoutingDomainStore`。EncryptedKV 为 F04 opt-in，只包在各预装实例内部 KV
    （若启用），不在 `RoutingStoreManager` 外包一层。
15. **命名数据面（F08）**：`manager.domain_store(name)` 多槽，多套命名数据面共享同一物理
    Store 集、差异仅在检索 profile（`preferred_retrieval_pipeline` + recallers 组合）；经
    `store_manager.<inst>.params.domain_stores: {<name>: {覆盖键}}` 声明（段内 `"default"`
    键拒绝），`bind_domain_store(ds, name)` 供手工/测试接线。跨 Store 集的整栈切换走 Routing，
    不属命名数据面语义。
16. **纯点读/列表读注入 KVStore 端口**：Dedup `_load_unit` / Governor `_find` / Evolver
    源读 / KeywordRecaller 实体扩展等纯点读场景注入 `manager.kv(name)` + `load_units`；
    control 面（Engine×2 / LifecycleManager / EvolveJob / MiddleToLongJob）真源读写全部直连
    `manager.kv(name)`（点读 `load_units` / 列表 `list_units` / 枚举 `kv.scopes()`，lifecycle
    回写 `memory_key`+`dumps` 同 ForwardIndexBuilder 模式）——均不注入 DomainStore（运行期持
    最小接口，F08 修订 F07 决策 10、F09 推进到 control 面）。DomainStore 消费方只有检索路径
    （`PipelineRetriever`）与一体化写路径（`UnifiedIndexBuilder`）。
