# F05 — 统一 Storage 门面设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-06 |
| 影响范围 | `jiuwen_memory/storage` 统一门面，以及 `jiuwen_memory/construction`、`jiuwen_memory/retrieval`、`jiuwen_memory/control` 对存储层的依赖方式 |
| 测试基线 | Storage、Construction、Retrieval 与 Control 的目标单测通过；默认加密路径需要可写的本地密钥目录 |
| Refs | —（如有 issue 补 `Refs: #<n>`） |

> 本文档记录统一 Storage 的设计及首版实现。当前已落地统一存储门面、
> MemoryUnit 领域操作、底层 Store 能力暴露、安全扩展和实现边界；检索的多种 pipeline
> 见 [F05-storage-retrieval-pipelines.md](../retrieval/F05-storage-retrieval-pipelines.md)，
> 接口改名另行设计。

---

## 背景

当前上层组件直接注入 `KVStore`、`VectorStore`、`FulltextStore`、`GraphStore`、
`FusionStore` 和 `FSStore`。这使上层不仅知道自己使用的存储能力，还需要参与多个 Store
的选择、装配和实例共享：

- Construction 直接使用 KV 落真源，并把不同索引投影写入对应 Store；
- Retrieval 的 Recaller 和 MemoryUnit 加载逻辑分别依赖索引 Store 与 KVStore；
- Control 中的记忆读写和治理逻辑也需要知道 MemoryUnit 的真源位于 KV；
- 接入一个同时提供真源、索引和检索能力的一体化平台时，仍需把平台拆成多个 Store
  适配器后再由上层重新拼装。

这类依赖暴露了底层存储拓扑。替换后端不只是替换一个实现，还会影响上层构造参数、
装配分支和能力判断。

## 目标

1. 上层统一依赖 `Storage`，不再负责多个底层 Store 的创建、选择和共享。
2. `Storage` 提供 MemoryUnit 领域操作，使上层不需要知道 MemoryUnit 真源位于 KV。
3. `Storage` 同时暴露所支持的完整底层 Store 接口，满足索引构建等细粒度使用场景。
4. 调用方可以在装配阶段判断当前实现是否支持 KV、Vector、Fulltext、Graph、Fusion、FS
   等能力。
5. 现有多个 Store 的组合继续作为默认实现；一体化平台可以直接实现统一 Storage 契约。
6. scope 隔离、错误归一和既有 Store 行为不因增加门面而弱化。
7. 提供可插拔的统一授权入口，并允许各 Store 使用适合自身数据模型的安全模块。

## 非目标

- 不把信息抽取、分类、Embedding、分词、图关系生成等 Construction 逻辑移入 Storage。
- 不在本文重复定义 `recall -> get -> rank` 的多种 Retrieval pipeline。
- 不在本阶段执行 Memory API 和 Store 检索动词的改名。
- 不负责 grant/revoke、权限策略管理或 Space 生命周期管理。
- 不在当前阶段设计敏感 metadata 的字段级应用层加密。
- 不在统一门面内承诺跨异构后端的分布式事务。

---

## 决策

### 一、Storage 是统一契约，CompositeStorage 是默认实现

`Storage` 表示上层可依赖的统一存储契约，不使用 `UnifiedStorage` 作为具体实现名。
“统一”是该接口承担的架构角色，不是一种后端类型。

`CompositeStorage` 是 `Storage` 的默认实现。它接收已经完成装配的各类 Store，统一完成
能力声明、领域操作委托、底层端口暴露和健康检查。它不重新实现各 Store 的数据结构和
查询算法。

一体化平台使用独立的 `Storage` 实现，例如 `IntegratedStorage`。该实现可以直接调用
平台的原生接口，不需要先把平台物理拆成 KV、Vector、Fulltext 等多个服务。

```text
Construction / Retrieval / Control
                 |
              Storage
                 |
        +--------+----------------+
        |                         |
CompositeStorage           IntegratedStorage
        |                         |
 KV / Vector / ...          一体化存储或召回平台
```

