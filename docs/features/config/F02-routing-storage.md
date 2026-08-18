# F02 — RoutingStorage：Storage 实例动态配置

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-13 |
| 影响范围 | `jiuwen_memory/config/routing.py`（新增 `RoutingStorage`）、`jiuwen_memory/storage/storage.py` / `StorageProducer`、`jiuwen_memory/api/memory_api_impl/assembly.py`、Engine/Retriever/IndexBuilder 对 `Storage` 的握法；`docs/specs/S08-config.md`、`docs/features/config/F01-config-source.md` §2.1.5、`docs/features/storage/F05-unified-storage-design.md` |
| 测试基线 | `tests/unit/config/test_storage_routing.py`；`examples/config_source_embedder_routing_demo.py` 场景 P·F02 |
| Refs | 承接 F01「按 identity 选用不同存储」；作用对象上移到完整 `Storage` 实例 |
| 状态 | **已落地**（`RoutingStorage` + 惰性端口；单测与 demo 见上） |

---

## 背景

F05 将上层统一到 `Storage` 契约：默认实现是 `CompositeStorage`，产品也可自研一体化 `Storage`（例如对接外部记忆平台）。F01 则在**底层六类 Store** 上提供了：

- 同实现换连接：`kv_store.url` 等晚绑定（主路径）；
- 异质 Store：`Routing*Store` + `*_store.active`（方案 A，可按 identity）。

商用还有更高一层需求：

1. 进程内不止一种 `Storage` 实现——产品要 `@StorageProducer.register` 自研实现（一体化平台、只读镜像、合规隔离实例等）；
2. **运行时**按配置中心 / identity（租户、space、user）在多套**已预装的完整 `Storage` 实例**之间选用，而不是只换某一个 `vector_store`；
3. 与 F01「按用户配不同后端」对齐，但作用对象是整颗 `Storage`，不是单个 Store 端口。

F01 §2.1.5 曾把「`storage.active` / 热换整颗 CompositeStorage」写成非目标——本意是**禁止运行时拆换同一 `CompositeStorage` 内部的端口集合**（多端口同时改、引用重建、隐含迁移）。本特性**不恢复那种原地改拓扑**，而是增加与 `RoutingVectorStore` 同构的 **`RoutingStorage`**：装配期预装多套完整 `Storage`，运行期只改 `storage.active` 选用当前实例。

---

## 目标

| 编号 | 目标 |
|---|---|
| G1 | 产品可 `@StorageProducer.register` 任意 `Storage` 实现（不限于 `composite`） |
| G2 | 提供 `RoutingStorage` + `ActiveRouter[Storage]`，运行时用 `storage.active` 在已预装实例间切换 |
| G3 | 支持按请求 `ConfigSource.bind_identity` 为不同用户配置不同 `storage.active` |
| G4 | 与 F01 分层共存：Store 级 Routing / url 晚绑定仍然有效，职责不重叠 |
| G5 | Engine / Retriever / IndexBuilder 握 `RoutingStorage`（或同一 `Storage` 入口）；禁止构造期把 `.kv`/`.vector` 固化成某一后端的裸引用 |

## 非目标

- 不做 Store 数据迁移、索引重建、跨 `Storage` 实例的事务。
- 不在运行时向 `ActiveRouter` 的 instances **动态追加**未预装的 Storage 名（与 F01「注册 ≠ 预装配」一致）。
- 不把同实现换 DSN/URL 做成多套 `Storage` + `storage.active`（那是 F01 同实例晚绑定）。
- 不强制默认拓扑预装多套 `Storage`；开箱仍是单套 `composite`。
- 不引入 YAML 内置 `storage.target: routing`（方案 A，与 F01 Store Routing 一致）。

---

## 决策

### 决策 1：采用 `RoutingStorage` 做 Storage 实例动态配置

在 `jiuwen_memory.config.routing` 增加：

