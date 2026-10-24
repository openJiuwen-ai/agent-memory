# F08 — 大文本写路径护栏：预切分 + prompt 截断 + 超时不重试（issue #231）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 影响范围 | `jiuwen_memory/ingest/ingestor_impl/simple_ingestor.py`、`jiuwen_memory/construction/prompt_guard.py`、construction 各 LLM 算子（classifier/extractor/dynamic_llm_extractor/abstractor/associator/router/layer_annotator）、`jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`、`jiuwen_memory/control/engine_impl/cloud_engine.py`、`deploy/docker/online/config.yml` |
| 测试基线 | 新增 9 用例全绿（TDD）；全量 unit 失败集合与基线一致（既有测试隔离 flakiness，零新增） |
| Refs | issue #231（同步 add 500KB 卡死 900s）、PR !352（attempt 键名统一） |

## 背景

同步 add 一条 500KB 文本时：全文单发 LLM 分类 → 出站超时 300s × 重试 3 次 ≈ 900s 无响应。
超时对确定性失败原样重试毫无意义；且 500KB 全文进 prompt 对判定类任务没有信息增益。

## 方案：三层收口 + 精排限长

```
写入侧   SimpleIngestor 预切分：max_unit_chars(默认 8000) 按段落聚合拆 unit
判定侧   prompt_guard.truncate_unit_content：unit.content 进 prompt 前截断
重试侧   LLMClassifier 超时类异常不重试（其余瞬时失败仍按 max_attempts 重试）
检索侧   PipelineRetriever 精排输入限长（query 2000 / doc 4000，可配）
```

### 1. 写入侧预切分（SimpleIngestor）

- `max_unit_chars` 缺省 8000；`<= 0` 是文档化关闭开关（保持旧行为）；显式
  null/空串/非整数在装配期以 `ValidationError` 拒绝——`or 0` 式兜底会把它们
  静默归零、反向关闭切分，与加固意图相反。
- 切分按 `"\n\n"` 段落边界聚合；**段落聚合路径无损可逆**：
  `"\n\n".join(pieces)` 严格还原原文（含首部空段落、冲刷边界上的空段落分隔符
  ——实现用 `buf=None` 哨兵区分"尚未缓冲"与"缓冲了空段落"）。单段超长时硬切
  兜底，硬切点拼接会引入一个段落分隔（不影响记忆语义，不做转义保真）。
- 分片继承同一份（消毒后的）hierarchy 提示与 metadata；多片时
  `system_metadata` 记 `_part`/`_parts` 序号。

### 2. 判定侧 prompt 截断（prompt_guard）

- 判定类任务（分类/路由/关联/分层标注）只依赖主题与结构特征 → 默认
  `DEFAULT_MAX_UNIT_CHARS=4000`（头 3/4 + 尾 1/4 + `[TRUNCATED n chars]` 标记）。
- **内容型任务（抽取/抽象）用 `CONTENT_MAX_UNIT_CHARS=8000`**：抽取的事实
  分布覆盖全篇，中段截断等于静默漏抽。该上限必须 ≥ ingest 预切分上限——否则
  预切分产物仍会被二次截断，中段内容永远进不了抽取/抽象。**两处阈值（8000）
  改任一处须同步**：`simple_ingestor.py` 默认值与 `prompt_guard.CONTENT_MAX_UNIT_CHARS`。
- 护栏只截判定输入，不动存储与索引（embed/全文索引仍用全文），不影响检索质量。

### 3. 重试侧超时识别（LLMClassifier）

- 超时是确定性失败：重试只会把等待叠加成 N×timeout，直接抛出走逐批降级。
- 识别口径：SDK 超时类型不共享基类，主路径按异常类名匹配
  `_TIMEOUT_EXC_NAMES`（openai.APITimeoutError / httpx.ReadTimeout /
  内建 TimeoutError / urllib3 ReadTimeoutError 等），另以内建 `TimeoutError`
  的 `isinstance` 兜底捕获其子类。
- **取舍**：`httpx.ConnectTimeout` 等连接建立超时不在名单内——连接失败可能是
  瞬时抖动，保留重试；只有"请求已发出、等待响应超时"才视为确定性失败。

### 4. 检索侧精排限长（PipelineRetriever）

- 交叉编码器负载 = query 长度 × 候选数；超长 query（rewrite 后可能更长）或
  doc 会把远程 rerank 服务顶满 `reranker_timeout`（issue #231 实测 124.9s）。
- 出厂默认 `_MAX_RERANK_QUERY_CHARS=2000` / `_MAX_RERANK_DOC_CHARS=4000`，
  可经 retriever params `rerank_max_query_chars` / `rerank_max_doc_chars` 覆盖
  （与 `reranker_timeout` 同通道）；上调须自行承担精排超时风险。召回向量化
  仍用全量 query，只截断送精排的文本；相关性以头部为主，故头部截断。

### 5. 配置键（deploy/docker/online/config.yml）

- `classifier_max_attempts: 1`——**注意上游 !352 已统一 attempt 键名并把旧键
  `classifier_retry_max` 列入装配期拒绝清单**，写旧键服务无法启动。
- `llm_timeout: 60.0`（SDK 默认 300s 收敛）；`max_unit_chars: 8000`。

## 其他决策

- **资产映射**：分片把 payload 的全部 assets 引用整份携带（N 倍引用放大），
  不维护分片→资产的精确子集映射——分片可独立检索与披露；复制的是引用而非
  资产本体。记录于 `jiuwen_memory/ingest/AGENTS.md` 本地约束 5。
- **`_part`/`_parts` 消毒**：两键是实现内部序号约定，不对输入开放。用户注入
  的同名键一律剔除，多片时强制覆盖（`setdefault` 会让注入值污染全部分片），
  单片时不残留。
- **cloud_engine**：`classifier.classify` 经 `asyncio.to_thread` 让出事件循环
  线程（同步分类内联在共享事件循环上会冻结全部并发请求）。

## 已知边界

- 硬切点不可逆（join 引入一个段落分隔）——段落聚合路径严格可逆，硬切仅兜底
  单段超长场景。
- 用户把 `max_unit_chars` 配到 8000 以上时，抽取护栏仍停在 8000（静态安全
  上限，不随 ingest 配置联动）——超出部分在抽取 prompt 中仍会被截断，判定类
  截断更早。两层阈值联动的收益不值得引入跨模块配置读取的脆弱性。
- `retry_backoff_ms` 退避等待仍是同步 sleep；非超时重试路径的时延上界 =
  max_attempts × timeout + 退避和，配置 `max_attempts=1` 可彻底消除。

## 验证

- 新增 9 用例（TDD 先 RED 后 GREEN）：ingest 4（切分边界可逆性 ×2、序号消毒、
  max_unit_chars 配置校验）+ classifier 1（超时子类 isinstance 兜底）+
  extractor 2（8000 全文进抽取 prompt / 超限截断有界）+ rerank 限长 2
  （默认上限、参数覆盖）。
- 全量 unit 失败集合与 847af96 基线逐条一致（既有隔离性 flakiness，零新增）。
- GitCode CI：静态检查 / UT / ruff / 禁用词 / 防投毒 / 开源合规全绿。
