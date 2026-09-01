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
| `_support.py` | 跨层共用的小工具：配置值布尔归一（`as_bool`）、SSL 配置读取与装配期校验（`SslConfig`/`build_ssl_config`/`require_tls_scheme`/`require_ca_file`/`outbound_verify`/`read_ssl_config`/`reject_url_tls_params`）、scope 命名空间渲染（`SCOPE_DIMS`/`scope_segments`）、后端异常归一（`wrap_backend`）；storage、lock 与出站客户端共用，避免各写一份 |
| `type_def/` | 核心数据类型定义目录 |
| `type_def/memory.py` | MemoryUnit/Relation/Segment/Temporal/ContentLayers 等；MemoryUnit id 在完整 Scope 内唯一；KV key 前缀 `MEMORY_KEY_PREFIX`/`memory_key`（建索引记忆 `/memory/{id}`）。`ContentLayers`(l0/l1) 为分层披露标注，由 LayerAnnotator 对超阈 content 产出 |
| `type_def/scope.py` | Scope：`org/space/user/agent/session` 五维归属；非空 `space` 是全局唯一的逻辑隔离标识且为 keyword-only，旧位置参数保持 `org/user/agent/session` 顺序。另有 `KERNEL_COORD_KEYS`——内核自带的归属坐标实体名，三项取值必须是 `Scope` 的字段名，故与该类同处 |
| `type_def/filter.py` | FilterClause/FilterGroup/FilterExpr 及 normalize/evaluate；统一 API、检索和存储的树形过滤契约 |
| `type_def/memory_filter.py` | MemoryUnit 字段投影与 FilterExpr 公共求值；供 retrieval 真源复核和 KV list 兼容实现共用 |
| `type_def/memory_codec.py` | `MemoryUnit` ↔ bytes 编解码（`dumps`/`loads`）；当前 `_v=4`，分别序列化 `system_metadata` / `user_metadata`，拒绝未迁移的 `_v<4` MemoryUnit |
| `type_def/raw.py` | RawPayload；KV key 前缀 `MESSAGES_KEY_PREFIX`/`messages_key`（未建索引 infer 原文 `/messages/{id}`） |
| `type_def/audit.py` | AuditEvent：记录 actor scope、target scope、action、decision、target_id 与 detail |
| `factory/factory.py` | Factory 基类：`TOP_NAME` 注册 + 三接口 `build`/`build_named`/`dep`（配置数据结构 `ComponentConfig`/`AssemblyContext`/`RawSpec` 在 `config/context.py`） |
| `embedder/` | Embedder 插件目录（接口 + 实现） |
| `chunker/` | Chunker 插件目录 |
| `tokenizer/` | Tokenizer 插件目录 |
| `normalizer/` | Normalizer 插件目录（passthrough / routing / video）；视频 ASR 支持 OpenAI transcription 与 DashScope filetrans |
| `feature_extractor/` | FeatureExtractor 插件目录 |
| `llm/` | LLM 插件目录（`echo` / `openai` / `dashscope`） |
| `reranker/` | Reranker 插件目录 |
| `audit/` | AuditLogger 插件目录 |
| `security/` | 安全域唯一归属地：F05 契约层（`types.py` 公共值对象、`authentication/` / `authorization/` / `cryptography/` / `protection/` 各能力 base、`request_context.py` 受控构造入口、`runtime.py`）+ 旧 `SecurityProvider` 横切接口（接口 + `local` ENC1 AES-GCM 实现）+ 过渡桥 `legacy.py`。`*_impl` 实现包暂缓合入（接口先行，见 `docs/features/common/F05-security-api-contracts.md`）。另含空间级授权判据：`space_roles.py` 两轴角色与动作矩阵、`space_decision.py` 判定链纯函数、`principal.py` 主体推导与作者标记及内核归属坐标折算、`space_predicates.py` 检索两族系统谓词的生成（收 `actor`、不访问存储，与 `space_decision.py` 的分工：后者判能否进入空间，前者定进入后可见哪些条目）（见 `docs/features/control/F07-collective-memory-design.md`） |
| `lock/` | LockProvider 横切接口目录：跨实例互斥原语（接口 + `redis` / `memory` 实现）。**common 层唯一的异步契约**，只交付原语、不在业务路径加锁，见 [F06-distributed-lock.md](../../docs/features/common/F06-distributed-lock.md) |

## 行为铁律

1. **插件接口与实现严格分离**
   接口模块（`<plugin>/base.py`）定义抽象契约 + Producer 工厂类，零依赖实现。实现模块（`<plugin>/<plugin>_impl/*.py`）具体实现 + 尾部 `@XxxProducer.register("name")` 自注册。消费方只 import 接口层，不触达 `*_impl`。
   唯一例外是 `LockProvider`：`acquire`/`release`/`guard` 在接口层落实现，只抽象后端原语。重入记账与 guard 组合是契约级行为而非后端细节，下沉会在两个实现里分叉。新增组件不得援引此例外，除非同样能论证「行为属于契约本身」。

