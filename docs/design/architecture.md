# agent-memory架构设计（Architecture）

> 文档性质：总体架构设计（概念、分层、组件与依赖方向）
> 版本：v0.2 ｜ 日期：2026-08-06
> 关联文档：[愿景 VISION](./vision.md) ｜ [统一 Storage](../features/storage/F05-unified-storage-design.md) ｜ [Storage 检索 Pipeline](../features/retrieval/F05-storage-retrieval-pipelines.md) ｜ [Benchmark 调研](./memory_benchmarks.md)
> 说明：本文描述系统级架构方向；精确接口契约以 `docs/specs/` 为准，特性取舍与首版实现边界以 `docs/features/` 为准。

---

## 1. 架构目标与约束

承接 [愿景](./vision.md)，架构需同时满足：


| 目标                                  | 架构含义                              |
| ----------------------------------- | --------------------------------- |
| 框架无关、可独立提供、可嵌入可服务                   | 内核与接入层解耦；可作为独立服务/产品交付，也可作为库嵌入；接入层是内核的薄封装 |
| 多形态调用（CLI/Skill/SDK·Python/API/MCP） | 记忆接口层为唯一入口，各形态映射到它               |
| 不止向量：分层 + 多索引 + 结构化关联               | 记忆按抽象粒度分层；索引阶段**支持**文档/关键词/向量/图多形式索引，**按配置启用**、混合检索 |
| 多模态数据输入                             | 多模态来源在接入层规约：保留原模态资产（或引用）+ 派生可治理文本/结构投影 |
| 可配置的记忆真源形态（文档 / 结构化）                | 真源唯一，承载形态可插拔；其上各粒度记忆与索引可重建        |
| 记忆自演进                               | 自演进闭环持续构建/维护分层记忆，在线/离线双通道         |
| 端 / 云 / 端云协同（部署形态）               | 同一抽象屏蔽运行位置；按场景选型、可平滑演进；Hybrid 内端↔云同步 |
| 单 / 多 Agent（隔离与共享）                  | 由 scope 模型统一隔离与共享；与部署形态正交         |
| 透明可治理                               | 可检视/编辑/审计/回溯/遗忘为一等公民              |


**核心架构信条**：**唯一真源 = 原始数据（用户及 Agent 记忆数据）；从中提取相关信息，经抽象与精炼、关联分析，挖掘出不同抽象粒度的记忆（此为记忆结构本身）；并在索引阶段按配置构建文档/关键词/向量/图等多形式索引（结构化关联/图为可选索引形式，非固有结构）——全部可从原始数据重建。**

---

## 2. 架构总览（Layered Overview）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  A. 调用与数据接入层          CLI·Skill·SDK(Python)·HTTP/gRPC·MCP           │
│     Access & Ingest           ＋ 多模态信息源(对话/文档/代码/工具轨迹/图像/音视频)接入│
│  B. （记忆接口） Memory API    add(同步/异步) · search · get · update ·       │
│                              delete · evolve · admin（形态无关）            │
├──────────────────────────────────────────────────────────────────────────┤
│  C. 记忆管理层 Manage         生命周期 · 治理(检视/编辑/审计/遗忘) · 权限 · 配置/策略 │
├──────────────────────────────────────────────────────────────────────────┤
│  D. 记忆检索层 Retrieve       查询解析 · Storage 检索内核适配 · 重排 · 渐进披露│
├──────────────────────────────────────────────────────────────────────────┤
│  E. 记忆构建层 Build          分层记忆结构（皆可从原始数据重建）：           │
│     (Layered Memory)          从原始数据提取 → 抽象精炼/关联分析 → 多抽象粒度 │
│                               记忆 ＋ 多形式索引(文档·关键词·向量·图)；       │
│                               由记忆自演进(§9.3)持续构建与维护                 │
├──────────────────────────────────────────────────────────────────────────┤
│  F. 记忆存储层 Storage        统一领域操作·能力发现·安全边界·检索适配入口    │
│                               后端端口: KV·向量·全文·图·融合·文件系统       │
├──────────────────────────────────────────────────────────────────────────┤
│  G. 数据层 Data               用户记忆数据 · Agent 记忆数据（原始数据，真源） │
└──────────────────────────────────────────────────────────────────────────┘
   横切：端/云/端云协同部署(§11) · 可观测(检索轨迹) · 多租户隔离 · 安全合规
```

> 从下往上看：**数据层（G）持久化原始数据 → 记忆构建层（E）从原始数据提取、经抽象精炼/关联分析挖掘多粒度记忆并构建多形式索引（皆可重建） → 记忆检索层（D）通过统一 Storage 选择检索内核并完成重排与披露 → 记忆管理层（C）做生命周期/治理/权限/配置 → 记忆接口层（B）→ 调用与数据接入层（A）**。记忆存储层（F）以统一 `Storage` 契约屏蔽物理装配，同时按能力暴露标准底层端口；端/云/端云协同为部署维度（§11）。
>
> **记忆管理层（C）总览**：C 层是管理面，负责生命周期、权限、治理、调度与运行时策略的统一编排；职责总览见 §7。

---

## 3. 核心抽象与数据模型

### 3.1 记忆单元（Memory Unit）

最小记忆载体，数据层（真源）中存储的原子记录（无论文档还是结构化形态，逻辑模型一致）：

```
MemoryUnit
├── id              Scope 内唯一 id
├── scope           归属：{ org, space, user, agent, session }（多维，用于隔离/共享）
├── tier            认知角色（记忆分类维度之一）：working/core/episodic/semantic/procedural/archival
├── layers          ContentLayers：l0 概要 / l1 片段；L2 是 content 合并视图
├── segments[]      Segment：每段含 content + assets[] + source
├── source_ref      RawPayload / 会话等来源引用
├── temporal        时间：t_event(发生) / t_ingest(摄入) / t_valid / t_invalid
├── provenance      演进血缘（多→一合成）：由哪些 unit 抽取/升华/合并而来（来源可仍有效）
├── supersedes      版本链（一→一更替）：本版取代的上一版 id（update SUPERSEDE 模式产生；空=首版）
├── hierarchy       HierarchyRef：跨 unit 树结构（设计目标，尚未实现）
├── tags/metadata   标签、命名空间、置信度、重要度等
└── lifecycle       状态：active / superseded(被取代) / archived / forgotten
```

- `temporal` 借鉴 Zep **双时间模型**，支持有效期与时间点回溯。
- **内容真相是 `segments[]`**：`content/assets/source` 均为折叠后的只读兼容视图，不是与 `segments[]` 并列写入的第二份数据。`ContentLayers.l0/l1` 已实现，L2 不重复存储，直接取 `MemoryUnit.content`。
- **`provenance`、`supersedes`、`hierarchy` 三分**：`provenance` 回答“由哪些 unit 抽取或合成”，供 `trace` 回溯；`supersedes` 回答“本版本取代谁”，供 `as_of` 版本回溯；目标 `HierarchyRef` 回答“结构上包含谁、隶属于谁”，供父子树构建与 `search(..., expand_depth>0)` 下钻。三者生命周期、遍历方向和治理动作互不替代。
- `lifecycle` 用「标记失效」而非物理删除（非破坏式更新）。`update` 默认 **SUPERSEDE**（生成新 id 版本、旧版标记 superseded、新版 `supersedes` 记链），亦可 **OVERWRITE**（同 id 原地覆写，旧内容仅留审计——非破坏式原则的有意例外）。
- **多模态**：每个 `Segment` 把可治理文本/结构投影、原模态资产引用与来源模态放在一起；下游索引与检索统一作用于各段合并后的 `content` 视图（详见 §5.1）。

### 3.2 作用域与多租户（Scope）

`Scope` 的字段集合为 `org / space / user / agent / session`。其中 `org > space`
是全局硬层级：`org` 表示组织、账务和上级管理边界，`space` 表示一个 org 内的
逻辑隔离单元，负责多租户的数据边界、权限边界和存储分区边界。检索、写入、更新、
删除、演进和治理默认都限定在单个 target space 内，跨 space 访问必须显式授权。

`agent` 与 `user` 不再隐含全局固定父子关系。space 内的主体归属顺序由
`principal_path` 配置决定：

| `principal_path` | 逻辑层级 | 适用场景 |
| --- | --- | --- |
| `user_agent` | `org > space > user > agent > session` | 以用户为中心的个人助手、企业员工助手、用户画像记忆 |
| `agent_user` | `org > space > agent > user > session` | 以 Agent/应用为中心的客服机器人、编码 Agent、多用户产品服务 |

权限判断中的 owner-cover 规则按 `principal_path` 解释为“actor scope 是 target
scope 的前缀”。这样同一套 `Scope` 字段既能表达 `user -> agent`，也能表达
`agent -> user`，不会把某一种业务关系写死成全局模型。

> **统一连接点**：同一套 scope 模型同时支撑两条正交维度——「**单/多 Agent 的隔离与共享**」与「**端云协同时数据的分级放置与同步粒度**」（哪些 scope 留端、哪些上云、按什么粒度同步），见 §11。

---

## 4. 分层记忆结构（Layered Memory by Abstraction Granularity）

本节只定义记忆结构的 **What**：系统区分**短时记忆**（工作/会话态，易失）与**长时记忆**（可持久、可治理、可重建的分层结构）。长时记忆按抽象粒度组织，索引是建在记忆之上的可配置检索结构，不是记忆本体。

```
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ 多形式索引 Indexes：文档索引 · 关键词索引 · 向量索引 · 图索引（主要建于长时记忆）│
 │   （检索时由记忆检索层 §8 做融合召回 + 重排）                                 │
 └────────────────────────────────────────────────────────────────────────────┘
                              ▲ 在各抽象粒度记忆上构建索引
 ┌────────────────────┐  升华  ┌──────────────────────────────────────────────────┐
 │ 短时记忆 Short-term  │ /沉淀  │ 长时记忆 Long-term（分层记忆结构，按抽象粒度）       │
 │  工作记忆/会话上下文  │ ─────▶ │   高抽象（精炼/概括）  画像 · 长期偏好 · 习得技能/模式 │
 │  近期缓冲/临时状态    │        │   中抽象（关联/组织）  事件 · 实体关系 · 主题聚类     │
 │  易失、快速读写       │        │   低抽象（贴近原始）  抽取的事实/片段               │
 └────────────────────┘        └──────────────────────────────────────────────────┘
            ▲ 缓冲/沉淀                 ▲ 构建算子生成不同抽象粒度（见 §9.1）
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ 数据层 原始数据（唯一真源，承载形态可配：文档 / 结构化）                        │
 └────────────────────────────────────────────────────────────────────────────┘
   ▲ 长时记忆与索引皆可从原始数据重建（短时记忆为易失工作态，不必持久重建）
