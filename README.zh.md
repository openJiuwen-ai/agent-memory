# JiuwenMemory

[中文版](README.zh.md) | [English Version](README.md)

## 1 简介

**JiuwenMemory**是**openJiuwen 社区**开源，一款专为智能体设计的**自主生长记忆系统（AutoGenetic Memory）**——让记忆从"**信息存储**"向"**自主生长**"进行认知跃迁。它的核心理念是：在AutoGenetic 记忆系统里，每一条记忆都像一段**基因片段**，围绕基因记忆的**精准性、高效性、跨组织复制**构建关键技术能力。

Agent对话系统依赖有限的上下文窗口，一旦超出 Token 限制或跨会话重启，信息便丢失殆尽。团队把这种"失忆"归纳为四个老问题：用户被迫重复提问、个性化能力缺失、跨会话决策前后矛盾、经验始终停留在零点无法积累。当大模型应用进入深水区，决定一个 Agent 体验上限的，早已不只是"答得对不对",而是"能不能持续记住同一个人"。

> "模型能力决定 Agent 的'智力下限'，记忆系统决定 Agent 的'体验上限'。"

**JiuwenMemory** 将 AI 记忆从**被动信息存储**重构为**可治理、可跨平台共享、可自我演化**核心数据资产的记忆系统，致力于让 Agent 真正具备"记住用户、理解用户、服务用户"的能力。

## 2 为什么选择 JiuwenMemory?

### 🧠 记忆精准构建与自动演化

- **分层记忆体系（L0–L3）**：四层渐进架构——L0 原始信息 → L1 摘要记忆 → L2 结构化记忆 → L3 用户画像——各层独立持久存储，信息密度逐级放大，彻底解决长对话记忆丢失与偏好覆盖问题。支持用户画像、语义记忆、情景记忆、变量、摘要多种类型记忆自动提取，灵活配置自定义变量与禁止变量，精准匹配用户需求。

- **Auto Dreaming（睡时记忆巩固）**：借鉴认知神经科学三阶段睡眠范式（浅睡筛选 → REM 提取归类 → 深睡去重消解），后台守护定时触发、忙碌退避与断点续扫，Token 开销线性可控。

- **MemoryTurbo 加速**：对话瞬间写入缓存即完成更新，后台异步调度记忆提取；小模型按话题合并对话后一组提取，保证连贯性并大幅摊销大模型调用次数。

- **知识图谱记忆（Graph Memory）**：支持对话/文档/JSON 多来源写入，LLM 自动抽取实体与关系，支持合并去重、图结构检索与 BFS 扩展，实体/关系/Episode 并行检索加 rerank 精准定位。

### 🔍 记忆高效检索与存储

- **语义检索与冲突检测**：跨记忆类型统一向量语义检索；`MemUpdateChecker` 通过 LLM 分析语义冲突智能决定 ADD/DELETE 策略，LLM 输出 UPDATE/DELETE 指令经语义校验后执行，确保记忆一致与操作可控。

- **全栈存储后端体系**：覆盖 KV（InMemoryKV/ShelveStore/DbBasedKV/Redis）、向量（ChromaDB/Milvus/Elasticsearch/GaussVector）、关系型（SQLite/PostgreSQL/MySQL/GaussDB）、消息（SqlMessageStore）、图（Milvus GraphStore）五大存储类别，适配本地单机到云端集群全场景。

- **数据迁移框架**：支持 KV/向量/SQL/消息/索引的版本化 schema 迁移与跨 BaseMemoryIndex 实例批量数据迁移，操作注册表支持自定义迁移扩展。

### 🔌 生态接入与易用性扩展

- **双维度解耦适配层**：Plugin 维度（钩子式记忆注入，支持 OpenClaw/openJiuwen）和 Provider 维度（统一 `MemoryProvider` 接口，支持 JiuwenMemory/Mem0）独立扩展，实现 N × M 自由组合。

