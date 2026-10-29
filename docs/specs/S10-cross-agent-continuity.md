# S10 — 跨 Agent 接入、关系扩展与任务接续契约（草案）

## 元信息

| 项 | 值 |
| --- | --- |
| 状态 | 内核与工具契约已实现；真实产品 G0 联调、真实存量验收待完成 |
| 关联模块 | jiuwen_memory_adapter/、jiuwen_memory_entry/、jiuwen_memory/api/、jiuwen_memory/control/、jiuwen_memory/common/、jiuwen_memory/storage/ |
| 最近一次修订日期 | 2026-10-29 |
| 关联特性文档 | [F09-role-memory](../features/construction/F09-role-memory.md) |
| 关联规约 | [S02](S02-memory-api.md)、[S03](S03-control.md)、[S04](S04-retrieval.md)、[S06](S06-storage.md)、[S07](S07-common.md)、[S09](S09-plugin-development.md) |

## 范围 / 边界

规定三方接入需要提供的认证、事件、任务绑定、历史导入与回写语义，以及 agent-memory 负责的任务登记、职责关系扩展和结构化视图。本规约定义跨模块边界；不更改现有 `add/search/get` 签名，不规定产品内部任务系统，不提供通用工作流或自动执行锁。

首版从已保存记录恢复任务背景，前任可以离线；不要求交接单或接收确认。多个授权参与者可贡献记录，本规约不定义执行所有权或外部任务锁。

## 不变量

1. 调用身份来自认证链，`product`、职责名称、会话 ID 和业务字段均不能替代认证。每个连接配置映射到可信主体及连接标识；绑定标识不是访问令牌。
2. 全局任务属于一个组织内的项目空间。任务绑定和来源引用不授予原记忆访问权限，所有读取仍按真实 Scope 判权。
3. 本地会话是上下文，不与全局任务简单等同。一任务可关联多个会话；同一会话涉及多任务时必须显式选择绑定或事件范围，不按标题或时钟自动合并。
4. TaskRegistry 是任务 ID、绑定及背景引用的唯一真源；客户端缓存、记忆摘要和产品侧任务列表都不能覆盖登记结果。
5. 职责只选择关系扩展策略，不授予权限；图路径不自动证明结论，关系类型/方向必须明确，共现不能冒充因果。
6. 旧 MemoryUnit 只要有合法 Scope、ID 且可读，就可被精确引用；职责标签、task_id 和向量索引不是引用资格的前提。
7. 视图中的未知、缺证和部分失败必须结构化表达；空列表不等于完成检查。受限记录的身份、数量或 Scope 不得借缺口说明泄露。
8. 登记状态更新和回执要先持久化再返回成功。普通聊天的 best-effort 写入不能作为可靠写入确认。

## Adapter 接入契约

### 认证与能力声明

| 项 | 必须提供 | 不满足时 |
| --- | --- | --- |
| 认证 | 远程连接使用 TLS 和服务端可验证的 Bearer/API 凭据；本地桥接从受控凭据存储取得同等身份上下文。凭据不写入事件/提示词 | 拒绝业务调用；不得切为匿名或 dev 身份 |
| 连接身份 | 服务端登记的 `connector_id` 及其所属主体；产品本地账号映射由可信配置确认 | 不以用户提供的产品名推导主体 |
| 工具调用 | 能调用 record/view、登记/绑定查询及复核操作，并接收结构化结果 | 不能标记为具备接续能力 |
| 上下文绑定 | 原生上下文 ID，或由适配器生成并持久化的上下文别名；同时标明 ID 来源 | 不声称别名是产品原生会话 ID |
| 记录可靠性 | 稳定事件/重试键、写入结果及失败状态可取得；无法自动采集时由显式工具提交 | 不把未回写的执行结果列为已交接 |
| 历史能力 | 声明 `native_export`、`user_supplied` 或 `unsupported`；记录实际覆盖范围 | 不承诺完整历史；不阻断从新任务开始的在线接续 |
| 生命周期钩子 | 声明是否有前置读取、轮次回写和结束钩子 | 无钩子时使用显式工具调用，不虚构自动捕获 |

上述能力在连接配置/版本确定后声明；临时认证或网络故障作为操作失败，不通过修改能力声明掩盖。Agent 主动工具模式需要用户或接入指引明确触发；未发生工具调用时，不能认为事件已被捕获或交接已完成。

