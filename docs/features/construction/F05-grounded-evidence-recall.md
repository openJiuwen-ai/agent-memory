# F05 — 原文证据保真与召回闭环

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-07-31 |
| 影响范围 | src/construction/，src/common/embedder/，src/storage/vector_impl/，docs/specs/S05-construction.md，docs/specs/S07-common.md |
| 测试基线 | 针对性 unit 137 passed；LongMemEval official Oracle-500 待同协议复测 |

## 背景

紧凑抽取陈述适合去重和向量检索，但只保存陈述会在表格、多动作、时间和更新冲突中
丢掉回答所需的关系槽位。反过来，把完整 source 复制到每条候选又会放大存储、索引和
去重 prompt。另外，Milvus JSON 路径上的集合过滤若被后端静默拒绝，会让表面正常的混合召回
实际退化为只倒排。

## 决策

- L2 保留紧凑陈述和包含 evidence 的原文窗，默认至少覆盖原文 30% 且不少于
  256 字符；evidence 无法定位时回退完整 source。去重查询和判定 prompt 只读
  `extracted_statement`，避免证据窗放大去重成本。
- 抽取 prompt 要求关系闭包、精确数值/状态与最小完整 evidence；可计数的多个待办动作在
  解析后原子化。陈述发生语言漂移时，只允许用已在原文定位的同语言 evidence 修复。
- 非法 JSON 先做有界格式修复，仍失败才把未改写 source 按空白边界分为不超过 512 字符
  的小块重试。坏候选和坏子批继续隔离，不把基础设施失败伪装成合法空结果。
- L0/L1 保留严格 ID 完整性。单条文本或长度异常只跳过该条；批结构失败时逐条
  重试，单条仍失败时使用严格短于 L2 的原文摘录。
- Milvus 对 JSON 路径的 `IN`/`NOT_IN` 编译为等值 OR/不等值 AND。向量索引增加默认关闭的
  fail-fast 开关，便于需要召回完整性的部署拒绝静默降级。
- OpenAI 兼容 Embedder 归一化 API root/完整 embeddings endpoint，并显式处理 TLS 校验开关。

## 拒绝的方案

- **每条候选复制完整 source**：召回稳健，但候选数越多存储和索引膨胀越明显。
- **只保存紧凑陈述**：成本最低，但不能保证时间、表格、否定和多动作证据可用。
- **新增独立 snapshot 公共接口**：溯源最清晰，但需要扩大 API、存储和装配面；本次使用现有
  MemoryUnit 与 metadata 表达，避免重写 dynamic extractor 及默认装配。
- **在内核中加题型路由、rerank 或评测 prompt**：与通用记忆建模无关，且会使评测无法归因。

## 验证

覆盖证据窗最小覆盖率、不可定位 evidence 回退、语言修复、多动作原子化、候选/子批
失败隔离、分层 ID/单条长度、去重文本、云 Embedder URL/TLS、严格向量索引与
Milvus JSON 集合过滤。

## 已知遗留

- 证据窗会增加 L2 存储和检索 token，需继续跟踪召回收益与上下文成本的平衡。
- 完整 LongMemEval 成绩属于评测协议指标，不作为产品 API 契约。
