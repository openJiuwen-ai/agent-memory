# S06 — 存储层（Storage Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/storage/ |
| 最近一次修订日期 | 2026-09-05 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/construction/F07-memory-write-entry.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F05-cloud-engine-design.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/retrieval/F05-storage-retrieval-pipelines.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F08-memory-tree.md，docs/features/common/F04-security-interfaces-and-encryption.md，docs/features/storage/F02-encrypted-storage.md，docs/features/storage/F03-postgres-backend.md，docs/features/storage/F04-storage-ssl.md，docs/features/storage/F05-unified-storage-design.md，docs/features/storage/F06-composite-recaller-assembly.md，docs/features/storage/F07-storage-manager-domain-store-split.md（合并原 F07/F08/F09） |
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
- 多后端索引存储抽象：向量（VectorStore）、全文（FulltextStore）、图（GraphStore）、融合（FusionStore）、文件系统（FSStore）、实体反向（EntityStore）
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
15. **EncryptedKVStore 只装饰 KV，不实现算法**：写前加密、读后解密通过注入的 `SecurityProvider` 完成；`list` 在解密后执行 MemoryUnit 过滤，不能把过滤下推到密文 raw KV。
16. **space 是 scope 的硬分区维度**：`scope_segments(scope)` 使用 `org/space/user/agent/session` 五段；`scope_dims(scope)` 在 `org` 非空时即使 `space==""` 也下推 `space == ""`，避免空 space 查询跨到非空 space。
17. **标识唯一性分层**：非空 Space id 在 Space 资源注册表中全局唯一；MemoryUnit 与各 Store 记录 id 只要求在完整 Scope 内唯一。
18. **SSL 声明即生效**：接外部后端的实现统一接受 `ssl_verify` / `ssl_ca_cert` 两个装配参数（默认关闭）。`ssl_verify` 只表示**是否校验服务端证书**，不负责开启加密——加密开关落在连接串上（`rediss://` / `https://` / `sslmode=`）。开启后不得静默降级：缺证书、连接串仍为明文、或连接串自带会覆盖本设置的 TLS 参数，一律在**装配阶段**报错。
19. **KV 是层级真源**（目标契约，尚未实现）：序列化 `MemoryUnit.hierarchy` 与 unit
    一同存入 KV。当前契约不新增 hierarchy Store，也不把父子包含边双写到 GraphStore；
    若未来迁移到独立边存储，必须先修订本 spec 和 S07 的数据模型契约。
20. **层级索引是派生物**：VectorRecord/Document 的 hierarchy metadata 必须能够从 KV
    中的 `MemoryUnit` 全量重建；索引丢失或不一致时以 KV 为准。
21. **GraphStore 边界明确**：GraphStore 表示关联和多跳关系，不表示 hierarchy containment；
    `HierarchyRef.parent_id/child_ids` 不投影为图边。
22. **CRUD 不级联层级关系**：KVStore 的 insert/update/delete 只作用于指定 key。删除父或子
    不会自动改写其他 unit；父子双向边维护、剪枝与修复由 construction/control 调用显式
    CRUD 完成。GraphStore 删除节点时清理关联图边的既有语义不适用于 hierarchy。
23. **Storage capability 唯一来源**：能力集合只包含 KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS/ENTITY
    （ENTITY 为 F07-D 新增第七席）；
    `has_*(name)` 由集合/命名端口表推导，未声明端口访问抛 `UnsupportedStorageCapabilityError`。
24. **端口单一入口且全量自动**：每个 capability 一对 `xxx(name="default")` / `has_xxx(name="default")`
    方法（无 property 快捷方式、无 `*_port` 后缀双入口）；七类 `*_store` 命名空间下所有非
    `default` 具名实例**声明即端口**（encrypted KV 的明文 raw 若以具名声明会随之暴露，raw 推荐
    inline 声明）；上层不得绕过 `StoreManagerProducer` 直接解析 Store 具名实例。
    ENTITY 的 **default** 端口额外接受「`entity_store.default` 声明即端口」的兜底解析
    （params 显式引用 → `entity_store.default` → 无该能力）：配置合并是实例级整体覆盖，
    若强制 params 引用，既有部署须在自身 config.yml 全量抄写 `store_manager.default.params`
    （含全部 `*_recaller` 键），漏抄即静默丢一路召回；受管成员本就并非都由 params 引用键
    驱动（`domain_store` 由 manager 工厂内部构建 + `bind_domain_store` 注入）。
    端口表的值必须非 None——builder 返 None（增强层未配即降级）的实例在
    `_named_ports` 统一丢弃。
