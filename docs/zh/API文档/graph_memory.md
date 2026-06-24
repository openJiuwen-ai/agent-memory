# memory_core.graph.graph_memory

`memory_core.graph.graph_memory` 是 JiuwenMemory 中的**知识图谱记忆模块**，用于把对话、文档或 JSON 字符串中的长期信息沉淀为实体、关系和事件片段，并支持面向图结构的混合检索。

> **说明**：Graph Memory 当前是独立模块，尚未接入 `LongTermMemory.add_messages` 主流程。使用时需要直接创建 `GraphMemory`，单独注册图存储、LLM 和 Embedding。

## 适用场景

Graph Memory 适合需要“关系理解”的记忆场景，而不仅是按文本相似度召回一段记忆：

- 从多轮对话中沉淀人物、组织、地点、项目、事件之间的关系。
- 将文档中的知识结构化为可检索的实体与事实关系。
- 需要合并同义实体、去重重复关系，并保留事实出现来源。
- 需要同时检索实体、关系和原始片段，并可沿图关系扩展召回。

## 核心概念

Graph Memory 的数据由三类图对象组成：

- `Entity`：实体节点，例如用户、公司、项目、地点、概念等。实体包含 `name`、`content`、`attributes`、关联关系和来源片段。
- `Relation`：实体间关系边，包含 `lhs`、`rhs`、`name`、`content`，以及 `valid_since` / `valid_until` 等时间信息。
- `Episode`：一次输入来源，可以是一段对话、一篇文档或一段 JSON 字符串。Episode 保存原始内容，并记录它提到了哪些实体。

一次 `add_memory` 会返回 `GraphMemUpdate`，用于描述本次写入产生的新增、更新和删除：

- `added_episode` / `updated_episode`
- `added_entity` / `updated_entity` / `removed_entity`
- `added_relation` / `updated_relation` / `removed_relation`

其中 `added_*` 和 `updated_*` 字段保存图对象；`removed_entity` 和 `removed_relation` 是 UUID 字符串集合。

## 快速开始

下面示例展示如何创建一个 Graph Memory 实例，写入一段文档，并检索图记忆。示例中的 LLM、Embedding 参数和向量维度需要替换为你的实际服务。

```python
import asyncio

from foundation.llm import Model
from foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from foundation.store.graph import GraphConfig, GraphStoreIndexConfig
from foundation.store.graph.index_field import MilvusAUTO
from memory_core.config.graph import EpisodeType
from memory_core.graph.graph_memory.base import GraphMemory
from retrieval.common.config import EmbeddingConfig
from retrieval.embedding.api_embedding import APIEmbedding


async def main():
    llm = Model(
        model_config=ModelRequestConfig(model="your-llm-model"),
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_base="https://your-llm-endpoint/v1",
            api_key="your-api-key",
        ),
    )

    embedding = APIEmbedding(
        config=EmbeddingConfig(
            model_name="your-embedding-model",
            base_url="https://your-embedding-endpoint/v1/embeddings",
            api_key="your-api-key",
        )
    )

    graph_config = GraphConfig(
        uri="./graph_memory.db",
        name="agent_memory_graph",
        backend="milvus",
        embed_dim=1024,  # 替换为实际 embedding 维度
        db_embed_config=GraphStoreIndexConfig(
            index_type=MilvusAUTO(),
            distance_metric="cosine",
        ),
    )

    memory = GraphMemory(
        db_config=graph_config,
        llm_client=llm,
        language="cn",
    )
    memory.attach_embedder(embedding)

    update = await memory.add_memory(
        src_type=EpisodeType.DOCUMENT,
        user_id="user_001",
        content="小明在华为云负责数据库产品，他最近在推进 Milvus 图记忆方案。",
    )
    print(update.added_entity)

    result = await memory.search(
        query="谁在推进图记忆方案？",
        user_id="user_001",
        entity=True,
        relation=True,
        episode=True,
    )
    print(result)


asyncio.run(main())
```

## API 入口

### class GraphMemory

```python
class GraphMemory:
    def __init__(
        self,
        db_config: GraphConfig,
        llm_client: Model | None = None,
        llm_structured_output: bool = True,
        reranker: Reranker | None = None,
        extraction_strategy: AddMemStrategy = DEFAULT_STRATEGY,
        db_kwargs: dict | None = None,
        llm_extra_kwargs: dict | None = None,
        language: Literal["cn", "en"] = "cn",
        debug: bool = False,
    ):
        ...
```

`GraphMemory` 是图记忆的主入口，负责调度 LLM 抽取、实体/关系合并、图存储写入和图检索。推荐从 `memory_core.graph.graph_memory.base` 导入 `GraphMemory`；当前 `memory_core.graph.graph_memory` 包入口未重新导出该类。

主要参数：

