# Agent 记忆 Benchmark 调研（Memory Benchmarks Survey）

> 调研对象：业界用于评测「Agent 长期记忆 / Memory Layer」能力的主流公开 Benchmark
> 用途：为 `agent-memory` 记忆系统的能力评测、效果对标与差异化定位提供参考
> 调研时间：2026-05
> 维度：来源链接、Benchmark 介绍说明、数据量、适用领域（评测能力）

---

## 0. 阅读指南

「Agent 记忆」评测的核心，是衡量系统能否在**长时间、多会话、信息不断累积/变更**的交互中，正确地存储、更新、检索并应用历史信息。本调研按以下维度梳理各 Benchmark：

- **数据形态**：多会话对话、用户—助手聊天、多源数字痕迹（App / 健康记录）、TV 剧本多人对话等。
- **核心评测能力**：单跳检索、多跳推理、时序推理、知识更新（冲突消解）、偏好/人格追踪、长程理解、拒答（abstention）、测试时学习等。
- **数据规模**：会话数 / 问题数 / 上下文 token 量级。
- **难度趋势**：从「短上下文检索」演进到「百万—千万 token、跨会话身份与状态一致性」。

> 关键背景：随着 LLM 上下文窗口扩展到 1M+ token，早期偏「检索」的 Benchmark（LoCoMo / LongMemEval）在「全量塞入上下文」的朴素方案下已能取得有竞争力的分数，因此评测重心正向**更大规模（BEAM 10M）**、**多源/非陈述性记忆（LifeBench）**、**隐式偏好（PersonaMem）**、**冲突消解与状态一致性**等更难的维度迁移。

---

## 1. 主流 Benchmark 逐一分析

### 1.1 LoCoMo（Long Conversational Memory）

- 来源链接：
  - 论文：https://arxiv.org/abs/2402.17753 （Evaluating Very Long-Term Conversational Memory of LLM Agents, ACL 2024）
  - 项目主页：https://snap-research.github.io/locomo/
  - GitHub / 数据：https://github.com/snap-research/LoCoMo （`data/locomo10.json`）
  - Leaderboard：**无官方榜单**。可参考第三方聚合榜：① 统一榜 AMB https://agentmemorybenchmark.ai/ （含 locomo 列）；② mem0 公开研究页 https://mem0.ai/research ；③ Papers With Code 聚合 https://www.wizwand.com/dataset/longmemeval （含 LoCoMo 相关任务）；④ 社区跑分多以「GitHub Issue 提交」形式公开，见 https://github.com/snap-research/locomo/issues （如 #31/#34 等 SOTA 提交）。

- 介绍说明：由 Snap Research 提出，通过「LLM Agent + 人物 persona + 时序事件图」的人机协作流水线生成**超长期多会话对话**，并经人工校验长程一致性。每段对话由两个虚拟人物跨多个会话进行，包含**图片分享与图片反应**行为（具备多模态属性）。评测框架包含三类任务：**问答（QA）、事件摘要（event summarization）、多模态对话生成**。问题分为 5 类推理：单跳（single-hop）、多跳（multi-hop）、时序（temporal）、常识/世界知识（open-domain）、对抗（adversarial）。

- 数据量：公开版 `locomo10.json` 含 **10 段**高质量超长对话（初版 arxiv 为 50 段，后裁剪为最长的 10 段以降低闭源模型评测成本）；平均每段约 **600 turns / 16K tokens**，最多 **32 个会话**；QA 任务约 **1,540 道**非对抗问题（业界常用此 1,540 题口径）。
  - 注：社区审计（2026）发现 1,540 题中约 **6.4%（99 题）** 存在标注/答案错误，引用分数时建议参考其「调整后上限」。

- 适用领域：个人助手 / 陪伴类、开放域多会话对话记忆；是业界最早、被引用最广的「长期对话记忆」基准，常用于对标 Mem0、Zep、OpenAI Memory 等记忆层产品。

---

### 1.2 LongMemEval

