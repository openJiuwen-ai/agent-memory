# agent-memory

`agent-memory` 是面向 Agent 的结构化记忆内核。它把输入规约为可治理的 `MemoryUnit`，
提供写入、检索、演进、生命周期和多租户治理能力；SDK、CLI 与 HTTP 接入面复用同一套
`MemoryAPI` 语义。MCP 接入代码位于 `bootstrap/mcp_server/`，其发布可用性以本版本验证
结论为准。

当前包版本为 `0.2.0`。内核源码位于 [`jiuwen_memory/`](jiuwen_memory/)，安装后使用
`jiuwen_memory.*` 包路径；`bootstrap/` 和 `agent_plugin/` 是独立的接入适配层。

## 当前实现范围

下列内容描述已合入代码的实现范围，不等同于默认启用或已取得生产级发布准入。默认
`assemble()` 使用内存 KV/vector/fulltext/graph、`passthrough` Normalizer、`echo` LLM
和内存 SQLite 审计；可选后端、加密和模型服务均须显式配置。

- **统一核心接口**：`MemoryAPI` 提供 `add` / `batch_add` / `search` / `list` / `get` /
  `update` / `delete` / `evolve`，并在入口完成鉴权和审计。
- **结构化记忆与治理**：`Scope` 隔离、`system_metadata` / `user_metadata` 双命名空间、
  `t_event` / `t_message` 时间字段、生命周期、Space 与权限路由。
- **构建与检索**：写入统一由 `IndexBuilder` 交付正排与派生索引；支持全文、向量、图、
  实体关联召回、RRF/加权 RRF/Max/分层融合与渐进披露。
- **多模态与安全基础**：视频规约、视频记忆抽取和多模态检索的实现及局部测试；认证、凭据、
  绑定策略、限流和工作负载保护的公共契约与接入中间件，SQLite 审计日志和分布式锁。
  视频 ASR/VLM/远程模型链路依赖外部服务，本版本未将其作为默认或 GA 能力。
- **可配置装配**：可路由 Storage，以及内存/SQLite/Redis/PostgreSQL KV、Milvus、
  Elasticsearch、nano_graphrag 等可选目标。`EncryptedKVStore` 仅在显式选择
  `kv_store.default.target=encrypted` 时启用，不是默认 KV。

## V0.2.0 发布验证状态

| 范围 | 分级 | 当前边界 |
| --- | --- | --- |
| 默认离线 `MemoryAPI`、SDK、HTTP、CLI | 已验证实现，尚未授予 GA | Python 3.11 默认装配的写入、检索与 HTTP/CLI 闭环可运行；完整 unit suite 与静态检查仍有未解决失败。 |
| Redis KV | 可配置 | 已在本地 Redis 容器完成 CRUD、Scope 与 TTL 集成验证；不是默认后端。 |
| PostgreSQL/pgvector、Milvus、Elasticsearch | 可配置，发布验证阻塞 | 实际服务可连接并通过基础 CRUD，但元数据过滤或 `recall` 元数据集成回归尚未通过，不能作为经验证的发布后端承诺。 |
| 加密 KV、认证/凭据/密码学、锁与审计 | 可配置 | 有实现和契约测试；加密不是默认开启，Redis 跨实例锁与生产安全策略仍需按部署验收。 |
| 视频规约、视频记忆与多模态检索 | 实验性 | 有实现和局部自动化验证；未使用真实 ASR、VLM、Embedding 或 Rerank 服务做端到端验收。 |
| MCP | 当前不可用 | `.[mcp]` 当前解析的 `mcp 2.x` 已移除实现所导入的 `mcp.server.fastmcp`；修复依赖兼容性并完成受控 transport 验证前，不作为可用接入面声明。 |

完整分级和发布门禁结论见 [Roadmap](docs/Roadmap.md)。

## 快速开始

安装基础依赖：

```bash
pip install -e .
```

在进程内装配默认内存栈并写入、检索：

```python
from jiuwen_memory.api import Context, Scope, assemble

api = assemble()
scope = Scope(user="alice", agent="assistant")

api.add("用户偏好中文回答", scope, identity=scope)
result = api.search("用户偏好什么语言？", Context(scope=scope), identity=scope)
```

运行测试与静态检查：

```bash
pytest -m unit
ruff check
```

## 文档导航

- [Roadmap](docs/Roadmap.md)：当前 `0.2.0` 基线与后续规划
- [架构设计](docs/design/architecture.md)：系统分层、数据流和开放问题
- [接口规约](docs/specs/)：跨模块公共契约
- [特性归档](docs/features/)：已落地特性的决策与验证记录
- [内核地图](jiuwen_memory/AGENTS.md)：当前模块职责和本地约束

## 接入面

`bootstrap/` 提供 SDK、CLI、HTTP 与 MCP 适配代码；其中 SDK、CLI、HTTP 的默认离线闭环已
验证，MCP 的当前依赖兼容性状态见上表。`agent_plugin/` 包含 JiwenSwarm 适配，并为
OpenClaw、Codex 和 Hermes 等外部 Agent 预留适配目录。接入层只做协议和参数转换，业务语义
统一进入 `jiuwen_memory.api.MemoryAPI`。
