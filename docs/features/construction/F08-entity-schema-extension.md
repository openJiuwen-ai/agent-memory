# 可选的实体 Schema 属性抽取

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-16 |
| 影响范围 | `jiuwen_memory/api/`、`jiuwen_memory/common/`、`jiuwen_memory/config/`、`jiuwen_memory/construction/`、`jiuwen_memory/control/`、`jiuwen_memory/storage/`、`docs/specs/S02-memory-api.md`、`docs/specs/S03-control.md`、`docs/specs/S05-construction.md`、`docs/specs/S06-storage.md`、`docs/specs/S08-config.md` |
| 测试基线 | `tests/unit/construction/test_entity_schema_extension.py`；source 更新与主线隔离验证见下文 |
| Refs | #208 |

## 背景

通用抽取器能生成自由文本记忆，但无法保证实体类型、属性名称和输出粒度符合业务约束。
另一方面，mem2.0 已有 `MemoryUnit.entities`、EntityLinkService 和 EntityStore，不应再建立一套
Schema 专用实体真源、实体 ID 或索引协议。

本特性的目标是增加一条显式启用的 Schema 属性抽取路径：调用方提供实体类型及候选属性，
抽取器只生成白名单内的属性事实。ADD 路径中，属性成功落盘后，其所属实体名写回相应 Source
MemoryUnit 的标准 `entities` 字段；后续反向索引继续复用既有 EntityLinkService。

#208 补充 Source 显式更新场景：原 update 直接应用 patch 并刷新索引，正文更换人物时仍
携带旧 entities。例如“陈静负责推荐算法迭代”改成“李红负责推荐算法迭代”后，记忆仍可能
挂在陈静的反向索引中，关联 property 也没有同步更正。此次依据 `208-update-design-v3.md`
扩展已有 Schema 特性，保持公共 update 协议，并协调 Source、property 和实体索引的更新。

## 决策

### 1. 功能显式启用，与默认链路隔离

Schema 复用统一 `assemble()` / `build_kernel()` 入口。`globals.schema_enabled` 在
`defaults.py` 中默认为 `false`；只有装配时显式设为 `true` 才条件注册
Schema target。调用方还必须显式选择 `entity_schema` Extractor 和
`schema_orchestrating` Evolver 才会进入 Schema 链路；默认 target 均不改变。
Schema 不提供第二套公共装配入口，所有调用方继续使用既有 `assemble()` 或
`build_kernel()`。
开关关闭却配置 Schema target 时装配 fail-closed，防止同一进程曾经注册过 Schema
target 后绕过开关。

Schema 代码放在现有模块的对应目录中。统一 assembly 只增加配置判定和条件注册点，
不修改官方 `OrchestratingEvolver`、`DynamicEvolver`、Storage 或 `MemoryUnit` 定义。

### 2. 两阶段抽取并严格使用选中属性集

ADD 路径中，Extractor 先让模型从完整 Schema 中选择本轮相关的 entity type 和 property，再只把选中的
Schema 发送给属性生成 Prompt。Normalizer 使用同一个选中 Schema 校验结果，而不是使用
完整 Catalog；模型即使额外输出完整 Schema 中未被选中的属性，也会被拒绝。
只有显式 `relevant_properties=["all"]` 表示选中该类型的全部属性；缺失、非数组或空数组
不会扩展成全量 Schema。Schema Selection 调用或根 JSON 解析失败时降级为完整 Schema；
合法的 `selected_entities=[]` 表示本轮没有可抽取类型，不再调用属性生成 LLM。

模型响应必须是单个根 JSON 对象。属性逐条校验 entity type、property name、来源
`source_unit_ids`、Scope 和显式说话者绑定。未知 entity type 不会被自动改成某个已选类型。
所有这些错误都会进入同一个纠错 Prompt，默认最多尝试三次；重试耗尽后，保留某次
响应中数量最多的合法属性并隔离其无效兄弟。如果没有任何合法属性则本轮 Schema
抽取失败；模型明确返回 `entities=[]` 则表示正常的空抽取。重试次数可以在 Extractor 配置中调整。

### 3. 每个属性生成一个标准 MemoryUnit

一个实体可以对应多个属性 MemoryUnit；每个属性 MemoryUnit 只表达一个属性事实，并使用：

- `content`：包含明确主语的属性事实文本；
- `entities=[]`：Property Unit 本身不进入实体反向索引；
- `system_metadata`：只保存 Schema 名称、版本、实体类型、实体明文和属性名；
- `source_ref` 与 `provenance`：回指支持该事实的原始消息；
- `temporal.t_event`：仅在属性具有可完整解析的日期或时间时填写。

ADD 路径中，属性 Unit 成功持久化后，Evolver 按其 `provenance` 找到对应 Source Unit，把
`schema_entity_name` 去重聚合到 Source 的 `entities`。一个 Source 支持多个实体和多个属性，
Property Unit 仍通过 `source_ref/provenance` 回指 Source。属性名只保留在 Property Unit 的
`schema_property_name` metadata 中，避免不同人物因共享属性名被实体索引错误关联（#209）。