### 事件与回写信封

字段为逻辑 DTO 草案，传输工具名称另行确定；认证上下文与事件体分开。

| 字段 | 类型 / 约束 | 语义 |
| --- | --- | --- |
| `schema_version` | 固定版本字符串 | 首版使用 `continuity-event/1`；未知主版本拒绝 |
| `binding_id` | string | TaskRegistry 中当前有效的绑定；导入也需明确目标绑定/任务 |
| `event_id` | string | 在该连接和绑定内稳定；重试复用，不能每次随机生成 |
| `kind` | 枚举 | observation / statement / judgment / result / experience / correction；无法分类为 unclassified |
| `content` | string | 原始内容或明确的结构化投影，不含凭据 |
| `occurred_at` | RFC 3339 string 或 null | 来源事件时间；摄入时间由服务端记，不用三方时钟决定全局顺序 |
| `source_locator` | string 或 null | 原始消息、工具输出或导入文件位置；未知必须显式保留 |
| `claimed_author` | string 或 null | 导入文本声称的作者，不作为已认证作者 |
| `duty_ids` | string[] | 职责背景；不产生权限 |
| `evidence_refs` | MemoryRef[] | 已知证据引用，不由摘要编造 |
| `relations` | RelationRecord[] | 可选，仅引用已提交的 MemoryRef；缺省为空，不凭缺省推断无关系 |

服务端从认证链和绑定解析可信连接、提交者、任务与落点；若请求显式携带这些字段，须核对一致，不能覆盖可信值。稳定事件键的作用域包含组织、目标空间、可信连接与绑定；同键同内容重试不得增加独立证据计数，同键异内容返回冲突。

回写结果至少区分 `committed` / `partial` / `unknown` / `rejected`，返回已确认保存的 MemoryRef、索引状态与结构化错误；原始来源和派生单元引用分开。`unknown` 先核对再重试。不能仅返回 Markdown、“已记住”或一个后台 job ID 充当持久化确认。

### 历史导入交换格式

Adapter 交付 UTF-8 JSONL：每行一个上述事件；附批次清单，包含来源产品和版本、导出/采集方式、上下文 ID 与来源、事件范围、批次 ID、文件摘要、记录数及缺失字段。原生导出为每会话一行时由适配器拆成事件并保留原文件及行内定位。

本地无稳定事件 ID 时，使用固定导入批次标识＋源文件摘要＋行/事件序号生成可重试键，不能用文本相同推断是同一事件。用户手工提供片段标为 `user_supplied`，不能宣称完整会话或可信工具执行反馈。源文件变化作为新批次，跨批去重需来源映射，不保证自动识别所有重复。

导入只增加记忆，不改变任务管理权或恢复验证状态。覆盖缺口、冲突和失败清单随结果返回。

## TaskRegistry：任务登记与上下文绑定

### 对外操作语义

| 操作 | 行为 | 权限与错误 |
| --- | --- | --- |
| 注册任务 | 创建稳定 task_id，保存组织、项目空间、负责人及目标引用；初始为 open，可同时登记首个绑定 | 需要项目登记权限；不存在的空间、未授权负责人或绑定拒绝 |
| 绑定上下文 | 将可信连接＋本地上下文及 ID 来源关联任务，返回 binding_id | 负责人或被明确委托者可建立；须验证目标连接可参与该项目 |
| 查询任务/绑定 | 按 task_id 或连接＋上下文读取有权查看的映射及当前修订 | 多个匹配不得默选；背景引用按源权限裁剪，不暴露其他项目绑定或私有来源 |
| 解除绑定 | 软撤销映射，记录操作者、原因和修订，历史关联仍可审计 | 解除不等于删除历史或撤销源记录授权 |
| 关联已有记忆 | 将有权引用的 MemoryRef 挂到任务的背景清单，标注用途和范围 | 校验任务管理权和来源读取权；不复制正文、不改源 Scope，不授予他人读取权 |
| 关闭任务 | 标记 closed，保留历史；新事件不能隐式重开 | 负责人或受托者，带 expected_revision |

