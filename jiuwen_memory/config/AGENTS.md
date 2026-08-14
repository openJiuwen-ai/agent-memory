# config/ — 配置层

**规约文档**：[S08-config.md](../../docs/specs/S08-config.md)

> 本文档只记录相对稳定的模块本地规约。特性设计见 `docs/features/config/`。

负责装配配置解析/合并，以及运行时晚绑定配置来源 `ConfigSource`。

## 模块地图

| 文件/目录 | 职责 |
|---|---|
| `config.py` | `Config`：YAML/字典入口 → `AssemblyContext` |
| `context.py` | `AssemblyContext` / `ComponentConfig` / `RawSpec` |
| `defaults.py` | 内置默认装配拓扑与 `ROOT_PARAMS` |
| `config_source.py` | `ConfigSource` ABC + `ConfigSourceProducer` |
| `config_source_impl/` | `yaml_defaults` / `dict` / `overlay` 实现与自注册（当前实现：`yaml_defaults_config_source.py` / `dict_config_source.py` / `overlay_config_source.py`） |
| `keys.py` | 稳定 key 常量与拼接（`globals.*` / `prompts.*` / `<ns>.active`） |
| `project.py` | `AssemblyContext` → 扁平 key→str 投影 |
| `active.py` | `resolve_active_name` / `resolve_bound_value` |
| `binding.py` | 调用路径晚绑定：`resolve_endpoint` / `resolve_connection_url` |
| `routing.py` | `ActiveRouter` + `RoutingEmbedder`/`RoutingLLM`/`RoutingReranker` + `RoutingKVStore`/`RoutingVectorStore`/`RoutingFulltextStore`/`RoutingGraphStore`/`RoutingFusionStore`/`RoutingFSStore`（F01 Store 级）+ `RoutingStorage`（F02：`Storage` 实例动态配置；方案 A 手工注入） |

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

7. **统一 Storage 使用具名共享实例；多套 Storage 动态选用走 RoutingStorage（F02）**
   `storage.default` 选择统一 Storage 实现（可为 `RoutingStorage`）；Retriever 与 Kernel
   都引用该名称。CompositeStorage 的 `kv_store` / `vector_store` / `fulltext_store` /
   `graph_store` 等参数只引用下层具名 Store，不复制连接配置。Construction、Retrieval、Control
   的组件参数只引用 `storage`，不得再直接引用下层 Store 命名空间。
   **禁止**运行时拆换**同一** `CompositeStorage` 实例的内部端口拓扑。
   **允许**装配期预装多套完整 `Storage`，经 `RoutingStorage` + `storage.active` 动态选用（F02）；
   不注册默认 YAML `target: routing`。各实例内部的 Store 级 Routing / url 晚绑定仍归 F01。

8. **安全提供者必须经根引用装配**
   `ROOT_PARAMS["security"]` 指向 `security.default`；`build_kernel` 创建加密 KV 时必须通过该
   具名引用取 provider，确保用户的 `security` 参数可覆盖默认配置。

9. **启用 Encrypted 时 RoutingKV 须在加密层之内**
   EncryptedKV 为 F04 opt-in（`build_kernel` 不强制外包）。产品启用加密时：
   `RoutingKVStore` 挂在 encrypted 的 raw，禁止把 Routing 包在加密层外面（见 F01 §2.1.5 / S08）。
   未启用加密时，`RoutingKVStore` 可直接作为 `kv_store.default`。

## 与其他子目录的边界

**本模块管**：装配合并、ConfigSource 契约与默认实现、active/晚绑定解析辅助、多实例 Routing（`Routing*` / `RoutingStorage`）。

**本模块不管**：业务编排、Policy 策略语义、调用级 options、Store 数据迁移、配置中心 invalidate API。