- **REST API 服务与 OpenClaw 插件**：FastAPI 完整 REST API + bearer-token 认证快速接入任意后端；OpenClaw JavaScript 生命周期插件自动记忆存储与召回，零配置即用。

### 🔒 安全隐私加固

- **AES-256-GCM 加密**：透明加密记忆数据与 API Key 保障隐私，敏感信息自动加密保护，防止未授权访问。

- **分布式锁与并发一致**：基于 KV 存储的分布式锁机制，确保多实例并发场景下用户级数据操作的原子性与一致性。

- **多租户安全隔离**：按 `scope_id` 独立配置 LLM/嵌入模型/提取规则并加密存储，实现多租户数据与配置的安全隔离。

## 3 快速开始

### 安装

- 操作系统：兼容 Windows、Linux、macOS。
- Python 版本：Python 的版本应高于或者等于 Python 3.11 版本，并小于 Python 3.14。使用前请检查 Python 版本信息，我们建议使用 3.11.4 版本。

**从 PyPI 安装**

```bash
pip install -U JiuwenMemory
```

**安装可选存储后端**

```bash
# SQLite 支持
pip install JiuwenMemory[sqlite]

# PostgreSQL 支持
pip install JiuwenMemory[postgres]

# MySQL 支持
pip install JiuwenMemory[mysql]

# GaussDB 支持
pip install JiuwenMemory[gaussdb]

# Redis 支持
pip install JiuwenMemory[redis]

# ChromaDB 向量存储
pip install JiuwenMemory[chromadb]

# 记忆服务（含 uvicorn + fastapi，安装后可直接 memory-server 命令启动）
pip install JiuwenMemory[server]

# 安装所有存储后端（含记忆服务）
pip install JiuwenMemory[all]
```

### 样例

让我们创建一个简单的长期记忆实例，注册存储后端，添加对话消息并检索记忆：

