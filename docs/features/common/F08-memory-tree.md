# F08 — MemoryUnit 树结构

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-15 |
| 影响范围 | jiuwen_memory/api/、jiuwen_memory/common/、jiuwen_memory/construction/、jiuwen_memory/control/、jiuwen_memory/ingest/、jiuwen_memory/retrieval/、jiuwen_memory/storage/；docs/specs/S01–S07；关联 [`F05-construction-spec-multimodal-design`](../construction/F05-construction-spec-multimodal-design.md) |
| 测试基线 | 目标设计与 specs 已同步完成，待设计评审；树结构代码尚未实现，无 pytest 结果 |
| Refs | — |

## 背景

现有记忆模型已经覆盖三轴，彼此独立、互不推导：

1. **同 unit 披露轴**：`ContentLayers` 与 `DisclosureLevel` 决定一条
   `MemoryUnit` 以 L0 概要、L1 片段还是 L2 全文进入上下文。L0/L1/L2 只表示
   same-unit compression，不表示节点之间的关系。
2. **多模态构建轴**：多模态构建（F05）对**一条原始媒体源**（首期视频）产出
   CLM/ELM 等多条不同概括粒度的 `MemoryUnit`，用 `metadata.memory_level` +
   `provenance` 表达单媒体源构建粒度，不表示跨源的结构包含。
3. **认知抽象轴**：`MemoryTier` 与既有演进模式区分工作记忆、情景、语义、
   程序性、核心与归档等认知角色。

以上三轴仍不能单独回答“一段时间窗口内、跨多条原始源（文本/视频/图片…）的结构
包含与下钻”。因此本特性引入第四轴：

4. **树结构轴**：由 `HierarchyRef` 表达跨 `MemoryUnit` 的包含关系，
   以父节点作为可检索概要，以子节点作为可按需展开的证据。

四轴可在同一节点上共存，且互不推导：

| 轴 | 载体 | 作用域                   |
|---|---|-----------------------|
| 同 unit 披露 | `ContentLayers` | 单 unit                |
| 多模态构建 | CLM/ELM metadata + provenance | 单媒体源 → 多 unit（见 F05）  |
| 认知抽象 | `MemoryTier` | 单 unit 认知角色，unit 可以演进 |
| 树结构 | `HierarchyRef`（本文，首期 TIME） | 跨 unit / 跨源           |

## 架构裁决：树结构轴与其他三轴的边界

树结构轴与既有三轴正交。任意一轴的值都不能推导另外一轴；同一节点可同时携带
四轴信息。

### 与同 unit 披露轴

1. `ContentLayers` / `DisclosureLevel` 只描述**同一** `MemoryUnit` 的压缩披露，
   不表达跨 unit 的父子包含。
2. 父节点与子节点各自可以有独立的 L0/L1/L2；树展开选择的是**哪个节点**进入
   上下文，披露级别选择的是该节点**以何种压缩度**呈现。
3. **禁止**用 L0/L1/L2 或把子节点正文塞进父节点 `layers` 来模拟树结构。

### 与多模态构建轴

1. **禁止**用 `HierarchyRef` 表达同视频内 ELM⊃CLM——只用 F05 的 provenance/metadata。
2. TIME 建树的叶可以是文本直写 unit，也可以是 F05 产出的 CLM/ELM（或其它模态记忆）。
3. **建议叶粒度**：默认以 CLM（及文本叶）作为细粒度权威叶；ELM 可作为并行候选，
   首期**不要**自动把 ELM 写成 TIME 父节点（父由 `HierarchyComposer` 生成）。
4. `evolve(HIERARCHY)` **不**调用、不替代视频理解流水线；缺 F05 时对视频源可降级或跳过。
5. 未来若将单视频 CLM/ELM 升为 `HierarchyKind.MEDIA`，单独立项 RFC，不在首期混用。

### 与认知抽象轴

1. `MemoryTier` 表示认知角色（工作/情景/语义等），不是树位；`HierarchyRef.role`
   （如 snapshot/scene/event）表示结构树位，不等于 tier。
2. 父子节点可各自选择不同 tier；**禁止**用 `MemoryTier` 枚举或 evolve 模式映射
   代替树深、父子边或 `HierarchyKind`。
3. 既有非 `HIERARCHY` 演进模式不暗改 `HierarchyRef`；`evolve(HIERARCHY)` 只维护
   树结构边与派生父。

## 决策

### 1. 首期采用内嵌 `HierarchyRef`

首期把结构引用内嵌到 `MemoryUnit`，不新增独立边存储。主要原因是：

- KV 中的 `MemoryUnit` 继续作为完整真源，目标索引可从真源重建；
- 父命中后的常用读取是按有序 `child_ids` 点读，首期无需额外 join；
- 缺少 `hierarchy` 的旧数据可以按“非层级节点”兼容读取；
- 可先验证单 kind 严格树、重建与展开语义，再决定是否承担多父图的复杂度。

这是一项首期边界，不是否认独立边存储的长期价值。当同一节点必须在同一种 kind
下拥有多个父节点，或边属性、跨 kind 组合查询成为主路径时，再评估迁移。

公开数据结构、序列化兼容和错误语义已落入
[S07-common.md](../../specs/S07-common.md)；本文只记录选择理由和设计约束。

### 2. `HierarchyRef` 的字段职责

