# F01 — 存储层实现规约（src/storage/*_impl）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-06-24 |
| 影响范围 | src/storage/{kv,vector,fulltext,fusion,graph,fs}_impl/，docs/specs/S06-storage.md（如有） |
| 测试基线 | `pytest tests/unit/storage tests/integration/storage` 全绿（真实后端 redis/milvus/es/nano-graphrag/postgres 未配置或不可达时按约定 skip；PostgreSQL 真库由 `AGENT_MEMORY_TEST_PG_DSN` 启用） |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档归档**存储层各后端实现的规约**：每个 `*_impl/` 实现对应哪个接口契约、注册名（`target`）、必填/可选参数、scope 隔离方式、CRUD/search 语义与各自取舍。接口契约本身（方法签名、错误语义、不变量）归 `docs/specs/S06-storage.md`；本文聚焦「当前有哪几种后端、各自怎么落地、为什么这样选」。

---

## 背景

存储层定义 6 类 Store 契约（`kv` / `vector` / `fulltext` / `fusion` / `graph` / `fs`），每类各有一个或多个后端实现。装配按「两级命名空间 + Producer」选用：配置里 `kv_store.<实例>.target: redis` 即选 Redis 后端，参数走 `params`。所有实现共享三条铁律：

1. **接口与实现分离**：顶层 `storage/<type>.py` 是纯抽象 + `XProducer` 工厂（声明 `TOP_NAME`）；实现在 `<type>_impl/*.py`，文件尾部 `@XProducer.register("target")` 自注册，`import storage` 即注册（重依赖后端用惰性导入，未装也能 import + 注册，仅访问后端才报 `BackendError`）。
2. **scope 原生隔离**：所有实现按 `Scope(org/space/user/agent/session)` 隔离。两种落地范式见下「scope 隔离范式」。
3. **错误语义统一**：业务异常（`ConflictError`/`NotFoundError`/`ValidationError` 等 `AgentMemoryError` 子类）由实现按契约主动抛出并原样透传；后端 I/O 的非预期异常经 `_support.wrap_backend` 归一为 `BackendError`。

### scope 隔离范式（`_support.py`）

| 范式 | 工具 | 适用 | 语义 |
|---|---|---|---|
| **定长五段命名空间** | `scope_segments(scope)` | kv / fs / graph / fusion-graph | scope 折成 `org/space/user/agent/session` 定长五段（空维用 `_` 占位，`/`、`:` 转义），拼进 key/路径/namespace；不同 scope 互不可见，**精确匹配**该 scope |
| **维度等值过滤** | `scope_dims(scope)` | vector / fulltext（检索型后端） | 对非空维度施加等值约束；`org` 非空时即便 `space==""` 也下推 `space == ""`，避免空 space 查询跨到其他 space |

---

## 决策：各类后端实现规约

### KVStore（`storage/kv.py` · `KvProducer` · TOP_NAME=`kv_store`）

真源键值存储。`scope` 隔离、`ttl` 以秒计（`0`=永不过期）、值以 `bytes` 收发。

| target | 类 | 后端 | 必填参数 | 可选参数（默认） | 隔离 | 关键语义 |
|---|---|---|---|---|---|---|
| `memory` | `InMemoryKVStore` | 进程内 dict | — | — | scope 折五段命名空间键 | `ttl` 过期在 get/exists/scan 时**惰性清除**；`list` 使用公共 MemoryUnit 过滤/计数/分页 |
| `sqlite` | `SQLiteKVStore` | 标准库 `sqlite3` 落盘 | — | `db_path`(`agent_memory.db`) | scope 五维各落一列，主键 `(org,space,user,agent,session,key)` | 跨进程/重启保留；`check_same_thread=False` + 一把锁串行化（HTTP 多线程）；过期行读时过滤 + 惰性删；`":memory:"` 为进程内；旧表迁移到空 `space` |
| `redis` | `RedisKVStore` | Redis（`redis-py` 惰性导入） | `url` | `host`/`port`(6379)/`db`(0)/`password` | key 前缀 `org:space:user:agent:session:<key>` | `insert`=`SET NX`（已存在→`ConflictError`）、`update`=`SET XX`（不存在→`NotFoundError`）；`ttl`→`px` 毫秒；`scopes()` 扫 `*` 还原五段 |
| `postgres` | `PostgresKVStore` | PostgreSQL（`psycopg` 3 + 每实例连接池，惰性导入） | `dsn` | `schema`(`public`)/`table`(`agent_memory_kv`)/池大小/`auto_create_schema` | scope 五维各落一列，复合主键 | 条件式 `ON CONFLICT` 原子区分活跃冲突与过期覆盖；TTL 用库侧 Unix 秒，读取过滤过期行；`scan` 用 `starts_with` 做字面前缀；`list` 使用公共 MemoryUnit 过滤/计数/分页 |

