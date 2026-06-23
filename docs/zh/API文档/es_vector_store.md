# ElasticsearchVectorStore 使用方式与对外约束

## 适用范围

`ElasticsearchVectorStore` 是 `BaseVectorStore` 的 Elasticsearch 后端实现，文件位置：

```text
foundation/store/vector/es_vector_store.py
```

它将一个 vector collection 映射为一个 Elasticsearch index，index 名称格式为：

```text
{index_prefix}__{collection_name}
```

默认 `index_prefix` 为：

```text
agent_vector
```

## 依赖要求

如果 ES 服务端是 8.x，建议使用 8.x Python client：

```bash
pip install "elasticsearch[async]>=8,<9"
```

使用 9.x Python client 连接 8.x 服务端时，可能出现类似错误：

```text
Invalid media-type value on headers [Accept, Content-Type]
Accept version must be either version 8 or 7, but found 9
```

## 创建实例

### 通过 hosts 创建

```python
from foundation.store.vector.es_vector_store import ElasticsearchVectorStore

store = ElasticsearchVectorStore(
    hosts="http://127.0.0.1:9200",
    basic_auth=("elastic", "password"),
    index_prefix="agent_vector",
)
```

### 通过工厂创建

```python
from foundation.store import create_vector_store

store = create_vector_store(
    "elasticsearch",
    hosts="http://127.0.0.1:9200",
    basic_auth=("elastic", "password"),
    index_prefix="agent_vector",
)
```

### 传入已有 AsyncElasticsearch client

```python
from elasticsearch import AsyncElasticsearch
from foundation.store.vector.es_vector_store import ElasticsearchVectorStore

client = AsyncElasticsearch(
    hosts="http://127.0.0.1:9200",
    basic_auth=("elastic", "password"),
)
store = ElasticsearchVectorStore(es=client, index_prefix="agent_vector")
```

使用完成后关闭连接：

```python
await store.close()
```

## 支持的对外接口

`ElasticsearchVectorStore` 实现了 `BaseVectorStore` 要求的接口：

- `create_collection(collection_name, schema, **kwargs)`
- `delete_collection(collection_name, **kwargs)`
- `collection_exists(collection_name, **kwargs)`
- `get_schema(collection_name, **kwargs)`
- `add_docs(collection_name, docs, **kwargs)`
- `search(collection_name, query_vector, vector_field, top_k=5, filters=None, **kwargs)`
- `delete_docs_by_ids(collection_name, ids, **kwargs)`
- `delete_docs_by_filters(collection_name, filters, **kwargs)`
- `list_collection_names()`
- `get_collection_metadata(collection_name)`
- `update_collection_metadata(collection_name, metadata)`
- `update_schema(collection_name, operations)`

## Schema 示例

```python
from foundation.store.base_vector_store import CollectionSchema, FieldSchema, VectorDataType

schema = CollectionSchema(
    fields=[
        FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True, max_length=256),
        FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=4),
        FieldSchema(name="text", dtype=VectorDataType.VARCHAR, max_length=65535),
        FieldSchema(name="category", dtype=VectorDataType.VARCHAR, max_length=128),
        FieldSchema(name="score_value", dtype=VectorDataType.DOUBLE),
        FieldSchema(name="metadata", dtype=VectorDataType.JSON),
    ],
    description="example collection",
)

await store.create_collection("example_docs", schema, distance_metric="COSINE")
```

## 字段类型映射

| VectorDataType | Elasticsearch mapping |
| --- | --- |
| `FLOAT_VECTOR` | `dense_vector`，`index: true` |
| `VARCHAR` | `keyword` |
| `INT64` | `long` |
| `INT32` / `INT16` / `INT8` | `integer` |
| `FLOAT` | `float` |
| `DOUBLE` | `double` |
| `BOOL` | `boolean` |
| `JSON` / `ARRAY` | `object`, `enabled: true` |

向量距离参数映射：

| `distance_metric` | Elasticsearch similarity |
| --- | --- |
| `COSINE` | `cosine` |
| `L2` | `l2_norm` |
| `IP` | `dot_product` |

## Collection 创建约束

### 1. schema 必须包含 FLOAT_VECTOR 字段

`create_collection()` 要求 schema 至少包含一个 `VectorDataType.FLOAT_VECTOR` 字段，否则会抛出 `STORE_VECTOR_SCHEMA_INVALID`。

### 2. FLOAT_VECTOR 字段必须声明 dim

示例：

```python
FieldSchema(name="embedding", dtype=VectorDataType.FLOAT_VECTOR, dim=768)
```

写入向量时，向量长度应与 `dim` 一致。

### 3. collection 已存在时不会重建 index

如果目标 index 已存在，`create_collection()` 会跳过创建流程；如果 index 缺失 metadata，会尝试补写 metadata。

### 4. index mapping 使用 strict 模式

当前实现创建 index 时使用：

```python
{"dynamic": "strict", "properties": properties}
```

因此写入文档必须严格匹配 schema 声明的字段。

## 写入数据约束

### 1. 顶层字段必须在 schema 中声明

如果 schema 只声明了：

```text
id, embedding, text
```

则写入文档不能额外包含：

```python
{"unknown_field": "value"}
```

否则 Elasticsearch 会报：

```text
strict_dynamic_mapping_exception
```

### 2. 主键字段用于 Elasticsearch _id

如果 schema 中存在 `is_primary=True` 字段，`add_docs()` 会使用该字段值作为 Elasticsearch `_id`。

```python
FieldSchema(name="id", dtype=VectorDataType.VARCHAR, is_primary=True)
```

写入：

```python
{"id": "doc-1", "embedding": [0.1, 0.2, 0.3, 0.4]}
```

会写入 ES `_id = "doc-1"`。