`HierarchyRef` 用 `kind/role` 标识结构维度与树位，用 `parent_id/child_ids` 保存直接且
有序的双向边，用 `span_start/span_end` 表示覆盖区间，并用 `ordinal/status` 表示稳定
顺序与结构修正状态。`status` 是必填字段，默认 `HierarchyStatus.ACTIVE`，取值只允许
`ACTIVE/DISMISSED`；归档、遗忘和版本失效完全由 `LifecycleState`
管理，不进入结构状态。

TIME 节点必须声明有效 span，非 TIME 节点可选；任何已声明的 span 都必须成对、有效且
满足父覆盖直接子。首期仍坚持**同 org+space**、单 kind 严格树、双向一致、无环、稳定
顺序和叶权威；`user`/`agent`/`session` 可按 compose profile 放宽（见决策 2b），默认展开
不隐式跨 kind。具体字段类型、默认值、完整不变量与错误语义见
[S07-common.md](../../specs/S07-common.md)、[S03-control.md](../../specs/S03-control.md)
和 [S04-retrieval.md](../../specs/S04-retrieval.md)。

### 2b. 树结构边的 scope 规则（非五维全等）

父子节点之间的 scope ，不是 `Scope(org, space, user, agent, session)` 五维
全等，会挡住 TIME 的核心场景：跨多个 session 概括、乃至同租户下跨多个 user 概括。
首期的目标契约是：

| 维度 | 规则                                                        |
|---|-----------------------------------------------------------|
| `org` + `space` | **硬边界**：父子必须相同；跨 space / 跨 org 的树边一律拒绝。（与 F03 租户隔离一致） |
| `session` | **默认可跨**：同 user（或 profile 允许的主体）下连续多 session 可挂同一 TIME 树  |
| `user` / `agent` | **默认不可跨**；同 org+space 跨 user/agent 建树须 compose profile 显式开启 |

父节点通常写在 build 请求的 **tree home scope**（例如清空 `session` 的用户级归属，或
策略开启时的 space 级归属）；权威叶可仍驻留在更细的 session scope。因 id 只在完整
Scope 内唯一，跨细粒度 scope 的边必须携带 `child_scopes` / `parent_scope`（见 S07），
缺省时仍表示与持有边的 unit 完整 Scope 相同。

跨 space 的「共享记忆」继续走 F03 的 grant / shared space，**不允许**用 `HierarchyRef` 穿越
租户硬边界。

### 3. 血缘、版本与结构三分

三种引用表达不同事实，必须分离：

| 载体 | 回答的问题 | 生命周期 |
|---|---|---|
| `provenance` | 这条记忆由哪些记忆抽取、升华或合成而来？ | 随演进与可追溯性管理 |
| `supersedes` | 这个版本取代了哪个旧版本？ | 随版本链和 valid-time 管理 |
| `HierarchyRef` | 这个节点结构上包含谁、隶属于谁？ | 随建树、剪枝、重建与展开管理 |

`evolve(HIERARCHY)` 可以作为建树调度入口，但其产物关系仍只写
`HierarchyRef`。建树不意味着生成 `provenance`，结构重建不意味着
`supersedes`，沿血缘追溯也不承担树展开。

### 4. 丰富实体复用 `MemoryUnit` 槽位

不同角色不新增各自的实体表。每个节点仍是一条完整 `MemoryUnit`，领域信息按语义
进入既有或新增槽位：

| 信息               | 槽位 |
|------------------|---|
| 节点身份             | `MemoryUnit.id` |
| 正文、叙述            | `segments` 及其 `content` 合并视图 |
| 同 unit 压缩表示      | `ContentLayers.l0/l1`；L2 仍是 `MemoryUnit.content` |
| 认知角色             | `MemoryUnit.tier` |
| 结构身份、父子边、区间、顺序、状态 | `HierarchyRef` |
| 叶事件时间和双时间语义      | `MemoryUnit.temporal` |
| 设备、应用、标题、路径、模板、置信度、价值分等领域字段 | `metadata` |
| 主题分类             | `tags` |
| 原模态证据            | `segments[].assets` |
| 抽取或合成来源          | `provenance`，仅用于真实演进血缘 |
| 版本替换             | `supersedes` |

这使丰富角色可以共享存储、索引、生命周期和披露能力，又不把领域字段提升为所有
kind 都必须理解的核心类型。

首期推荐统一使用小写 snake_case metadata 键，TIME 叶可使用 `device_id`、`app`、
`window_title`，`event` 父可使用 `event_type`、`template_id`、`confidence`，
DIRECTORY 节点可使用 `path`。这些键是领域投影，不是 `MemoryUnit` 一级字段；
construction 可以提供 pack/unpack 辅助，但不得让 common 类型依赖某一种 kind。

### 5. `HierarchyRole` 与 `MemoryTier` 只提供指导映射

role 表示树位，tier 表示认知角色，两者不做硬编码等价。默认建议如下：

| role | 建议 tier | 理由                               |
|---|---|----------------------------------|
| `snapshot` | `EPISODIC` | 权威事件叶                            |
| `time_span` | `EPISODIC` | 连续活动片段                           |
| `scene` | `SEMANTIC` | 场景回顾仍以情节摘要或核心脉络为主                |
| `event` | `PROCEDURAL` | 表达任务流程或可复用模式                     |
| `profile` | `CORE` | 稳定画像；**独立 MemoryUnit**，不进入 TIME 主树，也**不**用 `parent_id`/`child_ids` 与 TIME 节点互挂 |
| `root` | `SEMANTIC` 或 `CORE` | 结构入口                             |
| `node` | 由内容决定 | 通用 kind 不预设认知角色                  |