2. **工厂随契约（住在接口层）**
   每个插件的 Producer 工厂定义在其接口模块（`base.py`）中，与抽象契约同处一地。

3. **注册靠 import 触发**
   实现文件尾部 `@XxxProducer.register("name")` 注册 _build 函数，`*_impl/__init__.py` import 各实现模块触发注册，`bootstrap.py::register_plugins()` 在装配前统一触发。

4. **types.py 零依赖其他文件**
   `type_def/*.py` 是纯数据定义，不 import 本层其他文件，被全局共享依赖。

5. **共享插件必须双侧同一**
   Embedder/Tokenizer/FeatureExtractor 必须在构建侧与检索侧使用同一实现/同一配置，保证同词表/同向量空间。靠配置里「具名 + 引用」显式表达共享：双侧 `dep` 引用同一具名实例 → `build_named` 命中同一缓存键 → 同一实例。

6. **MemoryUnit metadata 双命名空间**
   `MemoryUnit` / `RawPayload` 只使用 `system_metadata` 和 `user_metadata`，共用
   `MetadataValueType`；系统不解释用户命名空间。`Chunk` / `Relation` 等独立模型的
   `metadata` 保持自身契约，不机械改名。

## 与其他子目录的边界

**本模块管**：
- 共享插件接口定义与注册式工厂
- 核心数据类型（MemoryUnit/Scope/Context/Relation/Chunk/AuditEvent 等）
- 工厂注册基础设施（Factory 基类 + `TOP_NAME` 命名空间 + `build`/`build_named`/`dep` 三接口）
- 横切接口（AuditLogger / SecurityProvider / LockProvider）
- 安全域契约（认证/密码学/保护的抽象接口与公共安全值对象；接口先行，实现暂缓）
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
7. SecurityProvider、AuditLogger 与 LockProvider 都是横切组件，不继承 `Plugin`、不进入 `PluginType`；实现仍通过独立 Producer 与 `*_impl` 自注册。横切组件的接口文件命名为 `<name>/<name>.py`（不是插件的 `base.py`）。
8. 出站 HTTP 客户端（LLM / ASR / Embedder / Reranker）统一接受 `<prefix>_ssl_verify` /
   `<prefix>_ssl_ca_cert`（默认关闭），经 `_support.read_outbound_ssl` 读取。开启时须调
   `require_https` 与 `require_ca_file` 在装配期拦截明文 scheme 和缺失证书，并只在此时
   注入 `http_client`。OpenAI SDK 相关实现必须使用 `openai.DefaultHttpxClient`，不得用
   裸 `httpx.Client` 覆盖 SDK 的长读取超时、连接池与重定向等默认值。`verify` 取值统一经
   `outbound_verify` 翻译，不在各实现里内联。缺证书回落系统 CA 而非报错，这是与
   storage 侧唯一的差异，详见
   [F05-model-service-ssl.md](../../docs/features/common/F05-model-service-ssl.md)。
9. SSL 相关的公共件只在 `_support.py` 实现一份：`as_bool` / `SslConfig` /
   `build_ssl_config` / `require_tls_scheme` / `require_ca_file` / `outbound_verify` /
   `read_ssl_config` / `reject_url_tls_params`。storage 层、lock 与 security 层均从此处
   引用，新增出站客户端不得再各写一份归一或校验逻辑。同理，scope 命名空间渲染
   （`SCOPE_DIMS` / `scope_segments`）与后端异常归一（`wrap_backend`）也只此一份，
   `storage/_support.py` 是再导出而非第二实现。
10. LockProvider 的契约是异步的，`health()` 随之异步——这是 common 层唯一的异步组件。
    锁只交付原语，本层不在任何业务路径上加锁；在哪些临界区取锁由各消费方自行论证。
    锁是基于租约的协调机制而非共识算法，依赖方必须能容忍偶发互斥失效或自备第二道防线。
11. `security/` 是 F05 安全域的契约层：消费方只 import 契约与值对象，不反向 import；接口先行过渡期内不启用任何新认证/授权逻辑，旧 `SecurityProvider` 继续从包顶层导出，新契约异常从各能力子包取。
12. `RequestSecurityContext` 只经 `request_context.py` 的 `new_request_context` / `internal_context` 构造，不在各 surface 各自拼装；`legacy.py` 的 `legacy_request_context` 是过渡期唯一例外，实装 PR 与其全部调用点一并删除。
13. 安全域 `Grant` 在构造边界把动作迭代冻结为 `frozenset[Action]` 并拒绝非 `Action` 成员；`grant_id` 默认留空等待服务端生成，公共导出不得要求既有调用方预先提供服务端标识。
14. `RoutingFieldsProvider` 是授权策略路由字段的单一 capability 契约；接口先行过渡期的 `PermissionManager` 与目标 `Authorizer` 共同继承，禁止各自复制同名默认实现。