> 上述四个真源后端与 `encrypted` 装饰器同实现 `KVStore` 契约 + 同一字节编码；`list` 当前都使用公共兼容路径完成
> MemoryUnit 过滤、精确计数、稳定排序和分页，装配替换后上层语义不变。
> **`mget` 落地差异**（批量点读，返回与 `keys` 下标一一对应的 `list[bytes]`，不去重、支持重复 key；任一 key 缺失即抛 `NotFoundError`，与 `get` 一致）：`memory` 逐 key 走 `_live`，缺失即报（无往返开销）；`sqlite` 单条 `IN` 查询召回命中 → 按位置组装，缺失报错（`WHERE` 过滤已过期行，与 `list` 同款，惰性删仍走单 key 的 `_live_value`/`get`）；`redis` 原生 `MGET` 一次往返，redis 返回的 `None` 位归一为 `NotFoundError`（省逐条 `get` 网络往返）；`postgres` 单条 `ANY` 查询召回命中后按输入位置组装，过期或缺失 key 统一报错；`encrypted` 委托 raw `mget` 取密文（raw 已保证全命中）后逐项解密（AAD 绑 key，不能批量解）。

### VectorStore（`storage/vector.py` · `VectorProducer` · TOP_NAME=`vector_store`）

向量 ANN 索引 + 按 id 正排。`insert/update/delete/get` 走主键 CRUD，`search` 走近邻检索；`id` 是 scope 内逻辑主键，外部后端可用 `scope + id` 生成物理主键，`metadata` 承载标量、`filters` 为 scope 之外的谓词。

| target | 类 | 后端 | 必填参数 | 可选参数（默认） | 隔离 | 关键语义 |
|---|---|---|---|---|---|---|
| `memory` | `InMemoryVectorStore` | 进程内暴力余弦 | — | — | scope 折五段命名空间键 | `search` 暴力算余弦、过滤 `score>0`、降序 top-k；维度一致性由调用方（同一 Embedder）保证 |
| `milvus` | `MilvusVectorStore` | Milvus（`pymilvus` 2.4+ `MilvusClient` 惰性导入） | `uri`、`dim`（>0，回退 `globals.embedder_dim`） | `host`/`port`(19530)/`token`/`collection`(`agent_memory_vectors`)/`metric_type`(`COSINE`)/`consistency_level`(`Strong`)/`scope_field_max_length`(256)/`id_max_length`(512) | scope 五维落标量字段，表达式 `scope_x == v` 约束 | 首次连接 `_ensure_collection`（建 schema：id/vector/5×scope/metadata-JSON + AUTOINDEX）；**Strong 一致性**保证 read-after-write；`insert` 先 query 查重→`ConflictError`，`update` 查缺→`NotFoundError` 后 `upsert`；`score`=Milvus distance（COSINE/IP 越大越近、L2 越小越近） |
| `pgvector` | `PgVectorStore` | PostgreSQL 16 + pgvector ≥0.8.0（`psycopg` 惰性导入） | `dsn`、`dim` | `schema`/`table`/`metric_type`/`index_type`/HNSW 与池参数/`auto_create_schema` | scope 五维与逻辑 id 组成复合主键，search 按 scope 维度过滤 | HNSW + iterative scan；COSINE/L2/IP 均转为高分优先；FilterExpr 在 top-k 前编译为参数化 jsonb SQL；`update` 不改 scope；`index_type=none` 在事务内禁用 index scan 做精确搜索 |

> `dim` 必须 >0，构造期即校验（缺失或回退后仍为 0 → `ValidationError`）。

### FulltextStore（`storage/fulltext.py` · `FulltextProducer` · TOP_NAME=`fulltext_store`）

全文倒排。`search` 走关键词相关性，`filters` 承载 scope 之外的谓词。