### 4. Source-first 保证原始信息不丢失

非 procedural Schema 写入先持久化并索引原始 Source MemoryUnit，再执行 LLM 抽取：

```text
原始输入
  ├─ 持久化 Source MemoryUnit（失败则写入失败）
  └─ Schema 抽取
       ├─ 成功：直接新增属性 MemoryUnit
       └─ 失败：记录降级原因，保留 Source MemoryUnit
```

Schema 属性绕过普通相似度 Dedup，避免通用文本相似度把不同属性错误地 UPDATE 或
SUPERSEDE。ADD 路径采用 append-only：每次成功抽取的属性都作为新 MemoryUnit 写入；
显式 Source update 按下文的匹配与版本规则处理旧属性。

### 5. 复用既有实体链路

Schema Extractor 不生成自定义 `schema_entity_id`，也不维护 Schema Entity Registry。
ADD 路径中，Evolver 通过 Storage 只读加载 Source MemoryUnit，合并实体名后调用
`IndexBuilder.update(mode=ALL)`，由 IndexBuilder 统一回写 Source 本体并刷新检索索引。
IndexBuilder 看到 Source 的 `entities` 后，按现有配置调用 EntityLinkService；该服务负责
名称归一化、EntityRecord upsert 以及实体名→Source MemoryUnit 的反向链接。

因此，是否建立实体索引仍由 mem2.0 原有 `entity_enabled` 和 EntityStore 配置决定。Schema
功能本身不新增 `schema_entities` collection/index，也不要求自定义 Storage。

### 6. 功能边界

本特性包含 Schema Selection、属性抽取与校验、每属性一个 MemoryUnit、Source-first 降级、
标准实体字段接入，以及显式 Source update 的属性与索引同步。不包含：

- 自定义 Entity Identity、Entity Registry 或别名合并；
- ADD 路径的通用 Property Merge；Source update 内的属性撤回与版本处理见下文；
- relation/edge MemoryUnit、图投影和图查询；
- Schema 时间线、snapshot/range/history 查询；
- episode、higher-order property 或动态 property 生成。

### 7. Source update 显式启用，与普通更新隔离

仅在当前 pipeline 支持 Schema 更新、目标明确是 Schema Source 且传入正文发生变化时
启用更新分支。普通记忆、直接改 property、仅改标签/元数据/时间和正文相同的更新保持
原协议。实体索引开关不参与 Schema 能力判断。

API 和 Engine 在已读出的 Unit 上先做无 I/O 候选判断，避免普通 update 额外读取真源，
或在 CloudEngine 中重复复制 patch、解析路由。组件解析留在 Engine 类内，共享更新逻辑
只调用注入的 Evolver 和 IndexBuilder 解析回调，不跨类读取受保护字段。

两个 Engine 的直接 `update()` 在 Schema 分支中复用本次已加载的 Source 快照，避免
候选判断与准备阶段重复加载、读到不同版本。独立 `prepare_update()` 仍按原有 ID、Scope、
patch 协议自行加载一次，两条入口复用相同的内部准备逻辑；普通更新的读取和路由次数不变。

### 8. 完整抽取后规划变更，再鉴权和提交

更新专用抽取使用完整 Schema 和当前新正文，不附加历史上下文，也不执行常规演进
持久化。响应必须完整，并明确给出未解决撤销列表；部分合法结果、缺字段、截断、解析失败
和无法可靠表达的撤销均报错，防止把模型漏项当成业务删除。可表达的否定保存为明确否定事实。

先完整抽取、匹配并冻结动作，再逐条校验涉及记录的 UPDATE、WRITE、DELETE 权限，最后
提交。Source 的 `entities` 整体替换为新实体名，不加入 property 名；没有 property 的合法
实体也保留，合法空结果可以清空旧实体。这里只刷新当前 Source 的实体关联，保留同实体关联
的其他记忆；成功后不改原始 `/messages` 对话缓存。

### 9. Property 匹配、多源更正与版本关系

Property 在当前 Source 的同 Scope 关联集合内，按 Schema、实体类型、实体名和属性名
定位。优先匹配相同事实及事件时间，再接受同定位一旧一新的改值；多条候选存在歧义时拒绝。
换人物不凭相同属性名建立版本关系。

明确对应的多源 property 以本次更正为准，新值不继承不能支持它的其他来源；事实未变时
保留其他有效来源。新正文未提及的多源 property 仅撤回当前来源，保留其他来源支持的内容，
不推断剩余证据是否充分。

- OVERWRITE 保持对应 Source/property 的 ID，新增 property 使用新 ID，完全撤回的
  单源 property 删除本体与索引。
