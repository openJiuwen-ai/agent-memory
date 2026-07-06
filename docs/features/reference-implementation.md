# 最小参考实现

`src/` 各层定义的是**抽象契约**（`ABC` + `@abstractmethod`）。本文件描述把这些契约
落成「能端到端跑」的**纯内存参考实现**：无任何外部依赖（模型/数据库/服务），跑通
`write → recall → get/update/delete → evolve` 全链路。上线时按同样的装配位置换成真实
后端即可，上层接口不变。

## 落点约定

每个抽象旁边开一个 `<能力>_impl/` 子包，内放具体实现，**文件按实现类名的 snake_case 命名**
（一个 `_impl/` 下可有多个实现）：

```
src/<layer>/<capability>.py              # 抽象契约（或 <capability>/base.py）
src/<layer>/<capability>_impl/
    __init__.py                          # 重导出实现类
    <class_name_snake>.py                # 具体实现，如 in_memory_kv_store.py（InMemoryKVStore）
```

例：`storage/kv.py`（`KVStore`）→ `storage/kv_impl/in_memory_kv_store.py`、
`storage/kv_impl/sqlite_kv_store.py`；`common/tokenizer/base.py`（`Tokenizer`）→
`common/tokenizer/tokenizer_impl/whitespace_tokenizer.py`。

## 装配入口

`src/api/memory_api_impl/local_memory_api.py`：

- `build_kernel(policies=None, kv=None) -> Kernel`：把各层实现串成内核（`api` + 真源 `kv` 句柄）；`kv` 可注入落盘后端。
- `assemble(policies=None) -> LocalMemoryAPI`：只取 `api`，最常用入口。
- 经 `src/api/__init__.py` 重导出：`from api import assemble, build_kernel, Kernel, LocalMemoryAPI`。

`LocalMemoryAPI` 是鉴权 + 审计的执行点（PEP），逐方法委托控制层；同步方法用
`asyncio.run` 桥接引擎异步协程。

## 各能力实现与接入

| 层 | 契约 | 最小实现 | 做什么 / 如何被用 |
|---|---|---|---|
| common | Tokenizer | `WhitespaceTokenizer` | 拉丁成词、CJK 逐字 unigram；建索引与检索共用同实例 |
| common | Normalizer | `PassthroughNormalizer` | `RawPayload` → UTF-8 文本投影 |
| common | Embedder | `HashingEmbedder` | 确定性哈希词袋 + L2 归一化；构建/检索同向量空间 |
| common | Chunker | `FixedWindowChunker` | 定长字符窗口切分；Extractor 据此把长内容拆多条事实 |
| common | FeatureExtractor | `KeywordFeatureExtractor` | 关键词 + 拉丁长词实体；Associator 用它算关联 |
| common | Reranker | `OverlapReranker` | 词重叠精排；PipelineRetriever 在内容物化与后置过滤后重排序 |
| common | LLM | `EchoLLM` | 回显桩；QueryParser 用它改写 query（恒等、可复现） |
| common | AuditLogger | `InMemoryAuditLogger` | 审计事件入内存列表，供治理 `audit` 查询 |
| storage | KVStore | `InMemoryKVStore` | **真源**：存序列化字节；CRUD + `list`（scope 枚举）+ `scopes()` |
| storage | FulltextStore | `InMemoryFulltextStore` | 倒排 + 词重叠打分（KEYWORD 通道） |
| storage | VectorStore | `InMemoryVectorStore` | 余弦 ANN（VECTOR 通道） |
| storage | GraphStore | `InMemoryGraphStore` | 属性图 BFS 邻域 + 关键词种子（GRAPH 通道） |
| storage | FusionStore | `InMemoryFusionStore` | 向量+文本+标量合一融合检索（分离存储的替代形态） |
| storage | FSStore | `InMemoryFSStore` | 原始二进制资产，`ref` 寻址（`MemoryUnit.assets`） |
| common | (codec) | `type_def/memory_codec.py` | `MemoryUnit ↔ bytes`，无状态；只在写入序列化、产出结果时反序列化 |
| ingest | Ingestor | `SimpleIngestor` | `RawPayload` → 规约 → `MemoryUnit`（不落盘） |
| ingest | Source | `TextSource` | 拉取式信息源：`fetch()` → `RawPayload` 列表 |
| construction | IndexBuilder | `HybridIndexBuilder` | 写入时同建倒排 + 向量两套索引（另有纯倒排 `FulltextIndexBuilder`） |
| construction | Classifier | `KeywordClassifier` | 写入路径：判定 `tier` + 主题标签 |
| construction | Extractor | `KeywordExtractor` | 演进 EXTRACT：按 chunk 派生低抽象事实（记血缘） |
| construction | Abstractor | `ConcatAbstractor` | 演进 CONSOLIDATE：升华出 CORE 画像 |
| construction | Associator | `KeywordAssociator` | 演进 ASSOCIATE：共享关键词建关联 |
| construction | Evolver | `OrchestratingEvolver` | 编排 extract/associate/consolidate/forget，产物落 kv + 索引 + 图 |
| retrieval | QueryParser | `SimpleQueryParser` | 去噪 + 分词 + LLM 改写 + 向量化；建议 KEYWORD/GRAPH/VECTOR 通道 |
| retrieval | Recaller | `KeywordRecaller` / `VectorRecaller` / `GraphRecaller` | 三条召回通道 |
| retrieval | Fuser | `RRFFuser` | 倒数排名融合（量纲无关） |
| retrieval | Discloser | `TruncatingDiscloser` | L0/L1/L2 内容塑形；不做点读、过滤或重排 |
| retrieval | Retriever | `PipelineRetriever` | 编排 parse → recall(多路超采样) → fuse → 精排预算截断 → UnitReader/recheck → rerank → 相关性阈值 → 截断 top_k → disclose |
| control | MemoryEngine | `InMemoryEngine` | 接口语义编排中枢 |
| control | LifecycleManager | `KVLifecycleManager` | `delete` 委托其改状态；`sweep` 清扫到期 |
| control | Governor | `InMemoryGovernor` | 检视 / 沿 `supersedes` 回溯 / 审计查询 |
| control | PermissionManager | `AllowAllPermissionManager` | 最小放行（记录授权） |
| control | Scheduler | `InProcessScheduler` | 同步任务记账 |
| control | PolicyManager | `DictPolicyManager` | 内存运行时策略 |