### 二、提供 MemoryUnit 领域操作

`Storage` 顶层提供下列 MemoryUnit 领域操作：

| 操作 | 语义 |
|---|---|
| `add` | 保存上层已经形成的 MemoryUnit；不负责接入、抽取、演化或生成索引投影 |
| `update` | 更新 scope 内已经存在的 MemoryUnit |
| `delete` | 在 scope 内按 MemoryUnit id 幂等删除 |
| `get` | 在 scope 内批量按 id 读取 MemoryUnit，供业务点查和 Retrieval 的 get 阶段使用 |
| `list` | 按 memory type、FilterExpr、offset、limit 和 extensions 查询 MemoryUnit，并返回匹配总数 |

这些操作统一显式接收 `Scope`。写入时必须校验 MemoryUnit 自身归属与显式 Scope 一致，
不允许把一批属于不同 Scope 的 MemoryUnit 隐式拆分后写入。

`add` 和 `get` 采用批量语义：单条调用由上层归一为长度为 1 的列表。`get` 的结果按输入
id 顺序返回，缺失 id 不产生占位项；需要单条缺失报错的公共 API 由其调用层检查结果并
转换错误。

`add` 成功表示 MemoryUnit 真源已经可被 `get` 读取，不表示所有外部派生索引均已完成。
索引投影仍由 Construction 生成并通过对应底层端口写入。若一体化平台在 `add` 内部自动
建立原生索引，这是实现细节，但不得改变 MemoryUnit 真源的读写语义。

### 三、以命名空间暴露底层 Store 的完整接口

统一 Storage 既屏蔽装配，也允许有明确需求的组件访问标准底层能力。底层方法不平铺到
`Storage` 顶层，避免六类 Store 的 `insert`、`update`、`delete`、`get`、`search`
发生名称和参数冲突。

调用方式统一为：

```python
units = storage.get(scope, unit_ids, access=access)

if storage.has_vector():
    records = storage.vector.get(scope, vector_ids, access=access)
```

各端口返回现有抽象接口，不返回 Milvus、Elasticsearch、Redis 等具体实现类：

| Storage 端口 | 暴露的完整契约 |
|---|---|
| `storage.kv` | `KVStore`：insert / update / delete / get / mget / exists / scan / list / scopes / security / health |
| `storage.vector` | `VectorStore`：insert / update / delete / get / search / recall / score_higher_is_better / security / health |
| `storage.fulltext` | `FulltextStore`：insert / update / delete / get / search / security / health |
| `storage.graph` | `GraphStore`：seed_ids / insert / update / delete / get / search / security / health |
| `storage.fusion` | `FusionStore`：insert / update / delete / get / search / security / health |
| `storage.fs` | `FSStore`：insert / update / delete / get / stat / security / health |

底层 Store 的具体方法签名、过滤语义和错误语义继续由 `S06-storage.md` 维护。统一 Storage
只增加访问边界，不复制或修改这些契约。

### 四、能力集合是唯一事实来源

每个 `Storage` 实现提供一个不可变能力集合，至少覆盖：

- `KV`
- `VECTOR`
- `FULLTEXT`
- `GRAPH`
- `FUSION`
- `FS`

`has_kv()`、`has_vector()`、`has_fulltext()`、`has_graph()`、`has_fusion()`、
`has_fs()` 由 `Storage` 基类根据能力集合统一推导，具体实现不得分别维护另一组布尔状态。

能力表示该 Storage **对外完整提供对应 Store 契约**，不表示其内部使用了某项技术。例如，
一体化平台内部使用向量索引，但没有提供完整的 `VectorStore` 行为时，`has_vector()` 必须
返回 false。只有平台直接支持完整契约或 Storage 提供了符合契约的适配端口时，才能声明
`VECTOR`。

当 `has_vector()` 返回 true 时，`storage.vector` 必须可用。未声明的端口被直接访问时，
统一抛出 `UnsupportedStorageCapabilityError`，不返回 `None`，使已完成能力判断的调用代码
保持确定的非空类型。