- SUPERSEDE 分别生成新 Source/property ID，并建立各自的 `supersedes`；旧版保留原
  provenance，关闭有效期。没有继任者的旧 property 仅关闭有效期，不伪造 `supersedes`。
- 失效区间不可倒置，旧版已有更早失效边界不延长；仅由新 Source 支持的属性不超出该
  Source 的失效边界。

Schema Source 记录所用 Schema 名和版本。已有数据缺此记录时，关联 property 的配置
也必须与所选配置一致。跨 pipeline 更新沿用旧 Schema 解释正文，目标 pipeline 必须支持
相同 Schema，索引按各记录的新旧路由迁移；正文修改不承担 Schema 迁移。

### 10. 沿用原 update 的失败处理边界

变更计划只在本次请求内使用，包含操作 ID、候选 ID、前后快照和全部动作；提交时重读输入，
发现已变化则拒绝。全部记忆写入通过 IndexBuilder 完成，不读写 `/schema_updates/`，
不保存 pending/done 状态、请求指纹或执行进度，也不生成用于防回放的 Source 修订标记。

两种模式的变更列表都包含旧 Source：OVERWRITE 对应覆盖动作，SUPERSEDE 对应旧版退役
动作。因此 Source 与 property 统一在变更列表中校验修订，不再额外读取 Source 重复校验。
正常 API Schema 提交减少一次读取，直接 Engine Schema 更新连同准备阶段共减少两次。
这项优化保留现有 API/Engine 准备路由；修订检查与写入之间仍存在并发窗口，不提供原子事务。

索引失败通过 `PartialFailureError` 暴露已完成步骤，提示再次操作前检查受影响记录。
失败可能留下部分写入，不自动回滚、不保证相同 patch 复用 ID 或断点续写。实体索引的
严格错误反馈仍通过 ContextVar 限定到 Schema 提交，并在成功或失败时恢复，普通写入
继续沿用增强索引失败只记日志的行为。API 保留操作 ID 和涉及 Unit ID 的入口审计。

这是在 v3 基础上按原 update 协议收缩的范围：取消额外的持久化恢复与完成记录，也取消
依赖这些记录的成功请求幂等识别和旧证据防回放。后续 Schema ADD 沿用原抽取流程，
可能再次从旧证据生成属性；本次更新不提供对后续演进的持续保护。

## 拒绝的方案

### 建立 Schema 专用 Entity Registry 和独立 Storage

拒绝。mem2.0 已有 `MemoryUnit.entities` 和 EntityLinkService。再维护
`schema_entity_id/schema_entity_key`、隐藏 KV 和 `schema_composite` 会形成两套实体真源，
增加装配、重建和一致性成本。

### 在 ADD 路径实现通用 Property Merge

拒绝。可靠合并依赖稳定实体身份、按实体属性召回和明确的版本生命周期。ADD 路径先保证
抽取结果不丢失并接入标准实体链路；通用 Dedup 不适合代替属性级合并，因此属性采用直接
ADD。Source update 只协调本次显式更新涉及的关联属性，不扩展为全局属性合并。

### 把 Schema 行为塞进官方 Evolver

拒绝。Schema 的 Source-first 和直接 ADD 语义与默认 Evolver 不同。独立
`schema_orchestrating` 可以保持功能 opt-in，并降低与上游演进代码的冲突。

### Source update 的替代方案

- 直接重跑完整 evolve：会重新保存消息、追加独立 property、执行上下文抽取，无法表达
  对应记录的覆盖与版本关系。
- 把 `entity_enabled` 当成 Schema 开关：会让普通记忆进入未经配置的 Schema 路径。
- 新旧 entities 合并：旧人物无法解绑，#208 仍存在。
- 多源全部拒绝或把其他来源强绑到新值：分别阻断已确认的显式修改规则、制造错误证据。
- 部分抽取成功就撤销未出现属性：不能区分业务删除与模型漏项。
- 持久化完整恢复计划与永久 done 记录：超出原 update 的失败处理边界，还需处理恢复
  冲突与记录保留周期；本次取消这项额外保障，不以单个“显式修改”标志代替防回放机制。
- 用进程锁宣称云端事务：本次未引入跨进程互斥或跨后端事务能力。

## 验证

### Schema ADD 基线

- 验证选中属性白名单、空选择语义、严格根 JSON、来源绑定和事件时间映射；
- 验证来源绑定与实体类型错误参与三次纠错，且未知类型不会被静默改型；
- 验证一个实体的多个属性生成多个 Unit，Property Unit 的 `entities` 为空，只有实体名写回 Source；
- 验证 Schema 抽取失败后 Source MemoryUnit 仍可读取和检索；
- 验证 Schema 属性不进入普通 Dedup；
- 验证标准 EntityLinkService 能从更新后的 Source Unit 建立 EntityRecord 及反向链接；
- 验证 `schema_enabled` 默认关闭，开启后统一 `build_kernel()` 能完成 Source-first
  Schema 写入。

### Source update 与非 Schema 隔离