## 主链路

**write**（`api.write`）
```
鉴权 → Ingestor 规约(RawPayload→MemoryUnit) → Classifier 定 tier/标签
     → 序列化落 kv 真源 → HybridIndexBuilder 建倒排+向量 → Scheduler 提交 background
```

**recall**（`api.recall`）
```
鉴权 → QueryParser(去噪+分词+LLM改写+向量化) → [Keyword, Vector, Graph] 三路召回(超采样)
     → RRFFuser 融合 → 精排预算截断 → UnitReader 点读真源 + lifecycle/as_of/event-time/filters 复核
     → 可选 Reranker 精排 → 相关性阈值(min_score/ratio + min_results 兜底) → 截断 top_k
     → TruncatingDiscloser(L0/L1/L2 内容塑形)
```
真源只在 PipelineRetriever 的 UnitReader 阶段按 id 反序列化；Discloser 只消费已点读、已过滤、已排序的候选做内容塑形。

**evolve**（`api.evolve(scope, mode)`）
```
载入 scope 单元 → Evolver：
  EXTRACT     → Extractor(按 chunk 派生事实) → 落 kv + 索引
  CONSOLIDATE → Abstractor(升华画像)         → 落 kv + 索引
  ASSOCIATE   → Associator(关联) → GraphStore(建节点/边)
  FORGET      → 标记 superseded 旧版为 forgotten
```

**get / update / delete**：点读反序列化；update 默认 SUPERSEDE 记版本链；delete 委托
LifecycleManager 非破坏式流转（PURGE 才物理删）。

## 接入策略

- **默认热路径已接入**：tokenizer / normalizer / embedder / chunker / feature_extractor /
  reranker / llm / kv / fulltext / vector / graph / 全部 construction 与 retrieval 算子 /
  全部 control 算子。
- **可选后端 / 拉取式入口**（不挤占默认 3 通道演示，单独可用）：`FusionStore`（分离
  fulltext+vector 的合一替代）、`FSStore`（资产二进制；文本 demo 无二进制流）、
  `TextSource`（拉取式摄入；默认走 `api.write` 直推）。

## 运行

```bash
# API 层端到端（write/recall/get/update/trace/evolve/admin/audit）
PYTHONPATH=src python3 examples/quickstart.py

# CLI surface（同进程 dispatch 路径，Mem0 风格信封）
python3 examples/demo_cli.py

# HTTP surface（HttpServer(Server) 子类）+ CLI over HTTP
scripts/run-server.sh --port 8137 &
scripts/run-cli.sh --server http://127.0.0.1:8137 add "buy milk" -u alice -o text
scripts/run-cli.sh --server http://127.0.0.1:8137 search "milk" -u alice -o text
```

接入 surface 在 `bootstrap/`：共享应用核在 `bootstrap/core/`（`Server` 基类 + 共享
`dispatch` + `profiles` / `config_loader`）；其上 `bootstrap/http_server/`（HTTP `__main__`）、
`bootstrap/mcp_server/`（FastMCP，记忆 API → MCP 工具）、`bootstrap/cli/`（Mem0 风格命令行，
经 `InProcessClient` 复用同一 `dispatch`）各为薄传输适配器，彼此解耦、共用 `core`。