任务标识由 agent-memory 生成，用户或脚本负责显式绑定；Adapter 负责解析并回传绑定，产品无需实现“跨产品任务等价”API。一会话多任务默认使用不同绑定，未提供绑定而无法唯一解析时拒绝。解除后重新关联创建新绑定，不能让迟到事件被静默写入另一个任务。

背景清单在控制真源中区分必需与可选引用。对外查询按源授权裁剪，不意味着内部删除引用：view 和可选背景快照 的受信任核验仍检查必需集合。任何必需引用因无权或缺失不可用时，返回不含引用身份的通用 blocking gap，continuation_ready=false；不得通过先过滤、再对剩余集合宣布完整来绕过条件。可选引用未能读取时仅显示不泄露来源身份的覆盖限制。

`TaskRecord` 至少保存 task_id、项目 Scope、负责人/受托管理者、revision、绑定集合（含撤销状态）、背景 MemoryRef 清单、open/closed 状态。`Binding` 保存连接、本地上下文、ID 来源、创建者、有效状态及归属任务。首版绑定归入任务控制记录一并持久化；按本地上下文反查的索引仅是可重建投影，缺失或滞后时回查控制真源，不据旧索引接受已经解除的绑定。记录不得包含第三方登录凭据。

### 首版存储和部署约束

这是 **control 管理的数据域**，通过公开 TaskRegistry 端口访问；不把登记记录当普通 MemoryUnit 交给抽取、向量检索或 dreaming。数据经 StoreManager 的显式具名持久端口保存，不能在 Adapter 私建数据库。管理域必须执行同组织/项目授权、审计和保留策略，生命周期由装配根管理；这些是 S09 统一门面例外所需的明确边界。

首版采用**一个有权写入的 Registry 服务实例**，按 task 串行处理修订检查及持久写入；持久端口需保证单任务控制记录原子替换和故障后可读。现有通用 KV 接口不声明 CAS，不能把“先 get 再 update”包装成跨实例原子保证。首版禁止多个写实例；无法保证单写部署或持久能力时拒绝启用。多写实例支持需另增条件写/租约等契约，本期不承担。

三方接续调用必须指向**同一个 agent-memory 服务中的 Registry 写入口**，不是各产品各自 assemble 一个 SDK 内核再共享同一个 KV。独立 SDK 实例只可维持原有非接续模式；不得成为第二个 Registry 写者或在远端失败后本地接管。单进程内嵌测试可共享同一具名 Registry 实例，但不替代三方真实集成的统一服务部署验证。

修改已有任务的请求须携带 expected_revision 和稳定 request_id；创建请求以调用身份、项目和 request_id 形成稳定创建键，避免重试创建多个任务。同修订并发更新只有一个成功，其余返回 conflict；在声明的回执保留窗口内，同一已提交请求返回既有结果，同 request_id 不同操作或内容返回冲突。回执过期后旧修订请求仍拒绝，不能当成新操作重新执行；调用方改为查询当前状态核对。重试也须重新认证授权，不回显已失去权限的内容。状态及操作回执作为同一任务控制记录提交，不能先报告成功再异步落盘。审计记录请求和实际结果，不把失败请求记为完成。

## 无交接接续与并发语义

新产品取得授权并绑定任务即可查询，不需要前任在线、prepare 或 accept。可选背景快照是普通有引用的记录，不转移执行权。任务背景缺失时返回 gaps，不能把未知当作未做或已完成。

Registry 的修订检查只保护映射和背景清单。多个绑定可并发写观察，事件按稳定键幂等；互相冲突的结果分别保留并标注，不按最后写入覆盖任务事实。已撤销绑定拒绝新事件；关闭任务后仅允许授权的历史查询和显式管理操作，不因迟到结果自动重开。正文保存与关联登记非跨库事务，部分失败返回已保存引用和未完成关联，按回执核对后重试。

## 关系记录与职责扩展契约（Draft）

### 关系模型

下列类型是新语义草案，不声明与已有 RelationType 同名值兼容；适配必须核对方向及端点含义，不能直接混用。关系类型先定义、版本化；具体关系后续增量生成。首版端点使用 MemoryRef，实体索引作为定位入口，其规范实体标识需解析成受权记忆引用，不能直接冒充记忆 ID。