```

长时记忆的“分层”由四个不能互相推导的轴组成：

| 轴 | 表达载体 | 含义 |
| --- | --- | --- |
| 同 unit 渐进披露 | `ContentLayers` + `DisclosureLevel` | 一条 unit 的 L0/L1/L2 压缩度 |
| 多模态构建 | `metadata.memory_level` + `provenance` | 同一媒体源产出的不同粒度 unit |
| 认知抽象 | `MemoryTier` + evolve | unit 的认知角色与演进 |
| 跨 unit 树结构 | `HierarchyRef` | 父摘要包含可按需展开的子证据 |

- **短时记忆 vs 长时记忆**：短时记忆承载当前工作/会话上下文（近期缓冲、临时状态，易失、快速读写）；长时记忆承载可跨会话复用的事实、事件、主题关系、画像、偏好、技能与模式。
- **抽象粒度**：低抽象贴近原始事实/片段，中抽象组织事件、实体关系与主题，高抽象沉淀画像、长期偏好、可复用技能/模式。具体构建算子见 §9.1。
- **多形式索引（按配置启用）**：索引建立在记忆之上，支持文档 / 关键词 / 向量 / 图等形态，具体启用哪些由配置决定（§13），检索融合见 §8。
- **真源唯一、皆可重建**：原始数据是唯一权威来源；各粒度记忆与索引均可从它重算（推广 memSearch「删索引不丢数据」理念到整个记忆构建层）。本架构主信条。
- **认知角色作为分类维度**：「认知角色（working/core/episodic/semantic/procedural/archival）」作为记忆的**一个分类维度**存在（决定常驻上下文 or 按需检索），不单列为独立轴。
- **由自演进持续维护**：这些结构不是一次性产物，而是由记忆自演进（§9.3）按触发时机持续维护。

> 注：「演进产物与真源的关系（append-only？演进产物是否回写真源？）」为**设计阶段开放问题**，见 §17。

---

## 5. 调用与数据接入层（Access & Ingest）

本层合并「调用」与「数据接入」两个职责：对外暴露多形态调用入口，并承接多模态信息源的接入与规约。调用入口是记忆接口层的**薄封装**，区分「嵌入」与「接入」两类耦合（见 VISION §6 讨论）：

```
   in-process（嵌入 embed）        │   out-of-process / 协议（接入 integrate）
   ───────────────────────────────┼────────────────────────────────────────────
   SDK (Python, 一等支持)          │   CLI · Skill · MCP Server · HTTP/gRPC API
        │                          │     │       │        │           │
        └──────────────┬───────────┴─────┴───────┴────────┴───────────┘
                       ▼
              记忆接口层 (Memory API)
   ───────────────────────────────────────────────────────────────────
   数据接入：多模态信息源 → 保留原模态资产(或引用) + 规约出可治理文本/结构投影 → 写入真源
```


| 形态             | 耦合    | 典型宿主/用途                               |
| -------------- | ----- | ------------------------------------- |
| SDK（Python 为主） | 进程内嵌入 | 直接集成进 Agent 应用，低时延                    |
| CLI            | 子进程调用 | 脚本/调试/端侧工具/编码 Agent                   |
| Skill          | 技能包加载 | OpenClaw 等 Agent 生态                   |
| MCP Server     | 协议连接  | Cursor / Codex / Claude Code 等 MCP 宿主 |
| HTTP/gRPC API  | 远程服务  | 任意语言 / 分布式系统                          |


> 核心主张是**框架无关**：SDK 提供嵌入，CLI/Skill/MCP/API 提供接入，二者共同支撑「不绑定单一框架」。

### 5.1 多模态数据接入与规约

多模态体现在**数据输入侧**：图像/音频/视频/文档/代码等均可作为记忆来源。接入层对每种模态做两件事，**检索链路本身不感知模态**：

```
 多模态来源 ─┬─ 保留原模态资产(或对象存储引用)  ──▶ 写入真源 (assets，可治理、可回溯)
            └─ 规约出可治理文本/结构投影        ──▶ 写入真源 (content)
                 · 图像 → caption / OCR / 视觉描述
                 · 音频 → 转录(ASR)
                 · 视频 → 关键帧描述 + 转录
                 · 文档/代码 → 解析 + 结构化切分
                          │
                          ▼
            记忆构建层(§9.1/§9.2，分层结构见 §4) 在 content 投影上提取/抽象/关联、构建多形式索引