25. **检索路径独立于 capability**：`DomainStore` 提供 recall/recall_and_get/retrieve，并以
    实例级稳定的 `preferred_retrieval_pipeline()` 选择首选入口；路径值不加入 capability。
26. **统一授权不可绕过**：`DomainStore` 领域接口和 `StoreManager` 暴露的 Store 代理端口都先执行
    `StorageSecurity.authorize`；默认 AllowAll 可省略 access。Store 自身 `security` 表示数据保护。
    **ENTITY 端口的授权适配**（`_AuthorizedEntityStoreProxy`，与通用代理分开实现）：
    `find_by_entity_text_hash` / `find_by_linked_memory_id` → `SEARCH`；`execute_operations`
    → 按 batch 内 op 类型派生动作集逐个授权（`INSERT`→ADD、`LINK`/`UNLINK_UPDATE`→UPDATE、
    `DELETE`→DELETE；空 batch 零授权，因为它不执行任何动作）；`ensure_index` → `ADMIN`。
    交给 `authorize` 的 Scope 是**有损近似** `Scope(space=space_id, user=filters.actor_id)`：
    `space` 是 `space_id_from_scope` 的算值（可能是 space id、org id 或字面量 `"default"`），
    `org`/`agent`/`session` 恒空，`execute_operations` 无 `filters` 故 `user` 也恒空。
    自定义 `StorageSecurity` 不得对 `resource == "entity"` 按 org/agent/session 判定；
    写入侧的 actor 隔离由 `EntityRecord.filters` 记录内字段承担，不由授权入参承担。
27. **写接口覆盖范围由实现决定**：`DomainStore.add`/`update`/`delete` 落成哪些索引形式取决于该
    实现的能力，调用方不得假定「只写记忆本体」。`IndexWriteMode` / `IndexRemoveMode` 表达调用方
    意图，能否拆分由实现按自身能力决定——不具备检索索引能力的实现在 `RETRIEVAL_ONLY` /
    `SOFT` 时为空操作。差额由 IndexBuilder 补齐，匹配关系由装配期约定保证。
28. **正排的 key 方案与编解码是跨层共享契约**：`memory_key` 与 `memory_codec` 归口
    `common.type_def`，写侧在 `ForwardIndexBuilder` 与 `KVLifecycleManager` 回写、读侧在
    `DomainStore.get`/`list` 与 `load_units`/`list_units` helper，`KVStore.list`
    本身也按 `MEMORY_KEY_PREFIX` 扫描。这是正排作为唯一需要**两向投影**的索引形式的固有代价：
    实现分居两层，靠这对共享契约对齐。
29. **命名数据面共享物理 Store 集**：`manager.domain_store(name)` 的多套命名数据面差异仅在
    检索 profile（`preferred_retrieval_pipeline` + recallers 组合）；同名多次调用返回同一实例。
    跨 Store 集的整栈切换走 F02 Routing（`store_manager.active`），不属命名数据面语义。
30. **所有 XXXStore 获取经 StoreManager**：消费者不直接调 Store Producer 解析具名后端
    （F07-D 起 `EntityStore` 一并纳入——写入侧 `HybridIndexBuilder` 与召回侧 `KeywordRecaller`
    改经 `manager.entity(name)`，`EntityStoreProducer.dep` 旁路移除，读写共享同一实例由
    manager 保证而非配置纪律）；
    端口选择键（`params.kv_store` / `vector_store` / `entity_store` / ... / `domain_store`）的值是 manager 端口/
    数据面名（仅字符串，inline dict 拒绝）。纯「按 unit_id 点读」与「列表/全量扫描」场景注入
    KVStore 端口并用 `load_units` / `list_units` helper，不过度依赖 `DomainStore`——control 面
    （Engine×2 / LifecycleManager / EvolveJob / MiddleToLongJob）真源读写全部直连 KV 端口，
    DomainStore 的消费方是检索路径（PipelineRetriever）与一体化写路径（UnifiedIndexBuilder）。