```python
import asyncio
import tempfile
from sqlalchemy.ext.asyncio import create_async_engine
from jiuwen_memory.memory_core import LongTermMemory
from jiuwen_memory.memory_core.config.config import MemoryEngineConfig, MemoryScopeConfig, AgentMemoryConfig, DreamingConfig
from jiuwen_memory.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from jiuwen_memory.foundation.llm import UserMessage, AssistantMessage
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.foundation.store.db.default_db_store import DefaultDbStore
from jiuwen_memory.foundation.store.vector.chroma_vector_store import ChromaVectorStore
from jiuwen_memory.retrieval.embedding.api_embedding import APIEmbedding
from jiuwen_memory.retrieval.common.config import EmbeddingConfig

# ============== 配置区：直接在代码中设置，无需 .env ==============
# LLM 配置
MODEL_PROVIDER = "xxxx"
API_BASE = "xxxx"
API_KEY = "xxxx"
MODEL_NAME = "xxxx"

# Embedding 配置
EMBED_MODEL_NAME = "xxxx"
EMBED_API_BASE = "xxxx"
EMBED_API_KEY = "xxxx"
# =================================================================


async def main():
    # 获取 LongTermMemory 单例
    memory = LongTermMemory()

    # 创建大模型配置
    model_client_config = ModelClientConfig(
        client_provider=MODEL_PROVIDER,
        api_key=API_KEY,
        api_base=API_BASE,
        verify_ssl=False,
    )
    model_config = ModelRequestConfig(
        model=MODEL_NAME
    )

    # 创建存储后端（示例使用内存 KV、SQLite 和 ChromaDB）
    kv_store = InMemoryKVStore()
    engine = create_async_engine("sqlite+aiosqlite:///./memory.db")
    db_store = DefaultDbStore(engine)
    vector_store = ChromaVectorStore(persist_directory=tempfile.mkdtemp())

    # 创建嵌入模型
    embedding_config = EmbeddingConfig(
        model_name=EMBED_MODEL_NAME,
        base_url=EMBED_API_BASE,
        api_key=EMBED_API_KEY,
    )
    embedding_model = APIEmbedding(config=embedding_config)

    # 注册存储后端
    await memory.register_store(
        kv_store=kv_store,
        db_store=db_store,
        vector_store=vector_store,
        embedding_model=embedding_model,
    )

    # 配置记忆引擎
    engine_config = MemoryEngineConfig(
        default_model_cfg=model_config,
        default_model_client_cfg=model_client_config,
    )
    memory.set_config(engine_config)

    # 配置作用域
    scope_config = MemoryScopeConfig(
        model_cfg=model_config,
        model_client_cfg=model_client_config,
        embedding_cfg=embedding_config,
    )
    await memory.set_scope_config(scope_id="my_app", memory_scope_config=scope_config)

    # 配置智能体记忆
    agent_config = AgentMemoryConfig(
        enable_long_term_mem=True,
        enable_user_profile=True,
        enable_semantic_memory=True,
        enable_episodic_memory=True,
        enable_summary_memory=True,
    )

    # 逐轮喂入对话——每轮都会实时存储并在线提取记忆
    conversation = [
        ("我是一名数据分析师，平时用 pandas 处理销售数据，但最近流水线太慢了。",
         "可以试试 Polars，它通常比 pandas 快 5-10 倍，很适合大规模数据处理。"),
        ("我平时挺喜欢打篮球，也爱看科幻小说。",
         "劳逸结合不错！如果喜欢硬科幻，刘慈欣的《三体》很值得一读。"),
        ("今天下午我还和朋友在公园打了场篮球，很开心。",
         "听起来是个充实的下午！"),
    ]
    for user_text, assistant_text in conversation:
        await memory.add_messages(
            messages=[UserMessage(content=user_text), AssistantMessage(content=assistant_text)],
            agent_config=agent_config,
            user_id="user_001",
            scope_id="my_app",
            session_id="session_001",
        )

    query = "怎样加速数据处理"

    # 打印该用户当前的全部记忆，并执行一次语义检索
    async def show():
        print("  记忆生成:")
        page = await memory.get_user_mem_by_page(user_id="user_001", scope_id="my_app", page_size=50)
        for m in page:
            print(f"    [{m.type.value}] {m.content}")
        print("  记忆检索:")
        for res in await memory.search_user_mem(query=query, num=5, user_id="user_001", scope_id="my_app"):
            print(f"    {res.mem_info.content} (相关度: {res.score:.2f})")

    await show()

asyncio.run(main())
```

预期输出（记忆内容由大模型生成，措辞会有出入）：
```
记忆生成:
  [user_profile] 用户的职业是数据分析师
  [user_profile] 用户平时用 pandas 处理销售数据
  [user_profile] 用户喜欢打篮球
  [user_profile] 用户爱看科幻小说
  [episodic_memory] 用户在2026年6月18日下午和朋友在公园打了场篮球   # 日期取运行当天，会变
记忆检索:
  用户平时用 pandas 处理销售数据 (相关度: 0.80)
```

> 睡时巩固走的是与在线提取**相同**的写入路径、产出**相同**的记忆类型（`user_profile` / `semantic_memory` / `episodic_memory`），由同一个 `search_user_mem` 检索。它在这里的增量价值就是那条 **Polars 知识**：助手给出的、对用户有长期复用价值的事实，逐轮窄窗口漏掉了，而通读整段会话的 sweep 把它沉淀成了记忆。

## 4 架构设计

**JiuwenMemory** 作为记忆架构的核心模块，核心能力包括：

* **记忆处理层**：通过对对话消息的智能分析，自动提取用户画像（UserProfile）、语义记忆（SemanticMemory）、情景记忆（EpisodicMemory）、变量（Variable）和摘要（Summary）五类记忆，并支持自定义提取规则和指令式记忆操作（新增、更新、删除）。

* **记忆管理层**：针对不同记忆类型提供差异化的管理策略，包括片段记忆管理器（FragmentMemoryManager）、变量管理器（VariableManager）和摘要管理器（SummaryManager），通过写入管理器（WriteManager）和检索管理器（SearchManager）统一协调读写操作。