| relation_type | source → target 含义 | 端点约束 |
| --- | --- | --- |
| supports | 证据 → 判断 | 不能由相似度自动判定支持 |
| contradicts | 证据 → 判断 | 双向等价；保留条件，按下述消解契约处理 |
| possible_cause_of | 原因假设 → 观察到的问题 | 始终保留假设性质，确认关系不等于确认因果 |
| verification_method_for | 方法/用例 → 问题观察、判断或方案 | 描述如何验证，不表示执行过 |
| verifies | 结果 → 问题观察、判断、方案或用例 | 需注明验证条件及结果依据 |
| requires | 方案/行动 → 前置条件 | 是执行约束，不表示条件已满足 |

RelationRecord 必填：schema_version（`memory-relation/1`）、relation_id、revision、relation_type、source/target（MemoryRef）、evidence_refs（MemoryRef[]）、conditions（string[]）、status（candidate / confirmed / needs_review / revoked）、model_version（关系模型版本）。写入主体、时间及关系真实归属由服务端保存；导入批次、抽取器/模型版本按来源补充。confirmed 表示关系经可定位依据或有权责任方确认，不把其端点推断升级为外部事实。模型抽取默认 candidate；缺证不得自动 confirmed，candidate 只能在显式未核实区呈现，不作为已验证路径。

未知类型、无效方向/端点、模型版本不兼容拒绝写入；端点未能解析保留为待处理项，不生成虚构边。真源关系记录先持久化，再以 relation_id/revision 幂等更新图投影。关系修订保留历史，撤回和源隔离立即通过真源核验阻止使用；投影滞后只能降低完整性，不能恢复旧状态。关系与两端及证据均须可读；无权节点不可作为中间桥梁，响应不得泄露隐藏路径或节点数量。

涉及本次新内容的关系采用两步写入：先 record 取得服务端返回的 MemoryRef，再通过关系登记操作提交 RelationRecord（最终公开签名实装前登记 S02）。首版不接受客户端临时端点。第二步失败不回滚第一步；返回正文引用、关系提交状态与幂等回执，按 relation_id/revision 核对后重试，图投影失败另行修复，不重复抽取正文。关系登记与修订需校验真实归属的操作权和端点读取权，expected_revision 冲突拒绝。

### 查询输入与策略

下列为拟议 view 逻辑输入，不改变现有 search 签名：

| 字段 | 约束 |
| --- | --- |
| query | 非空文本，可选；用于向量/全文/实体定位起点 |
| anchor | MemoryRef，可选；存在时直接点读作为起点，不强制再做语义搜索 |
| task_id | 可选；指定时必须读取获授权的任务共同背景 |
| duty | 服务端注册的策略名；内置 general / diagnosis / testing / execution，未知值报错 |
| budget | 可选 max_depth / max_nodes / max_edges / timeout_ms；正整数，不得超过服务端上限 |

query、anchor、task_id 至少提供一个。仅 task_id 时以登记背景为起点；同时给 query 与 anchor 时 anchor 固定起点，query 只辅助排序。anchor 与 task_id 同传不自动创建关联。响应报告实际解析到的职责、策略版本和生效预算；配置默认职责与显式值的优先级为“显式值 → 连接默认值 → general”，均不影响授权。

业务 duty_ids 与经验适用职责可多值，查询 duty 是单一遍历策略，不是业务身份。团队自定义职责通过启动配置新增遍历策略；本期不支持多策略自动合并。

职责策略映射为“关系类型＋遍历方向＋深度/数量限制”，不要求每条边写 duty。diagnosis 从问题沿 possible_cause_of 入边找假设，再沿 supports/contradicts 入边找证据；testing 沿 verification_method_for/verifies 入边找方法和结果，再找方法的 verifies 入边；execution 从已选方案沿 requires 出边找条件，并沿 verifies 入边找执行验证。问题没有明确方案关系时报告缺口，可另做语义候选检索，不能虚构问题到方案的连接。

需要跨任务经验且没有已知关系时，可从已授权任务目标/query/anchor 正文做有界语义候选发现。返回 ViewItem 的 discovery 为 semantic_candidate，path_ids 为空；不得编造与原起点的关系，可将候选作为附加起点继续展开。无可用文本报 missing_input；候选与图共用总预算，额外候选不挤掉必需背景。

