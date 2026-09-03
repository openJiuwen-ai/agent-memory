# E-05 / F-02 / F-05 费曼学习资料：统一 Storage 与控制基础设施边界

> **文档性质**：辅助性学习资料，不是 spec，也不是 feature 归档。
>
> 本文用于帮助开发者用自己的话解释三个架构问题、解决方案和实施顺序。
> 精确契约以代码、测试以及 `docs/specs/S03-control.md`、
> `docs/specs/S05-construction.md`、`docs/specs/S06-storage.md`、
> `docs/specs/S07-common.md` 为准。

## 1. 先记住一句话

把系统想成一栋有门禁的仓库：

```text
Memory 数据面
  MemoryUnit / Raw Data / Entity / Vector / Fulltext / Graph / FS
                              │
                              ▼
                 统一 Storage + StorageSecurity

Control 基础设施
  JobState / Audit / Lock / Checkpoint
       │
       └── 由各自的 Control 契约拥有；可以使用 KV adapter，但不能把底层 KV 细节泄漏给上层
```

核心原则不是“所有东西都必须共用一个物理数据库”，而是：

1. **Memory 数据面必须经过统一的 Storage 边界和授权。**
2. **Control 基础设施可以有自己的 Store 契约，但必须明确 owner、权限、生命周期和例外范围。**
3. **上层算子只表达业务意图，不拼 key、不选 codec、不直接选择底层 Store。**

## 2. 费曼学习法怎么用

每个问题按四步读：

1. 用一句不带术语的话解释它。
2. 找到当前代码中真正绕过边界的位置。
3. 说明新方案把哪个决策移到了哪个拥有者手里。
4. 尝试回答本节末尾的自测题；答不出来，说明还没有真正理解。

### 2.1 先认识项目里的几个词

| 名词 | 初学者可以把它理解成 | 在本项目中的作用 |
|---|---|---|
| `Scope` | 一条数据的归属地址 | 由 `org / space / user / agent / session` 五个维度组成 |
| `RawPayload` | 接入层收到的一次原始输入信封 | 携带文本/URI、模态、metadata，交给 `Ingestor` 规约 |
| `MemoryUnit` | 系统整理后的“一条记忆”对象 | 包含内容、时间、标签、生命周期、实体等字段 |
| `KVStore` | 一个“键 → 字节值”的小型字典数据库 | 存正排记忆、临时原文、控制状态等 bytes |
| `Storage` | 统一的存储总入口 | 组合 KV、向量、全文、图、文件等能力，并做授权 |
| `IndexBuilder` | 记忆落盘和建索引的编排器 | 写正排，并建立向量/全文/实体索引 |
| `Evolver` | 把本轮输入加工成稳定记忆的算子 | 抽取、合并、去重、关联，再调用 `IndexBuilder` |
| `Entity` | 内容中识别出的人、组织、项目等名称 | 作为反向索引的检索锚点，例如用 “Alice” 找相关记忆 |
| `Control` | 负责流程和任务的控制层 | 管理异步摄入、状态查询、调度和空间治理 |

### 2.2 “原文”“正排”“索引”不是一回事

同一条用户输入，在不同阶段可能有三种形态：

| 形态 | 用途 | 当前典型位置 |
|---|---|---|
| 原文上下文 | 给 `Evolver` 做指代消解和语境补全 | `/messages/{id}`，不参加检索 |
| 正排记忆 | 按 id 取回完整 `MemoryUnit` | `/memory/{id}` |
| 检索索引 | 快速按词、向量、图或实体找到候选 id | Fulltext / Vector / Graph / Entity store |

“不建索引”只代表查找方式不同，不代表这份数据不重要或不需要授权。

### 2.3 `key`、`key prefix` 和 `Scope`

`KVStore` 只认识“名字和值”：

```text
key                 value
----------------------------------
/memory/u123        一串 bytes
/messages/u456      一串 bytes
```

`key` 是记录名；`/messages/` 是 **key prefix**，也就是统一的开头，用来分类和扫描：

```text
/memory/            正排记忆
/messages/          infer 上下文原文
/ingest/jobs/       摄入任务
/ingest/payloads/   payload_id 到 job_id 的幂等映射
```

代码调用 `scan(scope, prefix="/messages/")`，意思是“找出这个 Scope 下所有以该前缀
开头的 key”。prefix 只是命名约定，不是权限系统：它不能判断 actor 是否有权读取，
也不能代替五维 Scope 隔离。

`Scope` 是另一个维度，像数据的地址：

```text
(org=A, space=one) + /memory/u123
和
(org=A, space=two) + /memory/u123
是两条互不可见的记录。
```

### 2.4 `codec`、`Port` 和 `Adapter`

`codec` 是“对象和 bytes 之间的转换规则”。当前 `MemoryUnit` 是 Python 对象，而 KVStore
要求 `bytes`，所以需要：

```text
MemoryUnit --dumps--> JSON bytes --KVStore-->
KVStore bytes --loads--> MemoryUnit
```

`jiuwen_memory/common/type_def/memory_codec.py:51-115` 中的 `dumps/loads` 会处理字段、
时间、枚举和 `_v` 版本号。它不是数据库，只是序列化格式。

`Port` 是上层可以依赖的稳定业务入口；`Adapter` 是把这个入口翻译成具体后端调用的
转换层。例如：

```text
上层：list_raw(scope, limit=10)
  → Raw adapter：scan(prefix="/messages/") + loads + 排序
  → 后端：KV / 文件 / 独立 RawDataStore
```

所以 Raw port 不是给现有 KV 再起一个好听的名字，而是把 prefix、codec、加密 purpose、
后端选择等细节收回 Storage 内部。

### 2.5 `StorageSecurity` 为什么是门禁

`jiuwen_memory/storage/security.py:14-55` 定义了：

```text
authorize(access, scope, action, resource)
```

其中 `access` 说明“谁在访问”，`scope` 说明“访问哪一块数据”，`action` 是读/写/删等
动作，`resource` 是 `memory_unit`、`raw`、`entity` 这类资源类别。当前代码中，Raw 代理
使用资源名 `raw`，Entity 代理使用 capability 名 `entity`；`raw_message` 不是授权资源，
而是 `EncryptedKVStore` 选择的加密 purpose。`job_state` 属于 Control 自有的
`JobStateStore`，当前不经过 `StorageSecurity`，不能把它误写成 Memory Storage 资源。