- 来源链接：
  - 论文：https://arxiv.org/abs/2410.10813 （LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory, ICLR 2025）
  - GitHub / 数据：https://github.com/xiaowu0162/LongMemEval
  - ICLR Poster：https://iclr.cc/virtual/2025/poster/28290
  - Leaderboard：**无官方榜单**。可参考：① 统一榜 AMB https://agentmemorybenchmark.ai/ （含 longmemeval 列）；② mem0 公开研究页 https://mem0.ai/research ；③ Papers With Code 风格聚合 https://www.wizwand.com/dataset/longmemeval ；④ 厂商/社区 SOTA 提交多以 GitHub Issue 公开，见 https://github.com/xiaowu0162/LongMemEval/issues 。

- 介绍说明：聚焦**聊天助手的长期交互记忆**。通过「属性可控」的 needle-in-a-haystack 风格流水线，把含证据的对话嵌入到可自由扩展、带时间戳的聊天历史中。评测 **5 项核心记忆能力**：信息抽取（information extraction）、多会话推理（multi-session reasoning）、时序推理（temporal reasoning）、知识更新（knowledge updates）、拒答（abstention）。题目细分为 7 种类型（single-session-user / single-session-assistant / single-session-preference / temporal-reasoning / knowledge-update / multi-session，以及 `_abs` 拒答变体）。主流以 GPT-4o 作为 LLM-as-judge。

- 数据量：**500 道**人工精编问题；提供两档标准设定：
  - **LongMemEval-S**：约 **115K tokens**（约 40 个历史会话）。
  - **LongMemEval-M**：约 **500 个会话**（约 **1.5M tokens**）。
  - 另含 `longmemeval_oracle.json`（仅保留证据会话的 oracle 检索设定）。
  - 问题源自 164 个用户属性本体（生活方式、所属物、人生事件、情境、人口统计 5 大类）。

- 适用领域：带历史记忆的聊天机器人 / 个人助手；是当前**直接对比记忆架构**最常被引用的基准（尤其在知识更新与多会话任务上区分度高）。长上下文 LLM 在 S 档相比简单设定有 30%~60% 的性能下降。

---

### 1.3 BEAM（Beyond a Million Tokens）

- 来源链接：
  - 论文：Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs（ICLR 2026）
  - GitHub：https://github.com/leegisang/BEAM
  - 数据：https://huggingface.co/datasets/Mohammadta/BEAM （及 `Mohammadta/BEAM-10M`）
  - 解读：https://mem0.ai/blog/what-is-beam-memory-benchmark-the-paper-that-shows-1m-context-window-isnt-enough
  - Leaderboard：**统一榜 AMB（含 beam 100K/500K/1M/10M 四档）** https://agentmemorybenchmark.ai/ ；另见 mem0 研究页 https://mem0.ai/research 与 Hindsight 的 10M 档 SOTA 汇总 https://hindsight.vectorize.io/blog/2026/04/02/beam-sota 。

- 介绍说明：针对「1M 上下文窗口是否足以替代记忆系统」这一问题设计。通过规划式流水线生成**叙事连贯、保持身份一致、事实随时间演化**的多领域超长对话，并配套探针问题。评测 **10 项记忆能力**：事实/实体追踪、信息更新、矛盾消解（contradiction resolution）、时序顺序/事件排序、区分指令与偏好、多跳推理、长历史摘要、偏好遵循、指令遵循、拒答等。论文同时提出受人类认知启发的 **LIGHT** 框架（情景记忆 + 工作记忆 + 事实便签 scratchpad）。

- 数据量：**100 段**对话、**2,000 道**校验过的探针问题；多尺度上下文 **128K / 500K / 1M / 10M tokens**（分 BEAM-1M 与 BEAM-10M 两个 track）。10M 档单段约含 1 万+ 用户/助手消息、7,757 turns。

- 适用领域：多领域（编程、数学、健康、金融、个人等）**生产级、超大规模**记忆评测。设计目标是「任何现有记忆架构都无法刷满」——是验证大上下文窗口无法替代真正记忆架构的关键基准（10M 档下纯 RAG 显著退化）。

---

### 1.4 MemoryAgentBench

