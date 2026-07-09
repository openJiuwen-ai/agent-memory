# jiuwen_memory.memory_core.config

`jiuwen_memory.memory_core.config` 是 JiuwenMemory 中统一的**记忆配置管理模块**，负责：

- 定义 `MemoryEngineConfig` 全局引擎配置；
- 定义 `MemoryScopeConfig` 作用域级配置（用于不同业务场景的模型/向量参数）；
- 定义 `AgentMemoryConfig` Agent 级记忆策略配置（用于定义需要提取的变量记忆和是否开启长期记忆）；
- 定义 `DreamingConfig` 睡时记忆巩固（后台整合用户历史会话）流程的配置。


## class jiuwen_memory.memory_core.config.config.MemoryEngineConfig

```
class jiuwen_memory.memory_core.config.config.MemoryEngineConfig(default_model_cfg: ModelRequestConfig | None = None, default_model_client_cfg: ModelClientConfig | None = None, input_msg_max_len: int = 8192, crypto_key: bytes = b'', codec: str = '')
```

全局记忆引擎配置，用于设置引擎级别的通用参数。

**参数**：

* **default_model_cfg**(ModelRequestConfig | None, 可选)：默认用于生成记忆的大模型请求参数（模型名、温度、最大 token 等）；若为 `None`，则无法生成记忆（除非通过 `MemoryScopeConfig` 为特定 scope 配置模型）。默认值：`None`。
* **default_model_client_cfg**(ModelClientConfig | None, 可选)：默认的大模型客户端配置（`client_id / client_provider / api_base / api_key / verify_ssl` 等）；若为 `None`，同样无法生成记忆（除非通过 scope 配置）。默认值：`None`。
* **forbidden_variables**(str, 可选)：禁止记忆的变量（逗号分隔的变量名）；默认值：`""`（不禁止任何变量）。
* **input_msg_max_len**(int, 可选)：输入消息最大长度（字符数）；在生成记忆时，超过此长度的消息内容会被截断。默认值：8192。
* **crypto_key**(bytes, 可选)：AES-256-GCM 加密密钥，长度必须为 32 字节（32 bytes）。若设置为非空字节串，在调用 `set_config` 时会自动为 `memory_index`（`BaseMemoryIndex`）注入 `AesStorageCodec`，对记忆内容的 `text` 字段进行存储层透明加解密；同时也会用于加密 `MemoryScopeConfig` 中的 `api_key` 等敏感参数。若为空字节串 `b''`，则所有加解密功能不启用。默认值：`b''`（不加密）。
* **codec**(str, 可选)：第三方加解密编解码器（codec）的**注册名**（如 `"sm4"`、`"hsm"`）。这是接入国密 SM4、HSM 硬件密码机等非 AES 加密方案的扩展点：调用方先通过 `register_storage_codec(name, codec)` 注册一个**已构造好的** `StorageCodec` 实例（密钥已在实例内部，引擎不再调用构造函数，因此支持任意构造签名——单参 SM4 或多参 HSM 均可），再在配置里用该 `name` 引用。`set_config` 时引擎按名查实例；查到则注入该实例，`crypto_key` 被忽略。若 `codec` 为空字符串 `""`，或该名下未注册实例，则回退到由 `crypto_key` 构造的 `AesStorageCodec`（并打一条 warning 日志）。默认值：`""`（使用内置 AES）。

  > **说明**：第三方 codec 需实现 `encode(text: str) -> str` / `decode(data: str) -> str` 两个方法，满足 `decode(encode(x)) == x`；对 `None`/空串应原样返回（与 `AesStorageCodec` 的空密钥直通行为一致）。协议定义见 `jiuwen_memory.foundation.codec.StorageCodec`。注册必须在 `set_config` 之前完成。详见 [LongTermMemory API 文档 — 自定义 codec 拓展](long_term_memory.md)。
* **single_turn_history_summary_max_token**(int, 可选)：单轮历史摘要生成的最大 token 数，必须大于 0。默认值：128。

**参数校验**：

`crypto_key` 参数带有 `field_validator`：

- 若长度为 0，则保持为空（不加密）；
- 若长度等于 `AES_KEY_LENGTH`（32），则通过校验；
- 否则抛出异常（`MEMORY_SET_CONFIG_EXECUTION_ERROR`），错误信息为：`"crypto_key must be empty or {AES_KEY_LENGTH} bytes length"`。

**样例**：