general 使用同一关系集合的有界通用展开。所有策略对返回判断/方案的直接反证、必要条件执行共同检查，共用总预算；不足时明确 unchecked_dependencies，不宣称完整。按 Scope/ID/摘要去重节点，按关系 ID/版本去重边，访问去重防环。固定模型和数据下，相同 anchor 不同 duty 应体现路径选择差异，而不是只重写摘要。

声明支持该能力的查询实现须支持端点解析、类型/方向筛选、逐跳授权、路径返回和预算限制，不可用私有 extensions 隐藏必需契约。图通道临时失败可返回起点及明确背景的 partial 结果；未装配能力返回 unsupported，不能伪装无结果。无图覆盖与确实无匹配边通过 coverage/gaps 区分。

覆盖清单由构建流程维护，至少记录 Scope、处理来源集合及摘要、model_version、构建批次、各项完成/失败状态、关系真源修订及图投影水位。只有请求可界定范围内的来源已登记且摘要未变、相应投影追平时才可 covered_for_requested_scope；新来源/修订使该范围失效，失败为 partial，无可证明清单为 unknown。无法建立全量水位时保守返回 unknown；该状态只证明登记范围被处理，不证明自动抽取无遗漏或外部世界无其他关系。

### Agent 能力发现与工具暴露

工具 schema 描述 query/anchor/task_id/duty/budget 及错误语义；工具说明明确“定位记忆并按职责扩展关系”，暴露支持的 duty 与缺省行为。系统提示词或 Skill 仅补充何时调用：恢复任务先读取背景、缺证时展开、形成结果后记录。不得仅靠一段提示词宣称已实现工具能力。

Provider/MCP Adapter 只负责注册、可信上下文、参数转换及结构化回写，不执行图算法。已固定职责可经连接配置提供默认值；多职责 Agent 可显式选择。宿主不能表达所需 schema 时由适配器显式转换并测试，不静默丢弃职责或路径。Agent 不需掌握全部边类型，关系策略由服务端维护。

## 结构化角色视图 v1（Draft）

以下是最小字段 schema，适用于逻辑 view 结果；MCP/HTTP 传输适配必须保留其语义，Markdown 只是附加呈现。所有顶层字段必填，未知值使用 null 或显式状态，不能省略 gaps 来表示“不知道”。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | string | `role-view/1`；未知主版本拒绝解析 |
| `task_id` | string 或 null | 无任务的记忆扩展返回 null |
| `anchors` | MemoryRef[] | 实际使用的可读起点 |
| `paths` | RelationPath[] | 有依据且已授权的扩展路径 |
| `facts` | ViewItem[] | 观察或来源陈述；类型在 item 中明确 |
| `judgments` | ViewItem[] | 解释、建议或决策，不与事实合并 |
| `experiences` | ViewItem[] | 带适用条件的职责经验 |
| `open_issues` | ViewItem[] | 未决事项及未分配责任 |
| `risks` | ViewItem[] | 已知风险和约束 |
| `gaps` | Gap[] | 数据缺失、核验未完成、预算或通道限制 |
| `meta` | ViewMeta | 下表所列元信息 |

`ViewItem` 必填：`id: string`（视图内唯一）、`text: string`、`kind: string`、`status: string`、`evidence_refs: MemoryRef[]`、`conditions: string[]`、`duty_ids: string[]`（此项经验的适用职责，非作者身份）、`discovery: string`（background / anchor / relation / semantic_candidate）、`path_ids: string[]`。`kind` 取 observation / statement / inference / suggestion / decision / experience / open_issue / risk；`status` 取 observed / reported / unverified / supported_in_conditions / needs_review / refuted / unknown。`reported` 仅表示来源陈述。无证据允许呈现问题或未验证建议，但不允许标为 supported_in_conditions；该状态的 conditions 非空。

首版仅由已确认 `supports` 关系提升其语义 target 的 inference 条目：关系、两端及证据均可读，且 evidence_refs 与 conditions 非空。反向遍历不颠倒被支持者；条目携带该关系的条件、证据与路径。多条支持路径保留各自条件，不把不同条件组拼成无条件结论。`verifies` 本身不编码结果是支持还是否定，不能仅凭该关系提升状态；需另外登记有依据的 supports/contradicts。待复核反证及未解决冲突优先降级为 needs_review，明确反驳保留 refuted；关系撤回后重新计算视图。

