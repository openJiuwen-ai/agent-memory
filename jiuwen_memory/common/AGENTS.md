# Agent Memory Common（公共组件层）

**规约文档**：[S07-common.md](../../docs/specs/S07-common.md)；安全横切契约见
[S10-security.md](../../docs/specs/S10-security.md)

> 本文档只记录相对稳定的模块本地规约（职责边界、行为铁律、本地约束）。特性设计与方案取舍记录在 `docs/features/` 下。

全局共享的可插拔插件与核心数据类型。提供工厂注册基础设施，定义跨层数据结构，是各层消费的共享依赖。

## 模块地图

| 文件/目录 | 职责 |
|---|---|
| `base.py` | Plugin 基类：所有共享插件的自描述契约 |
| `bootstrap.py` | 统一触发各插件注册（per-layer bootstrap） |
| `errors.py` | 自定义异常（含组件能力不匹配的 `UnsupportedCapabilityError`） |
| `_support.py` | 跨层共用的小工具：配置值布尔归一（`as_bool`）、SSL 配置读取与装配期校验（`SslConfig`/`build_ssl_config`/`require_tls_scheme`/`require_ca_file`/`outbound_verify`/`read_ssl_config`/`reject_url_tls_params`）、scope 命名空间渲染（`SCOPE_DIMS`/`scope_segments`）、后端异常归一（`wrap_backend`）；storage、lock 与出站客户端共用，避免各写一份 |
| `_import_support.py` | 导入容错样板的唯一归属地（`import_optional`/`import_required`/`import_required_attr`）：装配各 `*_impl` 包触发 `@Producer.register` 时逐个隔离可选依赖，单个缺失只让该插件缺席、不连坐同批其他插件、不阻断 `MemoryAPI` 构造。刻意与 `_support.py` 分家——后者讲配置值语义且连带 `type_def`/`Factory`/`errors`，本模块讲导入机制且**只依赖标准库**（51 个 `*_impl`/bootstrap 消费点各自 import 一次，自身须停在零项目依赖）；**不得**引入任何 `jiuwen_memory` 内部对象，以免装配期导入顺序反受各层先后影响。接入侧另有一份 `jiuwen_memory_entry/core/import_support.py`——Access 只允许依赖 `jiuwen_memory.api`（见 `tests/unit/api/test_access_api_boundary.py`），两侧不复用 |
| `log/` | 统一运行日志入口：`base.py` 提供 `get_logger` / `setup_logging`，`privacy_filter.py` 提供默认全局启用的 `SensitiveDataFilter` 及 `install_privacy_filter` / `redact_for_log` / `metadata_for_log` / `scope_for_log`；普通服务端日志在 Formatter 前脱敏，保留字段结构和经调用点确认的 MemoryUnit 技术 ID，不修改业务对象或正式 API 响应 |
| `type_def/` | 核心数据类型定义目录 |
| `type_def/memory.py` | MemoryUnit/Relation/Segment/Temporal/ContentLayers 等；MemoryUnit id 在完整 Scope 内唯一；KV key 前缀 `MEMORY_KEY_PREFIX`/`memory_key`（建索引记忆 `/memory/{id}`）。`ContentLayers`(l0/l1) 为分层披露标注，由 LayerAnnotator 对超阈 content 产出 |
| `type_def/scope.py` | Scope：`org/space/user/agent/session` 五维归属；非空 `space` 是全局唯一的逻辑隔离标识且为 keyword-only，旧位置参数保持 `org/user/agent/session` 顺序。另有 `KERNEL_COORD_KEYS`——内核自带的归属坐标实体名，三项取值必须是 `Scope` 的字段名，故与该类同处 |
| `type_def/filter.py` | FilterClause/FilterGroup/FilterExpr 及 normalize/evaluate；统一 API、检索和存储的树形过滤契约 |
| `type_def/memory_filter.py` | MemoryUnit 字段投影与 FilterExpr 公共求值；供 retrieval 真源复核和 KV list 兼容实现共用 |
| `type_def/memory_codec.py` | `MemoryUnit` ↔ bytes 编解码（`dumps`/`loads`）；当前 `_v=4`，分别序列化 `system_metadata` / `user_metadata`，拒绝未迁移的 `_v<4` MemoryUnit |
| `type_def/raw.py` | RawPayload（含交给 Ingestor 映射的 `assets` 资产引用）；KV key 前缀 `MESSAGES_KEY_PREFIX`/`messages_key`（未建索引 infer 原文 `/messages/{id}`） |
| `type_def/audit.py` | AuditEvent：记录 actor scope、target scope、action、decision、target_id 与 detail |
| `factory/factory.py` | Factory 基类：`TOP_NAME` 注册 + 三接口 `build`/`build_named`/`dep`（配置数据结构 `ComponentConfig`/`AssemblyContext`/`RawSpec` 在 `config/context.py`） |
| `embedder/` | Embedder 插件目录（接口 + 实现） |
| `chunker/` | Chunker 插件目录 |
| `tokenizer/` | Tokenizer 插件目录 |
| `normalizer/` | Normalizer 插件目录（passthrough / routing / video）；passthrough 只接已是 UTF-8 文本的 TEXT/CODE，DOCUMENT 需专用解析器；`ensure_normalizer_supports` 是共用模态能力门禁；视频 ASR 支持 OpenAI transcription 与 DashScope filetrans |
| `feature_extractor/` | FeatureExtractor 插件目录 |
| `llm/` | LLM 插件目录（`echo` / `openai` / `dashscope`） |
| `reranker/` | Reranker 插件目录 |
| `audit/` | AuditLogger 插件目录；`protected_audit_logger.py` 的 `ProtectedAuditLogger` 把 record 委派审计完整性 provider、query 透传，并在构造时校验 provider chain store 与 logger 是同一对象（PR3 契约，接口先行，PR1 无调用点） |
| `security/` | 安全能力的唯一归属地（F05）。PR1 已实装认证、保护与静态加密；`authentication/` 提供 Authenticator、PrincipalKeyStore 与 CredentialStatusRegistry，唯一实现目录是其内部的 `authentication/authentication_impl/`，包含 api_key / dev / trusted 三种认证器及凭据存储实现。进程内调用方经 `request_context.internal_context(authenticator)` 显式穿过认证边界，身份由传入的认证器产出、调用方不能自述身份（legacy 桥与公共 `ScopedAuthenticator` 均已删除，测试替身在 `tests/support/`）。PR2 在 `authorization/authorization_impl/` 提供 Standard/Routing/SpaceAware Authorizer 及 memory/SQLite GrantStore、DelegationStore；冻结约束是 MemoryAPI/Authorizer 唯一 PEP/PDP、凭据逐请求在线复核且旧 PermissionManager 不进入生产路径。`audit_integrity/` 仍只有 PR3 固定接口，未实装或激活。`request_context.py` 提供受控上下文构造。安全注册入口统一使用 `import_required` 记录并重抛依赖导入错误。另含空间级授权事实与谓词纯函数（见 `docs/features/control/F07-collective-memory-design.md`）。 |
| `lock/` | LockProvider 横切接口目录：跨实例互斥原语（接口 + `redis` / `memory` 实现）。**common 层唯一的异步契约**，只交付原语、不在业务路径加锁，见 [F06-distributed-lock.md](../../docs/features/common/F06-distributed-lock.md) |
| `security/_delegation_binding.py` | 私有服务端装配适配：凭据指纹绑定现有委托 ID，认证 actor 不变，记录与 Authorizer 必须同源；供 PEP/PDP 共用绑定复核，不注册新认证 target、不扩展冻结接口。 |

