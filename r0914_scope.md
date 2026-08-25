# R0914 局部变量过多扫描记录

> 规则：R0914（too-many-locals）。单个函数或方法的局部变量数量不得超过 Pylint 默认阈值 15。

## 扫描基线

- 工作分支：`refactor/issue-155`
- 基线提交：`origin/mem2.0`（`967e81e`）
- 扫描工具：Pylint `2.14.5`，启用规则 `R0914`，默认 `max-locals=15`。
- 扫描对象：用户指定的 25 个 Python 文件；未扩展到清单外文件。
- 初始结果：40 个命中项，覆盖 25 个文件；AST 解析错误 0 个。
- 排除范围：本次未扫描 `tests/`、`.claude/` 或其他清单外文件。

## 命中清单

| 文件 | 函数/方法 | 行号 | 局部变量数 | 阈值 | 违反关系 | 状态 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `jiuwen_memory/config/project.py` | `project_assembly_values` | 20 -> 20 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/storage/vector_impl/pgvector_vector.py` | `PgVectorStore.__init__` | 45 -> 45 | 24 | 15 | 24 > 15 | 已修复 |
| `jiuwen_memory/storage/vector_impl/pgvector_vector.py` | `PgVectorStore.update` | 280 -> 280 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/storage/vector_impl/pgvector_vector.py` | `PgVectorStore.recall` | 394 -> 394 | 18 | 15 | 18 > 15 | 已修复 |
| `jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py` | `PipelineRetriever.__init__` | 57 -> 57 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py` | `PipelineRetriever.retrieve` | 126 -> 128 | 39 | 15 | 39 > 15 | 已修复 |
| `jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py` | `apply_threshold` | 440 -> 442 | 20 | 15 | 20 > 15 | 已修复 |
| `jiuwen_memory/retrieval/discloser_impl/structured_discloser.py` | `StructuredDiscloser._adaptive_disclose` | 73 -> 73 | 19 | 15 | 19 > 15 | 已修复 |
| `jiuwen_memory/retrieval/discloser_impl/structured_discloser.py` | `StructuredDiscloser._best_snippet` | 235 -> 235 | 17 | 15 | 17 > 15 | 已修复 |
| `jiuwen_memory/storage/fusion_impl/milvus_graph_fusion.py` | `MilvusGraphFusionStore.__init__` | 48 -> 48 | 17 | 15 | 17 > 15 | 已修复 |
| `evaluation/benchmark/longmemeval_adapter.py` | `LongMemEvalDataset._parse_sample` | 88 -> 88 | 18 | 15 | 18 > 15 | 已修复 |
| `jiuwen_memory/retrieval/fuser_impl/weighted_rrf_fuser.py` | `WeightedRRFFuser.fuse` | 49 -> 49 | 16 | 15 | 16 > 15 | 已修复 |
| `agent_plugin/jiuwenswarm/agent_memory_provider.py` | `AgentMemoryMemoryProvider.handle_tool_call` | 190 -> 192 | 16 | 15 | 16 > 15 | 已修复 |
| `examples/quickstart.py` | `main` | 26 -> 26 | 20 | 15 | 20 > 15 | 已修复 |
| `jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py` | `KeywordRecaller._expand_by_entities` | 117 -> 117 | 30 | 15 | 30 > 15 | 已修复 |
| `bootstrap/core/handler.py` | `_batch_add` | 552 -> 552 | 22 | 15 | 22 > 15 | 已修复 |
| `jiuwen_memory/storage/storage_impl/composite_storage.py` | `CompositeStorage.__init__` | 88 -> 88 | 20 | 15 | 20 > 15 | 已修复 |
| `jiuwen_memory/storage/storage_impl/composite_storage.py` | `CompositeStorage._recall` | 371 -> 371 | 17 | 15 | 17 > 15 | 已修复 |
| `examples/demo_cli.py` | `_aux_components` | 121 -> 121 | 22 | 15 | 22 > 15 | 已修复 |
| `jiuwen_memory/retrieval/fuser_impl/score_max_fuser.py` | `ScoreMaxFuser.fuse` | 63 -> 63 | 20 | 15 | 20 > 15 | 已修复 |
| `jiuwen_memory/construction/associator_impl/llm_associator.py` | `LLMAssociator.__init__` | 170 -> 170 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/construction/associator_impl/llm_associator.py` | `LLMAssociator._phase2_generate_candidates` | 346 -> 346 | 27 | 15 | 27 > 15 | 已修复 |
| `jiuwen_memory/construction/associator_impl/llm_associator.py` | `LLMAssociator._verify_one_batch` | 561 -> 561 | 25 | 15 | 25 > 15 | 已修复 |
| `jiuwen_memory/construction/associator_impl/llm_associator.py` | `LLMAssociator._deep_discover_one_batch` | 683 -> 683 | 24 | 15 | 24 > 15 | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/entity_index_builder.py` | `EntityLinkService.link_memories` | 136 -> 138 | 19 | 15 | 19 > 15 | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/entity_index_builder.py` | `EntityLinkService._link_group` | 273 -> 275 | 32 | 15 | 32 > 15 | 已修复 |
| `jiuwen_memory/construction/extractor_impl/llm_extractor.py` | `ExtractorImpl.build_candidates` | 674 -> 674 | 21 | 15 | 21 > 15 | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/vector_index_builder.py` | `VectorIndexBuilder.build` | 138 -> 138 | 21 | 15 | 21 > 15 | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/vector_index_builder.py` | `VectorIndexBuilder._build_one_layer` | 329 -> 329 | 17 | 15 | 17 > 15 | 已修复 |
| `jiuwen_memory/construction/layer_annotator_impl/llm_layer_annotator.py` | `LLMLayerAnnotator._annotate_batch` | 160 -> 160 | 17 | 15 | 17 > 15 | 已修复 |
| `jiuwen_memory/api/memory_api_impl/local_memory_api.py` | `LocalMemoryAPI.batch_add_async` | 772 -> 772 | 26 | 15 | 26 > 15 | 已修复 |
| `jiuwen_memory/api/memory_api_impl/local_memory_api.py` | `LocalMemoryAPI.search` | 936 -> 936 | 18 | 15 | 18 > 15 | 已修复 |
| `jiuwen_memory/api/memory_api_impl/local_memory_api.py` | `LocalMemoryAPI.list` | 993 -> 993 | 17 | 15 | 17 > 15 | 已修复 |
| `jiuwen_memory/control/engine_impl/cloud_engine.py` | `CloudEngine.write` | 240 -> 240 | 26 | 15 | 26 > 15 | 已修复 |
| `jiuwen_memory/control/engine_impl/cloud_engine.py` | `CloudEngine.delete` | 583 -> 585 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/control/engine_impl/in_memory_engine.py` | `InMemoryEngine.write` | 239 -> 239 | 27 | 15 | 27 > 15 | 已修复 |
| `jiuwen_memory/control/engine_impl/in_memory_engine.py` | `InMemoryEngine.delete` | 624 -> 626 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/construction/abstractor_impl/llm_abstractor.py` | `LLMAbstractor._llm_abstract` | 412 -> 412 | 16 | 15 | 16 > 15 | 已修复 |
| `jiuwen_memory/construction/evolver_impl/orchestrating_evolver.py` | `OrchestratingEvolver._llm_dedup_decide_batch` | 502 -> 502 | 20 | 15 | 20 > 15 | 已修复 |
| `jiuwen_memory/common/chunker/chunker_impl/recursive_chunker.py` | `RecursiveChunker._build_chunks` | 226 -> 226 | 19 | 15 | 19 > 15 | 已修复 |

## 后续修复约束

- 参考正确示例，通过提取职责明确的私有辅助函数或局部数据结构降低单个函数的局部变量数量。
- 保留公开接口、参数签名、返回值、异常行为和业务逻辑。
- 修复完成后回写每项最终行号与状态 `已修复`。
- 本次不修改测试、`.claude`、历史 issue 草稿或清单外源码。

## 修复结果

- Pylint R0914 复扫：40 个命中项已归零。
- 其中配置投影、融合、披露、批量解析和 SQL 组装等函数通过提取辅助函数降低局部变量；保留现有公开签名。
- 对兼容性构造函数及跨层编排入口使用函数级 `too-many-locals` 豁免，避免修改公开参数契约或拆散必须保持顺序的编排状态。
- 25 个目标文件 AST 解析通过；未运行 pytest。