构建器可以按领域策略覆盖建议值，但不得用 tier 代替 role。

### 6. 写叶与构建父节点分离

普通 `write` 继续负责写入权威叶或调用方明确提供的单节点，不同步构建整棵树。
叶可以没有 `HierarchyRef`，也可以显式标记为某个 kind 的叶角色。

父节点及父子边由显式或后台的 `evolve(HIERARCHY)` 构建。构建过程读取目标范围内
的权威叶，生成父节点正文与可选 `ContentLayers`，写入有序子引用，并回写子节点
的直接父引用。默认写路径保持轻量，也让父节点能够按区间重新推导。

公开的 write/evolve 参数、调度与返回结构分别由
[S02-memory-api.md](../../specs/S02-memory-api.md)、
[S03-control.md](../../specs/S03-control.md) 和
[S05-construction.md](../../specs/S05-construction.md) 定义；本文不复制目标签名。

### 7. 演进模式不隐式混写 hierarchy

普通 `write` 与 `EXTRACT/ASSOCIATE/CONSOLIDATE` 不因产生 unit、血缘或图关系而自动
挂树；`HIERARCHY` 才负责创建或重建父节点和双向直接边。`FORGET` 必须同时断开遗忘
节点的直接父边和全部直接子边：从父 `child_ids` 移除该节点、清空该节点
`parent_id`，并清空其 `child_ids` 及所有直接子的对应 `parent_id`；这些节点保留且
不发生级联删除。逐模式字段行为和删除顺序以
[S05-construction.md](../../specs/S05-construction.md) 与
[S03-control.md](../../specs/S03-control.md) 为准。

### 8. `replace_in_span` 以叶权威为边界

TIME 父节点会因切分策略、修正或新增叶而重算。`replace_in_span` 只替换与目标区间
相交的派生父层及其索引，完整断旧边并一致挂新边，所有权威叶及其内容保持不变。
区间边界不得留下半断开的双向引用。存储仍提供通用 CRUD，具体替换步骤、失败修复与
事务边界由
[S05-construction.md](../../specs/S05-construction.md)、
[S03-control.md](../../specs/S03-control.md) 与
[S06-storage.md](../../specs/S06-storage.md)。

替换区间若切过一个旧父节点中部，构建器只能扩大替换集至该旧父的完整覆盖范围，
或者在任何写入前拒绝请求；不得保留“半个旧父”。断开旧边后尚未重挂的子节点只清空
`parent_id` 成为未挂接节点，仍是可检索、可再次建树的权威节点，不进入 FORGOTTEN，
也不因空父回收而被删除。

### 9. 检索支持按父侧 role 优先召回，再按需展开

层级检索分成两个阶段：

1. **父层召回**：调用方按 kind、父侧 role、区间等结构条件筛选父节点，走现有混合
   召回、融合、重排和阈值链路。`expand_depth=0` 只返回直接命中的父节点，不自动附带
   子全文；省略 role 时同 kind 下所有活动角色均可参与，不再称为“只召回父节点”。
2. **子树展开**：调用方或检索编排依据深度与预算，沿父节点有序 `child_ids` 点读
   子节点；展开默认不跨 kind。

父优先使粗粒度摘要成为稳定入口，同时保留“先看概要、再取证据”的交互方式。
叶命中向父上卷是可选策略，默认关闭，避免单个噪声叶把整棵父树带入候选。
检索轨迹必须区分父层命中与子树展开阶段，并记录根节点、展开深度、返回节点数和预算
截断原因，使父→子的证据路径可审计。

`RetrievedItem` 保持扁平，不嵌套 `child_ids` 或树容器。调用方在同一次 `search` 中通过
非零 `expand_depth` 展开；**不另设公开 `MemoryAPI.expand`**。`rollup` 只把后代相关性
传播到目标父角色，默认不展开后代；“父命中”“分数上卷”和“内容展开”是三个可独立启用的动作。

层级过滤、展开和结果结构的精确公开契约已写入
[S02-memory-api.md](../../specs/S02-memory-api.md) 与
[S04-retrieval.md](../../specs/S04-retrieval.md)。

### 10. 展开选子与既有 `max_tokens` 共用，不另设树预算池

父子结构新增两类跨节点决策：

- **分数传播**：首期采用 MaxP，把父自身得分与相关子节点最高分合并；同一父下应有
  top-M 或阈值收敛，防止候选爆炸。其他传播算法留待后续基准验证。
- **节点准入与主披露级**：`expand_depth>0` 时，选哪些子节点及每个节点的主
  `DisclosureLevel`，与父命中一起消耗既有 `RetrievalQuery.max_tokens`（来自
  `context.extensions["max_tokens"]`），**不**另设 `expand_budget_tokens` 或独立
  `tree_budget` 控制面。

现有 Discloser 的职责仍是对**单个 unit**选择或塑形 L0/L1/L2 内容；跨节点遍历由
Retriever 内的 Expander 在 recall 编排中完成，再调用 Discloser。二者分工不同，但
token 预算是同一池。
由于 `RetrievedItem` 始终返回 abstract/overview/content 全字段，实际响应可超过该逻辑
预算；严格 wire-size 投影不在当前契约内。