`MemoryRef` 必填：`scope`（包含 org / space / user / agent / session 五个字符串）、`unit_id: string`、`content_digest: string`（服务端按下述内容序列化计算的 SHA-256）。旧记录不需先增加字段，首次关联时由服务端读取并计算摘要；读取仍以原 Scope 和 ID 为准。这里仅引用 MemoryUnit，不把 `/messages/` 原始来源 ID 冒充 unit ID。

摘要输入为 `segments` 的有序列表，每段仅含 content、assets（保留顺序）和 source 的枚举值；封装成键为 segments 的 JSON 对象，按键排序、无额外空白、保留 Unicode 字符而不做规范化，以 UTF-8 编码后计算 SHA-256，输出 64 位小写十六进制。层级摘要、向量、生命周期和授权信息不入内容摘要；这些状态在每次读取/接受时另行核验，不能因摘要未变而跳过。

`RelationPath` 必填：path_id（string，视图内唯一）、anchor（MemoryRef）、steps（有序列表，每步含 relation_ref: MemoryRef、relation_id、revision、relation_type、direction: in/out、source/target: MemoryRef、status、evidence_refs、conditions）。direction 是遍历方向，不改变关系固有方向。路径可以共享节点；正文按引用去重，路径不得从摘要重新抽取。

`Gap` 必填：`code: string`、`message: string`（经脱敏）、`blocking: boolean`、`affected_item_ids: string[]`。首版 code 包含 missing_input / reference_unavailable / unchecked_dependencies / budget_exhausted / retrieval_partial / environment_unconfirmed / review_pending / relation_coverage_unknown / graph_unavailable；reference_unavailable 不对无权者区分不存在和受限。

| ViewMeta 字段 | 类型 | 约束 |
| --- | --- | --- |
| `based_on_handoff` | MemoryRef 或 null | 基于哪个明确交接；null 不等于整个历史已检查 |
| `generated_at` | RFC 3339 string | 生成时间，不是三方同步水位 |
| `registry_revision` | integer 或 null | 本次任务登记修订；无任务时 null |
| `duty` / `policy_version` / `model_version` | string | 实际职责及策略/关系模型版本 |
| `effective_budget` | object | 生效的深度、节点、边和耗时上限 |
| `relation_coverage` | covered_for_requested_scope / partial / unknown | 仅描述登记的构建范围，不声称世界知识完整 |
| `completeness` | complete_for_requested_scope / partial | 只描述本次请求范围；存在未检查范围时不得为 complete |
| `continuation_ready` | boolean | 所请求背景的必需证据及已声明环境条件是否满足；未知时 false。仅为信息就绪提示，不授权执行，不代表任务完整历史已采集 |
| `unchecked_scope` | string[] | 语义范围，例如 additional_history / dependencies / environment；不是未授权空间 ID 列表 |

遍历队列耗尽仅表示本次有界图展开结束，不能证明摄入覆盖或环境已核实。首版未提供这两类覆盖证明，因此维持 partial，不依据 gaps 为空升级为 complete。

所有引用和内容在返回前判权；无权信息不得以 item ID、计数、Scope 或缺口说明回显。必需交接引用不可用时 continuation_ready=false；可选经验通道失败可返回 partial，但必须说明实际可继续的边界。

## 验证基线

- 每种产品独立认证和工具注册；伪造 duty、actor 或 binding 不得扩大权限。
- 前任离线且无交接单时，新产品能恢复已保存背景，未采集历史明确为缺口。
- 相同 query/anchor 不同 duty 返回正确关系类型与方向；对照纯向量、标签重排和无职责图展开，检查路径正确率及成本。
- candidate（兼容旧 proposed）、反证、环、超预算、失效端点、撤回边、图滞后、缺失通道、权限中间节点均有可验证行为。
- 任务映射重启可读；并发修订、事件重试、绑定撤销及任务关闭不误关联或重开。
- 实际存量无需职责标签即可点读；关系补建幂等、可撤回，不改原正文，不误删后续关系。
- 结构化响应包含 anchors/paths/gaps 和策略版本；工具 schema、默认职责与显式职责一致。

## 与其它 spec 的关系

S02 是公开 API 和鉴权入口，登记 role_task/record/relation/view/rebuild 及 surface 覆盖。S03 承载 TaskRegistry 及复核编排，S04 承载职责关系扩展和 view，S06 明确具名管理端口及持久能力，S07 承载最终 DTO/编解码。S09 约束装配、能力、错误、health 和关闭。此草案不自动赋予现有插件这些能力。