* **存储基础层**：提供 KV 存储、向量存储、关系型数据库和消息存储四类抽象接口，支持多种存储后端的灵活接入，并通过版本化迁移框架保障数据 schema 的平滑升级。

* **图谱记忆层**：Graph Memory 通过 LLM 从对话、文档或 JSON 字符串中抽取实体（Entity）、关系（Relation）和事件片段（Episode），进行实体合并、关系去重和图结构检索；当前作为独立模块使用，尚未接入 `LongTermMemory.add_messages` 主流程。

* **外部集成层**：通过 MemoryProvider 抽象接口，支持与 Mem0、AgentArts、openJiuwen 和 openViking 等第三方记忆服务的无缝集成，提供统一的工具调用和会话同步机制。

## 5 功能特性

### **分层记忆体系（L0–L3）**

**JiuwenMemory** 引入四层渐进架构，各层独立持久存储，信息密度逐级放大，彻底解决长对话记忆丢失与偏好覆盖问题：

- **L0 — 原始信息**：对话原始消息逐字存储，作为记忆的基础层。
- **L1 — 摘要记忆**：逐轮与增量摘要，压缩对话上下文。
- **L2 — 结构化记忆**：自动提取的语义记忆（SemanticMemory）、情景记忆（EpisodicMemory）和变量（Variable）——按类型组织的知识事实和事件记录。
- **L3 — 用户画像**：关于用户的肯定/否定陈述汇总（身份、偏好、人际关系、资产等），形成个性化长期画像。

支持灵活配置自定义变量与禁止变量，精准匹配用户需求。

### **Dreaming（睡时记忆巩固）**

在线提取（`add_messages`）每次只能看到一轮对话。**Dreaming** 是一个可选的后台服务，会定期重新读取用户已存储的会话，从中提炼出可复用的知识，并通过与在线提取相同的写入路径写回——产出的就是普通的用户画像 / 语义 / 情景记忆单元，不引入新的记忆类型，也不新增存储字段。

- **三阶段睡眠范式**：借鉴认知神经科学——浅睡筛选 → REM 提取归类 → 深睡去重消解——模拟人类睡眠中的记忆巩固过程。
- **一次启动，后台自动运行**：`start_dreaming(scope_id, user_id, config=DreamingConfig(enabled=True))` 启动一个后台调度器（对每个 `(scope_id, user_id)` 幂等）；`stop_dreaming()` 停止它。默认关闭。
- **通过 `DreamingConfig` 调节**：sweep 间隔（`interval_seconds`）、会话预过滤（`min_session_rounds`、`max_sessions_per_sweep`）以及提取上限（`max_compress_tokens`、`max_items_per_session`）。
- **忙碌退避**：系统负载较高时自动退避，不干扰在线服务。
- **带 checkpoint、并发安全**：增量扫描通过 checkpoint 记录进度，可跨进程重启保留；写入时持有与 `add_messages` 相同的用户级锁并复用语义冲突检测，因此在线与睡时写入不会冲突或重复。

### **MemoryTurbo 加速**

对话瞬间写入缓存即完成更新，后台异步调度记忆提取；小模型按话题合并对话后一组提取，保证连贯性并大幅摊销大模型调用次数——记忆飞轮转得更快、耗得更少。

- **动能解耦**：打破记忆系统串行化，将记忆写入和记忆提取解耦——原始对话瞬间写入缓存层的向量库即完成更新，记忆提取在后台根据优先级和算力负载异步调度执行，降低用户感知时延降低92%。
- **离心式语义聚类**：异步提取前由小模型按话题分类合并对话，一组对话一起提取。保证话题连贯性，避免多次经过大模型提取造成的语义漂移；相比传统每轮对话都进行大模型调用，大幅摊销提取次数和Token使用量。
- **提前检索与精度保障**：后台异步未完成前即可检索——缓存层原始对话具备向量embedding可检索，结果为缓存层与提取层整合，保证时延降低的同时精度不变。

