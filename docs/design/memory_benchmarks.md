# Agent 记忆 Benchmark 调研（Memory Benchmarks Survey）

> 调研对象：业界用于评测「Agent 长期记忆 / Memory Layer」能力的主流公开 Benchmark
> 用途：为 `agent-memory` 记忆系统的能力评测、效果对标与差异化定位提供参考
> 调研时间：2026-05
> 维度：来源链接、Benchmark 介绍说明、数据量、适用领域（评测能力）、License、Leaderboard 状态、成本估算

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

> 统一字段：来源链接 / 介绍说明 / 数据量 / 适用领域 / License / Leaderboard 状态 / 成本估算。

### 1.1 LoCoMo（Long Conversational Memory）

- 来源链接：论文 https://arxiv.org/abs/2402.17753 ；项目主页 https://snap-research.github.io/locomo/ ；GitHub / 数据 https://github.com/snap-research/LoCoMo （`data/locomo10.json`）。
- 介绍说明：Snap Research 提出的超长期多会话对话基准，通过「LLM Agent + 人物 persona + 时序事件图」生成并人工校验长程一致性。任务覆盖 QA、事件摘要、多模态对话生成；问题含单跳、多跳、时序、开放域、对抗等类型。
- 数据量：公开版 `locomo10.json` 含 **10 段**高质量超长对话（初版 arxiv 为 50 段）；平均每段约 **600 turns / 16K tokens**，最多 **32 个会话**；QA 任务约 **1,540 道**非对抗问题。
- 适用领域：个人助手 / 陪伴类、开放域多会话对话记忆；常用于对标 Mem0、Zep、OpenAI Memory 等记忆层产品。
- License：以 GitHub 仓库声明为准。
- Leaderboard 状态：无官方榜单；推荐查分入口为 AMB、mem0 研究页、wizwand 聚合与 GitHub Issues 自报。
- 成本估算：中等；公开版规模小，但多题型评测通常需要 LLM-as-judge，引用分数时需保留数据版本与 judge 口径。

### 1.2 LongMemEval

- 来源链接：论文 https://arxiv.org/abs/2410.10813 ；GitHub / 数据 https://github.com/xiaowu0162/LongMemEval ；ICLR Poster https://iclr.cc/virtual/2025/poster/28290 。
- 介绍说明：聚焦聊天助手的长期交互记忆，通过 needle-in-a-haystack 风格流水线将证据对话嵌入可扩展、带时间戳的聊天历史。评测信息抽取、多会话推理、时序推理、知识更新、拒答等能力。
- 数据量：**500 道**人工精编问题；LongMemEval-S 约 **115K tokens / 40 会话**，LongMemEval-M 约 **500 会话 / 1.5M tokens**；另含 oracle 检索设定。
- 适用领域：带历史记忆的聊天机器人 / 个人助手；尤其适合区分知识更新与多会话推理能力。
- License：以 GitHub 仓库声明为准。
- Leaderboard 状态：无官方榜单；推荐查分入口为 AMB、mem0 研究页、wizwand 与 GitHub Issues / 厂商博客自报。
- 成本估算：中到高；M 档上下文长，且主流以 GPT-4o 作为 LLM-as-judge，成本随模型与样本重复次数上升。

### 1.3 BEAM（Beyond a Million Tokens）

- 来源链接：论文 https://arxiv.org/abs/2510.27246 ；GitHub https://github.com/leegisang/BEAM ；数据 https://huggingface.co/datasets/Mohammadta/BEAM ；解读 https://mem0.ai/blog/what-is-beam-memory-benchmark-the-paper-that-shows-1m-context-window-isnt-enough 。
- 介绍说明：针对「1M 上下文窗口是否足以替代记忆系统」设计，通过规划式流水线生成身份一致、事实随时间演化的多领域超长对话。评测事实追踪、信息更新、矛盾消解、时序、多跳、摘要、偏好/指令遵循、拒答等 10 项能力。
- 数据量：**100 段**对话、**2,000 道**探针问题；上下文覆盖 **128K / 500K / 1M / 10M tokens**，含 BEAM-1M 与 BEAM-10M track。
- 适用领域：生产级、超大规模、多领域记忆评测；用于验证大上下文窗口无法替代真正记忆架构。
- License：以 GitHub / Hugging Face 数据页声明为准。
- Leaderboard 状态：无独立官方榜，AMB 统一托管 100K/500K/1M/10M 四档；可参考 mem0 研究页与 Hindsight 10M 档汇总。
- 成本估算：高；1M/10M 档对上下文长度、检索延迟、存储与 judge 成本要求最高，适合作为压力测试而非日常单测。