### 11. TIME 是结构 kind，不是时间字段或召回通道

`HierarchyKind.TIME` 用树结构组织时间维度的多粒度记忆，典型角色顺序是：

```text
event（可选森林根）
  └─ scene
       └─ time_span
            └─ snapshot
```

TIME 的主要约束是：

- `snapshot` 通常是权威叶，事件时刻使用 `MemoryUnit.temporal.t_event`；
- 区间父节点使用 `HierarchyRef.span_start/span_end` 表示覆盖范围；
- 直接子节点按时间稳定排序；
- `profile` 属于画像组织：产出独立 `MemoryUnit`（常 `CORE`），**不**通过 TIME 的
  `parent_id`/`child_ids` 与 snapshot/time_span/scene/event 互相关联；画像检索走
  既有召回/标签/tier，不以 TIME 结构边表达；
- 高层可重建，叶不可因父层重建被清除。

`MemoryUnit.temporal` 是双时间字段，`RecallChannel.TEMPORAL` 是检索中的时间过滤
通道，二者都不等于 `HierarchyKind.TIME`。TIME 负责“谁在时间结构上包含谁”，
时间字段负责“何时发生、摄入、生效或失效”，通道负责“如何按时间约束召回”。

### 12. 多 kind 复用协议，避免新增结构轴

同一套父子协议还可表达：

- `HierarchyKind.DIRECTORY`：`root`/`node` 组成路径浏览树；
- `HierarchyKind.TOPIC`：主题根与主题节点组织相关记忆；
- `HierarchyKind.CLUSTER`：聚类父节点包含成员节点；
- `HierarchyKind.CUSTOM`：由调用方或插件定义的包含结构。

每种 kind 可以拥有自己的构建策略和排序规则，但共享树校验、父优先召回、展开、
分数传播与预算机制。首期默认单 kind 遍历；同一节点的多 kind、多父或图关系不做
隐式合并，非包含关系继续由 GraphStore 表达。

### 13. 模块分解与实现顺序

树结构横跨七个内核模块，但每层只承担一种职责：

| 模块 | 本特性职责 | 不承担的职责 |
|---|---|---|
| `common` | 公共枚举、`HierarchyRef`、codec、无副作用树校验 | 建树和存储事务 |
| `storage` | KV 真源、索引 metadata、scope 隔离 CRUD | 解释父子业务语义或级联 |
| `construction` | `HierarchyComposer`（构建算子，由 Evolver/`evolve(HIERARCHY)` 调用）、kind pipeline、父内容生成、索引更新 | 鉴权和召回 |
| `retrieval` | 结构过滤、Retriever 内 Expander、MaxP、与 `max_tokens` 共用的展开准入、轨迹 | 建树和修复 |
| `control` | 策略闸门、任务调度、结构事务（落在既有治理/Engine）、ensure、生命周期联动 | kind 专属切分算法 |
| `api` | 参数装配、PEP、错误透传 | 数据面编排 |
| `ingest` | 把可信来源提示映射为无边叶身份 | 建父、查父或回写边 |

实现依赖顺序固定为：

```text
common → storage → construction → retrieval → control → api
                      ↑                         ↑
                    ingest --------------------┘
```

这里表示类型和能力依赖，不表示所有代码必须串行开发。construction 不得反向依赖
control 或 Scheduler；control 负责提交任务，construction 只执行构建请求。
ingest 与 api 可以在公共契约稳定后并行实现。jiuwen_memory_entry 和 jiuwen_memory_adapter 只做薄适配，
不承载内核建树算法。

结构事务的业务编排归 control：它负责 scope/kind/span 并发闸门、任务终态，以及
update/delete/FORGET/SUPERSEDE 路径。construction 的 `HierarchyComposer` 负责生成并
校验候选子树，并通过不含鉴权和 Policy 的提交端口完成 KV/索引写入。control 调度
evolve/replace 并以 `HierarchyComposeResult.complete` 判断终态；Composer 不读取运行时
Policy，也不自行提交后台任务。

### 14. 构建层采用统一 Composer 加 kind pipeline

`HierarchyComposer` 与 `Extractor`/`Abstractor` 等一样，是 `ConstructionOperator`
实现：由控制层通过 `evolve(..., mode=HIERARCHY)` → Evolver 调度调用，不自行鉴权、
不自行提交后台任务。它是跨 kind 的统一构建入口，负责请求校验、pipeline 选择、
结构校验、持久化和修复报告；kind 专属算法由可替换 pipeline 承担：

```text
HierarchyComposer
├─ TimeHierarchyPipeline
│  ├─ TimeSpanMerger
│  ├─ SceneSegmenter
│  └─ EventBuilder
├─ TopicHierarchyPipeline（后置）
├─ DirectoryHierarchyPipeline（后置）
└─ HierarchyMaintainer
```

每个 stage 接收同 kind、稳定排序、且满足决策 2b scope 规则的叶或中间节点集合，以及
构建 span 和不可变构建选项；输出候选父 `MemoryUnit` 与待应用的直接边变更。stage 不直接
鉴权、调度或提交存储事务，因而可以用内存输入做确定性单测。`HierarchyComposer` 在所有
stage 完成后统一验证整棵候选子树，再决定提交或返回错误。