### **语义检索与冲突检测**

- **统一向量语义搜索**：基于嵌入模型的跨记忆类型向量检索，结合相似度阈值过滤和排序机制。
- **MemUpdateChecker**：通过 LLM 分析语义冲突智能决定 ADD/DELETE 策略——在写入新记忆前，自动检测与已有记忆的语义冲突，确保记忆一致性。
- **指令式记忆操作**：支持通过 LLM 输出 UPDATE/DELETE 指令对已有记忆进行更新和删除，经语义校验后执行，确保操作准确可控。

### **Graph Memory（知识图谱记忆）**

Graph Memory 是独立的知识图谱记忆模块，可将输入内容沉淀为实体、关系和事件片段组成的图结构，适用于需要关系检索、实体追踪和图扩展召回的场景。

- **多来源写入**：支持对话、文档和 JSON 字符串三种 `EpisodeType` 输入，将原始片段保存为 Episode。
- **实体与关系抽取**：通过 LLM 抽取实体声明、实体摘要、属性和实体间关系，并记录关系有效时间。
- **合并与去重**：写入时召回已有实体和关系，执行实体合并、关系过滤与语义去重，减少重复节点和边。
- **图结构检索**：支持按实体、关系和 Episode 三类集合并行检索，可配置混合排序、rerank，以及实体/关系结果的 BFS 图扩展。

[→ Graph Memory API 文档](docs/zh/API文档/graph_memory.md)

### **灵活的存储后端与数据迁移**

- **全栈存储后端**：覆盖 KV（InMemoryKV/ShelveStore/DbBasedKV/Redis）、向量（ChromaDB/Milvus/Elasticsearch/GaussVector）、关系型（SQLite/PostgreSQL/MySQL/GaussDB）、消息（SqlMessageStore）、图（Milvus GraphStore）五大存储类别，适配本地单机到云端集群全场景。
- **版本化迁移**：提供完整的迁移框架，支持 SQL schema 变更、向量字段重命名、KV 数据更新、消息数据转换和索引字段操作等多种迁移类型。
- **跨索引迁移**：支持在不同 BaseMemoryIndex 实例之间批量迁移记忆数据，操作注册表支持自定义迁移扩展，实现存储引擎的平滑切换。

### **安全与并发控制**

- **AES-256-GCM 加密**：透明加密记忆数据与 API Key 保障隐私，敏感信息自动加密保护。
- **分布式锁**：基于 KV 存储的分布式锁机制，确保多实例并发场景下用户级数据操作的原子性与一致性。
- **多租户作用域隔离**：支持按 `scope_id` 进行记忆数据隔离，每个作用域独立配置 LLM、嵌入模型和提取规则，配置数据加密存储，实现多租户安全隔离。

### **双维度解耦适配层**

JiuwenMemory 通过两个独立维度将 Agent 平台与记忆引擎彻底解耦：

- **Plugin 维度**：钩子式记忆注入，支持 JiuwenSwarm/OpenClaw/openJiuwen——在 Agent 回复前注入相关上下文，在回复后捕获对话并提取记忆。
- **Provider 维度**：统一 `MemoryProvider` 接口，支持 JiuwenMemory/Mem0/openViking/AgentArts——可随时更换引擎，无需修改 Agent 代码。

两个维度独立扩展，实现平台与引擎的 N × M 自由组合。

### **多模型 LLM 客户端**

支持 OpenAI、DashScope、DeepSeek、SiliconFlow、OpenRouter、InferenceAffinity、IntelliRouter 等多种推理引擎，灵活选择最优推理服务。

## 6 记忆服务与 OpenClaw 插件

一行命令启动记忆后端，配一个 OpenClaw 插件即可让智能体拥有持久、可检索的记忆——无需额外开发。

### 记忆服务

一条命令启动本地记忆引擎，完整 REST API：

