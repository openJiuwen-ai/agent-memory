# Agent 记忆系统竞品调研（Competitor Analysis）

> 调研对象：业界主流的「Agent 长期记忆 / Memory Layer」产品与框架
> 用途：为 `agent-memory` 记忆系统的架构设计与差异化定位提供参考
> 调研时间：2026-05
> 维度：支持的 Agent 类型、核心架构（信息源 / 索引检索方式 / 模态支持 / 端云支持）、关键特性、优缺点
>
> 说明：本文「模态支持」特指**输入信息源的模态**（即写入记忆的原始数据是纯文本、还是含图像 / 音频 / 视频 / 文档 / 工具轨迹等），而非输出模态。

---

## 0. 阅读指南

「Agent 记忆」与传统 RAG 的核心区别在于：RAG 是无状态的文档检索（对所有人返回相同结果），而 Memory 是**对用户/会话随时间演化的事实进行抽取、更新、消解冲突与个性化召回**。因此本调研重点关注各产品在以下四件事上的取舍：

1. **支持的 Agent 类型（Who uses it）**：通用 Agent（与框架无关的记忆层）、Code Agent（编码助手 / IDE / CLI）、个人助手 / 陪伴类、企业级 Agent 等。
2. **信息源（What goes in）**：对话消息、文档、工具调用轨迹、多模态数据等。
3. **索引检索方式（How to retrieve）**：向量相似度、关键词/BM25、知识图谱遍历、时间过滤、混合检索 + 重排。
4. **模态支持（输入信息源模态）**：写入记忆的原始数据是纯文本，还是含图像 / 音频 / 视频 / 文档 / 工具轨迹等。
5. **记忆建模（How to store）**：扁平事实、向量、知识图谱、分层（核心/归档）、时序图。
6. **端云支持（Where it runs）**：托管云服务、可自托管、纯本地/端侧（on-device / 零云依赖）。

---

## 1. 竞品逐一分析

### 1.1 Mem0

- 来源链接：
  - 官网：https://mem0.ai/
  - GitHub：https://github.com/mem0ai/mem0
  - 架构文档：https://github.com/mem0ai/mem0/blob/HEAD/skills/mem0/references/architecture.md
  - 第三方拆解：https://memo.d.foundation/breakdown/mem0

- 支持的 Agent 类型：**通用 Agent（与框架无关的记忆层）**，主打个人助手 / 聊天机器人，也用于客服等机构知识场景；提供各语言 SDK，可嵌入任意 Agent。

- 核心架构：
  - **信息源**：对话消息（user + assistant），结合「滚动摘要（异步刷新的全局会话摘要）+ 最近 10 条消息（近因）」作为上下文。
  - **记忆建模**：双形态。基础版 `Mem0` 把记忆存为自然语言「事实（facts）」放入向量库；图谱版 `Mem0g` 把记忆建模为有向标注图（实体为节点、关系为边，存入 Neo4j）。
  - **索引检索**：v3 检索流水线并行打分 —— 语义向量相似度 + BM25 关键词 + 实体匹配（entity graph boost）。Mem0g 还结合「实体中心图遍历 + 语义三元组匹配」。向量后端可插拔（Qdrant / Chroma / Milvus / pgvector / Redis）。
  - **写入流水线**：两阶段 —— 抽取（LLM 识别候选事实）→ 更新（对每条候选做向量相似检索取 Top-10，由 LLM 决定 ADD / UPDATE / DELETE / NOOP），实现去重与冲突消解。冲突时图谱版做「标记失效」而非删除。
  - **模态支持（输入信息源）**：以文本为主（从对话文本抽取事实）。
  - **端云支持**：开源（Apache 2.0），可自托管；同时提供托管 Platform（云）。

- 关键特性：分层记忆（短期 / 长期 / 多租户 scope）；CRUD 式记忆管理；用 GPT-4o-mini 这类小模型做抽取/更新（强调便宜可靠的工具调用）；多租户隔离（user/session/agent scope）。

- 优点：开源生态成熟（约 4.8 万 stars），向量后端可插拔；LOCOMO 基准 66.9%（优于 OpenAI memory 的 52.9%）；号称相比全量上下文节省 90%+ token、p95 延迟降低 91%。
- 缺点：图谱能力（Mem0g）在部分场景属 Pro/付费门槛；以文本事实为主，多模态弱；异步处理存在写入延迟（最新写入不一定立即可检索）。

---

### 1.2 Zep / Graphiti

- 来源链接：
  - 论文：https://arxiv.org/abs/2501.13956 （Zep: A Temporal Knowledge Graph Architecture for Agent Memory）
  - Graphiti GitHub：https://github.com/getzep/graphiti
  - Neo4j 博客：https://medium.com/neo4j/graphiti-knowledge-graph-memory-for-a-post-rag-agentic-world-0fd2366ba27d

- 支持的 Agent 类型：**企业级通用 Agent**，典型为客服、商业智能、决策类 Agent（需融合对话 + 业务数据、且数据随时间演化的场景）。

- 核心架构：
  - **信息源**：同时摄入非结构化对话消息 + 结构化业务数据。
  - **记忆建模**：以 Graphiti「时序感知动态知识图谱」为核心。图分三层子图：episode 子图（原始数据）、semantic entity 子图（抽取的实体/关系）、community 子图（高层领域摘要），模拟人类记忆的「情景 / 语义 / 社区」层次。
  - **双时间（bi-temporal）模型**：同时跟踪「事件发生时间 T」与「数据摄入时间 T'」，每条边带 (t_valid, t_invalid) 有效期。冲突时**标记失效而非删除**，支持时间点（point-in-time）回溯查询，可回答「上个月用户的偏好是什么」。
  - **索引检索**：混合检索 —— 语义向量 + BM25 关键词 + 图遍历，再做重排。增量更新，无需批量重算整图。底层存储 Neo4j。
  - **模态支持（输入信息源）**：以文本 + 结构化业务数据为主（消息文本 + 业务记录）。
  - **端云支持**：Zep 为商业 SaaS（云）；Graphiti 引擎开源，自托管主要走 Graphiti 路线。

