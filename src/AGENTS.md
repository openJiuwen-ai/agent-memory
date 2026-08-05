# src/ — agent-memory 内核

记忆系统的核心实现。本文件描述核心代码内部结构与数据流。各模块职责边界由 `docs/specs/` 定义，各模块本地规约由各层子模块下的 `AGENTS.md` 定义。

**每个子模块的 `AGENTS.md` 文件开头都链接了对应的 spec 规约文档**，便于快速跳转查看详细接口规范。


## 模块地图

```
src/
├── api/            # 接口层：统一 Core API（write/recall/get/update/delete/evolve/admin），形态无关
├── common/         # 跨层共享插件、security/（认证/资源保护/密码学）、审计 + type_def/
├── config/         # 配置加载与校验（待实现）
├── construction/   # 构建层：落盘 + 多形式索引构建 + 自演进闭环
├── control/        # 编排层：MemoryEngine 跨层编排中枢 + Scheduler/Permission/Policy/Governance/Space
├── ingest/         # 接入层：多模态 → 文本投影 + MemoryUnit，不落盘
├── retrieval/      # 检索层：scope 过滤 → 多路召回 → 融合重排 → 渐进式披露
└── storage/        # 存储层：统一 CRUD + search，scope 原生隔离（vector/graph/fulltext/kv/fs/fusion）
```

## 数据流

```
外部调用（SDK/CLI/MCP/HTTP）
       ↓
  api/MemoryAPI ─── 鉴权 + 参数装配
       ↓ 委托
  control/MemoryEngine ─── 跨层编排中枢
       │
       ├─ write ──→ ingest/Ingestor（规约）→ construction/（落盘 + 索引）→ storage/*Store
       ├─ recall ─→ retrieval/Retriever → storage/*Store.search
       ├─ evolve ─→ control/Scheduler → construction/Evolver → storage/*Store
       └─ get/update/delete ──→ storage/*Store（点读 + 非破坏式修正）
```

## 各层简述

### api/ — 接口层

`MemoryAPI` 是控制层的薄封装。所有接入形态最终映射到本接口，调用层只依赖本包即可触达全部能力。鉴权与入口审计在本层执行，编排逻辑不在这里。

### ingest/ — 接入层

承接多模态信息源，保留原模态资产引用（`MemoryUnit.assets`），规约出可治理文本投影（`content`）。`Source` 连接器 + `Ingestor` 编排。**不落盘**。

### construction/ — 构建层

接收接入层产出的 `MemoryUnit`，调用 `storage` 落盘，在其上构建多形式索引。六个可插拔算子：`Extractor` → `Abstractor` → `Classifier` → `Associator` → `IndexBuilder` → `Evolver`（自演进闭环）。

### retrieval/ — 检索层

五步检索链路：`QueryParser`（查询理解）→ `Recaller`（多路召回）→ `Fuser`（融合+重排）→ `Discloser`（渐进式披露 L0→L1→L2）→ `Retriever`（编排+轨迹）。

### control/ — 编排层

`MemoryEngine` 是接口层各语义的编排中枢（异步协程）。`Scheduler` 双通道调度演进任务，`PermissionManager` / `PolicyManager` / `Governor` / `SpaceManager` 管治理面。

### storage/ — 存储层

统一 CRUD 动词（insert/delete/update/get）+ 检索型 `search`。六种后端：`VectorStore` / `GraphStore` / `FulltextStore` / `KVStore` / `FSStore` / `FusionStore`。scope 隔离是存储层原生职责。

### common/ — 共享插件 + 类型

共享插件协议与横切能力均采用 `base.py + *_impl + Producer`，由 YAML 选择已注册 target。
安全能力（认证、凭据存储、资源保护、密码学）统一归 `common/security/`，其请求身份与
加密上下文类型住 `security/types.py`；`type_def/` 定义 `MemoryUnit`、`Scope`、
`AuditEvent` 等跨层类型，`errors.py` 统一异常体系。

## 架构铁律

1. **接口层不做编排**  
   `MemoryAPI` 是控制层的薄封装：做参数装配与鉴权，编排逻辑全部在 `control`。不要在接口层堆业务逻辑。

2. **接入层不落盘**  
   `Ingestor` 只负责规约（多模态 → 文本投影）和转换为 `MemoryUnit`；写入存储由构建层调用 `storage` 完成。

3. **共享插件必须同一实例**  
   构建侧与检索侧使用同一 Tokenizer / Embedder，才能保证同词表 / 同向量空间。插件由装配注入，算子不持有具体后端引用。

4. **scope 隔离是存储层的原生职责**  
   检索型 Store 的 `search` 物理约束在 `query.scope` 内，绝不跨 scope 返回。隔离必须在存储层强制，上层不依赖调用纪律。

5. **MemoryUnit 是唯一跨层数据结构**  
   接入层产出它，构建层落盘并建索引，检索层与控制层读取它。不要在层间传递原始字典或临时结构。

## 子模块 AGENTS.md 规则

`src/<subdir>/AGENTS.md` 写**可执行的规则**（不变量、禁止项、文件关系导航）与**当前实现列表**，不写意图描述。代码是 source of truth，与代码不一致时必须当场修。目的是让 AI 辅助工具读取后能快速理解当前目录下的内容。描述模块内部结构与本地约束，**不描述跨模块边界**（边界在 `docs/specs/`）。

**关键约束**：
- ✅ 记录"当前有哪些实现"（如"当前实现：keyword_recaller.py / vector_recaller.py"）
- ✅ 记录文件职责（模块地图）
- ✅ 记录行为铁律（可执行的不变量）
- ❌ 不写接口签名细节（归 `docs/specs/`）
- ❌ 不写跨模块契约（归 `docs/specs/`）

### src/\<subdir\>/AGENTS.md 骨架

```markdown
# Agent Memory <SubModule>

<1-2段概述：模块定位、核心设计思想、角色归属>

## 模块地图 / Module Map

| 文件 | 职责 |
|---|---|
| `xxx.py` | 一句话说清楚这个文件做什么、边界在哪 |
| `xxx_impl/` | 实现目录（列举当前实现） |

## 文件关系（按需）

（各文件/类之间的依赖或调用关系，可用有序列表或简单示意图）

## 行为铁律 / 关键约束

1. **规则名**：AI 可执行的不变量（禁止什么、唯一路径是什么）
2. ...

## 与其他子目录的边界（按需）

**本模块管**：
- 列出职责
✅ 可列"当前实现：xxx.py / yyy.py"

**不管**：
- 列出非职责（留给谁）

## 本地约束

（本目录内部需要遵守的约束与不变量；跨模块的规则落到 docs/specs/，不写在这里）
```

**更新规则**：以下三种情况必须在同一次 docs 提交里更新此文件：
- 目录内文件增删改名
- 公开入口（类/函数）增删改名
- 文件间调用关系发生变化

**创建条件**（满足任一即应创建）：
1. 有本模块特有的不变量，且无法从代码签名直接推断
2. 模块内超过 3 个文件，且文件间有非平凡协作（调用顺序、状态传递、互斥路径）
3. 存在 AI 会按常规思路犯错的非显而易见设计决策
4. 作为独立子系统对外暴露独立 API

**不创建条件**（全部满足则不必创建）：
- 模块行为在本文件的模块地图中已充分描述
- 文件数 ≤ 3，关系线性
- 无特殊陷阱，按代码签名和类型注解即可正确实现
- 不对外暴露独立 API，只被固定上游消费