31. **health 聚合覆盖全部已声明端口**：增强层的降级发生在**装配期**（builder 返 None →
    无该 capability → 消费方跳过），不在探活期；端口一旦声明且构造成功，后端不可达就应让
    `manager.health()` 报错，不给任何 capability 开健康豁免。运行期容错仍由消费方各自的
    try/except 承担（entity 后端故障不影响正排/倒排/向量的写入与召回）。
    **部署注意**：把 `health()` 当 liveness probe 的部署，增强层后端抖动会触发重启；
    建议用作 readiness，或在部署侧区分 required/optional 探活。

## 接口契约

### StoreManager（管理面，`store_manager.py`）

| 类别 | 接口 | 语义 |
|---|---|---|
| 端口 | `kv/vector/fulltext/graph/fusion/fs/entity(name="default")` 及 `has_*(name="default")` | 暴露经过统一授权代理的完整 Store 契约；单一入口（无 property、无 `*_port` 后缀）；未声明端口抛 `UnsupportedStorageCapabilityError` |
| 数据面 | `domain_store(name="default")` / `has_domain_store(name="default")` | 取命名数据面实例；实现需缓存（同名多次返回同一实例） |
| 能力 | `capabilities()` | 返回不可变标准端口能力集合 |
| 横切 | `security` / `health()` | 统一授权入口并聚合全部命名端口的健康检查 |

`CompositeStoreManager` 是默认实现（组合七类 Store + 命名端口表 + `_AuthorizedStoreProxy`，
ENTITY 端口用 `_AuthorizedEntityStoreProxy`）；
数据面实例由 manager 工厂内的 `DomainStoreProducer.build` 构建并经 `bind_domain_store(ds, name)`
注入。`StoreManagerProducer.TOP_NAME = "store_manager"`（F08 起从 `storage` 更名）；统一
manager 以 target 自注册，默认 `composite`。

**全局唯一 manager（F08）**：进程内共享一个 StoreManager，由 `globals.store_manager` 指名
（值 = store_manager 命名空间下的实例名）。`StoreManagerProducer.resolve(config)` 按
params 显式覆盖 → `globals.store_manager` → `"default"` 三级链解析；未声明实例名抛
`ValidationError`，不做匿名兜底构建。消费者不再逐个声明 `storage` 引用。

**命名数据面声明**：`store_manager.<inst>.params.domain_stores: {<name>: {覆盖键}}`——每套
经 `DomainStoreProducer.build` 构建（差异键：`preferred_retrieval_pipeline` / `domain_store_target`
/ `*_recaller` 选择键与 `vector_enabled` 等开关 overlay），段内不允许声明 `"default"`。

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

`bind_recallers` 仅落 `CompositeDomainStore`（手工/测试接线口），不下沉 `DomainStore` ABC；
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

### 共享读 helper（`kv.py::load_units` / `list_units`）

`load_units(kv, scope, unit_ids) -> list[MemoryUnit]`：按 unit_id 从 KV 真源点读
MemoryUnit 列表（`memory_key` + `loads`）——缺失省略、按输入顺序返回、重复 id 各自返回、
不做任何过滤/复核（lifecycle/valid-time 判定属调用方职责）。

`list_units(kv, scope, *, offset, limit, memory_types, filters, extensions) ->
tuple[list[MemoryUnit], int]`：列表读的对称件——`kv.list` + 逐条 `loads` 反序列化（非
MemoryUnit 记录自然过滤），返回 `(items, count)`；过滤/计数/分页语义全部由 `KVStore.list`
契约承担，helper 不做二次过滤。

两者供 Dedup._load_unit / Governor._find / Evolver 源读 / KeywordRecaller 实体扩展等纯按
id 点读场景，以及 Engine（点读/全量扫描/分页 list）、LifecycleManager sweep、
EvolveJob/MiddleToLongJob 候选拉取等列表场景共用——这类场景注入 KVStore 端口而非
DomainStore。

### BaseStore（基类，`base.py`）

