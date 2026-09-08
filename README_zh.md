<h1 align="center">agent-memory</h1>

<p align="center">
  <strong>框架无关 · 分层记忆 · 多形态接入 — 智能体的长期记忆底座</strong>
</p>

<p align="center">
  <a href="README.md">英文</a>
  ·
  <a href="docs/design/VISION.md">愿景(VISION)</a>
  ·
  <a href="docs/design/architecture.md">架构(ARCHITECTURE)</a>
  ·
  <a href="https://gitcode.com/openJiuwen/agent-memory">GitCode</a>
</p>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/python-≥3.11-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/os-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="OS Support" />
</p>

---

## 简介

openJiuwen **agent-memory** （又称Jiuwen Memory） 是九问社区开源的一款以用户为中心，高精准、高性能、原生安全、可配置的智能体记忆系统。它不只是向量检索，而是融合「多形式索引 + 记忆自演进 + 融合记忆检索 + 多集成方式」的通用记忆底座。无论上层是聊天助手、编码 Agent 还是自主 Agent，都能便捷接入（如通过SDK / HTTP API/ CLI）等方式接入同一套记忆能力。

**注**：后续openjiuwen记忆相关演进以此项目为主，agent-core中的记忆后续会逐步迁移至此。


### 为什么选择 agent-memory？

| 能力 | 价值 |
| --- | --- |
| 框架无关、多形态接入 | 记忆能力不再被某个框架/产品绑定：SDK 进程内嵌入 + HTTP API 进程外接入，全部收敛到同一个 `MemoryAPI` |
| 灵活可配置 | 记忆系统采用分层设计，整体流程采用pipeline+预置算子形式，可横向扩展和按场景配置 |
| 分层记忆结构 | 从原始数据**提取 → 抽象精炼 → 关联分析**，沉淀低（事实/片段）中（事件/关系/主题）高（画像/偏好/技能）多粒度记忆，支持细节与宏观双视角取用 |
| 多形式索引 | 不止向量：关键词(BM25) / 向量 / 图索引（规划中） / 文档（规划中）按配置启用，检索时**混合召回 + 重排**，兼具语义与可解释性 |
| 记忆自演进 | 记忆随交互自我生长：抽取 → 关联 → **冲突消解** → 升华 → **遗忘/降权** 体系化闭环，在线 hot / 离线 background 双通道 |
| 端 / 云 / 端云协同（规划中） | 同一套抽象按场景选型：端侧隐私不出端、云侧弹性强检索、混合形态下热/私有留端 + 冷/共享上云（选择性同步 + 冲突合并） |
| scope 原生隔离与共享 | `org + space` 硬隔离 + 多租户，单 Agent 独占、多 Agent 按需共享池，端云分级放置与同步粒度同源 |
| 透明可治理 | 记忆可检视 / 编辑 / 审计 / 血缘回溯 / 遗忘，`as_of` 历史回溯与检索轨迹（trajectory）全程可观测 |

## 功能特性

| 形态 | 说明 |
| --- | --- |
| **记忆接口层（MemoryAPI）** | `add / search / list / get / update / delete / evolve / admin` + 治理（inspect/trace/audit/grant）与 space 管理，所有接入形态最终映射到同一组语义 |
| **记忆检索（Retrieval）** | 查询理解与去噪 → Storage 首选 pipeline（recall/get/Fuser）→ Reranker → 相关性阈值 → **渐进式披露 L0→L1→L2**，检索轨迹可观测、通道错误结构化返回 |
| **记忆构建（Construction）** | 六类可组合算子：extractor / abstractor / associator / classifier / index_builder / evolver，覆盖从原始信息到记忆索引构建的全流程 |
| **存储抽象（Storage）** | 统一 `Storage` 门面 + 六类标准端口：**KV / 向量 / 全文 / 图 / 融合 / 文件系统**，能力发现 + 两级安全边界，后端可插拔 |
| **Agent 插件** | `agent_plugin/` 面向 JiuwenSwarm接入封装，OpenClaw / Codex / Hermes 等生态也在持续规划中 |
| **评测框架（Evaluation）** | 两层评测：组件级 IR（Recall@k/MRR/nDCG 等）与端到端 QA（LLM-as-judge），内置 LoCoMo / LongMemEval 适配器 + smoke test |

## 架构总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│  A. 调用与数据接入层   CLI · Skill · SDK(Python) · HTTP/gRPC · MCP           │
│      ＋ 多模态信息源(对话/文档/代码/工具轨迹/图像/音视频)接入                  │
│  B. 记忆接口层        add · search · get · update · delete ·               │
│  (Memory API)        evolve · admin（形态无关，PEP 鉴权/审计点）              │
├──────────────────────────────────────────────────────────────────────────┤
│  C. 记忆管理层       生命周期 · 治理(检视/编辑/审计/遗忘) · 权限 · 配置/策略    │
├──────────────────────────────────────────────────────────────────────────┤
│  D. 记忆检索层       查询解析 · Storage 检索内核 · 重排 · 渐进式披露           │
├──────────────────────────────────────────────────────────────────────────┤
│  E. 记忆构建层       分层记忆结构（皆可从原始数据重建）：                      │
│     (Layered Memory) 从原始数据提取 → 抽象精炼/关联分析 → 多抽象粒度          │
│                      记忆 ＋ 多形式索引(文档·关键词·向量·图)；                │
│                      由记忆自演进持续构建与维护                                │
├──────────────────────────────────────────────────────────────────────────┤
│  F. 记忆存储层       统一 Storage 领域操作 · 能力发现 · 安全边界 · 检索适配    │
│                      后端端口: KV · 向量 · 全文 · 图 · 融合 · 文件系统         │
├──────────────────────────────────────────────────────────────────────────┤
│  G. 数据层           用户记忆数据 · Agent 记忆数据（原始数据，唯一真源）        │
└──────────────────────────────────────────────────────────────────────────┘
   横切：端/云/端云协同部署 · 可观测(检索轨迹) · 多租户隔离 · 安全合规