## 行为铁律

1. **插件接口与实现严格分离**
   接口模块（`<plugin>/base.py`）定义抽象契约 + Producer 工厂类，零依赖实现。实现模块（`<plugin>/<plugin>_impl/*.py`）具体实现 + 尾部 `@XxxProducer.register("name")` 自注册。消费方只 import 接口层，不触达 `*_impl`。
   唯一例外是 `LockProvider`：`acquire`/`release`/`guard` 在接口层落实现，只抽象后端原语。重入记账与 guard 组合是契约级行为而非后端细节，下沉会在两个实现里分叉。新增组件不得援引此例外，除非同样能论证「行为属于契约本身」。

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

6. **MemoryUnit metadata 双命名空间**
   `MemoryUnit` / `RawPayload` 只使用 `system_metadata` 和 `user_metadata`，共用
   `MetadataValueType`；系统不解释用户命名空间。`Chunk` / `Relation` 等独立模型的
   `metadata` 保持自身契约，不机械改名。

7. **RawPayload 只承载资产引用**
   `RawPayload.assets` 是输入 Ingestor 的 `list[str]`，数据类型本身不解释如何分配给 MemoryUnit 或 Segment，也不限制模态。

8. **Normalizer 能力声明必须可执行**
   `modalities()` 不是说明性信息：显式 routing route 在装配时校验，真实 payload 在规约前校验。装配矛盾抛 `ValidationError`，运行时不支持抛 `UnsupportedCapabilityError`。它只决定“是否支持”，不决定 `RawPayload.data` / `uri` 的载体选择。Passthrough 只允许 TEXT 使用 URI 文本回退；CODE 只接 UTF-8 源码文本，DOCUMENT 与不支持的媒体不得借 URI 回退绕过能力门禁。

