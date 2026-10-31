# 逻辑分片（shard）——分片逻辑由 storage 统一管理，写入透明、读取可按分片收窄

> **术语**：「逻辑分片」（shard）指分片规则输出、对外可见的分片名（如 `working` / `profile` / `episodic`）。分片名是**对外契约**；分片背后的物理表 / 索引名、分片规则、分片标签的生成与维护由 storage 管理，不向上暴露。代码标识符沿用历史前缀（`DomainResolver` / target `domain_routed`），语义统一为「逻辑分片」。
>
> 本方案是**业务逻辑分区**，不是扩容意义上的分布式分片：不包含自动均衡、热点迁移、在线扩缩容。按哈希扩容由各后端自身能力承担。

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-30 |
| 影响范围 | `jiuwen_memory/storage/`（分片规则与定位两个内部契约、归属记录、KV / 向量 / 全文 / 实体 / 图五类 `domain_routed` 包装层、`EntityStore` 契约补齐（`Scope`、`shards`、增量 `UNLINK`、分组关联能力）、`GraphQuery` 节点过滤、`DomainStore.get_in_shards`、`keyword_recaller.py` 透传分片到实体查询）、`jiuwen_memory/common/type_def/`（图节点记忆引用键常量）、`jiuwen_memory/construction/index_builder_impl/entity_index_builder.py`（适配新签名、解除关联改用 `UNLINK`，不感知分片）、`jiuwen_memory/retrieval/expander_impl/default_expander.py`（请求带 `shards` 时改调 `DomainStore.get_in_shards`）、`scripts/`（DDL 模板 + 离线迁移 / 对账脚本）、`docs/specs/S02-memory-api.md` |
| 测试基线 | 待落地后补：`tests/unit/storage/test_domain_resolver.py`、`test_domain_routed_kv.py`、`test_domain_routed_vector.py`、`test_domain_routed_fulltext.py`、`test_domain_routed_entity.py`、`test_domain_routed_graph.py`、`test_shard_support_matrix.py`、`tests/unit/api/test_shards_extension.py` |
| Refs | — |

> **分两阶段**：第一阶段做稳定路由与指定分片召回，**分片在记忆创建时确定，此后修改 `tier` 等字段照常更新，但记忆留在原分片**（粘性归属）；把已有记忆迁到其它分片放到第二阶段，前提是先定义完整的一致性协议（见「第二阶段」）。本文为**设计提案，尚未落地**；实现前须满足文末「实现前置条件」。

## 背景

### 诉求

1. **用户可自定义分片**：分片名称、数量由配置决定。
2. **按 MemoryUnit 字段分片**：例如按抽取阶段由 LLM 判定的 `tier` 分片。
3. **用户可直接指定某条记忆存到哪个分片**，且优先于按字段分片。
4. **分片逻辑只在 storage**：分片规则、物理表、分片标签由 storage 管理；上层唯一可见的是分片名，读取时可用 `shards` 指定只查哪些分片（同 ES：客户端知道索引名 / 别名，不知道物理落点）。上层只透传分片名，不理解分片规则。
5. **指定分片时，分片过滤在各通道截断候选之前生效**：不采用「先召回全部、再过滤」。各通道仍有自身的近似与预算边界（ANN 近似、实体按记录数计 limit、图的深度与访问预算），见 D11。检索耗时以「性能基线」中的验收目标衡量，不作为已成立的结论。

### 现状

- **真源单表混存**：所有记忆同住 `agent_memory_kv`——`/memory/{id}` 存序列化 `MemoryUnit`（`value bytea`），`/messages/{id}` 存原文，`/space/info` 等根 scope 元数据也同表。
- **索引全量混合，但已支持召回前过滤**：向量与全文各三层（正文 L2 走 `default` 端口，摘要 L0 / 概览 L1 走 `layers_l0` / `layers_l1` 端口），记录 metadata 带 `tier` / `system_metadata.*` / `user_metadata.*` 等字段，`filters` 在 top-k 截断前下推到后端。**因此「只查某些 tier」本身不需要拆表，现状用 `filters` 即可做到**。不能下推的是实体扩展：实体索引只存 unit_id，召回后再点读真源过滤。
- **写入顺序**：`HybridIndexBuilder` 依次写正排 KV → 全文 → 向量 → 实体；写实体时该记忆已落 KV。
- **透明门面先例**：`EncryptedKVStore`、`config/routing.py` 的 `Routing*Store` 都实现原 ABC、对上层透明。

### 必要性

物理分片的理由建立在**存储治理**上，指定分片召回是配套能力：

| 需求 | 是否需要物理分片 |
|---|---|
| 只召回某些 tier | 不一定，现有 `filters` 下推即可 |
| 自定义业务分类 | 不一定 |
| 不同索引参数、embedding 资源配额 | 需要 |
| 不同数据库、集群或存储后端 | 需要 |
| 独立容量管理、独立备份 | 有价值；实体与图为共享索引，独立性受限（见「运维」） |
| 不同保留策略（TTL） | 有价值，但需要完整的派生索引清理协议，**第一阶段不提供** |
| 强安全隔离 | 不能仅靠分片保证，仍需授权与部署边界 |

### 第一阶段非目标

- 把已有记忆迁到其它分片（第二阶段）。
- 按分片的保留策略 / TTL 生命周期治理。
- 扩容意义上的分片（自动均衡、热点迁移、扩缩容）。
- 跨分片全文检索与单索引排序完全等价（见 D8）。

---

## 决策

### 决策总览

所有召回相关的存储端口换成 storage 内的 `domain_routed` 包装层。**KV 是分片归属的唯一事实来源**：只有 KV 包装层在创建记忆时用分片规则决定归属；向量、全文、实体、图包装层一律经 `ShardLocator` 查询该记忆在 KV 中的分片并跟随，不自行按规则判定。

| 端口 | 分片方式 | 指定分片检索 |
|---|---|---|
| KV 真源 | 物理拆表 | 只查选中分片的表 |
| 向量（`default` / `layers_l0` / `layers_l1`） | 物理拆表 | 只查选中分片的表 |
| 全文（`default` / `layers_l0` / `layers_l1`） | 物理拆索引 | 只查选中分片的索引 |
| 实体 | 可配置：`shared`（单索引，storage 给关联打分片标签）/ `split`（每分片一个实体索引） | `shared` 下推分片过滤；`split` 只查选中分片的索引 |
| 图 | 可配置：`shared`（单图，storage 给节点打分片标签）/ `split`（每分片一个图存储） | `shared` 节点过滤下推、过滤后截 limit；`split` 只查选中分片的图 |

真源保持 bytea 模型；不新增业务类型；实体、图各有一处契约补齐（D9 / D10）。

### D1. 分片完全配置驱动

- 不引入 `MemoryDomain` 等公共业务类型；分片名、物理表名只出现在配置里。
- 每张物理表 / 索引对应一个普通 Store 实例，后端、连接、加密（内层套 `encrypted`）各自独立。
- 库代码只实现「按规则把记录分发到某个分片」，不识别任何具体分片。

### D2. 分片规则契约（`DomainResolver`）

- **契约与注册**：`DomainResolver` 是 ABC，经 `DomainResolverProducer`（顶层段 `domain_resolver`）注册与装配；默认实现 target `rule_based`，可替换。
- **分片集合的唯一权威来源**：`domain_resolver` 的 `shards` 参数显式列出全部分片名。规则里的分片名、`default_shard`、`shard_from` 运行时取到的值都必须属于该集合；各端口包装层只负责为该集合中的每个分片提供存储（`table_template` 按它展开）。只供显式指定的分片也要列在这里，即使它不出现在任何 `match` 规则中。
- **只在 KV 创建记忆时求值**：分片规则只由 KV 包装层在 `insert` 新记忆时使用，对 `MemoryUnit` 求值，求值语义复用 `list` / `search` 现有的 `matches_memory_unit`，值规范化与过滤同源。向量、全文、实体、图不对规则求值，而是经 `ShardLocator` 跟随 KV（D6），因此不存在「KV 与索引两侧各自求值、结果不一致」的问题，也不需要把索引投影下沉为公共函数。
- **纯函数**：结果只由「创建时的记忆内容 + 启动时冻结的配置」决定，不依赖历史状态。
- **规则写法**（按顺序首条命中，都不命中落 `default_shard`）：
  - `match`：`FilterExpr` 条件语义，**仅允许引用受支持的路由字段**（D4）。
  - `shard_from: <字段>`：字段值即分片名。
  - 两种写法是有意保留的：`shard_from` 对应「直接指定」，`match` 对应「谓词分类」。只用 `match` 的话，显式指定要为每个分片各写一条规则，新增分片也得改规则。
- **值规范化**：
  - 字段缺失、`None`、空字符串视为「未命中」。
  - 枚举按其 `value` 字符串比较（如 `tier` 比较 `"core"`）。
  - 数组字段只能用 `contains` / `in` 类条件；`shard_from` 不允许指向数组字段（启动校验）。
  - `shard_from` 取到非字符串值，或字符串不是已配置分片名时，按 `on_unknown` 处理：默认 `reject`（拒绝写入，抛 `ValidationError`），可选 `default`（落 `default_shard` 并告警）。
  - 嵌套 metadata 用扁平路径引用（如 `system_metadata.shard`）。

### D3. 两种分片需求都只靠配置满足

**需求 A：按 MemoryUnit 字段分片（如 LLM 判定的 `tier`）**

分片在记忆创建、写入 storage 那一刻判定，此时抽取 / 分类已把 `tier` 填好；规则只看创建时的值，不关心是谁填的。之后 `tier` 被修改（LLM 重新判定或 API update），更新照常生效，**记忆留在原分片**（D5）。

```yaml
rules:
  - { shard: working,  match: { tier: working } }
  - { shard: profile,  match: { tier: core } }
  - { shard: episodic, match: { tier: episodic } }
```

本特性只保证「按最终 `tier` 路由」，**不保证 LLM 正确填写 `tier`**。默认值、解析失败、非法枚举的处理属于抽取 / 分类能力，由其自身验证；分片规则对任意合法 `tier` 值都有确定落点（未命中落 `default_shard`）。

**需求 B：用户直接指定分片**

```python
api.add("下周三交付评审稿", scope, security=sec, system_metadata={"shard": "working"})
```

```yaml
rules:
  - { shard_from: system_metadata.shard }        # 用户指定优先
  - { shard: profile,  match: { tier: core } }   # 未指定再按 tier
default_shard: main
```

- 规则有顺序：**用户指定 → 字段规则 → `default_shard`**。
- 自定义键（键名由部署方定）不在 `KERNEL_SYSTEM_METADATA_KEYS` 内，调用方可以写。
- 一次 `add` 被 LLM 拆成多条记忆时，派生记忆经 `inherited_system_metadata` 继承原始输入的 `system_metadata`，都进同一分片。
- **显式分片键只在创建时生效**：创建后该键被修改或删除，更新照常生效，记忆留在原分片，与 `tier` 同样是粘性归属（D5）。不拒绝这类修改，是因为系统自身会改动它：演进器 UPDATE 合并 metadata 时用 `inherited_system_metadata([原记忆, 候选])` 只保留取值相同的键，原记忆的分片键可能被删掉；若拒绝，演进流程会失败（见 R19）。

**显式指定的对外语义**：显式指定约束**本次写入的记忆及其单一来源的派生记忆**，**不**约束此后多源合并产生的新记忆。多源合并时 `inherited_system_metadata` 只保留取值相同的键，来源指定了不同分片时，新记忆按自身字段规则或 `default_shard` 落位。该语义写入 S02。

**系统生成的层级摘要**：层级摘要（scene / event / time 管线生成）是独立的记忆，按自身字段落位，可能与其子记忆不在同一分片。语义：指定 `shards` 时只返回选中分片内的摘要；摘要展开到子记忆时，子记忆同样受 `shards` 约束，不在选中分片的子记忆被排除。

- **已核实**：展开由检索层 `default_expander.py` 经 `DomainStore.get(scope, unit_ids)` 按 id 点读子记忆，不经过召回通道，现状不会看 `shards`。
- **改法：新增方法，不改 `get` 的语义**。`DomainStore` 新增 `get_in_shards(scope, unit_ids, shards) -> ShardedGetResult`，结果分三类：
  - `found`：在 `shards` 中且可读的记忆；
  - `missing_ids`：不存在（无 `active` 归属或数据缺失）；
  - `excluded_ids`：存在但不在 `shards` 中。

  这个方法不因缺失抛 `NotFoundError`，单个 id 的结果不影响其它 id。现有 `get(scope, unit_ids)` 保持原样（缺失时的行为不变），未启用分片或未传 `shards` 时展开器继续用它。
- **展开器**：请求带 `shards` 时改调 `get_in_shards`，`missing_ids` 记 `missing_child`、`excluded_ids` 记新诊断码 `shard_excluded`（同样不回显内容、Scope 或真实存在性以外的信息）。这是检索层的一处显式修改，展开器只透传分片名，不理解分片规则。
- **未启用分片的 `DomainStore` 实现**：对 `get_in_shards` 返回「不支持」错误；请求带 `shards` 时按 D11 的「通道不支持即报错」处理。
- L0 / L1 是同一条记忆的分层索引，与该记忆同分片，不存在此问题。