```python
>>> from jiuwen_memory.memory_core.config import MemoryEngineConfig
>>> from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> 
>>> # 创建全局引擎配置
>>> engine_config = MemoryEngineConfig(
>>>     default_model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.0,
>>>     ),
>>>     default_model_client_cfg=ModelClientConfig(
>>>         client_id="default_memory_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-xxxx",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     forbidden_variables="user_id, phone_number, email",
>>>     input_msg_max_len=8192,
>>>     crypto_key=b"your-32-byte-aes-key-here!!",  # 32 字节
>>> )
```

**第三方 codec 样例**：

```python
>>> from jiuwen_memory.memory_core.config import MemoryEngineConfig
>>> from jiuwen_memory.foundation.codec import register_storage_codec
>>>
>>> # 1. 自定义 codec（国密 SM4 示意）：密钥在构造时注入实例
>>> class SM4StorageCodec:
...     def __init__(self, key: bytes):
...         self._key = key
...     def encode(self, text: str) -> str:
...         ...  # 返回 SM4 密文
...     def decode(self, data: str) -> str:
...         ...  # 返回 SM4 明文
...
>>> # 2. 注册"已构造好的实例"（引擎不再调用构造函数，故支持任意构造签名，
>>> #    包括多参的 HSM：HSMCodec(cert=..., slot=..., pin=...)）
>>> register_storage_codec("sm4", SM4StorageCodec(key=sm4_key))
>>>
>>> # 3. 配置里用注册名引用
>>> engine_config = MemoryEngineConfig(
>>>     # ...其他参数...
>>>     crypto_key=b"",     # 第三方 codec 不依赖 crypto_key，可留空
>>>     codec="sm4",        # 引擎按名查实例并注入
>>> )
```


## class jiuwen_memory.memory_core.config.config.MemoryScopeConfig

```
class jiuwen_memory.memory_core.config.config.MemoryScopeConfig(model_cfg: ModelRequestConfig | None = None, model_client_cfg: ModelClientConfig | None = None, embedding_cfg: EmbeddingConfig | None = None, user_profile_definition: str = "用户本人的肯定或否定表述（包含不限于基本身份、兴趣偏好、人际关系、资产状况）", semantic_memory_definition: str = "用户对话中涉及的和时间无明确关系的事实性内容或概念", episodic_memory_definition: str = "用户对话中涉及的和时间有明确关系的事实性内容或概念")
```

作用域级记忆配置，用于为不同的 `scope_id` 定义独立的模型和向量参数。

**参数**：

* **model_cfg**(ModelRequestConfig | None, 可选)：该 scope 下使用的大模型请求配置（模型名、温度等）；若为 `None`，则使用全局 `MemoryEngineConfig.default_model_cfg`。默认值：`None`。
* **model_client_cfg**(ModelClientConfig | None, 可选)：该 scope 下使用的大模型客户端配置（`client_id / api_base / api_key` 等）；若为 `None`，则使用全局 `MemoryEngineConfig.default_model_client_cfg`。默认值：`None`。
* **embedding_cfg**(EmbeddingConfig | None, 可选)：该 scope 下使用的嵌入模型配置（`model_name / base_url / api_key` 等）；若为 `None`，则语义检索功能可能不可用（取决于全局是否提供了嵌入模型）。默认值：`None`。
* **user_profile_definition**(str, 可选)：用户画像记忆提取的定义规则，用于自定义从对话中提取用户画像信息的范围。默认值：`"用户本人的肯定或否定表述（包含不限于基本身份、兴趣偏好、人际关系、资产状况）"`。
* **semantic_memory_definition**(str, 可选)：语义记忆提取的定义规则，用于自定义从对话中提取语义记忆信息的范围。默认值：`"用户对话中涉及的和时间无明确关系的事实性内容或概念"`。
* **episodic_memory_definition**(str, 可选)：情景记忆提取的定义规则，用于自定义从对话中提取情景记忆信息的范围。默认值：`"用户对话中涉及的和时间有明确关系的事实性内容或概念"`。

> **说明**：`MemoryScopeConfig` 中的 `api_key` 参数在保存到 `kv_store` 时会被自动加密（使用 `MemoryEngineConfig.crypto_key`），读取时会自动解密。

**样例**：

```python
>>> from jiuwen_memory.memory_core.config import MemoryScopeConfig
>>> from jiuwen_memory.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
>>> from jiuwen_memory.retrieval.common.config import EmbeddingConfig
>>> 
>>> # 创建作用域配置
>>> scope_config = MemoryScopeConfig(
>>>     model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.1,
>>>     ),
>>>     model_client_cfg=ModelClientConfig(
>>>         client_id="scope_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-yyyy",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     embedding_cfg=EmbeddingConfig(
>>>         model_name="text-embedding-3-small",
>>>         base_url="https://api.openai.com/v1",
>>>         api_key="sk-zzzz",
>>>     ),
>>> )
```