- 关键特性：业界最强的「时序推理」能力；非破坏式（non-lossy）更新；实时增量摄入。
- 优点：DMR 基准 94.8%（优于 MemGPT 93.4%）；LongMemEval 上准确率最高提升 18.5%、延迟降低 90%；非常适合企业级（客服、商业智能、决策）随时间演化的数据。
- 缺点：图谱 + Neo4j 部署相对重；Zep 完整能力偏向托管云（开源主要是 Graphiti 引擎层）；以文本/结构化为主，弱多模态。

---

### 1.3 Letta（前身 MemGPT）

- 来源链接：
  - 官网：https://www.letta.com/
  - 文档（核心记忆）：https://docs.letta.com/guides/core-concepts/memory/memory-blocks/
  - 博客：https://www.letta.com/blog/memory-blocks
  - MemGPT 论文：https://arxiv.org/abs/2310.08560

- 支持的 Agent 类型：**有状态通用 Agent / 多智能体系统**，定位「构建带身份和长期状态的 Agent」（个人助手、长程任务 Agent、多 Agent 协作均可）。

- 核心架构（LLM-as-OS 范式）：
  - **分层记忆（操作系统式分页）**：
    - 核心记忆（Core Memory / Memory Blocks）：固定在上下文窗口内、始终可见、带 label/description/字符上限的结构化文本块（如 `human`、`persona`、自定义块），无需检索。
    - 外部/归档记忆（Archival / Recall）：超出上下文的长期存储（向量库 / 会话历史），通过 `conversation_search`、archival 检索召回。
  - **自编辑记忆（self-editing）**：Agent 通过内置工具自主增删改自己的记忆 —— `memory_insert` / `memory_replace` / `memory_rethink` / `memory_finish_edits` 等；可标记 read-only（如 persona 锁定）。
  - **信息源**：对话消息为主，外接 RAG / 文件 / 向量库。
  - **索引检索**：核心记忆「常驻上下文、零检索」；归档记忆走向量检索。
  - **共享记忆**：多个 Agent 可共享同一 memory block（一处更新、处处可见），适合多智能体协作。
  - **持久化**：全部 Agent 状态（消息历史、记忆、工具）默认持久化到数据库，跨会话保持「身份」。
  - **模态支持（输入信息源）**：以文本为主。
  - **端云支持**：开源（Apache 2.0，约 2.1 万 stars），可自托管；提供云平台 + ADE（Agent Development Environment）可视化编辑记忆块。

- 关键特性：自编辑记忆 + 心跳循环（heartbeat looping）+ inner thoughts；上下文自动编译（系统提示 + 记忆块 + 近期消息 + 旧消息摘要）。
- 优点：范式优雅（让模型自己管理「内存/磁盘」），无需手工 prompt 工程；记忆块可被开发者通过 API/ADE 直接编辑，可控性强；多智能体共享记忆。
- 缺点：核心记忆字符上限有限，强依赖 LLM 自身判断「写什么」（可能漏记/错记）；时序与图谱关系建模较弱；自编辑频繁调用 LLM 带来额外成本。

---

### 1.4 Cognee

- 来源链接：
  - 官网：https://www.cognee.ai/
  - GitHub：https://github.com/topoteretes/cognee
  - Redis 集成博客：https://redis.io/blog/build-faster-ai-memory-with-cognee-and-redis/

- 支持的 Agent 类型：**通用 Agent**，偏企业知识 / RAG 类应用（需要把文档/数据库构建为知识图谱再供 Agent 推理的场景）。

- 核心架构：
  - **ECL 流水线（Extract → Cognify → Load）**替代标准 RAG 的 ETL：
    - Extract：从 API / 数据库 / 文档（30+ 数据源）摄入原始内容。
    - Cognify：分块、生成 embedding、识别关键实体、映射关系。
    - Load：把向量表示 + 图关系同时写入后端。
  - **记忆建模**：知识图谱 + 向量双存储；核心抽象是 `DataPoint`（同时定义节点实体与关系边），无需僵硬 schema 即可动态扩展图谱。
  - **索引检索**：融合向量语义检索 + 图谱关系遍历。
  - **后端**：图谱可用 Neo4j / Kuzu / FalkorDB / NetworkX；向量可用 pgvector / Redis(RedisVL) / LanceDB 等，轻量适配器架构。
  - **模态支持（输入信息源）**：多模态友好，可摄入文本、音频、图像（30+ 数据源）。
  - **端云支持**：开源（open core，约 1.2 万 stars），可自托管；可用 LanceDB 等做本地实例。提供托管云。

- 关键特性：确定性记忆（Deterministic AI Memory）；模块化自定义 pipeline；6 行 Python 即可起步。
- 优点：图谱 + 向量融合，关系/跨历史「连点成线」强；后端适配灵活；多模态摄入；中小数据集上开发部署快。
- 缺点：大规模数据集下的性能与扩展性仍待验证；偏「机构知识（institutional）」而非个性化用户画像；图谱构建对抽取质量敏感。

---

### 1.5 LangMem / LangGraph Memory

- 来源链接：
  - LangMem 发布博客：https://www.langchain.com/blog/langmem-sdk-launch
  - LangChain Memory 概念文档：https://docs.langchain.com/oss/python/concepts/memory

- 支持的 Agent 类型：**通用 Agent（LangGraph/LangChain 生态内）**，常见于个人助手、邮件助手等；与 LangGraph 深度绑定。

- 核心架构：
  - **记忆分类（CoALA 范式）**：语义记忆（事实/知识）、情景记忆（过往交互/few-shot 示例）、程序记忆（规则/系统提示，可随反馈优化）。
  - **存储**：LangGraph Store（`InMemoryStore` / `PostgresStore`），记忆以 JSON 文档形式按 `namespace`（类似文件夹，常含 user/org id）+ `key` 组织，支持跨 namespace 内容过滤检索。
  - **写入时机**：两种机制 —— hot path（在线/对话中）与 background（后台）。
  - **信息源**：对话消息、用户偏好、系统提示。
  - **索引检索**：扁平 key-value + 向量语义检索（需配 embedding 函数）。
  - **模态支持（输入信息源）**：以文本为主。
  - **端云支持**：开源（MIT，约 1.3K stars），可自托管；与 LangGraph 深度绑定（存在生态锁定）；有托管服务。