### D4. 受支持的路由字段（启动校验）

- **允许**：`tier`、`tags`、`source`、`system_metadata.*`、`user_metadata.*`。
- **禁用**：`lifecycle`、`t_valid`、`t_invalid`、`t_ingest`、`t_event`、`t_message`、层级字段（`hierarchy_*` / `tree_*`）。这些字段在创建时多为默认值或系统盖章值（如 `lifecycle` 恒为 `ACTIVE`），之后又会被 supersede、软删除、时间补全、树结构调整频繁改写；按创建时的值分片既没有区分意义，又会让分片与字段的当前值长期不一致，容易误解，故禁用。
- 规则引用不在允许集合内的字段 → 启动失败。

### D5. 第一阶段：分片在创建时确定（粘性归属）

- **insert**：KV 包装层按规则确定分片并写入，此后这条记忆的分片固定。
- **update**：定位记忆当前所在的分片，**在该分片内原地更新**，不重新按规则判定。修改 `tier`、显式分片键等任何路由字段都照常生效，记忆留在原分片。
- **对外语义**：「记忆属于哪个分片」= 它**创建时**被放进哪个分片，不随字段的当前值变化。例如一条记忆创建时 `tier=episodic` 进了 episodic 分片，后来 `tier` 被改成 `core`，它仍在 episodic 分片，用 `shards=["profile"]` 查不到它。需要按**当前** `tier` 筛选时，使用现有的 `filters`（如 `tier IN ["core"]`，在召回前下推）。该语义写入 S02。
- **影响面**（已核实）：
  - 全仓库原地修改已有记忆 `tier` 的只有 API update（engine 的 `patch.tier`）。新记忆在首次写入前设置 `tier`；middle→long 任务生成新记忆并软删除原记忆。
  - **演进**：SUPERSEDE 写一条新记忆（按其自身 `tier` 落分片），旧记忆只改 `lifecycle`；UPDATE（`orchestrating_evolver`、`dynamic_evolver`）原地合并内容与 metadata，**不改 `tier`**；dreaming / 演进任务都委托给这两个演进器。因此演进不会出现「episodic 原地变成 core」。
  - 即使将来新增原地改路由字段的逻辑，粘性归属下也只会出现分片与字段当前值不一致，不会报错或丢数据。
  - **不一致的来源不止 `tier`**：分片可以按任意允许字段分（D4）。除了 API update 修改 `tier`、`tags`、metadata，演进 UPDATE 也会原地合并 `system_metadata` / `user_metadata`（取交集），从而改变按 metadata 分片的记忆的字段当前值。这些都只造成「分片与字段当前值不一致」，按 `tier` 分片时，系统自动产生的记忆不会出现这种情况。
- **收益**：每条记忆生命周期内分片固定，因此——
  - 无需迁移锁、迁移状态和副本裁决（跨分片唯一性由 D7 的归属记录保证，不是由「不搬迁」推出）；
  - 向量、全文、实体、图都跟随 KV 的归属，`tier` 改了也不会把索引写到别的分片；
  - 实体关联、图节点的分片标签写入后不会过期，无需变更通知；
  - 返回结果属于目标分片，以返回前读取的生效归属为准（D11）。
- **规则变更与重新分区分开**：修改规则只影响此后新建的记忆，已有记忆的归属不变，无需迁移。把已有记忆移到别的分片（例如把 `tier` 已被修改的记忆归位）是显式的「重新分区」操作，在维护窗口内执行，先预览影响范围（「配置与使用」）。

### D6. storage 内部协作契约

向量、全文、实体、图包装层都需要知道「某条记忆在哪个分片」。为避免依赖具体 KV 包装类型或调用未声明的方法，定义两个 storage 内部的小契约，通过注入与能力声明使用：

| 契约 | 职责 | 提供方 | 使用方 |
|---|---|---|---|
| `DomainResolver` | 新记忆内容 → 分片名 | `domain_resolver` 实例 | KV 包装层（仅 `insert`） |
| `ShardLocator` | 批量 `(scope, [unit_id])` → **部分结果**：`{unit_id: 分片}` 只含存在 `active` 归属的 id，其余 id 归入「未定位」集合；单个 id 缺失不使整批失败 | KV 包装层实现并声明能力 | 向量、全文、实体、图包装层；`CompositeDomainStore` |
| `ConditionalKV`（能力） | 原子创建、按完整值的条件替换 / 条件删除、**批量部分读取**（返回存在的 key 及其值，缺失的不报错）、存储侧当前时间 | 归属存储声明 | KV 包装层、定位组件 |

- 向量 / 全文 / 实体 / 图包装层通过具名引用（`locator`）注入一个声明了 `ShardLocator` 能力的实例；启动时校验能力，缺失即失败。不做具体类型判断，不穿透加密或授权代理。
- `ShardLocator` 实现为独立的内部组件，**只读取 D7 的归属记录**。批量读取用 `ConditionalKV` 的部分读取，不用 `KVStore.mget`——`mget` 的约定是任一缺失即整批报错，不适合召回过滤这种「部分候选可能已删除」的场景。KV 包装层只负责路由与读写，对外的 `ShardLocator` 能力委托给该组件。
- **调用方如何处理「未定位」**：
  - 派生存储写入：该条记录失败，按既有 partial failure 语义返回，不会落到默认分片；
  - 召回结果校验（D11）：丢弃该候选，其余候选照常保留；
  - 实体 `find_by_linked_memory_id`：改为扇出查询（D9）。
- **前提：先写 KV，再写派生存储**。已核实两条写入路径都满足：`HybridIndexBuilder` 先写正排 KV 再写全文 / 向量 / 实体；`UnifiedIndexBuilder` 先经 `DomainStore.add` 写 KV 再写实体。
- **不做定位缓存**：同一 id 可以在删除后重新插入（D7），缓存在多实例下无法保证正确，TTL 只能缩短陈旧时间。派生存储写入时每次读归属，按批合并成一次部分读取。
- 第一阶段分片固定，标签写入后不会过期，**不需要变更通知接口（`ShardChangeObserver` 一类）**。第二阶段的迁移协调接口另行定义（见「第二阶段」）。
- 这两个契约属于 storage 内部，不对构建层、检索层暴露；写入 S06（实现 PR 时）。

### D7. KV 包装层与归属记录

- **路由**：只有 `/memory/` 记录按规则选分片；其余 key 写 `passthrough`。

**归属记录（跨分片唯一性、可见性与定位的唯一依据）**

- **记录内容**：每条记忆在归属存储中有一条记录，key 为 `(scope, unit_id)`，值为 `{分片 S, 状态, 操作 token, 创建时间}`。
  - 状态取值为 `pending`（插入进行中）、`active`（已生效）、`cleaning`（删除或对账清理进行中）；
  - **操作 token** 是每次创建归属时生成的随机值（UUID4），用来识别「这条归属是不是我创建的」；它只存在于归属记录中，不扩散到数据 key 和索引；
  - 创建时间由存储侧时间写入（PG `clock_timestamp()`、Redis `TIME`），只用于对账判断「是否超期」，不用于识别操作（时间精度可能使两次创建得到相同的值）。
- **数据 key 不变**：记忆数据仍写在分片 S 表中的 `/memory/{unit_id}`，索引记录也不带任何归属信息。
- **可见性规则**：一条记忆可见，当且仅当其归属为 `active`，且分片 S 中存在 `/memory/{unit_id}`。分片表中没有 `active` 归属指向的数据是「无主数据」，任何读取都看不到，由对账清理。
- **归属存储**：经 `placement_store` 参数注入，须声明 `ConditionalKV` 能力（D6），启动校验。
  - PG 用 `INSERT ... ON CONFLICT` 与条件 `UPDATE` / `DELETE ... WHERE value = 期望值`；Redis 用 `SET NX` 与 Lua 脚本；内存实现在单把锁内完成。
  - 默认是**专用内联表**（不成为端口，见 D13），不与 `passthrough` 混放。
  - 条件替换 / 删除比较的是整条归属值，其中操作 token 保证不同的创建尝试一定可以区分。

**同一 id 的协调边界：归属记录是互斥位，但只能约束「仍持有归属」的操作**

协调范围限于 **insert 与清理操作（delete、对账）**：归属记录存在期间（任何状态），持有它的操作之外，没有 insert 或清理操作能**开始**为该 id 写入或删除数据。update 不参与这一协调（约束 7）。归属在归属存储里，数据在分片表里，两处之间有时间窗口：**一个已经失去归属的在途 insert（变慢的 insert），仍可能晚到地写入数据**。协议不阻止这种晚到写入，只保证它既不能提交归属、也不能覆盖已有数据。具体约束：

1. **归属只能原子创建**：同 id 同时只能存在一条归属，持有归属的 insert 最多一个。
2. **数据只用原子创建写入，不覆写**：insert 第 ③ 步对分片表执行 `insert`，遇到已有数据即中止。这样晚到的旧 insert 无法覆盖新内容；反过来，旧 insert 晚到写入的数据会挡住新 insert（见下表的可用性限制）。
3. **删除先占位、再删数据、最后删归属**：delete 与对账都先持有 `cleaning` 归属，在持有期间删除数据，最后按自己的 token 条件删除归属。删除数据期间归属一直存在，同 id 的新 insert 无法开始，所以删除只会作用于本次要删除的那一代数据。
4. **`cleaning` 只能由创建它的操作完成，不在线接管**：若允许别的进程接管遗留的 `cleaning`，原清理者若只是变慢，恢复后迟到的数据删除会删掉接管完成后新插入的数据（且这次插入已成功）。仅在最后删除归属时比较 token，挡不住这次数据删除。因此：
   - 遗留的 `cleaning`（创建者崩溃）保持原样：该 id 读取为 `NotFoundError`（等同已删除），同 id insert 为 `ConflictError`；
   - 只在**维护窗口内、排空全部在途请求与后台任务后**由对账完成——此时可以确定原清理者已不存在。
5. **超期的 `pending` 由对账在线清理**：对账把它改为自己的 `cleaning` 后删数据、删归属。被清理的 insert 若仍在途，只可能晚到写入数据（成为无主数据），不会删除任何东西；它的提交因 token 不匹配而失败。
6. **对账清理无主数据**：先原子创建自己的 `cleaning` 归属，持有期间删数据，再删归属。占位成功**只说明此刻没有任何归属**，不说明没有在途的旧 insert；旧 insert 仍可能在之后晚到写入，形成新的无主数据。
7. **update 不参与协调**：update 读到 `active` 后直接对分片表执行 `update`，与现有 `KVStore.update` 一样是按 key 的盲写，不带版本。推演见下表：分片不会引入单表没有的结果。
8. **没有锁**：协调全部是存储侧的原子条件操作，不存在锁过期、旧持锁者继续写入的问题。

**各操作的状态转换**

| 操作 | 步骤 | 并发与失败时的结果 |
|---|---|---|
| `insert` | ① 按规则算出分片 S，生成操作 token t；② 原子创建归属 `v = {S, pending, t, 创建时间}`；③ 在 S 中对 `/memory/{unit_id}` 执行 `insert`；④ 把归属从 `v` 条件替换为 `{S, active, t, 创建时间}`，成功才返回成功 | ② 时归属已存在（任何状态）→ `ConflictError`。③ 时分片中已有同 key 数据 → 条件删除自己的 `v` 后返回 `ConflictError`，残留数据留给对账。④ 失败（归属已被删除或清理）→ insert 返回失败，③ 写入的数据成为无主数据 |
| `update` | 读归属，须为 `active` 的 S；对 S 中 `/memory/{unit_id}` 执行 `update` | 归属不存在或不是 `active` → `NotFoundError`；数据已被并发删除 → 内层 `update` 抛 `NotFoundError` |
| `delete` | ① 读归属值 `v`；② 条件替换为自己的 `v' = {S, cleaning, t', 创建时间}`；③ 删除 S 中的数据；④ 按 `v'` 条件删除归属 | 归属不存在 → 视为已删除（幂等）。② 失败（期间被提交）→ 重读后重试；读到的是别人的 `cleaning` → 视为删除已在进行，返回成功。对 `pending` 归属同样生效，进行中的 insert 会在第 ④ 步失败。③④ 之前崩溃留下 `cleaning`，按约束 4 处理 |
| `get` / `exists` / `mget` | 读归属，为 `active` 才读 S 中的数据 | `pending`、`cleaning`、不存在、数据缺失都返回 `NotFoundError` / `False`；`mget` 保持原语义（任一缺失整批 `NotFoundError`） |

**协议保证与不保证**

- **保证**：
  - 同一 id 同时至多一次 insert 成功；
  - insert 返回成功的记忆，在被删除前一直可见，且内容是该次 insert 写入的内容，或其后某次 update 的内容；
  - delete 返回成功后该记忆不可见；
  - 失效的 insert 既不能提交归属，也不能覆盖已有数据，它晚到写入的内容不会变得可见（但可能阻塞后续插入，见下）；
  - update 保持现有按 key 盲写的语义，**不保证拒绝跨「删除重建」的陈旧更新**：旧 update 可能把陈旧内容写进同 id 的新记录并使其可见，这与现有单表 KV 相同（见下方交错表）。