- 来源链接：
  - 论文：https://arxiv.org/abs/2507.05257 （Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions）
  - GitHub：https://github.com/HUST-AI-HYZ/MemoryAgentBench
  - 数据：https://huggingface.co/datasets/ai-hyz/MemoryAgentBench
  - Leaderboard：**无官方在线榜单**。结果以论文表格形式给出（详见论文与仓库 README）；HF 数据卡 https://huggingface.co/datasets/ai-hyz/MemoryAgentBench 亦含各记忆 Agent 对比。

- 介绍说明：强调**增量式多轮交互**（把数据切块模拟真实多轮对话流），而非静态长上下文 QA。基于认知科学定义 **4 项核心能力**：准确检索（Accurate Retrieval, AR，含多跳）、测试时学习（Test-Time Learning, TTL，运行时学习新规则/技能而无需训练）、长程理解（Long-Range Understanding, LRU，≥100K tokens 的全局理解/摘要）、冲突消解 / 选择性遗忘（Conflict Resolution / Selective Forgetting, CR/SF，覆盖过时事实）。是首个同时覆盖这四项能力的基准。

- 数据量：由既有数据集重构 + 新构建的 **EventQA** 与 **FactConsolidation** 两个数据集组成；上下文规模覆盖 100K+ tokens 量级（增量切块注入）。

- 适用领域：通用记忆 Agent（从 context-based、RAG 到带外部记忆模块/工具集成的高级 Agent）。适合系统性诊断记忆系统在「检索 / 学习 / 理解 / 更新」四个维度的短板。

---

### 1.5 PersonaMem（及 PersonaMem-v2）

- 来源链接：
  - PersonaMem 论文：https://arxiv.org/abs/2504.14225 （Know Me, Respond to Me: Benchmarking LLMs for Dynamic User Profiling and Personalized Responses at Scale）
  - 项目主页：https://zhuoqunhao.github.io/PersonaMem.github.io/
  - 数据：https://huggingface.co/datasets/bowen-upenn/PersonaMem
  - PersonaMem-v2 论文：https://arxiv.org/html/2512.06688v1
  - Leaderboard：**有榜单**。① GitHub README「Performance Leaderboard」区（评测 15 个 SOTA LLM）https://github.com/bowen-upenn/PersonaMem ；② 统一榜 AMB（含 personamem 列）https://agentmemorybenchmark.ai/ 。

- 介绍说明：聚焦**个性化 / 人格与偏好追踪**。每个样本是一个含静态属性（人口统计）与动态属性（随时间演化的偏好）的用户 persona，用户与 chatbot 在多会话、多场景（如餐饮推荐、旅行规划、心理咨询）中交互。评测 **7 类 in-situ 用户查询**，考察模型能否：记住用户画像、追踪偏好演化、在新场景下生成个性化回复。强调**偏好多为隐式表达**。

- 数据量：
  - **PersonaMem (v1)**：180+ 段模拟交互历史，每段最多 **60 个会话（~1M tokens）**，覆盖 15 个个性化场景；提供按上下文长度划分的多个版本。
  - **PersonaMem-v2**：1,000 个 persona、20,000+ 偏好、300+ 场景、每条上下文最高 **128K tokens**；含 5,000 条评测 QA + 20,000 条训练/验证 QA；支持多模态、多语言。

- 适用领域：个性化助手 / 推荐型对话、用户画像与偏好建模。前沿模型（GPT-4.5、o1、Gemini-2.0、Llama-4-Maverick 等）整体准确率约 50% 或更低，说明隐式偏好追踪仍是难点。

---

### 1.6 MemBench

- 来源链接：
  - 论文：https://arxiv.org/abs/2506.21605 （MemBench: Towards More Comprehensive Evaluation on the Memory of LLM-based Agents, ACL 2025 Findings）
  - ACL Anthology：https://aclanthology.org/2025.findings-acl.989.pdf
  - GitHub：https://github.com/import-myself/Membench
  - Leaderboard：**无官方在线榜单**。论文给出 7 种记忆机制对比结果；第三方统一榜 AMB（含 membench 列）https://agentmemorybenchmark.ai/ 收录。

