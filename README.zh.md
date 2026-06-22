# openJiuwen Memory

[中文版](README.zh.md) | [English Version](README.md)

## 简介

**openJiuwen Memory** 是一款面向 AI 智能体的长期记忆模块，为运行在 **openJiuwen** 框架上的智能体提供记忆提取、存储、检索与迁移能力。该模块不仅支持用户画像、语义记忆、情景记忆、变量记忆和摘要记忆等多类型记忆的自动提取与结构化管理；还内置了基于向量检索的语义搜索能力，支持多种存储后端（KV 存储、向量数据库、关系型数据库）的灵活接入与数据迁移；更提供了可插拔的外部记忆提供者（MemoryProvider）机制，支持与 Mem0、AgentArts 等第三方记忆服务的无缝集成。**openJiuwen Memory** 模块兼顾灵活性与安全性，助力开发者高效构建具备持久记忆能力的智能体应用。

## 为什么选择 openJiuwen Memory?

- **多类型记忆自动提取**：通过对对话内容的智能分析，自动提取用户画像、语义记忆、情景记忆、变量和摘要等多类型记忆，无需手动配置规则，大幅降低记忆管理的开发门槛。

- **灵活可扩展的存储架构**：内置 KV 存储、向量数据库、关系型数据库等多种存储后端的抽象接口，支持 Milvus、ChromaDB、PostgreSQL、MySQL、GaussDB、SQLite、Redis 等多种存储引擎，开发者可按需选择和替换。

- **高效精准的语义检索**：基于向量嵌入的语义搜索能力，支持跨记忆类型的统一检索，结合相似度阈值过滤和排序机制，确保检索结果的高相关性与精准性。

- **安全可靠的数据管理**：内置 AES-GCM 加密编解码能力，支持记忆数据加密存储与传输；提供分布式锁机制，确保并发场景下的数据一致性；支持按作用域（scope）的细粒度配置管理。

- **灵活的数据迁移能力**：提供完整的版本化迁移框架，支持 KV 存储、向量数据库、SQL 数据库和消息存储的 schema 迁移，以及跨索引的数据迁移，助力平滑升级。

## 快速开始

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