| target | 类 | 后端 | 必填参数 | 可选参数（默认） | 隔离 | 关键语义 |
|---|---|---|---|---|---|---|
| `memory` | `InMemoryFulltextStore` | 进程内词重叠计分 | — | 依赖 `tokenizer`（`dep`，缺省 `whitespace`） | scope 折五段命名空间键 | 分词复用注入的 `Tokenizer`（与构建侧同实例=同词表）；`score`=命中词数/文档词数模拟 BM25；降序 top-k |
| `elasticsearch` | `ElasticsearchFulltextStore` | Elasticsearch（`elasticsearch-py` 8.x 惰性导入） | `hosts` | `index`(`agent_memory_fulltext`)/`username`+`password` 或 `api_key`/`text_field`(`text`)/`refresh`(`false`) | scope 落文档 `scope.{dim}` 嵌套 keyword，`term` 过滤非空维 | 首次连接 `_ensure_index`：`metadata.*` 字符串**动态映射为 keyword**（精确等值/集合/包含；text 分析器会拆词小写化导致匹配不上），数值/布尔动态推断支持 range；`insert`=bulk `create`（409→`ConflictError`），`update` 先 mget 查缺→`NotFoundError` 再 bulk `index`，`delete` 用受 scope 约束的 `delete_by_query`；`refresh: wait_for` 让写入对随后 search 立即可见；`search`=`match` + scope/filters，`score`=BM25 `_score` |

### FusionStore（`storage/fusion.py` · `FusionProducer` · TOP_NAME=`fusion_store`）

向量·倒排·正排（·图）合一的**另一种存储形态**：同一 id 一行同时承载向量/文本/标量/原始值，一次召回免 join。

| target | 类 | 后端 | 必填参数 | 可选参数（默认） | 形态 | 关键语义 |
|---|---|---|---|---|---|---|
| `memory` | `InMemoryFusionStore` | 进程内 | — | 依赖 `tokenizer`（缺省 `whitespace`） | 向量 + 文本混合 | `search` 一次内做余弦 + 词重叠，按 `vector_weight` 混合（`w*vscore + (1-w)*tscore`），受 `scalar_filters` 约束；分词复用注入 Tokenizer |
| `milvus_graph` | `MilvusGraphFusionStore` | Milvus（向量+正排）+ nano-graphrag（图） | `uri`、`working_dir`、`dim`（回退 `embedder_dim`） | `collection`(`agent_memory_fusion`)/`metric_type`(`COSINE`)/`namespace_prefix`/`link_field`(`links`)/`neighbor_depth`(1)/`neighbor_decay`(0.5)/`neighbor_relation`(`linked`) | **向量 → 图** | 写入：每条 `FusionRecord` 的向量/标量/文本/value 落 Milvus（value base64 进 metadata），同时作为节点 upsert 进图，`scalars[link_field]` 声明邻居建边（图侧 upsert，幂等、不走严格 CRUD）；检索：先 Milvus ANN 召回种子，再沿图扩展邻居（最多 `neighbor_depth` 跳，邻居分 `seed.score * decay**hop`），种子向量分覆盖其衰减分，合并去重降序——**结果条数可超 `top_k`**；未用 `FusionQuery.text`/`vector_weight`（文本仅存供 get 回读，不做 BM25） |

> `milvus_graph` 内部复用 `MilvusVectorStore`（继承其 scope 隔离/冲突/缺失语义）与 nano-graphrag `NetworkXStorage`（每 scope 一 namespace）。

### GraphStore（`storage/graph.py` · `GraphProducer` · TOP_NAME=`graph_store`）

属性图：节点/边 CRUD + 邻域遍历 + `seed_ids`（按关键词在节点 `content` 属性子串命中找种子，供图召回在「无 query 起点」时定位起点）。

| target | 类 | 后端 | 必填参数 | 可选参数（默认） | 隔离 | 关键语义 |
|---|---|---|---|---|---|---|
| `memory` | `InMemoryGraphStore` | 进程内邻接表 | — | — | scope 折五段命名空间键 | 节点/边按 id 隔离存；`search` 从 `start_id` 无向 BFS 扩展（按 `depth` 跳、可选 `relation` 过滤、`limit` 截断）；删节点连带删关联边；`seed_ids` 属性子串命中 |
| `nano_graphrag` | `NanoGraphRAGGraphStore` | nano-graphrag `NetworkXStorage`（GraphML 持久化） | `working_dir` | `namespace_prefix`(`agent_memory_graph`)/`create_root`(True) | 每 scope 一 namespace（独立 GraphML 文件） | **shim 惰性加载**：直接加载 `nano_graphrag._storage.gdb_networkx`，绕过包 `__init__` 的重依赖（openai/tiktoken/graspologic/dspy/hnswlib/neo4j），仅需 networkx+numpy+tiktoken；async↔sync 经常驻事件循环桥接；`nx.Graph` 单边（非多重图），边逻辑 id 存为属性、按 id 反查 `(u,v)` 定位，同对端点至多一边（再插→冲突）；删除走底层 `_graph`（`NetworkXStorage` 无删除） |

### FSStore（`storage/fs.py` · `FsProducer` · TOP_NAME=`fs_store`）

原模态资产/原始负载二进制存储（`MemoryUnit.assets` 指向的图片/音频/原件）：`insert` 返回规范引用 `ref`，后续 `(scope, ref)` 寻址 get/stat/delete。