```python
class StoreType(str, Enum):
    KV / FULLTEXT / VECTOR / GRAPH / FUSION / FS

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

**ttl** 单位为秒（float），`0` 表示永不过期。

#### EncryptedKVStore（`kv_impl/encrypted_kv_store.py`）

`encrypted` 是 KVStore 装饰器实现，用于把加密能力透明套到任意 raw KV 后端之上。

| 方法 | 行为 |
|------|------|
| `insert` / `update` | 构造 `SecurityContext(scope, purpose, metadata)` 与 AAD，调用 `SecurityProvider.encrypt` 后写入 raw KV |
| `get` / `scan` / `mget` | 从 raw KV 读取密文字节，调用 `SecurityProvider.decrypt` 后返回明文字节；任一解密失败抛 `BackendError`，不跳过坏数据。`mget` 委托 raw 一次性批量取密文（raw 缺失即抛 `NotFoundError`）后**逐项解密**——AAD 绑定 scope+key+purpose，各 key AAD 不同，不能批量统一解密 |
| `list` | 扫描目标 Scope 的 `/memory/` 密文并逐条解密，再执行统一过滤、计数、排序和分页；不调用 raw KV 的 `list` |
| `exists` / `delete` / `scopes` | 直接委托 raw KV，不读取或改写 value |

装配参数：

| 参数 | 语义 |
|------|------|
| `raw_kv_store` | 必填，指向被装饰的 raw KVStore 具名实例或内联配置；不得指向当前 encrypted 实例自身 |
| `security` | 必填，指向 `common.security.SecurityProvider` 具名实例或内联配置 |

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
| `seed_ids` | `(scope, tokens: set[str]) -> list[str]` | 在 scope 内按关键词/词项定位种子节点 id |
| `insert` | `(scope, nodes=None, edges=None) -> None` | 在 scope 下新建节点/边；id 已存在时报冲突 |
| `update` | `(scope, nodes=None, edges=None) -> None` | 更新已有节点/边；id 不存在时报缺失 |
| `delete` | `(scope, node_ids=None, edge_ids=None) -> None` | 在 scope 内按 id 删除节点（连带其关联边）/ 边（幂等） |
| `get` | `(scope, node_ids: list[str]) -> list[Node]` | 在 scope 内按 id 点查节点；缺失的 id 从结果中省略 |
| `search` | `(scope, query: GraphQuery) -> list[Node]` | 在 scope 内从 query.start_id 出发扩展邻域/子图（多跳遍历） |

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

### EntityStore（`entity_store.py`）

实体反向索引（实体 → 关联的 memory）。F07-D 起是 `StorageCapability` 第七席 ENTITY，经
`manager.entity(name)` 取用，与其余六类同构。

| 方法 | 签名 | 语义 |
|------|------|------|
| `ensure_index` | `() -> None` | 确保索引已创建并就绪；实现可懒触发（首次读写时） |
| `find_by_entity_text_hash` | `(space_id, entity_text_hashes, *, filters, limit=500) -> list[EntityRecord]` | 按 `entity_text_hash` keyword term 精确查询（不做向量归并） |
| `find_by_linked_memory_id` | `(space_id, memory_id, *, filters) -> list[EntityRecord]` | 反查：哪些实体关联了该 memory（unlink 用） |
| `execute_operations` | `(space_id, operations) -> EntityBatchResult` | bulk 变更（INSERT/LINK/UNLINK_UPDATE/DELETE 混合） |

**隔离模型是本层唯一的 scope 契约偏离**：第一入参是 `space_id: str`（`space_id_from_scope`
的算值：`scope.space` → `scope.org` → 字面量 `"default"` 三级降级，走后端 routing）而非
`Scope`；额外的隔离维度是 `EntityStoreFilters.actor_id`（= `scope.user`）单段 term。
agent/session **不作**隔离维度——实体是 user 级知识，同 user 下跨 agent、跨 session 共享。
隔离仍由存储层强制（不变量 1），只是维度表达与五段模型不同构；代价由授权代理承担
（不变量 26 的有损 scope 近似）。

**partial failure 语义**：`execute_operations` 是 per-item 粒度——一条失败不影响其他，
失败 id 经 `EntityBatchResult.failed_ids` 回传，**不抛异常**（与主链路 `BulkWriteError`
的 all-or-nothing 相反）。

**装配期降级**：entity 是增强层，builder 在必填连接参数（`hosts`）未配时返回 `None` 而
非抛错 → 无 ENTITY 能力 → 两侧消费方跳过（不变量 24 的端口表 None 过滤、不变量 31）。
这与其余六类"缺必填参 `require_param` 抛错"的约定不同，是有意的差异。

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

目标层级索引 metadata 在既有 `unit_id`、`content_layer`、`tier`、`lifecycle`、`seq`
基础上增加：

| 键 | 表示 |
|---|---|
| `hierarchy_kind` | kind 的字符串值；空 hierarchy 时缺省 |
| `hierarchy_role` | role 的字符串值；空 hierarchy 时缺省 |
| `parent_id` | 直接父 id；根或未挂接时为空串 |
| `span_start` | ISO 8601 区间起点；未声明区间时缺省 |
| `span_end` | ISO 8601 区间终点；未声明区间时缺省 |

同一 unit 的 L0/L1/L2 VectorRecord 必须携带相同的 hierarchy metadata；现有记录 id
格式保持不变。

### 全文（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `Document` | id / text / metadata |
| `TextQuery` | text / top_k / filters: FilterExpr \| None / extensions: dict[str, Any] |

Document 使用与 VectorRecord 相同的五个 hierarchy metadata 键，并保留既有
`content_layer`。L0/L1/L2 文档的当前 id 规则保持不变；增加 metadata 不改变主键。

### 层级过滤与区间表示（目标契约，尚未实现）

层级过滤继续使用现有 `FilterClause(field, op, value)`，不新增查询结构：

- kind/role/parent 精确过滤使用 `EQ`，例如
  `field="hierarchy_kind"`、`field="parent_id"`。
- 区间相交 `[query_start, query_end]` 表示为
  `span_start <= query_end AND span_end >= query_start`，即分别使用 `LTE` 与 `GTE`。
- 时间值统一写为 ISO 8601 字符串；同一索引内必须规范到可按时间顺序比较的统一时区格式。
- filters 只承载 scope 之外的谓词，scope 仍是 Store 方法的显式第一参数。

后端若不能原生执行区间谓词，可以在同 scope 候选上做等价后过滤，但不得放宽结果语义。
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

## 实现注册机制

```
jiuwen_memory/storage/<store>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")