# 安装所有存储后端
pip install JiuwenMemory[all]
```

### 样例

让我们创建一个简单的长期记忆实例，注册存储后端，添加对话消息并检索记忆：

```python
import asyncio
import tempfile
from sqlalchemy.ext.asyncio import create_async_engine
from memory_core import LongTermMemory
from memory_core.config.config import MemoryEngineConfig, MemoryScopeConfig, AgentMemoryConfig, DreamingConfig
from foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from foundation.llm import UserMessage, AssistantMessage
from foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from foundation.store.db.default_db_store import DefaultDbStore
from foundation.store.vector.chroma_vector_store import ChromaVectorStore
from retrieval.embedding.api_embedding import APIEmbedding
from retrieval.common.config import EmbeddingConfig

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

    # 先启动后台“睡时记忆巩固”。它按自己的定时器在后台运行，作为下面逐轮提取的补充。
    await memory.start_dreaming(
        scope_id="my_app",
        user_id="user_001",
        config=DreamingConfig(enabled=True, interval_seconds=3600, min_session_rounds=2),
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

    # ===== dreaming 完成前 =====
    print("===== dreaming 完成前 =====")
    await show()

    # 待后台睡时整理完成后（首次 sweep 有一小段预热）再看一次
    # ===== dreaming 完成后 =====
    print("===== dreaming 完成后 =====")
    await show()

    await memory.stop_dreaming()  # 关闭时停止

asyncio.run(main())
```

预期输出（记忆内容由大模型生成，措辞会有出入）：
```
===== dreaming 完成前 =====
  记忆生成:
    [user_profile] 用户的职业是数据分析师
    [user_profile] 用户平时用 pandas 处理销售数据
    [user_profile] 用户喜欢打篮球
    [user_profile] 用户爱看科幻小说
    [episodic_memory] 用户在2026年6月18日下午和朋友在公园打了场篮球   # 日期取运行当天，会变
  记忆检索:
    用户平时用 pandas 处理销售数据 (相关度: 0.80)

===== dreaming 完成后 =====
  记忆生成:
    [user_profile] 用户的职业是数据分析师
    [user_profile] 用户平时用 pandas 处理销售数据
    [user_profile] 用户喜欢打篮球
    [user_profile] 用户爱看科幻小说
    [episodic_memory] 用户在2026年6月18日下午和朋友在公园打了场篮球
    [semantic_memory] Polars 通常比 pandas 快 5-10 倍，适合大规模数据处理   # ← dreaming 新增
  记忆检索:
    用户平时用 pandas 处理销售数据 (相关度: 0.80)
    Polars 通常比 pandas 快 5-10 倍，适合大规模数据处理 (相关度: 0.86)
```

> 睡时巩固走的是与在线提取**相同**的写入路径、产出**相同**的记忆类型（`user_profile` / `semantic_memory` / `episodic_memory`），由同一个 `search_user_mem` 检索。它在这里的增量价值就是那条 **Polars 知识**：助手给出的、对用户有长期复用价值的事实，逐轮窄窗口漏掉了，而通读整段会话的 sweep 把它沉淀成了记忆。

## 架构设计

**openJiuwen Memory** 作为 openJiuwen 记忆架构的核心模块，核心能力包括：

* **记忆处理层**：通过对对话消息的智能分析，自动提取用户画像（UserProfile）、语义记忆（SemanticMemory）、情景记忆（EpisodicMemory）、变量（Variable）和摘要（Summary）五类记忆，并支持自定义提取规则和指令式记忆操作（新增、更新、删除）。

* **记忆管理层**：针对不同记忆类型提供差异化的管理策略，包括片段记忆管理器（FragmentMemoryManager）、变量管理器（VariableManager）和摘要管理器（SummaryManager），通过写入管理器（WriteManager）和检索管理器（SearchManager）统一协调读写操作。

* **存储基础层**：提供 KV 存储、向量存储、关系型数据库和消息存储四类抽象接口，支持多种存储后端的灵活接入，并通过版本化迁移框架保障数据 schema 的平滑升级。

* **外部集成层**：通过 MemoryProvider 抽象接口，支持与 Mem0、AgentArts、openJiuwen 和 openViking 等第三方记忆服务的无缝集成，提供统一的工具调用和会话同步机制。

## 功能特性

### **多类型记忆提取**

**openJiuwen Memory** 支持五类记忆的自动提取与管理，功能丰富、开发灵活，可满足不同场景下的智能需求。

- **用户画像（UserProfile）**：提取用户本人的肯定或否定表述，涵盖基本身份、兴趣偏好、人际关系、资产状况等，构建用户个性化画像。
- **语义记忆（SemanticMemory）**：提取对话中涉及的和时间无明确关系的事实性内容或概念，存储通用知识性信息。
- **情景记忆（EpisodicMemory）**：提取对话中涉及的和时间有明确关系的事实性内容或概念，记录带时间戳的事件性信息。
- **变量（Variable）**：从对话中提取结构化的键值对信息，支持自定义变量定义和禁止变量配置。
- **摘要（Summary）**：对对话内容进行自动摘要，支持按轮次生成和增量更新。

### **Dreaming（睡时记忆巩固）**

在线提取（`add_messages`）每次只能看到一轮对话。**Dreaming** 是一个可选的后台服务，会定期重新读取用户已存储的会话，从中提炼出可复用的知识，并通过与在线提取相同的写入路径写回——产出的就是普通的用户画像 / 语义 / 情景记忆单元，不引入新的记忆类型，也不新增存储字段。

- **一次启动，后台自动运行**：`start_dreaming(scope_id, user_id, config=DreamingConfig(enabled=True))` 启动一个后台调度器（对每个 `(scope_id, user_id)` 幂等）；`stop_dreaming()` 停止它。默认关闭。
- **通过 `DreamingConfig` 调节**：sweep 间隔（`interval_seconds`）、会话预过滤（`min_session_rounds`、`max_sessions_per_sweep`）以及提取上限（`max_compress_tokens`、`max_items_per_session`）。
- **带 checkpoint、并发安全**：已扫描的会话在 KV 存储中记录 checkpoint，可跨进程重启保留；写入时持有与 `add_messages` 相同的用户级锁并复用语义冲突检测，因此在线与睡时写入不会冲突或重复。

### **语义检索与冲突检测**

- **向量语义搜索**：基于嵌入模型的向量检索能力，支持跨记忆类型的统一搜索，结合相似度阈值过滤和排序机制。
- **冲突检测与更新**：在写入新记忆前，自动检测与已有记忆的语义冲突，通过语义校验决定是更新还是新增，确保记忆的一致性。
- **指令式记忆操作**：支持通过 LLM 输出指令对已有记忆进行更新和删除，结合语义校验保障操作准确性。

### **灵活的存储后端与数据迁移**

- **多存储后端**：内置 KV 存储（内存、Shelve、Redis、数据库）、向量存储（Milvus、ChromaDB、GaussVector）、关系型数据库（SQLite、PostgreSQL、MySQL、GaussDB）和消息存储四类抽象接口。
- **版本化迁移**：提供完整的迁移框架，支持 SQL schema 变更、向量字段重命名、KV 数据更新、消息数据转换和索引字段操作等多种迁移类型。
- **跨索引迁移**：支持在不同 BaseMemoryIndex 实例之间批量迁移记忆数据，实现存储引擎的平滑切换。

### **安全与并发控制**

- **AES-GCM 加密**：内置加密编解码器，支持记忆数据加密存储，API Key 等敏感信息自动加密保护。
- **分布式锁**：基于 KV 存储的分布式锁机制，确保并发场景下用户级数据操作的原子性与一致性。
- **作用域隔离**：支持按 scope_id 进行记忆数据隔离，每个作用域可独立配置 LLM、嵌入模型和提取规则。

## 项目结构

```
agent-memory/
├── memory_core/                  # 核心记忆模块
│   ├── long_term_memory.py       # 长期记忆引擎入口
│   ├── config/                   # 配置管理
│   │   └── config.py             # 引擎配置、作用域配置、智能体配置
│   ├── manage/                   # 记忆管理
│   │   ├── index/                # 记忆管理器
│   │   │   ├── base_memory_manager.py     # 管理器基类
│   │   │   ├── fragment_memory_manager.py # 片段记忆管理器
│   │   │   ├── variable_manager.py        # 变量管理器
│   │   │   ├── summary_manager.py         # 摘要管理器
│   │   │   └── write_manager.py           # 写入管理器
│   │   ├── search/               # 检索管理
│   │   │   └── search_manager.py # 检索管理器
│   │   ├── update/               # 更新检测
│   │   └── mem_model/            # 数据模型
│   │       ├── memory_unit.py    # 记忆单元定义
│   │       ├── db_model.py       # 数据库模型
│   │       └── sql_db_store.py   # SQL 数据库存储
│   ├── process/                  # 记忆处理
│   │   ├── extract/              # 记忆提取
│   │   │   ├── generation.py     # 记忆生成器
│   │   │   ├── long_term_memory_extractor.py  # 长期记忆提取器
│   │   │   └── memory_analyzer.py # 记忆分析器
│   │   ├── dreaming/             # 睡时记忆巩固
│   │   │   ├── orchestrator.py   # 后台 sweep 调度器
│   │   │   ├── source.py         # 会话来源（读取消息存储）
│   │   │   ├── sweeper.py        # 压缩 -> 提取 -> 写入 流水线
│   │   │   └── store.py          # 将提炼知识写为记忆单元
│   │   └── refine/               # 记忆精炼
│   ├── prompts/                  # 提示词管理
│   │   └── prompt_applier.py     # 提示词模板引擎
│   ├── codec/                    # 编解码
│   │   └── aes_storage_codec.py  # AES 加密编解码器
│   ├── migration/                # 数据迁移
│   │   ├── migration_plan.py     # 迁移计划与注册
│   │   ├── migrator/             # 各类迁移器
│   │   └── operation/            # 迁移操作定义
│   ├── external/                 # 外部集成
│   │   ├── provider.py           # MemoryProvider 抽象接口
│   │   ├── mem0_provider.py      # Mem0 集成
│   │   ├── agentarts_memory_provider.py  # AgentArts 集成
│   │   ├── openjiuwen_memory_provider.py # openJiuwen 集成
│   │   └── openviking_memory_provider.py  # openViking 集成
│   └── common/                   # 公共工具
│       ├── distributed_lock.py   # 分布式锁
│       └── kv_prefix_registry.py # KV 前缀注册
├── foundation/                   # 基础能力层
│   ├── llm/                      # 大模型调用
│   │   ├── model.py              # 模型统一接口
│   │   └── model_clients/        # 多种模型客户端
│   ├── store/                    # 存储抽象
│   │   ├── base_kv_store.py      # KV 存储基类
│   │   ├── base_vector_store.py  # 向量存储基类
│   │   ├── base_db_store.py      # 数据库存储基类
│   │   ├── base_message_store.py # 消息存储基类
│   │   └── base_memory_index.py  # 记忆索引基类
│   ├── prompt/                   # 提示词模板
│   └── tool/                     # 工具定义
├── retrieval/                    # 检索能力
│   └── embedding/                # 嵌入模型
├── common/                       # 公共组件
│   ├── security/                 # 安全工具
│   ├── logging/                  # 日志管理
│   ├── exception/                # 异常处理
│   └── utils/                    # 通用工具
└── tests/                        # 测试用例
```

## 参与贡献

我们欢迎所有形式的贡献，包括但不限于:
- 提交问题和功能建议
- 改进文档
- 提交代码
- 分享使用经验

## 开源许可证

本项目依据 Apache-2.0 许可证授权。
