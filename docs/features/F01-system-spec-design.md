# F01 — 系统各层接口规约设计

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-06-23 |
| 影响范围 | jiuwen_memory/api, jiuwen_memory/control, jiuwen_memory/retrieval, jiuwen_memory/construction, jiuwen_memory/storage, jiuwen_memory/ingest, jiuwen_memory/common, docs/specs/ |
| 测试基线 | 基于现有代码接口设计完成规约编写 |
| Refs | — |

## 背景

项目已完成基础架构实现（七层架构：接入/接口/控制/检索/构建/存储/公共），各层接口代码已稳定，但缺少跨模块规约文档。规约文档缺失将导致：

1. **跨团队协作成本高**：新加入者需要阅读全部实现代码才能理解接口契约
2. **接口变更风险大**：没有显式不变量记录，重构时容易破坏跨模块约定
3. **架构一致性难保证**：scope 显式串参、identity 不下沉、读写同一套插件等原则散落在代码中

需要补全各层 spec 文档，形成完整的跨模块规约体系。

## 决策

### 规约覆盖范围

每层一个 spec 文档，覆盖：
- **S01** — 接入层（jiuwen_memory/ingest/）：Source、Ingestor、规约投影
- **S02** — 记忆接口层（jiuwen_memory/api/）：MemoryAPI 统一对外接口、鉴权执行点
- **S03** — 控制层（jiuwen_memory/control/）：已有，本次仅更新元信息
- **S04** — 检索层（jiuwen_memory/retrieval/）：QueryParser、Recaller、Fuser、Discloser、Retriever
- **S05** — 构建层（jiuwen_memory/construction/）：Extractor、Abstractor、Associator、Classifier、IndexBuilder、Evolver
- **S06** — 存储层（jiuwen_memory/storage/）：KVStore、FulltextStore、VectorStore、GraphStore、FusionStore、FSStore
- **S07** — 公共组件层（jiuwen_memory/common/）：Embedder、Chunker、Tokenizer、Normalizer、FeatureExtractor、LLM、Reranker、AuditLogger + 核心数据类型

### 文档结构

遵循 docs/AGENTS.md 定义的 spec 骨架：
```markdown
# <层名> — <英文名>

## 元信息
## 范围 / 边界
## 不变量
## 接口契约
## 数据结构
## 实现注册机制（如有）
## 与其它 spec 的关系
```

### 关键设计原则记录

在各层 spec 中显式记录架构一致性原则：

1. **scope 显式串参**（S02/S03/S04/S06）
   - scope 作为显式第一入参贯穿全链路
   - 不随查询对象携带、不混进 filters
   - 存储层据此做原生隔离

2. **identity 不下沉**（S02/S03）
   - 鉴权在 MemoryAPI 层执行
   - 下游只传已鉴权的 target scope

3. **读写同一套插件**（S04/S05/S07）
   - Embedder/Tokenizer/FeatureExtractor 必须双侧同一
   - 保证同词表/同向量空间

4. **接口与实现严格分离**（全层）
   - 顶层 .py 纯抽象，不 import *_impl/
   - 实现通过 Producer 工厂自注册

5. **索引可重建**（S05/S06）
   - 真源唯一（MemoryUnit 序列化存 KVStore）
   - 索引派生（向量/关键词/图/文档全部可从真源重建）

### 与代码实现对齐

所有 spec 严格对齐当前代码：
- 接口签名与 jiuwen_memory/ 下实际方法一致
- 数据结构与 common/type_def/ 一致
- 枚举与 **/types.py 一致
- 当前实现列举实际存在的 *_impl/ 文件

### 辅助文档

- **README.md**（specs 索引）：文档列表、快速导航（按职责/功能分类）、关键概念横向对照表
- **FEATURES.md**（本文档的索引摘要）：记录 F01/F00 等功能特性的简要信息

## 拒绝的方案

### 方案 A：等价复制代码注释

**描述**：直接从代码 docstring 导出为 spec 文档。

**拒绝原因**：
- spec 需要记录跨模块不变量（如 scope 不下沉），单个函数 docstring 无法表达
- spec 需要显式声明"不管什么"，代码注释只写"管什么"
- spec 是契约（长期稳定），docstring 是实现提示（可能随重构变化）