精简回归 `tests/unit/control/test_schema_update_regression.py` 及其共享夹具
`tests/unit/control/fixtures.py` 已随分支提交。覆盖两 Engine、两更新模式下的 #208 人物更换、
共享实体保留、检索和版本关系、合法空正文撤回，以及抽取失败、实际 UPDATE-only 授权、
实体索引部分失败、无持久化恢复记录和非 Schema 更新的读取/路由次数。另覆盖 Schema
独立准备和直接 Engine 更新的读取次数，以及两种模式下 Source 被修改或删除、property
被修改时，提交在任何业务写入前报冲突。
更完整的 `test_schema_source_update.py`、`test_schema_disabled_isolation.py` 仍保留在本地。
取消旧证据过滤后，原有 Schema 测试存储不再需要额外的 `scan()` 接口，已撤回该适配。

提交用例的确定性验证为 69 passed（Schema 更新回归 42 项、原有 Schema 抽取 27 项），
无跳过项；修改的 Python 文件通过 ruff check，测试文件额外检查受保护成员访问。
复现命令：

```powershell
.venv/Scripts/python.exe -X utf8 -m pytest tests/unit/construction/test_entity_schema_extension.py tests/unit/control/test_schema_update_regression.py -o addopts=-ra -q
```

2026-09-16 消除重复读取后，construction/control/api、导入隔离、配置加载和检索日志
回归为 1023 passed、3 skipped，包含未提交的本地更新/隔离用例。跳过项为真实 Redis
双实例用例和两个本地 Engine 不适用的云端迁移用例。新增读取次数用例在修改前均失败，
修改后通过；两 Engine 普通 update 主体的 AST 与修改前一致。

以下为取消持久化恢复之前的历史验证，包含的恢复与防回放用例不再代表当前功能承诺；
含本地用例的结果不能仅检出分支复现：

- 原 construction/control/api 回归：排除本地新增测试为 851 passed、1 skipped；包含
  本地新增测试为 955 passed、3 skipped，其中更新回归 86 passed、2 skipped，主线隔离
  回归 18 passed。
- 同步上游后的 construction/control/api、导入隔离、配置加载与检索日志回归：
  978 passed、3 skipped，包含本地 Schema 更新及非 Schema 隔离用例。跳过项为默认禁用的
  真实 Redis 双实例用例和两个本地 Engine 不适用的云端迁移用例。
- 静态修正后的导入隔离、配置加载、接入 API 边界及本地 Schema 回归：123 passed、
  2 skipped；AST 检查确认所报条件、推导式和受保护成员访问问题已修正，但不代替服务端
  静态检查器。修改过的 Python 文件通过 ruff check。
- 扩大到 construction/control/api/common 与配置加载的回归曾得到 1519 passed、
  5 skipped、6 failed；失败为两项 BGE-M3 用例缺少 torch、四项出站 SSL 用例缺少 httpx，
  不宣称全量回归通过。真实 LLM 用例需要外部服务与额外依赖，未纳入确定性单测。

本地更新回归使用真实 API、本地/云端 Engine、Schema normalizer、KV、全文/向量索引和
实体链接服务，仅模型回复与外部实体存储使用确定性替身。覆盖 #208 的陈静→李红正文案例，
并以“陈静上周评审了召回方案的设计文档”作为共享实体哨兵；在语料数量大于 top_k 时检查
新旧检索、实体哈希、关联 ID 和消息缓存。另覆盖两模式、多源改值/省略、版本回溯、负向
事实、完整空结果、歧义、权限、跨 pipeline 迁移、配置失效、截断/超时和正文改回旧值。
取消恢复机制后，相关用例改为验证成功或失败均不写操作记录、部分失败明确上报，以及
遗留操作记录不会阻塞新请求或过滤后续 ADD。

取消机制后的 Schema 更新、非 Schema 隔离及现有 Schema 抽取回归为 133 passed、
2 skipped；两项跳过为本地 Engine 不适用的云端迁移用例。此结果包含未提交的本地用例。

Rebase 到最新 mem2.0 并保留 #209 的实体名写回修复后，construction/control/api、
导入隔离、配置加载和检索日志回归为 981 passed、3 skipped；包含本地更新/隔离用例。
跳过项为真实 Redis 双实例用例和两个本地 Engine 不适用的云端迁移用例。相关修改文件
通过 ruff check；该轮验证时尚未提交上述精简回归。

18 个主线隔离用例使用 `schema_enabled=false` 的实际装配，覆盖两 Engine、两 mode、
实体索引开/关、普通 add、API 与 Engine 直接 update、真源读取/路由/索引调用次数，以及
实体后端异常、部分失败和 Schema 异常之后普通写入的失败策略。

同步上游日志脱敏后的结构核对确认 EntityLinkService 与上游一致，EntityIndexBuilder
非日志逻辑与合并前一致、日志调用与上游一致，保留 Schema 上下文中的失败上抛。

