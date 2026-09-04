# S06 — 存储层（Storage Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | jiuwen_memory/storage/ |
| 最近一次修订日期 | 2026-09-04 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/construction/F07-memory-write-entry.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F05-cloud-engine-design.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/retrieval/F05-storage-retrieval-pipelines.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F08-memory-tree.md，docs/features/common/F04-security-interfaces-and-encryption.md，docs/features/storage/F02-encrypted-storage.md，docs/features/storage/F03-postgres-backend.md，docs/features/storage/F04-storage-ssl.md，docs/features/storage/F05-unified-storage-design.md，docs/features/storage/F06-composite-recaller-assembly.md |
## Metadata 物理存储契约

索引记录保留 `system_metadata.<key>` 和 `user_metadata.<key>` 的逻辑路径。Milvus 与
PostgreSQL JSONB 使用完整路径作 key；Elasticsearch 写入时展开为对象层级，使
`metadata.user_metadata.<key>` 等 DSL 路径可下推。Store record 自身的 `metadata` 名称不变。
## 范围 / 边界

**管什么**：
- 统一 Storage 契约、CompositeStorage 默认组合实现与底层能力发现
- StorageProducer 注册与 `storage` 配置命名空间
- MemoryUnit 领域 add/update/delete/get/list
- StorageSecurity 数据面授权和 StoreSecurity 数据保护能力边界
- 可配置真源（文档/结构化）的 KV 存储抽象
- 原文业务端口（RawDataStore）及其受权访问
- KV 加密装饰器（EncryptedKVStore）
- 多后端索引存储抽象：向量（VectorStore）、全文（FulltextStore）、图（GraphStore）、融合（FusionStore）、实体（EntityStore）、文件系统（FSStore）
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
2. **记录 id 在 scope 内唯一**：`insert` 冲突 / `update` 缺失按 `(scope, id)` 判定；后端可用完整五维 `scope + id` 生成物理主键，保证同一逻辑 id 在不同 Scope 下互不冲突。
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
23. **Storage capability 唯一来源**：标准 Store 能力集合包含
    KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS/ENTITY；`has_*()` 由能力集合推导，未声明端口访问抛
    `UnsupportedStorageCapabilityError`。RawDataStore 是独立业务端口，不计入 capability，
    通过 `has_raw_port(name)` / `raw_port(name)` 发现与访问。
24. **命名端口仍受 Storage 管控**：`has_*_port(name)` 与 `*_port(name)` 成对使用；默认端口名为
    `default`，分层索引可使用 `layers_l0` / `layers_l1`。Construction、Retrieval、Control
    不得绕过 StorageProducer 直接解析 Store 具名实例；Entity 也必须经 `entity_port(name)`。
25. **检索路径独立于 capability**：Storage 提供 recall/recall_and_get/retrieve，并以全局稳定的
    `preferred_retrieval_pipeline()` 选择首选入口；路径值不加入 capability。
26. **统一授权不可绕过**：MemoryUnit 领域接口和 Storage 暴露的 Store、Raw、Entity 代理端口都先执行
    `StorageSecurity.authorize`；默认 AllowAll 可省略 access。Store 自身 `security` 表示数据保护。
27. **写接口覆盖范围由实现决定**：`add`/`update`/`delete` 落成哪些索引形式取决于该 Storage
    实现的能力，调用方不得假定「只写记忆本体」。`IndexWriteMode` / `IndexRemoveMode` 表达调用方
    意图，能否拆分由实现按自身能力决定——不具备检索索引能力的实现在 `RETRIEVAL_ONLY` /
    `SOFT` 时为空操作。差额由 IndexBuilder 补齐，匹配关系由装配期约定保证。
28. **正排的 key 方案与编解码是跨层共享契约**：`memory_key` 与 `memory_codec` 归口
    `common.type_def`，写侧在 `ForwardIndexBuilder`、读侧在 `Storage.get`/`list`，`KVStore.list`
    本身也按 `MEMORY_KEY_PREFIX` 扫描。这是正排作为唯一需要**两向投影**的索引形式的固有代价：
    实现分居两层，靠这对共享契约对齐。
29. **JobStateStore 是 Control 基础设施直连例外**：任务状态由 Control 模块拥有契约和保留期，
    底层可使用 KVStore（`KVJobStateStore`），但 `ingest_job.py` 不 import `KvProducer`、
    `KVStore` 或 key prefix/codec——key 与序列化细节收口在 `job_impl/job_state.py` 的 adapter
    内。JobStateStore 的读写接口显式接收 Scope 和可选 owner，不复用 Memory StorageSecurity 的
    资源名。除此之外，Construction/Retrieval 及其他上层模块不得直连任何底层 Store。