## 实装补充：关系配置、复核与恢复

- 公开入口为 `role_task`、`role_record`、`role_relation`、`role_view`、`role_rebuild`，逻辑 DTO 经统一 HTTP/CLI 契约解析；MCP 保留嵌套 schema。远程产品桥只转发到同一服务，不在产品内装配第二个 Registry。
- 关系状态统一为 candidate/confirmed/needs_review/revoked；兼容读取旧 proposed/retracted 字符串，不要求改写旧正文。自定义关系名为字符串，启动目录验证未知类型、端点类别及策略引用。内置语义不可配置覆盖，只允许改展示名和说明。
- `RelationRecord` 增加 conflict_kind、resolution、resolution_reason、resolution_origin、resolution_refs。resolution 取 prefer_a/prefer_b/both_valid/escalate/defer。resolution_origin 由服务器重写为 human/automatic/model_candidate，客户端字段不能充当信任依据。自动采用一方必须保留所用验证关系的引用；每次使用回查当前关系头，修订/撤回或源失效后降级。人工复核优先，后续自动修订不覆盖人工裁决。胜负仅对当前条件下的视图生效，不全局删除原记忆或删除反证边。
- advisory 差异不自动合并；无法证明条件可比、存在多方冲突或证据检查不完整时不自动选胜者。LLM 的结果是候选，即使返回高置信也不能代替独立依据。模型发现与冲突分类属于可选装配；不开启时可显式登记、复核。
- `ContinuityEvent.classification_confidence` 为 low/high/unknown，low 强制降为 unclassified；原文及信封保留供复核。ViewItem 附加同名字段，未知不会变为已验证结果。
- `role_rebuild` 可选 `recovery: ContinuityRecovery(kind, operation_id, refs)`；kind 为 event/relation。只能按已存在的操作回执核对真实引用，不创建第二份内容，不自动认定写入失败。事件恢复后重试原 event_id 完成背景登记；关系恢复后补建图。无可靠引用继续 unknown。
- Registry 的 256 条回执窗口按修订号淘汰；过期旧修订仍拒绝。首版任务管理限创建负责人，产品事件身份必须匹配当前启用的连接与项目配置。绑定记录保存创建者和撤销原因；未实现独立的任务管理委托表。
- 具名持久 KV 须声明原子单记录替换，并支持 `claim_single_writer(namespace)`，持有到 Store 关闭。SQLite 用独立文件锁防止第二写者；这是单机约束，不支持跨主机网络文件系统多写。allow_ephemeral 仅用于测试。
- 图投影使用 continuity:memory:/continuity:relation: 前缀；逻辑 relation_id 禁用 continuity: 前缀。投影由 IndexBuilder 的 RETRIEVAL_ONLY 路径完成；旧未分区投影需重建，不改变 MemoryRef 内容摘要。
- 关系覆盖没有可信全量水位时返回 unknown；不虚构 covered。当前实现不提供环境核验，因此 continuation_ready 保守为 false；timeout_ms 限制继续展开，不强杀已进入的同步存储调用，部署还须配置底层 I/O 超时。

角色入口与其他公开方法一样向 PEP 传递完整 `RequestSecurityContext`，先验证来源、时效和凭据撤销状态；不能只传 actor。项目空间成员授权由 `space_aware` Authorizer 执行。

配置与可执行接入步骤见 [示例](../../examples/role_memory/README.md)。正式产品联调和真实历史样本不由协议替身测试代替。

事件落盘与任务背景关联采用可恢复的分步提交：partial 回执保存已写正文引用；同键重试复核引用并幂等补齐背景，再持久化 committed。背景已保存、最终回执未保存时允许任务修订继续递增，但不重复写正文或追加相同背景。未保存可靠引用时仍为 unknown，必须核对恢复。可选关系发现失败不撤销已提交事件；association_status=partial 可重试，并记录不含原文及异常消息的错误类别诊断。

角色入口沿用同步 MemoryAPI 契约；异步宿主通过工作线程调用（MCP 已如此接线）。关系修订和恢复判权使用已读取 MemoryUnit 的同一快照，避免为判权额外读取另一版本。
