# Storage 层 API

Storage 层为上层提供两级存储抽象：

- `Storage`：面向 `MemoryUnit` 的统一领域接口，同时负责能力发现、授权和检索适配。
- `BaseStore` 及其子接口：面向 KV、向量、全文、图、融合、文件和实体反向索引后端的七类标准端口；原文消息通过 Storage 所拥有的受权 `RawDataStore` 业务端口访问。

本文是当前抽象接口的 API 参考，不规定某个具体后端的内部实现。以下源码是最终依据：

- [`storage.py`](../../../jiuwen_memory/storage/storage.py)
- [`base.py`](../../../jiuwen_memory/storage/base.py)
- [`security.py`](../../../jiuwen_memory/storage/security.py)
- [`kv.py`](../../../jiuwen_memory/storage/kv.py)
- [`vector.py`](../../../jiuwen_memory/storage/vector.py)
- [`fulltext.py`](../../../jiuwen_memory/storage/fulltext.py)
- [`graph.py`](../../../jiuwen_memory/storage/graph.py)
- [`fusion.py`](../../../jiuwen_memory/storage/fusion.py)
- [`fs.py`](../../../jiuwen_memory/storage/fs.py)
- [`entity_store.py`](../../../jiuwen_memory/storage/entity_store.py)
- [`types.py`](../../../jiuwen_memory/storage/types.py)
- [`common/type_def/entity.py`](../../../jiuwen_memory/common/type_def/entity.py)
- [`common/type_def/retrieval.py`](../../../jiuwen_memory/common/type_def/retrieval.py)
- [`common/errors.py`](../../../jiuwen_memory/common/errors.py)

## 1. 通用约定

### 1.1 Scope 隔离

`Storage`、原文端口和七类标准 Store 的数据操作都显式接收 `scope: Scope`（`ensure_index()`、`health()` 等管理方法除外）。`Scope` 由 `org`、`space`、`user`、`agent`、`session` 五个维度组成，存储实现必须在该范围内完成写入、查询和删除。

`scope` 是独立隔离轴，不应塞入 `metadata`、`filters` 或各种 Record/Query 结构中。同一个 ID 可以在不同 Scope 内独立存在。

`EntityStore` 不再是例外：公开端口以完整 `Scope` 为方法首参，并通过 `Storage.entity_port()` 访问。仍接受 `space_id + filters` 的旧后端只能在 Storage 内部适配，Construction 和 Retrieval 不得看到这种兼容形状。

```python
from jiuwen_memory.common.type_def import Scope

scope = Scope(
    org="org-1",
    space="space-1",
    user="user-1",
    agent="agent-1",
    session="session-1",
)
```

### 1.2 CRUD 语义

| 方法 | 语义 | 记录已存在/不存在时 |
|---|---|---|
| `insert` | 新建记录 | 已存在时抛 `ConflictError` |
| `update` | 替换或更新已有记录 | 不存在时抛 `NotFoundError` |
| `delete` | 删除记录 | 幂等，不存在也不报错 |
| `get` | 按 ID/key 点查 | 单条缺失抛 `NotFoundError`；检索型 Store 的批量点查省略缺失项 |

批量方法的接口层不承诺事务原子性，需要原子语义时应查阅具体后端的能力说明。

### 1.3 通用异常

| 异常 | 含义 |
|---|---|
| `ConflictError` | 新建的 ID/key 在当前 Scope 内已存在 |
| `NotFoundError` | 更新或点查的 ID/key 不存在 |
| `ValidationError` | 参数、Scope 或数据结构不满足要求 |
| `PermissionDeniedError` | `StorageSecurity` 拒绝当前操作 |
| `UnsupportedStorageCapabilityError` | 访问了 Storage 未声明的端口能力 |
| `BackendError` | 存储连接、IO、超时或远端服务等非预期错误 |
| `HealthCheckError` | `health()` 检查失败 |
| `StorageRetrievalError` | 所有选中的检索入口均失败 |

## 2. Storage 统一接口

```python
from jiuwen_memory.storage.storage import Storage
```

`Storage` 是 Engine、Construction 和 Retrieval 共享的存储入口。它是面向接口的功能约定，不等同于“只写真源”：具体实现可以只保存 MemoryUnit 本体，也可以同时完成正排、向量、全文或图索引的写入。

### 2.1 能力发现与端口访问

`StorageCapability` 包含七种标准能力：`KV`、`VECTOR`、`FULLTEXT`、`GRAPH`、`FUSION`、`FS`、`ENTITY`。原文是 Storage 所有的受权业务端口，但不是检索型 Store，因此不加入 `StorageCapability` 枚举。

| API | 返回值 | 说明 |
|---|---|---|
| `capabilities()` | `frozenset[StorageCapability]` | 返回当前 Storage 实例声明的能力集 |
| `has_kv()` / `has_vector()` / `has_fulltext()` | `bool` | 判断默认 KV/向量/全文能力是否存在 |
| `has_graph()` / `has_fusion()` / `has_fs()` / `has_entity()` | `bool` | 判断默认图/融合/文件/实体能力是否存在 |
| `has_*_port(name="default")` | `bool` | 判断具名端口是否存在 |
| `kv` / `vector` / `fulltext` / `graph` / `fusion` / `fs` / `entity` | 对应 Store | 访问默认端口 |
| `*_port(name="default")` | 对应 Store | 访问指定名称的端口 |
| `raw` / `raw_port(name="default")` | `RawDataStore` | 访问经过 `StorageSecurity` 代理的原文业务端口 |

访问端口前应先用 `has_*()` 或 `has_*_port()` 判断能力。未声明的端口会抛出 `UnsupportedStorageCapabilityError`。

```python
if storage.has_vector_port("default"):
    vector_store = storage.vector_port("default")
```

### 2.2 MemoryUnit 写入与删除

#### `add`

```python
storage.add(
    scope: Scope,
    units: list[MemoryUnit],
    *,
    mode: IndexWriteMode = IndexWriteMode.ALL,
    access: StorageAccessContext | None = None,
) -> None
```

在 `scope` 内新建一批 MemoryUnit。`unit.scope` 应与显式传入的 `scope` 一致。重复 ID 的处理遵循具体 Storage 实现对新建语义的落地，标准存储端口使用 `ConflictError` 表示冲突。

#### `update`