- **记忆读写** — 添加消息、增删改记忆、管理键值变量。
- **语义搜索** — 按含义检索，不是关键词匹配。
- **零配置起步** — 安装 `JiuwenMemory[server]` 后，把 LLM 和 Embedding 的 key 填入 `~/.jiuwenmemory/.env`，执行 `memory-server` 即可启动。

```bash
# 安装记忆服务
pip install JiuwenMemory[server]

# 创建配置目录并编辑 .env（可参考源码仓库 server/.env.example）
mkdir -p ~/.jiuwenmemory
cp server/.env.example ~/.jiuwenmemory/.env   # 或手动创建
vim ~/.jiuwenmemory/.env                       # 填入 API Key 等配置

# 启动服务
memory-server

# 源码启动（开发时仍可使用）
python -m server.memory_server
```

配置文件和数据目录统一存放在 `~/.jiuwenmemory/` 下：

```
~/.jiuwenmemory/
├── .env              ← 环境配置（LLM / Embedding / 存储后端等）
├── memory_data/      ← 数据目录（SQLite / ChromaDB 等，自动创建）
```

### MCP 服务

同一套记忆引擎还以 **MCP（Model Context Protocol）服务**形式暴露，兼容 MCP 的客户端（Claude Code、Codex、Cursor、VS Code ……）可直接调用记忆工具，无需编写 HTTP 客户端代码。MCP 进程**进程内**持有 `LongTermMemory` 引擎（首次工具调用时延迟装配，初始化失败也不崩溃）。

```bash
# 安装 [server] extras（提供 mcp + uvicorn）
pip install JiuwenMemory[server]

# 启动 MCP 服务（默认 Streamable HTTP，地址 http://127.0.0.1:8765/mcp）
memory-mcp

# 或源码启动
python -m jiuwen_memory.server.mcp_server
```

客户端按 URL 连接即可，工具包括 `add_messages`、`search_memories`、`search_history_summaries`、`get_memories`、`update_memory`、`delete_memory`、`delete_all_memories` 以及 `health_check`。

[→ MCP Server API 文档](docs/zh/API文档/mcp_server.md)

### OpenClaw 插件

OpenClaw 智能体的"自动记忆"——记住用户说过什么，在每次回复前自动召回。

- **回复前先回忆** — 注入相关历史上下文，智能体不再"从零开始"。
- **回复后自动存储** — 捕获每一轮对话，后台提取结构化记忆。

[→ 完整安装指引](agent-memory-plugin/JiuwenMemory-OpenClaw/README.md)

## 7 项目结构

