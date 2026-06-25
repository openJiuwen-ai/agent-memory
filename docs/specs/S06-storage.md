# S06 — 存储层（Storage Layer）

## 元信息

| 项 | 值 |
|---|---|
| 关联模块 | src/storage/ |
| 最近一次修订日期 | 2026-06-23 |

| 关联特性文档 | docs/features/F01-system-spec-design.md |
## 范围 / 边界

**管什么**：
- 可配置真源（文档/结构化）的 KV 存储抽象
- 多后端索引存储抽象：向量（VectorStore）、全文（FulltextStore）、图（GraphStore）、融合（FusionStore）、文件系统（FSStore）
- 统一 CRUD 动词（insert / delete / update / get）
- 检索型存储的 search 查询
- scope 原生隔离（scope 为显式第一入参，物理约束在该 scope 内）

**不管什么**：
- 不做鉴权（由 `src/api` 层负责）
- 不做检索编排（由 `src/retrieval` 层负责）
- 不做索引构建逻辑（由 `src/construction` 层负责）
- 不实现具体后端（实现在 `*_impl/` 下，通过 Producer 注册）

## 不变量

1. **scope 原生隔离**：`scope: Scope` 为每个 Store 方法的显式第一入参，不放进记录/查询结构体、也不编进 `metadata` / `filters`。
2. **记录 id 是全局唯一主键**：`insert` 冲突 / `update` 缺失按 id 判定，scope 是其归属属性。
3. **统一 CRUD 动词**：insert（增）/ delete（删）/ update（改）/ get（查），各存储接口保持同一命名。
4. **检索型存储额外提供 search**：fulltext / vector / graph / fusion 在 CRUD 之上再提供 `search` 查询。
5. **kv 提供 exists 与 list**：exists（存在性查询）、list（枚举一个 scope 内的全部 key-value）、scopes（枚举有哪些 scope）。
6. **fs 提供 stat**：stat（文件元信息查询）。
7. **scope 对 key/路径做命名空间隔离**：kv / fs 是通用原语，`scope` 入参用于对 key / 路径做命名空间隔离（同一逻辑 key 在不同 scope 下是相互隔离的不同物理键）。
8. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
9. **所有 Store 必须实现 `store_type()` 和 `health()`**：继承自 `BaseStore`。

## 接口契约

### BaseStore（基类，`base.py`）

```python
class StoreType(str, Enum):
    KV / FULLTEXT / VECTOR / GRAPH / FUSION / FS

class BaseStore(ABC):
    def store_type(self) -> StoreType  # 自描述
    def health(self) -> None            # 存活探测：健康返回 None，否则抛 HealthCheckError
```

### KVStore（`kv.py`）

键值存储，统一 CRUD + 范围枚举。

| 方法 | 签名 | 语义 |
|------|------|------|
| `insert` | `(scope, key, value: bytes, ttl=0.0) -> None` | 在 scope 下新建 key；已存在时报冲突 |
| `update` | `(scope, key, value: bytes, ttl=0.0) -> None` | 覆写 scope 下已有 key；不存在时报缺失 |
| `delete` | `(scope, key) -> None` | 删除 scope 下的 key（幂等） |
| `get` | `(scope, key) -> bytes` | 读取 scope 下 key 的值；不存在时报缺失 |
| `exists` | `(scope, key) -> bool` | 返回 scope 下 key 是否存在 |
| `list` | `(scope, prefix="") -> list[tuple[str, bytes]]` | 枚举 scope 下的全部 (key, value)（可选只取 prefix 开头的 key） |
| `scopes` | `() -> list[Scope]` | 枚举本存储中已用过的全部 scope |

**ttl** 单位为秒（float），`0` 表示永不过期。

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

### 向量（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `VectorRecord` | id / vector: list[float] / metadata |
| `VectorQuery` | vector: list[float] / top_k / filters: list[FilterClause] |

### 全文（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `Document` | id / text / metadata |
| `TextQuery` | text / top_k / filters: list[FilterClause] |

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
| `FusionQuery` | vector / text / scalar_filters / top_k / vector_weight |

### 文件系统（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `FileStat` | ref / size / content_type / created_at / updated_at |

### 通用（`types.py`）

| 类型 | 关键字段 |
|------|----------|
| `ScoredID` | id / score |

**注**：所有 `metadata` / `filters` / `scalar_filters` 只承载 scope 之外的额外谓词，scope 作为显式第一入参，不混进这些结构体。

## 实现注册机制

```
src/storage/<store>_impl/
    __init__.py             # 重导出实现类
    <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

各 Producer：`KvProducer` / `FulltextProducer` / `VectorProducer` / `GraphProducer` / `FusionProducer` / `FsProducer`。
注册由 `storage.bootstrap.register_backends` 统一触发。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S03-memory_manage | Engine 通过 KVStore 读写真源；LifecycleManager/Governor 依赖 kv.scopes() + kv.list() 跨 scope 枚举 |
| S04-retrieval | 检索层各 Recaller 消费本层索引 Store |
| S05-construction | 构建层通过本层抽象做真源与索引持久化 |
| architecture.md §5 | 可配置真源形态（文档/结构化）与多后端 |
