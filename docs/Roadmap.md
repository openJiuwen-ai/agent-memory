# agent-memory Roadmap

`agent-memory` 在 1.0 的基础上完成了内核重构，当前发布包版本为 **V0.2.0**。本路线图区分
已合入的实现范围与发布验证分级：代码、接口或局部测试存在不等同于默认启用或 GA。后续条目
是规划草案，随立项与 Issue 调整，不构成实现承诺。

最近一次修订日期：2026-08-27

---

## 版本总览

| 版本 | 定位 | 状态 |
| --- | --- | --- |
| **V0.2.0** | 结构化记忆内核与多形态接入基线 | 实现已合入；发布验证进行中 |
| **V0.2.1** | 存储、构建与治理能力增强 | 规划中 |
| **V0.2.2** | 群体记忆与扩展生态 | 规划中 |

## V0.2.0（实现基线与发布验证）

发布分级采用以下口径：`GA` 要求公开入口、默认或明确配置装配、有效实现与 Python 3.11
自动化验证均通过；`可配置` 需要显式选择后端、密钥或服务；`实验性` 有实现和局部测试但未
取得真实依赖端到端证据；`未实现` 仅有接口、设计或目标契约。

本次 Python 3.11 发布审计已验证默认内存 `MemoryAPI` 写入/检索、HTTP 健康检查与 HTTP 驱动
CLI 写入/检索；但完整 unit suite 尚有两个失败，产品目录 `ruff check jiuwen_memory bootstrap
agent_plugin` 尚有 121 项错误。因此当前不得将 V0.2.0 的任何默认闭环标记为 GA，以下“已合入”
仅表示实现范围。

### 核心接口与治理

- 形态无关的 `MemoryAPI`：`add` / `batch_add` / `search` / `list` / `get` / `update` /
  `delete` / `evolve`，同步与异步入口共享语义。
- `Scope` / Space 多租户隔离、权限路由、授权、治理查询、生命周期和任务调度。
- `system_metadata` / `user_metadata` 双命名空间，`t_event` / `t_message` 双时间语义，
  树形 FilterExpr 和运行时 pipeline 路由。
- 认证、凭据与密码学接口，绑定策略、限流、工作负载保护，内存与 SQLite 审计日志，
  以及用于跨实例临界区的分布式锁。这些是可配置的契约与实现；生产安全策略、Redis 跨实例锁
  和审计完整性保护分别需要部署验收或后续实现。

### 记忆接入、构建与检索

- 文本接入与可插拔规约；视频 ASR/画面规约、视频记忆抽取和多模态检索实现。视频和模型链路
  在未验证真实 ASR/VLM/Embedding/Rerank 服务前均为**实验性**，不是默认路径。
- 由 `IndexBuilder` 统一承接记忆本体（正排）与全文、向量、实体反向索引的写入、更新和删除。
- 抽取、分类、关联、去重、巩固、遗忘和中期到长期记忆任务。
- 关键词、向量、图和实体关联召回；RRF、加权 RRF、Max 与分层融合；L0/L1/L2 渐进披露。

### 存储、接入面与评测

- 统一 `Storage` 门面和按 Space/metadata 路由的后端装配。内存与 SQLite 是默认/本地实现；
  Redis、PostgreSQL KV、Milvus、Elasticsearch、nano_graphrag、Milvus Graph Fusion 与本地 FS
  是显式配置目标。Redis KV 已完成本地服务集成验证；PostgreSQL/pgvector、Milvus、
  Elasticsearch 虽可连接且基础 CRUD 已通过，但元数据过滤或 `recall` 元数据集成回归失败，
  当前不能作为已验证发布后端承诺。
- SDK、CLI、HTTP 适配与 JiwenSwarm `MemoryProvider` 适配。SDK/CLI/HTTP 默认离线闭环已
  验证；MCP 代码仍依赖 `mcp.server.fastmcp`，而 `.[mcp]` 当前解析到的 `mcp 2.x` 已移除该
  模块，故 MCP 在依赖兼容性修复及受控 transport 验证前不列为可用接入面。
- LoCoMo / LongMemEval 评测框架与相关回归测试。

## V0.2.1（规划中）

### 存储与治理

- GaussDB 后端适配。
- 外部 KMS / Vault 密钥提供方、审计完整性保护和更完整的生产级审计存储策略。
- 基于 `SpaceMember.role` 的默认权限矩阵、跨 Space 的多 target API、后端原生
  namespace/tenant 物理删除和精确用量统计。

### 构建与检索

- 跨 unit 树结构：层级构建、区间重建、父子边一致性、`Expander` 展开与层级检索。
- 时序分层记忆、记忆冲突处理优化、多策略遗忘、图扩展闲时巩固、Wiki 文档记忆构建。
- `t_message` 范围过滤、多事件时间数组的索引级过滤，以及不暴露标准端口的一体化 Storage 适配器。

## V0.2.2（规划中）

- 多模态记忆压缩，图片与音频规约，查询驱动的快速多模态处理。
- 动态文件记忆、共享记忆增强构建和群体记忆构建。
- Agent 经验沉淀与 Skill 生成。
- Codex、Hermes 与更多 Agent 生态的正式插件适配。

## 维护原则

1. `V0.2.0` 是当前能力基线；后续版本只增能力或硬化行为，破坏性变更必须提供迁移说明。
2. 影响公开接口、跨模块协调或有明确取舍的特性，按代码、测试、`docs/features` 连续提交归档。
3. `docs/specs/` 描述当前契约，`jiuwen_memory/**/AGENTS.md` 描述当前实现地图；发现与代码不一致时以代码为准同步修订。

## 修订记录

| 日期 | 说明 |
| --- | --- |
| 2026-08-27 | 以 V0.2.0 已合入代码、测试与规格为基线，移入已实现能力并收敛后续规划 |
| 2026-08-03 | 初始版本规划 |