- **不保证（第一阶段可用性限制）**：
  - 失效的在途 insert 晚到写入数据后，同 id 的下一次 insert 会因数据位置被占而失败，直到对账清理了这份无主数据；
  - 若失效请求反复出现，不保证在有限次数内重试成功。系统中 `/memory/` 的 id 是系统生成的 UUID，同 id 再次插入只出现在 schema update 等少数路径，影响面有限，已写入已知遗留与 S02；
  - 遗留的 `cleaning` 只在维护窗口内清理，在此之前该 id 无法重新插入。

**关键交错**

| 交错 | 结果 |
|---|---|
| 并发 insert 同一 id | 只有一个能在 ② 创建归属，另一个 `ConflictError`。赢家若之后在 ④ 失败，两者都失败、可重试 |
| insert（已创建 `pending`）与 delete | delete 持有 `cleaning` 并删除数据；insert 在 ④ 条件替换失败，**返回失败**。它若晚于 delete 的 ③ 写入数据，成为无主数据。不会出现「insert 返回成功、读不到」 |
| delete 与同 id 重新插入（schema update 路径） | delete 持有 `cleaning` 期间，重新插入得到 `ConflictError`；delete 完成后重新插入才能开始，此时 delete 已不会再动数据 |
| 失效的旧 insert A 与新 insert B（A 的 `pending` 已被对账清理） | A 晚到写入数据成功、B 在 ③ 因数据已存在而中止，或 B 先写入、A 在 ③ 中止。最坏情况两者都失败、旧内容成为无主数据，**旧内容不可见**；对账清理后 B 可重试（可用性限制） |
| 对账清理无主数据与新 insert | 对账持有 `cleaning` 期间新 insert 得 `ConflictError`；新 insert 已持有归属时对账跳过。对账不会删掉已持有归属的新 insert 的数据 |
| 原清理者变慢、遗留 `cleaning` | 在线不接管，所以不存在「迟到的数据删除删掉新插入的数据」。该 id 在维护窗口清理前保持「已删除、不可重新插入」 |
| 旧 update 与「删除后同 id 重新插入」 | 旧 update 若在删除的 ③ 之后执行：新记忆在别的分片 → 旧分片无数据，内层 `update` 抛 `NotFoundError`，等价于排在删除之后、重新插入之前；新记忆在同一分片且已写入 → 覆写新内容，等价于排在重新插入之后的一次 update。两种结果都能排成合法的串行顺序，与现在单表 KV 的盲写 `update` 相同；调用方读改写带来的陈旧覆盖是现状已有行为，本方案不改变 |
| 旧 update 与进行中的 delete | update 读到 `active` 后、delete 的 ③ 之前写入 → 随后被删除；在 ③ 之后写入 → `NotFoundError`。都等价于 update 排在 delete 之前或之后 |
| 多实例、进程崩溃 | 崩溃只会留下 `pending`、`cleaning` 归属或无主数据：都不可见，只阻止同 id 的新插入，分别由在线对账（`pending`、无主数据）或维护窗口（`cleaning`）清理 |

**维护窗口的适用范围**：用于首次分区、重新分区，以及清理遗留的 `cleaning`。正常运行中的 insert / update / delete，包括 schema update 以原 id 重新插入的路径，都按上面的规则执行，不依赖维护窗口。

**重试与 id 复用**

- `insert` 与现有 `KVStore` 契约一致，**不是幂等的**：响应丢失后重试，若首次已成功则返回 `ConflictError`。需要幂等的调用方沿用现有做法（先 get 再判断）。
- **删除完成后，允许用同一 id 重新插入**（已核实 `schema_update.py` 在 KV 中查不到时会以原 id 重新插入），可能落到不同分片。

**其它**

- **点读可用性**：`get` 依赖归属存储与目标分片，不依赖其它分片。
- **list**：扫描选中分片各表的 `/memory/`，对扫到的 id 批量部分读取归属，只保留「归属 `active` 且指向本分片」的条目，再复用 `list_memory_entries` 做过滤、计数、稳定排序、分页，语义与单表一致。第一阶段定位为**兼容实现**：成本随选中分片数据总量线性增长，外加一次批量归属读取；精确 count 需全量扫描；分页期间并发修改的语义与现状相同。
- **对账**：在线清理超期 `pending` 与无主数据（约束 5、6），维护窗口内清理遗留 `cleaning`（约束 4），并清理派生存储中归属分片与所在表不一致的记录（D8）。对账只做清理，不是正确性前提——可见性在任何时刻都由归属记录决定。
- **可选能力**：`persistent_atomic_records` / `claim_single_writer` 委托给 `passthrough`（管理类 key 都落在这里）。

**KV / 向量 / 全文的 CRUD 契约**（包装层对外保持原 ABC 语义）：

| 操作 | KV | 向量 / 全文 |
|---|---|---|
| insert | 见上表；同 key 已有归属（任何分片、任何状态）→ `ConflictError` | 校验记录 id 与 `unit_id` 的绑定（D8）；按 `active` 归属写入分片 S；**不**到其它分片删除同 id 的记录（见 D8「旧记录残留」）；未定位 → 该记录失败 |
| update | 写 `active` 归属的分片；不存在 → `NotFoundError` | 同 insert，不按 metadata 换分片 |
| delete | 见上表，幂等 | 按 id 在各分片幂等删除 |
| get | 只读 `active` 归属指向的分片 | 按 id 扇出到各分片（记录 id 不能可靠反解出 unit_id）；缺失语义同内层实现 |
| 批量 | `mget` 任一缺失 `NotFoundError`（原语义） | 按分片分组写入；某组失败时成功组不回滚，返回失败记录列表；重试按 id 幂等 |
| 绕过路由 | 分片表与归属存储默认内联，不成为端口（D13） | 同左 |

### D8. 向量 / 全文包装层

- **写入**：从记录 metadata 取 `unit_id`，经 `ShardLocator` 查出该记忆 `active` 归属的分片，写对应表 / 索引；不按记录 metadata 里的 `tier` 自行判定，所以 `tier` 被修改后索引仍写回原分片，与 KV 一致。`delete` 按 id 到各分片幂等删除。
- **记录 id 与 unit_id 的绑定（契约，写入 S06）**：索引记录 id 必须由其 `metadata.unit_id` 派生——等于 `unit_id`，或以 `unit_id` 加分隔符（`-` / `:`）开头。包装层写入时校验，不满足即拒绝该记录。
  - 现有构建器全部满足：全文 L2 用 `unit.id`，向量分块用 `{unit_id}-{chunk_id}`，分层用 `{unit_id}-layer-l0` / `{unit_id}:{layer}`。
  - **已核实的 id 生成路径**：`/memory/` 记忆 id 是 `str(uuid.uuid4())`（36 位字符串），见 `simple_ingestor`、`text_source`、scene / event / time 层级管线、engine 的 SUPERSEDE 与 `schema_update`。在这一前提下，任一记忆 id 都不会是另一记忆 id 加分隔符后的前缀，所以同一记录 id 只能属于同一条记忆，进而只落在同一个分片，跨分片的同 id 冲突不会因路由产生。
  - 若部署经其它路径写入非 UUID 形式的记忆 id，前缀可能二义，上述唯一性论证不成立，列入已知遗留。
- **旧记录残留**：派生写入已定位到分片 S，但该记忆随后被删除、并以同 id 重新插入到分片 S'，S 中会残留旧记录。
  - **写入路径不做跨分片清理**：若在写入时「到其它分片删除同 id 的记录」，一个暂停后恢复的旧索引任务会删掉另一分片中刚建好的有效索引（旧任务定位到 S 后暂停 → 记忆被删除并在 S' 重建、建好 S' 的新索引 → 旧任务恢复、写 S 后清理其它分片，删掉 S' 的有效索引）。这是包装层新增的盲删，单索引中没有，所以第一阶段去掉。
  - **按 id 的 `delete` 仍扇出到各分片**：它等价于单索引中按 id 删除，语义不变。
  - **残留只在维护窗口清理**：在线对账若「先查归属、再按结果删除」，查与删之间同 id 可能被重建到被删的分片，仍会误删有效索引；派生存储又没有可用于协调的条件删除。所以残留索引只在维护窗口、排空在途请求与任务后，由对账比对「记录所在分片」与「该 unit_id 的 `active` 归属分片」后清理。
  - **残留的在线影响**：指定的 `shards` 不含该记忆当前分片时，残留候选在真源校验时被丢弃；查全部分片、或指定的 `shards` 同时包含两个分片时，残留候选能通过校验，按旧内容的向量 / 文本参与排序，但返回的是当前记忆（按 unit_id 去重，内容回读自真源，见下条）。只影响相关性，不越过分片边界，列入已知遗留。
- **返回内容必须来自当前真源（验收条件）**：索引只用于召回与排序，不作为返回内容的来源。三条检索路径都必须在返回前按 unit_id 回读当前真源 `MemoryUnit`，用真源内容组装结果，不得把索引记录携带的正文、摘要或 metadata 直接当作返回内容。归属校验只能证明记忆所在的位置，不能证明内容最新，所以这一条单独验收。
  - 现状已核实满足：`RECALL_GET_RANK` 经 `pipeline_retriever._materialize_recalled` 调 `DomainStore.get`；`RECALL_AND_GET_RANK` 与 `RETRIEVE` 经 `CompositeDomainStore._get_units` 读真源。
  - 实现分片时不得改变这一点，例如不得为省一次点读而直接使用向量 `recall()` 回带的 metadata 作为结果。
- **检索**：带 `shards` 时只查选中分片；不带时查全部，各分片各取 top_k 后合并截断。
- **向量分数可比的启动校验**：各分片的 embedding 模型配置、维度、距离度量、归一化方式、`score_higher_is_better()` 一致；不一致则启动失败。精确性：各分片的局部 top-k 合并在「同一排序尺度、局部结果精确」时等价于全局 top-k；ANN 后端本身是近似的，合并后与单索引 ANN 同样是近似，不承诺全局精确。
- **全文排序（产品语义）**：不带 `shards` 查全部是默认行为，BM25 受各索引 IDF 影响，合并后与单索引排序**近似而非等价**。
  - 各分片索引可由同一次原生查询覆盖时（如位于同一 ES 集群），通过**可选能力**表达：内层全文存储声明「可与哪些实例合并查询」的能力及合并查询方法，包装层只在所有选中分片的内层都声明且相互兼容时使用，否则按分数合并。包装层不判断内层是否为 ES，也不访问其内部客户端。
  - 异构后端时按分数近似合并。
  - 验收标准：在评测集上，全部分片检索与分片前单索引的 top-k 召回率差异不超过约定阈值（数值在实现前与业务方确认）。
- **可选能力**：`recall`（召回同时回带 metadata）在所有内层都支持时扇出，否则抛 `NotImplementedError`。

### D9. 实体包装层：`shared` / `split` 两种模式

实体包装层的参数 `mode` 二选一，构建层发出的指令与看到的 `EntityRecord` 在两种模式下完全一致。

**新增与移除关联对定位的要求不同**：

- **新增关联（`INSERT` / `LINK`）**：必须定位到该记忆生效的归属，定位不到则该条操作失败。
- **移除关联（`UNLINK`）**：**不依赖定位**，归属已被删除也能幂等完成。记忆删除时，`HybridIndexBuilder.remove` 的顺序是全文 → 向量 → 实体 → 正排 KV，解除关联时归属通常还在，但并发删除或清理悬空关联时可能已不存在。
  - `shared` 模式：从记录的所有分组中移除该 id；
  - `split` 模式：扇出到全部分片，按 `linked_memory_ids` 反查后逐个移除。

**`EntityStore` 契约补齐**（两种模式共用，是公共契约变更，不是机械替换）：

- 所有方法首参由 `space_id: str` 改为 `scope: Scope`。S06 规定 Store 方法首参是 scope，实体存储是现有唯一例外。
- `find_by_entity_text_hash` / `search` 增加可选参数 `shards: frozenset[str] | None`：`None` 表示全部分片；不支持分片的实现收到非空 `shards` 时抛「不支持」错误。
- **新增两个增量操作**：
  - `UNLINK(record_id, unlink_memory_ids)`：从记录中移除这些关联，**从不删除记录**；移除后为空的记录保留为「空记录」，查询（`find_*`、`search`）不返回空记录。
  - `DELETE_IF_EMPTY(record_id)`：仅当记录的关联为空时原子删除它，否则不做任何事。
  - **已核实的现有问题**：构建层解除关联时「读出完整关联 → 去掉一条 → `UNLINK_UPDATE` 整体替换，为空则 `DELETE`」（`entity_index_builder.py`）。这在不分片时就会与并发 `LINK` 互相覆盖。
  - 构建层改为：剩余关联非空时**只发 `UNLINK`**（只需 UPDATE）；只有读到的剩余关联为空时，才在同一批次追加 `DELETE_IF_EMPTY`（此时才需要 DELETE）。不会每次 `UNLINK` 都附带 `DELETE_IF_EMPTY`。若并发 `LINK` 使记录在此期间变为非空，`DELETE_IF_EMPTY` 不生效，记录保留；若并发 `UNLINK` 使记录变空而本次未发 `DELETE_IF_EMPTY`，空记录由对账清理。这是构建层的一处修改，不涉及分片。
  - `UNLINK_UPDATE` 保留一个版本用于兼容，标记废弃。