```python
storage.update(
    scope: Scope,
    units: list[MemoryUnit],
    *,
    mode: IndexWriteMode = IndexWriteMode.ALL,
    access: StorageAccessContext | None = None,
) -> None
```

更新一批已存在的 MemoryUnit。无法将本体和检索索引拆分写入的实现，在 `FORWARD_ONLY` 模式下仍必须至少保证本体已更新。

#### `delete`

```python
storage.delete(
    scope: Scope,
    unit_ids: list[str],
    *,
    mode: IndexRemoveMode = IndexRemoveMode.HARD,
    access: StorageAccessContext | None = None,
) -> None
```

在 `scope` 内删除指定记忆，删除操作幂等。

#### 写入范围 `IndexWriteMode`

| 枚举值 | 语义 |
|---|---|
| `ALL` | 请求写入记忆本体和实现支持的全部检索索引 |
| `FORWARD_ONLY` | 只写记忆本体，不主动更新检索索引 |
| `RETRIEVAL_ONLY` | 本体已存在，只写检索索引；无检索能力的实现可为空操作 |

`mode` 表达调用方要求的逻辑写入范围，不代表底层一定有两个物理存储。例如，`CompositeStorage` 不负责索引投影，而一体化 Storage 可以在一次 `add` 中完成多种写入。

#### 删除范围 `IndexRemoveMode`

| 枚举值 | 语义 |
|---|---|
| `SOFT` | 只移出检索索引，`get`/`list` 仍可读取记忆本体；无检索能力的实现可为空操作 |
| `HARD` | 物理删除检索索引和记忆本体 |

### 2.3 MemoryUnit 读取与列表

#### `get`

```python
storage.get(
    scope: Scope,
    unit_ids: list[str],
    *,
    access: StorageAccessContext | None = None,
) -> list[MemoryUnit]
```

在 `scope` 内批量点读 MemoryUnit。返回值只包含实际读到的记忆；调用方如果需要与 `unit_ids` 一一对齐，应自行按 `unit.id` 建立映射。

#### `list`

```python
storage.list(
    scope: Scope,
    *,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    filters: FilterExpr | None = None,
    extensions: dict[str, str] | None = None,
    access: StorageAccessContext | None = None,
) -> MemoryListResult
```

| 参数 | 说明 |
|---|---|
| `offset` | 分页起始位置 |
| `limit` | 本页最大条数 |
| `memory_types` | 记忆类型白名单；`None` 表示不按该字段过滤 |
| `filters` | Scope 之外的 `FilterExpr` 元数据谓词 |
| `extensions` | 由具体实现或业务约定解释的透传参数 |
| `access` | 可选授权上下文 |

`MemoryListResult.items` 是当前页，`MemoryListResult.count` 是分页前的匹配总数。标准语义是先过滤、再计数和分页。

### 2.4 检索适配 API

`Storage` 以三种粒度向 Retrieval 层暴露检索能力：

| API | 返回值 | 责任边界 |
|---|---|---|
| `recall(scope, query, *, channels, recall_limit, access=None)` | `RecallResult[ScoredUnit]` | 只返回未物化的 `unit_id` 候选、分数和通道证据 |
| `recall_and_get(scope, query, *, channels, recall_limit, access=None)` | `RecallResult[ScoredMemoryUnit]` | 召回并读取完整 `MemoryUnit` |
| `retrieve(scope, query, fuser, *, channels, recall_limit, rank_limit, access=None)` | `RankedStorageResult` | 在 Storage 内完成召回、物化和 Fuser 排序 |

共享参数：

- `query: ParsedQuery`：由 Retrieval 层解析后的结构化查询。
- `channels`：需要使用的逻辑召回通道。直接调用 Storage 时，`None` 的展开方式由具体 Storage 和其装配的召回入口决定；业务调用优先使用 `Retriever.retrieve()`。
- `recall_limit`：每个物理召回入口的候选上限。
- `rank_limit`：Storage 内融合后的候选上限。
- `fuser: CandidateFuser`：只要实现 `fuse(query, candidates)` 即可。

`preferred_retrieval_pipeline() -> RetrievalPipeline` 返回 Storage 实例的稳定首选路径：

| 枚举值 | Retriever 的调用方式 |
|---|---|
| `RECALL_GET_RANK` | `recall` → 点读 → `Fuser` |
| `RECALL_AND_GET_RANK` | `recall_and_get` → `Fuser` |
| `RETRIEVE` | 直接调用 `Storage.retrieve` |

`RecallResult` 支持部分成功：`batches` 保留每个物理召回入口的候选，`errors` 记录失败入口的 `ChannelError`。

### 2.5 其他统一 API

| API | 说明 |
|---|---|
| `security` | 返回当前 Storage 的 `StorageSecurity` |
| `scopes() -> list[Scope]` | 枚举统一 Storage 已知的 Scope；实现应合并正排 KV 与 Raw 端口的 Scope，顺序由实现定义 |
| `health() -> None` | 检查 Storage、安全组件及所属后端；健康时返回 `None` |

## 3. BaseStore 基类

```python
from jiuwen_memory.storage.base import BaseStore, StoreType
```

所有底层 Store 都继承 `BaseStore`，并提供下列 API：

| API | 类型 | 说明 |
|---|---|---|
| `security` | 属性 | 返回 `StoreSecurity`；默认为未启用保护的 passthrough 实现 |
| `store_type()` | 抽象方法 | 返回 `StoreType` |
| `health()` | 抽象方法 | 健康时返回 `None`，否则抛 `HealthCheckError` |

`StoreType` 包含 `KV`、`FULLTEXT`、`VECTOR`、`GRAPH`、`FUSION`、`FS`、`ENTITY`。

## 3.1 RawDataStore API

```python
from jiuwen_memory.storage.raw import RawDataStore
```

`RawDataStore` 是 Storage 所有的原文业务端口，用于保存抽取上下文所需的原文消息。它不是
MemoryUnit 真源，也不是检索索引，因此不作为 `StorageCapability` 枚举值；但它和其他 Storage
端口一样受 `StorageSecurity` 授权，并且每次数据操作都必须显式接收完整 `Scope`。

端口把 `/messages/` key 前缀、`MemoryUnit` 编解码、按 `t_ingest` 排序、保留淘汰和后端选择都
封装起来。Evolver 只能调用这些业务方法，不能导入 `KVStore`/`KvProducer`，也不能拼接
`/messages/` 或自行调用 codec。