- 介绍说明：强调评测维度的「全面性」。数据集区分两个**记忆层级**：事实性记忆（factual memory，显式陈述的信息）与反思性记忆（reflective memory，隐式推断的偏好等）；并设计两类**交互场景**：参与（participation，Agent 直接与用户交互）与观察（observation，Agent 以第三方视角追踪消息）。提供时间感知的评测框架，含 **4 项指标**：准确率（accuracy）、召回（recall）、容量（capacity）、时间效率（temporal efficiency）。是首个强调反思性记忆的基准之一。

- 数据量：多场景（participation / observation）× 多层级（factual / reflective）构建的可扩展数据集，约 100K tokens 量级上下文；论文在其上评测了 7 种常见记忆机制。

- 适用领域：通用 LLM Agent 记忆机制的综合性诊断，特别适合需要同时衡量**有效性、效率与容量**的研究对标。

---

### 1.7 MemSim / MemDaily

- 来源链接：
  - 论文：https://arxiv.org/abs/2409.20163 （MemSim: A Bayesian Simulator for Evaluating Memory of LLM-based Personal Assistants）
  - OpenReview：https://openreview.net/pdf?id=8w22WLy2R8
  - GitHub：https://github.com/nuster1128/MemSim
  - Leaderboard：**无官方在线榜单**。完整基准结果在仓库 `benchmark/full_results`；第三方统一榜 AMB（含 memsim 列）https://agentmemorybenchmark.ai/ 收录。

- 介绍说明：本质是一个**数据生成器 + 基准**。提出 **Bayesian Relation Network (BRNet)** 与因果生成机制，从模拟的用户消息中**自动构造可靠的 QA 对**，以缓解 LLM 幻觉对事实信息的污染，同时兼顾多样性与可扩展性。基于该模拟器生成日常生活场景数据集 **MemDaily**，并据此构建评测基准。从两个角度评测：有效性（effectiveness，存储与利用事实的能力）与效率（efficiency）。

- 数据量：MemDaily 为自动合成、规模可扩展的日常生活 QA 数据集（完整基准结果在仓库 `benchmark/full_results`）。

- 适用领域：LLM 个人助手的记忆机制评测；其「可自动、低成本扩展生成可靠 QA」的特性，适合作为持续回归测试与大规模评测的数据来源。

---

### 1.8 LifeBench（Long-Horizon Multi-Source Memory）

- 来源链接：
  - 论文：https://arxiv.org/html/2603.03781 （LifeBench: A Benchmark for Long-Horizon Multi-Source Memory）
  - GitHub / 数据：https://github.com/1754955896/LifeBench
  - TLDR：https://tldr.takara.ai/p/2603.03781
  - Leaderboard：**无官方在线榜单**。论文 Figure 7 给出 MemU / Hindsight / MemOS 等系统排名（MemOS 居首约 55.2%）；第三方统一榜 AMB（含 lifebench 列）https://agentmemorybenchmark.ai/ 收录。

- 介绍说明：突破「只评对话」的局限，模拟个人**长达一整年的多源数字痕迹**（personas、每日事件、手机操作痕迹、健康记录、月度摘要等），同时要求**陈述性记忆（语义/情景）与非陈述性记忆（习惯/程序性）** 的联合推理。受认知科学启发，按事件的部分—整体层级（partonomy）组织以保证跨时间尺度一致性，并支持并行生成。设计 **4 大类共 2,003 道问题**：信息抽取、多源推理、时序演化、非陈述性记忆推理。提供中英文双语版本，并已转换为 LoCoMo 输入格式（`our.json`）便于复用。Apache 2.0 许可。

- 数据量：**10 个用户**的全年数据；总规模约 **66M tokens / 332 MB**；单用户均值：短信约 1,813 条、事件约 234 个、Agent 对话约 688 段、照片约 1,233 张、笔记约 363 条、推送约 2,350 条。问题共 **2,003 道**。

- 适用领域：个性化 Agent 的长周期、多源记忆；也可用于推荐系统、弱势群体服务研究、游戏 NPC 生成等。SOTA 记忆系统（MemU、Hindsight、MemOS 等）准确率仅约 **55.2%**，难度较高。

---