- **`DELETE_IF_EMPTY` 必须在存储内原子判空并删除**：不能先查询为空、再单独发删除。否则查询之后并发 `LINK` 新增的关联会随记录一起被删掉。ES 在 painless 脚本内判空后 `ctx.op = 'delete'`，否则 `ctx.op = 'noop'`；内存实现在单条记录锁内完成。
- **`UNLINK` 与 `DELETE_IF_EMPTY` 的提交与授权**：
  - 构建层把两者放进**同一次** `execute_operations` 批次；
  - 实体授权代理在执行前，对批次内每条操作推导出的动作逐一授权，全部通过才执行（已核实，`composite_store_manager.py` 的 `authorized()` 先授权、后调用）。任一动作被拒，整个批次不执行，返回授权错误，**不会出现「解除已成功、清理因权限失败」**。
  - 新操作类型必须加入动作映射，否则会落到 ADMIN（见下方授权一节）。
  - 执行阶段的非授权失败按条返回：`UNLINK` 成功而 `DELETE_IF_EMPTY` 失败时，结果如实报告为部分完成（计入现有 `EntityLinkResult.failed_count`），空记录不被查询返回，由对账清理。
- **原子性是能力声明，不是默认承诺**：实现须声明 `atomic_unlink`。
  - 声明了的实现，必须保证 `UNLINK` 的增量移除与 `DELETE_IF_EMPTY` 的「判空 + 删除」各自在单个原子操作内完成：ES 用 painless 脚本（`DELETE_IF_EMPTY` 在脚本中判空后 `ctx.op = 'delete'`，否则 `ctx.op = 'noop'`），内存实现在单条记录锁内完成。
  - 旧签名适配器只能用「读出 → 修改 → 写回」「读出为空 → 删除」模拟这两个操作，存在与现状相同的竞态（可能删掉并发 `LINK` 刚加入的关联），所以声明 `atomic_unlink = False`，不宣称原子，只允许在未分片的部署中使用。
  - `domain_routed` 实体包装层（两种模式）要求内层声明 `atomic_unlink = True`，否则启动失败。
- **分组关联能力（可选能力，S09 #6 / #7）**：`LINK` / `UNLINK` / `INSERT` 的每个关联 id 可携带所属分片；声明该能力的实现须按分片分组保存关联，并在 `shards` 过滤时只返回选中分片的关联。ES 与内存实现各自履约（物理结构见下）。`shared` 模式要求 `inner` 声明该能力，启动时校验；包装层只通过契约调用，不判断内层类型、不访问 ES 客户端。
- 后端内部仍用 `space_id_from_scope(scope)` 计算 ES routing，物理路由不变。
- **授权代理：保留按操作授权，只简化 Scope 提取**：现有实体专用代理有两项职责（`composite_store_manager.py:100-108`），改造后只简化前一项。
  - **还原 Scope（简化）**：目前只能从 `space_id` 与 `filters.actor_id` 有损近似 Scope，交给 `StorageSecurity.authorize` 的 `space` 可能其实是 org id 或字面量 `"default"`，写入侧的 `user` 恒为空。改为直接取首参的真实 Scope。
  - **按批量中每条操作推导授权动作（保留）**：映射保持不变——`INSERT`→ADD、`LINK`→UPDATE、`UNLINK_UPDATE`→UPDATE、`DELETE`→DELETE、`find_*`→SEARCH。新增 **`UNLINK` → UPDATE**、**`DELETE_IF_EMPTY` → DELETE**。
  - **与现状的权限对比（兼容说明）**：
    - 解除非最后一个关联：现状发 `UNLINK_UPDATE`（UPDATE），新流程发 `UNLINK`（UPDATE），**不变**；
    - 解除最后一个关联：现状只发 `DELETE`（DELETE），新流程发 `UNLINK` + `DELETE_IF_EMPTY`（UPDATE + DELETE），**收紧**——只持有 DELETE、不持有 UPDATE 的主体会被拒绝；
    - 收紧的原因：现状的 `DELETE` 在并发 `LINK` 时会把刚加入的关联一起删掉，新流程用增量移除避免这一点，因此需要 UPDATE；
    - 发布说明须列出这一变化。实体索引由构建层以记忆写入者身份维护，正常部署中该主体同时持有 UPDATE 与 DELETE；部署方若有只授 DELETE 的自定义策略，需在升级前补授 UPDATE。
  - **不能改用通用代理**：通用代理只按方法名推导动作，会丢掉批量操作的逐条授权。
  - **授权会更精确**：写入侧开始带 user/agent/session，原先按近似 Scope 放行的请求可能被拒绝。须列出授权语义差异，并补安全测试（授权拒绝、跨 Scope 访问、批量中混合操作的逐条授权、只持有 UPDATE 时可解除非最后一个关联、解除最后一个关联须同时有 UPDATE 与 DELETE、只持有 DELETE 时解除最后一个关联被拒并给出明确错误）。
- **兼容策略（S09 #14）**：
  - 提供一个版本的旧签名适配器：把旧签名 `space_id` 实现包装成新契约，`shards` 非空时报不支持，`UNLINK` / `DELETE_IF_EMPTY` 用非原子模拟并声明 `atomic_unlink = False`；
  - 适配器使用时打废弃告警，下一个大版本移除；
  - 第三方实现须在废弃期内迁到新签名。
- 该契约在实现 PR 中先写入 S06（与代码同一提交，符合「specs 描述当前状态」），本节为完整定义。

**`shared` 模式：单索引，关联按分片分组**

- **物理文档结构**（ES 实现履约分组能力的方式，storage 内部，不改变对外的 `EntityRecord`）：

  | 字段 | ES mapping | 含义 |
  |---|---|---|
  | `linked_memory_ids` | `keyword` | 全部关联的并集，保留供 `find_by_linked_memory_id` 反查与旧数据兼容 |
  | `linked_shards` | `keyword` | 至少有一个关联的分片名列表，供 term 过滤 |
  | `linked_by_shard` | `object`，按分片名动态生成 `keyword` 子字段 | 每个分片的关联 id 列表 |

  三个字段由同一个 painless 脚本在单文档内原子维护。内存实现用等价结构并在单条记录上加锁。
- **对外视图**：返回的 `EntityRecord.linked_memory_ids` = 选中分片（未指定时为全部分片）的分组并集。
- **各操作语义**（新增关联时，包装层经 `ShardLocator` 为每个关联 id 标注分片，再交给 `inner`；移除关联不需要定位）：
  - `INSERT`：带分片标注写入三个字段。
  - `LINK`：增量追加到对应分组。
  - `UNLINK`：从**所有分组**中增量移除该 id（不需要知道它属于哪个分片），单文档原子，不删除记录。并发 `LINK` / `UNLINK` 都是增量操作，不会互相覆盖。
  - `DELETE_IF_EMPTY`：单文档原子地判空并删除。
  - `UNLINK_UPDATE`（废弃，兼容用）：整体替换，与现状同样存在并发覆盖风险，不承诺不丢关联。
  - `DELETE`：按 id 删除，与现状一致。
  - `find_by_entity_text_hash(scope, hashes, filters, shards, limit)`：`shards` 非空时追加 `terms(linked_shards)` 过滤并下推；`limit` 按实体记录数计（不是记忆数）。
  - `find_by_linked_memory_id`：按 `linked_memory_ids` 反查，与分片无关，语义不变。
  - `search`（向量 kNN）：`shards` 非空时以 `terms(linked_shards)` 作为 kNN 前置过滤。
  - `ensure_index`：mapping 增加 `linked_shards` 与 `linked_by_shard`；已有索引由迁移脚本补齐字段并回填分组。
- **部分失败（S09 #12）**：`execute_operations` 按条返回结果的语义保持不变。某条操作里有 id 定位失败（KV 不可用或记忆不存在）时，**仅该条操作失败**，返回结构化错误，同批其它操作照常执行；包装层不会把一条操作拆成部分成功。

**`split` 模式：每个分片一个实体索引**

- 每个分片对应一个实体存储实例（经 `table_template` 展开，如 `memory_entities_{shard}`），每个分片内实体记录只关联本分片的记忆。
- **稳定的逻辑实体身份**：对外的 `EntityRecord.id` 是**逻辑 id**（即构建层 `INSERT` 时给出的 id），与是否分片无关。每个分片中的物理文档 id 由逻辑 id 确定性派生：`{逻辑 id}@{分片名}`，文档内同时保存逻辑 id。包装层负责一个逻辑实体在所有分片中的全部物理文档。
- **各操作语义**：
  - `INSERT(record)`：按定位结果把关联分组，每个有关联的分片以确定性物理 id **条件创建**一份文档（已存在则视为已完成），重试幂等。
  - `LINK(逻辑 id, ids)`：按分片分组，对每个分片的 `{逻辑 id}@{分片}` 做**增量追加，不存在则创建**（upsert，单文档原子）。实体文本、类型等字段取自该逻辑实体已有的任一物理文档。
  - `UNLINK(逻辑 id, ids)`：不经定位，在该逻辑实体的全部分片物理文档上增量移除这些 id（各文档内原子），不删除文档。
  - `DELETE_IF_EMPTY(逻辑 id)`：对该逻辑实体的每份物理文档各自原子判空删除；全部删完即逻辑实体消失。
  - `DELETE(逻辑 id)`：删除全部分片中的 `{逻辑 id}@*` 文档，幂等。
  - `UNLINK_UPDATE`（废弃，兼容用）：按新关联集合重新分组后逐分片整体替换，存在与现状相同的并发覆盖风险。
  - `find_by_entity_text_hash`：只查选中分片（未指定时查全部），**按逻辑 id 聚合**同一逻辑实体的物理文档，返回一条 `EntityRecord`（id = 逻辑 id，关联 = 选中分片的并集）。对它执行 `LINK` / `UNLINK` / `DELETE_IF_EMPTY` / `DELETE` 都作用于该逻辑实体的全部物理文档，与 `shared` 模式对调用方语义一致。
  - `find_by_linked_memory_id`：能定位到生效归属时只查该分片；定位不到（记忆已删除、正在清理悬空关联）时扇出到全部分片。结果按逻辑 id 返回。
  - `search`：只查选中分片，按逻辑 id 去重后按分数合并。
  - `ensure_index`：对每个分片的索引分别执行；任一分片失败则整体失败，并报告失败的分片。
- **并发去重的边界**：两个构建请求同时为同一实体文本各自 `INSERT`，会得到两个逻辑实体。这是现状「先按 hash 查、未命中再新建」的已有行为，分片不会加剧，本方案不解决。
- **部分失败**：同 `shared`，按条返回；一条操作涉及多个分片时，任一分片失败，该条报失败并附各分片结果。重试幂等：确定性 id 加条件创建 / upsert / 增量操作。
- **代价**：每个分片独立治理实体数据（容量、备份、索引参数）；同一逻辑实体在 N 个分片中有 N 份物理文档。

**两种模式共同点**：指定 `shards` 时，分片过滤在实体查询截断（`limit` 按实体记录数）之前生效，不点读真源；`keyword_recaller.py` 把 `query.extensions["shards"]` 显式传给实体查询（该算子属 storage 层）；分片在创建后固定，标签与分组写入后不会过期。

### D10. 图包装层：`shared` / `split` 两种模式

**节点识别（两种模式共用）**：图节点 id 不一定对应 unit_id。节点若代表某条记忆，其 `properties` 中带 `MemoryRef`（含 `scope` 与 `unit_id`）。把该属性键提升为 `common/type_def/continuity.py` 的公共常量并写进 S06；`evidence_graph` 已按此写入，构建层无需改动。用 `ref.scope` + `ref.unit_id` 经 `ShardLocator` 求分片——须用 `ref.scope`，跨 agent 的 continuity 关系可能引用其它 scope 的记忆。不带记忆引用的节点是「公共节点」。

**授权（两种模式共用）**：遍历范围与现状一致，只在本次查询的 scope 内进行；分片不扩大授权边界。`ref.scope` 与查询 scope 不同时，分片标签只用于过滤，不据此授予访问；最终读取真源仍走既有授权。

**`shared` 模式：单图，节点打标签，过滤下推**