```


## 快速上手

### 环境要求

- Python 3.11+
- （可选）配置 LLM（用于自演进的抽取/抽象）与 embedding 模型

### 安装

```bash
# 方式一：源码方式（当前推荐，仓库根即包根）
git clone https://gitcode.com/openJiuwen/agent-memory.git
cd agent-memory
pip install -e .                 # 最小内核 + SDK
pip install -e ".[dev]"          # 追加开发/测试依赖
pip install -e ".[deploy]"       # 追加真实存储后端（Milvus / ES / Redis / PostgreSQL）
pip install -e ".[embed]"        # 追加高级 embedding / 重排（torch、BGE 等）
```

### 快速集成（SDK 进程内嵌入）

```python
from jiuwen_memory.api import assemble
from jiuwen_memory.config import Config
from jiuwen_memory.common.type_def import Scope, Context

# 从 YAML 装配（无 YAML 则按内置默认, 纯内存离线栈）
api = assemble(config=Config.from_yaml("examples/config.yml"))

scope = Scope(org="acme", user="alice", agent="assistant", session="s1")

# 写入记忆
units = api.add("Alice 喜欢在早上喝美式咖啡，不加糖。", scope, identity=scope, tags=["demo"])

# 检索记忆（混合召回 + trajectory 可观测）
res = api.search("咖啡 早上", Context(scope), identity=scope, top_k=3, with_trajectory=True)
for item in res.items:
    print(item.content)
```

### HTTP Server

```bash
scripts/run-server.sh                       # 默认在 http://127.0.0.1:8080 启动
scripts/run-cli.sh --server http://127.0.0.1:8080 search "coffee" -u alice
curl -X POST http://127.0.0.1:8080/v1/add \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default", "scope": "alice", "content": "Alice is a Python developer."}'
```

## 运行评测

```bash
# Smoke Test（CI 必过）
python -m pytest evaluation/smoke_test -v

# 组件级 IR 评测（默认跑内置冒烟评测基准）
python evaluation/scripts/run_ir_eval.py --json results.json

# 端到端 QA 评测（需配置 JUDGE_* 环境变量，内置 LoCoMo / LongMemEval 适配器）
export JUDGE_BASE_URL=...  JUDGE_MODEL=...  JUDGE_API_KEY=...
python evaluation/scripts/run_e2e_eval.py --dataset locomo
```

## 存储后端配置

通过 `examples/config_template.yml`（两级命名空间：组件 → 具名实例）按需替换默认实现，改变实现/换后端只需改配置、各层不动：

```yaml
globals:
  vector_enabled: true        # 向量索引 + 召回路
  graph_enabled: true         # 图召回路
  rerank_enabled: true        # 披露前精排

# 换存储后端：覆盖命名空间下的 default 实例
kv_store:
  default: { target: redis, params: { url: "redis://localhost:6379/0", db: 0 } }
vector_store:
  default: { target: milvus, params: { uri: "http://localhost:19530" } }
```

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [愿景 VISION](docs/design/VISION.md) | 设计原则、五大能力支柱、竞品对标与差异化、成功标准 |
| [架构 ARCHITECTURE](docs/design/architecture.md) | 七层架构、数据模型（MemoryUnit/Scope）、检索/构建/存储/部署细节、开放问题 |
| [竞品分析](docs/design/competitor_analysis.md) | 主流记忆系统调研 |
| [Benchmark 调研](docs/design/memory_benchmarks.md) | LoCoMo / LongMemEval / BEAM 等基准选型 |
| [评测说明](evaluation/README.md) | 两层评测用法与数据集指引 |
| [特性设计](docs/features/) | 各模块设计取舍（storage / construction / retrieval / control / common） |
| [技术规约](docs/specs/) | 各模块设计规约文档 |
| [开发者指导文档](docs/zh/) | 主要包含Agent接入指导， API 文档，FAQ, 安装指导等|


## 参与贡献

欢迎开发者参与 agent-memory 的建设。你可以通过以下方式贡献：

- 提交 Bug、功能建议或使用问题：[Issues](https://gitcode.com/openJiuwen/agent-memory/issues)
- 提交代码、文档或示例：[Pull Requests](https://gitcode.com/openJiuwen/agent-memory/pulls)

**重要**：合入 PR/MR 时，base 分支请选择 `mem2.0`（当前开发分支）而非默认的 `main`。

贡献前请阅读 [开发规范](.claude/CLAUDE.md)，了解代码风格、分层与测试策略。项目遵循「算子 + 插件 + 存储」三类契约，新功能请先在 `docs/design/architecture.md` 对应层确定落点。

## 常见问题

1. **为什么内核包名是 `jiuwen_memory` 而不是 `src`？** 内核已由 `src/` 迁移为发布用的一级包 `jiuwen_memory`，保证 wheel 的顶层包唯一（避免 `src` 布局干扰）。
2. **没有 LLM / 向量库时能用吗？** 能。默认装配为纯内存离线栈（无任何外部依赖），keyword 检索即可工作；自演进/图/向量按配置启用。
3. 更多相关文档，详见 [FAQ](docs/zh/FAQ/)

## License

本项目基于 [Apache License 2.0](LICENSE) 开源。

本产品为记忆基础设施，不内置 AI 模型能力；用户在连接 AI 模型用于特定业务场景时，需自行承担 GDPR、欧盟 AI 法案等相关合规义务。