- 关键特性：把「程序记忆」做成可被反馈优化的系统提示（prompt optimization）；与 LangGraph/LangChain 原生集成。
- 优点：记忆类型划分清晰、教学/工程友好；可用任意存储后端；与主流 Agent 框架无缝。
- 缺点：偏个性化、能力相对基础（扁平 + 向量），无时序图谱；情景记忆工具尚不完善；与 LangGraph 生态耦合较强。

---

### 1.6 OpenAI ChatGPT Memory

- 来源链接：
  - 官方说明：https://help.openai.com/en/articles/11146739-how-does-reference-saved-memories-work
  - Memory FAQ：https://help.openai.com/en/articles/8590148-memory-faq

- 支持的 Agent 类型：**个人助手（消费级聊天机器人）**，仅服务于 ChatGPT 自身产品，不可作为通用记忆层被外部 Agent 集成。

- 核心架构：
  - **两种模式**：
    - Saved memories（保存的记忆）：用户显式或模型自动保存的具体事实（姓名、偏好、饮食等），存于专用存储，注入每次对话，优先级最高，可在设置中查看/编辑/删除。
    - Reference chat history（引用聊天历史，2025-04 起）：从全部历史对话中提取兴趣/偏好的动态摘要注入系统提示，更像「动态画像」而非结构化事实表，会随时间变化。
  - **机制**：底层为 RAG —— 新对话开始时检索相关记忆并注入上下文。
  - **信息源**：用户与 ChatGPT 的对话。
  - **模态支持（输入信息源）**：以文本为主（产品级，从对话文本抽取）。
  - **端云支持**：纯托管云（封闭，不可自托管），属消费级产品而非可集成框架。

- 关键特性：消费级、零配置、自动管理（Plus/Pro 在记忆满时自动保留最相关项）；可一句话「忘记 X」。
- 优点：开箱即用、体验顺滑、规模化；显式记忆 + 隐式画像双轨。
- 缺点：黑盒、不可自托管、不可作为基础设施被集成；可控性/可解释性弱；数据归 OpenAI。仅作为「产品形态」基准参考。

---

### 1.7 Supermemory

- 来源链接：
  - 官网：https://supermemory.ai/
  - GitHub：https://github.com/supermemoryai/supermemory
  - 自托管文档：https://supermemory.ai/docs/deployment/self-hosting

- 支持的 Agent 类型：**个人助手 + 通用应用 Agent**，既有面向 C 端的 App / 浏览器扩展（给任意 AI 助手加记忆），也有面向开发者的统一 Context API。

- 核心架构：
  - **一体化 Context Stack**：记忆图 + 完整 RAG + 数据连接器 + 文件处理 + 用户画像，统一一个 API。
  - **记忆建模**：自动抽取记忆、构建并维护用户画像；事实分为 static（长期）/ dynamic（近期上下文）两类。
  - **索引检索**：内部自动处理 embedding、分块、事实抽取、冲突消解，无需单独配置向量库/分块策略。
  - **自动遗忘**：临时事实（如「明天有考试」）到期自动过期，矛盾自动消解，噪声不沉淀为永久记忆。
  - **基础设施**：Cloudflare Workers + PostgreSQL（pgvector）。
  - **信息源**：对话、文件、连接器数据。
  - **模态支持（输入信息源）**：文本为主，含文件/文档处理（连接器导入）。
  - **端云支持**：闭源（not open source）；自托管仅对企业版客户开放（需团队提供部署包）；主打托管云。配套 App / 浏览器扩展 / 插件 / MCP server，内置 Agent「Nova」。

- 关键特性：强调「Memory ≠ RAG」并同时跑两者；一站式上下文栈；MCP 集成。
- 优点：开发者几乎零配置即可拥有记忆 + RAG；自动画像/遗忘/冲突消解开箱即用；端到端产品化。
- 缺点：闭源、锁定强；自托管门槛高（仅企业版）；架构透明度低。

---

### 1.8 MemOS（MemTensor）

- 来源链接：
  - GitHub：https://github.com/MemTensor/MemOS
  - 论文：https://statics.memtensor.com.cn/files/MemOS_0707.pdf
  - 官网：https://memos.openmem.net/

- 支持的 Agent 类型：**通用 Agent / 自主 Agent**（深度对接 OpenClaw 等自主 Agent 生态），支持工具记忆与「技能记忆」用于 Code/任务型 Agent 的技能复用。

- 核心架构（「记忆操作系统」）：
  - **三层模块化架构**：Interface Layer（统一记忆 API）、Operation Layer（控制中枢：`MemOperator` 建标签/语义索引/图拓扑，`MemScheduler` 按任务意图调度记忆类型与调用顺序，`MemLifecycle` 管理记忆生命周期：创建/激活/过期/回收）、Infrastructure Layer。
  - **统一记忆 API**：把记忆建模为**可检视、可编辑的图**，而非黑盒 embedding 存储；支持 add/retrieve/edit/delete。
  - **异构记忆类型**：明文（Plaintext）、激活（activation）、参数（parameter）记忆三类协同（源自 Memory3 分层记忆模型）。
  - **多 Cube 知识库**：以可组合的「memory cube」管理多知识库，支持跨用户/项目/Agent 的隔离与受控共享、动态组合。
  - **索引检索**：本地插件用混合检索（FTS5 全文 + 向量）；标签系统 + 语义索引 + 图拓扑。
  - **模态支持（输入信息源）**：多模态，可摄入文本、图像/图表、工具调用轨迹（tool memory）、persona。
  - **端云支持**：开源（Apache 2.0，约 7K+ stars）；提供 **Cloud Plugin**（托管，号称 72% token 降低 + 多 Agent 共享）与 **Local Plugin**（100% 端侧、零云依赖、持久化 SQLite、Memory Viewer 看板）。深度对接 OpenClaw 生态。

- 关键特性：记忆反馈与纠正（自然语言修正/补充/替换记忆）；MemScheduler 毫秒级异步摄入（Redis Streams）；跨任务技能记忆复用与演化；号称 35.24% token 节省。
- 优点：架构最「系统化/工程化」，记忆可治理、可检视、可调度；端云双形态齐全（含纯本地）；多模态 + 工具记忆 + 技能演化；国产、对中文/国产生态友好。
- 缺点：体系庞大、概念多、上手与运维成本相对高；部分高级特性较新、稳定性仍在迭代。