```
agent-memory/
├── jiuwen_memory/                # 主包
│   ├── memory_core/                  # 核心记忆模块
│   │   ├── long_term_memory.py       # 长期记忆引擎入口
│   │   ├── config/                   # 配置管理
│   │   │   ├── config.py             # 引擎配置、作用域配置、智能体配置
│   │   │   └── graph.py              # Graph Memory 写入与检索策略配置
│   │   ├── manage/                   # 记忆管理
│   │   │   ├── index/                # 记忆管理器
│   │   │   │   ├── base_memory_manager.py     # 管理器基类
│   │   │   │   ├── fragment_memory_manager.py # 片段记忆管理器
│   │   │   │   ├── variable_manager.py        # 变量管理器
│   │   │   │   ├── summary_manager.py         # 摘要管理器
│   │   │   │   └── write_manager.py           # 写入管理器
│   │   │   ├── search/               # 检索管理
│   │   │   │   └── search_manager.py # 检索管理器
│   │   │   ├── update/               # 更新检测
│   │   │   └── mem_model/            # 数据模型
│   │   │       ├── memory_unit.py    # 记忆单元定义
│   │   │       ├── db_model.py       # 数据库模型
│   │   │       └── sql_db_store.py   # SQL 数据库存储
│   │   ├── process/                  # 记忆处理
│   │   │   ├── extract/              # 记忆提取
│   │   │   │   ├── generation.py     # 记忆生成器
│   │   │   │   ├── long_term_memory_extractor.py  # 长期记忆提取器
│   │   │   │   └── memory_analyzer.py # 记忆分析器
│   │   │   ├── dreaming/             # 睡时记忆巩固
│   │   │   │   ├── orchestrator.py   # 后台 sweep 调度器
│   │   │   │   ├── source.py         # 会话来源（读取消息存储）
│   │   │   │   ├── sweeper.py        # 压缩 -> 提取 -> 写入 流水线
│   │   │   │   └── store.py          # 将提炼知识写为记忆单元
│   │   │   └── refine/               # 记忆精炼
│   │   ├── graph/                    # 知识图谱记忆
│   │   │   ├── graph_memory/         # GraphMemory 写入、检索和状态管理
│   │   │   └── extraction/           # 实体/关系抽取模型与提示词
│   │   ├── prompts/                  # 提示词管理
│   │   │   └── prompt_applier.py     # 提示词模板引擎
│   │   ├── codec/                    # 编解码
│   │   │   └── aes_storage_codec.py  # AES 加密编解码器
│   │   ├── migration/                # 数据迁移
│   │   │   ├── migration_plan.py     # 迁移计划与注册
│   │   │   ├── migrator/             # 各类迁移器
│   │   │   └── operation/            # 迁移操作定义
│   │   ├── external/                 # 外部集成
│   │   │   ├── provider.py           # MemoryProvider 抽象接口
│   │   │   ├── mem0_provider.py      # Mem0 集成
│   │   │   ├── agentarts_memory_provider.py  # AgentArts 集成
│   │   │   ├── openjiuwen_memory_provider.py # openJiuwen 集成
│   │   │   └── openviking_memory_provider.py  # openViking 集成
│   │   └── common/                   # 公共工具
│   │       ├── distributed_lock.py   # 分布式锁
│   │       └── kv_prefix_registry.py # KV 前缀注册
│   ├── foundation/                   # 基础能力层
│   │   ├── llm/                      # 大模型调用
│   │   │   ├── model.py              # 模型统一接口
│   │   │   └── model_clients/        # 多种模型客户端
│   │   ├── store/                    # 存储抽象
│   │   │   ├── base_kv_store.py      # KV 存储基类
│   │   │   ├── base_vector_store.py  # 向量存储基类
│   │   │   ├── base_db_store.py      # 数据库存储基类
│   │   │   ├── base_message_store.py # 消息存储基类
│   │   │   ├── base_memory_index.py  # 记忆索引基类
│   │   │   └── graph/                # 图存储抽象与 Milvus 图存储实现
│   │   ├── prompt/                   # 提示词模板
│   │   └── tool/                     # 工具定义
│   ├── retrieval/                    # 检索能力
│   │   └── embedding/                # 嵌入模型
│   ├── common/                       # 公共组件
│   │   ├── security/                 # 安全工具
│   │   ├── logging/                  # 日志管理
│   │   ├── exception/                # 异常处理
│   │   └── utils/                    # 通用工具
│   ├── server/                       # 记忆服务（FastAPI）
│   │   ├── __init__.py               # 包初始化
│   │   ├── memory_server.py          # HTTP API 服务（CLI 入口 main()）
│   │   ├── store_factory.py          # 存储后端工厂
│   │   └── .env.example              # 环境变量模板
│   └── agent-memory-plugin/          # OpenClaw 生命周期插件
│       ├── lib/                      # 插件库
│       │   └── openjiuwen-memory-api.js # 记忆 API 客户端
│       ├── openjiuwen-memory-index.js # 插件入口
│       ├── openclaw.plugin.json      # 插件清单
│       ├── package.json              # npm 包配置
│       └── README.md                 # 插件文档
├── docs/                             # 文档
└── tests/                            # 测试用例
```

## 参与贡献

我们欢迎所有形式的贡献，包括但不限于:
- 提交问题和功能建议
- 改进文档
- 提交代码
- 分享使用经验

## 开源许可证

本项目依据 Apache-2.0 许可证授权。