当前 `Storage.add/get/list/recall` 等接口已经预留 `access`，见
`jiuwen_memory/storage/storage.py:197-307`。E-05 的问题恰恰是 Evolver 绕到裸
`KVStore`，没有让这道门成为原文读写的必经点。

---

## 3. E-05：原文 `/messages/` 直接使用 KVStore

### 3.1 用大白话解释问题

系统把用户输入规约后的、**未建索引上下文记录**暂存到 `/messages/`，供 Evolver 做上下文补全。
E-05 所指出的历史实现中，Evolver 自己拿着一个 `KVStore`，自己决定：

- key 要不要加 `/messages/` 前缀；
- 用什么 codec 序列化；
- 扫描多少条、保留多少条；
- 什么时候删除；
- 访问是否需要授权。

这相当于“业务算子自己拿仓库后门钥匙”。原文不是索引，只说明它不需要建索引，
并不说明它可以绕过数据面授权。

### 3.1.1 典型业务流程：`infer=true` 的一条对话

假设用户在
`(org=acme, space=project-a, user=alice, agent=assistant, session=s1)` 下发送：

```text
“我昨天和 Bob 确认了，项目 Atlas 下周发布。”
```

并在 `system_metadata` 中设置 `infer=true`。一次典型请求会这样走：

```text
API/SDK
  → Engine.write(content, scope)
  → RawPayload → Ingestor → MemoryUnit
  → infer=true，调用 Evolver.evolve(units, EXTRACT)
  → 通过 Storage.raw_port().list_raw 取最近 N 条历史原文
  → 通过 RawDataStore.append_raw 写入本轮原文并执行保留策略
  → Extractor 使用本轮内容 + 最近上下文抽取派生记忆
  → IndexBuilder 把派生记忆写入 /memory/ 并建立检索索引
```

把流程和当前代码一一对应起来：

| 业务步骤 | 当前代码 | 代码做了什么 |
|---|---|---|
| 接收输入 | `jiuwen_memory/control/engine_impl/in_memory_engine.py:235-288` | 接收 `content`、`scope`、metadata，构造 `RawPayload` |
| 规约输入 | `in_memory_engine.py:289-300` | 调用 `Ingestor.ingest` 得到 `MemoryUnit` |
| 选择 infer 分支 | `in_memory_engine.py:326-354` | `infer=true` 时调用 `evolver.evolve(..., EXTRACT)` |
| 进入抽取流程 | `orchestrating_evolver.py:913-948` | 非 procedural 的 EXTRACT 先维护原文，再调用 Extractor 和去重落盘 |
| 读取/保存/淘汰原文 | 旧实现为 `orchestrating_evolver.py:738-819`；当前为 `:742-809` | 旧实现直接 `scan/insert/delete`；当前通过 `RawDataStore.append_raw/list_raw` 完成 |
| 保存派生记忆 | `orchestrating_evolver.py:265-268` | 通过 `IndexBuilder.build` 写入正排和索引 |

这里的“原文”不是最终长期记忆。它是给本轮抽取临时使用的上下文副本；最终派生记忆
才会进入 `/memory/`，供后续检索和生命周期管理。

还要注意：当前 `/messages/` 保存的不是“单纯的原始字符串”，而是已经经过 Ingestor
规约的 `MemoryUnit` 的 `dumps()` 结果，见 `orchestrating_evolver.py:744-755`。因此
X-01 若引入独立 Raw 模型，需要明确它是否继续保存这种规约后对象，还是改存新的
`RawRecord`；这正是 Raw 契约要先冻结的内容。

当前实现还有一个重要的降级语义：如果历史 `scan` 失败，
`orchestrating_evolver.py:781-790` 会记录告警、把 `recent` 当成空列表，仍然写入本轮
原文，并跳过本次淘汰；它不会因为读历史失败就直接拒绝整次抽取。这个行为以后是否
保留，也要写进 Raw 契约，而不能由 adapter 各自决定。

### 3.1.2 原问题的代码形态（历史快照）

`_persist_and_maintain_messages` 可以先按下面的伪代码理解：

```python
historical = message_store.scan(scope, prefix="/messages/")
recent = 按 t_ingest 排序后的历史前 N 条
message_store.insert(scope, "/messages/本轮-unit-id", dumps(unit))
message_store.delete(scope, 超出 N 条的旧 key)
```

问题单描述的旧实现关键位置是：

- `OrchestratingEvolver.__init__` 曾接收 `message_store: KVStore`；
- `_add_messages` 曾调用 `insert(scope, messages_key(unit.id), dumps(unit))`；
- `_list_messages` 曾调用 `scan(scope, prefix=MESSAGES_KEY_PREFIX)`，再 `loads(raw)`；
- `_delete_messages` 曾调用 `delete`；
- `_resolve_message_store` 曾通过 `KvProducer` 解析裸 KV。

这段历史形态仍然值得学习，因为它正是 E-05 要消除的耦合；但不要把它误认为当前
工作区的现状。

因此，Evolver 同时知道了五件本来应该由 Storage 拥有的事：

1. 物理命名：`/messages/` 前缀和 key 拼接；
2. 编码格式：`dumps/loads` 和 JSON 版本演进；
3. 后端 API：`insert/scan/delete`；
4. 治理策略：最近 N 条、排序、淘汰时机；
5. 数据面入口：没有显式 `StorageAccessContext` 的授权检查。

把旧边界和当前边界并排看，会更直观：

```text
旧：Evolver → KVStore.scan/insert/delete
              ├── 自己写 /messages/
              ├── 自己 dumps/loads
              └── StorageSecurity 看不见这次访问

当前：Evolver → Storage.raw_port() → Authorized proxy → KVRawDataStore → KVStore
                                                │              ├── /messages/
                                                │              ├── dumps/loads
                                                │              └── retain_limit
                                                └── StorageSecurity.authorize(...)
```

这里有一个容易误解的点：Raw adapter 仍会使用 prefix 和 codec，它们没有消失；变化是
**只有 Storage 内部的 adapter 知道它们**。这使后续替换为文件或专用 RawDataStore 时，
Evolver 的业务代码不必跟着改。

### 3.2 当前代码在哪里，以及已经修到哪一步

- 当前 `OrchestratingEvolver` 接收并校验 `RawDataStore`（`orchestrating_evolver.py:175-207`），
  不再接受裸 `KVStore` 作为生产消息端口。
- `_add_messages`、`_list_messages`、`_delete_messages` 只调用
  `append_raw/list_raw/delete_raw`（`:742-754`）。