## 按 Source 定位关联 property 的性能优化

2026-09-16 首版实现内存 KV 与单实例 Redis 的可选关联索引；默认关闭，SQLite、PostgreSQL
及加密 KV 继续扫描。此次只替换候选查询，保留 Source 更新原有的抽取、匹配及提交语义。

### 1. 目标与实施边界

把 Schema Source 正文更新的关联查询从扫描 Scope 内 N 条 MemoryUnit，改为定位该 Source
关联的 k 条 property 后读取真源。k 包括该 Source 关联的历史记录；仍按原规则过滤状态和
有效期，不把 k 宣称为活跃 property 的数量。内存与 Redis 候选枚举为 O(k)；后续 SQL
适配目标为 O(log E + k)，E 为索引关系总数。不承诺固定延迟，也不包含 LLM 抽取时间。

此次只优化候选查找，不改变完整抽取、属性匹配、多源更正规则、OVERWRITE/SUPERSEDE、
entities 写回、API 逐项鉴权或 Engine 现有准备路由。保留请求内计划与修订校验，不增加
`/schema_updates/`、pending/done、请求重放或自动恢复流程，也不增加跨后端事务承诺。

### 2. 查询协议

在 `KVStore` 增加可选查询能力 `get_schema_properties_by_source`，接收完整 Scope 和 Source ID，
返回候选真源的 key/value 列表或 `None`。默认返回 `None`，现有第三方后端无需立即实现；
正式接口契约见 S06。维护入口为按 Scope 的显式重建与清理，均属于存储管理权限。

- `None`：后端不支持、索引尚未建全或索引已失效，协调器使用原有全 Scope 扫描。
- `[]`：索引覆盖完整，确认没有候选；不能因此再次触发全量扫描。
- 非空列表：返回候选真源的 key/value，沿用协调器现有 Schema、来源、生命周期、时间及
  Schema 版本检查。索引只定位 ID，不复制 property 正文或替代真源。
- 授权失败、连接失败等异常照常上抛，不把异常转换为“无关联”。读取索引后 property
  已过期/删除时可省略该候选；已存在的真源仍需完整加载，不能因批量读取部分缺失而漏掉其他 ID。
- 候选不能按 top_k 截断。后端可以分批读完全部关联，再返回完整结果；不得截取部分结果
  交给完整抽取协调器执行撤回判断。

`SchemaUpdateCoordinator.prepare()` 只将数据来源替换为“可选索引查询 → 不可用时扫描”，
后面的过滤及匹配保持不变。返回完整字节记录而非让调用方再次解析 KV 端口，可保证一次查询
使用同一个实际后端；动态路由不得从 A 的索引定位后去 B 读取正文。

### 3. 索引数据模型

关系主键为 `(org, space, user, agent, session, source_id, property_id)`，严格保留 Scope
五个维度，并归属于 property 真源所在的实际 KV 实例。不同库、命名 KV 或路由实例不共用索引。

只为 `system_metadata.extraction_mode == "schema"` 的 MemoryUnit 建关系。来源规则与
现有 `sources(unit)` 完全一致：`provenance` 非空时使用其中去重后的全部 ID，否则回退
`source_ref`。不把 `source_ref` 另行并入非空 provenance，以免扩大现有语义。

例如 P1 的 `provenance=[S1, S2]`、`source_ref=S1`，必须同时有 `S1 → P1` 和 `S2 → P1`。
还需要按 property 定位其旧来源集合，以便更新、硬删除时撤销旧关系。集合更新使用单条关系
增删，不对某个 Source 的完整 ID 列表进行无并发保护的读改写。

索引保留历史 property 的来源关系，由查询后的真源过滤决定本次是否关联。不能按当前时间
直接移除所有过期边，因为 SUPERSEDE 的生效边界可以是过去时间；不能改变现有时间过滤语义。

### 4. 写入位置与一致性

关系维护放在启用了该功能的 KV 后端写入实现内，与 `/memory/{id}` 本体的 insert/update/
delete 一起执行。构建层仍通过 IndexBuilder 写记忆；协调器仍不直接写 KV。

这样既覆盖 ForwardIndexBuilder，也覆盖 CompositeDomainStore 的写入以及生命周期组件
对 KV 真源的修改；不能只在 Schema `update()` 或 ADD 后补写，否则会漏掉直接 property
更新、删除等路径。非 KV 真源的自定义 DomainStore 不自动声称支持此索引。

| 操作 | 关联索引处理 |
|---|---|
| ADD 产生 property | 为全部来源增加关联 |
| OVERWRITE | 根据旧、新 provenance 的差集增删边；内容变化但来源不变时保留边 |
| 多源 property 撤回一个来源 | 仅删除对应 Source 的边，保留其他来源 |
| SUPERSEDE | 为新 property 建立新来源关联；旧 property 保留历史关联 |
| 关闭有效期、软删除或归档 | 保留边，读取真源后按原规则过滤 |
| 硬删除 property | 删除该 property 的全部边和反向来源记录 |
| 清空 Scope / 删除 Space | 在清理真源时同步清理该 Scope 的关联数据，不能遗留不可枚举的内部键 |