| API | 返回值 | 说明 |
|---|---|---|
| `append_raw(scope, units, *, retain_limit=0, access=None)` | `None` | 追加当前 Scope 的原文；`retain_limit=0` 表示不按数量淘汰 |
| `list_raw(scope, *, limit=100, access=None)` | `list[MemoryUnit]` | 按 `t_ingest` 倒序返回最近原文；`limit=None` 返回全部 |
| `delete_raw(scope, record_ids, *, access=None)` | `None` | 在当前 Scope 内按 ID 幂等删除 |
| `scopes(*, access=None)` | `list[Scope]` | 枚举含原文的 Scope，供空间级治理使用 |
| `usage(scope, *, access=None)` | `RawDataUsage` | 返回原文条数和可获得的物理字节数 |
| `purge(scope, *, access=None)` | `RawDataUsage` | 清空当前 Scope 的原文并返回清理前用量 |

默认 `CompositeStorage` 在只配置 KV 时使用 `KVRawDataStore` 适配器：适配器在 Storage 内部
处理 `/messages/`、`dumps/loads`、排序和 retention；如果未来 X-01 选择独立 Raw 后端，只需
替换这个端口实现，上层业务契约保持不变。

授权资源名是 `raw`。如果底层 KV 使用 `EncryptedKVStore`，它根据 `/messages/` 前缀选择加密
purpose `raw_message`。`raw` 是访问授权资源，`raw_message` 是加密上下文，两者不是同一个概念。

## 4. KVStore API

```python
from jiuwen_memory.storage.kv import KVStore
```

| API | 返回值 | 说明 |
|---|---|---|
| `insert(scope, key, value, ttl=0.0)` | `None` | 新建二进制值；`ttl` 单位为秒，`0` 表示永不过期 |
| `update(scope, key, value, ttl=0.0)` | `None` | 覆写已有 key |
| `delete(scope, key)` | `None` | 幂等删除 key |
| `get(scope, key)` | `bytes` | 读取单个 key；缺失抛 `NotFoundError` |
| `mget(scope, keys)` | `list[bytes]` | 返回顺序与 `keys` 一一对应，不去重；任一 key 缺失即抛 `NotFoundError` |
| `exists(scope, key)` | `bool` | 判断 key 是否存在 |
| `scan(scope, prefix="")` | `list[tuple[str, bytes]]` | 扫描 Scope 内未过期的原始键值；可按前缀过滤，顺序未定义 |
| `list(scope, *, offset=0, limit=100, memory_types=None, filters=None, extensions=None)` | `KVMemoryListResult` | 查询 `/memory/` 命名空间中的 MemoryUnit 原始条目 |
| `scopes()` | `list[Scope]` | 枚举已使用的 Scope，顺序未定义 |

`KVMemoryListResult.entries` 为当前页 `(key, value)`，`count` 为分页前总数。

## 5. VectorStore API

```python
from jiuwen_memory.storage.vector import VectorStore
from jiuwen_memory.storage.types import VectorQuery, VectorRecord
```

| API | 返回值 | 说明 |
|---|---|---|
| `insert(scope, records)` | `None` | 新建 `VectorRecord` 列表 |
| `update(scope, records)` | `None` | 替换已有向量行 |
| `delete(scope, ids)` | `None` | 幂等删除向量行 |
| `get(scope, ids)` | `list[VectorRecord]` | 批量点查，缺失 ID 省略 |
| `search(scope, query)` | `list[ScoredID]` | 在 Scope 内做 ANN 检索 |
| `recall(scope, query, output_fields=None)` | `list[ScoredHit]` | 可选单请求回带 payload 的 ANN 能力；默认抛 `NotImplementedError` |
| `score_higher_is_better()` | `bool` | 返回分数方向；默认 `True` |

`VectorRecord` 由 `id`、`vector`、`metadata` 组成。`VectorQuery` 由 `vector`、`top_k`、`filters`、`return_metadata` 组成。

`recall()` 是可选优化 API，目前 `output_fields` 只识别 `"metadata"`。后端未覆盖该方法时，调用方应回退到 `search()` + `get()`。距离型后端如果“分越小越相关”，必须覆盖 `score_higher_is_better()` 返回 `False`。

## 6. FulltextStore API

```python
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.types import Document, TextQuery
```

| API | 返回值 | 说明 |
|---|---|---|
| `insert(scope, docs)` | `None` | 新建 `Document` 列表 |
| `update(scope, docs)` | `None` | 重建已有文档索引 |
| `delete(scope, ids)` | `None` | 幂等删除文档 |
| `get(scope, ids)` | `list[Document]` | 批量点查，缺失 ID 省略 |
| `search(scope, query)` | `list[ScoredID]` | 执行 BM25 等关键词检索，返回 top-k |

`Document` 由 `id`、`text`、`metadata` 组成。`TextQuery` 由 `text`、`top_k`、`filters` 组成。

## 7. GraphStore API

```python
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node
```

| API | 返回值 | 说明 |
|---|---|---|
| `seed_ids(scope, tokens)` | `list[str]` | 根据词项定位图遍历的种子节点；匹配策略由后端定义 |
| `insert(scope, nodes=None, edges=None)` | `None` | 新建节点和/或边 |
| `update(scope, nodes=None, edges=None)` | `None` | 更新已有节点和/或边 |
| `delete(scope, node_ids=None, edge_ids=None)` | `None` | 幂等删除节点和/或边；删节点时连带关联边 |
| `get(scope, node_ids)` | `list[Node]` | 批量点查节点，缺失 ID 省略 |
| `search(scope, query)` | `list[Node]` | 从 `start_id` 开始执行多跳遍历 |

`Node` 由 `id`、`label`、`properties` 组成；`Edge` 由 `id`、`source`、`target`、`relation`、`properties` 组成。`GraphQuery` 提供 `start_id`、`relation`、`depth`、`limit`。

## 8. FusionStore API

```python
from jiuwen_memory.storage.fusion import FusionStore
from jiuwen_memory.storage.types import FusionQuery, FusionRecord
```

| API | 返回值 | 说明 |
|---|---|---|
| `insert(scope, records)` | `None` | 新建融合行 |
| `update(scope, records)` | `None` | 替换已有融合行 |
| `delete(scope, ids)` | `None` | 幂等删除融合行 |
| `get(scope, ids)` | `list[FusionRecord]` | 正排点查完整融合行，缺失 ID 省略 |
| `search(scope, query)` | `list[ScoredID]` | 在一次调用内完成向量、文本和标量谓词的融合检索 |