---

### 1.9 memU（NevaMind-AI）

- 来源链接：
  - GitHub：https://github.com/NevaMind-AI/memU
  - PyPI：`memu-py`
  - 分析文章：https://a-bots.com/blog/memu-2026

- 支持的 Agent 类型：**个人助手 / 陪伴类 / 主动式 Agent**，主打 24/7 always-on 的个人化代理（兼容 Claude/GPT/Gemini/DeepSeek/Qwen 等多模型）。

- 核心架构：
  - **文件系统式三层分层记忆**：
    - Resource Layer（资源层）：原始数据摄入（对话、文档、图像、视频）。
    - Memory Item Layer（记忆项层）：抽取的事实/偏好/知识单元，自动归类并以 **Markdown 文件**形式存储（透明、可读）。
    - Memory Category Layer（类别层）：类似「文件夹」的高层组织结构，分组关联记忆项以便高效检索。
    - （在研）Intention Layer（意图层）：基于累积模式**预测用户需求、主动行动**。
  - 类比：类别=文件夹、记忆项=文件、交叉引用=符号链接（symlink）。
  - **索引检索**：双模检索 —— RAG-based + LLM-based。
  - **写入**：Agent 自主决定记什么、如何归类、何时更新/遗忘（proactive/always-on）。
  - **模态支持（输入信息源）**：多模态，可摄入对话、文档、图像、视频。
  - **端云支持**：开源（约 1.3 万 stars），自托管 / local-first；支持 MCP；兼容 Claude / GPT / Gemini / DeepSeek / Qwen / Grok / OpenRouter。

- 关键特性：主动式（proactive）24/7 记忆演化；透明 Markdown 存储（高可读/可审计）；高密度结构化上下文降低 token 成本。
- 优点：LOCOMO 基准 92.09%（很高）；存储透明可读；主动/意图层方向前瞻；多模型兼容、MCP 友好。
- 缺点：相对年轻（2026-02 才上 PyPI），生态/稳定性待沉淀；意图层尚未落地；许可证为 NOASSERTION（需注意合规）。

---

### 1.10 AgentScope Memory + ReMe（阿里巴巴）

- 来源链接：
  - AgentScope 记忆文档：https://doc.agentscope.io/zh_CN/tutorial/task_memory.html
  - ReMe GitHub：https://github.com/agentscope-ai/ReMe
  - 技术解读：https://www.cnblogs.com/alisystemsoftware/p/19417127

- 支持的 Agent 类型：**通用 Agent（AgentScope 框架内的 ReActAgent）**，框架级方案，需在 AgentScope 体系内使用。

- 核心架构：
  - **两层记忆**：
    - 短期记忆（`MemoryBase`）：当前会话上下文，提供原子级消息存储/管理；后端可选 `InMemoryMemory` / `AsyncSQLAlchemyMemory`（SQLite/PostgreSQL/MySQL）/ `RedisMemory` / `TablestoreMemory`（阿里云表格存储，支持分布式 + 多用户/会话隔离）。
    - 长期记忆（`LongTermMemoryBase`）：跨会话持久化，依赖外部组件（`Mem0LongTermMemory` 或 `ReMe`）。
  - **AutoContextMemory**：对话历史超阈值时自动应用 6 种渐进式压缩策略（轻→重）压缩上下文同时保留要点；支持把大内容卸载到外部存储（UUID 寻址）。
  - **ReMe 长期记忆栈**：LLM（语义抽取/决策/生成）+ Embedder + VectorStore（向量检索）+ GraphStore（实体-关系知识图谱）+ Reranker（重排）+ SQLite（操作审计日志、版本回溯）。支持个人/任务/工具三种记忆类型。
  - **工作模式**：`agent_control`（Agent 自主调用 `record_to_memory`/`retrieve_from_memory`）/ `static_control`（开发者控制）/ `both`。
  - **标记（marks）**：给记忆消息打标签用于分类/过滤/检索（如 `hint` 标签用完即删）。
  - **模态支持（输入信息源）**：以文本为主。
  - **端云支持**：开源框架，可自托管；可对接阿里云表格存储等云服务；国产生态。

- 关键特性：记忆作为可序列化状态（`StateModule`）结合 `SessionManager` 持久化；完整可追溯（SQLite 审计 + 版本回溯）；图谱 + 向量 + 重排齐备。
- 优点：与 AgentScope Agent 框架深度集成、工程化完善；国产、对接阿里云生态；记忆可审计可回溯；短/长期记忆边界清晰。
- 缺点：长期记忆能力主要依赖外接组件（Mem0/ReMe），强绑定 AgentScope 框架；多模态偏弱。

---

### 1.11 OpenViking（字节跳动 / 火山引擎 Volcengine）

- 来源链接：
  - GitHub：https://github.com/volcengine/OpenViking
  - 官网文档：https://www.openviking.ai/docs
  - 介绍文章：https://www.marktechpost.com/2026/03/15/meet-openviking-an-open-source-context-database-that-brings-filesystem-based-memory-and-retrieval-to-ai-agent-systems-like-openclaw/
  - Red Hat 部署：https://developers.redhat.com/articles/2026/04/23/deploy-openviking-openshift-ai-improve-ai-agent-memory

- 支持的 Agent 类型：**通用 / 自主 Agent**，面向 OpenClaw 等自主 Agent，统一管理「记忆 + 资源 + 技能」，对长程任务型与 Code Agent 同样适用（约 23K stars）。