```text
RoutingStorage(Storage)
  └─ ActiveRouter[Storage](namespace="storage", instances={...}, config_source, default_name)
```

- 每次领域操作（`add`/`get`/`list`/…）与端口访问（`kv`/`vector`/…）均先 `router.get()`，再委托当前 active 实例。
- `storage.active` 的值必须是装配期已放入 `instances` 的具名键；未知则 `ValidationError`（与既有 `ActiveRouter` 一致）。
- ConfigSource key：`storage.active`（稳定后缀；多租户前缀规则同 F01 §2.1.6）。

### 决策 2：与 F01 的两层 Routing 并存（不冲突）

```text
                    ┌─────────────────────────────────────┐
请求 identity       │ ConfigSource（可按 tenant/space/user）│
                    └─────────────────────────────────────┘
                         │ storage.active          │ *_store.active / *.url
                         ▼                         ▼
              ┌──────────────────┐      ┌─────────────────────────┐
              │ RoutingStorage   │      │ 各 Storage 实例内部        │
              │ (本特性 F02)      │      │ · Routing*Store (F01)     │
              └────────┬─────────┘      │ · Redis url 晚绑定 (F01)  │
                       │ get()          └─────────────────────────┘
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     composite_a   composite_b   integrated_x
     (完整 Storage) (完整 Storage) (完整 Storage)
```

| 诉求 | 用哪一层 | 配置 |
|---|---|---|
| 换 Redis 集群连接（同实现） | F01 同实例晚绑定 | `kv_store.url` |
| 同一 Composite 内 pgvector↔milvus | F01 `RoutingVectorStore` | `vector_store.active` |
| 在多套完整 Storage 实例间切换（如 Composite ↔ 一体化实现） | **F02 `RoutingStorage`** | `storage.active` |
| 用户 A / 用户 B 使用不同预装 Storage 实例 | F02 + identity | 配置中心按 identity 写 `storage.active` |

**选用规则（产品指南）**：

1. 能 F01 晚绑定解决的，不用 Routing。
2. 只换某一类 Store 实现、仍共享同一 Composite → F01 `Routing*Store`。
3. 要换的是整颗 `Storage` 实现，或两套数据互不可见的完整实例 → F02 `storage.active`。
4. **禁止**叠用：不要为「换 url」再拆两套 `Storage`；也不要在已用 `storage.active` 区分的两套 Composite 里，再重复表达同一套 Store 级异质诉求（除非两套实例内部各自仍需独立的 Store 级 Routing）。

### 决策 3：方案 A 装配（与 F01 Store Routing 同构）

默认**不**注册 `StorageProducer` 的 `target: routing`。产品侧：

```text
1. @StorageProducer.register("composite_ops") / "integrated_compliance" / …
   或复用内置 "composite" 多次 build_named 得到多实例（见下）
2. @StorageProducer.register("demo_routing_storage")  # 产品自选名
   def _build(config):
       cs = ConfigSourceProducer.get_cached("default")
       return RoutingStorage(ActiveRouter(
           namespace="storage",
           instances={
               "ops": StorageProducer.build_named("composite_ops", …),
               "compliance": StorageProducer.build_named("integrated_compliance", …),
           },
           config_source=cs,
           default_name="ops",
       ))
3. YAML: storage.default.target = demo_routing_storage
4. build_kernel：ConfigSource → 各 Storage 实例 → `storage.default=RoutingStorage`
   （Encrypted 为 F04 opt-in：启用时各实例内 RoutingKV 仍须在加密层内；
    不要在 RoutingStorage 外包一层 Encrypted）
5. Engine / Retriever → StorageProducer.resolve → 得到 RoutingStorage
```

多套 `composite` 变体：允许同一 target 构建多个**具名**实例（不同 params / 不同下层 `*_store` 引用），再放入 `RoutingStorage` 的 instances。具名实例名即 `storage.active` 可取值。

### 决策 4：上层须跟随 `storage.active`（修正「构造期握死端口」）