- `_resolve_message_store` 当前通过 `StorageProducer.resolve(config).raw_port()` 获取端口
  （`:1001-1003`）。
- `KVRawDataStore` 集中拥有 `/messages/`、`dumps/loads`、按 `t_ingest` 排序和保留淘汰，见
  `jiuwen_memory/storage/raw.py:42-145`。
- `CompositeStorage` 默认可把正排 KV 包成 `KVRawDataStore`，并为 raw port 建授权代理，见
  `jiuwen_memory/storage/storage_impl/composite_storage.py:141-202`。
- `KVSpaceManager` 已通过 Raw 端口的 `scopes/usage/purge` 管理原文；它只直接使用 KV
  统计正排记忆和空间元数据，不再解释 `/messages/` 前缀。独立 Raw 后端的治理路径也已
  纳入统一接口，剩余的是继续补充真实后端的集成验证。

当前 `CompositeStorage.scopes()` 会合并正排 KV 与 Raw 端口的 Scope，见
`jiuwen_memory/storage/storage_impl/composite_storage.py:396-403`。因此独立 Raw 后端已经
进入统一的 Scope 枚举路径；真实后端仍应继续验证删除、usage 和异常语义一致。

默认装配下，`message_store` 通常和正排 `kv_store.default` 是同一个物理 KV 实例，
只是用 `/messages/` 和 `/memory/` 两个 prefix 分开。**共用一个物理实例本身不是问题**；
原问题里的真正问题是 Evolver 直接拥有 KV API，并且没有经过统一授权、治理和 Raw 业务契约。

这里要区分两层事实：`prefix` 和 `codec` 本身未必是坏东西，坏的是它们被 Evolver 这个
业务算子直接掌握。比如 `scan(prefix="/messages/")` 只能找到“看起来像原文”的 key，
不能证明当前 actor 有权读这些数据。

当前实现的关键位置：

- `jiuwen_memory/construction/evolver_impl/orchestrating_evolver.py`
- `jiuwen_memory/construction/evolver_impl/dynamic_evolver.py`
- `jiuwen_memory/construction/evolver_impl/schema_orchestrating_evolver.py`
- `jiuwen_memory/control/space_impl/kv_space_manager.py`

### 3.3 为什么这是架构问题

这条路径同时有四个风险：

1. **授权旁路**：`StorageSecurity` 只保护了 Storage 暴露的领域接口或代理端口，Evolver
   可能拿到的是裸 KV。
2. **实现细节上浮**：Evolver 知道 key prefix、codec 和 FIFO 维护策略，未来换 RawDataStore
   时必须修改构建层。
3. **授权和 Scope 约束分散**：KVStore 本身会按 Scope 做命名空间隔离，但裸 KV 调用方仍可
   自行选择传入哪个 Scope，且没有统一 StorageSecurity 防线来约束访问上下文。
4. **治理路径不完整**：如果原文未来独立后端，space 删除、usage、`scopes()` 仍只扫 KV，可能漏数据。

把风险和代码对上：

| 风险 | 对应代码/事实 | 为什么有风险 |
|---|---|---|
| 授权/Scope 约束分散 | 历史 `orchestrating_evolver.py:176-200, 744-759` | 历史依赖是裸 `KVStore`；后端的 Scope 命名空间存在，但访问上下文没有统一 StorageSecurity 检查点 |
| 细节上浮 | 历史 `orchestrating_evolver.py:744-819` | 历史 Evolver 自己拼 prefix、codec、排序和淘汰 |
| 后端绑定 | 历史 `orchestrating_evolver.py:1011-1028` | 直接用 `KvProducer` 解析 KV |
| 多实现重复暴露 | `dynamic_evolver.py:101-107`、`schema_orchestrating_evolver.py:130-160` | 子类复用同一套裸 KV 路径 |
| 治理漏数据 | 历史 `kv_space_manager.py:427-493` | 历史 space delete/usage 只扫描 KV prefix |

### 3.4 对应解决方法

当前代码已经提供了受权 Raw 端口，接口形态如下：

```text
append_raw(scope, records, retain_limit, access)
list_raw(scope, limit, access)
delete_raw(scope, record_ids, access)
```

当前 `RawDataStore` 方法已经保留 `access` 参数，`CompositeStorage` 的
`_AuthorizedStoreProxy` 会在端口调用前执行授权；Raw 资源名是 `raw`，操作映射到
`StorageAction.ADD/LIST/DELETE`。当前默认调用允许省略 access，由默认安全策略兼容旧调用；
需要进一步推进的是 API/Engine 到 Evolver 的访问上下文统一传播，让非默认安全策略也能在
完整调用链上执行细粒度授权。这是接线和测试覆盖问题，不是 Raw 端口缺少授权入口。

职责分工应保持不变：

| 谁 | 只负责什么 |
|---|---|
| Evolver | 请求“追加本轮原文并保留 N 条”“列出最近 N 条” |
| Storage Raw 端口 | Scope 校验、授权、排序、计算并执行常规淘汰、错误语义 |
| Raw adapter | key prefix、序列化、加密 purpose、底层后端读写 |
| Space/治理层 | 通过受权 Raw 管理能力做空间删除、统计和显式清理 |

这里建议把“追加和保留”设计为同一个 Raw port 操作：

```text
Evolver → append_raw(..., retain_limit=N)
             └── Raw port 按约定排序，自己计算并删除超额记录
```

这样通常流程中 Evolver 不再计算 `evicted_ids`，也不再调用 `delete_raw`。`delete_raw`
仍可保留给显式管理动作，例如空间注销、按 id 删除或治理清理；它同样必须经过 Scope 和
授权校验。若最终选择另一种接口形态，也必须明确“谁计算淘汰项”，不能让两层同时负责。

X-01 尚未确定最终 Raw Data 模型，因此当前端口把规约后的 `MemoryUnit` 当作兼容 raw record，
由 `KVRawDataStore` 复用现有数据。后续若引入独立 `RawRecord`，应只替换 adapter 或记录类型，
不改变已经落地的 Scope、授权和保留契约。

这里的三个名字要分开记：

```text
Raw data       数据类别：用户原始上下文
Raw port       对上层公开的受权业务接口
Raw adapter    把 Raw port 翻译成 KV/文件/独立后端调用的实现
```

例如 `list_raw(scope, limit=10, access=access)` 的内部实现可以是：

```text
Storage.raw_port.list_raw
  → authorize(access, scope, LIST, "raw")
  → KVRawDataStore.scan(prefix="/messages/")
  → loads / 解密 / 按 t_ingest 排序
  → 当前返回最近 10 条兼容载荷 `MemoryUnit`
```