`FusionRecord` 可同时承载 `vector`、`text`、`scalars`、`value`，也允许部分字段为 `None`。`FusionQuery.vector_weight` 控制向量和文本得分的混合比例，值域语义为 `1.0` 纯向量、`0.0` 纯文本。

## 9. FSStore API

```python
from jiuwen_memory.storage.fs import FSStore
```

| API | 返回值 | 说明 |
|---|---|---|
| `insert(scope, key, data)` | `str` | 写入新文件并返回规范化 `ref` |
| `update(scope, ref, data)` | `str` | 覆写已有文件并返回可能更新的 `ref` |
| `delete(scope, ref)` | `None` | 幂等删除文件 |
| `get(scope, ref)` | `BinaryIO` | 打开文件；返回的流由调用方关闭 |
| `stat(scope, ref)` | `FileStat` | 返回文件引用、大小、MIME 类型和创建/更新时间 |

```python
with storage.fs.get(scope, ref) as stream:
    payload = stream.read()
```

## 10. EntityStore API

```python
from jiuwen_memory.storage.entity_store import EntityStore
```

`EntityStore` 是实体到 MemoryUnit ID 的反向索引端口，属于 `StorageCapability.ENTITY`，通过 `storage.entity` 或 `storage.entity_port(name)` 访问。`CompositeStorage` 会为该端口加上 `StorageSecurity` 授权代理；`EntityStoreProducer` 只保留为 Storage 装配时的底层实现工厂，业务组件不得直接解析它。

| API | 返回值 | 说明 |
|---|---|---|
| `ensure_index()` | `None` | 确保实体索引已创建并就绪；使用其他 API 前必须调用 |
| `find_by_entity_text_hash(scope, entity_text_hashes, *, limit=500)` | `list[EntityRecord]` | 在完整 Scope 内按实体文本 SHA-256 hash 精确查询，不提供向量近邻检索 |
| `find_by_linked_memory_id(scope, memory_id)` | `list[EntityRecord]` | 在完整 Scope 内反查关联了指定 MemoryUnit ID 的实体 |
| `execute_operations(scope, operations)` | `EntityBatchResult` | 在完整 Scope 内批量执行 `INSERT` / `LINK` / `UNLINK_UPDATE` / `DELETE` 混合操作 |

公开契约保留 `org`、`space`、`user`、`agent`、`session` 五个 Scope 维度。后端可以从 Scope 派生物理 `space_id` routing 和兼容用 `EntityStoreFilters`，但不得因此放宽查询：不同 agent 或 session 的实体不会互相可见，除非调用方明确使用相同 Scope。

`EntityBatchResult.successful_ids` 和 `failed_ids` 以单项粒度报告批处理结果，允许部分失败。`EntityStore` 继承 `BaseStore`，其 `store_type()` 返回 `StoreType.ENTITY`。

## 11. 安全 API

### 11.1 StorageSecurity

```python
security.authorize(
    access: StorageAccessContext | None,
    scope: Scope,
    action: StorageAction,
    resource: str,
) -> None
```

允许操作时返回 `None`，拒绝时抛 `PermissionDeniedError`。`StorageAccessContext.actor` 表示访问主体，`attributes` 承载授权实现需要的扩展上下文。`StorageAction` 包含 `ADD`、`UPDATE`、`DELETE`、`GET`、`LIST`、`SEARCH`、`ADMIN`。

`health() -> None` 用于检查授权组件，基类默认直接返回 `None`，依赖外部策略服务的实现可覆盖它。

`AllowAllStorageSecurity` 是默认放行实现，因此未启用自定义授权时可以不传 `access`。

### 11.2 StoreSecurity

`StoreSecurity` 表示底层数据保护能力，与 `StorageSecurity` 的访问授权职责不同。

| API | 说明 |
|---|---|
| `enabled() -> bool` | 返回后端是否已启用实际数据保护 |
| `health() -> None` | 检查保护组件健康状态 |

`BaseStore.security` 默认返回 `PassthroughStoreSecurity`，其 `enabled()` 为 `False`。

## 12. Producer 与实现注册

| Producer | `TOP_NAME` | 产物 |
|---|---|---|
| `StorageProducer` | `storage` | `Storage` |
| `KvProducer` | `kv_store` | `KVStore` |
| `VectorProducer` | `vector_store` | `VectorStore` |
| `FulltextProducer` | `fulltext_store` | `FulltextStore` |
| `GraphProducer` | `graph_store` | `GraphStore` |
| `FusionProducer` | `fusion_store` | `FusionStore` |
| `FsProducer` | `fs_store` | `FSStore` |
| `EntityStoreProducer` | `entity_store` | `EntityStore` |

具体实现使用 `@XxxProducer.register("name")` 注册，并由 `storage.bootstrap.register_backends()` 触发实现模块导入。`StorageProducer.resolve(config)` 用于从组件配置解析共享 Storage，默认 target 为 `composite`。业务代码通常使用装配层已注入的 `Storage`，不应自行解析和固化底层 Store。

## 13. 可配置实现

### 13.1 配置结构

配置使用“Producer 命名空间 → 具名实例 → `target/params`”两级结构：

```yaml
kv_store:                 # KvProducer.TOP_NAME
  default:                # 具名实例，可被其他组件引用
    target: sqlite        # 已注册的实现名
    params:               # 实现参数和依赖引用
      db_path: ./data/memory.db
    new_instance: false   # 可选；false 表示共享具名实例
```

无参数实现可使用字符串简写：

```yaml
kv_store:
  default: memory
```

依赖参数有两种写法：

- 字符串，如 `storage: default`：引用相应 Producer 命名空间下的具名共享实例。
- 映射，如 `raw_kv_store: {target: sqlite, params: {...}}`：就地创建不共享的匿名实例。

`params` 中未找到的普通参数会回退读取 `globals`。用户配置会按“命名空间 + 实例名”覆盖内置默认；覆盖某个已有实例时，该实例的 `params` 整体替换，因此不能丢失必要的依赖引用。

直接传给 `Config.from_dict()` / `Config.from_yaml()` 时，以上命名空间就是顶层段；部署配置中则放在 `memory_api:` 下。