F01 真值表写明 Recaller / IndexBuilder 常在构造期取 `vector_port` 等**固定引用**。若该引用指向某一 `CompositeStorage.vector` 的具体后端，则切换 `storage.active` 后仍打到旧实例——**F02 失效**。

本特性强制：

| 组件 | 允许握的引用 | 禁止 |
|---|---|---|
| Engine / Kernel | `Storage`（实际可为 `RoutingStorage`） | 握某一 active 实例的裸 `Storage` |
| IndexBuilder / Dedup / Evolver | `Storage`；**每次**操作经 `storage.vector` / `storage.kv` 访问 | `__init__` 里 `self._vector = storage.vector` 且跨请求复用（当 default 为 RoutingStorage 时） |
| Recaller | 同左；或握会随 `RoutingStorage` 重解析的端口 | 构造期缓存具体 `MilvusVectorStore` |

落地实现选项（择一写入代码，优先 a）：

- **(a) 推荐**：改造 IndexBuilder/Recaller：成员保存 `self._storage`，`build`/`search` 内使用 `self._storage.vector`（属性每次经 `RoutingStorage` → `get().vector`）。
- **(b)**：`RoutingStorage.vector` 返回惰性端口代理（每次方法调用再 `get()`）；改造面更小，但代理须覆盖 `VectorStore` 全契约。

无论 a/b，单测必须覆盖：「构造后切换 `storage.active`，下一次 add/index/search 打到新实例」。

### 决策 5：EncryptedKV 与装配顺序

- EncryptedKV 为 **F04 opt-in**（`258f398` 起 `build_kernel` 不强制外包）。
- **每个** Storage 实例若启用加密：其 `kv` 仍遵守 F01——`RoutingKVStore`（若有）在 Encrypted **之内**。
- `RoutingStorage` **从不**外包一层 Encrypted（加密属于各实例内部的 KV，不属于 Routing 这一层）。
- 建议装配顺序：

```text
1. ConfigSourceProducer.dep → put("default")
2. 构建各具名 Storage 实例（各实例内部：KvProducer →[可选 Encrypted]→ Composite/自研）
3. 构建 RoutingStorage(instances=各实例) → StorageProducer.put("default", routing)
4. Engine / Retriever / … → resolve storage.default
```

### 决策 6：修订 F01 / S08 非目标表述（消除冲突）

F01 / S08 原句「不提供 `storage.active`」收窄为：

> **禁止**：运行时拆换**同一** `CompositeStorage` 实例的内部端口集合（没有 `RoutingStorage`、原地改拓扑）。  
> **允许（F02）**：装配期预装多套完整 `Storage`，经 `RoutingStorage` + `storage.active` 动态选用。

Store 级 `Routing*Store`、同实现晚绑定、方案 A、不迁移数据等 F01 约束**保持不变**。

### 决策 7：能力与 pipeline 语义

- `capabilities()` / `preferred_retrieval_pipeline()`：委托**当前** active 实例；切换后可能变化，调用方（Retriever）须按次读取或容忍变化。
- 某实例缺少能力（如无 graph）时，行为与单套 Storage 一致（如 `UnsupportedStorageCapabilityError`），不因经过 `RoutingStorage` 而吞掉错误。

---

## 拒绝的方案

| 方案 | 拒绝原因 |
|---|---|
| 仅装配期改 `storage.default.target`、运行时不切换（原方案 2） | 无法满足「运行时 / 按用户不同 Storage」；与 F01 identity 故事不一致 |
| 原地热换同一 Composite 的 kv/vector 字段 | F01 已否定；引用重建与迁移语义不清 |
| 默认 YAML `target: routing` | 与 F01 方案 A 不一致；默认拓扑不应预装多后端 |
| 用多进程多内核代替进程内 RoutingStorage | 运维重；无法在同进程按请求 identity 低成本切换 |
| 只扩展 `*_store.active`、不提升到 Storage | 无法表达一体化 `Storage` 实现与「多套完整实例」切换 |