如果 schema 没有主键字段，当前实现会尝试使用文档中的 `id` 字段作为 `_id`。

### 3. `None` 值不会写入 `_source`

`add_docs()` 会过滤掉值为 `None` 的字段：

```python
_source = {key: value for key, value in doc.items() if value is not None}
```

### 4. JSON / ARRAY 字段不能随意写入未声明子字段

当前 `JSON` / `ARRAY` 会映射为：

```python
{"type": "object", "enabled": True}
```

同时 index 顶层是 `dynamic: strict`。因此如果 schema 中声明：

```python
FieldSchema(name="metadata", dtype=VectorDataType.JSON)
```

写入下面的数据可能失败：

```python
{
    "id": "doc-1",
    "embedding": [1.0, 0.0, 0.0, 0.0],
    "metadata": {"source": "test", "rank": 1},
}
```

典型错误：

```text
strict_dynamic_mapping_exception: dynamic introduction of [source] within [metadata] is not allowed
```

在不修改 `es_vector_store.py` 的前提下，调用方应避免给 JSON 字段写入带任意子字段的对象。可以选择不传该字段，或传空对象：

```python
"metadata": {}
```

### 5. bulk 写入错误只记录 warning

当前 `add_docs()` 调用 `async_bulk(..., raise_on_error=False)`。如果 bulk 返回 errors，实现会记录 warning，但不会抛出异常。

因此调用方如需强校验写入结果，建议在测试或业务侧额外调用 ES count/search 做确认。

## 写入示例

```python
docs = [
    {
        "id": "doc-1",
        "embedding": [1.0, 0.0, 0.0, 0.0],
        "text": "apple banana fruit",
        "category": "fruit",
        "score_value": 0.95,
        "metadata": {},
    },
    {
        "id": "doc-2",
        "embedding": [0.9, 0.1, 0.0, 0.0],
        "text": "orange fruit",
        "category": "fruit",
        "score_value": 0.85,
        "metadata": {},
    },
]

await store.add_docs("example_docs", docs, batch_size=500)
```

## 查询约束

### 1. search 使用 ES kNN 查询

`search()` 会构造如下 kNN 条件：

```python
{
    "field": vector_field,
    "query_vector": query_vector,
    "k": top_k,
    "num_candidates": max(top_k * 10, 100),
}
```

可通过 `num_candidates` 参数覆盖默认值：

```python
await store.search(
    collection_name="example_docs",
    query_vector=[1.0, 0.0, 0.0, 0.0],
    vector_field="embedding",
    top_k=5,
    num_candidates=200,
)
```

### 2. filters 只支持简单等值匹配和列表匹配

`search()` 和 `delete_docs_by_filters()` 中的 filters 会转换为：

- 标量值：`term`
- list / tuple：`terms`

示例：

```python
filters={"category": "fruit"}
```

会转换为：

```json
{"term": {"category": "fruit"}}
```

```python
filters={"category": ["fruit", "vehicle"]}
```

会转换为：

```json
{"terms": {"category": ["fruit", "vehicle"]}}
```

适合过滤 `keyword`、数值、布尔等标量字段。

### 3. output_fields 控制返回字段

如果传入 `output_fields`，会映射为 ES `_source.includes`：

```python
output_fields=["id", "text", "category"]
```

未传时默认排除内部 `_meta` 字段。

## 查询示例

```python
results = await store.search(
    collection_name="example_docs",
    query_vector=[1.0, 0.0, 0.0, 0.0],
    vector_field="embedding",
    top_k=5,
    filters={"category": "fruit"},
    output_fields=["id", "text", "category", "score_value"],
)

for result in results:
    print(result.score, result.fields)
```

## 删除约束

### 按 ID 删除

```python
await store.delete_docs_by_ids("example_docs", ["doc-1", "doc-2"])
```

该接口按 Elasticsearch `_id` 删除，因此写入时需要确保文档 `_id` 与传入 ID 一致。

### 按 filters 删除

```python
await store.delete_docs_by_filters("example_docs", {"category": "fruit"})
```

filters 的限制与 `search()` 一致，只支持当前实现中的 `term` / `terms` 转换。

## Metadata 约束

`create_collection()` 会写入一条内部 metadata 文档：

```text
__collection_metadata__
```

metadata 包含：

- `schema`
- `distance_metric`
- `vector_field`
- `vector_dim`
- `schema_version`
- `collection_name`
- `primary_key_field`，如果 schema 有主键字段

`update_collection_metadata()` 中如果传入 `schema_version`，必须是非负整数：

```python
await store.update_collection_metadata("example_docs", {"schema_version": 1})
```

非法示例：

```python
await store.update_collection_metadata("example_docs", {"schema_version": -1})
```

## Schema migration 约束

`update_schema()` 通过临时 collection 完成迁移：

1. 计算新 schema。
2. 创建临时 collection。
3. 读取旧 collection 文档。
4. 转换并写入临时 collection。
5. 删除旧 collection。
6. 重新创建原 collection。
7. 从临时 collection 写回数据。
8. 删除临时 collection。

当前实现读取旧文档时使用：

```python
"size": 10000
```

因此超出 10000 条文档的 collection 迁移可能不完整，调用方需要谨慎使用。

## 集成测试

当前仓库提供真实 ES 集成测试：

```text
tests/test_elasticsearch_vector_store_integration.py
```

通过环境变量配置：

```bash
ES_HOSTS="http://127.0.0.1:9200" \
ES_USERNAME="elastic" \
ES_PASSWORD="password" \
pytest tests/test_elasticsearch_vector_store_integration.py -v
```

如果是 HTTPS 自签证书，可设置：

```bash
ES_VERIFY_CERTS=false
```

或：

```bash
ES_CA_CERTS="/path/to/http_ca.crt"
```
