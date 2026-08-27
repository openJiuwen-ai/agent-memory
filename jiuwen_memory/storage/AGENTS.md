# Agent Memory Storage（存储层）

**规约文档**：[S06-storage.md](../../docs/specs/S06-storage.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

`Storage` 为上层提供 MemoryUnit 领域操作与能力发现，`CompositeStorage` 组合六类标准
Store 并暴露授权代理端口。底层 Store 统一 CRUD 动词（insert/delete/update/get）和检索型
`search`。**scope 隔离是存储层的原生职责**。

## 模块地图

| 文件 | 职责 |
|---|---|
| `storage.py` | Storage 统一契约与 StorageProducer：MemoryUnit 领域操作（`add`/`update` 带 `IndexWriteMode` 写入范围参数、`delete` 带 `IndexRemoveMode` 软/硬删除参数）、能力发现、底层端口与检索适配入口。原文不在此列——它不是存储领域概念，由构建层注入独立 `KVStore` 自行读写 |
| `security.py` | StorageSecurity 通用授权与 StoreSecurity 数据保护能力标识 |
| `base.py` | BaseStore 基类：所有存储后端的自描述契约（store_type / health） |
| `types.py` | 存储层数据类型：`IndexWriteMode`/`IndexRemoveMode` 写删语义枚举、KVMemoryListResult/VectorRecord/Document/Node/Edge/FusionRecord/FileStat 等 |
| `kv.py` | KVStore 接口：键值存储，统一 CRUD + MemoryUnit 列表查询 + 范围枚举 |
| `vector.py` | VectorStore 接口：向量存储，统一 CRUD + ANN 检索 |
| `graph.py` | GraphStore 接口：属性图存储，节点与边统一 CRUD + 邻域遍历 |
| `fulltext.py` | FulltextStore 接口：全文倒排索引存储，统一 CRUD + 关键词检索（BM25） |
| `fusion.py` | FusionStore 接口：融合存储（向量+倒排+正排一体） |
| `fs.py` | FSStore 接口：文件系统存储（原始负载/二进制资产） |
| `entity_store.py` | EntityStore 独立接口：以 `space_id` routing + actor filter 隔离的实体反向索引；不属于 StorageCapability 六端口 |
| `_support.py` | 后端实现共用：异常归一（`wrap_backend`）、scope 派生（`scope_dims`/`scope_segments`）、SSL 配置读取（`read_ssl_config`）；`SslConfig` 与 scheme 校验复用 `common._support` |
| `_pg.py` | PostgreSQL 后端共享的惰性连接池、schema 工具与 FilterExpr SQL 编译；`dsn` 支持 ConfigSource 晚绑定 |
| `kv_impl/` | KVStore 实现目录（memory / sqlite / redis / encrypted / postgres）及共用的 `memory_list.py` 兼容逻辑；连接型后端支持 `kv_store.*` 晚绑定 |
| `vector_impl/` | VectorStore 实现目录（memory / milvus / pgvector）；`uri`/`dsn` 晚绑定 |
| `graph_impl/` | GraphStore 实现目录（memory / nano_graphrag）；`working_dir` 晚绑定 |
| `fulltext_impl/` | FulltextStore 实现目录（memory / elasticsearch）；`hosts` 晚绑定 |
| `fusion_impl/` | FusionStore 实现目录（memory / milvus_graph）；`uri`/`working_dir` 晚绑定 |
| `fs_impl/` | FSStore 实现目录（local）；`root` 晚绑定 |
| `entity_impl/` | EntityStore 实现目录（elasticsearch）；独立装配，不经 Storage capability 路由 |
| `storage_impl/` | Storage 实现目录；`CompositeStorage` 以 `composite` target 自注册 |
| `bootstrap.py` | 统一触发六类 Store 后端与 Storage 实现注册 |

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
   capability 只包含 KV/VECTOR/FULLTEXT/GRAPH/FUSION/FS。`has_*()` 必须由不可变集合推导；
   未声明端口直接访问时抛 `UnsupportedStorageCapabilityError`。

12. **统一授权覆盖领域接口和暴露端口**
   Storage 顶层操作与 `storage.vector.get()` 等端口调用先经 `StorageSecurity`；默认
   `AllowAllStorageSecurity` 允许省略 access。各 Store 同时暴露自身 `security`，未启用数据
   保护时返回 passthrough，`EncryptedKVStore` 明确声明已启用。

## 与其他子目录的边界

**本模块管**：
- 可配置真源（KVStore）
- 多后端索引存储（Vector/Fulltext/Graph/Fusion）
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
   postgres/pgvector `sslrootcert`（配 `sslmode=verify-full`）、milvus `server_pem_path`（配
   `secure=True`）。不做跨后端的 TLS 参数抽象层——各客户端语义切分不同，详见
   [F04-storage-ssl.md](../../docs/features/storage/F04-storage-ssl.md)。
   `SslConfig`、归一（`build_ssl_config`）与 scheme 校验（`require_tls_scheme`）住在
   `common._support`，与出站客户端共用；storage 侧只保留缺证书即报错这条自有策略。
10. `CompositeStorage` 的默认首选检索路径是 `RECALL_GET_RANK`；首选路径是实例级稳定值，
    不随请求或健康状态切换，也不加入 Store capability 集合。
11. `StorageProducer.TOP_NAME` 固定为 `storage`；默认具名实例为 `storage.default`，
    `CompositeStorage` 只装配配置中声明的 Store 端口。兼容 Recaller 由 Retriever 在装配期绑定，
    storage 包不得导入 retrieval。
12. `Storage.scopes()` 枚举 MemoryUnit 真源已有 Scope；分层索引通过
    `has_*_port(name)` / `*_port(name)` 访问命名端口，Construction、Retrieval、Control 不得直接
    调用 Store Producer 解析具名后端。
13. 连接型后端须支持 ConfigSource 晚绑定（S08 / F01 §2.1.4）：在取客户端/连接路径
    `fetch` 对应 key，值变化则重建连接（同实现换 Redis URL / db_path 走此路径，不必多实例）。
    异质 Store 级 `*.active` 切换由 `config.routing.Routing*Store` 承担（F01）；
    多套完整 `Storage` 实例的动态选用由 `config.routing.RoutingStorage` + `storage.active`
    承担（F02）。二者均为产品手工注入（方案 A），不改默认拓扑、不注册内置 `target: routing`。

14. **上层握共享 `Storage` 入口，勿构造期握死某一实例的裸端口**
    Engine / Retriever / IndexBuilder / Recaller 共享的 `storage.default` 可以是
    `RoutingStorage`。`RoutingStorage` 的 `.kv`/`.vector`/… 与 `*_port` 返回惰性代理，
    使构造期 `self._vector = storage.vector` 仍随 `storage.active` 重解析。EncryptedKV
    为 F04 opt-in，只包在各预装实例内部 KV（若启用），不在 `RoutingStorage` 外包一层。