## class jiuwen_memory.memory_core.config.config.AgentMemoryConfig

```
class jiuwen_memory.memory_core.config.config.AgentMemoryConfig(mem_variables: list[Param] = [], enable_long_term_mem: bool = True, enable_user_profile: bool = True, enable_semantic_memory: bool = True, enable_episodic_memory: bool = True, enable_summary_memory: bool = True)
```

Agent 级记忆策略配置，描述某个智能体希望提取和管理哪些类型的记忆。

**参数**：

* **mem_variables**(list[Param], 可选)：变量记忆配置列表；每个 `Param` 定义一个变量名、描述、类型、是否必填等；在 `LongTermMemory.add_messages` 时，会根据这些配置从对话中提取变量值并保存。默认值：`[]`。
* **enable_long_term_mem**(bool, 可选)：是否开启长期记忆生成；为 `True` 时，会从对话中提取用户画像（长期记忆）并保存到语义存储中；为 `False` 时，仅保存消息和变量记忆，不生成用户画像。默认值：`True`。
* **enable_user_profile**(bool, 可选)：是否开启用户画像生成和使用；为 `True` 时，会从对话中提取用户个人信息（如姓名、手机号等）并保存到语义存储中，在后续搜索中会使用用户画像；为 `False` 时，不生成和使用用户画像。默认值：`True`。
* **enable_semantic_memory**(bool, 可选)：是否开启语义记忆生成；为 `True` 时，会从对话中提取语义记忆并保存到语义存储中，在后续搜索中会使用语义记忆；为 `False` 时，不生成和使用语义记忆。默认值：`True`。
* **enable_episodic_memory**(bool, 可选)：是否开启情景记忆生成；为 `True` 时，会从对话中提取情景记忆并保存到语义存储中，在后续搜索中会使用情景记忆；为 `False` 时，不生成和使用情景记忆。默认值：`True`。
* **enable_summary_memory**(bool, 可选)：是否开启用户摘要记忆生成；为 `True` 时，会从对话中提取用户摘要（如最近的对话内容）并保存到语义存储中；为 `False` 时，不生成用户摘要记忆。默认值：`True`。

> **说明**：`Param` 类型定义在 `jiuwen_memory.common.schema.param` 中，通常包含 `name / description / type / required` 等参数。

**样例**：

```python
>>> from jiuwen_memory.memory_core.config import AgentMemoryConfig
>>> from jiuwen_memory.common.schema.param import Param
>>> 
>>> # 创建 Agent 记忆策略配置
>>> agent_config = AgentMemoryConfig(
>>>     mem_variables=[
>>>         Param(
>>>             name="favorite_color",
>>>             description="用户喜欢的颜色",
>>>             type="string",
>>>             required=False,
>>>         ),
>>>         Param(
>>>             name="age",
>>>             description="用户年龄",
>>>             type="number",
>>>             required=False,
>>>         ),
>>>     ],
>>>     enable_long_term_mem=True,
>>>     enable_user_profile=True,
>>>     enable_semantic_memory=True,
>>>     enable_episodic_memory=True,
>>>     enable_summary_memory=True,
>>> )
```


## class jiuwen_memory.memory_core.config.config.DreamingConfig

```
class jiuwen_memory.memory_core.config.config.DreamingConfig(enabled: bool = False, interval_seconds: float = 14400.0, min_session_rounds: int = 4, max_sessions_per_sweep: int = 10, max_compress_tokens: int = 30000, max_items_per_session: int = 5)
```

**睡时记忆巩固**（Dreaming）流程的配置：后台周期性地重新读取用户已存储的会话，从中提炼可复用知识。由调用方构造并传入 `LongTermMemory.start_dreaming`，**不**从任何全局配置文件读取。

**参数**：

* **enabled**(bool, 可选)：总开关。为 `False` 时 `LongTermMemory.start_dreaming` 返回 `None`，不启动任何流程。默认：`False`。
* **interval_seconds**(float, 可选)：两次后台 sweep 之间的间隔（秒），必须大于 0。orchestrator 会将实际生效值钳制为最小 60 秒。默认：`14400.0`（4 小时）。
* **min_session_rounds**(int, 可选)：会话被处理所需的最小用户轮次，轮次更少的会话会被跳过，必须 >= 1。默认：4。
* **max_sessions_per_sweep**(int, 可选)：单次 sweep 处理的最大会话数，必须 >= 1。默认：10。
* **max_compress_tokens**(int, 可选)：提取前会话被压缩到的 token 预算，必须大于 0。默认：30000。
* **max_items_per_session**(int, 可选)：从单个会话中提取的最大知识条数，必须 >= 1。默认：5。

