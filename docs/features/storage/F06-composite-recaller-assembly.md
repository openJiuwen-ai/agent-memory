# CompositeStorage 召回路装配内收

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-31 |
| 影响范围 | jiuwen_memory/storage/storage_impl/composite_storage.py，jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py，jiuwen_memory/config/defaults.py，docs/specs/S04-retrieval.md，docs/specs/S06-storage.md |
| 测试基线 | `.venv-wsl/bin/python -m pytest tests/unit tests/integration`：1318 passed / 82 skipped；4 failed 均为 HEAD 既有（2 个 bge_m3 缺 torch；2 个 entity_linker 测试间干扰，干净检出复现） |

## 背景

统一 Storage 落地后（F05-unified-storage-design），`PipelineRetriever` 的生产装配仍按
旧世界做事：retriever 工厂自己组装 keyword/vector/graph/L0/L1 各路 Recaller，再
`isinstance(storage, CompositeStorage)` 判断后 `bind_recallers` 塞回 storage。问题：

1. **责任错位**：recall/get/rank 三条首选路径的执行都在 Storage 内，唯独召回路的
   组装留在 Retriever 工厂——`CompositeStorage` 的能力开关（`vector_enabled` 等）要
   由别人替它解释。
2. **非 Composite 实现被拖进无关概念**：一体化 Storage 自带检索路径，根本不需要
   Recaller；旧装配却仍先组装一整组 recaller 再发现无处可绑。
3. `PipelineRetriever` 构造签名被迫携带 `recallers` 参数，并在构造期做「无 storage
   就用 unit_reader.kv 临时拼一个 CompositeStorage 再绑 recaller」的兼容逻辑。

## 决策

1. **召回路装配内收到 `CompositeStorage` 工厂**：`@StorageProducer.register("composite")`
   的 `_build` 按配置组装 recaller——能力开关 `vector_enabled` / `graph_enabled` /
   `layers_index_enabled`（`config.get` 回退 globals），recaller 选择键
   `keyword_recaller` / `vector_recaller` / `graph_recaller` / `keyword_l0_recaller` /
   `keyword_l1_recaller` / `vector_l0_recaller` / `vector_l1_recaller`。这组键相应从
   `defaults.py` 的 `retriever.default.params` 移到 `storage.default.params`。
2. **构建期同步组装 + 双预注册打破循环**：recaller builder 内部会
   `StorageProducer.resolve(config)` 回取本 Storage 实例，而 `build_named` 没有
   「构建中」检测——装配期直接同步组装会无限递归（recaller 回取触发再建一个
   CompositeStorage）。故 `_build` 先把构建中的实例预注册进具名缓存再组装：
   - **具名构建**（`config.name` 非空）：`StorageProducer.put(config.name, storage)`
     预注册，recaller 命名空间下声明的具名实例（`recaller.keyword` 等）的 `storage`
     字段是字符串引用，`RecallerProducer.dep` 走 `build_named` 命中缓存打破循环。
   - **匿名构建**（无 `config.name`，如 `IndexBuilderProducer` 调
     `StorageProducer.resolve` 落到第三分支构建的 CompositeStorage）：无缓存键，
     `dep` 缺省回落到 `cls.build(default, {}, ctx)` 触发 recaller builder 用空 params
     再走 `StorageProducer.resolve` 落第三分支再建一个匿名 CompositeStorage → 递归。
     `_assemble_recallers` 用合成名（`__anon_storage_{id(storage)}__`，`id` 保证唯一）
     预注册本实例，改走 `RecallerProducer.build(target, {"storage": synthetic_name}, ctx)`
     直接注入 storage 引用，让 builder 内 `resolve` 走第一分支（`cls.dep`）命中合成名
     缓存打破循环。
   装配错误构建期 fail-fast 暴露（选择键指向未注册实现、必填插件缺失等），不拖到首次召回。
3. **`PipelineRetriever` 不再持有召回路**：构造签名删除 `recallers` 参数与
   `recallers` 属性；`retrieve` 只按 `storage.preferred_retrieval_pipeline()` 委托
   `recall` / `recall_and_get` / `retrieve`。非 Composite 实现不再被拖进 recaller
   概念。`storage=None` 回退保留为 `CompositeStorage(kv=unit_reader.kv)`（无召回路，
   仅供只走点读/短路路径的调用）。
4. **`bind_recallers` 保留为手工/测试接线口**：recaller 构造需要 storage 实例
   （`KeywordRecaller(storage)`），手工接线仍是「先建 storage、再建 recaller、再
   bind」；绑定后惰性 loader 作废。重绑守卫不变（不允许绑定两套不同实例）。