- **写入**：带记忆引用的节点写入时把分片名放入节点属性；公共节点不打标签；边不打标签，跨分片关系完整保留。
- **检索**：`GraphQuery` 增加节点属性过滤字段，包装层把 `shards` 翻译成该过滤条件交给后端。后端可经过授权范围内的任意节点，**只返回选中分片的节点，`limit` 计过滤后返回的节点数**。深度、访问预算、超时与现状一致。指定 `shards` 时公共节点不返回；多个节点指向同一记忆时按 unit_id 去重。
- **后端可行性（已核实）**：现有三个图后端都是进程内广度优先遍历——先收集到达节点、再截 `limit`（`in_memory_graph_store.py`、`nano_graphrag_graph.py`），或遍历中达到 `limit` 即提前返回（`kv_graph_store.py`）。前两者在截取前按节点属性过滤即可；`kv_graph_store` 需在遍历中读取节点属性再计数，读放大发生在存储内部，不是召回后过滤。第三方图后端须实现该过滤字段，否则声明不支持分片（D12）。

**`split` 模式：每个分片一个图存储**

- 每个分片对应一个图存储实例（经 `table_template` 展开）。
- **写入路由**：
  - 带记忆引用的节点写入其所在分片的图；
  - 公共节点写入 `default_shard` 的图；
  - 边写入 `source` 节点所在分片的图；`target` 不在同一分片时，在该分片补一个**占位节点**：只含 id 与记忆引用，并标注 `placeholder=true` 和 target 的**真实所属分片**。占位节点只为让分片内能遍历到边的终点。
- **检索**：
  - 只在选中分片的图中遍历，未指定时扇出到全部分片。
  - **返回过滤与 `shared` 模式相同**：按节点的真实所属分片过滤，并在截取 `limit` 之前执行。占位节点的真实分片不在 `shards` 中时不返回，所以 `shards=["working"]` 不会经由 working 图里的占位节点返回 profile 的记忆。`split` 模式因此同样要求内层实现节点属性过滤（D12）。
  - 扇出合并时按节点 id 去重，**真实节点优先于占位节点**；`limit` 计合并后的返回节点数。
- **代价（显式语义）**：遍历不跨分片。经由其它分片节点才能到达的关系不会被找到，跨分片的多跳关系在 `split` 模式下丢失。需要完整跨分片关系的部署应选 `shared`。
- `seed_ids`（按词项找遍历起点）只在选中分片中查找。

分片在创建后固定，标签与节点归属写入后不会过期；离线迁移时全量扫描图节点重打标签或重新分配。

### D11. 按分片检索：`extensions["shards"]`

- **语义**：

  | 取值 | 行为 |
  |---|---|
  | 键不存在 | 查全部分片 |
  | `["working"]` / `["profile", "episodic"]` | 只查这些分片 |
  | `[]` / `None` / 含未配置的分片名 | storage 抛 `ValidationError` |
  | 按 id 的 `get` / `update` / `delete` | 忽略 `shards` |

- **透传**：`search` 的 API 边界只取走约定键，其余经查询解析透传，向量 / 全文 / 图召回算子复制进各自 `Query.extensions`；实体通道由 `keyword_recaller` 显式传入（D9）；`list` 的扩展参数原样到达 KV。
- **通道支持声明**：每个召回通道（包装层、召回算子）须声明是否支持分片。请求带 `shards` 而某启用通道不支持时，**报错而不是静默返回全部**。
- **一致性边界**：「属于某分片」指记忆**所在**的分片，即生效归属中的分片（创建时确定，D5），不是按字段当前值重新计算的结果。按字段当前值筛选请使用 `filters`。
  - **最终保证由真源校验给出**：指定 `shards` 时，`CompositeDomainStore` 在召回结果交给检索层之前，按候选的 unit_id 经 `ShardLocator` 批量部分读取 `active` 归属，丢弃不在 `shards` 中或未定位的候选，其余候选照常保留。层级展开的子记忆经 `DomainStore.get_in_shards` 同样按归属过滤（D3）。
  - **各通道的分片过滤是性能路径**：正常情况下，各通道的分片过滤已使候选都在目标分片内，真源校验不会丢弃任何候选。只有派生存储残留旧记录（D8，删除后同 id 重建到别的分片）等竞争情况下，校验才会丢弃候选——表现为该次召回少一条，结果不会越过分片边界。
  - 因此**返回结果中每条记忆都位于 `shards` 中的某个分片**，以返回前读取的生效归属为准。真源校验每次检索只做一次批量归属读取，不点读记忆内容。
- **内容来源不受 `shards` 约束**：`shards` 限定的是**返回记录所在的位置**，不保证内容来源全部来自这些分片。
  - 例如：层级摘要或演进合并后的记忆，可能综合了多个分片的记忆；它本身落在 working 分片时，`shards=["working"]` 会返回它，其内容仍可能含有 profile 来源的信息。
  - 如果业务要求「只利用指定分片的信息作答」，需要另外限制跨分片的摘要生成与演进合并，仅靠存储路由无法满足。该边界写入 S02。
- **各通道的保证与边界**：分片过滤都在该通道截断候选之前生效，但各通道原有的近似与预算边界不变：
  - 向量：ANN 近似，各分片局部 top-k 合并，不承诺全局精确（D8）；
  - 全文：跨分片 BM25 合并近似（D8）；
  - 实体：`limit` 按实体记录数计，不等于记忆数；
  - 图：深度、访问预算、超时会限制找到的节点；`split` 模式不保留跨分片多跳关系（D10）。
- **可用性**：指定分片检索时，向量 / 全文 / 实体 / 图只访问选中分片；回读真源依赖归属存储与目标分片（D7），不依赖其它分片。
- **安全**：`shards` 只能收窄，永远与授权、scope 过滤取 AND（S09 #11）。

### D12. 支持矩阵与装配校验

| 组合 | 第一阶段 | 装配行为 |
|---|---|---|
| KV / 向量 / 全文三层 的 `domain_routed` | 支持 | — |
| 实体 `domain_routed`，`mode: shared` / `split` | 支持 | `shared` 要求 `inner` 声明分组关联能力；`split` 要求每个分片都有实体存储 |
| 图 `domain_routed`，`mode: shared` / `split` | 支持 | 两种模式都要求 `inner` 实现节点属性过滤（截 limit 前）；`split` 另要求每个分片都有图存储 |
| 未配置实体或图通道 | 支持 | 无需对应包装层 |
| Document Mode（`write_document=true`，`shadow` 召回）+ 分片 | 不支持 | 启动失败 |
| FusionStore（当前不在召回链路）+ 分片 | 不支持 | 分片模式下若有消费方注入 FusionStore，启动失败 |
| 自定义 DomainStore / Recaller | 须声明分片支持 | 未声明时，请求带 `shards` 报错（D11） |
| 新增端口 | 仅限已有 Store 类型、已有 `domain_routed` 实现、记录 metadata 带 `unit_id`（供定位） | 其它情况启动失败 |

同时校验：

- 各端口为 `domain_resolver.shards` 中的每个分片都提供表（严格 1:1）；规则中的分片名与 `default_shard` 都属于 `shards`；
- 归属存储声明 `ConditionalKV` 能力（D6 / D7）；`domain_routed` 实体包装层的内层声明 `atomic_unlink = True`（D9）；
- 拓扑登记（「配置与使用」）：各分片物理标识与登记一致；登记中的分片都仍在 `shards` 中；规则不输出 `retiring` 分片；
- 向量分数可比条件（D8）；
- 物理资源命名（D13）。

### D13. 配置与装配

- 配置按 Producer 顶层段划分（`domain_resolver` / `kv_store` / `vector_store` / `fulltext_store` / `entity_store` / `graph_store`），依赖经 `Producer.dep` 引用；`resolver: default` 在 `domain_resolver` 命名空间按名查找。
- **`table_template` 经标准装配路径展开，且不进端口表**：对每个分片，把模板中的 `{shard}` 替换后，经对应 Producer 的标准 `build` 路径以**内联（匿名）实例**构建（同一 target 注册表、同一校验、同一生命周期登记），不是包装层私有的装配系统。
  - 只有 `kv_store` / `vector_store` 等命名空间下的**具名**声明才会被 `_collect_ports` 收成 `manager.xxx(name)` 端口；模板实例是内联的，不成为对外端口。这避免了几十张分片表都变成可被绕过路由直接访问的端口。
  - 模板实例归包装层所有，随包装层关闭。任一分片构建失败时，只清理本次新建的实例，装配失败。
  - `tables` 覆盖若引用具名实例，该实例会成为端口，可被绕过路由直接访问。因此**推荐 `tables` 覆盖也用内联声明**（与现有「加密 KV 的明文 raw 推荐 inline」同理）；引用具名实例时，该实例是借用的，包装层不关闭它。
- **物理粒度（两种都支持）**：
  - **同后端分表**（默认）：`table_template` 只变表名 / 索引名，所有分片共用一个数据库 / 集群。注意：**相同连接配置不等于共享连接池**——现有 `PgStoreBase`（`storage/_pg.py`）每个实例持有自己的连接池，N 个分片表就是 N 个池，总连接数 = 各实例 `pool_max_size` 之和。第一阶段按实例估算并在模板中调小每个分片表的 `pool_max_size`；把连接池抽成具名共享依赖（定义所有权与关闭顺序）列为后续演进。
  - **独立后端**：对需要独立数据库 / 集群的分片，用 `tables` 覆盖为不同连接参数（如不同 DSN、不同 ES 集群）。
- **物理资源命名校验（启动时）**：
  - 分片名须匹配 `^[a-z][a-z0-9_]{0,31}$`；
  - 分片名只决定在已配置集合中选哪一个，数据中的值从不直接拼进 SQL / 索引标识符；
  - 展开后的表 / 索引名满足后端的长度与字符限制；
  - 不同分片展开后不得指向同一张表或同一个索引，也不得与 `passthrough` 重名（规范化后比较）。
- **实体 / 图包装层**：`inner` 引用实际存储，`locator` 引用声明了 `ShardLocator` 的实例。
- **不想分片**：各端口 `default` 直接指向单后端，行为与现状一致；无 `enabled` 开关。
- **归属存储与内部数据的保护边界（S09 #4 / #11 / #13）**：
  - **默认内联专用表**（如 `agent_memory_shard_placement`），不成为端口，普通 KV 消费方无法经 `manager.kv(name)` 访问。
  - 若部署选择把归属放在 `passthrough` 等共用实例中：
    - 归属使用保留前缀 `/__shard_placement/`；
    - KV 包装层对外拒绝任何以该前缀为 key 的 `insert` / `update` / `delete` / `get`（`ValidationError`），并在 `scan` / `list` / `scopes` 中隐藏它；
    - 若该实例另以具名端口暴露，等同于允许绕过，启动时告警。
  - **分片表的物理 key**（`#g` 后缀）不对外暴露，因为分片表只在包装层内部使用。
  - **加密准入覆盖内联实例**：分片表、归属存储都是包装层内联构建的实例，不在端口表里。现有统一加密准入与资源依赖检查若只作用于端口，就会漏掉它们，所以包装层装配时对每个内联实例执行同一检查；实现时须确认现有准入的作用范围，并补测试。
- **元信息聚合**：`security()` / `store_type()` 取各分片最弱的一方（含归属存储）；`health()` 汇总全部分片表、`passthrough`、归属存储、`inner`。
- **关闭顺序（S09 #13）**：
  1. 先关闭使用方（召回算子、实体 / 图包装层）；
  2. 再关闭 KV / 向量 / 全文包装层；包装层关闭时只关闭自己拥有的内联实例（分片表、内联的归属存储），不关闭借用的具名实例、`inner`、`domain_resolver` 与定位组件；
  3. 借用的依赖由装配根按对象身份去重后统一关闭。
  第一阶段不引入锁。

### D14. 故障与一致性

- **查全部分片**：任一分片不可用则整体失败，不静默返回部分结果。分片越多，全局查询的可用性越受单个分片影响，这是严格完整性的代价。
- **指定分片**：只依赖选中分片，以及归属存储（D7、D11）。
- **共享故障域（架构事实）**：归属存储是所有记忆写入、点读与指定分片检索的共同依赖。分片隔离了数据与索引负载，但**没有消除这个共享故障域**——归属存储不可用时，所有分片的记忆读写都不可用。它的可用性要求与现在的单表 KV 相同，应按同等级别部署与备份。
- **跨索引部分失败**：写 KV 成功但写向量 / 全文 / 实体失败——现状已有同样问题，由既有 `BulkWriteError` / partial failure 语义承担。派生存储一律跟随 KV 的归属，不各自判定分片，因此不会出现 KV 与索引落在不同分片的错放。
- **加密**：`encrypted` 套在每个分片表内层，各分片可独立开关、独立密钥。离线迁移必须经包装层写入，以便按各分片密钥重新加密。

---

## 配置与使用

### 运维：四步启用

**第 1 步：写分片规则**

```yaml
domain_resolver:
  default:
    target: rule_based
    params:
      shards: [working, profile, episodic, main]         # 分片集合的唯一权威来源
      default_shard: main
      on_unknown: reject
      rules:
        - { shard_from: system_metadata.shard }          # 需求 B（不需要可删）
        - { shard: working,  match: { tier: working } }  # 需求 A
        - { shard: profile,  match: { tier: core } }
        - { shard: episodic, match: { tier: episodic } }
```

**第 2 步：把各存储端口切到分片模式**