---

## 与相关文档的关系

| 文档 | 关系 |
|---|---|
| F01-config-source | 本特性是其 **Storage 实例层**延伸；底层 Store Routing / 晚绑定仍归 F01。落地时修订 §2.1.5 措辞（见决策 6） |
| F05-unified-storage | 仍成立：`Storage` 是契约，`CompositeStorage` 是默认实现；本特性增加常见形态：`RoutingStorage`（按 `storage.active` 选用已预装实例，不是第三种后端算法） |
| S08-config | 增加 `storage.active` 契约与 RoutingStorage 方案 A；收窄原「不做 storage.active」边界 |
| S06/S07（若存在 Storage 规约） | 不改变 `Storage` ABC 方法集；仅增加可选的 `RoutingStorage` 与上层握法约束 |

---

## 产品装配示意（用户故事）

```text
用户 A（ops = Composite，内部可有 F01 RoutingVector）
用户 B（compliance = 自研 IntegratedStorage）

storage.default → RoutingStorage({
    ops: CompositeStorage(...),
    compliance: IntegratedStorage(...),
})

运行时（ConfigSource + bind_identity）:
  identity=A → storage.active=ops
  identity=B → storage.active=compliance

同一用户切换所用实例（运维窗口）:
  put(identity, {storage.active: compliance})  # 无迁移；旧数据仍在旧实例
```

样例：`examples/config_source_embedder_routing_demo.py` **场景 P** 末尾 F02 小节——双
`CompositeStorage` 实例 + `RoutingStorage` + identity/`storage.active`；同场景前半仍回归 F01。

---

## 验证（落地基线）

| # | 基线 | 落点 |
|---|---|---|
| 1 | `RoutingStorage` + 双实例装配；领域 `add`/`list` 随 `storage.active` | `tests/unit/config/test_storage_routing.py`；demo P·F02 |
| 2 | 切换后数据互不可见（无迁移） | 同上 |
| 3 | 两 identity 交替 `bind_identity` 各写各实例 | demo P·F02（user_A/ops vs user_B/compliance） |
| 4 | 与 F01 共存：场景 P 前半 `kv_store.url` + `vector_store.active` | demo P·F01 |
| 5 | 未知 `storage.active` → `ValidationError` | `test_routing_storage_unknown_active_raises` |
| 6 | 构造期缓存的 `.vector`/`.kv` 仍随 active（惰性代理） | `test_lazy_port_follows_active_after_construct_cache`；demo `cached_kv` |

---

## 已知遗留

- 首版采用决策 4 **选项 b**（`RoutingStorage` 惰性端口代理），未强制改造 IndexBuilder/Recaller 握 `self._storage`（选项 a 仍为长期推荐）。
- 未对 `StorageProducer.resolve` 增加「禁止二次包裹 RoutingStorage」防护。
- 多实例可观测性（audit 是否记录 `storage.active`）留给产品；内核首版只保证切换生效。
- 未注册内置 YAML `storage.target: routing`（方案 A，有意为之）。

---

## 附录：落地时对 F01 §2.1.5 / S08 的修订要点（摘录）

**F01「明确不做」原句**替换为：

- 禁止：无 `RoutingStorage` 时，运行时拆换同一 `CompositeStorage` 内部端口拓扑。
- 允许：F02 `RoutingStorage` + `storage.active` 在预装完整 `Storage` 实例间动态选用。
- 真值表新增一行：Storage 实例动态配置 → `storage.default = RoutingStorage`；错误挂载点 = 企图原地改同一 composite 的端口字段。

**S08** 同步收窄，并增加：

- `storage.active`：仅当 `storage.default` 为产品注入的 `RoutingStorage` 时有效。
- 不变量补充：上层共享的 `storage.default` 可以是 `RoutingStorage`；其 instances 均须为完整 `Storage`。