```

- **原模态资产为真源、文本投影为派生**：与「真源唯一 + 派生可重建」一致；重跑规约器即可重建投影。
- **检索统一在文本/结构投影上进行**：向量/关键词/图/文档索引均基于 `content`，不引入专门的「多模态向量检索」（保持检索链路简单一致）。
- **可插拔规约器**：ASR/OCR/caption 模型为可替换组件（§12 可插拔），端侧可降级或延迟到云侧（§17 开放问题）。

---

## 6. 记忆接口层（Memory API）

所有接入形态最终映射到同一组语义。接口已落地为 `jiuwen_memory/api/memory_api.py` 的 `MemoryAPI`（统一 Core API，形态无关）。它是**控制层的薄封装且为鉴权/审计执行点（PEP）**：数据面（add/search/list/get/update/delete/evolve）委托 `jiuwen_memory/control/engine.py` 的 `MemoryEngine`（编排中枢），管理面查询（任务状态、治理、授权、space 管理）直达对应控制算子（Scheduler/Governor/PermissionManager/SpaceManager），admin 直达 PolicyManager。每个涉及租户数据/治理的方法都收 `scope`（目标范围 target）与 `identity`（调用方身份，**必填 keyword-only**）——本层先 `check(identity, scope, action)`、落带 identity 的入口审计，通过后才委托，下游只收已鉴权的 target scope（签名以代码为准）。写入类方法及 `MemoryPatch` 分别接收 `system_metadata` 与 `user_metadata`，不再接收混合 `metadata`。Space 管理接口已由 `SpaceManager` 承接。

本章**只列出当前代码已实现的对外接口**，一方法一行。详细用法、数据结构、特性文档对照、**已设计但尚未实现**的增量见 [S02-memory-api.md](../specs/S02-memory-api.md)。代码落地后须：去掉 S02（及受影响 F 文档）中的「尚未实现」标注，并把该方法（或增量入参）补进本表。

| 对外接口方法 | 语义 | 入参 | 出参 |
| --- | --- | --- | --- |
| `add` | **同步**写入记忆：`content` 文本/结构投影 + 可选 `assets` 原模态资产引用；阻塞至 hot path 完成（落盘 + 轻量索引）后返回本次插入的记忆单元列表（规约/切分可产生多条）。`system_metadata["infer"]=="true"` 时原文落 `/messages/` 并同步抽取派生单元后返回派生列表；`system_metadata["procedural"]=="true"` 时原文不落 KV、返回过程记忆。缺省走 `/memory/` 直写。重演进走 background。实现上桥接引擎异步 `write`，供 CLI/脚本等同步形态 | `content: str`；`scope: Scope`；`source: Modality = TEXT`；`*`；`identity: Scope`；`assets: list[str] \| None = None`；`tags: list[str] \| None = None`；`system_metadata: dict[str, MetadataValueType] \| None = None`；`user_metadata: dict[str, MetadataValueType] \| None = None`；`occurred_at: datetime \| None = None` | `list[MemoryUnit]` |
| `add_async` | **异步**写入记忆：签名/语义同 `add`，直通引擎异步 `write`，供事件循环/高并发接入形态（HTTP/MCP）非阻塞调用 | 同 `add` | `list[MemoryUnit]` |
| `batch_add` | **同步**批量写入。`BatchWriteItem` 含 `content` 及可选 `scope/source/assets/tags/system_metadata/user_metadata/occurred_at/stream_id/sequence/idempotency_key`。顶层参数作批次默认值，单项可覆盖。结果 `outcomes` 与输入索引一一对应；默认归集单项错误，`continue_on_error=False` 时后续项为跳过 | `items: list[BatchWriteItem]`；`scope: Scope \| None = None`；`source: Modality = TEXT`；`*`；`identity: Scope`；`tags: list[str] \| None = None`；`system_metadata` / `user_metadata`（同 `add`）；`occurred_at: datetime \| None = None`；`stream_id: str = ""`；`continue_on_error: bool = True` | `BatchWriteResult` |
| `batch_add_async` | **异步**批量写入：签名/语义同 `batch_add`，串行保序 | 同 `batch_add` | `BatchWriteResult` |
| `check_write` | Pre-flight WRITE 鉴权，不落盘。镜像 `add` 的鉴权与 space 可写校验，供长耗时摄入任务入队前拒绝无权限请求 | `scope: Scope`；`identity: Scope`；`*`；`tags: list[str] \| None = None`；`system_metadata` / `user_metadata`（同 `add`） | `None` |
| `search` | 混合检索召回。`context.scope` 为目标范围；`context.extensions["max_tokens"]` 由本层解析为披露预算后从透传中移除。`filters` 为结构化过滤（FilterExpr / 旧 list / dict DSL）。`as_of` 为 valid-time 回溯。`RetrievalResult` 含命中项、可选轨迹和通道错误 | `query: str`；`context: Context`；`*`；`identity: Scope`；`filters: FilterExpr \| list[FilterClause] \| dict \| None = None`；`as_of: datetime \| None = None`；`top_k: int = 10`；`disclosure: DisclosureLevel = L0`；`with_trajectory: bool = False` | `RetrievalResult` |
| `list` | 列出 scope 下已建索引记忆（只含 `/memory/`，不含 infer 原文）。支持类型/结构化过滤、自定义透传与分页；`items` 为当前页，`count` 为分页前精确总数。`memory_types` 与 `filters` 取 AND；`org/space/user/agent/session` 不得出现在 filters | `scope: Scope`；`*`；`identity: Scope`；`offset: int = 0`；`limit: int = 100`；`memory_types: list[str] \| None = None`；`extensions: dict[str, Any] \| None = None`；`filters: FilterExpr \| list[FilterClause] \| dict \| None = None` | `MemoryListResult` |
| `get` | 按 id 读取记忆单元；`as_of` 非空时沿 `supersedes` 版本链返回当时有效版本；不存在抛 `NotFoundError` | `unit_id: str`；`scope: Scope`；`*`；`identity: Scope`；`as_of: datetime \| None = None` | `MemoryUnit` |
| `update` | 修正记忆（仅非 None 字段生效）：`patch.mode` = **SUPERSEDE**（默认、非破坏式：生成新 id 版本、旧版标记 superseded、新版 `supersedes` 记链）/ **OVERWRITE**（同 id 原地覆写、旧内容仅留审计）。`system_metadata` / `user_metadata` 分别合并 | `unit_id: str`；`scope: Scope`；`patch: MemoryPatch`；`*`；`identity: Scope` | `MemoryUnit` |
| `delete` | 按选择器（id / scope / 标签 / 时间，条件取「与」，至少一项）批量执行；`mode` = forget 遗忘 / archive 归档 / downweight 降权（均非破坏式）/ **purge 完全删除**（物理删除真源与全部派生索引，合规删除、不可恢复、仅留审计记录）；返回命中的 id。未给 `selector.scope` 时鉴权退到根 scope | `selector: DeleteSelector`；`*`；`identity: Scope` | `list[str]` |
| `evolve` | 触发演进（mode：extract / associate / consolidate / forget），经控制层 Scheduler 双通道调度，返回任务 id，不表示已完成；索引维护不在此（随数据面操作自动跟进） | `scope: Scope`；`mode: EvolveMode`；`channel: Channel = BACKGROUND`；`*`；`identity: Scope` | `str`（job id） |
| `job_status` | 查询演进任务或长耗时 Ingest 任务（委托 Scheduler / Ingest 任务表）。Ingest 任务要求传入 target `scope`；API 对任务真实 Scope 执行 READ 鉴权与审计 | `job_id: str`；`*`；`identity: Scope`；`scope: Scope \| None = None` | `JobInfo` |
| `job_cancel` | 取消尚未完成的演进任务（幂等，委托 Scheduler） | `job_id: str`；`*`；`identity: Scope` | `None` |
| `inspect` | 治理·检视：读取完整内容与治理字段（含已失效版本，委托 Governor） | `unit_ids: list[str]`；`scope: Scope`；`*`；`identity: Scope` | `list[MemoryUnit]` |
| `trace` | 治理·血缘回溯：沿 `provenance` 追溯演进来源链（委托 Governor；不沿层级树、不沿 `supersedes`） | `unit_id: str`；`scope: Scope`；`*`；`identity: Scope` | `list[MemoryUnit]` |
| `audit` | 治理·审计查询：按 actor/target/action/layer/时间段等检索审计留痕（委托 Governor）。无具体 target scope 时以根 `Scope()` 为鉴权闸门 | `filters: dict[str, str]`；`*`；`identity: Scope`；`limit: int = 100` | `list[AuditEvent]` |
| `grant` | 跨 scope 授权（委托 PermissionManager）。`Grant`：`grantor` / `grantee` / `actions: list[Action]` / `expires_at`。`Action`：`READ`/`WRITE`/`UPDATE`/`DELETE`/`SHARE` | `grant: Grant`；`*`；`identity: Scope` | `None` |
| `revoke` | 回收授权（幂等；匹配哪条由实现定义，委托 PermissionManager） | `grant: Grant`；`*`；`identity: Scope` | `None` |
| `create_space` | 创建 space，并写入 `principal_path`、状态、metadata 与初始 policy。以 `Scope(org=spec.org)` 做 WRITE 鉴权 | `spec: SpaceSpec`；`*`；`identity: Scope` | `SpaceInfo` |
| `get_space` | 读取单个 space 的基础信息、状态与策略摘要 | `org: str`；`space: str`；`*`；`identity: Scope` | `SpaceInfo` |
| `list_spaces` | 列出 org 下 spaces。`SpaceStatus`：`ACTIVE`/`FROZEN`/`ARCHIVED`/`DELETING`/`DELETED` | `org: str`；`*`；`identity: Scope`；`status: SpaceStatus \| None = None`；`limit: int = 100`；`cursor: str \| None = None` | `list[SpaceInfo]` |
| `update_space` | 修改 display name、metadata、policy 等非破坏字段。`SpacePatch` 仅非 None 生效 | `org: str`；`space: str`；`patch: SpacePatch`；`*`；`identity: Scope` | `SpaceInfo` |
| `archive_space` | 归档 space；已归档后 `add/update/evolve` 拒绝，读取与导出保留 | `org: str`；`space: str`；`*`；`identity: Scope` | `SpaceInfo` |
| `delete_space` | 删除 space 的真源与可重建索引，作为 offboarding 主路径。当前实现只支持 `PURGE` | `org: str`；`space: str`；`*`；`identity: Scope`；`mode: DeleteMode = PURGE` | `SpaceDeleteResult` |
| `export_space` | 提交 space 导出任务，返回 export id | `org: str`；`space: str`；`*`；`identity: Scope`；`include_audit: bool = True` | `str`（export id） |
| `space_usage` | 查询 space 级 memory/message/KV bytes 用量 | `org: str`；`space: str`；`*`；`identity: Scope` | `SpaceUsage` |
| `get_space_policy` | 读取 space 级策略（`require_space` / `principal_path` / 隔离策略 / retention / quotas / index_profiles / pipeline_profiles） | `org: str`；`space: str`；`*`；`identity: Scope` | `SpacePolicy` |
| `set_space_policy` | 替换 space 级策略，并同步主体路径 | `org: str`；`space: str`；`policy: SpacePolicy`；`*`；`identity: Scope` | `SpacePolicy` |
| `list_space_members` | 列出 space 成员与角色 | `org: str`；`space: str`；`*`；`identity: Scope` | `list[SpaceMember]` |
| `add_space_member` | 添加或更新 space 成员角色 | `org: str`；`space: str`；`member: SpaceMember`；`*`；`identity: Scope` | `None` |
| `remove_space_member` | 移除 space 成员 | `org: str`；`space: str`；`member: Scope`；`*`；`identity: Scope` | `None` |
| `admin_get` | admin：读取一项运行时可变策略的当前值（直达 PolicyManager）。管理面以根 `Scope()` 鉴权 | `key: str`；`*`；`identity: Scope` | `str` |
| `admin_set` | admin：调整一项运行时策略（启停索引、检索/演进开关等；键未知或不可变配置抛 `PolicyError`） | `key: str`；`value: str`；`*`；`identity: Scope` | `None` |
| `admin_all` | admin：列出全部运行时策略及当前值 | `*`；`identity: Scope` | `dict[str, str]` |


> - **接口层 PEP 与 Storage 数据面授权是两道边界**：公开 API 仍以 `identity`（调用方）和 `scope`（目标 target）执行鉴权与入口审计，`identity` 不自动下沉。统一 Storage 另提供可选的 `StorageAccessContext`，供嵌入式直调或需要纵深防御的装配显式传入；默认 `AllowAllStorageSecurity` 不增加授权限制。两者不能互相替代，当前 API 链路也不宣称已自动传播 Storage 授权上下文。
> - **表面 = 数据面 + 管理面**：数据面委托 `MemoryEngine`，管理面查询（job/inspect/trace/audit/grant/revoke/space 管理）直达 Scheduler/Governor/PermissionManager/SpaceManager，admin 直达 PolicyManager；调用层只依赖 `jiuwen_memory/api` 即可触达全部对外能力。
> - **薄封装 + 引擎编排**：`MemoryAPI` 不含业务逻辑；接入/落盘/索引/检索/调度的编排全部在 `MemoryEngine`（`jiuwen_memory/control`）。引擎内核只保留**一条异步写链路**（`async def write`），接口层的同步 `add` / `batch_add` 由其自行桥接（如 `asyncio.run`）。
> - 接口形态无关：不论真源是文档还是结构化、运行在端还是云，调用方语义一致。
> - **双时间一等暴露**：`search`/`get` 的 `as_of`（valid-time）直接消费 §3.1 的双时间模型，支持「按当时状态」的时间点查询与历史回溯，与 query 文本里解析出的事件时间（event-time）分轴（对应 §15 吸收 Zep 的落点）。
> - **统一异常契约**：错误由 `common/errors`（根 `AgentMemoryError`）的类型承载——`NotFoundError`/`ConflictError`/`PermissionDeniedError`/`ValidationError`/`PolicyError`/`HealthCheckError`/`BackendError`，调用方跨后端/跨层用同一套捕获，不依赖具体实现自带异常。
> - **控制模式**：`evolve` 与自动触发对应 §9.3 的 `agent_control / static_control / both`。
> - **不设 `link` 接口**：记忆/实体关联不对外暴露为接口语义，由构建层 Associator 在演进（§9.3）中自动维护（`Relation` 结构供图索引内部使用）。

---

## 7. 记忆管理层（Manage）

记忆管理层是架构中的控制面，而非另一条数据存储或检索流水线。它负责把生命周期、治理、权限、调度与运行时策略统一成可审计的管理动作，并通过记忆接口层暴露给调用方。

| 管理职责 | 作用 | 主要落点 |
| --- | --- | --- |
| 生命周期 | 维护 memory 的 active / superseded / archived / forgotten 状态，并管理 space 的创建、冻结、归档、删除与 offboarding | 数据模型 §3.1；scope 模型 §3.2；自演进触发 §9.3；接口 `delete/update/inspect/delete_space` §6 |
| 权限与隔离 | 基于 `org + space` 硬边界、space 级 `principal_path` 与 identity 做访问控制，跨 space 共享必须显式授权 | scope 模型 §3.2；接口层 PEP §6 |
| 治理与审计 | 支持检视、编辑、血缘回溯、space 级审计查询、导出、用量统计与可观测轨迹 | 横切关注点 §12；接口 `inspect/trace/audit/export_space/space_usage` §6；结构轴 §4；构建 §9；`evolve(HIERARCHY)` §6 |
| 调度与策略 | 管理 hot/background 任务、演进阶段、索引开关与运行时可变策略 | 自演进控制 §9.3；配置体系 §13 |

---

## 8. 记忆检索层（Retrieve）

```
 query ─▶ ① QueryParser：查询理解/去噪（清洗为空则短路返回）
        ─▶ ② 合并 scope/标签/时间等系统谓词与用户过滤
        ─▶ ③ Retriever 按 Storage 的全局首选值选择检索内核
              ├ recall ─▶ 读取 id 去重 ─▶ get ─▶ 恢复分通道证据 ─▶ Fuser
              ├ recall_and_get ──────────────────────────────────▶ Fuser
              └ retrieve(parsed_query, fuser) ─▶ Storage 内完成上述三步
        ─▶ ④ Reranker ─▶ 相关性阈值 ─▶ 最终 top_k
        ─▶ ⑤ 渐进式披露 (L0 摘要 → L1 片段 → L2 全文)
        ─▶ ⑥ 返回 items + errors + 可选 trajectory
