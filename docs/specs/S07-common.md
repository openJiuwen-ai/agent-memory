# S07 — 公共组件层（Common Layer）

## 元信息

| 项 | 值           |
|---|-------------|
| 关联模块 | src/common/ |
| 最近一次修订日期 | 2026-07-31 |
| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/api/F01-memory-api-impl-design.md，docs/features/construction/F04-cc-memory-compat.md，docs/features/common/F01-memory-layer.md，docs/features/common/F02-dashscope-llm-provider.md，docs/features/common/F03-scope-space-isolation.md，docs/features/common/F04-security-interfaces-and-encryption.md，docs/features/control/F02-control-isolation-and-audit.md，docs/features/retrieval/F03-metadata-filtering.md，docs/features/security/F01-authentication-kernel.md，docs/features/security/F03-audit-integrity.md |

## 范围 / 边界

**管什么**：
- 共享可插拔插件（Embedder/Chunker/Tokenizer/Normalizer/FeatureExtractor/LLM/Reranker）
- 核心数据类型定义（MemoryUnit/Scope/Context/Relation 等）
- 工厂注册机制（Factory/Producer 基础设施）
- 审计日志（AuditLogger）
- 数据保护横切接口（SecurityProvider）
- 错误类型（自定义异常）
- 工具函数（ID 生成/时间解析等）

**不管什么**：
- 不做具体算子实现（算子由各层 `*_impl/` 实现）
- 不做存储后端实现
- 不做业务编排逻辑
- 不做鉴权/策略管理

## 不变量

1. **共享插件必须双侧同一**：Embedder/Tokenizer/FeatureExtractor 等必须在构建侧与检索侧使用同一实现/同一配置，保证同词表/同向量空间。
2. **接口与实现严格分离**：顶层 `.py` 是纯抽象，不 import `*_impl/`。
3. **所有插件必须实现 `plugin_type()` 和 `health()`**：继承自 `Plugin` 基类。
4. **types.py 零依赖其他文件**：纯数据定义，被全局共享依赖。
5. **工厂注册发生在 import 时**：实现文件尾部 `@XxxProducer.register("name")` 绑定构建函数，`__init__.py` 导入实现文件触发注册。
6. **LLM Provider 参数不上浮到业务层**：厂商专属请求字段只能由对应 Adapter 生成；消费 `LLM` 的算子只传递通用生成选项。
7. **SecurityProvider 是字节级横切接口**：调用方在持久化字节写入前加密、读取后解密；接口不绑定 `MemoryUnit` 或存储后端，是否启用由装配配置决定。
8. **标识唯一性分层**：非空 Space id 全局唯一；`MemoryUnit.id` 只要求在完整 Scope 内唯一。
9. **Scope 位置参数兼容**：`space` 可为空但只能按关键字传入；旧位置参数顺序保持
   `Scope(org, user, agent, session)`。
10. **Scope 是 frozen value object**（安全加固，F01 决策 16）：`@dataclass(frozen=True)`，
    身份/隔离值不可变是跨模块安全不变量。改某维用 `dataclasses.replace(scope, org=...)`
    返回新值，禁止原地 `scope.x = ...`（抛 `FrozenInstanceError`）。frozen 同时使 Scope
    可哈希。防的是「签发 key 后改原 actor 的 org 让已签发身份跟着变」的越权。

## 接口契约

### Plugin（基类，`base.py`）

```python
class PluginType(str, Enum):
    TOKENIZER / CHUNKER / EMBEDDER / FEATURE_EXTRACTOR / LLM / NORMALIZER / RERANKER

class Plugin(ABC):
    def plugin_type(self) -> PluginType  # 自描述
    def health(self) -> None              # 存活探测
```

### Embedder（`embedder/base.py`）

向量化能力：文本 → 稠密向量。

| 方法 | 签名 | 语义 |
|------|------|------|
| `embed` | `(texts: list[str]) -> list[list[float]]` | 批量向量化：每条输入产出一个向量 |
| `dimension` | `() -> int` | 返回输出向量维度（须与目标向量索引一致） |
| `embed_query` | `(text: str) -> list[float]` | 单条便捷方法 |

### Chunker（`chunker/base.py`）

内容切分能力。

| 方法 | 签名 | 语义 |
|------|------|------|
| `chunk` | `(text, unit_id="", metadata=None) -> list[Chunk]` | 将 text 切分为有序 chunk，每块带上 unit_id 与透传的 metadata |

### Tokenizer（`tokenizer/base.py`）

