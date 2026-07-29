# S07 — 公共组件层（Common Layer）

## 元信息

| 项 | 值           |
|---|-------------|
| 关联模块 | src/common/ |
| 最近一次修订日期 | 2026-07-12 |

| 关联特性文档 | docs/features/F01-system-spec-design.md，docs/features/common/F02-dashscope-llm-provider.md，docs/features/control/F02-control-isolation-and-audit.md、docs/features/common/F01-memory-layer.md|

## 范围 / 边界

**管什么**：
- 共享可插拔插件（Embedder/Chunker/Tokenizer/Normalizer/FeatureExtractor/LLM/Reranker）
- 核心数据类型定义（MemoryUnit/Scope/Context/Relation 等）
- 工厂注册机制（Factory/Producer 基础设施）
- 审计日志（AuditLogger）
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

## 接口契约

### Plugin（基类，`base.py`）

```python
class PluginType(str, Enum):
    EMBEDDER / CHUNKER / TOKENIZER / NORMALIZER / FEATURE_EXTRACTOR / LLM / RERANKER / AUDIT_LOGGER

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
解释。通用 Adapter 不得默认发送其他厂商的扩展字段；健康检查与正常
`chat` 必须使用同一套 Provider 请求选项。

OpenAI 兼容实现支持厂商扩展请求体：构造参数 / 配置项 `llm_extra_body` 会作为
OpenAI SDK 的 `extra_body` 默认值传入；单次 `chat(..., extra_body={...})` 会与默认值
合并且同名字段以单次调用为准。常见 Aliyun / DashScope base URL 会自动补
`{"enable_thinking": false}`；自定义网关可显式配置 `llm_extra_body:
{"enable_thinking": false}` 或 `llm_enable_thinking: false`。

### Reranker（`reranker/base.py`）

重排能力：cross-encoder 精排。

| 方法 | 签名 | 语义 |
|------|------|------|
| `rerank` | `(query: str, texts: list[str]) -> list[float]` | 对每条 text 计算与 query 的相关性得分 |

### AuditLogger（`audit/base.py`）

审计日志。

| 方法 | 签名 | 语义 |
|------|------|------|
| `record` | `(event: AuditEvent) -> None` | 写入一条审计事件 |
| `query` | `(filters: dict[str, str], limit=100) -> list[AuditEvent]` | 按 `action` / `layer` / `decision` / `target_id` / `actor_org` / `actor_user` / `actor_agent` / `actor_session` / `occurred_after` / `occurred_before` 检索审计留痕 |

治理层通过 `Governor.audit(filters, limit)` 提供对外查询入口；`AuditLogger.query(...)` 是控制层消费审计后端的内部接口，不直接暴露为用户 API。

## 数据结构

### 核心类型（`type_def/memory.py`）

| 类型 | 关键字段 | 语义 |
|------|----------|------|
| `MemoryUnit` | id / scope / tier / layers / segments / source / temporal / provenance / supersedes / tags / metadata / lifecycle | 记忆单元 |
| `ContentLayers` | l0 / l1 | 分层披露标注（l0=50-100 字概要、l1=200-500 字要点 overview）；默认空串，extractor 对超阈 content 产出 |
| `Segment` | type / content / asset_ref / metadata | 内容段 |
| `Temporal` | t_event / t_ingest / t_valid / t_invalid | 时间字段 |
| `Relation` | id / source_id / target_id / relation / weight / metadata | 关联关系 |
| `Scope` | org / user / agent / session | 作用域 |
| `Context` | scope / max_tokens / extensions | 检索上下文 |
| `Entity` | text / type / confidence | 实体 |
| `FeatureSet` | keywords / entities / tags | 特征集合 |
| `Chunk` | id / text / unit_id / metadata | 切分块 |
| `ChatMessage` | role / content | LLM 对话消息 |
| `RawPayload` | id / scope / modality / data / uri / metadata / occurred_at | 原始负载 |
| `FilterClause` | key / op / value | 过滤条件 |
| `AuditEvent` | id / timestamp / actor / action / target_id / layer / detail | 审计事件 |

### 枚举（`type_def/memory.py`）

| 枚举 | 值 |
|------|------|
| `Modality` | TEXT / IMAGE / AUDIO / VIDEO / CODE / DOCUMENT |
| `LifecycleState` | ACTIVE / SUPERSEDED / ARCHIVED / FORGOTTEN |

### MemoryUnit 编解码（`type_def/memory_codec.py`）

真源 KVStore 存**字节**，`MemoryUnit` 对象只在写入（`dumps`）与产出结果（`loads`）两处出现。编解码与 `MemoryUnit` 同住 `common/type_def`，纯函数、无存储后端依赖。

- `dumps(unit) -> bytes`：`MemoryUnit` → JSON 字节，带 `_v` 版本号、枚举取 `.value`、时间取 isoformat。字段含 `layers`（`{l0, l1}`）。
- `loads(raw) -> MemoryUnit | None`：逆 `dumps`；非 dict 返回 `None`（KVStore 中混有索引/跟踪等非 unit 记录，靠此过滤）。
- **容错演进**：未知字段忽略、缺失字段取默认。加字段是兼容演进（老数据缺省读出，不升 `_v`）；改字段含义/结构才升 `_v` 并在 `loads` 按 `_v` 分支。当前 `_v=2`（segments 列表化）。
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
| `NormalizerProducer` / `FeatureExtractorProducer` / `LlmProducer` / `RerankerProducer` / `AuditProducer` | 各自唯一 |

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
- `EmbedderProducer` / `ChunkerProducer` / `TokenizerProducer` / `NormalizerProducer` / `FeatureExtractorProducer` / `LlmProducer` / `RerankerProducer` / `AuditProducer`

## 错误类型（`errors.py`）

| 异常 | 含义 |
|------|------|
| `ConflictError` | 资源冲突（id 已存在） |
| `NotFoundError` | 资源不存在 |
| `PermissionDeniedError` | 鉴权失败 |
| `PolicyError` | 策略错误（未知键/不可变配置） |
| `BackendError` | 后端不可用 |
| `HealthCheckError` | 健康检查失败 |

## 实现注册机制

```
src/common/<插件>/
    base.py                 # 接口 + Producer
    <插件>_impl/
        __init__.py         # 重导出实现类
        <impl_class_snake>.py   # 具体实现 + 尾部 @XxxProducer.register("name")
```

注册由 `common.bootstrap.register_plugins` 统一触发。

## 与其它 spec 的关系

| 关联 spec | 关系 |
|-----------|------|
| S01-ingest_access | 接入层消费 Normalizer |
| S03-memory_manage | 控制层消费 AuditLogger 记录的审计事件，并通过 Governor.audit 暴露查询 |
| S04-retrieval | 检索层消费 Embedder/Tokenizer/FeatureExtractor/LLM/Reranker |
| S05-construction | 构建层消费 Chunker/Embedder/Tokenizer/FeatureExtractor/LLM |
| S06-storage | 存储层依赖本层的数据类型定义（Scope/FilterClause 等） |
| architecture.md 全文 | 本层承载全局共享的数据类型与工具 |
