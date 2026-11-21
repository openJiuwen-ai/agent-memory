# features 目录页

`docs/features/` 按模块分目录，编号作用域是所在目录（见 [docs/AGENTS.md](../AGENTS.md#命名规约)）。
**编号不表达阅读顺序**；本页按目录列出主题，供按主题查找与按顺序通读。
新增、改名、搬移或删除文档时同步更新本页——它是跨目录入口，不是版本记录。

## 顶层（跨模块特性）（`docs/features/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-system-spec-design](F01-system-spec-design.md) | 系统各层接口规约设计 |
| `F02` | [F02-packaging-unique-toplevel](F02-packaging-unique-toplevel.md) | whl 唯一顶层包名（src → jiuwen_memory） |
| `F03` | [F03-mcp-surface](F03-mcp-surface.md) | MCP surface：MemoryAPI 的 Model Context Protocol 接入面 |
| `F05` | [F05-personal-memory-portal](F05-personal-memory-portal.md) | 个人记忆管理 Portal 设计方案 |
| `F06` | [F06-coding-document-memory](F06-coding-document-memory.md) | Coding 场景接入文档记忆 |
| `F09` | [F09-team-document-memory](F09-team-document-memory.md) | Team 文档记忆、共享隔离与 Pipeline 分流 |

## API / 接入面（`docs/features/api/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-memory-api-impl-design](api/F01-memory-api-impl-design.md) | 记忆接口层实现规约（jiuwen_memory/api/memory_api_impl） |
| `F02` | [F02-write-infer-extract](api/F02-write-infer-extract.md) | write 路径演进策略重构 + infer 同步抽取开关 |
| `F03` | [F03-batch-write-api](api/F03-batch-write-api.md) | batch_add / batch_add_async 批量写入接口设计 |
| `F04` | [F04-memory-metadata-separation](api/F04-memory-metadata-separation.md) | Memory Metadata 双命名空间分离 |
| `F05` | [F05-http-memory-api-alignment](api/F05-http-memory-api-alignment.md) | HTTP / CLI 与 MemoryAPI 一对一对齐 |
| `F06` | [F06-access-parameter-forwarding](api/F06-access-parameter-forwarding.md) | 接入层参数完整转发 |
| `F07` | [F07-access-surface-error-semantics](api/F07-access-surface-error-semantics.md) | 接入层错误语义一致性 |
| `F08` | [F08-md-file-api](api/F08-md-file-api.md) | md 文件级接口（read_md_file / edit_md_file） |
| `F09` | [F09-sdk-runtime-logging](api/F09-sdk-runtime-logging.md) | SDK 运行日志业务接管 |
| `F10` | [F10-batch-update-api](api/F10-batch-update-api.md) | batch_update 批量更新接口设计 |

## common 横切能力（`docs/features/common/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-memory-layer](common/F01-memory-layer.md) | MemoryUnit 内建 L0/L1/L2 内容层 |
| `F02` | [F02-dashscope-llm-provider](common/F02-dashscope-llm-provider.md) | DashScope LLM Provider Adapter |
| `F03` | [F03-scope-space-isolation](common/F03-scope-space-isolation.md) | Scope Space 隔离与目标定位设计 |
| `F04` | [F04-security-interfaces-and-encryption](common/F04-security-interfaces-and-encryption.md) | 安全能力与设计决策 |
| `F05` | [F05-model-service-ssl](common/F05-model-service-ssl.md) | 模型服务 SSL 与本地模型加载 |
| `F06` | [F06-distributed-lock](common/F06-distributed-lock.md) | 分布式锁接口与 Redis 实现 |
| `F07` | [F07-temporal-t-message](common/F07-temporal-t-message.md) | Temporal 新增 t_message —— 消息时间与事件时间分离 |
| `F08` | [F08-memory-tree](common/F08-memory-tree.md) | MemoryUnit 树结构 |
| `F09` | [F09-log-privacy](common/F09-log-privacy.md) | 日志敏感信息脱敏 |
| `F10` | [F10-llm-reranker](common/F10-llm-reranker.md) | LLM Reranker：语义去重与冲突消解 |
| `F11` | [F11-new-trace](common/F11-new-trace.md) | Trace 统一追踪设计 |
| `F12` | [F12-absolute-date-recall](common/F12-absolute-date-recall.md) | 绝对日期两侧共享规则：查询时间窗与写入事件时间（issue #252） |

## config 配置源与路由（`docs/features/config/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-config-source](config/F01-config-source.md) | ConfigSource：六类参数运行时动态配置抽象 |
| `F02` | [F02-routing-storage](config/F02-routing-storage.md) | RoutingStorage：Storage 实例动态配置 |

## construction 构建层（`docs/features/construction/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-construction-spec-design](construction/F01-construction-spec-design.md) | 构建层实现规约（jiuwen_memory/construction/*_impl） |
| `F02` | [F02-dynamic-extraction-consolidation](construction/F02-dynamic-extraction-consolidation.md) | 动态抽取与三步编排巩固 |
| `F03` | [F03-extraction-layer-integrity](construction/F03-extraction-layer-integrity.md) | 抽取与分层完整性最小修复 |
| `F04` | [F04-cc-memory-compat](construction/F04-cc-memory-compat.md) | cc_memory 系统架构、接口与评测报告 |
| `F05` | [F05-construction-spec-multimodal-design](construction/F05-construction-spec-multimodal-design.md) | 多模态记忆接入设计 |
| `F06` | [F06-unified-index-builder](construction/F06-unified-index-builder.md) | 统一存储直写 IndexBuilder（含向量化下传与 _index_ops 抽取） |
| `F07` | [F07-memory-write-entry](construction/F07-memory-write-entry.md) | 记忆写入入口收敛到 IndexBuilder |
| `F08` | [F08-entity-schema-extension](construction/F08-entity-schema-extension.md) | 可选的实体 Schema 属性抽取 |
| `F09` | [F09-agent-llm-extractor](construction/F09-agent-llm-extractor.md) | agent_llm：agent 方向记忆提取 Extractor（中英文可配） |
| `F10` | [F10-schema-conflict-consolidation](construction/F10-schema-conflict-consolidation.md) | Schema 属性记忆的冲突巩固（CONFLICT_CONSOLIDATE） |
| `F11` | [F11-role-memory](construction/F11-role-memory.md) | 角色记忆：职责驱动的关系扩展与跨 Agent 接续设计 |
| `F12` | [F12-importance-and-source-attribution](construction/F12-importance-and-source-attribution.md) | 记忆重要度与来源标识 |
| `F13` | [F13-dreaming](construction/F13-dreaming.md) | dreaming：evolve 定时演进（三态分发 / 候选源 / 持续授权） |

## control 控制层（`docs/features/control/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-control-impl-design](control/F01-control-impl-design.md) | 控制层实现规约（jiuwen_memory/control/*_impl） |
| `F02` | [F02-control-isolation-and-audit](control/F02-control-isolation-and-audit.md) | 控制层隔离与审计（一阶段） |
| `F03` | [F03-control-pipeline-routing](control/F03-control-pipeline-routing.md) | 控制层 Pipeline 路由 |
| `F04` | [F04-permission-context-routing](control/F04-permission-context-routing.md) | 权限上下文路由 |
| `F05` | [F05-cloud-engine-design](control/F05-cloud-engine-design.md) | CloudEngine 读写编排设计 |
| `F06` | [F06-middle-term-memory](control/F06-middle-term-memory.md) | 中期记忆（mem2.0）落地规约 |
| `F07` | [F07-collective-memory-design](control/F07-collective-memory-design.md) | 群体记忆端到端设计 |
| `F08` | [F08-engine-job-builder-alignment](control/F08-engine-job-builder-alignment.md) | Engine 与后台 Job 的 IndexBuilder/Evolver 对齐（E-06 收口） |
| `F09` | [F09-agent-engine-doc-mode-read](control/F09-agent-engine-doc-mode-read.md) | AgentEngine 文档模式读路径经 DomainStore + lifecycle 绕过 |

## ingest 接入层（`docs/features/ingest/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-ingest-impl-design](ingest/F01-ingest-impl-design.md) | 接入层实现规约（jiuwen_memory/ingest/*_impl） |
| `F02` | [F02-assets-ingestor-boundary](ingest/F02-assets-ingestor-boundary.md) | Assets 映射与 Normalizer 能力边界 |

## retrieval 检索层（`docs/features/retrieval/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-retrieval-impl-design](retrieval/F01-retrieval-impl-design.md) | 检索层实现规约（jiuwen_memory/retrieval/*_impl） |
| `F02` | [F02-retrieval-threshold-topk-design](retrieval/F02-retrieval-threshold-topk-design.md) | 检索相关性阈值 + 候选预算重构（jiuwen_memory/retrieval） |
| `F03` | [F03-metadata-filtering](retrieval/F03-metadata-filtering.md) | 元数据过滤设计 |
| `F04` | [F04-score-max-fusion](retrieval/F04-score-max-fusion.md) | 通道内 max 归一化 + 通道间取最大值融合（jiuwen_memory/retrieval） |
| `F05` | [F05-storage-retrieval-pipelines](retrieval/F05-storage-retrieval-pipelines.md) | Storage 能力适配的 Retrieval Pipeline 设计 |
| `F06` | [F06-entity-recall-channel](retrieval/F06-entity-recall-channel.md) | 实体关联召回并入 fulltext L2（hash-only 归并） |
| `F07` | [F07-bm25-scored-fuser](retrieval/F07-bm25-scored-fuser.md) | 融合阶段引入 BM25 词法项（BM25_scored_fusor） |
| `F08` | [F08-schema-temporal-entity](retrieval/F08-schema-temporal-entity.md) | Schema TemporalEntity 双轴检索 |
| `F09` | [F09-query-rewrite](retrieval/F09-query-rewrite.md) | 上下文感知 Query Rewrite |
| `F10` | [F10-staged-retriever-and-selectable-pipelines](retrieval/F10-staged-retriever-and-selectable-pipelines.md) | 分阶段 Retriever 基类与按查询选择检索 pipeline |
| `F11` | [F11-recency-importance-secondary-rank](retrieval/F11-recency-importance-secondary-rank.md) | 检索次级排序：带内按重要度与时效性 tie-break |
| `F12` | [F12-filter-contract-governance](retrieval/F12-filter-contract-governance.md) | filters 契约治理：把公共过滤语言当产品治理 |
| `F13` | [F13-filter-governance-playbook](retrieval/F13-filter-governance-playbook.md) | filters 契约治理落地规范（团队执行版） |

## storage 存储层（`docs/features/storage/`）

| 编号 | 文档 | 主题 |
|---|---|---|
| `F01` | [F01-storage-impl-design](storage/F01-storage-impl-design.md) | 存储层实现规约（jiuwen_memory/storage/*_impl） |
| `F02` | [F02-postgres-backend](storage/F02-postgres-backend.md) | PostgreSQL KV 与 pgvector 存储后端 |
| `F03` | [F03-storage-ssl](storage/F03-storage-ssl.md) | 存储后端 SSL 配置 |
| `F04` | [F04-unified-storage-design](storage/F04-unified-storage-design.md) | 统一 Storage 门面设计 |
| `F05` | [F05-asyncpg-driver](storage/F05-asyncpg-driver.md) | PostgreSQL 驱动从 psycopg 迁移到 asyncpg |
| `F06` | [F06-composite-recaller-assembly](storage/F06-composite-recaller-assembly.md) | CompositeStorage 召回路装配内收 |
| `F07` | [F07-storage-manager-domain-store-split](storage/F07-storage-manager-domain-store-split.md) | Storage 双面拆分（StoreManager/DomainStore）、全局唯一 manager、控制面直连 KV 与 EntityStore 纳管 |
| `F08` | [F08-document-memory](storage/F08-document-memory.md) | 文档记忆（markdown 视图 + 影子索引 + 看门狗） |
| `F09` | [F09-logical-sharding](storage/F09-logical-sharding.md) | 逻辑分片（shard）——分片逻辑由 storage 统一管理，写入透明、读取可按分片收窄 |

---

共 76 篇。`docs/features/common/F04` 是安全能力唯一维护页（见 [docs/AGENTS.md](../AGENTS.md#安全文档例外单一维护)）。