分词能力：文本 → token 序列。

| 方法 | 签名 | 语义 |
|------|------|------|
| `tokenize` | `(text: str) -> list[str]` | 将 text 分词为 token 列表 |
| `tokenize_batch` | `(texts: list[str]) -> list[list[str]]` | 批量分词 |

### Normalizer（`normalizer/base.py`）

规约投影能力：多模态 RawPayload → 可治理文本 content。

| 方法 | 签名 | 语义 |
|------|------|------|
| `normalize` | `(payload: RawPayload) -> str` | 从原始负载提取/翻译为可治理文本 |
| `modalities` | `() -> list[Modality]` | 返回本 normalizer 支持的模态类型 |

### FeatureExtractor（`feature_extractor/base.py`）

特征抽取能力：文本 → 结构化特征（关键词/命名实体/标签，不含稠密向量）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `extract` | `(text: str) -> FeatureSet` | 从 text 抽取结构化特征 |
| `extract_batch` | `(texts: list[str]) -> list[FeatureSet]` | 批量抽取 |

### LLM（`llm/base.py`）

大模型调用能力（vLLM 部署 / OpenAI 兼容 chat 后端）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `chat` | `(messages: list[ChatMessage], **options) -> str` | 执行一次对话补全，返回助手回复文本 |
| `generate` | `(prompt: str, **options) -> str` | 单 prompt 便捷方法 |

`LLM` 的具名配置通过 `target` 选择 Provider Adapter，`params` 只由该 Adapter
解释。通用 Adapter 不得默认发送其他厂商的扩展字段，业务算子不得硬编码
`extra_body` 等厂商专属请求字段；健康检查与正常
`chat` 必须使用同一套 Provider 请求选项。

DashScope Adapter 的 `params.enable_thinking` 由 Adapter 转换为
`extra_body.enable_thinking`，缺省为 `false`；通用 OpenAI Adapter 不按 base URL
猜测厂商，也不自动注入该字段。

### Reranker（`reranker/base.py`）

重排能力：cross-encoder 精排。

| 方法 | 签名 | 语义 |
|------|------|------|
| `rerank` | `(query: str, texts: list[str]) -> list[float]` | 对每条 text 计算与 query 的相关性得分 |

### AuditLogger（`audit/base.py`）

审计日志。完整性保护（HMAC）方法有默认实现，普通后端不破坏（审计 PR③ F03）。

| 方法 | 签名 | 语义 |
|------|------|------|
| `record` | `(event: AuditEvent) -> None` | 写入一条审计事件 |
| `query` | `(filters: dict[str, str], limit=100, *, offset=0) -> list[AuditEvent]` | 按 `action` / `layer` / `decision` / `target_id` / `actor_*` / `target_*` / `occurred_after` / `occurred_before` 检索；`offset` 分页流式（审计 P2-1） |
| `tail` | `(limit=1) -> list[AuditEvent]` | 最近 `limit` 条事件（O(1)，持久化后端 override 成 DESC LIMIT；默认全量取最后） |
| `record_chained` | `(event, expected_head) -> str` | 链式 CAS 追加（持久化后端 override 成事务 CAS；默认降级为 record，不检查 expected_head） |
| `get_chain_head` | `() -> str` | 当前链头 HMAC（O(1)，持久化后端读 chain-head 表；默认走 tail） |
| `get_chain_state` | `() -> tuple[str, int, int, str, int]` | 原子快照读取（head_hmac, head_last_seq, last_event_seq, last_event_hmac, schema_version）；持久化后端 override 成单条 SQL CTE 查询；默认走 tail + get_chain_head 组合（审计 P1） |
| `init_chain_head` | `(last_seq, last_hmac) -> None` | 旧库迁移初始化链头；持久化后端 override 成 INSERT chain-head 行；默认空操作（审计 P1-2） |
| `get_last_event` | `() -> AuditEvent \| None` | 读取最后事件（用于启动验证）；持久化后端 override 成 MAX(seq) 查询；默认走 tail（审计 P1-2） |
| `iter_chain` | `(after_seq=0, limit=1000) -> list[tuple[int, AuditEvent]]` | keyset 分页遍历（`WHERE seq > ?`，审计 P2-1；默认降级为 query offset） |
| `verify_integrity` | `() -> AuditIntegrityResult` | 校验完整性，返回结构化状态（`unsupported`/`clean`/`tampered`，审计 P2-2；默认 `unsupported`） |