- 核心架构（「Agent 上下文文件系统」）：
  - **文件系统范式（viking:// 协议）**：摒弃传统扁平向量存储，把记忆（Memory）、资源（Resource，文档/代码/FAQ）、技能（Skill，工具/MCP）统一组织为分层虚拟文件系统，每个条目对应唯一 URI（`viking://{scope}/{path}`）。Agent 可用 `ls`/`find` 等确定性路径操作浏览，而非只靠相似度检索。
  - **三类上下文**：Resource（知识/规则，长期静态）、Memory（用户偏好/习得经验，长期动态）、Skill（可调用能力，长期静态）。Memory 又细分 user 域（profile/preferences/entities/events）与 agent 域（cases/patterns）。
  - **目录递归检索**：先用向量相似度定位目录，再在目录内二次检索、递归下钻；每步遍历都记录为**可见的检索轨迹（trajectory）**，便于调试（非黑盒）。
  - **L0/L1/L2 分层加载**：先读摘要（`.abstract.md`/`.overview.md`），需要时再加载全文，显著省 token。
  - **记忆自迭代**：每次会话结束可触发记忆抽取，异步分析任务执行结果与用户反馈，自动更新 user/agent 记忆目录。
  - **信息源**：对话消息、工具调用、任务执行历史、文档/代码资源。
  - **模态支持（输入信息源）**：以文本/文档/代码为主（资源 + 对话 + 工具轨迹）。
  - **端云支持**：开源（Python），可自托管（提供 REST API，支持 OpenShift AI / 自托管 embedding 部署）。

- 关键特性：文件系统式可浏览上下文；检索轨迹可观测；分层加载省 token；记忆 + 资源 + 技能三位一体。
- 优点：确定性路径访问 + 可观测检索，调试友好；统一管理记忆/资源/技能，解决上下文碎片化；大厂（字节火山引擎）背书、生态活跃。
- 缺点：文件系统范式较新、心智模型与传统向量库不同，迁移有学习成本；多模态（图像/音视频）支持相对弱；偏工程基础设施。

---

### 1.12 Zilliz memSearch

- 来源链接：
  - GitHub：https://github.com/zilliztech/memsearch
  - Milvus 博客：https://milvus.io/blog/we-extracted-openclaws-memory-system-and-opensourced-it-memsearch.md
  - 发布新闻：https://www.prnewswire.com/news-releases/zilliz-open-sources-memsearch-giving-ai-agents-persistent-human-readable-memory-302711968.html

- 支持的 Agent 类型：**Code Agent 为主**（官方定位「面向 AI 编码 Agent 的跨平台语义记忆」，内置 Claude Code / Codex CLI / OpenCode / OpenClaw 插件），同时也可作为任意 Agent 的通用记忆层。

- 核心架构：
  - **Markdown 为唯一真源（source of truth）**：所有记忆即 `.md` 文件，人类可读、可编辑、可纳入 git 版本管理；脱胎于 OpenClaw 的记忆子系统。
  - **Milvus 作为「影子索引（shadow index）」**：向量索引只是从 Markdown 派生的可重建缓存——删掉索引不丢数据，几分钟即可重新 embed + 重建检索层。
  - **混合检索 + 渐进式召回**：三层召回（search → expand → transcript）；稠密向量 + BM25 稀疏 + RRF 重排；SHA-256 内容哈希跳过未变更内容；文件监听器实时自动索引（live sync）；智能去重。
  - **信息源**：对话 / 编码会话产生的 Markdown 记忆文件。
  - **模态支持（输入信息源）**：纯文本（Markdown）。
  - **端云支持**：开源（MIT）；后端三种 Milvus 部署可一键切换——**Milvus Lite**（零配置单文件、本地）/ 自托管 Milvus（Docker、团队多用户共享）/ **Zilliz Cloud**（全托管、自动扩缩）。

- 关键特性：Markdown 透明可审计；索引可重建（数据与索引解耦）；Milvus 强检索底座；即插即用插件（Claude Code/Codex/OpenCode）。
- 优点：记忆完全透明、可版本控制、零厂商锁定（数据是你的 .md 文件）；本地到云平滑过渡；背靠 Milvus 检索性能强。
- 缺点：纯文本、无图谱/时序建模；偏「检索 + 存储」基础设施，事实抽取/冲突消解等高级记忆逻辑相对轻；项目较新（2026-02 创建）。

---

### 1.13 Claude Code 记忆（Anthropic）

- 来源链接：
  - 官方文档：https://code.claude.com/docs/en/memory
  - Auto Memory 指南：https://zenn.dev/zenchaine/articles/claude-code-auto-memory-guide

- 支持的 Agent 类型：**Code Agent（编码 CLI/IDE 助手）**，记忆与「项目 / 个人 / 组织」编码工作流绑定。

- 核心架构（两套互补机制，每次会话开始都加载）：
  - **CLAUDE.md（人写的静态指令）**：开发者手写的 Markdown，给 Claude 持久化的项目/个人/组织级指令。读取时从当前目录向上「沿目录树游走」，逐级加载 `CLAUDE.md` 与 `CLAUDE.local.md`（项目 `./CLAUDE.md`、用户 `~/.claude/CLAUDE.md`、本地 `./CLAUDE.local.md`）。本质是**配置/上下文**而非强约束。
  - **Auto Memory（Claude 自写的动态笔记，v2.1.32 引入）**：Claude 根据用户纠正与偏好自动记录项目模式、调试经验等，存于 `~/.claude/projects/<project>/memory/`。两层设计：`MEMORY.md` 作为精简索引（会话开始加载前 200 行 / 25KB），细节拆到各主题文件（如 `debugging.md`），仅在语义相关时按需加载。
  - **管理**：通过 `/memory` 命令开关 Auto Memory、查看/编辑已加载文件；可用 `CLAUDE_CODE_DISABLE_AUTO_MEMORY` 关闭。
  - **信息源**：项目代码库、用户指令、会话中的纠正/偏好/调试洞见。
  - **模态支持（输入信息源）**：纯文本（Markdown + 代码）。
  - **端云支持**：随 Claude Code 客户端本地落盘（Markdown 文件在本机/仓库），模型推理走 Anthropic 云；记忆文件本身可纳入 git。

- 关键特性：手写指令（CLAUDE.md）+ 自写笔记（Auto Memory）双轨；索引/主题文件分层省 token；原生内置、零额外基础设施。
- 优点：开箱即用、对编码场景贴合；记忆透明可编辑、可随仓库共享；分层加载控制上下文开销。
- 缺点：仅限 Claude Code 生态、跨工具不通用；本质是「文件 + 注入」，无语义检索 / 图谱 / 时序（CLAUDE.md 大了会整段吃 token）；团队协作的归属/时效/结构化弱（社区常用 MCP 记忆补足）。