| target | 类 | 后端 | 必填参数 | 可选参数（默认） | 隔离 | 关键语义 |
|---|---|---|---|---|---|---|
| `memory` | `InMemoryFSStore` | 进程内 bytes | — | — | `ref = fs://org/space/user/agent/session/key` | `insert` 读流落库、返回 ref；`get` 返回 `BytesIO`；`stat` 给 size/时间，content_type 固定 `application/octet-stream` |
| `local` | `LocalFSStore` | 本地文件系统 | `root` | `create_root`(True) | `root/<scope 五段>/<ref>` | `ref`=相对 scope 子目录的逻辑路径（`insert` 的 `key` 即 ref）；**阻断目录穿越**（`ref` 逃出 scope root → `ValidationError`）；`delete` 幂等（`missing_ok`）；`stat` 经 `mimetypes` 猜 content_type |

---

## 拒绝的方案

- **`import nano_graphrag` 直接用其图存储**：被拒。包 `__init__` 急切拉起整条 GraphRAG 流水线（openai/tiktoken/graspologic/dspy/hnswlib/neo4j），在新版 Python 上多无法构建。改用 `PathFinder` + stub 占位，只加载 `_storage.gdb_networkx` 子模块。
- **重依赖后端缺失即 import 失败 / 连坐默认实现**：被拒。改为惰性导入——未装 redis/pymilvus/es/nano-graphrag 仍可 `import storage` 并完成工厂注册，只有真正访问后端才抛 `BackendError`；可选后端在 `*_impl/__init__.py` 用 `try/except ImportError` 包裹，互不连坐。
- **必填连接参数（url/uri/hosts/root/working_dir）惰性校验**：被拒。改为 `Factory.require_param` 在 **build 阶段**即报错，而非拖到首次连接才暴露。
- **Milvus 默认 Bounded 一致性**：被拒。记忆库需读己之写，固定 `consistency_level="Strong"`，让 get/search 立刻看到刚写入/删除的结果。
- **ES `metadata.*` 走默认 text 映射**：被拒。text 分析器拆词小写化会让 `"Red Hat"` 之类等值/集合过滤匹配不上；改用 dynamic_template 把 metadata 字符串映射为 keyword，数值/布尔仍动态推断以支持 range。
- **`mget` 返回 `dict[str, bytes]`（缺失 key 省略）**：被拒。`dict` 天然去重（重复 key 只占一项），与 Redis `MGET` 的位置返回不匹配——Redis 实现需把位置列表再转 `dict`，丢失「重复 key 各位置独立」的能力。改用 `list[bytes]` 位置对应：与 Redis `MGET` 同形、零转换，且显式表达「不去重」契约。
- **`mget` 内部做去重**：被拒。`mget` 是通用批量点读原语，去重是调用方语义（不同调用方策略可能不同），混进接口会让契约语义不纯——去重留在调用方（如 `UnitReader.load`），不下沉到 `mget`。
- **`mget` 缺失静默省略（返回 `list[bytes | None]`、缺失位 `None`）**：被拒。这会让 `mget` 缺失语义与单条 `get` 分叉——同一事实（key 不存在）在 `get` 报错、在 `mget` 却静默，调用方须记两套规则；且把「索引↔真源短暂不一致」的兜底职责偷偷下沉进存储接口，违背「存储接口语义纯、调用方自担容忍度」。改为缺失即抛 `NotFoundError`（与 `get` 一致），缺失兜底由需要它的调用方自己承担。

---

## 验证

- `pytest tests/unit/storage tests/integration/storage` 全绿（exit 0）。
- 真实后端（redis 落盘已装、milvus@19530 / nano-graphrag fusion 未连通）按现有 integration 约定 **自动 skip**；`redis` lazy-missing 路径在已装 redis 的环境下亦 skip。
- 内存实现（memory/sqlite/local/networkx-shim）在无外部服务下全程可跑，是 CI 的常驻覆盖面。

---

## 已知遗留

- **`milvus_graph` 融合不支持 text/BM25 通道**：`FusionQuery.text` / `vector_weight` 仅 `InMemoryFusionStore` 实现，Milvus 融合形态聚焦「向量→图」，文本只随记录存储供 get 回读。
- **`milvus_graph` update 不移除旧链边**：`update` 走 upsert 追加新边，移除旧链需 `delete + insert`。
- **`InMemoryFulltextStore` 计分非真 BM25**：词重叠比值近似，仅用于离线/测试；生产全文走 ES。
- **`nano_graphrag` 单边模型**：同一对端点至多一条边，多重关系无法并存（再插按冲突处理）。