- `db_config`：图存储配置，当前内置后端为 `milvus`。
- `llm_client`：用于实体抽取、关系抽取、合并去重的 LLM。构造函数允许为空，但调用 `add_memory` 前需要提供可用 LLM，否则写入流程会在调用 LLM 时失败。
- `llm_structured_output`：是否向 LLM 请求结构化 JSON 输出。
- `reranker`：可选的重排器，用于检索结果 rerank。
- `extraction_strategy`：写入策略，控制召回、合并、语言等行为。默认策略中 `chinese_entity=True`，因此即使 `GraphMemory.language="en"`，实体抽取也会使用中文提示词；如果希望完整英文抽取，需要设置 `AddMemStrategy(chinese_entity=False)`。
- `llm_extra_kwargs`：透传给每次 LLM 调用的额外参数。
- `language`：默认提示词语言，支持 `"cn"` 和 `"en"`。
- `debug`：开启后记录 prompt 名称、LLM 输入和输出，便于排查抽取问题。

### attach_embedder

```python
def attach_embedder(self, embedder: Embedding) -> None:
    ...
```

为图存储绑定 Embedding。`add_memory` 和 `search` 都依赖 embedding；如果没有绑定，会抛出 `MEMORY_GRAPH_EMBED_MODEL_NOT_FOUND`。

`GraphConfig.embed_dim` 必须和 `embedder.dimension` 一致，否则 Milvus 后端会拒绝绑定。

### attach_reranker

```python
def attach_reranker(self, reranker: Reranker) -> None:
    ...
```

绑定检索重排器。只有当某个 `SearchConfig` 设置 `rerank=True` 时，检索阶段才会使用它。

### register_search_strategy

```python
def register_search_strategy(
    self,
    name: str,
    search_entity: SearchConfig | None = None,
    search_relation: SearchConfig | None = None,
    search_episode: SearchConfig | None = None,
    force: bool = False,
) -> None:
    ...
```

注册一组检索策略。每个策略包含实体、关系和 Episode 三类集合的检索配置。

默认策略名为 `default`：

- entity：使用 `WeightedRankConfig`，并继承 `SearchConfig` 默认 `top_k=3`、`min_score=0.3`
- relation：`min_score=0.02`，并使用默认 `RRFRankConfig`
- episode：`min_score=0.025`，并使用默认 `RRFRankConfig`

### add_memory

```python
async def add_memory(
    self,
    src_type: EpisodeType,
    user_id: str,
    content: list[BaseMessage | dict] | str,
    content_fmt_kwargs: dict | None = None,
    reference_time: datetime | None = None,
) -> GraphMemUpdate:
    ...
```

向图记忆写入一段内容。

参数说明：

- `src_type`：内容来源类型，见 `EpisodeType`。
- `user_id`：用户标识，写入和检索都会按用户隔离。默认存储配置下长度不能超过 32。`add_memory` 遵循 `GraphStoreStorageConfig.user_id`，`search` 当前要求每个用户标识长度不超过 32。
- `content`：输入内容。对话可以传 `BaseMessage` 列表或 OpenAI 风格 dict；文档和 JSON 通常传字符串。
- `content_fmt_kwargs`：对话格式化参数，例如 `{"user": "张三（用户）", "assistant": "智能客服小李"}`。
- `reference_time`：本段内容发生的参考时间。为空时使用当前时间。

返回值 `GraphMemUpdate` 会记录本次写入新增、更新或删除了哪些 Episode、Entity 和 Relation。

### search

```python
async def search(
    self,
    query: str,
    user_id: str | list[str],
    search_strategy: str = "default",
    *,
    entity: bool = True,
    relation: bool = True,
    episode: bool = True,
    query_embedding: list[float] | None = None,
) -> dict[str, list[tuple[float, BaseGraphObject]]]:
    ...
```

按自然语言查询图记忆。

参数说明：

- `query`：检索文本。
- `user_id`：限定检索用户，可以是单个用户或用户列表。默认存储配置下每个用户标识长度不能超过 32。
- `search_strategy`：使用哪组检索策略。
- `entity` / `relation` / `episode`：是否检索对应集合。
- `query_embedding`：可选的预计算 embedding。为空时使用已绑定的 embedder。

返回值是按集合分组的结果字典，可能的 key 为 `ENTITY_COLLECTION`、`RELATION_COLLECTION`、`EPISODE_COLLECTION`；只有启用检索的集合才会出现在结果中。value 为 `(score, graph_object)` 列表。

## 配置说明

### EpisodeType

```python
class EpisodeType(Enum):
    CONVERSATION = 0
    DOCUMENT = 1
    JSON = 2
```