---

### 1.14 Cursor 记忆（Cursor IDE）

- 来源链接：
  - Memories 介绍（第三方）：https://memnexus.ai/blog/2026-02-20-cursor-persistent-memory
  - Mem0 Cursor 集成：https://docs.mem0.ai/integrations/cursor
  - 社区方案 cursor-mem：https://github.com/liuhao6741/cursor-mem

- 支持的 Agent 类型：**Code Agent（IDE 编码助手）**，记忆按项目/个人维度服务于编码对话。

- 核心架构（原生 + 扩展两条路径）：
  - **原生 Memories（v1.0 起）**：由后台模型在对话中**自动发现并提议**有用事实，用户**逐条审批**后保存；按「项目 + 个人」维度存储，可在 `Settings → Rules → Generate Memories` 开关与管理（查看/编辑/删除）。
  - **Rules（规则）**：`.cursor/rules/*.md`（或 `.mdc`）或项目根 `AGENTS.md`，是开发者手工维护的静态文本，载入该项目每次对话（旧的单文件 `.cursorrules` 已弃用）。
  - **MCP 扩展记忆（跨会话/跨工具/跨 Agent）**：通过 `.cursor/mcp.json` 接入 MCP 记忆服务（如 Mem0、MemNexus、cursor-mem 等），借生命周期 hook（`sessionStart` / `beforeSubmitPrompt` / `preCompact` 等）自动捕获与召回，提供语义检索/全文检索的动态知识库。
  - **信息源**：编码对话、用户纠正/偏好、（扩展方案下）编辑记录 / shell 命令 / MCP 调用轨迹。
  - **模态支持（输入信息源）**：纯文本（代码 + 对话 + 规则）。
  - **端云支持**：原生 Memories 随 Cursor 账户（云端按项目/个人存储，需审批）；Rules 为本地仓库文件；MCP 方案可本地（如 cursor-mem 的本地 SQLite）或云（Mem0 Cloud）。

- 关键特性：原生「自动提议 + 人工审批」的记忆；规则文件静态约束；MCP 生态可插拔增强；与 codebase 索引/Tab 补全协同。
- 优点：编码场景体验顺滑、审批机制可控；原生 + 规则 + MCP 三层灵活；可借 Mem0 等获得跨工具持久记忆。
- 缺点：原生 Memories 仅存简短偏好、能力有限（深度知识需 MCP 外挂）；项目级隔离、跨工具默认不互通；以文本为主，无图谱/时序。

---

### 1.15 OpenAI Codex 记忆（Codex CLI / App）

- 来源链接：
  - Codex 记忆机制解析：https://mem0.ai/blog/how-memory-works-in-codex-cli
  - AGENTS.md 指南：https://www.augmentcode.com/guides/how-to-build-agents-md
  - 上下文管理指南：https://iceberglakehouse.com/posts/2026-03-context-openai-codex/
  - MCP 记忆方案（Hindsight）：https://hindsight.vectorize.io/blog/2026/04/08/adding-memory-to-codex-with-hindsight

- 支持的 Agent 类型：**Code Agent（编码 CLI / App / 云端）**，记忆与「仓库 / 个人 / 团队」编码工作流绑定。

- 核心架构（静态指令层 + 生成记忆层 + MCP 外挂）：
  - **AGENTS.md（静态指令层）**：人写的 Markdown，每次会话开始读取。分层发现机制——全局 `~/.codex/AGENTS.md`（`AGENTS.override.md` 优先）+ 从 git 仓库根向当前工作目录「下行游走」，沿途所有 `AGENTS.md` 按路径顺序拼接（monorepo 可多级叠加）。AGENTS.md 是 Codex/Cursor/Aider/Jules 等跨工具收敛的开放约定（2025-12 捐给 Linux 基金会旗下 Agentic AI Foundation）。
  - **Memories（生成记忆层）**：Codex 在后台自动总结历史会话，把摘要写入 `~/.codex/memories/`，后续会话自动读取——**无需用户手动把事实抄进 Markdown，由 Agent 自己写**；在 `~/.codex/config.toml` 配置。这正是补上 AGENTS.md「静态、无时间概念」短板的一层。
  - **MCP 外挂记忆**：Codex 原生支持 MCP server（`~/.codex/config.toml` 或 `codex mcp` 配置，会话开始自动拉起），可接入 Hindsight / MemNexus / Mem0 / EchoVault 等，借生命周期 hook（prompt 前 auto-recall 注入 `additionalContext`、会话结束 `Stop` 时 auto-retain 抽取事实）实现跨会话语义记忆。
  - **云端 Codex（App）**：官方称记忆可跨云端会话持久化，但留存策略/配置面公开信息少于 CLI（underspecified）。
  - **信息源**：仓库代码、用户指令（AGENTS.md）、历史会话转录（决策/模式/Bug 等）。
  - **模态支持（输入信息源）**：纯文本（Markdown + 代码 + 会话转录）。
  - **端云支持**：CLI 记忆本地落盘（`~/.codex/` 下的 Markdown 文件，可纳入仓库），模型推理走 OpenAI 云；App 提供跨会话云端项目记忆。

- 关键特性：AGENTS.md 跨工具标准 + 分层叠加；自动生成的会话摘要记忆（agent 自写）；原生 MCP 可插拔外部记忆；与 Skills / 多 worktree 编排协同。
- 优点：AGENTS.md 是事实标准、生态广（多工具通用、可 git 共享）；生成记忆层减轻手工维护；MCP 开放、可换任意记忆后端。
- 缺点：内置 Memories 仍偏「摘要式」、无语义检索/图谱/时序，深度持久记忆基本要靠 MCP 外挂；云端记忆细节不透明；以文本为主。

---

## 2. 分类汇总

### 2.0 按「支持的 Agent 类型」分类