### 1.4 MemoryAgentBench

- 来源链接：论文 https://arxiv.org/abs/2507.05257 ；GitHub https://github.com/HUST-AI-HYZ/MemoryAgentBench ；数据 https://huggingface.co/datasets/ai-hyz/MemoryAgentBench 。
- 介绍说明：强调增量式多轮交互，而非静态长上下文 QA。基于认知科学定义准确检索、测试时学习、长程理解、冲突消解 / 选择性遗忘四项核心能力。
- 数据量：由既有数据集重构并新增 **EventQA** 与 **FactConsolidation**；上下文规模覆盖 100K+ tokens 量级。
- 适用领域：通用记忆 Agent 的系统性诊断，适合定位检索、学习、理解、更新四类短板。
- License：以 GitHub / Hugging Face 数据页声明为准。
- Leaderboard 状态：无官方在线榜；结果以论文表格、仓库 README 与 HF 数据卡为主。
- 成本估算：中等；增量切块注入增加运行轮次，但上下文规模低于 BEAM 超长档。

### 1.5 PersonaMem（及 PersonaMem-v2）

- 来源链接：PersonaMem 论文 https://arxiv.org/abs/2504.14225 ；项目主页 https://zhuoqunhao.github.io/PersonaMem.github.io/ ；数据 https://huggingface.co/datasets/bowen-upenn/PersonaMem ；PersonaMem-v2 论文 https://arxiv.org/html/2512.06688v1 。
- 介绍说明：聚焦个性化 / 人格与偏好追踪。样本包含静态属性与动态偏好，用户与 chatbot 在多会话、多场景中交互，评测模型能否记住画像、追踪偏好演化并生成个性化回复。
- 数据量：PersonaMem v1 含 **180+ 段**模拟交互历史，每段最多 **60 会话 / ~1M tokens**，覆盖 15 个场景；PersonaMem-v2 含 **1,000 persona、20,000+ 偏好、300+ 场景、5,000 评测 QA、20,000 训练/验证 QA**，上下文最高 **128K tokens**。
- 适用领域：个性化助手 / 推荐型对话、用户画像与偏好建模，尤其适合隐式偏好追踪。
- License：以 GitHub / Hugging Face 数据页声明为准。
- Leaderboard 状态：有 README 榜；推荐查分入口为 GitHub README「Performance Leaderboard」与 AMB。
- 成本估算：中到高；v2 样本规模更大，个性化回复通常需要生成式 judge 或人工复核。

### 1.6 MemBench

- 来源链接：论文 https://arxiv.org/abs/2506.21605 ；ACL Anthology https://aclanthology.org/2025.findings-acl.989.pdf ；GitHub https://github.com/import-myself/Membench 。
- 介绍说明：强调评测维度的全面性，区分事实性记忆与反思性记忆，并覆盖参与 / 观察两类交互场景。指标包含准确率、召回、容量、时间效率。
- 数据量：多场景 × 多层级构建的可扩展数据集，约 100K tokens 量级上下文；论文评测了 7 种常见记忆机制。
- 适用领域：通用 LLM Agent 记忆机制综合诊断，适合同时衡量有效性、效率与容量。
- License：以 GitHub 仓库声明为准。
- Leaderboard 状态：无官方在线榜；可参考论文表格与 AMB。
- 成本估算：中等；数据规模适合作为回归基准，成本主要来自多机制对比与 judge。

### 1.7 MemSim / MemDaily