上层只看到第一行，不需要知道后面三行。这样设计的价值在于：如果后端从 KV 换成文件，
或者 `MemoryUnit` 改成独立的 `RawRecord`，上层 Evolver 的业务流程仍然不变。

### 3.5 为什么这样能解决

因为所有原文读写都会经过同一个检查点：

```text
Evolver → Storage.raw_port → StorageSecurity.authorize → Raw adapter → backend
```

于是：

- 自定义 `StorageSecurity` 可以拒绝 append/list/delete；
- Evolver 不再知道 prefix、codec 和加密目的；
- Scope 隔离由存储层强制，而不是依赖 Evolver 的调用纪律；
- 更换 KV、文件或独立 RawDataStore，不需要改 Evolver 的业务流程。

注意，Raw port 也不应该只是把下面的方法原样转发出去：

```python
raw_port.scan(scope, prefix="/messages/")  # 仍然暴露了 KV 思维
```

更好的 port 使用业务语言：

```python
raw_port.list_raw(scope, limit=10, access=access)
```

前者只是“换了一个对象名”，后者才真正隐藏了 key、codec、排序和后端。

有一个不能藏起来的配套问题：虽然 `OrchestratingEvolver` 已有 `raw_access` 注入点
（`:188-199`），但完整 API/Engine/Job 调用链是否都能构造并传递该上下文，仍需确认。因此
契约必须决定 access 是：

- 显式加入 Evolver/Job 的调用链；还是
- 在构造期注入一个绑定访问上下文的受权端口。

不能只把 `KVStore` 改名为 `RawStore`，否则授权仍然没有真正落地。

### 3.6 E-05 自测

- 如果 RawDataStore 不再使用 KV，space 删除如何找到它的 Scope？
- 为什么“原文不建索引”不能推出“原文不需要授权”？
- Evolver 是否还能看到 `/messages/` 这个物理前缀？如果能，说明封装还没完成。

---

## 4. F-02：EntityStore 游离于统一 Storage

### 4.1 用大白话解释问题

实体索引也是 Memory 数据的一部分。F-02 描述的是历史上它像仓库外另搭的一间小库：

- Storage 不声明它有 Entity 能力；
- CompositeStorage 不持有或代理它；
- Construction 和 Retrieval 各自通过 `EntityStoreProducer` 找它；
- 实体方法用 `space_id` 和 `actor_id`，不是统一的五维 `Scope`。

结果是上层知道了太多后端拓扑，而且实体索引没有统一授权入口。下面的“旧流程”专门
说明这个问题；当前流程已经收敛到 `Storage.entity_port()`。

### 4.1.1 先理解 Entity 为什么属于 Memory 数据

假设一条记忆是：

```text
“Alice 在 Atlas 项目中负责发布流程。”
```

普通全文索引可以根据词语命中这条记忆；Entity 反向索引还会保存：

```text
Alice → [memory-1, memory-8, memory-21]
Atlas → [memory-1, memory-5]
```

这样搜索 “Alice” 时，系统可以先找到一条带有 Alice 的记忆，再沿实体关系反查其他
关联记忆。上图是**逻辑关系图**；当前 Elasticsearch 实现实际持久化的是
`entity_text_hash` 和关联的 memory id，不持久化实体明文，见
`jiuwen_memory/storage/entity_impl/elasticsearch_entity_store.py:151-177`。Entity 不是 UI
标签，而是一种 Memory 数据索引。

### 4.1.2 典型业务流程一：写入包含实体的记忆

假设 `MemoryUnit` 大致是：

```text
scope=(acme, project-a, alice, assistant, s1)
content="Alice 在 Atlas 项目中负责发布流程"
entities=["Alice", "Atlas"]
```

当前写入流程：

```text
1. HybridIndexBuilder.build(units)
2. 先写正排，再写全文索引，再写向量索引
3. 如果启用 entity，再调用 EntityIndexBuilder
4. EntityLinkService 读取 unit.entities
5. 归一化实体名并计算 hash
6. 按完整 Scope 分组（Storage 适配器再派生 routing/filter）
7. 查询已有实体记录
8. 不存在则 INSERT，存在则把 unit.id LINK 到实体
```

对应代码：

| 业务步骤 | 当前代码 | 代码做了什么 |
|---|---|---|
| 四路建索引 | `hybrid_index_builder.py:74-85` | 依次调用 forward/fulltext/vector/entity builder |
| 读取实体 | `entity_index_builder.py:161-191` | 读取 `unit.entities`，归一化并构造 `EntityMention` |
| 分组和隔离 | `entity_index_builder.py:193-202` | 以完整 Scope 分组；端口边界负责派生 `space_id` 和 `EntityStoreFilters` |
| 写入/链接 | `entity_index_builder.py:275` 之后 | 查询已有实体并提交 bulk operations |

历史上其余三路 builder 都从同一个 `Storage` 取端口，但 Entity linker 是单独注入的
`EntityStore`，所以表面上是“四路一起建索引”，实际装配边界并不一致。当前 Entity linker
也由同一个 `Storage.entity_port()` 提供。

### 4.1.3 典型业务流程二：搜索 “Alice”

问题单中的旧版 `KeywordRecaller` 实体扩展可以简化成：

```text
1. FulltextStore.search(scope, query)
2. 根据命中的 id 点读 MemoryUnit
3. 若 batch1 不足 top_k，读取候选的 metadata['entities']
4. 归一化并 hash 实体文本
5. 旧版 EntityStore.find_by_entity_text_hash(space_id, hashes, filters)
6. 从 linked_memory_ids 得到关联 memory id
7. 再从 Storage 点读真源并做生命周期/时间/标量过滤
8. 与全文结果合并后返回 top_k
```

对应代码：

- 主召回流程在 `jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py:101-128`；
- Entity 反查、Scope 和真源点读在 `keyword_recaller.py:163-224`；
- 旧版装配在 `keyword_recaller.py:257-268`，直接 import `EntityStoreProducer` 并调用
  `EntityStoreProducer.dep(...)`；当前装配在 `:252-261` 从 `Storage` 取得 Entity 端口。

### 4.2 当前代码在哪里，以及已经修到哪一步

- 当前 `StorageCapability` 已包含 `ENTITY`，见 `jiuwen_memory/storage/storage.py:73-80`。
- `CompositeStorage` 已接收 Entity 端口并建立授权代理，见
  `jiuwen_memory/storage/storage_impl/composite_storage.py:145-202`；`RoutingStorage` 也已转发
  `has_entity_port/entity_port`。