`EvolveMode.HIERARCHY` 直接委托 `HierarchyComposer`，不进入 EXTRACT/CONSOLIDATE 的
Dedup 主路径。父摘要的内容去重可以作为以后独立策略加入，但不得让相似性判定改变
树的单父、区间覆盖和稳定顺序。

`HierarchyMaintainer` 处理 dismiss、剪边、空父回收和显式修复。节点正文或 metadata
修改仍走既有 update，不新增“重命名”旁路。Maintainer 可以复用 Composer 的校验与
提交器，但不重新执行内容派生算法；空父默认保留，只有明确策略才能退役，且永不级联
删除权威叶。

精确的请求、结果和算子签名由
[S05-construction.md](../../specs/S05-construction.md) 单点定义，本文只确定组件边界。

### 15. TIME 派生链及各 stage 逻辑

TIME pipeline 的输入是指定 span 内、`role=snapshot`、生命周期和结构状态均可用的权威叶。
输入先按 `span_start`、`temporal.t_event`、请求中的稳定顺序排序；重复 id、跨 org/space、
跨 kind 或区间非法在进入 stage 前拒绝。profile 未允许的跨 user/agent 同样拒绝。

```text
snapshot → TimeSpanMerger → time_span
      → SceneSegmenter → scene
      → EventBuilder → event
```

| stage | 输入 | 边界判定 | 输出内容 |
|---|---|---|---|
| `TimeSpanMerger` | 连续 snapshot | 设备/会话硬边界、配置的上下文键变化、事件间隔超过阈值时切断；其余相邻叶合并 | 一个连续活动片段，子为 snapshots |
| `SceneSegmenter` | 有序 time_spans | 明确上下文切换为硬边界；主题/任务相似度、最大持续时间和显式结束信号形成软边界 | 一个可回顾场景，子为 time_spans |
| `EventBuilder` | 有序 scenes | 按任务目标、动作序列和实体重合聚合；不得为了相似度打乱时间顺序或让 scene 多父 | 一个任务流程或可复用模式，子为 scenes |

硬边界优先于任何语义相似度；软边界的阈值和特征组合属于 compose profile，不写死在
公共类型。算法必须确定性消费已排序输入：同样的输入、profile 和模型版本应产生相同
分段顺序。LLM 可用于命名和摘要，但不能绕过硬边界或直接提交结构边。

各层字段生成遵循以下规则：

| role | span | content / layers | tier |
|---|---|---|---|
| `snapshot` | 事件点可表示为起止相同 | 保留权威内容；不由 pipeline 改写 | 通常 `EPISODIC` |
| `time_span` | 直接 snapshot 区间并集 | 连续活动摘要，保留关键应用/标题等 metadata | 通常 `EPISODIC` |
| `scene` | 直接 time_span 区间并集 | 目标、关键动作、结果和证据摘要 | 通常 `EPISODIC`，稳定抽象后可为 `SEMANTIC` |
| `event` | 直接 scene 区间并集 | 任务模式、步骤和结果；可写 `event` 领域 metadata | 通常 `PROCEDURAL` |

父 span 默认取直接子 span 的最小起点和最大终点，不得缩小到遗漏直接子。父正文先由
stage 生成 segments，再由 `LayerAnnotator` best-effort 生成 `layers.l0/l1`。子
`parent_id` 回写时不改子内容、tier、temporal、provenance 或 lifecycle。

`profile` 不进入 TIME 主链，也不得把 snapshot/time_span/scene/event 挂为 `profile` 的
结构子节点。稳定画像应作为 `MemoryTier.CORE` 的独立 unit，或进入 TOPIC 结构；它与
TIME 证据只通过 metadata 或真实演进来源弱连接。把 profile 设为 TIME 根或 TIME 父会把
无界、持续更新的画像强行变成一个时间区间父，破坏 span 和局部重建语义。

首个可交付构建切片 P1 只要求 snapshot→time_span；scene 在 P2 加入，event 在 P3
加入；pipeline 协议从一开始允许缺省后续 stage。

### 16. 字段填充、校验与持久化顺序

Composer 创建父节点时按下列顺序处理：

```text
1. stage 生成候选父的 id、scope、role、span、segments、tier 和领域 metadata
2. LayerAnnotator best-effort 生成 l0/l1；失败保留空 layers
3. 组装候选 parent_id/child_ids 和对子节点的边变更
4. 对完整候选子树校验 scope、kind、单父、无环、排序和 span 覆盖
5. 写入新父 KV，并在同一结构提交中回写子 parent_id/旧父 child_ids
6. KV 成功后 build/update 内容层索引及 hierarchy metadata
7. 返回 created/updated/replaced/repair_required/complete
```

父节点 id 必须新生成；结构派生不写 `provenance`，除非该父正文确实通过既有演进算子
由来源 unit 合成，且这条血缘在脱离层级关系后仍然成立。`metadata` 只接收该 kind
约定的领域键（见决策 4），不得覆盖 id、scope、temporal、lifecycle 或 hierarchy。

`replace_in_span` 在步骤 1 前先读取所有相交旧派生父并扩大替换边界，然后计算“旧边
断开、旧父退役、新父写入、新边挂接”的完整变更集。只有新树整体可验证时才开始写。
索引始终后于 KV；索引失败不会把索引提升为真源，但操作必须返回不完整状态并进入修复。