jiuwen_memory/storage/store_manager_impl/   # 管理面实现（CompositeStoreManager）
jiuwen_memory/storage/domain_store_impl/    # 数据面实现（CompositeDomainStore）
```

各 Producer：`StoreManagerProducer` / `DomainStoreProducer` / `KvProducer` / `FulltextProducer` /
`VectorProducer` / `GraphProducer` / `FusionProducer` / `FsProducer` / `EntityStoreProducer`。
注册由 `storage.bootstrap.register_backends` 统一触发。`DomainStoreProducer` 不是平级
YAML 入口——domain_store 由 manager 工厂内构建（manager 是唯一装配入口）。

具体 Store target 名与实现文件列表归 `jiuwen_memory/storage/AGENTS.md` 维护；本 spec 只固化
Store 抽象、跨后端不变量与注册机制。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S03-control | Engine/LifecycleManager/Jobs 真源读写直连注入的 KVStore 端口（点读 `load_units` / 列表 `list_units`）；目标生命周期/治理操作按显式 Scope 定位，全局 sweep/offboarding 才跨 Scope 枚举 |
| S04-retrieval | Retriever 经 `StoreManagerProducer.resolve` 取全局 manager 并持其 `domain_store()`；兼容 Recaller 由 manager 工厂按配置在构建期同步组装（具名构建用 `config.name` 预注册、匿名构建用合成名预注册打破循环） |
| S05-construction | 构建层通过本层抽象做真源与索引持久化 |
| S07-common | 定义 `MemoryUnit.hierarchy`、`HierarchyKind`、`HierarchyRole` 与 `FilterClause` |
| S08-config | Store 连接参数与 `*.active` 可由 ConfigSource 晚绑定；切换后端不包含数据迁移 |
| architecture.md §5 | 可配置真源形态（文档/结构化）与多后端 |
