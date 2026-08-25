# Huge CCA Cyclomatic Complexity 规范修复范围

## 规则与范围

- 规则：`huge_cca_cyclomatic_complexity[python]`，单个方法的 CCA cyclomatic complexity 不得超过 20。
- 分支：`refactor/issue-155`。
- 扫描对象：用户指定路径映射后的 8 个实际 Python 文件；不扫描或修改测试、`.claude/` 和清单外文件。
- 复扫方式：AST 分支节点（`if`、循环、异常处理、条件表达式、匹配、推导式与布尔分支）计数。

## 路径映射与修复记录

| 用户路径 | 实际路径 | 初始超阈值方法 | 修复方式 | 最终位置 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `jiuwen_memory/ai/memory_apiimpl/local_memory_ai.py` | `jiuwen_memory/api/memory_api_impl/local_memory_api.py` | `_normalize_batch_item`、`batch_add_async` | 拆分批量字段校验、scope/source 解析、默认标签校验、元数据合并、批量准备、授权和写入阶段。 | 666、736、998 | 已修复 |
| `jiuwe_memory/retrievalrecaller_impkeyword_recallerpy` | `jiuwen_memory/retrieval/recaller_impl/keyword_recaller.py` | `_expand_by_entities` | 拆分实体哈希、实体查询、贡献计算与 anchor 计算。 | 165 | 已修复 |
| `jiuwen_memory/retrievalrtieer_imli_rriever.y` | `jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py` | `retrieve` | 拆分召回融合、轨迹记录、重排和披露步骤。 | 265 | 已修复 |
| `jiuwe_eor/tutio/asoiar_ml_ssociator.py` | `jiuwen_memory/construction/associator_impl/llm_associator.py` | `_phase2_generate_candidates` | 拆分 coreference、向量和关键词候选生成。 | 448 | 已修复 |
| `jiuwen_memory/construction/index_builder_impl/entity_index_builder.py` | 同原路径 | `_link_group` | 拆分实体收集与链接处理。 | 301 | 已修复 |
| `jiuwen_memory/control/engine_impl/in_memory_engine.py` | 同原路径 | `write`、`delete` | 拆分写入标志、摄入、演化及删除目标、清理、降权、状态迁移步骤。 | 316、718 | 已修复 |
| `bootstrap/core/handler.py` | 同原路径 | `_batch_add` | 拆分默认值解析和项目构建。 | 637 | 已修复 |
| `jiuwen_memory/common/feature_extractor/feature_extractor_impl/hanlp_feature_extractor.py` | 同原路径 | `_extract_with_hanlp` | 拆分分词、词性、关键词和命名实体解析。 | 209 | 已修复 |

## 验证

- AST CCA 复扫确认上述 8 个目标文件中没有方法超过阈值 20。
- 所有拆分均为私有辅助方法，未改变公共签名、同步/异步属性、异常传播、返回值或处理顺序。
- 已通过 `python -m compileall` 与 `git diff --check`。
- 相关现有单元测试通过：`pytest tests/unit/retrieval/test_pipeline_retriever_logging.py tests/unit/control/test_middle_to_long_job.py tests/unit/control/test_middle_to_long_job_lock.py tests/unit/api/test_batch_handler.py tests/unit/api/test_handler_identity_split.py tests/unit/common/test_api_reranker.py`（53 passed）。