支持事务的 KV 后端应原子提交全部受影响 unit。不支持事务的后端采用可恢复顺序：
先持久化无活动边的新父，再按稳定顺序切换子边，最后退役旧父；任何中断返回
`complete=false` 和 `repair_required`，任务不得标记成功。修复以 KV 中可见 unit
重新计算双向边和索引，不从旧索引反推真源。

### 17. 运行时 Policy 与不可变 compose profile 分离

运行时 Policy 控制“是否执行”，compose profile 决定“如何构建”：

| 分类 | 内容 | 变更语义 |
|---|---|---|
| 运行时 Policy | 总开关、auto derive、ensure、MaxP、内部/接入形态默认展开深度、top-M | 可以治理时调整；不回写已有树 |
| compose profile | kind、leaf role、parent role 序列、stage 启用、硬边界键、阈值、模型/提示版本 | 装配期固定；变更后通过显式 rebuild 生效 |

首期所有运行时能力默认关闭：普通 add 和 search 行为不变。`auto_derive` 只在叶成功
写入且 profile 能确定有界 span 时提交 BACKGROUND 任务，不阻塞 hot path。
`ensure_on_recall` 只服务显式 kind+有界 span 的召回，并阻塞等待构建终态，避免调用方
请求“确保后召回”却拿到静默的无结构结果。

compose profile 至少定义 `leaf_role`、从近叶到远叶的 `parent_roles` 和每个 stage 的
算法配置。role 序列不放入可随时修改的 PolicyManager，避免运行中改变树形导致同一
scope 出现两套半成品结构。profile 缺失时 ensure 抛 `PolicyError`，auto derive 记录
跳过原因；两者都不得猜测默认 role 序列。

`hierarchy.expand_default_depth` 只供未显式给 depth 的内部或接入形态使用；公开
recall 的默认值始终是 `expand_depth=0`，Policy 不得隐式改写该公开默认。

Policy 键、默认值和校验由
[S03-control.md](../../specs/S03-control.md) 定义；构建请求字段由
[S05-construction.md](../../specs/S05-construction.md) 定义。

### 18. 失败、降级与并发决策

| 场景 | 决策 |
|---|---|
| 父摘要或 layers 生成失败 | 保留结构候选，父 content 使用确定性规则摘要或最低可用拼接，layers 为空；记录诊断 |
| HIERARCHY 部分写入失败 | `complete=false` 并返回逐项 `repair_required`；任务状态不得为 SUCCEEDED |
| `replace_in_span` 中断 | 不删除权威叶；根据 KV 重算未完成边，修复前不宣称替换完成 |
| expand 遇到缺子、跨 kind、环或不可见节点 | 跳过该分支、记录 issue、`complete=false`；不让一个坏分支使所有有效结果失败 |
| ensure 任务失败、取消或超时 | recall 抛 `BackendError`，不降级为普通无层级召回 |
| auto derive 提交失败 | 不回滚已成功写入的叶；记录任务和审计错误 |

同一 `scope + kind` 下存在重叠 span 的 build、replace、update、FORGET 或 PURGE 必须
串行化，或者由后端乐观版本条件检测冲突。并发 write 可以先完成叶写入；若其 span 与
正在替换区间相交，当前 replace 不能悄悄吸收未参与初始快照的叶，必须冲突重试或由
后续增量任务补建。这样保证一次构建的输入快照和结果可解释。

`HierarchyRepair` 只报告结构差异，不借用 provenance trace。修复器重读当前 KV、
重建期望双向边并重建派生索引；无法确定唯一父时停止并返回冲突，不凭 id 顺序猜测。

### 19. 分阶段落地与兼容边界

| 阶段 | 交付范围 | 进入下一阶段的条件 |
|---|---|---|
| P0 | 公共类型、codec、纯函数校验、索引 metadata | 旧数据兼容；环、跨 org/space、非法跨 user/agent、重复子和单 kind 多父被拒绝 |
| P1 | snapshot→time_span、结构提交器、`replace_in_span` | 可重复重建且叶内容零变化 |
| P2 | snapshot→time_span→scene、父侧召回、`search(..., expand_depth=1)`、MaxP | 默认 depth=0 不返回子全文；与 `max_tokens` 共用预算，轨迹可区分展开阶段 |
| P3 | event、ensure/auto derive、修复任务 | 失败状态和后台任务可观测 |
| P4 | 至少一种非 TIME kind | 复用同一校验、存储与展开协议 |
| P5 | Maintainer 修正流（dismiss/剪边/空父回收/修复）和性能优化 | 并发冲突与展开性能达到已设基线 |

每个阶段都必须满足：`hierarchy.enabled=false` 时既有 add/evolve/search 结果和错误语义
不变；没有 `hierarchy` 的历史 `_v=2` 数据无需迁移即可读取；目标接口未启用时不得改变
现有插件装配和 Store 抽象。

## ingest 接入层 （层级叶提示）

Source adapter 可以在 `RawPayload.metadata` 中提供以下保留键：

| 键 | 类型 | 语义 |
|---|---|---|
| `hierarchy_kind` | str | `time` / `topic` / `directory` / `cluster` / `custom` |
| `hierarchy_role` | str | 接入允许的叶角色：TIME 为 `snapshot`，其他 kind 为 `node`；完整枚举见 S07 |
| `hierarchy_span_start` | ISO 8601 str | 可选覆盖区间起点 |
| `hierarchy_span_end` | ISO 8601 str | 可选覆盖区间终点 |