```

- **检索内核只含三步**：Storage 适配的边界仅为 `recall`、`get`、`rank`，其中 `rank` 只指 Fuser 的分层归并与跨通道融合；Reranker、阈值、最终 `top_k` 和 Discloser 始终留在 Retriever。
- **三条等价 Pipeline**：组合后端使用 `recall -> get -> rank`；可直接回带 MemoryUnit 的后端使用 `recall_and_get -> rank`；一体化平台使用 `retrieve`。Storage 通过稳定的 `preferred_retrieval_pipeline()` 声明首选路径，Retriever 不按请求动态探测或失败后静默切换。
- **共享候选契约**：索引候选使用 `ScoredUnit`，物化候选使用 `ScoredMemoryUnit`；`ParsedQuery`、候选、分批结果、通道错误及 Fuser 最小协议位于 `jiuwen_memory/common/type_def`，避免 Storage 反向依赖 Retrieval 实现。
- **读取去重不丢证据**：第一条 pipeline 只对批量 `get` 的 id 去重；读取完成后恢复每个召回入口的 score、channel 和 evidence，再交给 Fuser，保证同一 MemoryUnit 的多通道命中仍参与融合。
- **分层索引不是新通道**：L0/L1/L2 可以是同一 Vector 或 Fulltext 通道的多个物理入口；Fuser 先按同一 channel、同一 unit 做 MaxP，再做跨通道融合，避免层数更多的 MemoryUnit 被重复加权。
- **部分失败可返回**：部分通道失败时保留成功候选，并通过 `RetrievalResult.errors` 返回结构化 `ChannelError`；全部选中通道失败时抛 `StorageRetrievalError`。错误可见性不依赖 `with_trajectory`。
- **渐进式披露**：吸收 OpenViking 的 L0/L1/L2 分层加载，控制 token。
- **检索轨迹**：吸收 OpenViking 的可观测性，每步召回/排序可追溯，非黑盒。
- **query 去噪与空查询短路**：`QueryParser` 先剥除上游包装噪声并产出结构化查询；清洗后无有效文本时，`Retriever` 在召回前直接返回空结果，避免噪声触发无意义召回。
- **scope 是独立轴、显式串参**：检索范围作为首参贯穿 `Retriever.retrieve(scope, query)` → `Storage` 检索入口 → 底层 Store（query 是「找什么」、scope 是「在谁的范围内找」），不随查询对象携带、也不混进过滤条件。Store 层必须以 `org + space` 作为硬隔离键；`agent/user/session` 只在 space 内按 `principal_path` 参与归属与过滤。
- **前置过滤结构化**：`filters` 为 `FilterExpr`（`FilterClause` 叶子 +
  `FilterGroup` 的 AND/OR/NOT 树），由检索层与系统谓词做外层 AND 后下推。生产
  Milvus/Elasticsearch 在通道截断前完整执行，物化后再用共享纯函数复核真源。
- **两条时间轴**：`as_of` 是系统相信时间（valid-time，回溯「T 时刻哪个版本有效」），与从 query 文本解析出的事件时间约束（event-time，`time_from/time_to`，过滤 `t_event`）分开，互不折叠。
- **通道↔Store 非 1:1**：`RecallChannel` 是逻辑召回路，到物理 Store 的映射由 Storage 装配内部决定（一路对一 Store，多路也可合到 FusionStore 一次召回；TEMPORAL 多为叠加在其他通道上的时间过滤）。未指定通道表示调用全部已配置通道，显式空列表是无效输入。
- **树结构过滤与按需展开（目标）**：当入参显式给出 `hierarchy_kind`（及可选 role/span）时，在既有流程的过滤阶段叠加结构条件；指定父侧 role 即父优先召回。`expand_depth>0` 时，在融合/重排之后沿命中父节点的有序 `child_ids` 点读子证据（单 kind），再进入既有 Discloser。叶→父分数上卷（`rollup`）默认关闭。展开与父命中共用既有 `max_tokens` 上下文预算，不另设独立树预算池。`HierarchyKind.TIME` 不等于 `RecallChannel.TEMPORAL`。

---

## 9. 记忆构建层详解：构建算子（自演进触发见 §9.3）

本层对应 §2 的 E 层，负责从真源或既有记忆产物生成派生记忆、关系与索引，并按自演进触发持续维护。分层记忆结构本体见 §4；§9.1/§9.2 讲 How，§9.3 讲 When。

本节定义记忆构建的 **How**：构建层由一组可组合算子组成，从真源或已有记忆产物生成新的记忆单元、关系与索引。算子本身不决定何时运行；触发时机、hot/background 通道与控制模式见 §9.3。

### 9.1 多粒度记忆挖掘：提取 → 抽象精炼 → 关联分析

从原始数据或既有记忆产物沉淀出不同抽象粒度的记忆：


| 环节        | 作用                         | 关键点                                                                           |
| --------- | -------------------------- | ----------------------------------------------------------------------------- |
| **信息提取**  | 从原始数据抽取事实/事件/偏好（贴近原始的低抽象粒度） | 多模态信息源已在接入层规约为可治理文本/结构                                                         |
| **抽象与精炼** | 情景→语义、经验→技能/模式，概括出高抽象记忆    | 升华出画像、长期偏好、可复用技能/模式                                                            |
| **关联分析**  | 实体共指、因果/引用链、跨会话/跨 Agent 关联 | 支持多跳推理、「连点成线」；构成中抽象的关系/主题结构                                                    |
| **多维分类**  | 按主题/认知角色/来源/重要度等多维度归类      | 认知角色（working/core/episodic/semantic/procedural/archival）是其中一维，决定常驻上下文 or 按需检索 |
|| **同 unit 内容层标注** | `LayerAnnotator` 生成 L0 概要 / L1 片段；L2 复用全文                    | 当前已接 EXTRACT/CONSOLIDATE 派生 unit，必须在其持久化与索引前完成；普通 write 尚未接入 |
| **时间字段**         | 有效期、时间点、事件先后                                                 | 双时间模型，支持历史回溯与非破坏式更新                                                           |
| **树结构构建（目标）**    | `HierarchyComposer` 从权威叶生成父摘要与双向父子引用                         | 所有 kind 共享树校验、叶权威与单 kind 遍历约束 |
| **TIME 树管线（目标）** | `TimeHierarchyPipeline` 按区间构建 snapshot/time_span/scene/event | `MemoryUnit.temporal` 提供叶事件时间，`HierarchyRef.span_*` 表示父覆盖区间 |

### 9.2 多形式索引：文档 / 关键词 / 向量 / 图

索引构建器把记忆单元、关系与分类字段写入对应检索结构。索引类型由配置决定（§13），检索层（§8）只消费已启用的索引。


| 索引        | 作用                   | 关键点                                |
| --------- | -------------------- | ---------------------------------- |
| **文档索引**  | 面向文档形态记忆的路径/章节式定位与浏览 | 对齐 memSearch/OpenViking；文档真源下的一等索引 |
| **关键词索引** | 全文/BM25 精确匹配         | 与向量互补，提升可解释性                       |
| **向量索引**  | 语义相似召回               | 可插拔 embedding；端侧用轻量模型              |
| **图索引**   | 实体-关系、因果/引用链多跳遍历     | 支撑关联分析与「连点成线」                      |
| 标签/命名空间   | scope 过滤、多租户隔离       | `org + space` 硬隔离，`agent/user/session` 按 space 内主体路径过滤 |


> 各粒度记忆与各形式索引本身都是可重建派生物，落在记忆存储层的对应后端中。

### 9.3 记忆自演进（Evolution）：触发时机与控制模式

本节定义自演进的 **When**：哪些事件触发构建算子、走在线还是后台、由 Agent 还是系统管线控制。构建算子的职责见 §9.1/§9.2。

```
   交互流 ─▶ 抽取 ─▶ 关联 ─▶ 冲突消解 ─▶ 升华 ─▶ 遗忘/降权
 (对话/工具/  (候选   (挂实体   (新旧矛盾   (情景→语义  (过期/低价值
  多源痕迹)   事实)    /图谱)    标记失效)   经验→技能)   清理)
                              ▲                         │
                              └──── 反馈 / 自评 ◀────────┘
