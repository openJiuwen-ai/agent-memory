# Agent Memory Storage（存储层）

**规约文档**：[S06-storage.md](../../docs/specs/S06-storage.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

统一 CRUD 动词（insert/delete/update/get）+ 检索型 `search`。六种后端：`VectorStore`（向量）/ `GraphStore`（图）/ `FulltextStore`（全文）/ `KVStore`（键值）/ `FSStore`（文件系统）/ `FusionStore`（向量+倒排+正排融合）。**scope 隔离是存储层的原生职责**。

## 模块地图

| 文件 | 职责 |
|---|---|
| `base.py` | BaseStore 基类：所有存储后端的自描述契约（store_type / health） |
| `types.py` | 存储层数据类型：KVMemoryListResult/VectorRecord/Document/Node/Edge/FusionRecord/FileStat 等 |
| `kv.py` | KVStore 接口：键值存储，统一 CRUD + MemoryUnit 列表查询 + 范围枚举 |
| `vector.py` | VectorStore 接口：向量存储，统一 CRUD + ANN 检索 |
| `graph.py` | GraphStore 接口：属性图存储，节点与边统一 CRUD + 邻域遍历 |
| `fulltext.py` | FulltextStore 接口：全文倒排索引存储，统一 CRUD + 关键词检索（BM25） |
| `fusion.py` | FusionStore 接口：融合存储（向量+倒排+正排一体） |
| `fs.py` | FSStore 接口：文件系统存储（原始负载/二进制资产） |
| `kv_impl/` | KVStore 实现目录（memory / sqlite / redis / encrypted）及共用的 `memory_list.py` 兼容逻辑 |
| `vector_impl/` | VectorStore 实现目录（memory） |
| `graph_impl/` | GraphStore 实现目录（memory） |
| `fulltext_impl/` | FulltextStore 实现目录（memory） |
| `fusion_impl/` | FusionStore 实现目录（memory） |
| `fs_impl/` | FSStore 实现目录（local） |
| `bootstrap.py` | 统一触发所有存储后端注册 |

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

## 与其他子目录的边界

**本模块管**：
- 可配置真源（KVStore）
- 多后端索引存储（Vector/Fulltext/Graph/Fusion）
- 文件系统存储（FSStore）
- 统一 CRUD 动词
- scope 原生隔离

**不管**：
- 鉴权（归 `api`）
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
