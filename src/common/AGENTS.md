# Agent Memory Common（公共组件层）

**规约文档**：[S07-common.md](../../docs/specs/S07-common.md)；安全横切契约见
[S08-security.md](../../docs/specs/S08-security.md)

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
| `type_def/scope.py` | Scope：`org/space/user/agent/session` 五维归属；非空 `space` 是全局唯一的逻辑隔离标识且为 keyword-only，旧位置参数保持 `org/user/agent/session` 顺序。**frozen value object**（`@dataclass(frozen=True)`）：身份/隔离不可变是跨模块安全不变量，改某维用 `dataclasses.replace(scope, org=...)` 返回新值，禁止原地 `scope.x = ...`（抛 `FrozenInstanceError`）。详见 S07 不变量与 F01 决策 16 |
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
| `security/` | 安全能力的唯一归属地（F05）：`types.py`（AuthContext/RequestSecurityContext/CryptoContext/Role/Surface/Credentials/ResourceDescriptor/AuthorizationEnvironment，ContextVar 只作日志-trace 传播）、`request_context.py`（`RequestSecurityContext` 的受控构造入口：`new_request_context` / `internal_context`）、`runtime.py`（SecurityRuntime）、`authentication/`（Authenticator + PrincipalKeyStore + CredentialStatusRegistry，内置 dev/trusted/api_key + memory Argon2id；`PrincipalKeyStore.is_revoked` 供 PEP 在线复核撤销，`CredentialStatusRegistry` 由 PEP 持有按 `(credential_type, credential_issuer)` 路由撤销查询，不放 Authorizer）、`authorization/`（Authorizer + GrantStore + DelegationStore，内置 standard/allow_all + memory/sqlite 存储）、`protection/`（RateLimiter/WorkloadGuard/BindingPolicy，内置 token_bucket/unlimited/semaphore/loopback）、`cryptography/`（CryptographyProvider + KeyProvider（含 `rotate` 轮换契约），内置 `local` ENC1 AES-GCM）。注册入口 `security/bootstrap.py::register_security()` |
| `audit/` | AuditLogger 插件目录 |

## 行为铁律

1. **插件接口与实现严格分离**
   接口模块（`<plugin>/base.py`）定义抽象契约 + Producer 工厂类，零依赖实现。实现模块（`<plugin>/<plugin>_impl/*.py`）具体实现 + 尾部 `@XxxProducer.register("name")` 自注册。消费方只 import 接口层，不触达 `*_impl`。

2. **工厂随契约（住在接口层）**
   每个插件的 Producer 工厂定义在其接口模块（`base.py`）中，与抽象契约同处一地。

3. **注册靠 import 触发**
   实现文件尾部 `@XxxProducer.register("name")` 注册 _build 函数，`*_impl/__init__.py` import 各实现模块触发注册，`bootstrap.py::register_plugins()` 在装配前统一触发（安全域转交 `security/bootstrap.py::register_security()`）。

4. **type_def 不依赖能力实现**
   `type_def/*.py` 只定义跨层数据与 ContextVar，可在 `type_def` 内部引用基础类型
   （如 `audit.py` 引用 `scope.py`），不得 import security/audit/storage 等能力实现。
   安全类型住 `security/types.py` 而非 `type_def/`：`type_def` 被所有层 import，身份
   类型放进去会让「谁能构造/改写身份」的边界消失。

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
- 横切接口（Authenticator / PrincipalKeyStore / RateLimiter / WorkloadGuard / BindingPolicy / CryptographyProvider / KeyProvider / AuditLogger）
- 错误类型
- 工具函数

**不管**：
- 具体算子实现（归各层 `*_impl/`）
- 存储后端实现
- 业务编排逻辑
- 授权的**执行点**（PEP 是 `api/MemoryAPI`）与业务权限语义的编排；授权**判定**（PDP）本身归 `common/security/authorization/`

## 本地约束

1. 继承 `Plugin` 的模型插件必须实现 `plugin_type()` 和 `health()`；横切能力不继承
   `Plugin`，只实现各自 `base.py` 的契约（例如 Authenticator 有 `health()`，AuditLogger
   没有 `plugin_type()`）。
2. 实现通过 `@XxxProducer.register("name")` 自注册。
3. 新增插件实现：在 `<plugin>_impl/` 下新建文件 → 实现接口 → 尾部注册 → 在 `__init__.py` 添加 import。
4. 重依赖实现在 `*_impl/__init__.py` 中用 `try/except ImportError` 包裹。
5. 两级命名空间配置驱动装配：每个 Producer 声明全局唯一 `TOP_NAME`（占配置顶层段），其下是若干具名实例（`target` 指定实现名、`params` 传参、`new_instance` 控制是否共享）。`_build(config)` 里用 `XProducer.dep(config, param_name=None, default=...)` 取子依赖（引用名→共享 / 内联 dict→匿名 / 缺省→默认匿名）。
6. LLM 的厂商扩展参数必须由对应 Provider Adapter 注入；构建、检索等内核业务调用点不得硬编码 `extra_body` 等传输层字段。
7. 横切能力（Authenticator / PrincipalKeyStore / Authorizer / GrantStore /
   DelegationStore / RateLimiter / WorkloadGuard /
   BindingPolicy / CryptographyProvider / KeyProvider / AuditLogger）不继承 `Plugin`、
   不进入 `PluginType`；接口统一在能力目录的 `base.py`（安全域为
   `security/<能力域>/`），实现统一在同级 `*_impl/`，YAML 只能选择已经注册的 target
   并传递 params。当前不从 YAML import Python 类，也不自动发现未被应用启动代码
   import 的外部包。
8. 安全能力一律落 `security/<能力域>/`，不新开顶层目录。核心不得按 target 名或
   `mode()` 字符串分支——需要区分的行为差异由 capability 方法（如
   `requires_loopback_binding()`、`bind_instance_name()`、`is_test_only()`）显式声明，详见 S08。
9. `RequestSecurityContext` 只能由 `security/request_context.py` 的两个入口构造：
   `request_id` 由服务端生成、`started_at` 取服务端时钟、`attributes` 只由系统组件
   写入、`surface` 无默认值必须由适配层写入。进程内直连调用方走 `internal_context(authenticator)`，
   身份仍由 authenticator 产出——不存在 `auth=None`，也不存在把传入 Scope 直接当成
   已认证 actor 的旁路（F05 §进程内调用）。