```

- **写入触发**：新内容写入后，hot path 做低时延落盘与轻量索引；需要重推理的抽取、关联、升华与冲突消解进入 background。
- **周期触发**：按策略对过期、低价值或长期未访问记忆做降权、归档或遗忘（非破坏式，保留血缘）。
- **显式触发**：调用方可通过 `evolve(scope, mode, channel, *, identity)` 触发指定阶段。
- **双通道**：
  - **Hot path（在线）**：低时延的即时记忆写入与轻量更新。
  - **Background（离线）**：异步做重的抽取/升华/重索引，不阻塞主链路。
- **控制模式**：`agent_control`（Agent 自主调用记忆工具）/ `static_control`（开发者/管线控制）/ `both`。
- **演进阶段（EvolveMode）**：`extract / associate / consolidate / forget / hierarchy`。**索引维护不是演进模式**——它随数据面操作（write/update/delete）由 IndexBuilder 增量跟进（build/update/remove），从真源全量重建走 `IndexBuilder.rebuild()` 维护路径（上述 background「重索引」即指此类维护工作，由数据面/维护触发，而非 `evolve(mode=…)`）。

---

## 10. 记忆存储层：真源与存储抽象（Source of Truth & Storage）

### 10.1 可配置的真源承载形态

真源唯一（数据层中的原始数据），但**承载形态按场景配置**，对上层接口透明：

```
 ┌────────── 文档形式 (Document-as-source) ──────────┐   ┌──── 结构化形式 (Structured-as-source) ────┐
 │ 真源 = Markdown / 文件                            │   │ 真源 = DB / KV 中的 MemoryUnit 记录       │
 │ 派生 = 影子索引(可重建)                           │   │ 派生 = 向量 / 全文 / 图等附加索引         │
 │ 优势: 人可读·可编辑·可 git·可审计·删索引不丢数据  │   │ 优势: 高并发·强检索·规模化·多租户弹性    │
 │ 适配: 编码 Agent / 跨工具复用 / 端侧轻量          │   │ 适配: 高并发个性化 / 大规模多租户        │
 └───────────────────────────────────────────────────┘   └──────────────────────────────────────────┘
```


| 场景                           | 建议真源形态                |
| ---------------------------- | --------------------- |
| 编码 Agent / git 化 / 可审计 / 跨工具 | 文档形式（Markdown + 影子索引） |
| 高并发个性化 / 大规模多租户              | 结构化形式（向量 + 结构化关联）     |
| 时序敏感 / 企业知识                  | 结构化 + 时序图             |
| 端侧隐私 / 轻量                    | 文档形式 或 SQLite         |


### 10.2 存储抽象（Pluggable Backends）

上层统一依赖 `Storage`，不感知底层 Store 的选择、组合和实例共享；需要索引数据模型的组件
仍可通过标准端口访问完整底层契约。一体化存储或召回平台可以直接实现 `Storage`，不必先拆成
多个物理 Store 再让上层重新装配。

```text
        Construction / Retrieval / Control
                         |
                      Storage
       领域操作 · 能力发现 · Security · 检索适配入口
                         |
             +-----------+-----------+
             |                       |
      CompositeStorage         IntegratedStorage
             |                       |
   KV / Vector / Fulltext /     一体化存储或召回平台
    Graph / Fusion / FS
```

- **领域操作**：顶层 `add/update/delete/get/list` 面向 `MemoryUnit`；`add` 只保存上层已经形成的真源记录，不负责接入、抽取、演进或生成索引投影。
- **标准端口**：`storage.kv/vector/fulltext/graph/fusion/fs` 暴露对应 Store 的完整抽象接口，而非 Redis、Milvus 等具体实现；端口访问仍经过 Storage 的统一授权代理。
- **能力模型**：`capabilities()` 是全局、不可变的唯一事实来源，`has_kv()`、`has_vector()` 等由其推导。未声明端口被访问时抛 `UnsupportedStorageCapabilityError`。检索 pipeline 不属于 capability，由 `preferred_retrieval_pipeline()` 单独表达。
- **组合与一体化实现**：`CompositeStorage` 复用已装配的 Store 并提供默认组合能力；`IntegratedStorage` 是面向一体化平台的扩展形态，不要求物理暴露平台内部使用的每一种索引技术。
- **两级安全边界**：`Storage.security` 负责可插拔的通用数据面授权，默认 allow-all；各 `Store.security` 负责适配自身数据模型的数据保护，未启用时必须明确为 passthrough。固定调用顺序为 Storage 授权、选择/调用 Store、Store 数据保护、访问后端。
- **健康检查**：`Storage.health()` 检查其声明的能力、统一 Security 和 Store Security；能力集合不会随单次健康状态动态变化。

| 抽象 | 端侧实现示例 | 云侧实现示例 | 备选 |
| --- | --- | --- | --- |
| 真源（文档/二进制） | 本地文件系统 / Markdown | 对象存储 / 文件服务 | Git 仓库 |
| 真源（结构化）/ KV | SQLite | PostgreSQL / Redis | — |
| 向量索引 | 轻量本地向量 | Milvus | pgvector / Qdrant / Chroma / GuassVector |
| 图/关联索引 | SQLite 关系表 / Kuzu | Neo4j | FalkorDB / Kuzu |
| 全文索引 | SQLite FTS5 | 专用全文引擎 | — |
| 融合存储 | 本地组合实现 | 一体化检索平台 | — |

> 当前首版已落地 `Storage`、`StorageProducer`、`CompositeStorage`、能力与安全模型；
> `storage.default` 可选择组合或一体化实现，Kernel 与 Retriever 共享同一具名实例。
> Construction 与 Control 仍保留部分直接 Store 依赖，后续按模块迁移。
> `IntegratedStorage` 是已定义的实现方向，尚未提供仓内实现。精确契约见
> [S06-storage.md](../specs/S06-storage.md)，设计取舍见
> [F05-unified-storage-design.md](../features/storage/F05-unified-storage-design.md)。
> **hierarchy 存储边界（目标）**：首期 `HierarchyRef` 内嵌于 KV 真源的 `MemoryUnit`；kind/role/span 等字段投影到全文/向量索引 metadata 供前置过滤，目标索引可从 KV 重建（当前 rebuild 实现缺口见 §9.2）。GraphStore 只表达实体、因果、引用等非包含关系，不作为首期父子树真源；当同 kind 多父、丰富边属性或跨 kind 组合查询成为主路径时，再评估独立边存储。

---

## 11. 部署架构（Edge / Cloud / Hybrid）

同一套记忆抽象（接口/数据模型/检索与演进语义/scope 一致）屏蔽运行位置；部署形态**按场景选型、可平滑演进**，三选一：

```
        三种部署形态（按场景选型，二/三选一）

  端侧 Edge-only            云侧 Cloud-only
 ┌──────────────┐          ┌──────────────┐
 │ 内核(本地)    │          │ 内核集群      │
 │ SQLite+轻向量 │          │ 全量多索引    │
 │ 隐私不出端    │          │ 弹性扩缩      │
 │ 离线/低时延   │          │ 大容量强检索  │
 └──────────────┘          └──────────────┘

  端云协同 Hybrid（端、云各一节点，内部双向同步）
 ┌──────────────────────────────────────────────┐
 │  端节点                            云节点       │
 │ ┌──────────┐                    ┌──────────┐  │
 │ │热/私有留端│ ◀─选择性同步/加密─▶ │冷/共享/重计算│  │
 │ │ 低时延    │   (双时间+冲突合并)  │大容量强检索 │  │
 │ └──────────┘                    └──────────┘  │
 └──────────────────────────────────────────────┘