| Agent 类型 | 代表产品 | 说明 |
| --- | --- | --- |
| 通用 Agent（框架无关记忆层） | **Mem0**、Cognee、Supermemory、**OpenViking**、memSearch | 提供 SDK/API，可嵌入任意 Agent |
| 通用 Agent（绑定特定框架） | **LangMem**（LangGraph）、**AgentScope+ReMe** | 强依赖宿主框架 |
| 有状态 / 自主 Agent | **Letta/MemGPT**、**MemOS**、OpenViking | 长程任务、多 Agent、技能复用 |
| 企业级 Agent | **Zep / Graphiti**、Cognee | 客服 / BI / 决策 |
| 个人助手 / 陪伴类 | **ChatGPT Memory**、**memU**、Supermemory | C 端个性化 |
| **Code Agent（编码助手）** | **memSearch**、**Claude Code**、**Cursor**、**Codex**、（OpenViking 亦适用） | IDE / CLI 编码场景 |

### 2.1 按「记忆建模 / 存储范式」分类

| 范式 | 代表产品 | 特点 |
| --- | --- | --- |
| 纯向量 / 扁平事实 | Mem0（基础版）、LangMem、ChatGPT Memory | 抽取事实→向量检索，简单高效，弱关系/时序 |
| 向量 + 知识图谱 | Mem0g、Cognee、ReMe | 实体关系建模，支持多跳推理 |
| 时序知识图谱（Temporal KG） | **Zep / Graphiti** | 双时间模型、有效期、时间点回溯，时序最强 |
| 分层记忆（OS 式） | **Letta/MemGPT**、**MemOS**、AgentScope | 核心(常驻)/归档(检索) 分层，模拟内存/磁盘 |
| 分层文件系统式 | **memU**、**OpenViking** | 类别=文件夹、项=文件 / viking:// 虚拟文件系统 |
| Markdown 文件 + 向量影子索引 | **memSearch**、Claude Code、Cursor(部分) | Markdown 为真源，向量索引可重建 |
| Markdown 指令文件 + 自动摘要 | **Codex**（AGENTS.md + Memories） | 静态指令 + agent 自写会话摘要 |
| 一体化 Context Stack | **Supermemory** | 记忆 + RAG + 画像 + 连接器一站式 |

### 2.2 按「检索方式」分类

| 检索方式 | 代表产品 |
| --- | --- |
| 语义向量为主 | LangMem、ChatGPT Memory、Letta（归档层） |
| 混合检索（向量 + BM25 + 实体/图/重排） | **Mem0(v3)**、**Zep**、Cognee、MemOS、ReMe、**memSearch**（向量+BM25+RRF） |
| 图遍历 + 语义三元组 | Mem0g、Zep、Cognee |
| 文件系统路径浏览 + 递归检索 | **OpenViking**（ls/find + 目录递归） |
| 时间过滤 / 时间点查询 | **Zep**（独有优势） |
| 常驻上下文（零检索）+ 检索 双轨 | **Letta**、ChatGPT（saved memories 注入） |
| 文件注入（会话开始全量/索引加载） | **Claude Code**（CLAUDE.md/MEMORY.md）、**Cursor**（Rules/Memories）、**Codex**（AGENTS.md/Memories） |

### 2.3 按「端云支持」分类

| 部署形态 | 代表产品 |
| --- | --- |
| 纯托管云（不可自托管） | **ChatGPT Memory**（封闭消费级） |
| 客户端本地落盘 + 云端推理 | **Claude Code**（记忆文件本地/仓库）、**Cursor**（原生 Memories 云端、Rules 本地）、**Codex**（CLI 本地、App 云端） |
| 托管云为主 + 引擎/部分开源 | Zep（Graphiti 开源）、Supermemory（自托管仅企业版） |
| 开源 + 可自托管 + 托管云 | **Mem0**、Cognee、Letta、LangMem、**OpenViking** |
| 开源 + 端云双形态（含纯本地/零云依赖） | **MemOS**（Local/Cloud Plugin）、**memU**（local-first）、**memSearch**（Milvus Lite→Zilliz Cloud） |
| 端侧 / 本地优先（隐私/合规） | **MemOS Local Plugin**、SuperLocalMemory、memU、memSearch(Lite) |

### 2.4 按「定位 / 适用场景」分类

| 定位 | 代表产品 | 适用场景 |
| --- | --- | --- |
| 个性化用户记忆（Personalization） | Mem0、LangMem、Supermemory、ChatGPT | 个人助理、聊天机器人、用户画像 |
| 企业级 / 机构知识（Institutional） | Zep、Cognee | 客服、商业智能、跨会话/跨文档知识 |
| 有状态 Agent / 多智能体 | Letta、MemOS、AgentScope、OpenViking | 长程任务、多 Agent 协作、技能复用 |
| **编码助手记忆（Code Agent）** | memSearch、Claude Code、Cursor、Codex | IDE/CLI 编码、项目上下文、调试经验沉淀 |
| 时序敏感 | Zep / Graphiti | 偏好随时间变化、历史回溯、审计 |
| 主动式 / 端侧隐私 | memU、MemOS Local、SuperLocalMemory | 24/7 主动代理、本地隐私优先 |

### 2.5 综合速览表

