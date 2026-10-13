# config/ — 配置层

**规约文档**：[S08-config.md](../../docs/specs/S08-config.md)

> 本文档只记录相对稳定的模块本地规约。特性设计见 `docs/features/config/`。

负责装配配置解析/合并，以及运行时晚绑定配置来源 `ConfigSource`。

## 模块地图

| 文件/目录 | 职责 |
|---|---|
| `config.py` | `Config`：YAML/字典入口 → `AssemblyContext`（`from_yaml` 读文件、`from_yaml_str` 读文本，均展开 `${VAR}` / `${VAR:-默认}`，优先级 显式传参（`None` 视同未传）> 环境变量 > 默认值 > 空串；只支持单层占位符，嵌套与含 `}` 的默认值抛 `ValidationError`；`from_dict` 不展开） |
| `context.py` | `AssemblyContext` / `ComponentConfig` / `RawSpec` |
| `defaults.py` | 内置默认装配拓扑与 `ROOT_PARAMS` |
| `encryption.py` | PR1-1 的 AtRestLocation / EncryptionPolicy / deployment_bindings / EncryptionBoundConfigSource：严格装配期开关、消费插件自注册的 at_rest_locations 核查部署声明及受保护位置变更拒绝 |
| `config_source.py` | `ConfigSource` ABC + `ConfigSourceProducer` |
| `config_source_impl/` | `yaml_defaults` / `dict` / `overlay` 实现与自注册（当前实现：`yaml_defaults_config_source.py` / `dict_config_source.py` / `overlay_config_source.py`） |
| `keys.py` | 稳定 key 常量与拼接（`globals.*` / `prompts.*` / `<ns>.active`） |
| `project.py` | `AssemblyContext` → 扁平 key→str 投影 |
| `active.py` | `resolve_active_name` / `resolve_bound_value` |
| `binding.py` | 调用路径晚绑定：`resolve_endpoint` / `resolve_connection_url` |
| `routing.py` | `ActiveRouter` + `RoutingEmbedder`/`RoutingLLM`/`RoutingReranker` + `RoutingKVStore`/`RoutingVectorStore`/`RoutingFulltextStore`/`RoutingGraphStore`/`RoutingFusionStore`/`RoutingFSStore`（F01 Store 级）+ `RoutingStoreManager`/`RoutingDomainStore`（F02/F07：StoreManager 实例动态配置，`store_manager.active`；方案 A 手工注入） |

## 行为铁律

1. **默认可运行**  
   未自定义时默认 `config_source.target=yaml_defaults`，行为对齐 `defaults.py` + 用户 YAML 合并结果。

2. **同实现凭证优先晚绑定**  
   model/api_key/base_url/url 在 Embedder/LLM/Reranker/Store 调用或取连接路径 `fetch`；`*.active` 仅用于异质实现互切。
   **禁止**为同实现换 Redis URL / sqlite `db_path` 预装 `Redis_1`/`Redis_2` 等多具名实例再切 `kv_store.active`（装配期猜集群数；后来的集群应 `put(kv_store.url)`）。

3. **A/B 分离**  
   换 ConfigSource 实现或增减预装实例 = 装配/重建（A）；已注入来源上 `fetch` / 改 `*.active` = 运行时（B）。

4. **注册 ≠ 预装配**  
   Producer 已注册的 target 不等于进程内有实例；`*.active` 只能指向装配期已创建的具名实例，未知名抛 `ValidationError`。

5. **配置不进业务入参**  
   prompt 全文、api_key、base_url、Store 连接、能力开关的写入不经 `add`/`search`/`evolve`；`DictConfigSource.put` 仅供产品/配置中心侧。

6. **与 PolicyManager 边界**  
   lifecycle / `scope.require_space` 等已知策略键仍走 `PolicyManager`；六类动态配置走 `ConfigSource`。

7. **全局唯一 StoreManager 由 globals 指名；多套 manager 动态选用走 RoutingStoreManager（F02/F07）**
   `store_manager.default` 是统一 manager 实现（配置段名 F07 起从 `storage` 更名）；全局实例由
   `globals.store_manager` 指名，消费者不再逐个声明 storage 引用，统一经
   `StoreManagerProducer.resolve` 解析。manager 的 `kv_store` / `vector_store` / `fulltext_store` /
   `graph_store` 等端口由各命名空间的已声明实例全量聚合，manager params 不决定端口；
   `domain_stores` 段声明命名数据面及其真源/召回选择。Construction、Retrieval、Control 的组件参数经
   `params.<ns>_store` / `params.domain_store` 指名端口/数据面，不得直接引用 Store Producer。
   **禁止**运行时拆换**同一** `CompositeStoreManager` 实例的内部端口拓扑。
   **允许**装配期预装多套完整 manager，经 `RoutingStoreManager` + `store_manager.active`
   动态选用（F02/F07）；不注册默认 YAML `target: routing`。各实例内部的 Store 级 Routing /
   url 晚绑定仍归 F01。

8. **密码学提供者必须经具名引用装配**
   `security` 顶层段属于 `SecurityRuntimeProducer`；加密存储通过
   `params.cryptography` 指向 `cryptography.<name>`，Runtime 若需要同一 provider 也引用同名
   实例。`build_kernel` 先建立共享 Factory 缓存，Runtime 随后命中该缓存，禁止把同名有状态
   provider 构造成两份。旧 `security.*.target=local` / `params.security` 只在一个发布周期内由
   `AssemblyContext` 自动迁移并告警。

9. **启用 Encrypted 时 RoutingKV 须在加密层之内**
   EncryptedKV 支持显式 opt-in；统一开关开启时由 manager 装配保护全部 KV/FS。启用加密时：
   `RoutingKVStore` 挂在 encrypted 的 raw，禁止把 Routing 包在加密层外面（见 F01 §2.1.5 / S08）。
   未启用加密时，`RoutingKVStore` 可直接作为 `kv_store.default`。

10. **全后端静态加密是装配期开关**
    `globals.storage_encryption_enabled` 默认 false，严格接受 bool 或 true/false 字符串。
    开启后全部持久化索引及文件型 SQLite audit/permission（含 space_aware）须逐实例声明
    at_rest 的 method/reference/bindings；豁免必须由该插件的 at_rest_locations 明确声明。
    无检查器、同名插件替换后未重声明或未知位置均拒绝，不维护配置层 target 白名单。
    自动 KV/FS 包装可通过 storage_encryption_kv / storage_encryption_fs 选择 Producer target。
    绑定值须匹配实际配置；ConfigSource 初始覆盖或热切换受保护位置均拒绝，重核查后重装配。
    AtRestLocation 将位置约束与部署证明分开；SQLite :memory: 仅免证明，db_path 仍须固定，
    具名键和共享键均检查，豁免位置也参与多实例共享键冲突检查（契约见 S08）。
    关闭统一开关不拆除显式 encrypted target，不提供混读明文或自动数据迁移。

11. **Schema 是装配期开关**
    `globals.schema_enabled` 默认 `false`；它只在 `build_kernel` 装配期决定是否注册
    Schema target，不是可经 ConfigSource 热切换的运行时能力开关。改值后必须重新装配。

## 与其他子目录的边界

**本模块管**：装配合并、ConfigSource 契约与默认实现、active/晚绑定解析辅助、多实例 Routing（`Routing*` / `RoutingStoreManager`/`RoutingDomainStore`）。

**本模块不管**：业务编排、Policy 策略语义、调用级 options、Store 数据迁移、配置中心 invalidate API。