```

- **端侧 (Edge-only)**：完整内核的轻量实现（轻量索引、SQLite、本地 embedding），离线可用、隐私优先。
- **云侧 (Cloud-only)**：强检索、大容量、弹性扩缩。
- **端云协同 (Hybrid)**：在**同一系统的端节点与云节点之间**同步（而非三种形态之间）：
  - 记忆分级：热/私有留端，冷/共享/需重计算的上云（分级粒度由 scope 决定，见 §3.2）。
  - **同步协议**：选择性同步、加密传输、冲突合并（与「非破坏式更新 + 双时间」配合解决一致性）。
  - 重计算（如大规模重索引/升华）可卸载到云侧，端侧消费结果。

> 单/多 Agent 与部署形态**正交**：上述任一形态都可承载单 Agent 或多 Agent（隔离与共享由 §3.2 scope 模型统一处理）。

---

## 12. 横切关注点（Cross-cutting）


| 关注点  | 设计要点                                 |
| ---- | ------------------------------------ |
| 可观测性 | 检索轨迹、演进事件、指标（检索 token、p50/p95 时延、容量） |
| 可治理  | 记忆可检视/编辑/审计/回溯/遗忘；演进血缘可追溯            |
| 安全合规 | 接口层 PEP、Storage 可插拔授权、Store 数据保护、scope 隔离、传输/存储加密、可遗忘（合规删除） |
| 多租户  | `org + space` 硬隔离；space 内支持 `user -> agent` 或 `agent -> user` 主体路径；跨 space 受控共享 |
| 可插拔  | embedding、向量库、图库、LLM 抽取器、重排器均为可替换组件  |


---

## 13. 可配置化与场景预设（Configurability & Scenario Profiles）

为灵活支撑不同 Agent 应用场景，本架构把上述各层的关键能力做成**可配置项**：同一套抽象与接口不变，通过配置**裁剪与组合**能力——**「既能全、也能轻」**。这既是适配多场景的手段，也是控制性能/成本的开关。

### 13.1 可配置维度

| 维度 | 可配置内容 | 默认 | 调小/关闭的收益 |
| --- | --- | --- | --- |
| **真源形态**（§10.1） | 文档 / 结构化 | 按 profile | 切轻量真源降存储与运维 |
| **索引类型**（§9.2） | 文档 / 关键词 / 向量 / 图 各自开关 | 关键词+向量（图/文档按需启用） | 关图/向量大幅降写入与存储成本 |
| **检索策略**（§8） | Storage 首选 pipeline、召回通道、重排 on/off、渐进披露层级、`as_of` | 按 Storage 实现选择 + 混合召回 | 关重排/单通道降时延 |
| **树结构**（§4/§8） | 见 [S03 PolicyManager 目标层级策略键](../specs/S03-control.md)（`hierarchy.enabled` / `auto_derive` / `ensure_on_recall` / `score_propagation` / `expand_default_depth` / `expand_top_m`）；建树装配见 [S05 HierarchyComposeProfile](../specs/S05-construction.md)；决策与取值语义见 [F08](../features/common/F08-memory-tree.md) | 默认关闭；公开 search 默认 `expand_depth=0` | 关闭建树/展开可保持 F01 基线成本 |
| **自演进**（§9.3） | 总开关、阶段（extract/associate/consolidate/forget）、hot/background、控制模式 | 全闭环+双通道 | 仅 extract 或纯离线，降在线时延与 LLM 成本 |
| **双时间**（§3.1） | 启用 / 关闭（仅留最新版本） | 启用 | 关闭可省去历史时间维护，适合无回溯需求 |
| **多模态规约**（§5.1） | 启用的规约器、是否留原模态资产、投影粒度 | 文本+按需图像 | 仅文本，去掉 ASR/OCR/caption 依赖 |
| **scope / 共享**（§3.2） | `org + space` 隔离粒度、space 级 `principal_path`、共享池、跨 scope 授权策略 | space 隔离 + `user_agent` 默认 | 单租户简化 |
| **存储后端**（§10.2） | Storage 实现、KV/向量/全文/图/融合/FS 端口选型（extras 选装） | CompositeStorage；端 SQLite / 云 PG+专用库 | 一体化 Storage 或端侧精简能力 |
| **部署 profile**（§11） | edge / cloud / hybrid | 按场景 | — |
| **模型**（§12 可插拔） | embedding / LLM 抽取器 / reranker；端侧降级策略 | 可插拔 | 端侧用小模型或规则降级 |

### 13.2 配置的分层与优先级

配置自上而下分层，**就近覆盖**（下层覆盖上层）：

```
 全局默认（内置 defaults.py）
   └─▶ 部署 Profile / 用户 YAML（装配期合并）
         └─▶ ConfigSource（可插拔来源；默认=上述合并快照，产品可换配置中心）
               └─▶ 租户/scope 级策略（org/space policy、principal_path 等）
                     └─▶ 调用级 options（search/add/evolve 的逐次业务参数）
```

- 这样既能用一个 Profile 一键起步，也能对特定 scope 或单次调用做精细化覆盖。
- **装配拓扑**（选哪些实现类、预装哪些具名实例）在 `build_kernel` 确定；**晚绑定值**（能力开关、prompt 文本、模型名/API Key/URL、Store 连接）经 `ConfigSource.fetch` 在运行中读取。同实现多套 model/key/url/hosts **优先**走晚绑定；`*.active` 多具名实例仅用于异质实现互切。
- 六类商用可配项的抽象与边界见 `docs/features/config/F01-config-source.md` 与 `docs/specs/S08-config.md`；**不**通过 add/search/evolve 入参写入这些配置。

### 13.3 开箱即用的场景预设（Presets）

把上述维度预组合成面向典型 Agent 场景的 profile（可作为起点再微调）：

> ⚠️ **下表的配置取值仅为示意举例**，用于说明「同一套能力如何按场景裁剪组合」，并非推荐值或最终默认；具体每项取值以立项后的 design/spec 与实测调优为准。

| 场景 Profile | 真源 | 索引 | 树结构（示意） | 自演进 | 双时间 | 多模态 | 部署 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **编码 Agent** | 文档(Markdown) | 文档+关键词+向量 | 按目录层级逐层构建      | 轻（extract，离线为主） | 可选 | 文本/代码 | 端侧/本地 | git 化、可审计、低开销 |
| **个人助手** | 结构化/SQLite | 向量+关键词(+图) | 按时间维度/主题维度逐层构建 | 全闭环 | 启用 | 文本+图像 | 端云协同 | 跨设备同步、隐私留端 |
| **企业多 Agent** | 结构化(向量+图) | 全开（含图） | 按时间维度/主题维度逐层构建 | 全闭环+重 background | 启用 | 按需 | 云侧 | 多租户 scope+共享池、强审计 |
| **端侧轻量** | 文档/SQLite | 关键词+轻向量 | 默认关闭树结构构建      | 降级（规则/小模型或延迟到云） | 可关 | 仅文本（音视频延迟到云） | 纯端 | 低资源、离线优先 |

> 「既能全、也能轻」：关闭图/向量/演进/双时间后，可退化为接近 memSearch 的轻量形态（低写入开销）；全开则对标 Mem0/Zep 的完整能力。配置裁剪是把「广度」转化为「按场景的合适成本」的关键手段。

### 13.4 实现落点

- **`jiuwen_memory/config/`**：`Config` / `defaults` / `AssemblyContext` 负责装配期合并；**`ConfigSource`** 负责可插拔配置来源（默认对齐 YAML/defaults，产品可注入配置中心实现）。契约见 `docs/specs/S08-config.md`。
- **`bootstrap/core/profiles.py`**：把维度组合成 `edge/cloud/hybrid` 与上述场景 Preset，装配对应组件与后端。
- **`admin_get/set/all`（§6）**：运行时查询/调整 **PolicyManager** 已知策略键（如 lifecycle 目标、`scope.require_space`）；**不是**六类模型/prompt/store 配置的主通道。
- **两层动态性**：换 ConfigSource 实现或增减预装配组件 → 重建内核；已注入来源上的值（开关/prompt/凭证/连接）→ 运行中 `fetch`（首选）；异质实例选用 → `*.active`（次选）。注册在仓内的多种实现 ≠ 默认已全部预装。

---

## 14. 关键数据流（Data Flows）

**写入路径（Write）**

```
add() → 接入/构建形成 MemoryUnit → Storage.add() 写真源
                                ├─▶ [hot] IndexBuilder 生成投影并写 Storage 标准端口
                                ├─▶ [background] 自演进: 提取→抽象精炼→关联→消解→重建多粒度记忆与索引
                                └─▶ 审计日志(横切)
```

`Storage.add()` 成功只表示 MemoryUnit 真源可读，不表示所有派生索引均已完成；索引投影仍由
Construction 负责。一体化 Storage 可在内部自动建索引，但不能改变这一对上语义。

**读取路径（Read）**

```
search() → 查询理解/去噪 → 空查询短路 → 合并过滤
         → Retriever 按 Storage 首选值执行 recall/get/Fuser 三步内核
         → Reranker → 相关性阈值 → top_k → 渐进式披露
         → items + errors + 可选 trajectory