5. **模块依赖方向不破**：`composite_storage.py` 只在 `_assemble_recallers` 函数体内
   惰性 `from jiuwen_memory.retrieval.recaller import RecallerProducer`，模块层面
   storage 仍不导入 retrieval。

## 拒绝的方案

- **装配期同步构建 recaller（仅 `StorageProducer.put` 预注册打破循环）**：`_build` 里
  先 `put(config.name, storage)` 再组装，具名路径可行，但匿名 `build`（无
  storage 段的自定义配置走 `StorageProducer.resolve` 回退）仍会经 recaller builder
  回取 `storage.default` 递归构建默认实例。本方案最终被采纳，但补上了合成名预注册
  + 直接 `RecallerProducer.build` 注入 storage 引用覆盖匿名路径。
- **首次召回时惰性物化（`_build` 只挂 `recaller_loader` 回调）**：能彻底回避循环，
  但 recaller 装配错误（如选择键指向未注册实现）会推迟到首次召回才暴露，违背
  「配置错误尽早暴露」原则；`test_recaller_assembly_error_surfaces_at_build_time`
  显式要求构建期 fail-fast。惰性物化也增加了首次召回延迟与状态管理复杂度。
- **保留 retriever 侧装配、仅按 `isinstance` 跳过非 Composite**：责任依然错位，
  `vector_enabled` 等开关仍由 retriever 工厂替 storage 解释；治标不治本。
- **让 recaller 不再依赖 Storage（改注入裸端口）**：可以彻底消除循环，但要改全部
  recaller 的构造契约与测试，且违背「上层握共享 Storage 入口、勿构造期握死裸端口」
  （storage/AGENTS.md 本地约束 14，RoutingStorage 惰性代理依赖这一点）。
- **召回路用命名空间扫描（枚举 recaller 命名空间全部实例）替代显式选择键**：
  失去按配置裁剪/替换单路的能力，且会把未接线入库的具名实例也拉进来。

## 验证

- `tests/unit/retrieval/test_storage_factory_wiring.py`：默认装配下
  `retriever.storage is storage` 且 `storage.recallers` 同步组装出 7 路；globals 关闭
  `vector_enabled`/`graph_enabled`/`layers_index_enabled` 后只剩 keyword 一路；
  `test_recaller_assembly_error_surfaces_at_build_time` 验证选择键指向未注册实现时
  `build_named` 在构建期抛 `ValidationError`（具名路径 fail-fast）。
- `tests/unit/construction/test_index_builder.py`：`test_fulltext_factory_*`、
  `test_vector_factory_*`、`test_unified_factory_*` 覆盖匿名 CompositeStorage 构建
  （经 `StorageProducer.resolve` 第三分支），合成名预注册打破循环、不触发递归。
- `tests/unit/retrieval/test_storage_pipelines.py`、`tests/unit/retrieval/test_recallers.py`、
  `tests/integration/retrieval/test_pipeline_retriever.py`（含 `keyword_recaller` 覆盖键
  移到 storage 段的用例）、`tests/integration/retrieval/test_index_contract.py` 全部适配
  并通过；`tests/conftest.py` 的 `make_world` 改为 bind 手工接线。

## 已知遗留

- recaller 装配的**运行时**错误（如距离型度量向量库被 `VectorRecaller` 在首次召回
  时拒绝、向量库连接不通等）仍推迟到首次召回才暴露；装配期 fail-fast 只覆盖选择键
  指向未注册实现、必填插件缺失等配置类错误。运行时错误依赖冒烟/健康检查覆盖。
- 匿名构建用 `id(storage)` 派生合成名预注册进 `StorageProducer._instances` 缓存，
  装配后该缓存条目无人再引用（recaller 已持直接引用），属无害死条目；进程内多
  次匿名构建会累积少量死条目，但 `Factory.reset_all` 在每次装配前清空。
- 用户自定义配置里写在 `retriever.*.params` 的 `*_recaller` 覆盖键不再生效，需迁移到
  `storage.*.params`（globals 里的能力开关不受影响）。


## 后续演进

- [F07-storage-manager-domain-store-split.md](F07-storage-manager-domain-store-split.md)（合并
  原 F07/F08/F09）：本文的召回
  装配链路整体保留（仍由 manager `_build` 工厂末尾调 `_assemble_recallers`），仅符号随
  拆分更名（`CompositeStorage` → `CompositeStoreManager`/`CompositeDomainStore`、
  `StorageProducer.resolve` → `StoreManagerProducer.resolve`、合成名
  `__anon_storage_{id}__` → `__anon_store_manager_{id}__`、recaller 具名实例的 storage 引用
  键改为 `store_manager`）。