每条 property 的本体与关系维护须满足后端的一致性约束；不能把“本体成功、关联丢失”当成
可用索引继续查询。关系缺失会造成漏撤回，不能仅记录日志。索引维护错误上抛；不能证明索引
完整时，停止使用该索引，后续回退扫描或显式报错。修复方式是重建派生索引，不重放业务请求。

### 5. 后端实现

| 后端 | 读取方式 | 写入及删除方式 |
|---|---|---|
| 内存 | `scope → source_id → set[property_id]` 精确定位 | 同一锁内维护本体、正向集合及 property 的旧来源集合 |
| SQLite / PostgreSQL（后续） | 首版返回 None；后续独立关系表，联合索引覆盖完整 Scope + source_id，另建 Scope + property_id 索引 | 后续使用单条 property 的数据库事务；首版不维护关系 |
| Redis | 每个 Source 一个原生 Set 保存 property ID，按已知 Set key 取成员，再分批读取本体 | 每个 property 另存来源集合；通过短 Lua 脚本校验旧关联版本并更新本体、相关 Set；发生并发变化则重新读取旧集合后有界重试 |

Redis 不用 `SCAN MATCH` 查关系，也不把全部 property ID 保存成一个 JSON 数组反复覆盖。
索引集合、property 来源集合和可用标记使用后端保留的内部命名空间；普通 KV.scan 和 scopes
枚举不得把这些集合当作业务字节记录或额外 Scope。Scope/Space 清理须显式覆盖这些内部键。
Lua 访问的所有 key 必须作为 KEYS 参数显式传入，旧来源集合与新来源集合共同决定这些 key。
不在一次脚本中扫描整个 Scope。首版只覆盖现有单实例 Redis 形态；跨槽 Cluster 未经过同槽
键布局及迁移设计前保持不支持，不静默退化为非原子分步写。

Lua 的执行隔离不等于失败回滚。脚本先校验参数、key 类型和旧关联版本；开始变更前把固定的
索引可用标记置为不可用，全部成功后仅在入口原本可用时恢复。脚本出错或执行结果不确定时
保持不可用，后续单次成功写入不能擅自恢复，须显式重建并验证。Redis 使用 noeviction 或
等价受控淘汰策略，禁止索引集合被单独淘汰而真源仍保留；索引成员不独立设置 TTL。
本体 TTL 到期可能留下多余关联，读取时跳过缺失真源，显式重建/清理时回收；不能因此漏查
其他仍存在的 property。

上述可用标记归属于实际 KV 实例，按完整 Scope 保存索引版本与重建世代；不按请求增长，
不保存业务计划或执行进度。实现采用 Scope 粒度，便于逐 Scope 回填和随空间清理。
首版内存/Redis 只在单条 property 范围内维护索引，不保证一次 Source 更新涉及的全部
property、全文、向量、实体存储原子提交。

