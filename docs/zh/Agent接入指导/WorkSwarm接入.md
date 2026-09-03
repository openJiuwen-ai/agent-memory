# WorkSwarm 接入

本文说明如何把 WorkSwarm（原 JiuwenSwarm，官网已更名；gitcode 仓库与代码标识仍为 `jiuwenswarm`）接入 agent-memory 记忆引擎，使 Agent 获得跨会话的持久记忆。WorkSwarm 通过 **JiuwenMemory** 外接记忆 provider 接入 agent-memory，支持 `server`（远程 HTTP）与 `sdk`（进程内装配内核）两种模式。

完整的安装、配置与排障步骤维护在 WorkSwarm 仓库的 [JiuwenMemory-SDK 接入指导](https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/zh/JiuwenMemory-SDK%E6%8E%A5%E5%85%A5.md)，本文只概述接入原理、步骤概要与本仓库侧的参考入口。

## 1. 接入原理

### 1.1 两种接入模式

| 模式 | 原理 | 适用场景 |
|---|---|---|
| `server` | 远程 HTTP 调用 agent-memory server（`POST /v1/<verb>`） | 生产部署、多端共享同一个记忆服务 |
| `sdk` | 在 WorkSwarm 进程内装配 agent-memory 内核，直接调用，无 HTTP 跳转 | 单机嵌入、不想额外起 HTTP 服务、对延迟敏感 |

两种模式最终都落到同一组 `MemoryAPI` 语义：`add` 写记忆、`search` 检记忆。上层 Agent 对模式无感。

### 1.2 记忆轨道自动驱动

接入后由 WorkSwarm 的 ExternalMemoryRail（记忆轨道）自动驱动记忆行为，**无需 Agent 主动调用工具**：

```text
before_model_call（模型调用前）
  └─ prefetch(用户本轮 query)        ← 自动检索记忆，命中内容注入 <memory-context> 上下文块

after_invoke（每轮结束）
  └─ sync_turn(用户 query, 助手输出)  ← 自动沉淀当轮对话（可配 LLM 抽取 + 去重）
```

同时向 Agent 暴露 `mem2_search` / `mem2_add` 两件工具，作为显式检索 / 写入的补充手段；单次读写失败不阻塞主对话流程。

## 2. 接入步骤概要

1. **安装内核**：在 WorkSwarm 所在的 Python 环境执行 `pip install JiuwenMemory`，并用 `python -c "from jiuwen_memory.api import assemble"` 验证；
2. **准备后端**：
   - `sdk` 模式：部署 Redis / Milvus / Elasticsearch 三个后端服务（轻量场景可退化为 sqlite / memory 组合）；
   - `server` 模式：按[部署方式概览](../安装指导/部署方式概览.md)启动 agent-memory HTTP 服务；
3. **配置**：在 WorkSwarm 的 `config.yaml` 的 `memory` 段选择 `provider: jiuwenmemory` 并指定 `mode`，`sdk` 模式还需在 `jiwen.sdk` 子段填写三个后端的 `type` / `url`；
4. **验证**：启动后日志出现 `JiuwenMemory provider built` 关键行即挂载成功；对话中让 Agent 记一件事，隔几轮或换会话再问，能召回即接入生效。

每一步的具体命令、字段说明、环境变量总表与常见问题，见 [JiuwenMemory-SDK 接入指导](https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/zh/JiuwenMemory-SDK%E6%8E%A5%E5%85%A5.md)。