| 产品 | 支持的 Agent 类型 | 存储范式 | 检索方式 | 输入模态 | 端云 | 开源/许可 | 基准亮点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Mem0 | 通用/个人助手 | 向量(+图Mem0g) | 向量+BM25+实体 | 文本 | 自托管+云 | Apache 2.0 | LOCOMO 66.9% |
| Zep/Graphiti | 企业级通用 | 时序知识图谱 | 向量+BM25+图+时间 | 文本+结构化 | 云+引擎开源 | Graphiti 开源 | DMR 94.8%、LongMemEval +18.5% |
| Letta/MemGPT | 有状态/多智能体 | OS 分层+自编辑 | 常驻+向量 | 文本 | 自托管+云 | Apache 2.0 | DMR 93.4% |
| Cognee | 通用/企业知识 | 图谱+向量(ECL) | 向量+图遍历 | 文本/音频/图像 | 自托管+云 | Open core | 中小数据集表现好 |
| LangMem | 通用(LangGraph) | 扁平KV+向量 | 向量 | 文本 | 自托管 | MIT | 记忆类型清晰 |
| ChatGPT Memory | 个人助手 | 显式事实+画像 | RAG 注入 | 文本 | 纯云(闭源) | 闭源 | 消费级标杆 |
| Supermemory | 个人助手/通用 | Context Stack | 自动混合 | 文本+文件 | 云(企业自托管) | 闭源 | 一站式 |
| MemOS | 通用/自主 | OS 三层+异构记忆 | FTS5+向量+图 | 文本/图像/工具 | 端+云齐全 | Apache 2.0 | token 节省 35% |
| memU | 个人助手/陪伴 | 文件系统分层 | RAG+LLM 双模 | 文本/文档/图像/视频 | local-first | NOASSERTION | LOCOMO 92.09% |
| AgentScope+ReMe | 通用(框架内) | 短/长两层+图 | 向量+图+重排 | 文本 | 自托管+阿里云 | 开源 | 可审计/回溯 |
| OpenViking | 通用/自主/Code | viking:// 文件系统 | 路径浏览+递归检索 | 文本/文档/代码/工具 | 自托管(REST) | 开源 | 检索轨迹可观测 |
| memSearch | **Code Agent**/通用 | Markdown+Milvus影子索引 | 向量+BM25+RRF | 文本(Markdown) | 本地→Zilliz Cloud | MIT | OpenClaw 同款架构 |
| Claude Code | **Code Agent** | Markdown 文件(双轨) | 文件注入+按需加载 | 文本/代码 | 本地文件+云推理 | 闭源(客户端) | 原生编码记忆 |
| Cursor | **Code Agent** | 原生Memories+Rules+MCP | 自动提议+文件注入 | 文本/代码 | 云端Memories+本地Rules | 闭源(客户端) | 审批式记忆 |
| Codex | **Code Agent** | AGENTS.md+自动摘要+MCP | 文件注入+MCP召回 | 文本/代码 | CLI本地+App云端 | 闭源(客户端) | AGENTS.md 跨工具标准 |

---

## 3. 对 agent-memory 的启示（讨论用，非结论）

> 以下为基于调研的观察，供后续设计探讨，未做任何取舍决策。

1. **检索方式趋同于「混合检索 + 重排」**：纯向量已不够，主流头部（Mem0 v3、Zep、MemOS、ReMe）都走「向量 + 关键词 + 图/实体 + 重排」。agent-memory 若想对标头部，混合检索应是基线。
2. **时序能力是高价值差异化点**：Zep 的双时间模型 + 有效期 + 非破坏式更新，是当前时序推理与「点查历史」的标杆，但部署偏重。是否引入「有效期 + 失效标记」值得评估。
3. **端云一体（尤其端侧/零云依赖）是差异化机会**：MemOS、memU、SuperLocalMemory 都在押注本地优先。若 agent-memory 面向隐私/合规或端侧场景，端云双形态是清晰卖点。
4. **记忆可治理/可检视 vs 黑盒**：MemOS、memU（Markdown）、ReMe（审计日志）都强调透明可编辑可回溯。可控性正成为企业级刚需。
5. **冲突消解与遗忘**几乎是标配：抽取→相似检索→LLM 决策 ADD/UPDATE/DELETE/NOOP（Mem0 范式），加自动过期/遗忘（Supermemory），是写入流水线的成熟模板。
6. **多模态 + 工具记忆（tool memory）/技能记忆**是新前沿：MemOS、Cognee、memU 已在做。若目标是「Agent」而非纯聊天，工具调用轨迹与技能复用值得纳入。
7. **Agent 类型决定记忆形态**：Code Agent（memSearch / Claude Code / Cursor）普遍走「Markdown 文件 + 可选向量索引 + 仓库内共享 + 人工审批」，强调透明、可 git 化、跨工具复用；个人助手类更重「事实抽取 + 画像 + 自动遗忘」；企业级更重「图谱 + 时序 + 审计」。agent-memory 需先明确主打哪类 Agent，再决定记忆形态——若覆盖 Code Agent，文件友好 + 仓库共享 + MCP 接入几乎是入场券。
8. **文件系统范式正在兴起**：OpenViking（viking://）、memU、memSearch、Claude Code 都把「记忆」落到可浏览/可编辑的文件结构上，相比黑盒向量库更透明可观测，是当前一条清晰的产品化路线。

---

## 附：信息源链接清单

- Mem0：https://mem0.ai/ ｜ https://github.com/mem0ai/mem0
- Zep / Graphiti：https://arxiv.org/abs/2501.13956 ｜ https://github.com/getzep/graphiti
- Letta / MemGPT：https://www.letta.com/ ｜ https://docs.letta.com/ ｜ https://arxiv.org/abs/2310.08560
- Cognee：https://www.cognee.ai/ ｜ https://github.com/topoteretes/cognee
- LangMem / LangGraph：https://www.langchain.com/blog/langmem-sdk-launch ｜ https://docs.langchain.com/oss/python/concepts/memory
- OpenAI ChatGPT Memory：https://help.openai.com/en/articles/8590148-memory-faq
- Supermemory：https://supermemory.ai/ ｜ https://github.com/supermemoryai/supermemory
- MemOS：https://github.com/MemTensor/MemOS ｜ https://statics.memtensor.com.cn/files/MemOS_0707.pdf
- memU：https://github.com/NevaMind-AI/memU
- AgentScope / ReMe：https://doc.agentscope.io/zh_CN/tutorial/task_memory.html ｜ https://github.com/agentscope-ai/ReMe
- OpenViking（火山引擎）：https://github.com/volcengine/OpenViking ｜ https://www.openviking.ai/docs
- Zilliz memSearch：https://github.com/zilliztech/memsearch ｜ https://milvus.io/blog/we-extracted-openclaws-memory-system-and-opensourced-it-memsearch.md
- Claude Code 记忆：https://code.claude.com/docs/en/memory
- Cursor 记忆：https://memnexus.ai/blog/2026-02-20-cursor-persistent-memory ｜ https://docs.mem0.ai/integrations/cursor
- OpenAI Codex 记忆：https://mem0.ai/blog/how-memory-works-in-codex-cli ｜ https://www.augmentcode.com/guides/how-to-build-agents-md
- 综合对比参考：https://vectorize.io/articles/best-ai-agent-memory-systems ｜ https://atlan.com/know/zep-vs-mem0/