```yaml
kv_store:
  default:
    target: domain_routed
    params:
      resolver: default
      passthrough: kv_legacy
      placement_store: { target: postgres, params: { table: agent_memory_shard_placement } }   # 内联专用表，须声明 ConditionalKV
      table_template: { target: postgres, params: { table: "agent_memory_kv_{shard}", pool_max_size: 4 } }
      tables:                                        # 可选：某分片单独加密 / 独立后端（内联，不成为端口）
        profile: { target: encrypted, params: { raw_kv_store: { target: postgres, params: { dsn: "${PROFILE_PG_DSN}", table: agent_memory_kv_profile } } } }
  kv_legacy:      { target: postgres,  params: { table: agent_memory_kv } }

vector_store:
  default:   { target: domain_routed, params: { resolver: default, locator: default, table_template: { target: pgvector, params: { table: "agent_memory_vectors_{shard}" } } } }
  layers_l0: { target: domain_routed, params: { resolver: default, locator: default, table_template: { target: pgvector, params: { table: "agent_memory_vectors_{shard}_l0" } } } }
  layers_l1: { target: domain_routed, params: { resolver: default, locator: default, table_template: { target: pgvector, params: { table: "agent_memory_vectors_{shard}_l1" } } } }

fulltext_store:
  default:   { target: domain_routed, params: { resolver: default, locator: default, table_template: { target: elasticsearch, params: { index: "memory_fulltext_{shard}" } } } }
  layers_l0: { target: domain_routed, params: { resolver: default, locator: default, table_template: { target: elasticsearch, params: { index: "memory_fulltext_{shard}_l0" } } } }
  layers_l1: { target: domain_routed, params: { resolver: default, locator: default, table_template: { target: elasticsearch, params: { index: "memory_fulltext_{shard}_l1" } } } }

entity_store:                                         # 二选一
  default:     { target: domain_routed, params: { resolver: default, locator: default, mode: shared, inner: es_entities } }
  # default:   { target: domain_routed, params: { resolver: default, locator: default, mode: split,
  #                table_template: { target: elasticsearch, params: { index: "memory_entities_{shard}" } } } }
  es_entities: { target: elasticsearch, params: { index: memory_entities } }

graph_store:                                          # 二选一
  default:    { target: domain_routed, params: { resolver: default, locator: default, mode: shared, inner: graph_main } }
  # default:  { target: domain_routed, params: { resolver: default, locator: default, mode: split,
  #               table_template: { target: memory } } }
  graph_main: { target: memory }
```

`locator: default` 指向声明了 `ShardLocator` 能力的 `kv_store.default`；非 KV 端口的 `resolver` 只用来取分片列表（展开模板、启动校验），不用于判定归属。构建层、检索层配置不用改。`shared` 模式的 `inner` 以具名声明时会成为端口；如不希望被绕过访问，同样可改为内联。

**第 3 步：建表**：`scripts/pg_schema_domain_template.sql` 按分片列表生成 KV 表、向量三层表与归属表；ES 索引按模板创建。

**第 4 步：在维护窗口内首次分区，然后切换配置**

```bash
scripts/migrate_kv_by_domain.py --config prod.yaml --dry-run     # 预览各分片条数
scripts/migrate_kv_by_domain.py --config prod.yaml               # 生成归属、搬 KV、重建三层索引、重建实体分组与图标签
scripts/migrate_kv_by_domain.py --config prod.yaml --reconcile   # 对账：每条记忆恰有一条生效归属、无错放
```

- **「离线」的操作边界是维护窗口，不只是停写**：
  1. 在 API 入口停止接受记忆的读写请求；
  2. 等待在途请求与后台任务（调度器、演进、dreaming）排空；
  3. 执行迁移与对账；
  4. 原子切换到分片配置；
  5. 重新开放读写。

  迁移期间不提供读取，避免读到「归属已写、数据未写完」「KV 已切换、索引还在旧位置」或「部分分片已迁完」的中间状态。
- 迁移幂等，中断后重跑即可。旧单表 `agent_memory_kv` 保留作 `passthrough` 与回滚兜底。
- 不停读的方案（新建完整目标数据集、校验后按版本切换，旧查询继续使用旧版本）列为后续演进。

### 分片相关运维操作

**改规则与重新分区是两件事**：粘性归属下，已有记忆的归属记录已经是权威落点，所以修改规则**只影响此后新建的记忆**，不需要迁移历史数据。想移动已有记忆，是单独的「重新分区」操作。

| 操作 | 是否需要迁移 | 做法 |
|---|---|---|
| 新增 / 修改 `match` 规则、调整规则顺序、修改 `default_shard`、增删 `shard_from` | **否** | 改配置后重启；只影响新建记忆，已有记忆的归属不变 |
| 新增分片 | 否 | 在 `domain_resolver.shards` 中加入、建表、重启 |
| 修改某分片的后端 / 物理表（换表、换库、换集群） | 是（该分片） | 维护窗口内执行 `--relocate-shard <分片>`：拷贝数据到新资源并校验，再更新拓扑登记中该分片的物理标识，最后切换配置 |
| 重命名分片 | 是，且**破坏对外契约** | 不建议；必须时用重新分区并同步通知调用方 |
| 删除（退役）分片 | 是 | 见下方「分片退役」 |
| **重新分区**（显式操作，例如把 `tier` 已被修改的记忆归位） | 是 | 维护窗口内执行：先 `--repartition --dry-run` 预览会移动哪些记忆，确认后执行，再对账 |

**拓扑登记（防止换错物理资源、删掉仍有数据的分片）**

- **登记内容**：归属存储中维护拓扑登记，每个分片一条：`分片名 → {每个端口的物理标识, 状态}`。
  - 物理标识由各端口内联实例声明，不含凭据：PG 为 `host:port/database/schema/table`，ES 为 `集群标识/索引名`；
  - 状态为 `serving`（可写入）或 `retiring`（退役中，不再接收新记忆）。
- **登记时机**：在服务开始接受请求之前，装配完成后、健康检查通过前，对配置中的每个分片做一次登记。
  - 登记不存在 → 原子创建；已存在 → 比对物理标识。
  - 登记一定早于该分片上任何归属生效，因此不存在「已有数据、但未登记」的崩溃窗口。
  - 登记失败则启动失败。
- **启动校验**（任一不满足即拒绝启动）：
  1. **物理标识一致**：配置中某分片任一端口的物理标识与登记不一致（同名分片换了表、库或集群），视为未经迁移的物理变更。只能通过 `--relocate-shard` 更新登记，不能靠改配置绕过。
  2. **登记中的分片都仍在配置里**：登记中存在、配置中已删除的分片，说明可能还有数据，必须先走退役流程。
  3. **退役中的分片不接收新记忆**：规则与 `shard_from` 输出 `retiring` 分片时，insert 返回 `ValidationError`；读取照常。
- 规则内容的变化不参与校验。

**分片退役（安全移除分片）**，在维护窗口内按顺序执行：

1. `--retire-shard <分片> --begin`：把登记状态改为 `retiring`，并从规则、`default_shard` 中去掉该分片（否则启动校验失败），重启后不再有新记忆写入该分片；
2. 用 `--repartition` 把该分片的记忆迁到其它分片，或明确删除；
3. `--retire-shard <分片> --verify`：确认归属存储中没有任何归属指向该分片，各派生存储中该分片的表 / 索引为空，`shared` 模式的实体分组与图标签中也没有该分片；
4. `--retire-shard <分片> --finish`：校验通过后删除该分片的登记；
5. 从 `domain_resolver.shards` 和各端口配置中移除该分片，重启；
6. 物理表 / 索引由运维另行归档或删除，不由工具自动删除。
- 分片名是对外契约，不要随意改名。

### 调用方：怎么用

```python
api.add("下周三交付评审稿", scope, security=sec)                                        # 分片由规则决定
api.add("下周三交付评审稿", scope, security=sec, system_metadata={"shard": "working"})  # 显式指定

api.search("张三最近在做什么", Context(scope=scope), security=sec)                                   # 全部分片
api.search("张三最近在做什么", Context(scope=scope, extensions={"shards": ["working"]}), security=sec)  # 只查工作记忆
api.list(scope, security=sec, extensions={"shards": ["profile"]})
```

- 按 id 读写不需要指定分片。
- `update` 修改 `tier`、显式分片键等字段照常生效，记忆**留在原分片**；按字段当前值筛选请用 `filters`（如 `filters={"tier": "core"}`）。
- 分片名未配置、`[]` / `None` 抛 `ValidationError`；启用的通道不支持分片时也报错。
- HTTP / MCP 同样在 `extensions` 里传 `"shards": [...]`。

---

## 第二阶段：在线迁移已有记忆到其它分片（不在第一阶段范围）

若「修改 `tier` 后记忆立即换到对应分片」或「在线把某条记忆移到另一个分片」成为硬需求，必须先定义以下协议，不能用「包装层里加锁 + 先写后删 + 进程内通知」实现：

1. **记录版本 / 迁移代次**：判断新旧副本的权威依据。
2. **并发控制边界**：覆盖 KV 与全部派生索引（向量、全文、实体、图），而不只是 KV 单次操作。否则 KV 最终在分片 B、索引最终在分片 A 的路由漂移无法避免。
3. **删除与迁移的协调**：防止删除后复活。
4. **持久化迁移状态**：崩溃后可恢复，不依赖进程内回调。
5. **幂等键与重试入口**：旧请求重试不得覆盖较新写入；锁过期后旧执行者的写入须被拒绝（fencing token）。
6. **派生索引同步**：可恢复的同步任务，多实例下不重复、不遗漏；登记 / 注销与装配生命周期绑定。
7. **对账依据与收敛条件**。

配套验证：两次跨分片更新以不同顺序完成、删除与搬迁并发、每一步后崩溃再恢复、锁过期后旧请求继续写入、实体 LINK / UNLINK 与搬迁并发、查询期间搬迁。

---

## 落地修改点（第一阶段）

1. **`storage/domain_routing.py`（新增）**：`DomainResolver` ABC + `rule_based` + `DomainResolverProducer`；`ShardLocator` 契约与能力声明。
2. **`common/type_def/`（修改）**：图节点记忆引用键常量。
3. **`storage/kv_impl/domain_routed_kv.py`（新增）**：KV 包装层与归属记录协议（D7，`pending` / `active` / `cleaning` 状态、操作 token、按完整值的条件提交与删除、数据只原子创建不覆写）；`ShardLocator` 定位组件（批量部分读取，不缓存）；`ConditionalKV` 能力（含批量部分读取）在 PG / Redis / 内存 KV 上的实现；拓扑登记。
4. **`storage/vector_impl/domain_routed_vector.py`、`storage/fulltext_impl/domain_routed_fulltext.py`（新增）**：D8，写入经 `ShardLocator` 跟随 KV。
5. **`storage/entity_impl/domain_routed_entity.py`（新增）+ `EntityStore` 契约补齐、授权代理重做、旧签名适配器（修改）**：D9 两种模式；ES / 内存实体后端支持分组字段与分片过滤。
6. **`storage/graph_impl/domain_routed_graph.py`（新增）+ `GraphQuery` 节点过滤（修改）**：D10 两种模式；三个现有图后端实现「过滤后截 limit」。
7. **`storage/domain_store_impl/`（修改）**：`keyword_recaller.py` 把 `shards` 传给实体查询；各召回算子声明分片支持；`DomainStore` 新增 `get_in_shards`（返回 found / missing / excluded 三类，`get` 不变）；`CompositeDomainStore` 在指定 `shards` 时，于召回结果返回前做一次批量归属校验（D11）。
8. **`construction/index_builder_impl/entity_index_builder.py`（修改）**：适配 `EntityStore` 新签名（传 `scope`）；解除关联改发增量 `UNLINK`，解除最后一个关联时追加 `DELETE_IF_EMPTY`。不感知分片。
9. **`retrieval/expander_impl/default_expander.py`（修改）**：请求带 `shards` 时改调 `DomainStore.get_in_shards`，缺失记 `missing_child`、被排除记 `shard_excluded`。不理解分片规则。
10. **装配校验**：支持矩阵（D12）、命名校验与内联实例的加密准入（D13）、启动前的拓扑登记与校验（「配置与使用」）。
11. **`scripts/`（新增）**：DDL 模板；首次分区、重新分区（`--repartition`，含预览）、物理迁移（`--relocate-shard`）、分片退役（`--retire-shard --begin / --verify / --finish`）、对账、反向合并，均要求在维护窗口内执行。
12. **文档同步**：见「文档同步」。

---

## 拒绝的方案

- **R1. 仅 KV 拆表、索引不分**：无法按分片独立治理索引资源与参数，也无法在召回阶段只查目标分片的物理索引。否决。
- **R2. 全链路字段化平铺**：新 ABC、字段集固化、改动面最大。否决。
- **R3. 硬编码 `MemoryDomain` 枚举**：违反 S09 #1 / #9。否决。
- **R4. 在构建层 / 数据面按分片装配**：分片知识泄漏到 storage 之外。否决。
- **R5. `unit_domain_map` 反查表（包装层私有 PG 表、与数据「同事务」写入）**：私有表违反 S09 #4，`KVStore` 无事务接口，「同事务」无法达成。否决。
  - **与 D7 归属记录的区别**：归属记录放在**注入的 KV 依赖**中，不是私有表；它不依赖事务，而是依赖 KV 自身原子的「不存在才创建」，并定义了「先占位、再写数据」的顺序与中断恢复。