- `HybridIndexBuilder` 当前从 `storage.entity_port()` 装配实体链接器，见
  `hybrid_index_builder.py:147-171`；`KeywordRecaller` 当前同样从 Storage 获取实体端口，见
  `keyword_recaller.py:252-261`。
- `EntityStore` 顶层接口已改为显式接收 `Scope`；旧的 `space_id + filters` 后端通过
  `adapt_entity_store` 兼容。当前 `EntityStoreFilters.from_scope` 已把 org/space/user/agent/session
  全部写为硬过滤字段（`common/type_def/entity.py:46-77`），并由 ES 与内存测试桩按该投影执行。

因此，F-02 的“上层直接找 EntityStoreProducer”已经修复；现在的剩余重点是继续验证授权
代理、五维 Scope 隔离、命名端口转发，以及让不依赖 Elasticsearch 的自定义 Storage 实现
通过同一能力契约工作。

关键位置：

- `jiuwen_memory/storage/storage.py`
- `jiuwen_memory/storage/storage_impl/composite_storage.py`
- `jiuwen_memory/config/routing.py`
- `jiuwen_memory/storage/entity_store.py`
- `jiuwen_memory/construction/index_builder_impl/hybrid_index_builder.py`
- `jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py`

### 4.3 为什么这是架构问题

问题单的验收目标是“Entity 属于统一 Storage 能力，并且五维 Scope 隔离”。历史代码存在两个
关键差异：

1. **能力模型不一致**：历史 Storage 说自己没有 Entity，但上层照样使用 EntityStore；当前
   `StorageCapability.ENTITY` 和端口已补齐。
2. **隔离模型缩水**：旧后端只按 `space_id + actor_id(user)` 路由；当前兼容层已补上五维
   filters，剩余工作是持续验证各实现不发生回归。

因此 F-02 不是单纯的 import 替换，而是接口、授权和数据隔离模型一起收敛。当前需要验证的
不是“要不要五维隔离”，而是各个 adapter 是否真的把五维 Scope 按同一规则下推。

旧边界和当前边界的差异如下：

```text
旧：HybridIndexBuilder / KeywordRecaller → EntityStoreProducer → Elasticsearch EntityStore
    两个上层模块各自决定如何装配，StorageSecurity 无统一入口。

当前：HybridIndexBuilder / KeywordRecaller → Storage.entity_port()
                                               → Authorized proxy
                                               → Entity adapter → Elasticsearch 或内存实现
```

`StorageCapability.ENTITY` 回答的是“这个 Storage 是否声称提供实体能力”；
`entity_port()` 回答的是“请给我一个可调用且受统一代理保护的实体入口”。前者用于能力发现，
后者用于真正操作。两者不能互相替代。

把风险和代码对上：

| 风险 | 对应代码/事实 | 为什么有风险 |
|---|---|---|
| 能力不可发现 | 历史 `storage.py:64-70` 没有 `ENTITY` | `Storage.capabilities()` 无法说明是否支持实体 |
| 组合不完整 | 历史 `composite_storage.py:97-151` 只组合六类 Store | 历史 CompositeStorage 无法统一持有或代理 Entity |
| 上层直连后端 | 历史 `hybrid_index_builder.py:151-177` | Construction 直接决定 `elasticsearch` |
| 另一路重复直连 | 历史 `keyword_recaller.py:257-268` | Retrieval 再次自行解析 EntityStore |
| 授权旁路 | 历史 CompositeStorage 代理不覆盖独立 Entity | Entity 读写没有统一 StorageSecurity 检查点 |
| Scope 维度缩水 | `entity_store.py` 与 `entity.py` 的兼容投影 | 旧后端仍可能只按 user/space 路由，五维隔离必须靠测试证明 |

还有一个需要一起盘点的连锁影响：`jiuwen_memory/construction/dedup.py:93-103` 的
`same_scope` 当前只比较 `org + space + user`，因此去重也把同一 user 跨 agent/session
视为同一范围。若 F-02 最终冻结“五维都隔离”，不能只改 EntityStore；`Dedup`、检索索引
和相关负向测试也要一起评估。反过来，若仍保留 user-level 共享，也应在契约中明确这是
有意的 Scope 投影，而不是遗漏 agent/session。

### 4.4 对应解决方法

当前代码已经把 Entity 纳入统一 Storage，核心接口是：

```text
StorageCapability.ENTITY
Storage.has_entity()/has_entity_port(name)
Storage.entity_port(name="default")
```

并要求：

- CompositeStorage 接收 default/named EntityStore；
- CompositeStorage 为 EntityStore 建立 StorageSecurity 代理；
- RoutingStorage 转发 Entity capability 和端口；
- EntityStore 顶层方法以 Scope 为隔离输入，旧后端由 adapter 转换；
- EntityIndexBuilder 和 KeywordRecaller 只接收 Storage 或 entity port；
- `EntityStoreProducer` 只保留为 Storage builder 的底层工厂；
- `StoreType.ENTITY` 已加入；自定义 Storage 仍必须在 capability、端口和 health 语义上保持一致。

这里的 `entity_port()` 不是简单把现有 `EntityStore` 重新暴露一次。当前代理至少承担三件事：

1. 把上层的完整 `Scope` 转成后端需要的 namespace/routing/filter；
2. 在每次读、搜、写、删前调用 `StorageSecurity`；
3. 让 CompositeStorage、RoutingStorage 和自定义一体化 Storage 都遵守同一套能力和错误语义。

可以把一次实体查询想成：

```text
entity_port.find_by_entity_text_hash(scope, hashes)
  → authorize(access, scope, SEARCH, "entity")
  → Entity adapter 按已冻结的 Scope 投影生成 routing + 隔离条件
  → 后端返回 EntityRecord
```

当前实现选择五维隔离：`EntityStoreFilters.from_scope` 会保留 agent/session。若产品以后要恢复
“同一 user 跨 agent/session 共享实体”，必须显式修订契约、下游索引和负向测试；不能靠删除
过滤字段悄悄改变隔离范围。

### 4.5 为什么这样能解决

调用链变成：

```text
Construction/Retrieval → Storage.entity_port → 授权代理 → Entity adapter → backend
```

上层不再关心 Elasticsearch 是否存在，也不会让 Construction 和 Retrieval 各装配一份
互不相同的 EntityStore。当前 ES 和测试用内存 EntityStore 都走同一端口；“同 org 不同
space 不串读”是否在所有自定义后端都成立，仍由跨 Scope 负向测试持续守住，而不是只依赖
`EntityStoreFilters` 的局部约定。