不传用户配置时，Storage 默认组合是 `composite + memory KV/vector/fulltext/graph`，L0/L1 向量和全文端口也使用 `memory`；FusionStore、FSStore 和 EntityStore 不会默认接入 `CompositeStorage`。外部后端 target 采用惰性导入/连接，配置成功不代表对应三方客户端和服务已就绪，上线前还应调用 `health()`。

### 13.2 Storage 实现

| `target` | 实现类 | 功能 | 主要 `params` |
|---|---|---|---|
| `composite` | `CompositeStorage` | 默认统一 Storage；组合各类 Store 端口，MemoryUnit 本体由 KV 端口持久化，原文由受权 Raw 端口提供，检索适配由装配的 Recaller 提供；本实现不负责向量/全文/图投影构建 | `kv_store`（默认 `memory`），可选 `vector_store` / `fulltext_store` / `graph_store` / `fusion_store` / `fs_store` / `raw_store` / `entity_store`，`preferred_retrieval_pipeline`（默认 `recall_get_rank`） |

`preferred_retrieval_pipeline` 可选 `recall_get_rank`、`recall_and_get_rank`、`retrieve`。`vector_store.layers_l0/layers_l1` 和 `fulltext_store.layers_l0/layers_l1` 会被 `CompositeStorage` 自动暴露为同名端口。

```yaml
storage:
  default:
    target: composite
    params:
      kv_store: default
      vector_store: default
      fulltext_store: default
      graph_store: default
      preferred_retrieval_pipeline: recall_get_rank
```

目前内置 Storage target 只有 `composite`。`RoutingStorage` 和 Routing Store 是产品可手工注入的路由组件，当前没有注册成可在 YAML 中直接使用的 `target: routing`。

### 13.3 KVStore 实现

| `target` | 实现类 | 功能 | 必填参数 | 主要可选参数 |
|---|---|---|---|---|
| `memory` | `InMemoryKVStore` | 进程内 KV，支持 TTL；适合默认开发、测试，进程结束后数据丢失 | 无 | 无 |
| `sqlite` | `SQLiteKVStore` | SQLite 单文件持久化，Scope 五维落列隔离 | 无 | `db_path`（默认 `agent_memory.db`；`":memory:"` 表示进程内 SQLite） |
| `redis` | `RedisKVStore` | Redis 远程 KV，将 Scope 编入键命名空间，支持连接参数晚绑定 | `url` | `ssl_verify`、`ssl_ca_cert`；代码仍接受 `host` / `port` / `db` / `password` 回退字段，但当前 builder 强制要求 `url`，URL 分支优先 |
| `postgres` | `PostgresKVStore` | PostgreSQL 持久化 KV，Scope 五维落列，支持 TTL 与连接池 | `dsn` | `schema`、`table`、`pool_min_size`、`pool_max_size`、`connect_timeout`、`application_name`、`auto_create_schema`、`ssl_verify`、`ssl_ca_cert` |
| `encrypted` | `EncryptedKVStore` | 加密装饰器；在任意 raw KV 外透明加解密，不自己实现密码算法 | `raw_kv_store`、`security` | 无；`raw_kv_store` 不能指向当前实例自身 |

```yaml
kv_store:
  raw:
    target: sqlite
    params:
      db_path: ./data/memory.db
  default:
    target: encrypted
    params:
      raw_kv_store: raw
      security: default

security:
  default:
    target: local
    params:
      key_env: AGENT_MEMORY_ENCRYPTION_ROOT_KEY
      allow_plaintext: false
```

`redis` 开启 `ssl_verify=true` 时，`url` 必须使用 `rediss://`，且必须同时配置 `ssl_ca_cert`。`postgres` 开启后内部使用 `sslmode=verify-full`。

### 13.4 VectorStore 实现

| `target` | 实现类 | 功能 | 必填参数 | 主要可选参数 |
|---|---|---|---|---|
| `memory` | `InMemoryVectorStore` | 进程内穷举向量检索，适合测试和小数据集 | 无 | 无 |
| `milvus` | `MilvusVectorStore` | Milvus ANN + 元数据过滤，支持召回时回带 metadata | `uri`，以及正数 `dim`（可回退 `globals.embedder_dim`） | `token`、`collection`、`metric_type`、`consistency_level`、`scope_field_max_length`、`id_max_length`、`ssl_verify`、`ssl_ca_cert` |
| `pgvector` | `PgVectorStore` | PostgreSQL + pgvector，支持 HNSW、Scope/元数据过滤下推和连接池 | `dsn`，以及正数 `dim`（可回退 `globals.embedder_dim`） | `schema`、`table`、`metric_type`、`index_type`、`hnsw_m`、`hnsw_ef_construction`、`ef_search`、`max_scan_tuples`、`create_metadata_index`、`pool_min_size`、`pool_max_size`、`connect_timeout`、`application_name`、`auto_create_schema`、`create_extension`、`ssl_verify`、`ssl_ca_cert` |

```yaml
globals:
  embedder_dim: 1024

vector_store:
  default:
    target: milvus
    params:
      uri: http://localhost:19530
      collection: agent_memory_vectors
      dim: 1024
      metric_type: COSINE
  layers_l0:
    target: milvus
    params:
      uri: http://localhost:19530
      collection: agent_memory_vectors_l0
      dim: 1024
      metric_type: COSINE
  layers_l1:
    target: milvus
    params:
      uri: http://localhost:19530
      collection: agent_memory_vectors_l1
      dim: 1024
      metric_type: COSINE
```

向量维度必须与 Embedder 输出一致。生产 Retriever 的 MaxP 和降序融合要求“分越大越相关”；Milvus 使用 `L2` 时会声明为距离语义并在 `VectorRecaller` 装配阶段被拒绝，检索链路应使用 `COSINE` 或 `IP`。`pgvector` 会把 `L2` 距离转换为高分优先，因此支持 `COSINE`、`IP`、`L2`。

`milvus` 开启 `ssl_verify=true` 时会设置 `secure=True` 和 `server_pem_path`；`pgvector` 的 SSL 行为与 PostgreSQL KV 一致。

### 13.5 FulltextStore 实现

| `target` | 实现类 | 功能 | 必填参数 | 主要可选参数 |
|---|---|---|---|---|
| `memory` | `InMemoryFulltextStore` | 进程内基于 Tokenizer 的词项命中计分 | 无 | `tokenizer`（具名引用，默认匿名 `whitespace`） |
| `elasticsearch` | `ElasticsearchFulltextStore` | Elasticsearch 文档 CRUD + `match`/BM25 检索，Scope 和 FilterExpr 下推 | `hosts` | `index`、`username`、`password`、`api_key`、`text_field`、`text_analyzer`、`refresh`、`ssl_verify`、`ssl_ca_cert` |