- **R20. 写入前逐表探测冲突 + 读取时并行探测、命中即返回**：两个并发 `insert` 可能都探测为空后分别写入不同分片的表，单表唯一约束挡不住跨表重复；读取时提前返回又无法发现重复。改为归属记录（D7）。
- **R21. 实体 `split` 模式返回「任取一个分片 id 的合并视图」**：对该 id 的 `DELETE` / 整体替换只作用于一个分片，合并视图并不是可正确更新的实体。改为确定性派生物理 id 的稳定逻辑实体身份（D9）。
- **R22. 「禁止搬迁」推出「第一阶段不需要并发协调」**：搬迁只是重复副本的来源之一，并发首次插入同样会产生跨表重复。并发协调由归属记录承担（D7）。
- **R23. 归属记录只做原子占位（无状态）+「同分片补写」重试规则**：原子占位只解决「谁占到位」，不保证整个操作正确——insert 与 delete 交错会出现「insert 返回成功但读不到」；同 id、同分片的不同内容会被误当成同一次补写。改为 `pending` / `active` / `cleaning` 状态 + 操作 token + 按完整值的条件提交与删除；超期 `pending` 由在线对账清理，但不允许新 insert 接管它的 id（D7）。
- **R29. 用创建时间充当操作标识**：时间精度可能使两次创建得到相同值。改为随机操作 token，只存在归属记录中（D7）。
- **R30. 用锁协调同一 id 的 insert / delete**：锁同样约束不到「已失去锁、仍在途」的写入，要真正隔离还需要在数据写入上做 fencing（等价于代次）。第一阶段改用归属记录做互斥位：它约束所有仍持有归属的操作，但**不阻止**失效在途 insert 的晚到写入，只保证旧内容不可见；由此产生的可用性限制写入已知遗留（D7）。完整隔离留给第二阶段的代次设计。
- **R27. 数据 key 与索引记录带代次**：能在 id 复用时隔离旧请求，但代次要扩展到所有物理 key 与索引 metadata，list 与对账也要按代次比对，复杂度高。系统中 `/memory/` 的 id 不会在删除前复用，删除后复用的竞争结果与单表相同（后写覆盖前写），代次带来的收益不足以抵消复杂度。保留状态与条件操作即可防止「插入返回成功但读不到」（D7）。
- **R28. 在线回收超期的 `pending` 归属**：允许其它请求重新占用该 id，等于允许删除前复用 id，需要把操作标识扩展到数据 key 才能防止旧请求覆盖。改为只由对账按完整值条件清理（D7）。
- **R24. 进程内缓存「id → 分片」**：允许删除后同 id 重新插入后，其它实例的缓存会让派生写入落到旧分片；TTL 只能缩短陈旧时间，不能保证正确。改为每次批量读归属（D6）。
- **R25. 实体端口改收 `Scope` 后换用通用授权代理**：通用代理按方法名推导动作，会丢掉 `execute_operations` 按每条操作推导的授权动作。改为保留实体专用的逐条授权，只简化 Scope 提取，并为 `UNLINK`（UPDATE）、`DELETE_IF_EMPTY`（DELETE）增加映射（D9）。
- **R26. 修改规则即要求迁移全部历史数据**：粘性归属下已有记忆已有权威落点，把「改新记忆的分配策略」与「移动已有记忆」绑在一起会增加停机成本，还可能移动用户希望保持不变的数据。改为规则变更只影响新建记忆，重新分区是独立的显式操作（「配置与使用」）。
- **R6. 自造 `RoutingRule` 条件词表**：与 `FilterExpr` 两套实现必然漂移。否决。
- **R7. 把物理表 / 内部枚举暴露给调用方**：改为按逻辑分片名收窄。
- **R8. 每个分片一套完整 `StoreManager`**：实例爆炸，破坏全局唯一 manager。否决。
- **R9. 实体 / 图只提供一种形态（只打标签，或只物理拆分）**：
  - 只打标签（`shared`）保留了跨分片关联，但实体 / 图数据无法按分片独立治理（容量、备份、索引参数）；
  - 只物理拆分（`split`）可以独立治理，但会切断跨分片关联（实体在不指定分片时由合并视图恢复，图的跨分片多跳关系丢失）。
  两种诉求都真实存在，改为可配置的 `shared` / `split` 两种模式（D9 / D10），各自定义写入、检索与迁移语义，支持矩阵在装配时校验。
- **R10. 跨分片检索固定 `rrf`**：不锁死 fuser。
- **R11. 一个分片再拆多个向量索引**：多向量空间属构建 / 检索层特性；扩容属后端职责。否决。
- **R12. 实体 / 图先召回全部、再点读真源过滤**：增加耗时且欠召回。否决。
- **R13. 构建层透传分片标签**：违反诉求 4。否决。
- **R14. 新增「tier 抄到 shard 字段」算子 / 给 MemoryUnit 加 `shard` 字段**：规则可直接读投影字段。否决。
- **R15. 第一阶段用「KV 加锁 + 先写后删 + 进程内通知」实现在线搬迁**：锁只约束 KV 单次操作，约束不到随后的派生索引更新，会发生跨存储路由漂移；也无法处理删除复活、崩溃恢复、重试覆盖、锁过期写入；进程内通知不可靠。另一种做法「声明上层保证串行」在当前代码中不成立。改为第一阶段分片在创建时固定，在线迁移按完整协议放到第二阶段。
- **R16. 「凡 filter 能表达的字段都能分片」**：与禁用字段冲突。改为只允许受支持字段（D4）。
- **R17. update 使分片变化时拒绝更新**：能保证分片与 `tier` 始终一致，但会让「修改已有记忆的 `tier`」这一常规操作失败，需要删除重写（id 变化，图与 continuity 中引用旧 id 的关系失效）。业务已确认可以接受「改 `tier` 不改分片」，改为粘性归属（D5），按当前 `tier` 筛选用 `filters`。
- **R18. 向量 / 全文按记录 metadata 自行判定分片**：`tier` 被修改后，IndexBuilder 重写的索引记录带新 `tier`，若按 metadata 判定会写到新分片，与留在原分片的 KV 不一致。改为一律经 `ShardLocator` 跟随 KV（D6 / D8），同时省去「KV 与索引共用路由投影」的改造。
- **R19. 修改显式分片键时拒绝更新**：本意是避免「键写着 profile、记忆却在 working」的误导，但系统自身会改动该键——演进器 UPDATE 用 `inherited_system_metadata` 合并 metadata，可能删掉原记忆的分片键，拒绝会导致演进失败。改为与 `tier` 一致的粘性归属，在 S02 中写明「显式分片键只在创建时生效」。

---

## 验证计划（第一阶段）

1. **验收标准**：构建层改动仅限实体存储签名适配与改发 `UNLINK`；检索层改动仅限展开器透传 `shards`；控制层、API 层无改动；现有 `tests/unit/` 除上述适配外不改即通过。
2. **规则**：`match` 求值与 `list` / `search` 的过滤语义一致；缺失 / `None` / 空串 / 枚举 / 数组 / 非字符串 `shard_from` 的规范化行为；未命中落 `default_shard`；`on_unknown` 拒绝或回落。
3. **需求 A**：LLM 判定 `tier` 后落到对应分片；`tier` 取任意合法值都有确定落点。
4. **需求 B**：显式指定优先；单源派生记忆继承指定分片；多源合并后按自身字段落位；指定未配置分片名时写入被拒绝。
5. **粘性归属**：update 把 `tier` 从 episodic 改成 core 后，KV、向量三层、全文三层、实体分组、图标签都仍在 episodic 分片（逐一断言）；`shards=["episodic"]` 能检索到它，`shards=["profile"]` 检索不到，`filters={"tier": "core"}` 能检索到；修改或删除显式分片键后更新成功、记忆仍在原分片。
   - **演进回归**：演进器 UPDATE 合并两条指定了不同分片（或一条未指定）的记忆，合并后分片键被删除，写回成功且记忆仍在原分片；SUPERSEDE 产生的新记忆按自身 `tier` 落到对应分片。
6. **派生存储跟随 KV**：向量 / 全文 / 实体 / 图写入时经 `ShardLocator` 定位（批量读归属）；无生效归属的记忆写派生存储时该条失败；删除后同 id 重新插入到别的分片时，派生写入跟随新归属。
7. **KV 与归属记录**（关键交错均用确定性的并发测试复现，在每个步骤之间注入暂停或崩溃）：
   - 两个并发 `insert` 同一 key、路由到不同分片：最多一个成功，另一个 `ConflictError`，生效归属只有一条；
   - **insert 与 delete 交错**：A 创建 `pending` 后，B 删除，A 再写数据并尝试提交——A 的 insert 返回失败，之后 `get` 为 `NotFoundError`，不存在「返回成功但读不到」；
   - **慢插入与对账交错**：对账清理超期 `pending` 后，原插入在提交步骤失败并返回失败；之后同 id 可重新插入；
   - **delete 与同 id 重新插入**：在 delete 的 ②～④ 之间发起重新插入，得到 `ConflictError`；delete 完成后重新插入成功，且数据不被旧 delete 删除；
   - **失效慢 insert 与新 insert**（按反例构造：A 停在写数据前 → 对账清理 → B 占位 → A 写入 → B 写入）：A、B 均失败，A 的旧内容为无主数据且不可见；对账清理后 B 重试成功；另一顺序下 B 成功、A 失败；
   - **对账清理无主数据与新 insert**：对账占位期间新 insert 得 `ConflictError`；新 insert 已占位时对账跳过；
   - **`cleaning` 不在线接管**：原清理者停在删数据前时，在线对账不处理该 `cleaning`；该 id 读取为 `NotFoundError`、insert 为 `ConflictError`；维护窗口排空后对账完成清理；
   - **update 推演**：旧 update 与「删除后同 id 重新插入」在同分片 / 跨分片两种情况下的结果，都能排成合法的串行顺序；
   - **操作 token**：两次创建尝试在同一时间戳发生时，条件提交仍能区分（构造相同创建时间的测试）；
   - insert / delete 在每一步后崩溃：`pending` / `cleaning` 归属在清理前阻止同 id insert（`ConflictError`）、`get` 为 `NotFoundError`；`pending` 由在线对账清理，`cleaning` 只在维护窗口清理；
   - 多实例：两个进程并发对同一 id 执行 insert / delete / 对账，结果符合 D7「协议保证」；
   - 删除后同 id 重新插入到不同分片：旧分片中在途的 `update` 抛 `NotFoundError` 或写成无主数据，不可见；
   - 重试：响应丢失后重试 `insert` 得 `ConflictError`（与现有契约一致）；
   - 其它分片残留同 id 的无主数据时，`get` / `mget` / `list` / `exists` 都只看 `active` 归属指向的分片，且各接口一致；
   - `ShardLocator` 批量定位中部分 id 缺失时，返回已定位部分与「未定位」集合，不整批失败；召回校验只丢弃未定位与不在 `shards` 中的候选；
   - 拓扑登记：同名分片换表 / 换库后启动被拒；未退役就从配置中删除分片时启动被拒；`retiring` 分片拒绝新写入、读取照常；退役 `--verify` 在仍有归属或派生数据时失败；登记早于首个归属生效（登记失败则不接受请求）；
   - 保留前缀：外部对 `/__shard_placement/` 的读写被拒绝，`scan` / `list` 不暴露归属；
   - 非 `/memory/` 落 `passthrough`；`update` 缺失报错；
   - 未选中分片不可用不影响点读；归属存储不可用时点读报不可用；
   - `list` 与单表结果一致；`ConditionalKV` 在 PG / Redis / 内存实现上的契约测试（含存储侧时间）。