### 1.9 DialSim / LongDialQA

- 来源链接：
  - 论文：https://arxiv.org/abs/2406.13144 （DialSim: A Real-Time Simulator for Evaluating Long-Term Multi-Party Dialogue Understanding）
  - OpenReview（ICLR 2026 投稿）：https://openreview.net/forum?id=O0FcS21JVY
  - Leaderboard：**无官方在线榜单**。结果以论文表格形式给出，代码/榜单维护在项目仓库 https://github.com/jiho-kim/DialSim 。

- 介绍说明：面向**长期多人对话（multi-party）** 理解的对话模拟评测框架。Agent 扮演剧本中的一个角色，仅凭对话历史实时回答自发提出的问题，并需识别「自己信息不足」而拒答。为减少对先验知识的依赖，所有角色名被匿名化/替换。配套数据集 **LongDialQA** 由长篇美剧剧本构建。

- 数据量：LongDialQA 含 **1,300+ 对话会话**，每段配 **1,000+** 精编问题，单段总量超 **352,000 tokens**。

- 适用领域：多人对话场景（影视、群聊、客服多方）下的长期记忆与理解；强调实时性与多方依赖，是对「两两对话」类基准的补充。即便大上下文窗口或 RAG 模型也难以在多方长程交互中保持准确理解。

---

## 2. 其他/相关 Benchmark（简要）

| Benchmark | 来源 | 简介 | 数据量 | 适用领域 |
| --- | --- | --- | --- | --- |
| **MSC（Multi-Session Chat）** | Facebook AI（arxiv 2107.07567） | 早期多会话开放域闲聊数据集，奠定「跨会话记忆」评测雏形 | ~5K 对话，平均 3.4 会话 | 开放域双人闲聊记忆 |
| **Conversation Chronicles** | arxiv 2310.13420（EMNLP 2023） | 带时间关系标注的多会话对话数据集 | ~200K 对话，平均 5 会话 | 开放域双人长期对话 |
| **MADail-Bench** | arxiv 2409.xxxxx | 儿童—助手情感对话的记忆/情感评测 | 80 段，平均 9.2 turns | 情感陪伴 / 儿童对话 |
| **ES-MemEval / EvoEmo** | arxiv 2602.01885 | 面向**情感支持**场景、用户状态随时间演化的多会话记忆基准（QA + 摘要 + 对话生成） | EvoEmo 平均 27.2 会话 / 13.3K tokens / 最多 33 会话 | 个性化情感支持对话 |
| **Life-Bench（多模态）** | arxiv 2602.19001 | 围绕「虚拟账户」的多模态个性化记忆与推理基准（文本+图像） | 16,315 QA / 10 账户 / 33 概念 / 2,479 图像 / 10 任务 | 多模态个性化检索与推理 |
| **LifeDialBench（EgoMem/LifeMem）** | github.com/qys77714/LifeDialBench | 面向「麦克风常开」生活日志式连续对话的长期记忆基准（含真实第一视角视频 EgoMem 与模拟社区 LifeMem），采用在线流式评测防时间泄漏 | 待论文接收后释出 | 生活日志 / 多方连续对话 |

> 说明：上述部分基准（如 LifeDialBench）数据尚未完全公开，或主要作为对比表中的「相关工作」出现，引用时建议以原论文为准。

---

## 3. 排行榜（Leaderboard）资源总览

> 重要现实：**多数学术 Benchmark（LoCoMo、LongMemEval、MemBench、MemSim、MemoryAgentBench、LifeBench、DialSim）并无官方维护的在线排行榜**，分数主要散落在原论文表格、各厂商研究页与 GitHub Issue/README 的「自报跑分」中，口径（judge 模型、检索设定、prompt）差异较大，横向对比需谨慎。

- **AMB（Agent Memory Benchmark）—— 当前唯一的中立、统一在线榜**：https://agentmemorybenchmark.ai/
  - 在同一条件下评测各记忆/检索系统，已统一收录 **beam、lifebench、locomo、longmemeval、personamem、memsim、membench、ama-bench** 等多个数据集的成绩，是目前做「跨基准、跨系统」对比的首选入口。
  - 配套理念见 https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark （Agent Memory Benchmark: A Manifesto）。