### 方案 B：单一 spec 文档涵盖全部层

**描述**：写一个巨型 SYSTEM_SPEC.md 包含所有层的规约。

**拒绝原因**：
- 文档过大（预估 5000+ 行），难以导航与维护
- 各层独立演进时无法独立修订
- 不符合 docs/AGENTS.md 约定（按模块拆分 spec）

### 方案 C：先写 spec 再对齐代码

**描述**：从 architecture.md 设计推导 spec，再检查代码是否对齐。

**拒绝原因**：
- 当前代码已稳定实现，存在 architecture.md 未覆盖的实现细节（如 MemoryUnit V2 结构）
- 会产生"spec 说有但代码没有"的不一致
- 本次目标是记录现状（描述当前），不是设计未来

## 验证

### 一致性检查

- [x] 接口签名与 jiuwen_memory/ 下实际代码一致（逐方法核对）
- [x] 数据结构与 `common/type_def/` 一致
- [x] 枚举与 `**/types.py` 一致
- [x] 实现注册机制与 `*_impl/` 目录结构一致
- [x] Producer 工厂与 `*/base.py` 中声明一致
- [x] 不变量与 architecture.md 核心信条一致

### 文档完整性

- [x] S01-S07 全部完成
- [x] 每个 spec 包含完整骨架（元信息/范围/不变量/接口/数据结构/关系）
- [x] 元信息中"关联变更记录"指向本 feature 文档
- [x] README.md 索引齐全
- [x] 关键概念横向对照表涵盖 scope/identity/MemoryUnit 等核心抽象

### 覆盖率

| 层 | 算子/Store 数 | spec 覆盖 |
|----|--------------|----------|
| S01 接入 | 2（Source/Ingestor） | ✅ |
| S02 接口 | 1（MemoryAPI） | ✅ |
| S03 控制 | 6（Engine/Lifecycle/Governor/Permission/Scheduler/Policy） | ✅ |
| S04 检索 | 5（QueryParser/Recaller/Fuser/Discloser/Retriever） | ✅ |
| S05 构建 | 6（Extractor/Abstractor/Associator/Classifier/IndexBuilder/Evolver） | ✅ |
| S06 存储 | 6（KV/Fulltext/Vector/Graph/Fusion/FS） | ✅ |
| S07 公共 | 8（Embedder/Chunker/Tokenizer/Normalizer/FeatureExtractor/LLM/Reranker/AuditLogger） | ✅ |

## 已知遗留

### 文档层面

1. **architecture.md 结构更新**：当前 architecture.md 中 MemoryUnit 描述仍是 V1 扁平结构（content/assets/source），应补充 V2 分段结构（segments）的说明。

2. **端云协同 spec 缺失**：architecture.md §11 描述了端/云/端云协同部署形态，但当前无对应 spec。待部署形态实现时补充。

3. **S03 中 actor 术语混用**：
   - `PermissionManager.check(actor, target, action)` 使用 `actor` 是正确的（语义是"执行者"）
   - `AuditEvent.actor` 字段也是正确的
   - `Governor.audit` 说明中的"按 actor/action/layer 过滤"是指过滤条件，不是参数名
   - 不算错误，但可能造成与 `identity` 混淆——已在本文档"背景"中说明两者关系

4. **示例代码片段**：当前 spec 以接口契约为主，未包含使用示例。待后续补充典型调用场景的代码示例。

### 实现层面（不在本 feature 范围，记录供未来参考）

1. **审计存储持久化**：当前 AuditLogger 只有 in_memory 实现，生产环境需要持久化实现（如写文件/数据库）。

2. **PolicyManager 策略持久化**：当前策略可能仅在内存，重启丢失。需要持久化机制。

3. **LifecycleManager.sweep 调度**：sweep 扫描到期记忆的触发机制未在 spec 中体现（是定时任务？还是 write 触发？）。

4. **FusionStore 后端缺失**：当前只有 memory_fusion 实现，缺少生产级后端（如 Milvus + 倒排）。

5. **GraphStore 图召回 seed_ids**：当前 spec 说明"匹配语义由后端定义"，但各后端实现可能不一致，需要统一约定或在 spec 中显式声明允许差异。