9. **普通运行日志默认脱敏**
   通过 `get_logger()` 获取的 logger 默认安装 `SensitiveDataFilter`；正文、用户身份、Scope
   实值、标签、模型原始响应和 metadata 叶子值必须先用对应日志标记函数包装。仅 MemoryUnit
   技术 ID 可由调用点显式列入可见集合；过滤器不得修改业务对象、持久化数据或 API 响应。

## 与其他子目录的边界

**本模块管**：
- 共享插件接口定义与注册式工厂
- 核心数据类型（MemoryUnit/Scope/Context/Relation/Chunk/AuditEvent 等）
- 工厂注册基础设施（Factory 基类 + `TOP_NAME` 命名空间 + `build`/`build_named`/`dep` 三接口）
- 横切接口（Authenticator / PrincipalKeyStore / RateLimiter / WorkloadGuard / BindingPolicy / CryptographyProvider / KeyProvider / AuditLogger / LockProvider）
- 安全域契约（认证/密码学/保护与 PR2 授权已实装；PR3 审计完整性仅固定接口）
- 错误类型
- 工具函数

**不管**：
- 具体算子实现（归各层 `*_impl/`）
- 存储后端实现
- 业务编排逻辑
- 业务编排与空间/成员事实存取（归 `control`）；授权终局判断归本层 Authorizer

## 本地约束

1. 继承 `Plugin` 的模型插件必须实现 `plugin_type()` 和 `health()`；横切能力不继承
   `Plugin`，只实现各自 `base.py` 的契约（例如 Authenticator 有 `health()`，AuditLogger
   没有 `plugin_type()`）。
2. 实现通过 `@XxxProducer.register("name")` 自注册。
3. 新增插件实现：在 `<plugin>_impl/` 下新建文件 → 实现接口 → 尾部注册 → 在 `__init__.py` 添加 import。
4. 重依赖实现在 `*_impl/__init__.py` 中用 `try/except ImportError` 包裹。
5. 两级命名空间配置驱动装配：每个 Producer 声明全局唯一 `TOP_NAME`（占配置顶层段），其下是若干具名实例（`target` 指定实现名、`params` 传参、`new_instance` 控制是否共享）。`_build(config)` 里用 `XProducer.dep(config, param_name=None, default=...)` 取子依赖（引用名→共享 / 内联 dict→匿名 / 缺省→默认匿名）。
6. LLM 的厂商扩展参数必须由对应 Provider Adapter 注入；构建、检索等内核业务调用点不得硬编码 `extra_body` 等传输层字段。
7. 横切能力（Authenticator / PrincipalKeyStore / RateLimiter / WorkloadGuard /
   BindingPolicy / CryptographyProvider / KeyProvider / AuditLogger / LockProvider）
   不继承 `Plugin`、不进入 `PluginType`；接口统一在能力目录的 `base.py`（安全域为
   `security/<能力域>/`，`lock/` 因早于该约定沿用 `lock.py`），实现统一在同级
   `*_impl/`，YAML 只能选择已经注册的 target 并传递 params。当前不从 YAML import
   Python 类，也不自动发现未被应用启动代码 import 的外部包。
8. 安全能力一律落 `security/<能力域>/`，不新开顶层目录。核心不得按 target 名或
   `mode()` 字符串分支——需要区分的行为差异由 capability 方法（如
   `requires_loopback_binding()`）显式声明，详见 S10。
9. 出站 HTTP 客户端（LLM / ASR / Embedder / Reranker）统一接受 `<prefix>_ssl_verify` /
   `<prefix>_ssl_ca_cert`（默认关闭），经 `_support.read_outbound_ssl` 读取。开启时须调
   `require_https` 与 `require_ca_file` 在装配期拦截明文 scheme 和缺失证书，并只在此时
   注入 `http_client`。OpenAI SDK 相关实现必须使用 `openai.DefaultHttpxClient`，不得用
   裸 `httpx.Client` 覆盖 SDK 的连接池与重定向等默认值。`verify` 取值统一经
   `outbound_verify` 翻译，不在各实现里内联。缺证书回落系统 CA 而非报错，这是与
   storage 侧唯一的差异，详见
   [F05-model-service-ssl.md](../../docs/features/common/F05-model-service-ssl.md)。