验收时可以用一个最小替代实现验证这件事：写一个只用内存字典的
`InMemoryEntityAdapter`，不安装 Elasticsearch，但通过同一个 `entity_port` 完成
INSERT、LINK、反查和删除。如果 Construction/Retrieval 不需要改代码，说明上层确实只
依赖了能力契约，而不是后端名称。

需要特别注意：当前 `IndexBuilder.build/update/remove` 和 `Recaller.recall` 仍没有统一的
access 参数。端口代理虽已存在，调用链是否总能提供正确上下文，必须和 E-05 一起冻结并测试。

### 4.6 F-02 自测

- 如果没有 Elasticsearch，为什么一体化 Storage 仍然可以提供 Entity 能力？
- 同一 user 跨两个 agent 的实体是否共享？当前答案应为不共享；哪段 Scope 投影和测试保证它？
- `KeywordRecaller` 是否能在不知道 EntityStoreProducer 的情况下工作？

---

## 5. F-05：Control 任务状态直连 KV

### 5.1 用大白话解释问题

摄入任务状态不是 MemoryUnit，也不是检索索引。它可以使用 KV，但“可以使用 KV”不等于
“业务控制器可以直接操作 KV”。问题单中的旧版 `IngestJobController` 自己负责：

- 任务状态 JSON 序列化；
- `/ingest/jobs/` 和 `/ingest/payloads/` key；
- payload 幂等映射；
- 重启后的状态修正。

这让 Control 的业务接口被底层 KV 细节绑住，而且没有明确保留期和恢复语义。

### 5.1.1 典型业务流程：提交一个视频摄入任务

视频解析可能要花很久，API 通常不会一直阻塞等待，而是先返回一个 job id：

```text
1. API 接收视频 URI、payload_id 和目标 Scope
2. IngestJobController.submit 创建 pending 任务
3. 用 payload_id 检查是否已经提交过同一个任务
4. 把任务状态持久化
5. 后台线程把状态改为 running
6. 视频解析和记忆写入完成，状态改为 succeeded，并记录 unit_ids
7. 失败时状态改为 failed，并保存 error
8. 客户端查询 job_status，API 再按任务真实 Scope 做 READ 授权
```

当前代码逐步对应：

| 业务步骤 | 当前代码 | 代码做了什么 |
|---|---|---|
| 视频入口 | `bootstrap/core/handler.py:421-467` | 生成 `payload_id`，先做 WRITE 鉴权，再提交 `ingest_jobs.submit` |
| 控制器持有存储 | 旧版 `control/job_impl/ingest_job.py:30-62` | 直接 import `KvProducer, KVStore`，并保存 `_kv` |
| 提交和幂等 | `ingest_job.py:73-143` | 创建 job、查已有 payload、写状态、提交线程 |
| 查 payload 映射 | 旧版 `ingest_job.py:160-178` | 直接 `exists/get`，再 `_load` 任务 |
| 状态流转 | `ingest_job.py:180-212` | `_run` 和 `_update` 直接更新内存字典并持久化 |
| 序列化和 key | 旧版 `ingest_job.py:214-241` | JSON、`/ingest/jobs/`、`/ingest/payloads/` 全在控制器里 |
| 读取和重启修正 | 当前 `ingest_job.py:234-252` | 通过 `JobStateStore.get` 读取；pending/running 仍被改成 failed |
| 对外查询授权 | `local_memory_api.py:2754-2793` | 先取任务 Scope，再执行 `Action.READ` 授权 |

这条流程说明一个重要边界：API 层已经有“按任务真实 Scope 授权”的动作。当前控制器内部
已经改为调用 `JobStateStore`，builder 保留 `kv_store` 兼容配置，但只在 adapter 层构造 KV
实现；“KV 只能出现在哪一层”的白名单已经写入 Control/Storage 规约。

### 5.2 当前代码在哪里，以及已经修到哪一步

- 当前 `InProcessIngestJobController` 只持有 `JobStateStore`，见
  `jiuwen_memory/control/job_impl/ingest_job.py:45-60`。
- `_persist` 只调用 `self._state_store.save`（`:231-232`）；JSON、prefix、KV insert/update
  已下沉到 `jiuwen_memory/control/job_impl/job_state.py` 的 `KVJobStateStore`。
- `JobStateStore` 已提供 `save/get/find_by_payload/delete/cleanup`，并按完整 Scope + 可选
  owner 隔离。
- 内存和 KV adapter 都已支持 TTL/终态 TTL、payload 幂等映射和清理；重启时
  pending/running 仍按当前实现标记 failed，不是自动恢复。
- builder 保留 `kv_store` 兼容输入，但只把它转换成 `JobStateStoreProducer.build("kv", ...)`；
  `ingest_job.py` 不再知道 `KvProducer`。

把风险和代码对上：

| 风险 | 对应代码/事实 | 为什么有风险 |
|---|---|---|
| 后端绑定 | 历史 `ingest_job.py:30-31`、`293-299` | 控制器和 builder 直接依赖 `KVStore/KvProducer` |
| 格式上浮 | 历史 `ingest_job.py:214-241` | 控制器自己决定 JSON schema、prefix 和 insert/update |
| 重启终止语义隐藏 | 当前 `ingest_job.py:234-252` | pending/running 被改成 failed；这不是自动恢复或重试 |
| 生命周期语义 | 当前 `job_state.py` / `job_impl/job_state.py` | TTL、cleanup、终态保留和重启中断已有实现；真实跨进程部署仍需持续验证 |
| 规则容易扩散 | 任务状态、audit、lock 都可能看到同一 KV | 各类基础设施必须分别建立契约和 adapter，不能复用 Memory Storage 规则 |

把一次 `_persist` 翻译成人话就是：

```text
1. 把 IngestJob 对象转成 JSON bytes
2. 拼出 /ingest/jobs/{job_id}
3. 用 exists + insert/update 写任务
4. 拼出 /ingest/payloads/{payload_id}
5. 保存 payload_id → job_id 的幂等映射
```

这些行为本身可能都需要保留，但它们不应该全部由 `InProcessIngestJobController` 拥有。
控制器应该关心“提交、查询、更新状态、处理重复提交”，而不是关心 JSON、prefix、
`insert` 还是 `update`。

同样可以把职责变化画成两条线：