Ingestor 只允许把一组完整且有效的提示映射到当前 unit 的叶安全字段：
`kind`、`role`、`span_start`、`span_end`。映射后的 `parent_id` 必须为空，
`child_ids` 必须为空，`status` 使用 `ACTIVE`。未提供任何保留键时，
`hierarchy` 保持默认空结构。

校验是确定性的：

1. kind/role 必须同时提供；区间必须同时提供或同时缺省。
2. 枚举值必须精确匹配，时间必须可按 ISO 8601 解析，且起点不得晚于终点。
3. `HierarchyKind.TIME` 必须提供区间；其他 kind 可省略区间。
4. 接入提示只接受叶角色：TIME 只接受 `snapshot`；DIRECTORY、TOPIC、CLUSTER、CUSTOM
   只接受 `node`。`time_span`、`scene`、`event`、`profile`、`root` 等父侧
   角色必须由构建层创建。
5. `hierarchy_parent_id`、`hierarchy_child_ids` 或其他试图建立边的保留前缀键一律以
   `ValidationError` 拒绝，不作为普通 metadata 静默保留。
6. 任一叶提示无效时拒绝该 payload 的转换，不产出半有效 `HierarchyRef`；非
   `hierarchy_` 前缀的 metadata 继续原样透传。

这些提示只是来源对当前 unit 结构身份的声明，不证明边存在。父子边只能由构建或控制
契约在持久化阶段校验并维护。

## 关键数据流

树结构专用路径不写入 architecture §14（该节只保留通用 write/recall/evolve 骨架）；
建树与按需展开细节如下。

**建树路径（`EvolveMode.HIERARCHY`）**

```text
evolve(..., mode=HIERARCHY, hierarchy_options=...)
  → Engine 策略闸门（hierarchy.enabled）
  → Evolver 委托 HierarchyComposer（ConstructionOperator）
  → 读权威叶 → stage 生成父 → LayerAnnotator(best-effort)
  → 结构校验 → KV 写父并回写直接子 parent_id/child_ids
  → IndexBuilder 投影 hierarchy metadata
  → HierarchyComposeResult / EvolveResult.hierarchy_result
```

普通 add 默认不建树；仅当 `hierarchy.auto_derive=true` 且 compose profile/span 完备时，
write 成功后向 BACKGROUND 提交等价的 HIERARCHY 任务（`replace_existing=true`）。

**读取展开路径（仍是 `search`，无公开 `expand`）**

```text
search(..., hierarchy_kind=..., hierarchy_role=?, expand_depth=N, rollup=?)
  → 既有 QueryParser → 多路召回 → Fuser → Reranker → 阈值 → top_k
  → [若 N>0] Retriever 内 Expander：沿命中父有序 child_ids 选子（共用 max_tokens）
  → Discloser（单 unit L0/L1/L2）
  → RetrievalResult（扁平 RetrievedItem + 可选轨迹）
```

`expand_depth=0` 时无 Expander 步骤；`rollup=true` 只影响父分，不自动展开子全文。

## 拒绝的方案

### 1. 用 `ContentLayers` 表示父子节点

拒绝。L0/L1/L2 是同一条 unit 的压缩表示，没有独立身份、生命周期或子证据集合。
把结构角色映射为披露级别会破坏 F01 已确立的 same-unit compression 语义。

### 2. 用 `MemoryTier` 表示 `snapshot`、`scene` 等树位

拒绝。tier 表示认知角色，同一个 role 可以因内容不同选择不同 tier；父子节点的 tier
也可以不同。绑定两者会使目录、主题和聚类结构无法复用。

### 3. 扩展 `provenance` 承载父子关系

拒绝。演进来源与结构包含有不同的遍历方向、重建时机和治理语义。混用后，血缘追溯、
版本治理、删除与展开都无法判断边的真实含义。

### 4. 首期直接采用独立边存储

拒绝作为首期默认。它能更自然地支持多父、多 kind 共节点和丰富边属性，但会增加
新 Store、双写一致性与查询 join。在严格树 MVP 尚未验证前，这些成本没有足够收益。

### 5. 每个角色建立专用实体和存储

拒绝。专用表会复制 scope、生命周期、索引、披露和序列化能力，并把通用层级协议
绑定到单一领域。丰富字段应优先复用 `MemoryUnit` 的结构化槽位。

### 6. 普通 add 同步自动建完整父树

拒绝作为默认。建树可能涉及区间读取、切分、聚类、摘要和多次写入，会扩大 hot path
时延，也使局部写入与全局重算耦合。显式或后台 `evolve(HIERARCHY)` 更符合父可重建、
叶权威的边界。

### 7. 召回父节点时自动返回整棵子树

拒绝。无界展开会放大延迟与 token 消耗，也让调用方无法先看概要再决定是否取证。
调用方显式按父侧 role 召回时，默认不展开；深度和预算必须显式控制。

### 8. 直接扩展现有 Discloser 负责整棵树预算

拒绝。Discloser 已有清晰的单 unit 披露职责。树遍历、节点选择与跨节点预算是独立
问题，应在调用 Discloser 之前完成。

## 验证

目标设计和 specs 同步已完成，代码尚未实现，设计评审待完成。不得用现有 pytest
结果替代本特性的实现验证。分阶段验收如下：

设计验收之后，实施验收依次对应决策 19 的 P0–P5：阶段 1 对应 P0，阶段 2 对应 P1，
阶段 3 对应 P2，阶段 4 对应 P3，阶段 5 对应 P4，阶段 6 对应 P5。每阶段只以本阶段
及此前已经交付的能力作为门禁。