```yaml
fulltext_store:
  default:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: agent_memory_fulltext
      text_analyzer: english
  layers_l0:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: agent_memory_fulltext_l0
      text_analyzer: english
  layers_l1:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: agent_memory_fulltext_l1
      text_analyzer: english
```

`text_analyzer` 只在创建索引时生效，修改后需要重建索引。`ssl_verify=true` 时 `hosts` 必须使用 `https://`，并配置 `ssl_ca_cert`。

### 13.6 GraphStore 实现

| `target` | 实现类 | 功能 | 必填参数 | 主要可选参数 |
|---|---|---|---|---|
| `memory` | `InMemoryGraphStore` | 进程内属性图，支持词项定位种子和多跳遍历 | 无 | 无 |
| `nano_graphrag` | `NanoGraphRAGGraphStore` | 基于 nano-graphrag `NetworkXStorage`，每个 Scope 一个 GraphML 命名空间，可落盘持久化 | `working_dir` | `namespace_prefix`（默认 `agent_memory_graph`）、`create_root`（默认 `true`） |

```yaml
graph_store:
  default:
    target: nano_graphrag
    params:
      working_dir: ./data/graph
      namespace_prefix: agent_memory_graph
```

### 13.7 FusionStore 实现

| `target` | 实现类 | 功能 | 必填参数 | 主要可选参数 |
|---|---|---|---|---|
| `memory` | `InMemoryFusionStore` | 进程内向量 + 词项 + 标量过滤 + 正排值融合存储 | 无 | `tokenizer`（默认匿名 `whitespace`） |
| `milvus_graph` | `MilvusGraphFusionStore` | Milvus 保存向量/标量/正排值，nano-graphrag 保存邻接图；检索先 ANN，再按图关系扩展 | `uri`、`working_dir`、正数 `dim`（可回退 `globals.embedder_dim`） | `collection`、`metric_type`、`namespace_prefix`、`link_field`、`neighbor_depth`、`neighbor_decay`、`neighbor_relation` |

```yaml
fusion_store:
  default:
    target: milvus_graph
    params:
      uri: http://localhost:19530
      working_dir: ./data/fusion-graph
      dim: 1024
      collection: agent_memory_fusion
      link_field: links
      neighbor_depth: 1
      neighbor_decay: 0.5
```

`milvus_graph` 当前聚焦“向量种子 → 图邻居扩展”，不使用 `FusionQuery.text` 和 `vector_weight`，因此不提供 BM25 融合。

### 13.8 FSStore 实现

| `target` | 实现类 | 功能 | 必填参数 | 主要可选参数 |
|---|---|---|---|---|
| `memory` | `InMemoryFSStore` | 进程内二进制文件存储 | 无 | 无 |
| `local` | `LocalFSStore` | 本地文件系统，将文件保存到 `root/<scope 五段>/` 下并阻止目录穿越 | `root` | `create_root`（默认 `true`） |

```yaml
fs_store:
  default:
    target: local
    params:
      root: ./data/assets
      create_root: true
```

### 13.9 EntityStore 实现

| `target` | 实现类 | 功能 | 启用参数 | 主要可选参数 |
|---|---|---|---|---|
| `elasticsearch` | `ElasticsearchEntityStore` | 实体 hash 精确反查、MemoryUnit 关联反查和批量变更 | `hosts` 或 `endpoint`；未配时 builder 返回 `None`，实体链路静默关闭 | `index`、`username`、`password`、`timeout`、`list_limit`、`number_of_shards`、`number_of_replicas`、`ssl_verify`、`ssl_ca_cert` |

```yaml
globals:
  entity_enabled: true

entity_store:
  default:
    target: elasticsearch
    params:
      hosts: http://localhost:9200
      index: memory_entities

# Entity 后端由 Storage 统一持有；写入侧和召回侧只引用同一具名 Storage
storage:
  default:
    target: composite
    params:
      kv_store: default
      entity_store: default

constructor:
  default:
    target: hybrid
    params:
      storage: default
      chunker: default
      embedder: default
recaller:
  keyword:
    target: keyword
    params:
      storage: default
```

`entity_enabled=true` 只是写入/召回链路的总开关；Entity 后端本身由 `storage.default.params.entity_store` 接入，Construction 和 Retrieval 都从同一 `Storage` 取得 `entity_port()`，不再分别注入或解析 `entity_store`。

### 13.10 安全接口的实现方式

`StorageSecurity` 目前没有独立 Producer 命名空间。`CompositeStorage` 配置构建默认使用 `AllowAllStorageSecurity`；自定义授权实现需要代码构造 `CompositeStorage(..., security=...)` 或由产品装配层注入，不能直接写成 YAML target。

`StoreSecurity` 也不单独选 target：普通 Store 默认为 `PassthroughStoreSecurity`；选择 `kv_store.target=encrypted` 后，`EncryptedKVStore.security` 自动变为已启用状态，真正的加密实现由 `params.security` 引用的 `SecurityProvider` 提供。

## 14. 最小调用示例

```python
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.storage.storage import Storage
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode


def save_and_load(storage: Storage, scope: Scope, unit: MemoryUnit) -> MemoryUnit | None:
    storage.add(scope, [unit], mode=IndexWriteMode.ALL)
    loaded = storage.get(scope, [unit.id])
    return loaded[0] if loaded else None


def remove_from_retrieval(storage: Storage, scope: Scope, unit_id: str) -> None:
    storage.delete(scope, [unit_id], mode=IndexRemoveMode.SOFT)
```

这两段代码只表达接口语义。`ALL` 或 `SOFT` 最终会操作哪些物理数据，取决于注入的 `Storage` 实现及其能力。

## 15. 存储数据类型

本节的数据类型都不携带 Scope。Scope 是 Store 方法的显式第一参数，不应重复写入
`metadata`、`filters` 或记录主体。

### 15.1 通用结果类型