## 接口契约

### Storage（统一门面，`storage.py`）

| 类别 | 接口 | 语义 |
|---|---|---|
| 领域操作 | `add/update(..., mode: IndexWriteMode = ALL)` / `delete(..., mode: IndexRemoveMode = HARD)` / `get` / `list` / `scopes` | 操作或枚举 MemoryUnit 真源；get 保序并省略缺失，list 返回 items 与 count |
| 能力 | `capabilities()` / `has_kv()` 等 | 返回不可变标准 Store 端口能力 |
| 端口 | `kv/vector/fulltext/graph/fusion/entity/fs` 及 `*_port(name)`；Raw 使用 `raw_port(name)` | 暴露经过统一授权代理的完整 Store/业务端口契约；命名端口通过 `has_*_port(name)` 判断，未声明能力时报错 |
| 检索适配 | `preferred_retrieval_pipeline()` / `recall` / `recall_and_get` / `retrieve` | 供 Retriever 选择 recall/get/rank 三步的组合位置 |
| 横切 | `security` / `health()` | 统一授权入口并聚合声明能力的健康检查 |

`CompositeStorage` 是默认实现。一体化实现可以只实现 Storage 的领域和首选检索入口；只有完整
提供某个标准 Store 契约时才声明对应 capability。

**写接口的覆盖范围是实现相关的**：`add`/`update`/`delete` 的语义是「按该实现的能力落地」，
而非「只写记忆本体」。`CompositeStorage` 不持有 Chunker/Embedder，无投影能力，故只落本体；
一体化平台可在一次 `add` 内建立全部索引形式。差额由 `IndexBuilder` 补齐，两者的匹配由
**装配期约定**保证（见 S05 不变量 15），不引入运行时能力协商。

两个枚举把调用方意图透传到实现：`IndexWriteMode`（`ALL` / `FORWARD_ONLY` /
`RETRIEVAL_ONLY`）表达写入范围，`IndexRemoveMode`（`SOFT` / `HARD`）表达删除语义——
`SOFT` 软删除只移出检索索引（search/recall 不再召回），本体保留、get/list 仍可读。
`UnifiedIndexBuilder` 原样下传，不代实现判断；不具备检索索引能力的实现在
`RETRIEVAL_ONLY` / `SOFT` 时为空操作，而 `FORWARD_ONLY` 时应**至少保证本体被写到**——
多刷新一次检索索引无害，漏写本体则丢数据。

**原文**（对话消息）不是 MemoryUnit 真源，也不是检索索引；但它仍是需要统一授权、Scope 隔离、
保留和加密策略的数据面。Storage 因此提供独立的 `RawDataStore` 业务端口，不把原文误报为
标准 Store capability：

| 方法 | 签名 | 语义 |
|---|---|---|
| `append_raw` | `(scope, units, *, retain_limit=0, access=None) -> None` | 追加当前 Scope 的原文；`0` 表示不按数量淘汰 |
| `list_raw` | `(scope, *, limit=100, access=None) -> list[MemoryUnit]` | 按 `t_ingest` 倒序列出最近原文 |
| `delete_raw` | `(scope, record_ids, *, access=None) -> None` | 仅删除当前 Scope 指定原文，幂等 |
| `scopes` | `(*, access=None) -> list[Scope]` | 枚举含原文的 Scope，仅供空间治理使用 |
| `usage` | `(scope, *, access=None) -> RawDataUsage` | 返回当前 Scope 的原文条数和物理字节数 |
| `purge` | `(scope, *, access=None) -> RawDataUsage` | 清空当前 Scope 的原文并返回清理前用量 |

标准 Storage 装配路径中的 `raw_port(name)` 返回经过 `StorageSecurity` 授权代理的端口；调用方
必须显式传入 Scope 和可选 `StorageAccessContext`。该授权资源当前为 `raw`。测试或自定义接线若
直接注入 RawDataStore，调用方必须自行提供同等的授权边界，不能把这种便利接线当成生产绕过路径。
默认 `KVRawDataStore` 只在 Storage 层内部把该契约适配到 KV，集中拥有 `/messages/` 前缀、
`MemoryUnit` 编解码、按摄入时间排序和 retention 淘汰；Evolver 不得导入 `KVStore`、拼接 prefix
或调用 codec。EncryptedKVStore 根据 `/messages/` 前缀使用 `raw_message` 加密 purpose；它与
StorageSecurity 的授权资源 `raw` 是两个不同层级的名称。

