# S06 — 存储层（Storage Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/storage/ |
| 最近一次修订日期 | 2026-08-18 |
| 关联特性补充 | docs/features/api/F04-memory-metadata-separation.md |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/control/F05-cloud-engine-design.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/retrieval/F05-storage-retrieval-pipelines.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F04-security-interfaces-and-encryption.md，docs/features/storage/F02-encrypted-storage.md，docs/features/storage/F03-postgres-backend.md，docs/features/storage/F04-storage-ssl.md，docs/features/storage/F05-unified-storage-design.md |
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
- KV 加密装饰器（EncryptedKVStore）
- 多后端索引存储抽象：向量（VectorStore）、全文（FulltextStore）、图（GraphStore）、融合（FusionStore）、文件系统（FSStore）
- 统一 CRUD 动词（insert / delete / update / get）
- 检索型存储的 search 查询
- scope 原生隔离（scope 为显式第一入参，物理约束在该 scope 内）

**不管什么**：
- 不管理 grant/revoke、授权策略生命周期或业务权限模型
- 不做检索编排（由 `src/retrieval` 层负责）
- 不做索引构建逻辑（由 `src/construction` 层负责）
- 不实现具体后端（实现在 `*_impl/` 下，通过 Producer 注册）

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
19. **Storage capability 唯一来源**：能力集合只包含 KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS；
    `has_*()` 由集合推导，未声明端口访问抛 `UnsupportedStorageCapabilityError`。
20. **命名端口仍受 Storage 管控**：`has_vector_port(name)` 与 `vector_port(name)` 等成对使用；
    默认端口名为 `default`，分层索引可使用 `layers_l0` / `layers_l1`，上层不得绕过
    StorageProducer 直接解析 Store 具名实例。
21. **检索路径独立于 capability**：Storage 提供 recall/recall_and_get/retrieve，并以全局稳定的
    `preferred_retrieval_pipeline()` 选择首选入口；路径值不加入 capability。
22. **统一授权不可绕过**：MemoryUnit 领域接口和 Storage 暴露的 Store 代理端口都先执行
    `StorageSecurity.authorize`；默认 AllowAll 可省略 access。Store 自身 `security` 表示数据保护。

## 接口契约

### Storage（统一门面，`storage.py`）

| 类别 | 接口 | 语义 |
|---|---|---|
| 领域操作 | `add/update/delete/get/list/scopes(..., access=None)` | 操作或枚举 MemoryUnit 真源；get 保序并省略缺失，list 返回 items 与 count |
| 能力 | `capabilities()` / `has_kv()` 等 | 返回不可变标准 Store 端口能力 |
| 端口 | `kv/vector/fulltext/graph/fusion/fs` 及 `*_port(name)` | 暴露经过统一授权代理的完整 Store 契约；命名端口通过 `has_*_port(name)` 判断，未声明能力时报错 |
| 检索适配 | `preferred_retrieval_pipeline()` / `recall` / `recall_and_get` / `retrieve` | 供 Retriever 选择 recall/get/rank 三步的组合位置 |
| 横切 | `security` / `health()` | 统一授权入口并聚合声明能力的健康检查 |

`CompositeStorage` 是默认实现。一体化实现可以只实现 Storage 的领域和首选检索入口；只有完整
提供某个标准 Store 契约时才声明对应 capability。

`StorageProducer.TOP_NAME = "storage"`。统一 Storage 实现以 target 自注册；默认
`CompositeStorage` target 为 `composite`。具名引用必须复用同一 Storage 实例，使 Kernel、
Retriever 及后续迁移的 Construction/Control 不重复装配底层 Store。

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
| `VectorQuery` | vector: list[float] / top_k / filters: FilterExpr \| None |

### 全文（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `Document` | id / text / metadata |
| `TextQuery` | text / top_k / filters: FilterExpr \| None |

### 图（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `Node` | id / label / properties |
| `Edge` | id / source / target / relation / properties |
| `GraphQuery` | start_id / relation / depth / limit |

### 融合（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `FusionRecord` | id / vector / text / scalars / value: bytes |
| `FusionQuery` | vector / text / scalar_filters: FilterExpr \| None / top_k / vector_weight |

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
src/storage/<store>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`StorageProducer` / `KvProducer` / `FulltextProducer` / `VectorProducer` /
`GraphProducer` / `FusionProducer` / `FsProducer`。
注册由 `storage.bootstrap.register_backends` 统一触发。

具体 Store target 名与实现文件列表归 `src/storage/AGENTS.md` 维护；本 spec 只固化
Store 抽象、跨后端不变量与注册机制。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S03-control | Engine 通过 KVStore 读写真源；目标生命周期/治理操作按显式 Scope 定位，全局 sweep/offboarding 才跨 Scope 枚举 |
| S04-retrieval | Retriever 经 StorageProducer 获取统一 Storage；CompositeStorage 的兼容 Recaller 在检索装配期绑定 |
| S05-construction | 构建层通过本层抽象做真源与索引持久化 |
| S08-config | Store 连接参数与 `*.active` 可由 ConfigSource 晚绑定；切换后端不包含数据迁移 |
| architecture.md §5 | 可配置真源形态（文档/结构化）与多后端 |