分层索引使用同一能力的命名端口，例如 `vector_port("layers_l0")` 与
`fulltext_port("layers_l1")`。`has_vector_port(name)` 等方法与端口成对使用；默认端口名为
`default`。命名端口是同一 Storage 的装配细节，不要求上层再从 Store Producer 解析具名实例。

该集合不加入 `RECALL`、`RECALL_AND_GET`、`RETRIEVE`。检索路径不是底层端口 capability，
而由 Storage 单独提供的全局首选 pipeline 表达；具体选择规则归 Retrieval 特性文档。

### 五、检索适配入口独立于 capability

Storage 保留以下检索适配入口，但不为它们增加 `has_*()`：

| 接口 | 作用 |
|---|---|
| `preferred_retrieval_pipeline` | 返回该 Storage 全局、稳定的首选路径 |
| `recall` | 按通道返回 id、分数和证据 |
| `recall_and_get` | 按通道返回已经物化的 MemoryUnit 候选 |
| `retrieve` | 接收 Fuser，在 Storage 入口内完成 recall、get、rank |

每个实现只需保证其首选入口可用；非首选入口可由实现提供兼容能力，但 Retriever 不据此做
运行期探测。首选值、参数、返回结构、部分失败和选择规则统一由
[F05-storage-retrieval-pipelines.md](../retrieval/F05-storage-retrieval-pipelines.md) 定义。

### 六、两级 Security 模型

Storage 同时提供统一授权入口和各 Store 自有的数据保护模块：

```text
Storage
├── security: StorageSecurity
├── kv.security: KVSecurity
├── vector.security: VectorSecurity
├── fulltext.security: FulltextSecurity
├── graph.security: GraphSecurity
├── fusion.security: FusionSecurity
└── fs.security: FSSecurity
```

`StorageSecurity` 负责通用数据面授权。授权能力可插拔，默认实现为
`AllowAllStorageSecurity`，不执行权限限制。启用非默认授权实现后，调用方必须传入有效的
`StorageAccessContext`；缺失或校验失败统一抛 PermissionDeniedError。

所有 Storage 领域接口和对外暴露的 Store 端口都接受显式、可选的 `access` 参数。默认
allow-all 下可省略；启用授权后不能省略。授权上下文不放入 Scope、MemoryUnit、查询对象或
metadata。

每个公开逻辑操作只执行一次完整授权。授权通过后生成仅限本次调用的内部授权结果，后续 KV、
Vector、Fulltext 等物理操作只校验其 Scope 和 Action，不重复查询授权策略。直接调用
`storage.vector.get()` 等端口时仍先经过统一授权；CompositeStorage 对外暴露安全代理，原始
Store 只供其内部委托，不能成为绕过统一授权的入口。

`StorageSecurity` 只做 authorize，不负责 grant、revoke 和策略生命周期。授权所需的共享
Authorizer 协议和上下文类型放在 common，Storage 不反向依赖 Control。

各 Store 的 security 由 Store 内部调用，不要求上层预先加密：

- KV 可对 value 做应用层加解密，现有 EncryptedKVStore 在迁移期继续兼容；
- Vector、Fulltext、Graph、Fusion 的检索字段保持可查询，依赖 TLS 和后端原生静态加密；
- FS 可根据后端能力保护二进制内容；
- 当前不设计敏感 metadata 字段分类，也不承诺可搜索密文能力；
- 未启用 Store 级数据保护时使用明确的 passthrough 实现，不能宣称已加密。

调用顺序固定为：

```text
StorageSecurity.authorize
  -> 选择 Store / 执行领域操作
  -> Store.security 保护或还原数据
  -> 后端
```

### 七、统一健康检查

`Storage.health()` 检查该实现对外声明的全部能力：

- `CompositeStorage` 逐一调用已配置 Store 的 `health()`；
- 一体化 Storage 调用平台自身的健康检查；
- StorageSecurity 与各 Store security 都参与健康检查；
- 任一已声明能力不可用时，统一抛出 `HealthCheckError`；
- 未声明、未配置的可选端口不参与健康检查。