原文的跨 Scope 管理不由普通业务调用方自行扫描：`CompositeStorage.scopes()` 合并主 KV 与所有
Raw port 的 Scope，`MemoryEngine.purge_space` 与 `SpaceManager.delete/usage` 负责 offboarding、
计数和清理，并且必须把原文纳入同一 `org + space` 的管理范围。SpaceManager 对原文使用 Raw
端口的管理方法；它只为 MemoryUnit/space 元数据使用受控的 KV 扫描作为基础设施 adapter。未来
Raw 后端拆分时应由等价的管理 adapter 提供相同语义，不得把该例外扩散到 Construction/Retrieval。

**EntityStore** 是标准 Storage capability：`StorageCapability.ENTITY`、`has_entity_port(name)`
和 `entity_port(name)` 共同构成发现与访问契约。Entity 端口的方法以完整 `Scope` 为首参，
由 CompositeStorage 建立 `StorageSecurity` 代理；旧的 `space_id + filters` 后端只允许在
Storage 内部通过 adapter 兼容。Construction 的 EntityIndexBuilder 与 Retrieval 的
KeywordRecaller 只能接收 Storage 提供的 Entity 端口，不得直接调用 `EntityStoreProducer`。

### EntityStore（实体反向索引）

Entity 是标准 Store capability，公开端口的领域操作如下。通过 `entity_port(name)` 取得的端口
同样接受可选的 `access` keyword，并由 StorageSecurity 以资源 `entity` 授权。

| 方法 | 签名 | 语义 |
|---|---|---|
| `ensure_index` | `() -> None` | 确保实体索引可用；属于装配/运维准备，不读取业务 Scope 数据 |
| `find_by_entity_text_hash` | `(scope, hashes, *, limit=500) -> list[EntityRecord]` | 在**完整 Scope**内按归一化实体 hash 精确查询 |
| `find_by_linked_memory_id` | `(scope, memory_id) -> list[EntityRecord]` | 在完整 Scope 内反查包含指定记忆 id 的实体，供 unlink 使用 |
| `execute_operations` | `(scope, operations) -> EntityBatchResult` | 对完整 Scope 执行 INSERT/LINK/UNLINK_UPDATE/DELETE 批量变更；逐项失败在 `failed_ids` 返回，后端整体故障仍抛异常 |

`EntityRecord` 中遗留的 `space_id` 与 `filters` 是后端投影，不是上层隔离输入。旧
`space_id + EntityStoreFilters` 后端只能由 Storage 内部 adapter 从 Scope 派生；上层不可传入或
拼接这些字段。后端必须使同一逻辑实体 id 在任何两个不同五维 Scope 中物理隔离。Elasticsearch
实现以完整 Scope 加逻辑实体 id 计算物理文档 id，space routing 仅用于定位 shard，不能替代隔离。

`StorageProducer.TOP_NAME = "storage"`。统一 Storage 实现以 target 自注册；默认
`CompositeStorage` target 为 `composite`。具名引用必须复用同一 Storage 实例，使 Kernel、
Retriever 及后续迁移的 Construction/Control 不重复装配底层 Store。

### BaseStore（基类，`base.py`）

```python
class StoreType(str, Enum):
    KV / FULLTEXT / VECTOR / GRAPH / FUSION / FS / ENTITY

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
```

各 Producer：`StorageProducer` / `KvProducer` / `FulltextProducer` / `VectorProducer` /
`GraphProducer` / `FusionProducer` / `FsProducer` / `EntityStoreProducer`。
注册由 `storage.bootstrap.register_backends` 统一触发。

具体 Store target 名与实现文件列表归 `jiuwen_memory/storage/AGENTS.md` 维护；本 spec 只固化
Store 抽象、跨后端不变量与注册机制。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S03-control | Engine 通过 Storage/IndexBuilder 读写真源；目标生命周期/治理操作按显式 Scope 定位。全局 sweep/offboarding 才跨 Scope 枚举；SpaceManager 的 usage/delete 必须覆盖 RawDataStore 原文 |
| S04-retrieval | Retriever 经 StorageProducer 获取统一 Storage；CompositeStorage 的兼容 Recaller 由本层工厂按配置在构建期同步组装（具名构建用 `config.name` 预注册、匿名构建用合成名预注册打破循环） |
| S05-construction | 构建层通过本层抽象做真源与索引持久化 |
| S07-common | 定义 `MemoryUnit.hierarchy`、`HierarchyKind`、`HierarchyRole` 与 `FilterClause` |
| S08-config | Store 连接参数与 `*.active` 可由 ConfigSource 晚绑定；切换后端不包含数据迁移 |
| architecture.md §5 | 可配置真源形态（文档/结构化）与多后端 |