```

**演进路径（Evolve）**

```
evolve()/触发器 → 读取原始数据 → LLM 提取/抽象升华 → 关联/冲突消解(标记失效) → 写演进产物 → 重建多粒度记忆与索引
```

**同步路径（Sync, 端云协同）**

```
端变更 → 选择性打包(热/私有过滤) → 加密传输 → 云合并(双时间+非破坏式冲突解决) → 回传共享更新
```

---

## 15. 对标映射（Absorb & Differentiate → 架构落点）


| 吸收来源         | 优点                               | 在本架构的落点                               |
| ------------ | -------------------------------- | ------------------------------------- |
| Mem0         | 抽取+更新双阶段流水线、可插拔后端、多租户 scope、混合检索 | §9.3 自演进、§10.2 存储抽象、§3.2 scope、§8 检索     |
| OpenViking   | 统一上下文、L0/L1/L2 分层加载、检索轨迹、记忆自迭代   | §8 渐进式披露+轨迹、§9.3 演进、§10.1 文档真源           |
| Zep/Graphiti | 双时间模型、有效期、非破坏式更新                 | §3.1 temporal、§9.1 时间字段、§14 同步冲突解决    |
| MemOS        | 技能记忆、memory cube 共享、异步调度         | §9.3 升华(经验→技能)、§3.2 共享、§9.3 background 通道 |
| memSearch    | 文档为真源 + 影子索引可重建                  | §10.1 文档真源、核心信条「派生可重建」                 |


**差异化目标**：以上各家多为单点强项，本架构尝试以「**唯一真源（形态可配） + 多抽象粒度的分层记忆 + 多形式索引 + 多形态接入 + 端云三态**」统一整合，且接口/检索体验一致。该目标是否成立，需要在公开基准、端侧资源占用、检索延迟与部署迁移成本上验证。

---

## 16. 代码目录结构（Repository Layout）

> Python 为主（SDK 一等支持）。整体是 **`jiuwen_memory/` 内核** + `bootstrap/`、`agent_plugin/` 薄封装的多形态接入；目录直接对应 §2 的分层，使架构可被代码落地。

```
agent-memory/
├── deploy/                         # 部署物料
│   ├── docker/                     #   容器化部署
│   └── local/                      #   本地部署
│       └── setup.py
│
├── docs/                           # 文档
│   ├── design/                     #   设计文档（VISION / ARCHITECTURE / 调研）
│   ├── specs/                      #   跨模块接口规约（S01-S07）
│   ├── features/                   #   特性文档
│   └── RULES.md
│
├── agent_plugin/                   # Agent 插件接入（依赖内核的封装）
│   ├── JiwenSwarm/
│   ├── openclaw/
│   ├── codex/
│   └── hermes/
│
├── bootstrap/                      # A 调用层（§5）：内核的薄封装（多形态接入），各 surface 共用 core
│   ├── core/                       #   共享应用核：Server 装配 + 共享 dispatch + profiles + config_loader
│   ├── http_server/                #   HTTP/REST surface（POST /v1/<verb>）
│   ├── mcp_server/                 #   MCP surface（FastMCP：记忆 API → MCP 工具）
│   ├── cli/                        #   CLI surface（client + 命令表）
│   └── sdk/                        #   SDK（Python 库嵌入）
│
├── examples/                       # 示例：嵌入用法 / 服务用法 / 端云协同
│
├── evaluation/                     # 测评
│   ├── benchmark/                  #   业界公开数据集（LoCoMo/LongMemEval/BEAM 等）
│   ├── metrics/                    #   测评定义
│   ├── scripts/                    #   测评脚本
│   └── smoke_test/                 #   总体测试
│
└── jiuwen_memory/                  # 内核（in-process 嵌入即用此包 = SDK 内核）
    ├── config/                     # 配置：真源形态 / 索引策略 / 部署 profile（不可变/重型配置）
    │
    ├── common/                     # 跨层共享：能力插件 + 通用结构体 + 异常 + 横切组件
    │   ├── base.py                 #   Plugin 插件契约（pluginType/health）+ PluginType 枚举
    │   ├── errors.py               #   异常类型：AgentMemoryError 根 + NotFound/Conflict/PermissionDenied/Validation/Policy/HealthCheck/Backend
    │   ├── type_def/               #   通用结构体：Scope / MemoryUnit / FilterExpr / RawPayload / AuditEvent；
    │   │                           #   Storage 与 Retrieval 共用的 ParsedQuery / ScoredUnit / ScoredMemoryUnit /
    │   │                           #   RecallBatch / ChannelError / CandidateFuser
    │   ├── tokenizer/              #   分词：构建建倒排 ↔ 检索 query 分词（须同一分词器）
    │   ├── chunker/                #   切分：写入切 chunk ↔ 重索引按同一规则重切
    │   ├── embedder/               #   向量化：chunk 向量 ↔ query 向量（须同模型同维度）
    │   ├── feature_extractor/      #   特征抽取：富化记忆/产实体 ↔ query 特征做图召回
    │   ├── llm/                    #   LLM 调用（vLLM/OpenAI 兼容）：提取/摘要/升华 ↔ 改写/合成 ↔ 消解
    │   ├── normalizer/             #   模态规约：接入写入 ↔ 重建路径重跑同一规约器（投影可复现）
    │   ├── reranker/               #   重排：检索融合后精排 ↔ 写入流水线相似去重排序
    │   └── audit/                  #   AuditLogger 审计记录（横切组件，非模型插件）
    │
    ├── api/                        # B 记忆接口层（§6）：统一 Core API（形态无关）
    │   └── memory_api.py           #   MemoryAPI：控制层薄封装 + 鉴权/审计执行点（PEP）；每方法收 scope(target)+identity；
    │                               #   数据面委托 MemoryEngine、管理面查询（job/inspect/trace/audit/grant）直达控制算子；
    │                               #   write 同步/异步双形态；重导出调用所需类型（调用层只 import 本包）
    │
    ├── ingest/                     # A 数据接入层（§5/§5.1）：规约 + 转换，不落盘
    │   ├── base.py                 #   IngestOperator 算子契约（含「规约投影」白话说明）
    │   ├── source.py               #   Source 信息源连接器：fetch → RawPayload
    │   └── ingestor.py             #   Ingestor 接入编排：记资产引用 + 规约投影 → MemoryUnit
    │
    ├── construction/               # E 记忆构建层（§4/§9.1/§9.2/§9.3）：负责落盘 + 建索引（无编排 service）
    │   ├── base.py                 #   ConstructionOperator 算子契约
    │   ├── extractor.py            #   信息提取：原始 unit → 低抽象派生 unit
    │   ├── abstractor.py           #   抽象与精炼/升华：→ 高抽象 unit（provenance 记血缘）
    │   ├── associator.py           #   关联分析：→ Relation（供图索引）
    │   ├── classifier.py           #   多维分类：tier / 主题 / 重要度
    │   ├── layer_annotator.py      #   当前：同 unit L0/L1 内容层标注
    │   ├── index_builder.py        #   多形式索引构建：build / update / remove / rebuild（scope 显式传给 Store）
    │   ├── evolver.py              #   自演进：EvolveMode（extract/associate/consolidate/forget/hierarchy；索引维护非演进模式）
    │   ├── hierarchy_composer.py   #   [planned] 构建算子：通用父子树创建/重建、replace_in_span（由 evolver 在 HIERARCHY 模式调用）
    │
    ├── retrieval/                  # D 记忆检索层（§8）：查询解析→Storage pipeline→Reranker→阈值→披露
    │   ├── base.py                 #   RetrievalOperator 算子契约
    │   ├── types.py                #   RetrievalQuery / RetrievalResult / RetrievedItem / 轨迹；重导出共享候选类型
    │   ├── query_parser.py         #   查询理解：去噪 / 改写 / 分词 / 实体 / 向量化 / 时间约束解析
    │   ├── recaller.py             #   单路召回接口：RecallChannel 是逻辑路，物理 Store 映射由 Storage 装配
    │   ├── fuser.py                #   物化候选融合（分层 MaxP + 跨通道融合；不含 Reranker）
    │   ├── discloser.py            #   渐进式披露：disclose(query, …) L0 摘要 → L1 片段 → L2 全文
    │   ├── retriever.py            #   检索入口接口：retrieve(scope, query)
    │   ├── hierarchy_expander.py   #   [planned] Retriever 内部算子：expand_depth>0 时单 kind 展开（共用 max_tokens）
    │   └── retriever_impl/         #   PipelineRetriever：按 Storage 首选值编排三条 pipeline
    │
    ├── storage/                    # F 记忆存储层（§10）：统一 Storage 门面 + 六类标准 Store
    │   ├── storage.py              #   Storage：MemoryUnit 领域操作、能力发现、端口与检索适配入口
    │   ├── security.py             #   StorageSecurity 通用授权 + StoreSecurity 数据保护边界
    │   ├── storage_impl/           #   CompositeStorage 默认组合实现
    │   ├── base.py                 #   BaseStore（storeType/health/security）+ StoreType 枚举
    │   ├── types.py                #   Storage/Store 数据类型（scope 独立入参 + filters=FilterExpr）
    │   ├── kv.py                   #   KVStore（+exists）
    │   ├── fulltext.py             #   FulltextStore 全文倒排（+search）
    │   ├── vector.py               #   VectorStore 向量 ANN（+search）
    │   ├── graph.py                #   GraphStore 属性图（节点/边一次调用，+search 遍历）
    │   ├── fusion.py               #   FusionStore 向量·倒排·正排融合（get 即正排，+search）
    │   └── fs.py                   #   FSStore 本地文件系统：原模态资产（insert 返回 ref，+stat）
    │
    └── control/                    # C 控制层：引擎编排 + 管理面（生命周期·治理·权限·调度·策略）
        ├── base.py                 #   ControlOperator 算子契约
        ├── types.py                #   Action / Grant / Channel（hot·background）/ JobInfo /
        │                           #   MemoryPatch·UpdateMode（supersede·overwrite）/ DeleteSelector·DeleteMode（forget·archive·downweight·purge）
        ├── engine.py               #   MemoryEngine 记忆引擎：§6 各语义的编排中枢（api 数据面委托于此；get/update 收 scope；
        │                           #   写链路仅异步 async write，返回插入的 MemoryUnit 列表）
        ├── lifecycle.py            #   生命周期：transition / sweep（非破坏式标记）
        ├── governance.py           #   治理：inspect 检视 / trace 血缘回溯 / audit 审计查询（目标含树结构一致性校验）
        ├── permission.py           #   权限：grant / revoke / check（跨 scope 显式授权）
        ├── scheduler.py            #   演进调度：submit / status / cancel（双通道驱动构建层）
        └── policy.py               #   运行时可变策略（§13.4 admin 落点）