健康检查只反映当前实现是否能够履行已声明契约，不借此动态增删 capability。运行期间
能力集合保持稳定，避免同一实例的装配结构随请求变化。

### 八、CompositeStorage 的职责边界

`CompositeStorage` 负责：

- 持有并复用装配完成的 Store 实例；
- 维护能力集合并暴露标准 Store 端口；
- 把 MemoryUnit 领域操作映射到真源 Store；
- 注入统一 StorageSecurity，并对外暴露经过授权的 Store 代理；
- 聚合健康检查；
- 统一缺失能力的错误。

`CompositeStorage` 不负责：

- 生成 VectorRecord、Document、Node、Edge 等索引投影；
- 执行 QueryParser、Reranker 或 Discloser；
- 管理 grant/revoke 或定义业务授权策略；
- 在 KV、Vector、Fulltext、Graph 等多个后端之间提供分布式事务；
- 根据业务请求动态创建或替换 Store。

Storage 的检索入口按 Retrieval 特性文档接收 Fuser；这是单次方法入参，不是
CompositeStorage 长期持有的反向依赖。

### 九、上层依赖规则

一般业务路径优先使用 `Storage` 顶层的 MemoryUnit 领域操作。只有确实需要底层数据模型或
索引原语的组件才访问命名端口：

| 调用方 | 推荐依赖方式 |
|---|---|
| Construction 真源落盘 | `storage.add/update/delete` |
| Construction IndexBuilder | 能力判断后访问 `storage.vector/fulltext/graph/fusion` |
| Retrieval 的 MemoryUnit 加载 | `storage.get` |
| 特定 Recaller | 能力判断后访问对应检索型 Store 端口 |
| Control 中的 MemoryUnit 治理 | `storage.get/update/delete/list` |
| Space 注册表等通用键值状态 | 能力判断后访问 `storage.kv` |
| 原模态资产读写 | 能力判断后访问 `storage.fs` |

上层可以知道自己需要“向量能力”或“通用 KV 能力”，但不能知道这些能力由哪个具体后端、
连接参数或共享实例提供。

### 十、统一装配入口

`StorageProducer` 使用 `storage` 顶层命名空间。默认 `storage.default` 选择 `composite`，
并通过具名引用复用已经装配的 KV、Vector、Fulltext、Graph 等 Store。`build_kernel` 先完成
真源 KV 的安全包装，再构建统一 Storage，因此 `CompositeStorage.kv` 与 Engine 使用同一真源。

Retriever 通过 `StorageProducer` 获取该具名实例。一体化平台只需注册新的 Storage target 并
把 `storage.default` 指向它，不需要修改 Retriever 的实现选择。现有 Recaller 仍是默认组合
实现的兼容适配器，由 Retriever 在装配阶段绑定；该绑定不让 storage 包导入 retrieval。

---

## 拒绝的方案

### 将所有 Store 方法平铺到 Storage

拒绝 `vector_get`、`fulltext_get`、`kv_get` 等前缀方法，也拒绝让 `Storage` 同时继承全部
Store。不同 Store 的同名方法参数和返回类型不同，平铺会制造大量重复接口，并使新增一种
Store 时必须扩展统一门面的全部方法。

### 只提供 MemoryUnit 领域接口，不暴露底层端口

该方案屏蔽最彻底，但 Construction 的索引构建器、特定 Recaller 和资产读写仍需要标准
底层数据模型。禁止端口访问会迫使这些能力重新绕过 Storage 装配，或者把 Embedding、分词、
图构建等逻辑错误地下沉到 Storage。

### 只封装 Store 端口，不提供 add/get

该方案只能屏蔽具体实例的创建，不能屏蔽“MemoryUnit 位于 KV”这一存储拓扑。Retriever
仍需自行拼 `memory_key`、反序列化并处理缺失，接入一体化平台时仍要模拟 KV 真源，因此拒绝。