10. SSL 相关的公共件只在 `_support.py` 实现一份：`as_bool` / `SslConfig` /
   `build_ssl_config` / `require_tls_scheme` / `require_ca_file` / `outbound_verify` /
   `read_ssl_config` / `reject_url_tls_params`。storage 层、lock 与 security 层均从此处
   引用，新增出站客户端不得再各写一份归一或校验逻辑。同理，scope 命名空间渲染
   （`SCOPE_DIMS` / `scope_segments`）与后端异常归一（`wrap_backend`）也只此一份，
   `storage/_support.py` 是再导出而非第二实现。
11. LockProvider 的契约是异步的，`health()` 随之异步——这是 common 层唯一的异步组件。
    锁只交付原语，本层不在任何业务路径上加锁；在哪些临界区取锁由各消费方自行论证。
    锁是基于租约的协调机制而非共识算法，依赖方必须能容忍偶发互斥失效或自备第二道防线。
12. `security/` 是 F05 安全域的契约层：消费方只 import 契约与值对象，不反向 import。旧
    `SecurityProvider` / `SecurityProducer` / `SecurityContext` / `KeySource` 仅保留一个发布周期
    的兼容导出，新代码统一走 `cryptography/` 的 `CryptographyProvider` / `KeyProvider`；契约
    异常从各能力子包取。
13. `RequestSecurityContext` 只经 `request_context.py` 的 `new_request_context` / `internal_context` 构造，不在各 surface 各自拼装；PR1 的 `auth_middleware.authenticated()` 已走该入口，PEP 侧 `has_valid_origin()` 校验归 PR2 启用。request ID 只由受控适配层或构造入口生成，不得来自客户端 header、query 或业务 payload，请求结束必须 reset。`legacy.py` 随 PR2 显式安全上下文接线删除。
14. 安全域 `Grant` 在构造边界把动作迭代冻结为 `frozenset[Action]` 并拒绝非 `Action` 成员；`grant_id` 默认留空等待服务端生成，公共导出不得要求既有调用方预先提供服务端标识。
    `new_request_context` 复用 `validate_actor_form` 校验所有认证器的输出；缺 org、无主体或
    双主体不得获得来源证明。资源 Scope 的双主体表达不受此约束。
    memory/SQLite GrantStore、DelegationStore 对同 ID 活动记录执行全字段更新，对已撤销记录
    拒绝重放覆盖。内存 Store 复制可变 Scope，禁止读写对象别名绕过锁内目标绑定。
    SQLite 委托空间限制使用带版本标记的 JSON；旧逗号多项格式有歧义时 fail-closed，需由
    可信部署重新写入明确集合。Grant 撤权的私有查找/条件更新接缝不增加公开 Store 方法。
15. `RoutingFieldsProvider` 是授权策略路由字段的单一 capability 契约；迁移期
    `PermissionManager` 与 `Authorizer` 可共同实现该查询能力，但生产 PEP 只能把字段交给
    Authorizer 判定，禁止形成两套默认实现或两个终局 PDP。
16. 审计增量验证必须经 `read_stable_snapshot(after_sequence)` 在同一快照取得精确 checkpoint 与固定链头，并令每页 `scan(..., through_sequence=快照链头)`；缺 checkpoint、序号缺口或未到快照链头都返回 `incomplete`，不得从 genesis 盲接。`AuditVerificationLimits` 是服务端可信单次资源边界，PEP 仍须截断 provider 的超量 samples。`ProtectedAuditLogger` 构造时必须满足 `provider.chain_store() is audit_logger`。
17. **OpenAI LLM/Embedder 出站等待策略显式可配**：OpenAI 兼容 LLM 与 Embedder 必须显式传入
    有限 timeout 和重试上限；默认 `300` 秒、`0` 次 SDK 重试，TCP connect 固定 5 秒。
    配置键为 `<prefix>_timeout` / `<prefix>_max_retries`，非法值在装配阶段报错；
    详见 [F05-model-service-ssl.md](../../docs/features/common/F05-model-service-ssl.md) 第八节。