### 阶段 0：设计验收

- [x] 四轴术语在 features、specs 与总体设计中一致，无披露级、多模态粒度、tier、时间字段和结构 kind 混用。
- [x] `HierarchyRef` 字段、兼容读取、错误语义和公开契约进入对应 specs。
- [x] 明确首期单 kind 严格树边界，以及迁移到独立边存储的触发条件。
- [ ] 完成设计评审。

### 阶段 1：模型与树一致性

- [ ] 旧数据缺少 hierarchy 时兼容读取为空结构。
- [ ] 拒绝跨 org/space、profile 未允许的跨 user/agent、重复子、环和单 kind 多父。
- [ ] 跨 session（及显式允许的跨 user）边携带可解析 `child_scopes`/`parent_scope`。
- [ ] 父子双向引用、稳定排序与区间覆盖校验通过。
- [ ] 索引可以从 KV 真源重建结构过滤 metadata。

### 阶段 2：构建与重建

- [ ] 普通 add 不自动构建父树，显式叶写入仍可工作。
- [ ] `evolve(HIERARCHY)` 能建立最小父子树，其他演进模式不暗改结构字段。
- [ ] `replace_in_span` 只替换相交派生父节点，不删除权威叶。
- [ ] 父节点内容层在落盘和建索引前按 best-effort 策略生成或安全降级。

### 阶段 3：P2 构建、检索与预算

- [ ] snapshot→time_span→scene 可构建、可重复重建，且权威叶内容零变化。
- [ ] 默认父层召回不自动包含子全文。
- [ ] 展开按顺序、深度、kind 与 scope 约束返回子树切片。
- [ ] 检索轨迹分别记录父层命中与展开阶段，并能解释展开深度和预算截断。
- [ ] MaxP 与 top-M 收敛策略有确定性测试。
- [ ] 树级预算先选节点与主 `level`，再由 Discloser 处理各节点的同 unit 披露。
- [ ] `MemoryUnit.temporal`、`RecallChannel.TEMPORAL` 和 `HierarchyKind.TIME` 的过滤行为互不替代。

### 阶段 4：调度、策略与修复

- [ ] ensure 阻塞等待任务终态；失败、取消或超时抛 `BackendError`，不静默降级。
- [ ] auto derive 不阻塞 write，提交失败不回滚已成功写入的叶。
- [ ] `complete=false` 或存在 `repair_required` 时任务为 FAILED，修复项可观测。
- [ ] compose profile 变更只通过显式重建生效，不产生两套半成品 role 序列。

### 阶段 5：多 kind 与回归

- [ ] 至少一种非 TIME kind 复用相同树校验与展开协议。
- [ ] hierarchy 关闭或字段为空时，既有 write/evolve/recall 行为保持兼容。
- [ ] 完成相关单元、集成、序列化兼容、索引重建与性能基线测试。

### 阶段 6：修正流与性能

- [ ] dismiss、剪边、空父回收和显式修复不级联删除权威叶。
- [ ] 重叠 span 并发冲突和展开性能达到已设基线。
- [ ] `profile` 不进入 TIME 主树，也不把 TIME 节点挂为结构子。

### 实现测试矩阵

| 测试层 | 必测内容 |
|---|---|
| common 单测 | codec 缺字段/未知字段；空结构；无环、单父、双向一致、span 覆盖 |
| construction 单测 | 各 TIME stage 的确定性边界；LayerAnnotator 失败降级；replace 不触叶；repair 路径 |
| control 单测 | attach/detach/SUPERSEDE/FORGET 的结构事务；ensure 终态；Policy 关闭 |
| retrieval 单测 | depth=0；稳定展开顺序；MaxP/top-M；预算截断；坏分支 issue |
| storage 单测 | hierarchy metadata 投影、区间过滤、从 KV 重建索引 |
| 集成测试 | P2 起：write snapshots → HIERARCHY → recall scene → expand time_span/snapshot → replace span |
| 回归测试 | hierarchy 关闭、空结构和旧 codec 数据下既有路径零行为变化 |

存储测试使用 in-memory Store；stage 算法使用固定 fixture 和规则 stub，不依赖在线 LLM。
模型参与的命名、摘要和语义切分质量另设离线评测，不把非确定外部调用混入单元测试。

## 已知遗留

1. **独立边存储迁移阈值**：多父、多 kind 共节点和边属性复杂度达到何种规模时迁移，
   需要以真实查询与一致性成本评估。
2. **并发一致性**：父子双向更新、`replace_in_span` 与并发 write 的后端事务能力和
   故障恢复仍需实现验证。
3. **父摘要生成质量**：不同 kind 的摘要器、失败降级与幂等性仍需实现阶段验证。
4. **树预算策略**：多父候选间的公平性与深度偏置需要基准评测。
5. **TIME 切分算法**：time_span、scene、event 的边界与置信度策略需要数据集和人工评审。
6. **多 kind 交叉查询**：首期只保证单 kind 遍历，跨 kind 联合过滤与结果合并后置。
7. **扩展分数传播**：衰减和等非 MaxP 算法需在真实浏览场景中验证后再进入契约。

F01 的同 unit 披露设计与实现历史见
[F01-memory-layer.md](F01-memory-layer.md)。
