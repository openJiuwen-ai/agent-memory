# G.CLS.06 修复范围清单

基线分支：`mem2.0`（当前 HEAD `e8dbcf...`）
规则：类内方法按 `__new__` → 初始化/魔法方法 → `@property` → `@staticmethod` → `@classmethod` → 公开方法 → 保护/私有方法排序。
用户清单中的乱码路径已按仓库实际路径归一化；无法唯一映射的路径不纳入源代码修改。


初始扫描：63 个文件、66 个类，107 个违规关系。


| 文件 | 类 | 违规方法（初始行号 → 最终行号） | 状态 |
|---|---|---|---|
| `agent_plugin/jiuwenswarm/agent_memory_provider.py` | `AgentMemoryMemoryProvider` | `_strip_tui_envelope` (L291->L169, classmethod after `handle_tool_call`) | 已修复 |
| `agent_plugin/jiuwenswarm/agent_memory_provider.py` | `_HttpClient` | `add` (L537->L532, public after `_request`); `_scope_payload` (L609->L520, staticmethod after `close`) | 已修复 |
| `bootstrap/cli/client.py` | `HttpClient` | `call` (L110->L92, public after `_request`) | 已修复 |
| `bootstrap/http_server/__main__.py` | `HttpServer` | `serve` (L85->L44, public after `_handler_cls`) | 已修复 |
| `bootstrap/http_server/__main__.py` | `Handler` | `handle_get` (L56->L61, public after `_send`) | 已修复 |
| `evaluation/benchmark/jsonl_dataset.py` | `JsonlDataset` | `seeds` (L84->L51, public after `_load`) | 已修复 |
| `evaluation/benchmark/locomo_adapter.py` | `LoCoMoDataset` | `seeds` (L151->L84, public after `_require_loaded`) | 已修复 |
| `evaluation/benchmark/longmemeval_adapter.py` | `LongMemEvalDataset` | `seeds` (L149->L71, public after `_require_loaded`) | 已修复 |
| `jiuwen_memory/api/memory_api_impl/local_memory_api.py` | `LocalMemoryAPI` | `add` (L554->L509, public after `_log`); `_batch_error_item` (L620->L412, staticmethod after `add_async`); `_batch_outcome` (L705->L430, staticmethod after `_normalize_batch_item`) | 已修复 |
| `jiuwen_memory/common/audit/audit_impl/sqlite_audit_logger.py` | `SqliteAuditLogger` | `record` (L99->L48, public after `_init_schema`); `query` (L120->L51, public after `_record_many`) | 已修复 |
| `jiuwen_memory/common/chunker/chunker_impl/recursive_chunker.py` | `RecursiveChunker` | `_pick_separator` (L168->L88, staticmethod after `_recursive_split`) | 已修复 |
| `jiuwen_memory/common/embedder/embedder_impl/bge_m3_embedder.py` | `BGEM3Embedder` | `plugin_type` (L151->L74, public after `_load_model`) | 已修复 |
| `jiuwen_memory/common/embedder/embedder_impl/openai_embedder.py` | `OpenAIEmbedder` | `client` (L113->L82, property after `_ensure_client`) | 已修复 |
| `jiuwen_memory/common/factory/factory.py` | `Factory` | `cfg_get` (L137->L54, staticmethod after `dep`) | 已修复 |
| `jiuwen_memory/common/feature_extractor/feature_extractor_impl/hanlp_feature_extractor.py` | `HanlpFeatureExtractor` | `plugin_type` (L189->L127, public after `_init_hanlp`) | 已修复 |
| `jiuwen_memory/common/feature_extractor/feature_extractor_impl/spacy_feature_extractor.py` | `SpacyFeatureExtractor` | `_get_lang_from_model_name` (L251->L226, staticmethod after `_init_spacy`) | 已修复 |
| `jiuwen_memory/common/llm/llm_impl/openai_llm.py` | `OpenAILLM` | `client` (L93->L81, property after `_endpoint`); `health` (L145->L107, public after `_merge_request_options`) | 已修复 |
| `jiuwen_memory/common/lock/lock.py` | `LockProvider` | `renew` (L278->L249, public after `_release`) | 已修复 |
| `jiuwen_memory/common/lock/lock_impl/in_memory_lock.py` | `InMemoryLockProvider` | `renew` (L66->L41, public after `_release`) | 已修复 |
| `jiuwen_memory/common/lock/lock_impl/redis_lock.py` | `RedisLockProvider` | `renew` (L109->L92, public after `_release`) | 已修复 |
| `jiuwen_memory/common/reranker/reranker_impl/api_reranker.py` | `APIReranker` | `plugin_type` (L131->L119, public after `_endpoint`) | 已修复 |
| `jiuwen_memory/common/reranker/reranker_impl/bge_reranker.py` | `BGEReranker` | `plugin_type` (L56->L36, public after `_load_model`) | 已修复 |
| `jiuwen_memory/common/security/security_impl/local_envelope_security_provider.py` | `LocalKeyProvider` | `validate_key_source_or_raise` (L154->L132, public after `_load_or_create_root_key`) | 已修复 |
| `jiuwen_memory/common/tokenizer/tokenizer_impl/jieba_tokenizer.py` | `JiebaTokenizer` | `plugin_type` (L71->L58, public after `_ensure_initialized`) | 已修复 |
| `jiuwen_memory/config/context.py` | `AssemblyContext` | `from_dict` (L79->L55, classmethod after `merged`) | 已修复 |
| `jiuwen_memory/config/routing.py` | `ActiveRouter` | `active_name` (L111->L101, property after `get`) | 已修复 |
| `jiuwen_memory/config/routing.py` | `RoutingStorage` | `security` (L459->L456, property after `_active`); `kv` (L484->L460, property after `has_fs_port`) | 已修复 |
| `jiuwen_memory/construction/abstractor_impl/llm_abstractor.py` | `LLMAbstractor` | `_strip_non_json` (L533->L209, staticmethod after `_parse_llm_response`) | 已修复 |
| `jiuwen_memory/construction/associator_impl/llm_associator.py` | `LLMAssociator` | `_cosine_similarity` (L448->L210, staticmethod after `_phase2_generate_candidates`); `_strip_non_json` (L822->L220, staticmethod after `_parse_llm_response`) | 已修复 |
| `jiuwen_memory/construction/classifier_impl/keyword_classifier.py` | `KeywordClassifier` | `classify` (L52->L39, public after `_topic`) | 已修复 |
| `jiuwen_memory/construction/classifier_impl/llm_classifier.py` | `LLMClassifier` | `_strip_non_json` (L237->L112, staticmethod after `_parse_response`) | 已修复 |
| `jiuwen_memory/construction/evolver_impl/orchestrating_evolver.py` | `OrchestratingEvolver` | `_is_procedural` (L652->L194, staticmethod after `_merge_content`); `evolve` (L807->L211, public after `_annotate_layers`) | 已修复 |
| `jiuwen_memory/construction/extractor_impl/dynamic_llm_extractor.py` | `DynamicLLMExtractor` | `parse_response` (L155->L119, public after `_extract_strategy`) | 已修复 |
| `jiuwen_memory/construction/extractor_impl/llm_extractor.py` | `ExtractorImpl` | `_is_procedural` (L473->L374, staticmethod after `extract`); `preprocess` (L576->L505, public after `_parse_procedural_response`); `build_candidates` (L674->L508, public after `_llm_extract_batch`); `parse_llm_response` (L811->L622, public after `_call_llm_with_retry`); `_log_trailing_json_text` (L857->L382, staticmethod after `parse_llm_response`) | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/fulltext_index_builder.py` | `FulltextIndexBuilder` | `build` (L114->L98, public after `_layer_doc`) | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/vector_index_builder.py` | `VectorIndexBuilder` | `_chunk_tracking_key` (L130->L124, staticmethod after `health`); `_layer_record_id` (L308->L133, staticmethod after `_remove_by_scope`) | 已修复 |
| `jiuwen_memory/construction/layer_annotator_impl/keyword_layer_annotator.py` | `KeywordLayerAnnotator` | `first_sentence` (L72->L36, staticmethod after `_annotate_one`) | 已修复 |
| `jiuwen_memory/construction/prompt_registry.py` | `PromptRegistry` | `from_dict` (L67->L50, classmethod after `has_phase`) | 已修复 |
| `jiuwen_memory/control/engine_impl/cloud_engine.py` | `CloudEngine` | `recall` (L453->L388, public after `_write_default_to_kv`) | 已修复 |
| `jiuwen_memory/control/engine_impl/in_memory_engine.py` | `InMemoryEngine` | `write` (L239->L229, public after `_recall_binding`); `batch_write` (L409->L343, public after `_write_default_to_kv`); `get` (L560->L461, public after `_version_family`) | 已修复 |
| `jiuwen_memory/control/governance_impl/in_memory_governor.py` | `InMemoryGovernor` | `inspect` (L33->L29, public after `_find`) | 已修复 |
| `jiuwen_memory/control/jobs_impl/middle_to_long_job.py` | `MiddleToLongJob` | `_format_for_continuity` (L210->L105, staticmethod after `_run_inner`) | 已修复 |
| `jiuwen_memory/control/permission_impl/sqlite_permission_manager.py` | `SQLitePermissionManager` | `operator_type` (L158->L146, public after `_migrate_schema`) | 已修复 |
| `jiuwen_memory/control/scheduler_impl/async_timer_scheduler.py` | `AsyncTimerScheduler` | `_scope_key` (L92->L86, staticmethod after `health`) | 已修复 |
| `jiuwen_memory/retrieval/discloser_impl/structured_discloser.py` | `StructuredDiscloser` | `_truncate` (L292->L35, staticmethod after `_scope`) | 已修复 |
| `jiuwen_memory/retrieval/discloser_impl/truncating_discloser.py` | `TruncatingDiscloser` | `disclose` (L51->L28, public after `_l1`) | 已修复 |
| `jiuwen_memory/retrieval/fuser_impl/score_max_fuser.py` | `ScoreMaxFuser` | `_normalize_weights` (L114->L44, staticmethod after `fuse`) | 已修复 |
| `jiuwen_memory/retrieval/fuser_impl/weighted_rrf_fuser.py` | `WeightedRRFFuser` | `_normalize_weights` (L90->L32, staticmethod after `fuse`) | 已修复 |
| `jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py` | `KeywordRecaller` | `layer` (L73->L67, property after `health`); `_entity_list_limit` (L237->L77, staticmethod after `_expand_by_entities`) | 已修复 |
| `jiuwen_memory/retrieval/recaller_impl/vector_recaller.py` | `VectorRecaller` | `layer` (L66->L60, property after `health`) | 已修复 |
| `jiuwen_memory/storage/_pg.py` | `PgStoreBase` | `pool` (L201->L178, property after `_close_pool_unlocked`); `sql` (L259->L233, property after `_ensure_schema`); `_lock_schema` (L276->L245, staticmethod after `_qualified`); `_require_vector_extension` (L311->L249, staticmethod after `_require_table`); `close` (L331->L259, public after `_health`) | 已修复 |
| `jiuwen_memory/storage/base.py` | `BaseStore` | `security` (L62->L54, property after `health`) | 已修复 |
| `jiuwen_memory/storage/entity_impl/elasticsearch_entity_store.py` | `ElasticsearchEntityStore` | `find_by_entity_text_hash` (L175->L219, public after `_require_index_ready`); `_parse_bulk_response` (L311->L92, staticmethod after `execute_operations`) | 已修复 |
| `jiuwen_memory/storage/fs_impl/local_fs.py` | `LocalFSStore` | `store_type` (L73->L51, public after `_path`) | 已修复 |
| `jiuwen_memory/storage/fulltext_impl/elasticsearch_fulltext.py` | `ElasticsearchFulltextStore` | `client` (L107->L85, property after `_resolved_hosts`); `_scope_dict` (L203->L111, staticmethod after `_ensure_index`); `_array_marker` (L258->L130, staticmethod after `_to_document`); `store_type` (L319->L188, public after `_scope_filters`); `get` (L386->L249, public after `_missing_ids`); `search` (L425->L268, public after `_analyze_query`) | 已修复 |
| `jiuwen_memory/storage/fusion_impl/in_memory_fusion_store.py` | `InMemoryFusionStore` | `insert` (L89->L86, public after `_index_tokens`) | 已修复 |
| `jiuwen_memory/storage/fusion_impl/milvus_graph_fusion.py` | `MilvusGraphFusionStore` | `_from_vector_record` (L166->L95, staticmethod after `_to_vector_record`) | 已修复 |
| `jiuwen_memory/storage/graph_impl/nano_graphrag_graph.py` | `NanoGraphRAGGraphStore` | `_node_data` (L154->L112, staticmethod after `_persist`) | 已修复 |
| `jiuwen_memory/storage/kv_impl/in_memory_kv_store.py` | `InMemoryKVStore` | `insert` (L54->L43, public after `_live`) | 已修复 |
| `jiuwen_memory/storage/kv_impl/postgres_kv.py` | `PostgresKVStore` | `store_type` (L92->L66, public after `_ensure_schema`); `_expiry_sql` (L99->L56, staticmethod after `health`) | 已修复 |
| `jiuwen_memory/storage/kv_impl/redis_kv.py` | `RedisKVStore` | `client` (L133->L72, property after `_client_key`) | 已修复 |
| `jiuwen_memory/storage/kv_impl/sqlite_kv_store.py` | `SQLiteKVStore` | `store_type` (L93->L66, public after `_require_conn`); `_expiry` (L111->L63, staticmethod after `close`); `insert` (L150->L83, public after `_live_value`) | 已修复 |
| `jiuwen_memory/storage/storage.py` | `Storage` | `kv` (L120->L80, property after `has_fs_port`) | 已修复 |
| `jiuwen_memory/storage/storage_impl/composite_storage.py` | `CompositeStorage` | `security` (L154->L144, property after `bind_recallers`); `has_kv_port` (L171->L190, public after `_has_port`); `kv` (L190->L148, property after `has_fs_port`); `_validate_units` (L253->L172, staticmethod after `_raw_kv`); `list` (L332->L280, public after `_get_units`); `recall_and_get` (L416->L319, public after `_recall`); `retrieve` (L471->L333, public after `_recall_and_get`) | 已修复 |
| `jiuwen_memory/storage/vector_impl/milvus_vector.py` | `MilvusVectorStore` | `client` (L145->L133, property after `_resolved_uri`); `_physical_id` (L193->L154, staticmethod after `_ensure_collection`); `_to_record` (L211->L158, staticmethod after `_row`); `store_type` (L273->L212, public after `_existing_ids`) | 已修复 |
| `jiuwen_memory/storage/vector_impl/pgvector_vector.py` | `PgVectorStore` | `store_type` (L211->L118, public after `_require_dimension`); `_first_duplicate` (L228->L110, staticmethod after `_vector_text`); `insert` (L248->L127, public after `_record_params`); `search` (L400->L227, public after `_apply_knn_settings`) | 已修复 |

## 处理约定

- 仅移动完整方法块，保留装饰器、签名、注释、docstring 和方法体。
- 不改变方法实现、调用关系和公开接口。
- 修复结果：全部条目已更新为 `已修复`，并记录最终行号。

## 扫描结论

- 目标清单复扫：0 个 G.CLS.06 顺序违规。
- `jiuwen_memory/`、`agent_plugin/`、`bootstrap/`、`evaluation/` 全部 Python 源文件复扫：0 个剩余违规。
- 当前工作分支：`codex/gcls06-method-order`。

## 全项目非测试扫描

- 扫描范围：Git 跟踪的开源项目 Python 文件，包含 `jiuwen_memory/`、`agent_plugin/`、`bootstrap/`、`evaluation/`、`deploy/`、`examples/`、`scripts/` 等项目目录。
- 排除范围：`tests/`、测试命名文件、`.claude/` 工具脚本、缓存目录、生成目录及 `jiuwen-test-agent_memory2.0/` 测试工作目录。
- 扫描结果：未发现新增 G.CLS.06 类内方法顺序违规；无需新增修复项。