| 类型.字段 | 类型 | 默认值/必填 | 语义 |
|---|---|---|---|
| `ScoredID.id` | `str` | 必填 | Scope 内逻辑 ID |
| `ScoredID.score` | `float` | 必填 | 相关性分数，当前检索链要求高分优先 |
| `ScoredID.metadata` | `dict[str, Any] \| None` | `None` | 可选的命中元数据 |
| `ScoredHit.id` | `str` | 必填 | 命中 ID |
| `ScoredHit.score` | `float` | 必填 | 相关性分数 |
| `ScoredHit.metadata` | `dict[str, Any]` | `{}` | `VectorStore.recall` 按需回带的 payload |
| `KVMemoryListResult.entries` | `list[tuple[str, bytes]]` | `[]` | 当前页原始 KV 条目 |
| `KVMemoryListResult.count` | `int` | `0` | 分页前匹配总数 |
| `MemoryListResult.items` | `list[MemoryUnit]` | `[]` | 当前页领域对象 |
| `MemoryListResult.count` | `int` | `0` | 分页前匹配总数 |

### 15.2 Vector 与 Fulltext

| 类型 | 字段（类型；默认值） | 约束 |
|---|---|---|
| `VectorRecord` | `id: str`；`vector: list[float]`；`metadata: dict[str, Any]={}` | vector 维度必须与后端索引一致；id 在 Scope 内唯一 |
| `VectorQuery` | `vector: list[float]`；`top_k: int=10`；`filters: FilterExpr \| None=None`；`return_metadata: bool=false` | `filters` 在构造边界规范化；`top_k` 应为正数 |
| `Document` | `id: str`；`text: str`；`metadata: dict[str, Any]={}` | id 在 Scope 内唯一 |
| `TextQuery` | `text: str`；`top_k: int=10`；`filters: FilterExpr \| None=None` | `filters` 在构造边界规范化；`top_k` 应为正数 |

`VectorStore.search()` 返回 `ScoredID`。可选的 `VectorStore.recall()` 可在同一次 ANN 请求中回带
`ScoredHit.metadata`；后端未实现时基类抛 `NotImplementedError`，`VectorRecaller` 回退到
`search + get`。

### 15.3 Graph、Fusion 与 FS

| 类型 | 字段（类型；默认值） | 约束 |
|---|---|---|
| `Node` | `id: str`；`label: str=""`；`properties: dict[str, Any]={}` | id 在 Scope 内唯一 |
| `Edge` | `id: str`；`source: str`；`target: str`；`relation: str=""`；`properties: dict[str, Any]={}` | source/target 是节点 ID |
| `GraphQuery` | `start_id: str`；`relation: str \| None=None`；`depth: int=1`；`limit: int=100` | relation 为 `None` 时不限关系类型 |
| `FusionRecord` | `id: str`；`vector: list[float] \| None=None`；`text: str \| None=None`；`scalars: dict[str, Any]={}`；`value: bytes \| None=None` | 允许只提供部分模态字段 |
| `FusionQuery` | `vector: list[float] \| None=None`；`text: str \| None=None`；`scalar_filters: FilterExpr \| None=None`；`top_k: int=10`；`vector_weight: float=0.5` | `vector_weight` 表示向量得分权重；具体后端可只支持子集 |
| `FileStat` | `ref: str`；`size: int`；`content_type: str=""`；`created_at/updated_at: float=0.0` | 时间为 Unix 秒 |

### 15.4 EntityStore 类型

| 类型 | 字段 | 语义 |
|---|---|---|
| `EntityStoreFilters` | `actor_id: str \| None=None` | 旧后端兼容投影字段；上层不传入，Storage 从完整 Scope 派生 |
| `EntityMention` | `entity_type: str`、`display_name: str`、`normalized_name: str` | 从记忆或 query 抽取并已归一化的实体提及 |
| `EntityRecord` | `id`、`space_id`、`entity_text`、`entity_type`、`linked_memory_ids: tuple[str, ...]`、`filters`、`entity_text_hash=""` | 实体到 MemoryUnit ID 的反向索引记录；`space_id/filters` 是兼容后端投影，不是上层隔离输入 |
| `EntityLinkResult` | `extracted_count/inserted_count/updated_count/deleted_count/failed_count/skipped_count: int=0` | 实体链接器的写入统计，不是 EntityStore 批量 API 的逐项结果 |
| `EntityOperation` | `type: EntityOpType`、`record: EntityRecord \| None=None`、`record_id: str \| None=None`、`link_memory_ids: tuple[str, ...]=()` | `INSERT/LINK/UNLINK_UPDATE/DELETE` 批量命令 |
| `EntityBatchResult` | `successful_ids: list[str]`、`failed_ids: list[str]` | 逐项部分成功结果，单项失败不要求整批抛异常 |

## 16. Store 完整方法签名

### 16.1 KVStore

```python
insert(scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None
update(scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None
delete(scope: Scope, key: str) -> None
get(scope: Scope, key: str) -> bytes
mget(scope: Scope, keys: list[str]) -> list[bytes]
exists(scope: Scope, key: str) -> bool
scan(scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]
list(
    scope: Scope,
    *,
    offset: int = 0,
    limit: int = 100,
    memory_types: list[str] | None = None,
    filters: FilterExpr | None = None,
    extensions: dict[str, str] | None = None,
) -> KVMemoryListResult
scopes() -> list[Scope]
```

`ttl` 单位为秒，`0` 表示永不过期。`mget` 保持输入顺序和重复 key，与 `keys` 一一对应；
任意 key 缺失都抛 `NotFoundError`。`scan` 和 `scopes` 的顺序由实现定义，调用方不应依赖。

### 16.2 VectorStore、FulltextStore 与 FusionStore

```python
# VectorStore
insert(scope: Scope, records: list[VectorRecord]) -> None
update(scope: Scope, records: list[VectorRecord]) -> None
delete(scope: Scope, ids: list[str]) -> None
get(scope: Scope, ids: list[str]) -> list[VectorRecord]
search(scope: Scope, query: VectorQuery) -> list[ScoredID]
recall(
    scope: Scope,
    query: VectorQuery,
    output_fields: list[str] | None = None,
) -> list[ScoredHit]
score_higher_is_better() -> bool

# FulltextStore
insert(scope: Scope, docs: list[Document]) -> None
update(scope: Scope, docs: list[Document]) -> None
delete(scope: Scope, ids: list[str]) -> None
get(scope: Scope, ids: list[str]) -> list[Document]
search(scope: Scope, query: TextQuery) -> list[ScoredID]

# FusionStore
insert(scope: Scope, records: list[FusionRecord]) -> None
update(scope: Scope, records: list[FusionRecord]) -> None
delete(scope: Scope, ids: list[str]) -> None
get(scope: Scope, ids: list[str]) -> list[FusionRecord]
search(scope: Scope, query: FusionQuery) -> list[ScoredID]
```