- 来源链接：论文 https://arxiv.org/abs/2409.20163 ；OpenReview https://openreview.net/pdf?id=8w22WLy2R8 ；GitHub https://github.com/nuster1128/MemSim 。
- 介绍说明：本质是数据生成器 + 基准。通过 Bayesian Relation Network 与因果生成机制，从模拟用户消息中自动构造可靠 QA 对，生成日常生活场景数据集 MemDaily，并从有效性与效率两个角度评测。
- 数据量：MemDaily 为自动合成、规模可扩展的日常生活 QA 数据集，完整结果在仓库 `benchmark/full_results`。
- 适用领域：LLM 个人助手记忆机制评测；适合作为持续回归测试与大规模合成评测数据来源。
- License：以 GitHub 仓库声明为准。
- Leaderboard 状态：无官方在线榜；可参考仓库 `benchmark/full_results` 与 AMB。
- 成本估算：低到中；可自动扩展数据，适合低成本回归，但需控制生成数据质量与分布漂移。

### 1.8 LifeBench（Long-Horizon Multi-Source Memory）

- 来源链接：论文 https://arxiv.org/html/2603.03781 ；GitHub / 数据 https://github.com/1754955896/LifeBench ；TLDR https://tldr.takara.ai/p/2603.03781 。
- 介绍说明：模拟个人长达一整年的多源数字痕迹，要求陈述性记忆与非陈述性记忆联合推理。问题覆盖信息抽取、多源推理、时序演化、非陈述性记忆推理，并提供中英文双语版本。
- 数据量：**10 个用户**全年数据，总规模约 **66M tokens / 332 MB**；问题共 **2,003 道**。
- 适用领域：个性化 Agent 的长周期、多源记忆，也可用于推荐系统、服务研究、游戏 NPC 生成等。
- License：Apache 2.0。
- Leaderboard 状态：无官方在线榜；可参考论文 Figure 7 与 AMB。
- 成本估算：高；多源全年数据和 66M token 规模适合作为系统级能力验证，常规迭代应抽样或做 mini split。

### 1.9 DialSim / LongDialQA

- 来源链接：论文 https://arxiv.org/abs/2406.13144 ；OpenReview https://openreview.net/forum?id=O0FcS21JVY ；代码/榜单维护在项目仓库 https://github.com/jiho-kim/DialSim 。
- 介绍说明：面向长期多人对话理解的实时模拟评测框架。Agent 扮演剧本角色，仅凭对话历史回答自发问题，并需在信息不足时拒答。
- 数据量：LongDialQA 含 **1,300+ 对话会话**，每段配 **1,000+** 精编问题，单段总量超 **352,000 tokens**。
- 适用领域：多人对话场景（影视、群聊、客服多方）下的长期记忆与理解，强调实时性与多方依赖。
- License：以项目仓库声明为准。
- Leaderboard 状态：无官方在线榜；结果以论文表格与项目仓库为主。
- 成本估算：中到高；问题量大且多方上下文长，适合评估多方依赖和拒答策略。

---

## 2. 其他/相关 Benchmark（简要）

| Benchmark | 来源 | 简介 | 数据量 | 适用领域 |
| --- | --- | --- | --- | --- |
| **MSC（Multi-Session Chat）** | Facebook AI（arxiv 2107.07567） | 早期多会话开放域闲聊数据集，奠定「跨会话记忆」评测雏形 | ~5K 对话，平均 3.4 会话 | 开放域双人闲聊记忆 |
| **Conversation Chronicles** | arxiv 2310.13420（EMNLP 2023） | 带时间关系标注的多会话对话数据集 | ~200K 对话，平均 5 会话 | 开放域双人长期对话 |
| **MADial-Bench** | arxiv 2409.15240 | 记忆增强对话生成评测，覆盖被动/主动记忆唤起与情感支持指标 | 80 段，平均 9.2 turns | 情感支持 / 记忆增强对话 |
| **ES-MemEval / EvoEmo** | arxiv 2602.01885 | 面向**情感支持**场景、用户状态随时间演化的多会话记忆基准（QA + 摘要 + 对话生成） | EvoEmo 平均 27.2 会话 / 13.3K tokens / 最多 33 会话 | 个性化情感支持对话 |
| **Life-Bench（多模态）** | arxiv 2602.19001 | 围绕「虚拟账户」的多模态个性化记忆与推理基准（文本+图像） | 16,315 QA / 10 账户 / 33 概念 / 2,479 图像 / 10 任务 | 多模态个性化检索与推理 |
| **LifeDialBench（EgoMem/LifeMem）** | github.com/qys77714/LifeDialBench | 面向「麦克风常开」生活日志式连续对话的长期记忆基准（含真实第一视角视频 EgoMem 与模拟社区 LifeMem），采用在线流式评测防时间泄漏 | 待论文接收后释出 | 生活日志 / 多方连续对话 |