治理层通过 `Governor.audit(filters, limit)` 提供对外查询入口；`AuditLogger.query(...)` 是控制层消费审计后端的内部接口，不直接暴露为用户 API。完整性保护由 `HmacAuditLogger` 装饰器（`security/audit_hmac.py`）提供，详见 F03。

### SecurityProvider（`security/security.py`）

数据保护横切接口。调用方以 bytes 为边界接入：写入持久化字节前调用 `encrypt`，读取持久化字节后调用 `decrypt`。接口只表达数据保护能力，不绑定 `MemoryUnit` 序列化、不绑定 KV 后端、不决定是否默认启用加密。

| 方法 | 签名 | 语义 |
|------|------|------|
| `encrypt` | `(plaintext: bytes, *, context: SecurityContext | None = None, aad: bytes = b"") -> bytes` | 加密明文字节，可结合 scope / purpose / metadata 与 AAD 做租户隔离和完整性保护 |
| `decrypt` | `(ciphertext: bytes, *, context: SecurityContext | None = None, aad: bytes = b"") -> bytes` | 解密密文字节并校验完整性 |
| `health` | `() -> None` | 存活探测；默认返回 `None`，具体实现可覆盖并抛出健康检查异常 |

`SecurityProducer.TOP_NAME` 为 `security`。具体 provider 的实现列表、target 名与
私有配置参数归 `src/common/AGENTS.md` 与对应 feature 文档记录；本 spec 只固化
接口、上下文和错误语义。

## 数据结构

### 核心类型（`type_def/memory.py`）

| 类型 | 关键字段 | 语义 |
|------|----------|------|
| `MemoryUnit` | id / scope / tier / layers / segments / source / temporal / provenance / supersedes / tags / metadata / lifecycle | 记忆单元；id 在完整 Scope 内唯一 |
| `ContentLayers` | l0 / l1 | 分层披露标注（l0=50-100 字概要、l1=200-500 字要点 overview）；默认空串，extractor 对超阈 content 产出 |
| `Segment` | type / content / asset_ref / metadata | 内容段 |
| `Temporal` | t_event / t_ingest / t_valid / t_invalid | 时间字段 |
| `Relation` | id / source_id / target_id / relation / weight / metadata | 关联关系 |
| `Scope` | org / space / user / agent / session | 作用域（frozen value object，`frozen=True`）；非空 `space` 是全局唯一逻辑隔离标识，空值为兼容域且该字段为 keyword-only。改维度用 `dataclasses.replace`，不可原地修改（见不变量 10） |
| `Context` | scope / max_tokens / extensions | 检索上下文 |
| `Entity` | text / type / confidence | 实体 |
| `FeatureSet` | keywords / entities / tags | 特征集合 |
| `Chunk` | id / text / unit_id / metadata | 切分块 |
| `ChatMessage` | role / content | LLM 对话消息 |
| `RawPayload` | id / scope / modality / data / uri / metadata / occurred_at | 原始负载 |
| `FilterClause` | field / op / value | 原子过滤谓词 |
| `FilterGroup` | logic / children | AND / OR / NOT 逻辑节点 |
| `FilterExpr` | FilterClause \| FilterGroup | 跨 API、检索和存储层的过滤树 |
| `matches_memory_unit` | `(MemoryUnit, FilterExpr \| None) -> bool` | retrieval 真源复核和 KV list 共用的 MemoryUnit 字段投影与过滤求值 |
| `AuditEvent` | id / timestamp / actor / target / action / target_id / layer / detail | 审计事件；`actor` 与 `target` 均为 Scope，支持 actor_* 与 target_* 字段过滤。`detail` 承载安全层四字段（`acting_user`/`role`/`key_fp`/`auth_mode`，§7.2）与 HMAC（`_hmac`/`prev_hmac`，§7.3） |
| `AuthContext` | actor / acting_user / role / from_oauth / authorizing_key_fp / auth_mode | 认证层产出的请求级安全上下文（`frozen=True`，`common/type_def/auth.py`）；ContextVar 传播（`set_current`/`reset_current`/`get_current`，未认证返回 `None`）。`auth_mode` 由 authenticator 填（dev/trusted/api_key）。详见 F01 / F03 |
| `SecurityContext` | scope / purpose / metadata | 一次加密/解密调用的安全上下文 |

### 枚举（`type_def/memory.py`）

| 枚举 | 值 |
|------|------|
| `Modality` | TEXT / IMAGE / AUDIO / VIDEO / CODE / DOCUMENT |
| `LifecycleState` | ACTIVE / SUPERSEDED / ARCHIVED / FORGOTTEN |