- `CONVERSATION`：对话内容，通常传 `BaseMessage` 列表或 `{"role": ..., "content": ...}` 列表。
- `DOCUMENT`：文档文本，适合较长的说明、知识库内容或网页正文。
- `JSON`：结构化 JSON 内容，适合已经有字段结构的数据。当前 `add_memory` 仍要求 `content` 传字符串，因此应传 JSON 序列化后的字符串，而不是 Python dict 或 list。

### GraphConfig

`GraphConfig` 描述图存储后端连接和索引参数：

- `uri`：Milvus 连接地址或本地 Milvus Lite 文件路径。
- `name`：Milvus database 名称，默认 `""`。
- `token`：远端 Milvus 鉴权 token，默认 `""`。
- `backend`：后端名称，当前内置为 `"milvus"`，默认 `"milvus"`。
- `timeout`：连接和操作超时时间，默认 `15.0`。
- `extras`：透传给后端客户端的额外参数，例如 Milvus alias。
- `max_concurrent`：图记忆内部并发上限，默认 `10`。
- `embed_dim`：向量维度，必须和 Embedding 维度一致，默认 `512`。
- `embed_batch_size`：写入时批量 embedding 的 batch size，默认 `10`。
- `embedding_model`：可选的初始 Embedding，也可以后续用 `attach_embedder` 设置。
- `db_storage_config`：图对象字段长度和数组大小限制。
- `db_embed_config`：向量索引和距离度量配置，需要提供 `index_type` 和 `distance_metric`。
- `request_max_retries`：图记忆内部 LLM 调用和写入前批量 embedding 的重试次数，默认 `5`。

### GraphStoreIndexConfig

`GraphStoreIndexConfig` 控制图存储里的 ANN 检索索引：

- `index_type`：索引类型，例如 `MilvusAUTO()`、`MilvusHNSW()`、`MilvusFLAT()`。
- `distance_metric`：距离度量，支持 `"cosine"`、`"euclidean"`、`"dot"`。
- `extra_configs`：索引构建的额外配置。
- `bm25_config`：稀疏检索 BM25 参数。
- `bm25_analyzer_settings`：BM25 analyzer 配置。

### AddMemStrategy

`AddMemStrategy` 控制写入阶段的抽取、召回和合并：

- `chinese_entity`：实体抽取是否强制使用中文提示词，默认 `True`。
- `chinese_entity_dedupe`：实体去重是否强制使用中文提示词，默认 `False`。
- `chinese_relation`：关系抽取是否强制使用中文提示词，默认 `False`。
- `skip_uuid_dedupe`：是否跳过 UUID 冲突检查，默认 `False`。
- `recall_episode`：写入前召回相关历史 Episode 的策略，默认包括 `top_k=3`、`min_score=0.025`、`same_kind=False`、`exclude_future_results=True`。
- `recall_entity`：抽取实体后召回已有 Entity 的策略，默认包括 `top_k=3`、`min_score=0.1`、`WeightedRankConfig(name_dense=0.7, content_dense=0.1, content_sparse=0.2)`。
- `recall_relation`：关系去重时召回已有 Relation 的策略，默认包括 `top_k=3`、`min_score=0.02`、`RRFRankConfig`。
- `summary_target`：实体摘要目标长度，默认 `250`。
- `merge_entities`：是否执行实体合并，默认 `True`。
- `merge_relations`：是否执行关系合并，默认 `True`。
- `merge_filter`：实体合并后是否过滤关系，默认 `True`。

### SearchConfig

`SearchConfig` 控制检索阶段：

- `top_k`：返回数量。
- `min_score`：最低相似度阈值。
- `rank_config`：混合检索排序配置，例如 `RRFRankConfig` 或 `WeightedRankConfig`。
- `bfs_k`：图扩展时每层扩展数量。
- `bfs_depth`：图扩展深度。当前仅对实体和关系集合生效，Episode 检索不做图扩展。
- `filter_expr`：额外过滤表达式。
- `output_fields`：返回字段。
- `rerank`：是否使用已绑定的 reranker。
- `language`：检索阶段传给后端和 reranker 的语言参数，默认 `"en"`。它和 `GraphMemory.language` 是两个独立配置。

## 写入流程

`GraphMemory.add_memory` 的核心流程如下：

1. 校验输入，按 `EpisodeType` 格式化内容。
2. 召回与当前内容相关的历史 Episode，作为 LLM 抽取上下文。
3. 创建当前 Episode，记录来源内容和参考时间。
4. 并发启动时区预测，并调用 LLM 抽取实体声明。
5. 对实体名称生成 embedding，并召回已有实体。
6. 调用 LLM 判断新旧实体是否重复，必要时合并实体。
7. 使用时区预测结果辅助关系有效时间解析，并调用 LLM 抽取实体间关系。
8. 为实体生成摘要和属性。
9. 对关系做过滤、分类和语义去重。
10. 后处理实体、关系和 Episode 的双向引用。
11. 批量生成 embedding，写入图存储。
12. 刷新后端并返回 `GraphMemUpdate`。