```

**目录 ↔ 架构层映射**


| 目录                                  | 对应架构层 / 章节            |
| ----------------------------------- | -------------------- |
| `bootstrap/`、`agent_plugin/`        | A 调用层（§5）：内核的薄封装（core 共享 + CLI/SDK/HTTP/MCP surface）与 Agent 插件接入 |
| `jiuwen_memory/ingest/`             | A 数据接入（§5/§5.1）：信息源接入 + 模态规约 + 转换为 MemoryUnit（**不落盘**） |
| `jiuwen_memory/api/`                | B 记忆接口层（§6）：`MemoryAPI` 统一 Core API，`MemoryEngine` 的薄封装 |
| `jiuwen_memory/control/`            | C 控制层：记忆引擎编排（§6 语义的执行中枢）/ 生命周期（§3.1）/ 治理审计（§12）/ scope 权限（§3.2）/ 演进调度（§9.3）/ 运行时策略（§13.4） |
| `jiuwen_memory/retrieval/`          | D 记忆检索层（§8）：查询理解 + Storage pipeline 选择 + Fuser + Reranker + 相关性阈值 + 渐进披露 + 轨迹/错误 |
| `jiuwen_memory/construction/`       | E 记忆构建层 / 分层记忆结构（§4/§9.1/§9.2/§9.3）：**负责 MemoryUnit 落盘**与多形式索引构建、自演进 |
| `jiuwen_memory/storage/`            | F 记忆存储层（§10）：统一 Storage 领域契约、能力/安全/检索适配，以及六种 Store（kv/fulltext/vector/graph/fusion/fs） |
| `jiuwen_memory/common/`             | 跨层共享：能力插件（Plugin：tokenizer/chunker/embedder/feature_extractor/llm/normalizer/reranker）+ 通用结构体（type_def，含 §3 数据模型）+ 横切组件（audit） |
| `jiuwen_memory/config/`             | 配置（§13）：装配合并（YAML/defaults）+ 可插拔 `ConfigSource`（晚绑定六类配置）；少量策略键仍归 `jiuwen_memory/control/policy` |
| `evaluation/`                       | 测评（对接 VISION §7：benchmark / metrics / scripts / smoke_test） |
| `deploy/`、`docs/`、`examples/`      | 部署物料 / 文档 / 示例      |


- **三类基础契约**：接口代码落地为「算子 + 插件 + 存储」三类契约——各层算子、共享能力插件，以及存储层的统一 `Storage` 与底层 `BaseStore`。`Storage` 以 capability 和标准端口描述组合能力，`BaseStore` 继续以 `storeType()` 和 `health()` 描述单一后端。
- **兼容报告单独归档**：跨层 legacy 兼容（例如 `rust/cc_memory` 的 `MemoryIngestor`/`MemoryRetriever`、`memdir`、`retained_eval`）不塞进单层接口；统一归 `docs/features/construction/F04-cc-memory-compat.md`，再映射回 `jiuwen_memory/api` / `jiuwen_memory/retrieval` / `evaluation` / `agent_plugin`。
- **写入边界**：`ingest` 只做规约与转换（RawPayload → MemoryUnit），**不落盘**；`construction` 负责把 MemoryUnit 写入真源、在其上挖掘分层记忆并构建索引。构建层**没有编排 service**，六个算子（extractor/abstractor/associator/classifier/index_builder/evolver）由上层/控制层驱动。
- **索引「构建」与「持久化」分离**：`jiuwen_memory/construction/index_builder` 负责生成/维护索引投影，`jiuwen_memory/storage` 负责真源与索引的持久化。MemoryUnit 优先走 `Storage.add/update/delete/get/list`，索引投影走所需标准端口；底层 Store 的 CRUD 仍为 `insert/delete/update/get`。所有 Storage/Store 操作显式携带 scope 并由存储层原生隔离。
- **共享插件保证两侧一致**：分词/切分/向量化/特征抽取/LLM/规约/重排抽到 `jiuwen_memory/common`，构建侧与检索侧（以及重建/演进路径）注入**同一实现**——同词表、同向量空间、同切分规则、同规约器，是「派生可重建」与召回对齐的前提。
- **依赖方向**：`jiuwen_memory/common` 承载跨层数据契约与插件；`jiuwen_memory/storage` 只依赖 common，不反向依赖 Retrieval。Retrieval 依赖统一 Storage 和 common，QueryParser/Fuser 等算法仍归 Retrieval。Construction/Control 的目标依赖也是 Storage 契约，但首版仍有直接 Store 依赖待迁移；API 继续作为 control/retrieval/construction 的薄封装。
- **鉴权/隔离/异常的统一落点**：① `MemoryAPI` 是公开接口 PEP，分离 `identity` 与 target `scope`；② `StorageSecurity` 是可插拔的数据面授权边界，默认 allow-all，各 Store Security 负责后端数据保护；③ scope 作为 Storage/Store 专用入参做原生隔离，`FilterExpr` 不承载 scope；④ `common/errors` 提供跨层异常契约；⑤版本链走 `supersedes`，演进血缘走 `provenance`。
- **一个内核，多形态接入**：`bootstrap/*` 与 `agent_plugin/*` 依赖内核、仅做协议/参数转换后调用 `jiuwen_memory/api`，不含业务逻辑。`bootstrap/core` 是各 surface 共享的应用核（内核装配 + 共享 `dispatch` + profile/配置加载）；其上 `http_server`（HTTP/REST）与 `mcp_server`（MCP）作为独立服务对外提供、`sdk` 作为库嵌入、`cli` 作为命令行——四个 surface 彼此解耦，共用同一 `core` 与 `jiuwen_memory/api`。
- **端/云/混合**靠 `jiuwen_memory/config` 的部署 profile 装配不同后端组合（端侧 SQLite+轻向量，云侧 PG+Milvus+Neo4j），逻辑模型不变。

> 当前状态：主要接口与默认实现已存在。本轮统一 Storage 首版已完成 Retriever 接入；
> Construction/Control 迁移和一体化 `Storage` 实现仍待后续迭代。实际签名与行为以
> `jiuwen_memory/`、`docs/specs/` 和对应 features 文档为准。标记 `[planned]` 的 hierarchy 条目仅表示目标落点，不声称代码已存在。

---

## 17. 开放问题（Parked for Design Phase）

> 以下问题在 VISION/架构层面**暂不下结论**，留待立项设计阶段细化：

1. **演进产物与真源的关系**：原始记忆数据是否 append-only 不可变？演进产物（升华出的语义记忆、消解后的事实）是单独成层，还是回写真源？这直接影响「真源唯一且派生可重建」的边界与重建语义。
2. **双真源是否可在同一实例内共存**：是「部署时二选一」，还是「同一实例不同 scope/namespace 用不同真源」？后者更强但一致性/同步更复杂。
3. **端云同步的一致性级别**：最终一致 vs 更强一致；冲突合并的具体策略与冲突可视化。
4. **演进所用 LLM 的端侧降级**：端侧无强模型时，抽取/升华如何降级（规则/小模型/延迟到云）。
5. **多模态规约的保真边界**：§5.1 已定（保留原模态资产 + 文本投影、检索走投影），但仍待细化——投影的保真度/成本权衡、是否保留多份不同粒度投影、原模态资产的生命周期与遗忘策略。
6. **审计后端缺口**：`common/audit` 已定义横切的 `AuditLogger` 记录接口与 `AuditEvent` 结构，但 `jiuwen_memory/storage` 的六种 Store 中没有审计持久化后端——是新增 `AuditStore`（append-only 写入 + 按条件查询，供 `Governor.audit()` 消费），还是复用 KV/Fulltext 存储审计流水？涉及合规保留期限与查询能力的权衡。
7. **数值元数据的类型**：`FilterClause` 已支持数值/时间范围算子（gt/lt 等），但 `MemoryUnit.metadata` 仍为 `dict[str,str]`、重要度/置信度按字符串存放。范围比较与 `LifecycleManager.sweep` 的「低价值」判断要可靠生效，需让数值类元数据以可比较类型入库，或把 `importance`/`confidence` 提升为 `MemoryUnit` 的显式数值字段；本轮暂不改，留待实现/调优阶段定。
8. **多父与独立边存储阈值**：同一 kind 多父、边属性或跨 kind 组合查询增长到什么规模时，内嵌 `HierarchyRef` 应迁移为独立边存储？需要以查询频率、双写成本和一致性故障率量化。
9. **父子双向更新并发语义**：并发 write、Expand、剪边与 `replace_in_span` 下，`parent_id`/`child_ids` 如何原子更新、失败回滚和修复，需结合具体后端验证。
10. **`HierarchyRole.PROFILE` 与 TIME 树的关系**：profile 级记忆是独立 `MemoryUnit`（常建议 `MemoryTier.CORE`），**不**通过 TIME 树的 `parent_id`/`child_ids` 挂接 snapshot/time_span/scene/event；画像检索走既有召回/标签/tier，不以 TIME 结构边表达。若未来用 TOPIC/CUSTOM kind 组织 profile，须单独立项，不得把 TIME 节点挂为 profile 的结构子。

> 注：原「召回通道 ↔ 存储后端一一对应」一项已结论——`RecallChannel` 是逻辑召回路，到物理 Store 的映射由检索层装配内部决定（非 1:1，详见 §8）。

---

## 18. 后续（Next）

- 本架构与 [VISION](./vision.md) 一致，作为 `/opsx-propose` 立项输入。
- 建议的首批 change 切分：① 记忆接口层 + 数据模型；② 记忆存储层/真源抽象（先文档 + SQLite）；③ 混合检索引擎；④ 自演进（写入流水线优先）；⑤ 调用与数据接入层（SDK Python + CLI + MCP 优先）；⑥ 配置体系与场景 Preset（§13，贯穿各 change）；⑦ 端云同步（后置）。

> 本文为设计阶段架构，不含实现代码与排期；具体技术选型与接口签名以立项后的 design/spec 为准。