Redis 语义依据：[Lua 执行与 KEYS 约束](https://redis.io/docs/latest/develop/programmability/eval-intro/)、
[Redis 事务不提供回滚](https://redis.io/docs/latest/develop/using-commands/transactions/)。

### 6. 启用、回填及兼容

增加存储实例级配置 `schema_source_index_enabled`，默认 false。配置启用时必须同时设置
`globals.schema_enabled=true`，否则在组件创建前抛 `ValidationError` 并指出配置路径。
检查所有具名 KV 及内联 raw KV，按 params > globals 取有效值，并沿用现有布尔字符串解析。
此规则在配置装配入口执行，不检查调用者手工构造或注入的 KV 对象。
允许 Schema 开启但索引关闭；不将两个开关合并，也不随 Schema 开启自动开启索引，保留
扫描路径和单独启停优化的能力。未开启索引时不创建索引、不解析额外投影、不增加旧 CRUD
的 I/O；Schema 查询沿用现有扫描。不要把索引是否维护绑定到单次
API 请求或该进程是否调用 Schema 抽取：一旦实际真源启用索引，所有写入该真源的进程都必须
执行相同维护规则，包括通过普通 API 直接修改 property 的进程；通过配置装配的这些
写入进程也必须开启 Schema 主开关。配置校验不探测其他进程，部署仍须统一维护配置。

启动校验回归覆盖默认/具名 memory、redis、内联 raw KV、开关组合、布尔字符串及全局回退/
局部覆盖：20 passed。API/config、Schema 构建与更新、来源索引回归合计 467 passed、
8 skipped（Redis 专项的内存参数组合）；21 个真实 Redis 用例本轮未运行，Redis 模拟用例通过。
Ruff 及返回一致性、布尔表达式复杂度、受保护成员访问的 Pylint 检查通过。

首版采用明确的维护窗口回填，不设计在线全量扫描与并发写入合并：

1. 升级该真源的全部写入进程，确认不存在绕过索引维护的旧版本或关闭开关的写入者；暂停目标 Scope 写入，并串行执行维护操作。
2. 调用 `rebuild_schema_source_index(scope)`，先设置不可用状态并清理旧关联，再从
   `/memory/` 真源回填；不使用旧索引作为回填来源。Redis 回填需有 CONFIG GET 权限以核验 noeviction。
3. 核对全部预期关系与实际关系，包括多源、空来源、跨 Scope 同 ID 和零关联 Source。
4. 发布新的就绪世代，恢复写入。失败则保持扫描路径，重建可从头执行，不保存业务恢复日志。
   Redis 回填期间如果有启用索引的并发写入，该写入会使回填标记失效，禁止发布不完整索引。
5. 停用前调用 `clear_schema_source_index(scope)` 清理关联和就绪状态；该方法不删除记忆本体。

查询读取候选前后都检查索引可用状态与世代；期间失效或切换则回退或有界重试。
这些检查不把关联读取变成快照事务，仍保留现有更新链路的并发边界。

内存实例从空库创建并同步维护，可直接使用索引；Redis 包括新建空库也须显式回填后启用。
关闭查询使用但仍有写入时，要么继续维护，要么先使索引失效；重新开启前重新回填，
禁止直接复用停用期间的陈旧索引。
路由切库或连接晚绑定后，按目标实际库的索引状态判定，不能复用进程级“已建好”缓存。

EncryptedKVStore 首版返回 `None`，继续用已有解密扫描，其 raw KV 必须关闭此索引开关；
不能从 raw 密文解析 provenance，
也不在未经设计的情况下把加密记录的来源关系变成明文旁路索引。RoutingKVStore 需要把新方法
转发给一次选定的后端。StoreManager 授权代理将新方法显式映射为与现有 KV.scan 一致的 GET，
保留完整 Scope；不得落入未映射方法的 ADMIN 默认分支。API 鉴权流程保持原状。

### 7. 修改范围及验证

- `storage/kv.py`：可选查询协议及未支持时的返回语义。
- `storage/kv_impl/`：共用关系投影、内存/Redis 原生索引、索引版本/可用状态、回填与清理。
  来源归一化与协调器共用 `common/memory_sources.py`，避免两个实现的 provenance/source_ref 规则漂移。
- `storage/store_manager_impl/composite_store_manager.py`、`config/routing.py`：授权映射与路由转发。
- `construction/evolver_impl/schema_update.py`：以索引候选查询替换扫描入口，保留过滤、计划与提交逻辑。
- `control/space_impl/kv_space_manager.py`：空间删除后清理内部关系，涵盖本体已 TTL 到期的关联。
- 沿用 F08 归档设计；同步 S06/S08 与模块地图，不新增独立 Schema 特性文件。

首版实现通用协议、内存实现、Redis 原生集合索引及扫描回退。依据是库默认 KV 为 memory，
仓库 `deploy/docker/local/config.yml` 与 `deploy/docker/online/config.yml` 均配置 Redis；
这是基于仓库部署模板的实施优先级，并非对实际线上配置的确认。SQLite/PostgreSQL 关系表
作为后续适配，首版返回 `None` 保留旧行为。Redis 原生索引及维护失败测试未通过前不切换
对应部署的查询；不能以统一 prefix 冒充已经实现按 Source 精确定位。

验收至少包括：索引查询结果与原扫描集合等价；多源 property 能从每个 Source 找到；两种更新
模式、直接 property 更新/删除、有效期、TTL、Scope/Space 清理、旧数据回填、加密回退、路由
切换均正确；模拟写入中断后不把不完整索引当空集合；已就绪且零关联时不触发扫描；关闭开关
时原主线 I/O 次数不变。原有 #208、entities、权限和修订冲突回归继续保留。

压测分离“关联查找时间”和 LLM 抽取时间，固定 k=3/10，分别使用 N=1千/1万/10万条 Scope，
比较吞吐、p50/p95、真源读取条数、返回字节与进程内存；同时固定 N、增加 k，验证成本随实际
关联数增长。就绪路径不得调用全 Scope scan；SQL 检查执行计划，Redis 检查无 SCAN 并使用集合
操作。以上是验收矩阵，不是已经测得的性能结论。

2026-09-16 验证记录：

- 扩大回归覆盖 storage/config/construction/control/api，以及原有导入隔离和接入配置相关
  用例：1341 passed、10 skipped。跳过包含仅适用于 Redis 的内存参数组合、可选第三方库、
  原有双实例任务及本地 Engine 不适用的云端迁移；本次新增 Redis 用例实际连接 Redis 7.4.10。
- 最后对来源索引、#208、entities 和普通更新隔离执行专项回归：107 passed、8 skipped；
  8 个跳过仅为 Redis 专项用例的内存参数组合，Redis 模拟及真实实例均执行通过。
- 新增覆盖：所有 provenance、直接 property 改写/删除、TTL、历史关联、并发集合写入、
  Lua 部分失败后持续回退、来源 CAS 重试、回填失败/并发写入禁止发布、读取期间索引切换、
  路由和实际 client 绑定、空间删除回收 TTL 遗留、GET/ADMIN 授权及默认关闭时原 I/O。
- Ruff（含 protected-access 检查）通过。未修改 API update 与两个 Engine.update，未恢复
  请求持久化或处理此前暂缓的 Engine 内部路由问题。
- 静态检查补正：两个 KV 的 rebuild 委托父类后使用无返回表达式的提前退出，消除
  inconsistent-return-statements；禁用时仍抛 UnsupportedCapabilityError。Pylint 的
  inconsistent-return-statements/unreachable 检查及 Ruff 通过；本次内存/模拟 Redis
  存储专项 38 passed、8 个不适用组合 skipped，21 个真实 Redis 参数用例未重复运行。

配置示例见 S08。开启 Redis 参数后，由存储管理方在维护窗口获取同一个命名端口执行回填：

```python
kv = storage.kv("default")
kv.rebuild_schema_source_index(scope)
```

自定义存储授权启用时需传相应管理访问上下文；回填完成后再恢复该 Scope 写入。
未回填时功能仍正确，但继续走全量扫描，不会自动在一次 update 内进行回填。

本机候选查询对照（2026-09-16）：Windows Python 3.11，Docker Redis 7.4.10，单 Scope
共 N 条短文本 MemoryUnit，其中两个 Source 分别关联 3/10 条 property，其余为普通记忆。
每组运行 5 次，以下为中位耗时，单位毫秒；两条路径都包含候选反序列化，旧路径遍历并
反序列化全部记忆，索引路径只读取对应候选。Redis 使用原 scan 实现，未调整其 SCAN 参数。

| 后端 | N | k=3 旧扫描 / 索引 | k=10 旧扫描 / 索引 |
|---|---:|---:|---:|
| 内存 | 1,000 | 20.087 / 0.090 | 19.129 / 0.162 |
| 内存 | 10,000 | 178.597 / 0.048 | 170.091 / 0.141 |
| 内存 | 100,000 | 1778.111 / 0.058 | 1657.678 / 0.301 |
| Redis | 1,000 | 253.457 / 6.345 | 163.833 / 6.700 |
| Redis | 10,000 | 1605.338 / 6.868 | 1699.547 / 7.026 |
| Redis | 100,000 | 17022.284 / 4.583 | 18482.784 / 5.422 |

这些是本机查询对照，受 Docker 往返、系统负载和少量样本波动影响，不是生产延迟承诺，
也不是完整 update 或 LLM 抽取耗时。尚未测量生产并发吞吐、稳定 p95 和持续写入开销。

## 已知遗留

1. 当前实体统一完全依赖现有 EntityLinkService 的名称归一化能力，不处理复杂别名或同名消歧；
2. ADD 路径的属性采用 append-only，尚未提供全局按实体和属性的版本合并；Source update
   只处理本次更新涉及的关联属性；
3. ADD 路径的 Source `entities` 合并属性所属实体名，不额外写属性值中提及的其他实体；
   Source update 则整体替换为新抽取的实体名，两条路径均不加入 property 名；
4. 关系、图和 Schema 时序检索留待独立特性设计；
5. `SchemaOrchestratingEvolver` 当前尚未接入 `Router`。启用群体记忆归属判定时，Schema
   派生属性仍沿用 Source MemoryUnit 的 Scope；未配置 Router 时不影响 Schema 抽取、属性
   落盘及 Source `entities` 写回。

Source update 另有以下边界：

- 沿用现有跨后端顺序写入与并发边界，不提供跨 KV、全文、向量、实体存储的原子回滚或
  跨进程隔离；部分失败期间读者可能看到部分结果。
- 不保留持久化恢复状态，失败后需检查 Source、property 与索引的实际状态；相同正文的
  再次更新不会自动补齐此前未完成的派生变更，也不保证成功请求的重复调用返回相同版本。
- 不阻止旧证据被后续 ADD 再次抽取；多源更正只作用于本次匹配到的关联 property。
- 曾运行旧实现的部署可能留有 `/schema_updates/` 数据，新实现不读取或自动删除它们；
  该变更不包含存量数据清理。
- 默认仍按 Scope 扫描关联 property；启用并完成回填的内存/Redis Source 索引按关联集合
  查找。SQLite/PostgreSQL、加密 KV 仍扫描，且此优化不减少 LLM 抽取开销。
- 不处理普通记忆实体刷新、全库旧脏数据回填、property 反向改 Source/对话、通用人工
  修订保护、递归重建所有下游派生记忆、多源证据充分性推理。