**示例**：

```python
>>> from jiuwen_memory.memory_core.config import DreamingConfig
>>> 
>>> # 开启睡时记忆巩固，sweep 间隔设为 1 小时
>>> dreaming_config = DreamingConfig(
>>>     enabled=True,
>>>     interval_seconds=3600,
>>>     min_session_rounds=2,
>>>     max_sessions_per_sweep=10,
>>>     max_compress_tokens=30000,
>>>     max_items_per_session=5,
>>> )
```


## 使用示例

```python
>>> from jiuwen_memory.memory_core import (
>>>     MemoryEngineConfig,
>>>     MemoryScopeConfig,
>>>     AgentMemoryConfig,
>>> )
>>> from jiuwen_memory.foundation.llm.schema.config import (
>>>     ModelClientConfig,
>>>     ModelRequestConfig,
>>> )
>>> from jiuwen_memory.retrieval.common.config import EmbeddingConfig
>>> from jiuwen_memory.common.schema.param import Param
>>> 
>>> # 1. 创建全局引擎配置
>>> engine_config = MemoryEngineConfig(
>>>     default_model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.0,
>>>     ),
>>>     default_model_client_cfg=ModelClientConfig(
>>>         client_id="default_memory_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-xxxx",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     forbidden_variables="user_id, phone_number, email",
>>>     input_msg_max_len=8192,
>>>     crypto_key=b"your-32-byte-aes-key-here!!",  # 32 字节
>>> )
>>> 
>>> # 2. 创建作用域配置（可选）
>>> scope_config = MemoryScopeConfig(
>>>     model_cfg=ModelRequestConfig(
>>>         model="gpt-3.5-turbo",
>>>         temperature=0.1,
>>>     ),
>>>     model_client_cfg=ModelClientConfig(
>>>         client_id="scope_llm",
>>>         client_provider="OpenAI",
>>>         api_key="sk-yyyy",
>>>         api_base="https://api.openai.com/v1",
>>>     ),
>>>     embedding_cfg=EmbeddingConfig(
>>>         model_name="text-embedding-3-small",
>>>         base_url="https://api.openai.com/v1",
>>>         api_key="sk-zzzz",
>>>     ),
>>> )
>>> 
>>> # 3. 创建 Agent 记忆策略配置
>>> agent_config = AgentMemoryConfig(
>>>     mem_variables=[
>>>         Param(
>>>             name="favorite_color",
>>>             description="用户喜欢的颜色",
>>>             type="string",
>>>             required=False,
>>>         ),
>>>         Param(
>>>             name="age",
>>>             description="用户年龄",
>>>             type="number",
>>>             required=False,
>>>         ),
>>>     ],
>>>     enable_long_term_mem=True,
>>>     enable_user_profile=True,
>>>     enable_semantic_memory=True,
>>>     enable_episodic_memory=True,
>>>     enable_summary_memory=True,
>>> )
>>>
```

## Graph Memory 配置

`jiuwen_memory.memory_core.config.config` 中的 `MemoryEngineConfig`、`MemoryScopeConfig`、`AgentMemoryConfig` 和 `DreamingConfig` 面向 `LongTermMemory` 主流程。Graph Memory 使用独立配置模块 `jiuwen_memory.memory_core.config.graph`，主要包含：

- `EpisodeType`：标识写入来源类型，支持对话、文档和 JSON 字符串。
- `AddMemStrategy`：控制图记忆写入阶段的实体召回、实体合并、关系去重和提示词语言。
- `RetrievalStrategy` / `EpisodeRetrievalStrategy`：`AddMemStrategy` 内部使用的召回策略，包含 `top_k`、`min_score`、`same_kind`、`exclude_future_results` 等字段。
- `SearchConfig`：控制图记忆检索阶段的 top_k、分数阈值、混合排序、rerank，以及实体/关系集合上的 BFS 图扩展。

Graph Memory 的图存储连接、向量维度、索引类型和后端参数由 `jiuwen_memory.foundation.store.graph.GraphConfig` 及其子配置管理。详见 [jiuwen_memory.memory_core.graph.graph_memory](graph_memory.md)。