> 说明：上述部分基准（如 LifeDialBench）数据尚未完全公开，或主要作为对比表中的「相关工作」出现，引用时建议以原论文为准。

---

## 3. 排行榜（Leaderboard）资源入口

> 重要现实：**多数学术 Benchmark（LoCoMo、LongMemEval、MemBench、MemSim、MemoryAgentBench、LifeBench、DialSim）并无官方维护的在线排行榜**，分数主要散落在原论文表格、各厂商研究页与 GitHub Issue/README 的「自报跑分」中，口径（judge 模型、检索设定、prompt）差异较大，横向对比需谨慎。

- **AMB（Agent Memory Benchmark）—— 当前唯一的中立、统一在线榜**：https://agentmemorybenchmark.ai/
  - 在同一条件下评测各记忆/检索系统，已统一收录 **beam、lifebench、locomo、longmemeval、personamem、memsim、membench、ama-bench** 等多个数据集的成绩，是目前做「跨基准、跨系统」对比的首选入口。
  - 配套理念见 https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark （Agent Memory Benchmark: A Manifesto）。
- **mem0 公开研究页**：https://mem0.ai/research —— 持续公布其在 LoCoMo / LongMemEval / BEAM-1M / BEAM-10M 上的准确率、检索 token 量与 p50 延迟。
- **Bench'd**：https://benchd.ai/benchmarks —— 第三方独立评测，覆盖 LoCoMo / LongMemEval 等并给出系统排名。
- **Papers With Code 风格聚合**：https://www.wizwand.com/dataset/longmemeval —— 汇总 LongMemEval / LoCoMo 各子任务的 SOTA。
- **PersonaMem 自带榜**：https://github.com/bowen-upenn/PersonaMem （README「Performance Leaderboard」评测 15 个 SOTA LLM）。

各 Benchmark 的「是否有官方榜」与推荐查分入口已并入 §1 各小节的 `Leaderboard 状态` 字段，避免同一口径在两处维护。

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

- **对话记忆基线对标**：优先用 **LoCoMo** + **LongMemEval**，二者是业界事实标准，便于与 Mem0、Zep/Graphiti、MemOS 等横向比较；注意 LoCoMo 社区 issue 中存在标注噪声反馈、LongMemEval 评测对 judge prompt 敏感（约 ±10% 摆动）。
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
- BEAM：https://arxiv.org/abs/2510.27246 · https://github.com/leegisang/BEAM · https://huggingface.co/datasets/Mohammadta/BEAM · https://mem0.ai/blog/what-is-beam-memory-benchmark-the-paper-that-shows-1m-context-window-isnt-enough
- MADial-Bench：https://arxiv.org/abs/2409.15240
- MemoryAgentBench：https://arxiv.org/abs/2507.05257 · https://github.com/HUST-AI-HYZ/MemoryAgentBench
- PersonaMem：https://arxiv.org/abs/2504.14225 · https://huggingface.co/datasets/bowen-upenn/PersonaMem · https://arxiv.org/html/2512.06688v1
- MemBench：https://arxiv.org/abs/2506.21605 · https://github.com/import-myself/Membench
- MemSim：https://arxiv.org/abs/2409.20163 · https://github.com/nuster1128/MemSim
- LifeBench：https://arxiv.org/html/2603.03781 · https://github.com/1754955896/LifeBench
- DialSim / LongDialQA：https://arxiv.org/abs/2406.13144 · https://openreview.net/forum?id=O0FcS21JVY
- 排行榜：**AMB 统一榜** https://agentmemorybenchmark.ai/ · mem0 研究页 https://mem0.ai/research · Bench'd https://benchd.ai/benchmarks · wizwand https://www.wizwand.com/dataset/longmemeval
- 综述/榜单：https://mem0.ai/blog/state-of-ai-agent-memory-2026 · https://mem0.ai/blog/ai-memory-benchmarks-in-2026 · https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark · https://hindsight.vectorize.io/blog/2026/04/02/beam-sota
- ES-MemEval / EvoEmo：https://arxiv.org/pdf/2602.01885 · 多模态 Life-Bench：https://arxiv.org/html/2602.19001