8. **返回内容来自真源**：构造「旧分片残留旧内容的索引 + 当前记忆内容已更新」，查全部分片时三条检索路径返回的正文、摘要、metadata 都与当前真源一致，不含残留索引中的旧内容。
9. **向量 / 全文**：CRUD 契约表逐项（insert 定位失败、批量部分失败、按 id 扇出 get、update 不换分片）；记录 id 与 `unit_id` 绑定不满足时拒绝写入；写入路径不做跨分片删除（构造「旧任务暂停 → 删除并在别的分片重建 → 旧任务恢复」，新分片的有效索引不被删除）；残留记录在指定分片检索时被真源校验丢弃，维护窗口内被对账清理；分数可比校验失败则启动失败；合并查询能力只在全部内层声明且兼容时使用；全部分片检索的召回率在验收阈值内。
10. **实体（两种模式各一套）**：
   - `shared`：`INSERT` / `LINK` / `UNLINK` / `DELETE` 后三个字段一致；带 `shards` 时下推 `linked_shards` 过滤、无额外 KV 点读；并发 `LINK` 与 `UNLINK` 交错执行不丢关联（增量操作）；`UNLINK` 移除最后一个关联后记录为空且不被查询返回，`DELETE_IF_EMPTY` 原子删除空记录、对非空记录不生效；`find_by_linked_memory_id` 不受分片影响；`ensure_index` 补齐 mapping；内层未声明分组关联能力时启动失败。
   - `split`：对外 id 为逻辑 id；`LINK` 中跨分片的 id 写入 `{逻辑 id}@{分片}`（不存在则创建）；`find_by_entity_text_hash` 按逻辑 id 聚合；对聚合结果 `DELETE` / `UNLINK` / `DELETE_IF_EMPTY` 作用于全部分片的物理文档；`INSERT` 重试不产生重复物理文档；`ensure_index` 部分分片失败时整体失败并报告分片。
   - 共同：批量操作中单条 id 定位失败只使该条失败、按条返回；**记忆已删除（无归属）时 `UNLINK` 仍能幂等完成**，`split` 模式扇出清理；内层未声明 `atomic_unlink` 时启动失败；重试幂等。
   - `DELETE_IF_EMPTY` 原子性：在判空与删除之间注入并发 `LINK`，记录被保留，新关联不丢失；
   - 批次授权：`UNLINK` + `DELETE_IF_EMPTY` 同批次、只持有 UPDATE 时整批被拒且 `UNLINK` 未执行；执行阶段 `DELETE_IF_EMPTY` 失败时报告部分完成；
   - 授权：用真实 Scope 授权（含授权差异的回归用例与跨 Scope 拒绝）；批量中混合 `INSERT` / `LINK` / `UNLINK` / `DELETE_IF_EMPTY` / `DELETE` 时逐条推导动作；只持有 UPDATE 可解除非最后一个关联；只持有 DELETE 时解除最后一个关联被拒（兼容回归用例，对应发布说明）。
   - 兼容：旧签名实现经适配器可用且有废弃告警，带 `shards` 时报不支持，声明 `atomic_unlink = False`。
11. **图（两种模式各一套）**：
   - `shared`：节点按 `MemoryRef` 打标签；三个现有后端都在截 limit 前过滤（`limit` 为过滤后返回数）；公共节点在指定 `shards` 时不返回；遍历不越过查询 scope；跨 scope 引用不授予访问。
   - `split`：节点、边、占位节点按规则写入，占位节点带真实所属分片；`shards=["working"]` 不会经由 working 图中的占位节点返回 profile 的记忆；遍历不跨分片；不带 `shards` 时扇出合并去重且真实节点优先于占位节点。
12. **按分片检索**：`shards=["working"]` 时其它分片的表 / 索引零调用（spy 断言）；三条检索路径结果都不含其它分片记忆（含派生存储残留旧记录的场景，由真源校验丢弃）；`get_in_shards` 正确区分 `found` / `missing_ids` / `excluded_ids`，展开器分别记 `missing_child` 与 `shard_excluded`；层级摘要展开时，其它分片的子记忆被排除并记 `shard_excluded`；不支持分片的启用通道在带 `shards` 时报错；未选中分片故障不影响指定分片检索。
13. **支持矩阵与装配**：Document Mode / FusionStore 消费方 + 分片启动失败；分片集合不一致、缺表、命名冲突、分片名非法、超长启动失败；归属存储未声明 `ConditionalKV` 启动失败；配置的 `shards` 不是已引用分片的超集时拒绝启动；内联实例经过加密准入；模板部分构建失败只清理新建实例；重复装配不泄漏资源；修改规则后重启无需迁移，已有记忆归属不变。
14. **替换测试**：替换 `DomainResolver` 实现、某分片的 KV 后端、实体后端时，其它包装层与核心装配代码无需修改。
15. **首次分区与重新分区**：幂等重跑；中断后重跑收敛；行数、key 集合一致；每条记忆恰有一条生效归属；实体分组与图标签完整；按各分片密钥重新加密正确；`--repartition --dry-run` 的预览与实际移动一致；维护窗口未排空时工具拒绝执行。
16. **真后端集成**：PG / pgvector / ES / 图后端上验证过滤下推、排序合并与分页。

---

## 性能基线

当前无 benchmark 数据。以下是**验收目标**，不是已成立的结论；阈值在实现前与业务方确认，落地前用测试补齐实测值：

| 场景 | 数据量 | 当前 | 分片后 | 验收目标 | 方法 |
|---|---|---|---|---|---|
| `search` 指定单分片 vs 现状（`filters` 下推 tier） | 10K / 100K × 4 分片 | ? ms | ? ms | P95 不高于现状 × (1 + 约定比例) | 对比测试 |
| `search` 全部分片 | 同上 | ? ms | ? ms | P95 不高于现状 × (1 + 约定比例) | 对比测试 |
| `list` | 1K / 10K / 100K | ? ms | ? ms | 与现状同量级 | benchmark |
| 写入额外开销（归属记录占位、派生存储定位、实体 / 图标签） | 4 / 10 分片 | — | ? ms | 单条写入增加不超过约定值 | benchmark |
| 点读额外开销（多一次归属读取） | — | — | ? ms | 单条点读增加不超过约定值 | benchmark |
| 检索返回前的真源归属校验（一次批量归属读取） | top_k = 10 / 50 | — | ? ms | 不超过约定值 | benchmark |

---

## 回滚 / 告警

1. **回滚**：停写，各端口改回单后端，迁移脚本反向模式合并回单表；实体 / 图改回直指 `inner` 并重建扁平关联。
2. **灰度**：规则启动时冻结，不支持运行期按 scope 灰度；灰度靠部署级切换。
3. **告警**：对账发现无主数据、长期未回收的 `pending` 归属、派生存储中分片不一致的记录、空实体记录；启动时拓扑校验失败；扇出单分片失败接入 health / 审计。

---

## 运维成本

| 维度 | 分片前 | 分片后（4 分片 + 1 passthrough，同后端分表，实体 / 图 `shared`） |
|---|---|---|
| 表 / 索引数 | KV 1 + 向量 3 + 全文 3 + 实体 1 + 图 1 = 9 | KV 5 + 归属 1 + 向量 12 + 全文 12 + 实体 1 + 图 1 = 32（实体 / 图用 `split` 时再各 +3） |
| 监控 | 9 组 | 32 组 |
| 连接池 | 1 个 / Store 实例 | 每个分片表实例各持一个池（`PgStoreBase` 实例级池），总连接数 = Σ `pool_max_size`；需按分片数调小单表池上限 |
| 备份 | 单组一致性快照 | 多表同一时刻快照。**单个分片不是独立的恢复单位**：完整恢复一个分片至少需要该分片的真源表、归属存储中指向它的归属记录、派生索引（或据真源重建）、拓扑登记中该分片的条目，以及对跨分片引用（`shared` 模式的实体分组与图标签、`split` 模式图的占位节点）的处理。归属存储是所有分片共用的控制数据 |
| DDL | 改 1 张表 | 改 N 张同构表（模板化缓解） |
| 规则变更 | — | 只影响新建记忆，无需迁移；重新分区需维护窗口 |
| 故障域 | 单表 KV | 归属存储是所有分片共用的故障域（D14） |

---

## 改动量估算

| 类别 | 估算 |
|---|---|
| 新增文件 | ~8 个（分片规则与定位契约 + 5 个包装层 + DDL 模板 + 迁移脚本） |
| 修改文件 | ~10 个（`common/type_def` 常量、`EntityStore` 契约与后端 + 兼容适配、授权代理、`GraphQuery` 与图后端、`DomainStore.get_in_shards`、`keyword_recaller`、`entity_index_builder`、`default_expander`、各 KV 后端的 `ConditionalKV`、装配校验与拓扑登记） |
| 新增代码 | ~1500-2200 行（含测试） |

---

## 文档同步

- **`docs/specs/S02-memory-api.md`**：`extensions["shards"]` 语义、一致性边界（「属于」= 创建时归属）、通道不支持时报错；写入显式指定分片的方式与语义；粘性归属：修改 `tier` 或显式分片键都不改分片。
- **`docs/specs/S06-storage.md`（实现 PR 时）**：`DomainResolver` / `ShardLocator` 契约与能力声明；分片集合的唯一来源；KV 是分片归属唯一事实来源、派生存储经定位跟随；归属记录协议（`pending` / `active` / `cleaning` 状态与转换、操作 token 与按完整值的条件操作、同 id 协调边界、可见性规则、超期 `pending` 只由对账清理而不被新 insert 接管、`cleaning` 不在线接管、update 不参与协调的盲写语义、重试与 id 复用语义）与归属存储的 `ConditionalKV` 能力；索引记录 id 与 `unit_id` 的绑定；`DomainStore.get_in_shards` 的三类结果；拓扑登记与分片退役；实体 `UNLINK` / `DELETE_IF_EMPTY` 增量操作、空记录不被查询返回、`atomic_unlink` 与分组关联能力、两者的授权动作映射与权限变化；「指定 `shards` 时各通道的分片过滤在截断前生效，返回结果以真源归属校验为准」；端口 × 分片一致性与支持矩阵；`EntityStore` 收 `Scope` 与 `shards` 参数、授权语义变化及兼容期；实体 / 图 `shared` / `split` 两种模式的对外语义；`GraphQuery` 节点过滤（过滤后截 limit）；图节点记忆引用键。不写实现类名 / target 列表。
- **`jiuwen_memory/storage/AGENTS.md`、`jiuwen_memory/construction/AGENTS.md`（实现 PR 时）**：模块地图与本地约束同步。

---

## 已知遗留

- **全文跨分片排序**：异构后端只能近似合并（D8）。
- **list 规模边界**：首版为全量扫描选中分片后统一分页（D7）。
- **派生索引残留只在维护窗口清理**：删除后同 id 重建到别的分片时，旧分片的索引残留到维护窗口；在此之前，查全部分片时它可能按旧内容参与排序（返回的仍是当前记忆）（D8）。
- **非 UUID 记忆 id**：索引记录 id 跨分片唯一的论证依赖记忆 id 为 uuid4 形式（D8）；若有路径写入非 UUID id，需另行约束 id 格式。
- **update 的弱一致性**：update 不参与同 id 协调，与单表 KV 相同，可能把陈旧内容写进删除重建后的同 id 新记录（D7）。
- **悬空关联**：删除某分片或单独恢复某分片备份时，实体关联与图节点可能指向不存在的记忆；召回按 `MissingMemoryUnit` 降级，由对账清理。
- **同一 id 再次插入的可用性限制**（D7「协议保证与不保证」）：
  - 崩溃遗留的 `pending` 在对账清理前、遗留的 `cleaning` 在维护窗口清理前，同 id 的 insert 返回 `ConflictError`；
  - 失效的在途 insert 晚到写入会占住数据位置，使同 id 的下一次 insert 失败，直到对账清理；不保证有限次数内重试成功；
  - 「最长写入时长」阈值需权衡慢写入被误判与阻塞时间。
  - 这些情况只影响同 id 再次插入（schema update 等少数路径），不会使旧内容可见；无主数据与派生存储残留记录由对账清理。
- **并发 insert 同一 id 时两者都可能失败**：赢家在提交前失败时，输家已收到 `ConflictError`，比单表「恰好一个成功」略弱（D7）。
- **`shards` 不约束内容来源**：跨分片生成的摘要与合并记忆可能带出其它分片的信息（D11）。
- **首次分区与重新分区需维护窗口**：不停读的版本化切换列为后续演进。
- **分片与路由字段当前值可能不一致**：粘性归属的预期结果（D5），来源包括 API update 与演进合并 metadata；需要归位时用离线迁移按当前内容重新计算。
- **实体并发新建的重复**：两个请求同时为同一实体文本各自新建，会得到两个实体。这是现状「先查后建」的已有行为，分片不加剧，本方案不解决。
- **连接池按实例**：每个分片表实例各持连接池（D13），分片多时需调小单表池上限。
- **待实现时核实**：
  - `kv_graph_store` 在遍历中读取节点属性的读放大成本（三个后端的可行性已核实，见 D10）；
  - `shard_from` 拒绝发生在 storage 写入时，此时 LLM 抽取已完成。

---

## 后续演进

- **第二阶段在线搬迁**：按「第二阶段」协议实现。
- **按分片保留策略**：在明确派生索引清理协议后提供。
- **单分片字段化**：某分片 `list` 出现瓶颈时，把该分片的 KV 实例换成字段化表。
- **新增端口**：满足 D12 条件的已有 Store 类型配成 `domain_routed` 即可。
- **共享连接池**：把连接池抽成具名共享依赖，定义所有权与关闭顺序，让同后端的分片表共用一个池。

---

## 实现前置条件

- **业务诉求确认**：分片数量；分片字段（按 tier / 显式指定 / 两者）；依赖物理分片的治理诉求（独立后端、索引参数、容量、备份）；全文全部分片检索的召回率验收阈值。
- **粘性归属**（2026-10-30 已确认）：分片在创建时确定；修改 `tier` 照常更新、不改分片，按当前 `tier` 筛选用 `filters`。
- **实体 / 图模式**（2026-10-30 已确认）：接受 `split` 模式下图的跨分片多跳关系丢失这一代价；各部署按需选择 `shared`（保留跨分片关联）或 `split`（独立治理）。