### MemoryUnit 编解码（`type_def/memory_codec.py`）

真源 KVStore 存**字节**，`MemoryUnit` 对象只在写入（`dumps`）与产出结果（`loads`）两处出现。编解码与 `MemoryUnit` 同住 `common/type_def`，纯函数、无存储后端依赖。

- `dumps(unit) -> bytes`：`MemoryUnit` → JSON 字节，带 `_v` 版本号、枚举取 `.value`、时间取 isoformat。字段含 `layers`（`{l0, l1}`）。
- `loads(raw) -> MemoryUnit | None`：逆 `dumps`；非 dict 返回 `None`（KVStore 中混有索引/跟踪等非 unit 记录，靠此过滤）。
- **容错演进**：未知字段忽略、缺失字段取默认。加字段是兼容演进（老数据缺省读出，不升 `_v`）；改字段含义/结构才升 `_v` 并在 `loads` 按 `_v` 分支。当前 `_v=3`（`_v=2` 为 segments 列表化；`_v=3` 把 scope 从 `org/user/agent/session` 扩展为 `org/space/user/agent/session`，老数据读为空 `space`）。
- `layers` 字段缺失时 `loads` 取空串 `ContentLayers()`——老数据无迁移读出。

### 工厂注册机制（`factory/factory.py`）

装配由两块基石协作：**Config 只产出「配置数据」**（解析成 `AssemblyContext`），**Factory 管「实例生成与共享」**（按具名实例缓存）。配置形态是**两级命名空间**——顶层每段对应一个 Producer 的 `TOP_NAME`，其下是若干**具名实例**；共享关系由配置里「具名 + 引用」显式表达，不再隐式按字段名约定。

#### 顶层命名空间名（`TOP_NAME`）

每个 `XProducer` 声明一个**全局唯一**的 `TOP_NAME`（即它在配置里占的顶层段名）。`Factory.__init_subclass__` 把 `TOP_NAME → cls` 登记进全局表（重名报错），供 Config 解析期校验顶层段拼写。

| 工厂 | `TOP_NAME` |
|------|-----------|
| `KvProducer` / `VectorProducer` / `FulltextProducer` | `kv_store` / `vector_store` / `fulltext_store` |
| `EmbedderProducer` / `ChunkerProducer` / `TokenizerProducer` | `embedder` / `chunker` / `tokenizer` |
| `IndexBuilderProducer` / `RecallerProducer` | `constructor` / `recaller` |
| `NormalizerProducer` / `FeatureExtractorProducer` / `LlmProducer` / `RerankerProducer` | `normalizer` / `feature_extractor` / `llm` / `reranker` |
| `AuditProducer` / `SecurityProducer` | `audit` / `security` |

#### Factory 基类

| 方法 | 签名 | 语义 |
|------|------|------|
| `register(target)` | `@classmethod (target: str) -> Callable` | 装饰器：注册某实现的 `_build` 函数（接口 1 的实现体） |
| `build(target, params, ctx, *, name="")` | `@classmethod -> T` | **接口 1（匿名）**：按 `target` 新建，**不入缓存** |
| `build_named(name, ctx)` | `@classmethod -> T` | **接口 2（具名/共享）**：按具名实例名取/建，按 `new_instance` 决定是否缓存共享 |
| `dep(config, param_name=None, default=None)` | `@classmethod -> T` | builder 内取依赖：引用名(str)→`build_named`（共享）/ 内联(dict)→`build`（匿名）/ 缺省(None)→`build(default)`（匿名默认） |
| `cfg_get` / `require_param` | `@staticmethod (config, key, ...)` | 读本组件参数（缺失给默认 / 必填缺失即抛 `ValidationError`） |
| `reset_all()` | `@classmethod () -> None` | 清空所有工厂的实例缓存（装配前调用，隔离多次装配） |
| `put(name, instance)` | `@classmethod -> None` | 把外部实例预置进缓存（如显式注入的真源 kv） |

- **接口 1（匿名）`build`**：查 `_registry[target]`，把 `params` 包成 `ComponentConfig` 交给注册的 `_build`，返回**新实例、不入缓存**。
- **接口 2（具名/共享）`build_named`**：命中 `cls._instances[name]` → 返回缓存共享实例；否则 `spec = ctx.lookup(cls.TOP_NAME, name)`（用自己的 `TOP_NAME` 定位命名空间）→ `build(spec.target, spec.params, ctx)`；`spec.new_instance` 为假则存入缓存供共享、为真则每次新建。
- **`dep`** 的 `param_name` 缺省取 `cls.TOP_NAME`，仅当 builder 入参名与依赖 Producer 顶层名不一致时显式传。