### 所有实现强制提供六类 Store

一体化平台可能原生完成记忆保存与检索，但不公开图、文件或通用 KV 原语。强制补齐全部端口
会产生无意义的模拟实现，也会让 capability 失去价值。各端口必须保持可选。

### 可选端口返回 None

调用方已经通过 `has_*()` 做能力判断后，仍需处理 Optional，会让能力发现和类型契约重复。
未支持的直接访问统一报明确异常，支持的端口保持非空接口类型。

### Storage.add 负责 Construction

若 `add` 同时执行抽取、Embedding、分词和图关系生成，Storage 将反向依赖 Construction，
破坏模块方向，也让不同后端产生不同的记忆构建结果。因此 `add` 只接收上层已经形成的
MemoryUnit；派生索引投影由 Construction 负责。

### 在调用方手工加密后再写 Store

该方案无法保证所有入口执行相同保护策略，还会引入重复加密和解密遗漏。Store 只接收正常领域
数据，由自身 security 在后端边界保护和还原。

### 授权仅由调用方自觉执行

Storage 同时暴露领域接口和细粒度 Store 端口，依赖调用纪律会使
`storage.vector.get()` 成为绕过点。统一授权必须由公开 Storage/Store 代理执行；默认不需要
授权通过显式 allow-all 插件表达，而不是省略安全边界。

### 对检索字段做普通应用层加密

普通密文无法执行 ANN、BM25 或图遍历。当前阶段依赖 TLS 和后端原生静态加密保护检索字段，
不引入可搜索加密或 TEE 基础设施。

---

## 验证计划

实现阶段至少需要覆盖：

1. `CompositeStorage` 对每一种可选 Store 组合正确生成 capability。
2. 每个 `has_*()` 与对应端口的可用性严格一致。
3. 未声明端口被访问时抛 `UnsupportedStorageCapabilityError`。
4. `add/get/update/delete/list` 与当前 KV 真源行为一致，且保持 Scope 隔离。
5. `get` 的批量顺序、重复 id 和缺失 id 语义有独立单测。
6. `list` 的过滤、计数、排序、分页和 extensions 透传不发生退化。
7. `health()` 只检查已声明能力，并正确归一子 Store 健康检查失败。
8. Construction、Retrieval、Control 的装配测试只注入一个 `Storage`。
9. 一个不暴露 KV/Vector 等端口的一体化假实现可以仅通过 MemoryUnit 领域接口完成读写。
10. 默认 AllowAllStorageSecurity 不限制调用；启用授权实现后缺失或无效 access 被拒绝。
11. 领域接口和 `storage.vector.get()` 等细粒度入口都无法绕过统一授权。
12. 每个 Store 在后端边界调用自身 security，passthrough 与受保护实现行为可区分。

## 已知遗留

- Retrieval 的三条 pipeline、首选路径和返回结构见
  [F05-storage-retrieval-pipelines.md](../retrieval/F05-storage-retrieval-pipelines.md)。
- Construction、Retrieval、Control 已统一通过 `StorageProducer.resolve()` 获得同一个
  `storage.default`。MemoryUnit 真源操作优先使用 Storage 领域接口；Space 注册表、原始消息
  缓冲和按原始 key 的清理仍经 `storage.kv` 端口完成。
- `StorageProducer`、`composite` target、默认 `storage.default` 与 Kernel 句柄已经落地；
  Kernel、Construction、Retrieval 与 Control 共享同一具名 Storage 实例。
- 首版只提供 `CompositeStorage`；不暴露底层端口的一体化 Storage 适配器尚未实现。
- `recall_and_get` 首版以 `recall + get` 组合实现，底层原生回带 MemoryUnit 的通道适配待补。
- Memory API `write -> add`、API `recall -> search`、Store `search -> recall` 的改名由第三阶段设计确定。
- 批量 `add` 的原子模式、逐项结果和同一 stream 的时序约束需与批量写设计统一，不在本文重复定义。