- **mem0 公开研究页**：https://mem0.ai/research —— 持续公布其在 LoCoMo / LongMemEval / BEAM-1M / BEAM-10M 上的准确率、检索 token 量与 p50 延迟。
- **Bench'd**：https://benchd.ai/benchmarks —— 第三方独立评测，覆盖 LoCoMo / LongMemEval 等并给出系统排名。
- **Papers With Code 风格聚合**：https://www.wizwand.com/dataset/longmemeval —— 汇总 LongMemEval / LoCoMo 各子任务的 SOTA。
- **PersonaMem 自带榜**：https://github.com/bowen-upenn/PersonaMem （README「Performance Leaderboard」评测 15 个 SOTA LLM）。

| Benchmark | 是否有官方榜 | 推荐查分入口 |
| --- | --- | --- |
| LoCoMo | ✖ | AMB · mem0 研究页 · wizwand · GitHub Issues 自报 |
| LongMemEval | ✖ | AMB · mem0 研究页 · wizwand · 厂商博客（Exabase/Honcho 等） |
| BEAM | ✖（AMB 统一托管） | **AMB（100K/500K/1M/10M）** · mem0 研究页 · Hindsight 博客 |
| MemoryAgentBench | ✖ | 论文表格 · HF 数据卡 |
| PersonaMem | ✅（README 榜） | GitHub README · AMB |
| MemBench | ✖ | 论文表格（7 种机制对比）· AMB |
| MemSim / MemDaily | ✖ | 仓库 `benchmark/full_results` · AMB |
| LifeBench | ✖ | 论文 Figure 7（MemOS≈55.2% 居首）· AMB |
| DialSim / LongDialQA | ✖ | 论文表格 · 项目仓库 |

---

## 4. 汇总对比表

| Benchmark | 年份/会议 | 数据形态 | 数据量（会话/问题/上下文） | 核心评测能力 | 多模态 | 适用领域 |
| --- | --- | --- | --- | --- | --- | --- |
| **LoCoMo** | 2024 / ACL | 两人多会话对话 | 10 段（原 50）/ ~1,540 QA / 平均 600 turns·16K tokens·≤32 会话 | 单跳、多跳、时序、开放域、对抗 + 事件摘要 + 多模态对话生成 | ✅（图片） | 个人助手 / 开放域对话 |
| **LongMemEval** | 2024 / ICLR 2025 | 用户—助手聊天 | 500 QA / S≈115K tokens·~40 会话；M≈500 会话·~1.5M tokens | 信息抽取、多会话推理、时序、知识更新、拒答 | ✖ | 带记忆的聊天助手 |
| **BEAM** | ICLR 2026 | 多领域超长对话 | 100 段 / 2,000 QA / 128K·500K·1M·10M tokens | 10 项：事实追踪、更新、矛盾消解、时序、指令/偏好、多跳、摘要、拒答等 | ✖ | 生产级、超大规模、多领域 |
| **MemoryAgentBench** | 2025 | 增量多轮交互 | 重构 + EventQA/FactConsolidation / 100K+ tokens | 准确检索、测试时学习、长程理解、冲突消解/选择性遗忘 | ✖ | 通用记忆 Agent 诊断 |
| **PersonaMem (v1/v2)** | 2025 / 2025 | persona 多会话 | v1：180+ 段·≤60 会话·~1M tokens·15 场景；v2：1,000 persona·20K+ 偏好·5,000 QA·128K tokens | 用户画像记忆、偏好演化追踪、个性化回复 | v2 支持 | 个性化助手 / 偏好建模 |
| **MemBench** | 2025 / ACL Findings | 参与+观察场景 | 多场景×多层级 / ~100K tokens | 事实性 + 反思性记忆；准确率/召回/容量/时间效率 | ✖ | 记忆机制综合诊断 |
| **MemSim / MemDaily** | 2024 | 合成日常生活 QA | 可扩展自动生成 | 有效性 + 效率（贝叶斯可靠 QA 生成） | ✖ | 个人助手 / 回归测试数据源 |
| **LifeBench** | 2026 | 多源数字痕迹（全年） | 10 用户 / 2,003 QA / ~66M tokens | 信息抽取、多源推理、时序演化、非陈述性记忆 | ✅（多源） | 长周期个性化 Agent |
| **DialSim / LongDialQA** | 2024 / ICLR 2026 投稿 | 多人剧本对话 | 1,300+ 会话 / 1,000+ QA·段 / >352K tokens | 多人长期对话理解、拒答 | ✖ | 多方对话（影视/群聊/客服） |