#### 配置数据结构（`config/context.py`）

| 类型 | 关键字段 | 语义 |
|------|----------|------|
| `RawSpec` | target / params / new_instance | 一个具名实例的纯数据 |
| `AssemblyContext` | globals / namespaces（`top_name → name → RawSpec`） | 全局装配上下文；`lookup(top_name, name) -> RawSpec` 取具名配置 |
| `ComponentConfig` | params / ctx / target / name | 传给每个 `_build` 的 config 视图；`get(key, default)` 先查本实例 `params`，缺失回退 `ctx.globals`，最终给 `default` |

- `ComponentConfig.get` 实现跨切面参数「写一处、处处读到」：`embedder_dim` / `vector_enabled` 等写在 `globals`，具名实例 `params` 可覆盖。
- `AssemblyContext.lookup` 是具名共享的来源：`build_named` 经它取 `RawSpec` 后建实例并按名缓存。

#### 注册模式

```python
# 接口模块（如 common/embedder/base.py）
class EmbedderProducer(Factory):
    """Embedder 的注册式工厂；TOP_NAME 即配置里的顶层命名空间。"""
    TOP_NAME = "embedder"

class Embedder(Plugin):
    ...

# 实现模块（如 common/embedder/embedder_impl/bge_m3_embedder.py）
from common.embedder.base import EmbedderProducer

@EmbedderProducer.register("bge_m3")
def _build(config: ComponentConfig) -> Embedder:
    model_path = Factory.require_param(config, "model_path", backend="bge_m3")
    return BgeM3Embedder(model_path=model_path)
```

**注册触发**：
1. 实现模块尾部 `@XxxProducer.register("target")` 注册 `_build` 函数
2. `*_impl/__init__.py` import 所有实现模块触发注册
3. 装配前调用 `common.bootstrap.register_plugins()` 确保注册完成

**共享语义**：
- 具名实例默认**共享**：多处 `build_named("main_vec", ctx)` 命中同一缓存键 → 同一实例
- 配 `new_instance: true` 的具名实例退出共享（每次引用都新建）
- 匿名 `build` 与内联实例（dict 依赖）天然不共享
- `reset_all()` 清空缓存（隔离多次装配 / 测试隔离）

各 Producer 继承 `Factory`：
- `EmbedderProducer` / `ChunkerProducer` / `TokenizerProducer` / `NormalizerProducer` / `FeatureExtractorProducer` / `LlmProducer` / `RerankerProducer` / `AuditProducer` / `SecurityProducer`

## 错误类型（`errors.py` / `security.py`）

| 异常 | 含义 |
|------|------|
| `ConflictError` | 资源冲突（id 已存在） |
| `NotFoundError` | 资源不存在 |
| `PermissionDeniedError` | 鉴权失败 |
| `PolicyError` | 策略错误（未知键/不可变配置） |
| `BackendError` | 后端不可用 |
| `HealthCheckError` | 健康检查失败 |
| `SecurityError` / `EncryptionError` | 安全横切处理失败的基类 |
| `InvalidMagicError` | 密文字节不符合当前 provider 期望的信封魔数 |
| `CorruptedCiphertextError` | 密文信封结构损坏、版本不支持或长度不完整 |
| `AuthenticationFailedError` | AES-GCM tag 校验失败，通常表示 AAD 不匹配或内容被篡改 |
| `KeyMismatchError` | 包裹的数据密钥无法用当前租户密钥解开 |

## 实现注册机制

```
src/common/<组件>/
    base.py | security.py   # 接口 + Producer
    <组件>_impl/
        __init__.py         # 重导出实现类
        <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

注册由 `common.bootstrap.register_plugins` 统一触发。`security_impl/` 当前注册 `local` SecurityProvider 实现。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | 接入层消费 Normalizer |
| S03-control | 控制层消费 AuditLogger 记录的审计事件，并通过 Governor.audit 暴露查询 |
| S04-retrieval | 检索层消费 Embedder/Tokenizer/FeatureExtractor/LLM/Reranker |
| S05-construction | 构建层消费 Chunker/Embedder/Tokenizer/FeatureExtractor/LLM |
| S06-storage | 存储层依赖本层的数据类型定义（Scope/FilterClause 等） |
| architecture.md 全文 | 本层承载全局共享的数据类型与工具 |
