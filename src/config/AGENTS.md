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
| `routing.py` | `ActiveRouter` + `RoutingEmbedder`/`RoutingLLM`/`RoutingReranker`（异质实例次选） |

## 行为铁律

1. **默认可运行**  
   未自定义时默认 `config_source.target=yaml_defaults`，行为对齐 `defaults.py` + 用户 YAML 合并结果。

2. **同实现凭证优先晚绑定**  
   model/api_key/base_url/url 在 Embedder/LLM/Reranker/Store 调用或取连接路径 `fetch`；`*.active` 仅用于异质实现互切。

3. **A/B 分离**  
   换 ConfigSource 实现或增减预装实例 = 装配/重建（A）；已注入来源上 `fetch` / 改 `*.active` = 运行时（B）。

4. **注册 ≠ 预装配**  
   Producer 已注册的 target 不等于进程内有实例；`*.active` 只能指向装配期已创建的具名实例，未知名抛 `ValidationError`。

5. **配置不进业务入参**  
   prompt 全文、api_key、base_url、Store 连接、能力开关的写入不经 `write`/`recall`/`evolve`；`DictConfigSource.put` 仅供产品/配置中心侧。

6. **与 PolicyManager 边界**  
   lifecycle / `scope.require_space` 等已知策略键仍走 `PolicyManager`；六类动态配置走 `ConfigSource`。

## 与其他子目录的边界

**本模块管**：装配合并、ConfigSource 契约与默认实现、active/晚绑定解析辅助、多实例路由门面。

**本模块不管**：业务编排、Policy 策略语义、调用级 options、Store 数据迁移、配置中心 invalidate API。