写入过程中同一 `user_id` 会持有用户级线程锁，避免同一用户的图谱更新互相覆盖。

## 检索流程

`GraphMemory.search` 会同时支持三类集合：

- `ENTITY_COLLECTION`：实体节点。
- `RELATION_COLLECTION`：实体关系。
- `EPISODE_COLLECTION`：原始片段。

检索时会先得到 query embedding，然后按当前 `search_strategy` 并发检索启用的集合。每个集合可以配置自己的 `SearchConfig`，因此可以分别设置不同的 `top_k`、`min_score` 和排序策略；图扩展深度当前只对实体和关系集合生效。

如果设置 `bfs_depth > 0`，底层 `GraphStore` 可以在实体和关系集合上沿图关系做 BFS 扩展召回。如果设置 `rerank=True`，并且 `GraphMemory` 已绑定 reranker，会在候选召回后执行二次排序。

## 图存储后端

图存储抽象定义在 `foundation.store.graph`：

- `GraphStore`：图存储协议，定义添加、查询、删除、搜索、刷新和关闭接口。
- `GraphStoreFactory`：根据 `GraphConfig.backend` 创建后端实例。
- `GraphConfig`：后端连接、索引和并发配置。
- `Entity` / `Relation` / `Episode`：图对象模型。

当前内置后端是 `MilvusGraphStore`。它会维护三类 collection：

- `ENTITY_COLLECTION`
- `RELATION_COLLECTION`
- `EPISODE_COLLECTION`

Milvus 后端支持 dense vector、BM25 sparse vector 和混合排序。`GraphStoreFactory.from_config` 在发现 backend 为 `"milvus"` 时会自动注册 Milvus 支持。

## Prompt 与多语言

Graph Memory 的抽取 prompt 位于：

- `memory_core/graph/extraction/prompts/cn/`
- `memory_core/graph/extraction/prompts/en/`

主要覆盖：

- 实体抽取
- 实体去重
- 实体合并
- 关系抽取
- 关系过滤
- 关系去重
- 时区预测
- 实体摘要生成

结构化输出模型定义在 `memory_core/graph/extraction/extraction_models.py`，并基于 `MultilingualBaseModel` 生成中英文 JSON Schema。

## 常见问题

### GraphMemory 和 LongTermMemory 是什么关系？

当前二者是并行模块。`LongTermMemory` 管理用户画像、语义记忆、情景记忆、变量和摘要等扁平记忆单元；`GraphMemory` 管理 Entity、Relation、Episode 组成的知识图谱。`LongTermMemory.add_messages` 当前不会自动调用 `GraphMemory.add_memory`。

### 为什么 add_memory 报 embedder 未找到？

`add_memory` 和 `search` 都依赖 Embedding。请在使用前调用：

```python
memory.attach_embedder(embedding)
```

或者在 `GraphConfig(embedding_model=embedding, embed_dim=...)` 中提供 embedding。`embed_dim` 仍需和 embedding 实际维度一致。

### 为什么 MilvusGraphStore 说 embed_dim 不一致？

`GraphConfig.embed_dim` 必须等于 `embedding.dimension`。如果不一致，实体、关系和 Episode 的向量字段维度无法匹配，后端会抛出配置错误。

### 什么情况下需要自定义 SearchConfig？

如果希望实体召回更严格、关系召回更宽松，或者希望只对实体/关系启用 BFS、对某类结果启用 rerank，可以注册自定义策略：

```python
from foundation.store.graph.result_ranking import WeightedRankConfig
from memory_core.config.graph import SearchConfig

memory.register_search_strategy(
    "entity_heavy",
    search_entity=SearchConfig(
        top_k=10,
        min_score=0.1,
        rank_config=WeightedRankConfig(name_dense=0.7, content_dense=0.2, content_sparse=0.1),
    ),
    search_relation=SearchConfig(top_k=5, min_score=0.02),
    search_episode=SearchConfig(top_k=3, min_score=0.025),
)
```

### 如何排查 LLM 抽取失败？

创建 `GraphMemory` 时设置 `debug=True`，可以看到调用的 prompt 模板名、LLM 输入和输出。建议同时检查 LLM 是否能稳定返回符合 JSON Schema 的内容。

`GraphConfig.request_max_retries` 控制图记忆内部 LLM 调用和写入前批量 embedding 的重试次数，不代表 Milvus 操作或所有外部服务调用都会自动重试。

## 相关源码

- `memory_core/graph/graph_memory/base.py`
- `memory_core/graph/graph_memory/states.py`
- `memory_core/graph/graph_memory/postprocess_graph_objects.py`
- `memory_core/graph/extraction/`
- `memory_core/config/graph.py`
- `foundation/store/graph/`
- `foundation/store/graph/milvus/`