```text
旧：IngestJobController → JSON + /ingest/jobs/ + /ingest/payloads/ + KVStore

当前：IngestJobController → JobStateStore → InMemoryJobStateStore
                                            └→ KVJobStateStore → JSON + prefix + KVStore
```

这里“Control 可以使用 KV”的准确含义是：`KVJobStateStore` 这个基础设施 adapter 可以引用
`KVStore/KvProducer`；`IngestJobController` 不能。前者负责翻译存储协议，后者负责调度任务。

关键位置：

- `jiuwen_memory/control/job_impl/ingest_job.py`
- `jiuwen_memory/control/ingest_job.py`
- `jiuwen_memory/config/defaults.py`
- `jiuwen_memory/api/memory_api_impl/assembly.py`

### 5.3 对应解决方法

当前代码已经定义了 Control 自有的 `JobStateStore` 契约：

```text
IngestJobController → JobStateStore → KV adapter（或数据库/队列 adapter）
```

`ingest_job.py` 只知道 JobStateStore，不知道：

- key prefix；
- JSON/codec；
- KVProducer；
- 底层数据库类型。

底层 KV adapter 可以继续存在，但它是文档白名单中的基础设施 adapter，按 JobStateStore
契约执行：

- Scope/owner 校验和授权边界；
- 任务状态持久化；
- payload 幂等映射；
- TTL 和清理；
- 重启中断后的终止、重试或恢复语义。

JobStateStore 使用的授权 resource 应独立于 Memory Storage 的 `memory_unit`、`raw` 等资源，
不能因为底层也是 KV 就复用错误的资源名；当前 JobStateStore 由 Control 自有契约管理。

当前接口实际提供的是下面这些业务操作（名字以代码为准）：

```text
save(job, scope, owner)
get(job_id, scope, owner)
find_by_payload(payload_id, scope, owner)
delete(job_id, scope, owner)
cleanup(scope, older_than, owner)
```

它的内部 KV adapter 才负责：

```text
JobStateStore → JSON/codec → /ingest/jobs/ 和 /ingest/payloads/ → KVStore
```

因此“JobStateStore”是业务契约，“KV adapter”是实现细节。两者都可以存在，但不能让
`IngestJobController` 同时扮演两种角色。

### 5.4 为什么这样能解决

这样既保留了 KV 作为一种实现选择，又把 Control 的所有权说清楚：

- 未来换 Redis、数据库或外部队列，不改 `IngestJobController`；
- Job 状态的 owner、TTL、清理和重启行为有单一契约；
- 任务状态不会被误认为 Memory 数据，也不会被 E-05 Raw 端口承载；
- 没有 KV capability 的一体化 Memory Storage 也不会阻止 Control 队列启动。

最小验收例子是：把同一个 `IngestJobController` 接到带 TTL 的内存
`JobStateStore`，再接到 `KVJobStateStore`。控制器代码不变，但可以分别测试 TTL、幂等、
Scope/owner 负向和重启策略。若必须修改控制器才能换 adapter，说明边界还没有真正收敛。

### 5.5 F-05 自测

- 为什么 JobStateStore 不能直接复用 `storage.kv`？
- 一个任务的 owner 是目标 Scope，还是提交者 actor？谁来持久化这个事实？
- 进程重启后 pending 任务是重试、失败，还是需要人工恢复？契约不写清楚就无法测试。

---

## 6. “冻结契约”到底是在冻结什么

冻结不是把所有实现细节提前写死，而是先把跨模块都必须遵守的边界写清楚。

可以把契约想成“多人同时施工前的接口图纸”。如果没有图纸：

```text
开发者 A 认为 raw 是 MemoryUnit，开发者 B 认为 raw 是字符串
开发者 A 用完整 Scope，开发者 B 只传 space_id
开发者 A 把拒绝当空列表，开发者 B 抛异常
```

每个人的局部代码都可能看起来合理，合在一起却无法互操作。冻结契约的目的，就是先把
这些跨模块事实定下来，让后面的实现可以并行而不各自发明规则。

### 6.1 Raw Data 契约

至少冻结以下内容：

| 契约项 | 要回答的问题 |
|---|---|
| 记录模型 | 当前临时记录是 `MemoryUnit`，还是新的 `RawRecord`/`RawPayload`？ |
| 操作 | append/list/delete 的输入输出、排序和幂等语义是什么？ |
| Scope | 是否严格使用 org/space/user/agent/session 五维 Scope？ |
| 授权 | access 如何传递？resource/action 如何命名？拒绝是否 fail-closed？ |
| 保留 | “最近 N 条”由谁配置、谁执行、删除失败如何处理？ |
| 编解码 | key prefix、序列化和加密 purpose 是否完全由 Storage 接管？ |
| 治理 | space delete、usage、`scopes()` 如何覆盖独立 Raw backend？ |

为什么这些都要先定？例如只冻结 `list_raw(scope, limit)` 的方法名，却不冻结“按哪个
时间排序”和“拒绝时抛什么异常”，Evolver、SpaceManager、测试仍然会对同一个接口产生
不同理解。实现可以换 KV 或文件，但 Scope、授权、排序和错误语义不能各写一份。

### 6.2 Storage 能力与端口契约

E-05 和 F-02 已经共同修改了这层；现在要做的是把实现中的约定提升为稳定契约，必须一次性
冻结：

- Raw 是 `Storage` 的受权业务端口，是否提供由 `has_raw_port()` 判断；
- `StorageCapability.ENTITY` 与 `StoreType.ENTITY` 已加入统一能力模型；
- default/named port 的命名和未声明能力的错误；
- CompositeStorage、RoutingStorage、授权代理、health 检查如何同步；
- raw/entity 操作分别映射到哪些 `StorageAction` 和 resource；
- 自定义 Storage 如何实现同样能力；
- `scopes()` 必须合并 Raw 独立后端的 Scope，Entity 作为派生索引不作为真源枚举来源；
- Engine、Evolver、IndexBuilder、Recaller 如何获得 `StorageAccessContext`。

这是 E-05 和 F-02 的共同地基。若 Raw 端口和 Entity 端口各自定义 access 传递方式，
后面会出现同一个 `StorageSecurity` 收到两种上下文，或者某一路完全没有上下文。

### 6.3 Entity 契约

至少冻结：

- 五维 Scope 是否全部参与隔离；
- 旧的 `EntityStoreFilters.actor_id` 是否保留为兼容字段；
- 同 user 跨 agent/session 共享是否取消；
- EntityStore 方法的首参、命名空间和批量失败语义；
- 读、搜、写、删、建索引分别对应什么授权动作；
- 无 Elasticsearch 时，一体化 Storage 提供 Entity 的最低能力是什么。