三种 Store 的批量 `get` 只返回实际存在的记录，不与输入 ID 一一对齐。内置 `search`
按高分优先返回；距离型 VectorStore 必须用 `score_higher_is_better()` 明确分数方向，
当前 `VectorRecaller` 会拒绝低分优先的直接装配。

### 16.3 GraphStore、FSStore 与 EntityStore

```python
# GraphStore
seed_ids(scope: Scope, tokens: set[str]) -> list[str]
insert(
    scope: Scope,
    nodes: list[Node] | None = None,
    edges: list[Edge] | None = None,
) -> None
update(
    scope: Scope,
    nodes: list[Node] | None = None,
    edges: list[Edge] | None = None,
) -> None
delete(
    scope: Scope,
    node_ids: list[str] | None = None,
    edge_ids: list[str] | None = None,
) -> None
get(scope: Scope, node_ids: list[str]) -> list[Node]
search(scope: Scope, query: GraphQuery) -> list[Node]

# FSStore
insert(scope: Scope, key: str, data: BinaryIO) -> str
update(scope: Scope, ref: str, data: BinaryIO) -> str
delete(scope: Scope, ref: str) -> None
get(scope: Scope, ref: str) -> BinaryIO
stat(scope: Scope, ref: str) -> FileStat

# EntityStore（StorageCapability.ENTITY；公开端口以完整 Scope 为首参）
ensure_index() -> None
find_by_entity_text_hash(
    scope: Scope,
    entity_text_hashes: tuple[str, ...],
    *,
    limit: int = 500,
) -> list[EntityRecord]
find_by_linked_memory_id(
    scope: Scope,
    memory_id: str,
) -> list[EntityRecord]
execute_operations(
    scope: Scope,
    operations: list[EntityOperation],
) -> EntityBatchResult
```

FS 的 `ref` 是 Store 返回的规范引用，不应由调用方拼接物理路径。GraphStore 的 `seed_ids`
只定位遍历入口，词项匹配策略由后端实现。

## 17. CRUD、批量与异常契约

| 场景 | 标准结果 |
|---|---|
| `insert` 的 `(scope, id/key)` 已存在 | `ConflictError` |
| `update` 的 `(scope, id/key)` 不存在 | `NotFoundError` |
| `delete` 目标不存在 | 幂等成功，不抛缺失异常 |
| KV/FS 单条 `get` 不存在 | `NotFoundError` |
| KV `mget` 任意 key 不存在 | `NotFoundError`，不返回部分列表 |
| 检索型 Store 批量 `get` 部分 ID 缺失 | 省略缺失项，返回实际找到的记录 |
| `MemoryUnit.scope` 与 `Storage.add/update` 显式 Scope 不一致 | 内置 `CompositeStorage` 抛 `ValidationError` |
| 过滤算子、向量维度、query 或配置非法 | `ValidationError` |
| 访问未声明能力/具名端口 | `UnsupportedStorageCapabilityError` |
| `StorageSecurity.authorize` 拒绝 | `PermissionDeniedError` |
| 外部后端连接失败、超时或不可用 | `BackendError` |
| 多召回入口部分失败 | 成功 batch + `ChannelError` |
| 全部选中召回入口失败 | `StorageRetrievalError` |

标准接口不要求多条批量或多 Store 操作具备全局原子性。实现如果依赖后端批量 API，
应在自身文档和测试中说明是 all-or-nothing 还是可能部分成功；调用方不应从 `None`
返回值推断跨后端事务存在。

## 18. Storage mode 与实现能力矩阵

`IndexWriteMode` / `IndexRemoveMode` 是面向 Storage 接口的逻辑要求，不代表每个 Storage
都内建索引投影能力。

| 组合 | `ALL` | `FORWARD_ONLY` | `RETRIEVAL_ONLY` | `SOFT` | `HARD` |
|---|---|---|---|---|---|
| `CompositeStorage` 直接 CRUD | 写/更新 KV 记忆本体 | 写/更新 KV 记忆本体 | 空操作 | 空操作，保留本体 | 删除 KV 记忆本体 |
| `HybridIndexBuilder + CompositeStorage` | forward + fulltext + vector + 可选 entity | 只执行 forward | 只执行检索子 builder | 只删派生检索索引 | 先删派生索引，后删 forward |
| `UnifiedIndexBuilder + CompositeStorage` | 只有 KV 记忆本体 | 只有 KV 记忆本体 | 空操作 | 空操作 | 删除 KV 记忆本体 |
| `UnifiedIndexBuilder + 自定义一体化 Storage` | 由该 Storage 实现全部写入 | 必须至少保证本体 | 由实现决定能否单独补索引 | 由实现保证本体保留 | 由实现删除本体和派生索引 |

因此，不能仅看 `Storage` 的抽象方法就假定当前 target 已经同时管理向量、全文和图索引。
是否具备索引投影能力，必须同时看 Storage 实现和与之搭配的 IndexBuilder。

## 19. 后端选择摘要

| Store | 进程内 target | 持久化/远程 target | 关键约束 |
|---|---|---|---|
| KV | `memory` | `sqlite`、`redis`、`postgres`；`encrypted` 包装任意 raw KV | Redis builder 需 `url`；Postgres 需 `dsn`；TTL 单位秒 |
| Vector | `memory` | `milvus`、`pgvector` | `dim` 与 Embedder 一致；检索链要求高分优先 |
| Fulltext | `memory` | `elasticsearch` | analyzer 改变后需重建索引 |
| Graph | `memory` | `nano_graphrag` | 外部实现按 Scope 生成独立 GraphML 命名空间 |
| Fusion | `memory` | `milvus_graph` | `milvus_graph` 当前为“向量种子 + 图扩展”，不实现 BM25 文本融合 |
| FS | `memory` | `local` | LocalFS 在 `root/<scope>/` 内存储并阻止目录穿越 |
| Entity | 无 | `elasticsearch` | `StorageCapability.ENTITY`；后端由 Storage 持有并通过 `entity_port()` 暴露 |

连接型后端通常在首次访问或 `health()` 时才完成真实连接。配置对象能构建成功，
不等于远程服务、schema/index 或 TLS 链路已可用；部署验收应显式调用 `health()`。