---

## 5. 选型建议（面向 agent-memory）

- **对话记忆基线对标**：优先用 **LoCoMo** + **LongMemEval**，二者是业界事实标准，便于与 Mem0、Zep/Graphiti、MemOS 等横向比较；注意 LoCoMo 已存在约 6.4% 标注噪声、LongMemEval 评测对 judge prompt 敏感（约 ±10% 摆动）。
- **大规模 / 生产级压力测试**：用 **BEAM（1M / 10M）** 验证「大上下文窗口不能替代记忆架构」，重点看矛盾消解与跨会话身份一致性。
- **能力维度诊断**：用 **MemoryAgentBench**（检索/学习/理解/更新四维）与 **MemBench**（事实 vs 反思、效率/容量）做细粒度短板分析。
- **个性化 / 偏好追踪**：用 **PersonaMem(-v2)** 评测隐式偏好与画像演化。
- **多源 / 非对话场景（端侧、多模态、长周期）**：用 **LifeBench**（多源数字痕迹）与多模态 **Life-Bench** 覆盖手机/健康记录等真实信息源，契合本项目「输入信息源多模态」的定位。
- **持续回归**：用 **MemSim/MemDaily** 自动生成可靠 QA，作为低成本、可扩展的回归评测数据源。

> 注：除准确率外，建议同时报告 **token 消耗 / 检索 token 量、p50/p95 延迟、记忆容量** 等效率指标（参考 mem0 在四个基准上「均 ~6.7K–7.0K tokens/检索、p50 ≤1.1s」的报告口径），以反映工程可用性。

---

## 6. 参考来源汇总

- LoCoMo：https://arxiv.org/abs/2402.17753 · https://github.com/snap-research/LoCoMo · https://snap-research.github.io/locomo/
- LongMemEval：https://arxiv.org/abs/2410.10813 · https://github.com/xiaowu0162/LongMemEval
- BEAM：https://github.com/leegisang/BEAM · https://huggingface.co/datasets/Mohammadta/BEAM · https://mem0.ai/blog/what-is-beam-memory-benchmark-the-paper-that-shows-1m-context-window-isnt-enough
- MemoryAgentBench：https://arxiv.org/abs/2507.05257 · https://github.com/HUST-AI-HYZ/MemoryAgentBench
- PersonaMem：https://arxiv.org/abs/2504.14225 · https://huggingface.co/datasets/bowen-upenn/PersonaMem · https://arxiv.org/html/2512.06688v1
- MemBench：https://arxiv.org/abs/2506.21605 · https://github.com/import-myself/Membench
- MemSim：https://arxiv.org/abs/2409.20163 · https://github.com/nuster1128/MemSim
- LifeBench：https://arxiv.org/html/2603.03781 · https://github.com/1754955896/LifeBench
- DialSim / LongDialQA：https://arxiv.org/abs/2406.13144 · https://openreview.net/forum?id=O0FcS21JVY
- 排行榜：**AMB 统一榜** https://agentmemorybenchmark.ai/ · mem0 研究页 https://mem0.ai/research · Bench'd https://benchd.ai/benchmarks · wizwand https://www.wizwand.com/dataset/longmemeval
- 综述/榜单：https://mem0.ai/blog/state-of-ai-agent-memory-2026 · https://mem0.ai/blog/ai-memory-benchmarks-in-2026 · https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark · https://hindsight.vectorize.io/blog/2026/04/02/beam-sota
- ES-MemEval / EvoEmo：https://arxiv.org/pdf/2602.01885 · 多模态 Life-Bench：https://arxiv.org/html/2602.19001