尤其要先决定 Scope 投影：当前代码用 `space_id + actor_id(user)`，而验收要求提到五维
Scope。这个决定会影响 EntityStore 的方法签名、索引字段、查询过滤和负向测试，不能等
到最后再“顺便改一下”。

### 6.4 JobStateStore 契约

至少冻结：

- owner 是 target Scope、提交者 actor，还是二者都持久化；
- submit/status 是否需要显式 access；
- pending/running/succeeded/failed 的状态转换；
- TTL、终态保留期、清理触发方式和幂等性；
- 重启是自动重试、标记失败还是恢复原状态；
- payload_id 映射是否和 job 状态一起清理；
- 哪些 Control adapter 可以直接引用 `KvProducer`。

如果不冻结重启、TTL 和 owner，`_load` 的“pending/running → failed”只是某个实现的
偏好，无法判断它是正确行为，也无法写稳定的恢复测试。

### 6.5 直连 Store 例外白名单

要把“统一走 Storage”和“Control 可直连基础设施”写成一张明确的表：

| 类别 | 所有权 | 上层允许直接引用底层 Store 吗？ |
|---|---|---|
| MemoryUnit、Raw Data、Vector/Fulltext/Graph/FS、Entity | Storage | 不允许；只能经 Storage/端口 |
| JobState | Control / JobStateStore | 只有 adapter 允许；`ingest_job.py` 不允许 |
| Audit、Lock、Checkpoint | 各自的基础设施契约 | 逐项定义 adapter 白名单和生命周期 |
| Space registry | 当前由 SpaceManager 管理 | 明确它是 Storage 端口能力还是 Control 例外 |

冻结这张表，是为了避免不同模块各自推导“哪些 KV 可以直连”。

---

## 7. 为什么必须先冻结再并行

### 7.1 不是因为三项语义互相依赖

E-05、F-02、F-05 的业务语义可以分别理解：

- E-05 解决 Raw 数据面授权和封装；
- F-02 解决 Entity 能力归属和 Scope 隔离；
- F-05 解决 Control 任务状态的基础设施边界。

所以三项的调查、契约草案和测试设计可以并行。

### 7.2 是因为 E-05/F-02 会同时改共享 Storage 核心

历史 Storage 只有六类 capability，CompositeStorage、RoutingStorage 和授权代理也只覆盖六类。
当前工作区已经加入 Entity capability 和 Raw 端口，但如果 E-05/F-02 各自独立修改这些核心文件，
很容易出现：

- 一条分支忘了 RoutingStorage；
- 另一条分支使用了不同的 resource/action 命名；
- 自定义 Storage 或测试桩漏实现新增抽象方法；
- access 传播方式被两边分别设计。

因此要先有一个共同 Storage 基线，再并行迁移各自上层调用方。

### 7.3 F-05 为什么可以更早并行

F-05 不需要 Entity capability，也不需要 Raw port。只要它坚持：

- `JobStateStore` 是 Control 自有契约；
- KV 仅出现在白名单 adapter；
- 不依赖 `storage.kv`；

它就可以和 E-05/F-02 的 Storage 工作并行。它与其他任务真正的冲突主要在
`assembly.py`、`config/defaults.py` 和 S03/S06 文档，适合最后统一接线。

---

## 8. 推荐执行顺序

### 阶段 A：契约草拟（可以并行）

分别完成四份草案：

1. Raw Data 临时兼容契约；
2. Entity 五维 Scope/授权契约；
3. JobStateStore owner/TTL/恢复契约；
4. Storage 端口、access 传播和直连例外白名单。

### 阶段 B：Storage 共享基线（需要串行合并；当前已基本落地）

一次性处理：

- `Storage` capability/port 抽象（当前已有 `ENTITY`、`raw_port`、`entity_port`）；
- CompositeStorage 组合和授权代理（当前已有 Raw/Entity 代理）；
- RoutingStorage 转发（当前已有 Raw/Entity 转发）；
- health、`scopes()` 和自定义 Storage 测试桩；
- Raw/Entity 的 action/resource 映射，以及 access 传播测试。

### 阶段 C：三路实现（可以并行；本轮已完成）

- **E-05**：Raw adapter、Evolver 和 SpaceManager 的 Raw 治理路径已完成；后续继续补充真实
  外部后端、加密以及完整 access 传播的集成覆盖。
- **F-02**：EntityStore Scope 迁移、EntityIndexBuilder、HybridIndexBuilder、KeywordRecaller、
  `StorageCapability.ENTITY` 和授权代理已完成；后续继续补充五维负向测试和无 Elasticsearch
  的自定义 Storage 实现覆盖。
- **F-05**：JobStateStore、KV adapter、IngestJobController、TTL/清理、owner 隔离和重启中断
  语义已完成；后续继续覆盖真实跨进程行为，并维护基础设施 adapter 白名单。

### 阶段 D：统一接线与验收（已完成首轮收口，后续按风险补充覆盖）

本轮已统一修改并检查：

- `assembly.py`、`config/defaults.py`、部署配置；
- S03/S05/S06/S07 及受影响的 AGENTS/F 文档；
- Construction/Retrieval 无 `KvProducer`/`EntityStoreProducer` 直连；Raw 授权拒绝、Scope
  隔离、清理、Entity 代理和 Job TTL/重启语义已有定向测试。真实外部后端和完整调用链
  access 传播仍是后续测试重点。

一句话版本：

```text
契约草拟并行
    → Storage 共享基线串行
    → E-05 / F-02 / F-05 实现并行
    → 装配、文档、全局扫描和集成验收串行
```

---

## 9. 最后一次费曼复述

如果只能用 30 秒向同事解释：

> E-05 是“原文也属于受保护的 Memory 数据，Evolver 不能拿裸 KV”；
> F-02 是“实体索引也属于统一 Storage 能力，不能由 Construction/Retrieval 各自找后端”；
> F-05 是“任务状态可以用 KV，但它是 Control 自己的 JobStateStore，不应把 KV 细节泄漏给任务控制器”。
> 先冻结 Raw、Entity、JobState 和 Storage/例外边界，是因为 E-05 和 F-02 会同时改 Storage 核心，
> 而 access、Scope、owner、TTL、恢复语义一旦后定，就会导致接口反复返工。

如果这段话能解释清楚，再开始并行实现才是安全的。
