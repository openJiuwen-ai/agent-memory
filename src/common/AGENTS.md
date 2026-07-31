# Agent Memory Common（公共组件层）

**规约文档**：[S07-common.md](../../docs/specs/S07-common.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

全局共享的可插拔插件与核心数据类型。提供工厂注册基础设施，定义跨层数据结构，是各层消费的共享依赖。

## 模块地图

| 文件/目录 | 职责 |
|---|---|
| `base.py` | Plugin 基类：所有共享插件的自描述契约 |
| `bootstrap.py` | 统一触发各插件注册（per-layer bootstrap） |
| `errors.py` | 自定义异常（ConflictError/NotFoundError/PermissionDeniedError/BackendError 等） |
| `type_def/` | 核心数据类型定义目录 |
| `type_def/memory.py` | MemoryUnit/Relation/Segment/Temporal/ContentLayers 等；MemoryUnit id 在完整 Scope 内唯一；KV key 前缀 `MEMORY_KEY_PREFIX`/`memory_key`（建索引记忆 `/memory/{id}`）。`ContentLayers`(l0/l1) 为分层披露标注，由 LayerAnnotator 对超阈 content 产出 |
| `type_def/scope.py` | Scope：`org/space/user/agent/session` 五维归属；非空 `space` 是全局唯一的逻辑隔离标识且为 keyword-only，旧位置参数保持 `org/user/agent/session` 顺序 |
| `type_def/filter.py` | FilterClause/FilterGroup/FilterExpr 及 normalize/evaluate；统一 API、检索和存储的树形过滤契约 |
| `type_def/memory_filter.py` | MemoryUnit 字段投影与 FilterExpr 公共求值；供 retrieval 真源复核和 KV list 兼容实现共用 |
| `type_def/memory_codec.py` | `MemoryUnit` ↔ bytes 编解码（`dumps`/`loads`）；当前 `_v=3`，序列化 `layers`({l0,l1}) 与五段 scope，缺失取默认容错老数据，详见 F01-memory-layer / F03-scope-space-isolation |
| `type_def/raw.py` | RawPayload；KV key 前缀 `MESSAGES_KEY_PREFIX`/`messages_key`（未建索引 infer 原文 `/messages/{id}`） |
| `type_def/audit.py` | AuditEvent：记录 actor scope、target scope、action、decision、target_id 与 detail |
| `factory/factory.py` | Factory 基类：`TOP_NAME` 注册 + 三接口 `build`/`build_named`/`dep`（配置数据结构 `ComponentConfig`/`AssemblyContext`/`RawSpec` 在 `config/context.py`） |
| `embedder/` | Embedder 插件目录（接口 + 实现） |
| `chunker/` | Chunker 插件目录 |
| `tokenizer/` | Tokenizer 插件目录 |
| `normalizer/` | Normalizer 插件目录 |
| `feature_extractor/` | FeatureExtractor 插件目录 |
| `llm/` | LLM 插件目录（`echo` / `openai` / `dashscope`） |
| `reranker/` | Reranker 插件目录 |
| `audit/` | AuditLogger 插件目录 |
| `security/` | SecurityProvider 横切接口目录（接口 + `local` ENC1 AES-GCM 实现） |

## 行为铁律

1. **插件接口与实现严格分离**
   接口模块（`<plugin>/base.py`）定义抽象契约 + Producer 工厂类，零依赖实现。实现模块（`<plugin>/<plugin>_impl/*.py`）具体实现 + 尾部 `@XxxProducer.register("name")` 自注册。消费方只 import 接口层，不触达 `*_impl`。

2. **工厂随契约（住在接口层）**
   每个插件的 Producer 工厂定义在其接口模块（`base.py`）中，与抽象契约同处一地。

3. **注册靠 import 触发**
   实现文件尾部 `@XxxProducer.register("name")` 注册 _build 函数，`*_impl/__init__.py` import 各实现模块触发注册，`bootstrap.py::register_plugins()` 在装配前统一触发。

4. **types.py 零依赖其他文件**
   `type_def/*.py` 是纯数据定义，不 import 本层其他文件，被全局共享依赖。

5. **共享插件必须双侧同一**
   Embedder/Tokenizer/FeatureExtractor 必须在构建侧与检索侧使用同一实现/同一配置，保证同词表/同向量空间。靠配置里「具名 + 引用」显式表达共享：双侧 `dep` 引用同一具名实例 → `build_named` 命中同一缓存键 → 同一实例。

6. **业务 metadata 保留原生类型**
   `MemoryUnit` / `RawPayload` / `Chunk` / `Relation` 的 metadata 使用 `dict[str, Any]`；
   不在公共类型层统一 string 化。过滤只做严格类型比较，不推测字符串数值的业务含义。

## 与其他子目录的边界

**本模块管**：
- 共享插件接口定义与注册式工厂
- 核心数据类型（MemoryUnit/Scope/Context/Relation/Chunk/AuditEvent 等）
- 工厂注册基础设施（Factory 基类 + `TOP_NAME` 命名空间 + `build`/`build_named`/`dep` 三接口）
- 横切接口（AuditLogger / SecurityProvider）
- 错误类型
- 工具函数

**不管**：
- 具体算子实现（归各层 `*_impl/`）
- 存储后端实现
- 业务编排逻辑
- 鉴权/策略管理

## 本地约束

1. 所有插件必须实现 `plugin_type()` 和 `health()`（继承自 `Plugin` 基类）。
2. 实现通过 `@XxxProducer.register("name")` 自注册。
3. 新增插件实现：在 `<plugin>_impl/` 下新建文件 → 实现接口 → 尾部注册 → 在 `__init__.py` 添加 import。
4. 重依赖实现在 `*_impl/__init__.py` 中用 `try/except ImportError` 包裹。
5. 两级命名空间配置驱动装配：每个 Producer 声明全局唯一 `TOP_NAME`（占配置顶层段），其下是若干具名实例（`target` 指定实现名、`params` 传参、`new_instance` 控制是否共享）。`_build(config)` 里用 `XProducer.dep(config, param_name=None, default=...)` 取子依赖（引用名→共享 / 内联 dict→匿名 / 缺省→默认匿名）。
6. LLM 的厂商扩展参数必须由对应 Provider Adapter 注入；构建、检索等内核业务调用点不得硬编码 `extra_body` 等传输层字段。
7. SecurityProvider 与 AuditLogger 一样是横切组件，不继承 `Plugin`、不进入 `PluginType`；实现仍通过独立 Producer 与 `*_impl` 自注册。
