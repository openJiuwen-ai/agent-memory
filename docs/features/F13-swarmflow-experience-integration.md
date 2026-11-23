# SwarmFlow 接入经验记忆

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-19 |
| 状态 | SDK 接线已实现，待三仓联调 |
| 影响范围 | jiuwenswarm、agent-core、agent-memory |
| 测试基线 | 下文为拟验收场景 |

## 背景

用户让 Swarm 修复 CI 时，一轮聊天可以只是追问“本地还是 CI”，而实际修复可能跨多轮执行。普通记忆保留问答和用户约束；经验记忆从执行记录中提炼可复用的方法。两者需要不同的提交时机。

## 决策

| 动作 | 触发时机 | 调用路径 |
|---|---|---|
| 查普通记忆 | 原有对话流程准备模型输入时 | agent-core → provider 的 `prefetch()` → 普通搜索 |
| 保存普通对话 | 一次调用结束，原有回调拿到有效的用户输入和助手输出 | agent-core 的 `after_invoke` → `sync_turn()` → 普通写入 |
| 查经验 | Agent 判断旧经验有助于当前任务时，例如已定位到具体 CI 报错 | Agent → `agent_memory_experience` 工具 → 经验搜索 |
| 提交经验轨迹 | SwarmFlow 已结束且有可评价的执行结果 | 工作流结束处理 → 组装轨迹 → 后台调用 `add_trajectory()` |
| 提交单 Agent 轨迹 | SDK 经验已启用，Agent 正常返回且有实际工具结果 | `JiuwenMemoryRail.after_invoke()` → 后台调用 `add_trajectory()` |
| 整理经验 | Swarm 服务启动时确认调度任务，之后按周期运行 | Swarm 经验调度服务 → provider 调 `evolve` 注册 → agent-memory 调度器执行 |

经验工具启用后，普通 Agent 和 SwarmFlow 都可以按需查询和自动提交轨迹。SwarmFlow 按 `run_id` 汇总；普通 Agent 按一次自然执行汇总，用同一 SDK provider 分别提交对话和轨迹。

普通 Agent 通过工具结束回调收集调用和结果，正常返回并有执行证据时才提交。纯问答、直接调用记忆工具或只有结构化追问的执行跳过。通用 `tool_call` 包装调用尚未按实际工具名判断，只查记忆时可能误触发提交。agent-core 将 `set_task_status(waiting_for_user)` 暂存为当前执行的状态，并提示使用 `ask_user`。在同一 Rail 上通过结构化中断恢复时继续累计工具证据。显式进入等待的执行需要后续 `completed` 标记、本轮正常结束及有效工具证据才提交。状态工具本身不交给经验提炼，普通独立消息不接续等待中的轨迹。

轨迹提交的正文是一份完整 JSON。默认接入器按一个记忆单元传给评价器，避免普通文本预切分破坏 JSON。验收时使用超过 8,000 字符且有多组工具记录的轨迹，核对评价器能读到末尾的工具结果。

单 Agent 和 SwarmFlow 用同一轨迹组装器发送工具名、参数和结果，以调用 ID 配对。工具调用采用 `function.name` 和 `function.arguments`。评价侧仍可重新读取旧的扁平调用。模型摘要在原字符预算内保留工具结果首尾，避免一段较长的命令说明挤掉后面的校验结果。

### 轨迹怎么收、何时交

以“帮我修构建”为例：

1. 助手先回复“本地还是 CI？”。如果这次正常返回并走到普通记忆回调，保存一轮对话。此时若尚未启动 SwarmFlow，就没有工作流轨迹要提交。
2. 用户回答“是 CI”，Agent 启动工作流 R1。记录任务目标、关键工具动作和结果；如果查了旧经验，记录查询引用。工作流中途等人回答时，保留片段；恢复原运行后继续追加到 R1。
3. R1 结束时，把关键报错、修复动作和验证结果组装成一份轨迹。重跑后仍报错，也可以提交带证据的失败轨迹；没有可评价结果的取消或中断跳过提炼。普通聊天的最终回复独立按原流程保存。

如果两轮都触发普通记忆写入，以上例子是两次普通写入加一次经验轨迹提交。暂停表单本身不单独增加普通写入，仍以原有对话回调收到的内容为准。SDK 下两个入口都可落到 `add_async()`，经验轨迹通过 `system_metadata.memory_type="experience"` 分流。

提交前由程序检查运行已结束、记录有可评价的结果。agent-memory 根据证据决定是否提炼。以运行归属和 `run_id` 生成稳定的 `trajectory_id`，重试沿用同一个 ID。每次运行独立收集引用和事件，避免并发运行互相混入。轨迹提交失败由后台重试，用户回复无需等待提炼或重试。

### 三仓接线

| 仓库 | 现状与接入动作 |
|---|---|
| jiuwenswarm | SDK 装配传入经验空间和 `sync_experience` Provider 回调，挂载 Core 的 `JiuwenMemoryRail`；聊天团队和代码团队通过能力装配提供按需搜索工具。工作流监视器在收到完成事件后，把聚合快照交给后台提交。 |
| agent-core | 单 Agent 复用 `JiuwenMemoryRail` 的生命周期，由 `JiuwenMemoryRail` 采集实际工具结果、处理状态、提交完整执行。SwarmFlow 使用现有工作流事件。 |
| agent-memory | `sync_experience` 接收 Core 提供的单 Agent 执行快照，工作流入口接收 Swarm 提供的快照。适配器组装经验轨迹、去重并携带搜索引用。复用现有 SDK 写入、评价提炼和周期整理入口。 |

源码指引：[普通对话回调](https://github.com/openJiuwen-ai/agent-core/blob/develop/openjiuwen/harness/rails/memory/external_memory_rail.py#L205)、[SwarmFlow 结束处理](https://github.com/openJiuwen-ai/agent-core/blob/develop/openjiuwen/agent_teams/workflow/tool_swarmflow.py#L437)、[运行日志](https://github.com/openJiuwen-ai/agent-core/blob/develop/openjiuwen/agent_teams/workflow/engine/journal.py#L223)、[经验 provider](../../jiuwen_memory_adapter/jiuwenswarm/agent_memory_provider.py)。

### 经验空间和 Dreaming

Swarm 模板用 `experience_space: own` 跟随 `memory.builtin.jiuwen.space`。提炼后的经验按 `org + space` 写入，有该空间经验读取权限的项目成员可以检索。普通对话和经验用 `memory_type` 分流。联调时检查三个位置一致：**提炼后条目的实际落点、经验搜索范围、Dreaming 整理范围**。原始轨迹仍带执行者与会话归属。

新安装默认启用 JiuwenMemory SDK。普通记忆空间为空时，`own` 解析为空，经验接入关闭。已有部署显式指定的 `shared-experience` 仍可使用，旧条目留在原空间。默认 SDK 装配从部署配置的组织、用户和经验空间初始化一条 `READ` 授权，随本地 runtime 装配重建。复用该记忆身份的会话都能搜索。部署显式配置 `authorizer` 或 `grant_store` 时，授权由部署管理。自带嵌入配置优先，未指定嵌入服务时使用本地 hashing。

- 进程内 SDK 用内置默认配置时，显式传入 `experience_space="shared-experience"` 会同时设置默认提炼落点，并开启 provider 的查经验和整理注册。provider 参数留空时，经验能力仍关闭。
- Swarm 配置键为 `memory.builtin.jiuwen.experience_space`。工作流写入和团队搜索由不同的 SDK 实例执行，所以 SDK 的 `kv_store.default` 须用 Redis 或持久化 SQLite，`fulltext_store.default` 用 Elasticsearch，`vector_store.default` 用 Milvus 或 pgvector，各实例使用相同的存储配置。内置的进程内存储只能供同一实例使用。
- HTTP 或自定义配置时，要核对内核最终解析出的落点。HTTP 服务端单独配置 `globals.experience.default_space`；若部署按坐标路由，还要检查路由是否覆盖默认落点以及实际查询身份是否有权读取。
- 当前自动注册用固定的 `experience_consolidate`、`dreaming=True` 和 7 天周期，只整理有效的语义或程序性经验。Swarm 在 AgentServer 启动时恢复并确认注册，由 agent-memory 调度器执行。需要不同模式、筛选条件或间隔时，由部署端分别注册或扩展配置，并明确由谁管理周期任务，避免重复注册同一策略。

Swarm 侧开关如下。三个 `sdk` 存储实例的连接参数沿用部署环境，可参考[通用配置模板](../../examples/config_template.yml)。

```yaml
memory:
  builtin:
    provider: jiuwen
    jiuwen:
      mode: sdk
      space: project-a
      experience_space: own
```

## 拒绝的方案

| 方案 | 未采用的原因 |
|---|---|
| 每轮同时自动查普通记忆和经验 | 简单问答没有经验检索需求；具体执行错误出现后，往往能提出更准确的经验查询。 |
| 用一次写入混交对话和轨迹 | 两份数据的归属、完成时机、路由和失败重试不同。 |
| 每次 Agent 返回就提交经验 | 一次返回可能只是追问或暂停，最终结果和验证还没有发生。 |
| 让模型决定何时写入经验 | 运行状态和工具结果由程序掌握，模型自述无法替代验证证据。 |

## 验证

| 场景 | 预期结果 |
|---|---|
| 两轮修 CI，工作流中途暂停并恢复 | 两次正常对话写入，一次逻辑上的经验轨迹提交；轨迹包含暂停前后的关键步骤与最后的验证结果。 |
| 未启动工作流的普通聊天 | 原有对话记忆照常运行，无自动轨迹提交；启用经验工具时可按需查询。 |
| 工作流失败、取消、写入重试 | 有证据的失败可提交；无结果的取消跳过；重试使用相同轨迹 ID，无重复经验。 |
| 经验写入后，用相同空间搜索 | 可从配置的空间查到符合检索条件的提炼条目；写查空间错配时能定位到实际落点。 |
| 初始化并触发一次整理周期 | 验证注册成功、整理空间和筛选条件正确，并确认调度器实际执行。 |

## 已知遗留

### 下一步：由部署统一管理 Dreaming

Swarm 在 `AgentWebSocketServer.start()` 启动 agent-memory 经验调度服务，在服务关闭时释放它的 SDK runtime。经验关闭时跳过。普通对话、工作流写入和临时搜索三处都显式关闭初始化时的注册，兼容适配器的既有独立调用方。此接线只负责 agent-memory 的经验整理；Swarm 自带的 agent/code Dreaming 仍由原来的调度入口管理。

启动时先恢复 SDK 注册库中的已有任务，再为配置的组织和经验空间确认存在一条 `EXPERIENCE_CONSOLIDATE` 周期任务。以现有 coordinator 的 scope + mode 注册键识别同一任务，在现有领导权及注册串行化保护下完成“缺失才创建”。已有任务沿用下次执行时间。多个进程须共用持久化注册库和分布式锁，由当前持锁的调度方执行。首次注册失败在后台重试，停止时释放 runtime 和领导权。新注册任务的默认周期为 3 天（259200 秒）；已有注册继续使用保存的周期和下次执行时间。

验收覆盖：首次启动一条任务；随后多轮聊天、多次轨迹写入和搜索均不新增注册；重启保留下次执行时间；两个进程共享同一注册库时只有一条记录和一个活动调度方；经验关闭时不注册。

### 普通 Agent 的经验接入范围

普通 Agent 在启用 SDK 经验空间后，使用开启可选经验回调的 `JiuwenMemoryRail` 搜索、收集并后台提交轨迹；普通对话照常按轮写入。单 Agent 使用独立的 `trajectory_id`，同一轨迹的重试使用同一个 ID。结构化中断可在同一 Rail 上恢复，进程重启或两次独立用户输入之间的自动拼接仍待接入。

当前 SDK 自动写入只消费 `workflow_completed`，并要求快照包含至少一组实际工具调用和结果。引擎也会用 `workflow_failed` 描述仍可恢复的预算中断，后续需有明确的不可恢复标记，才能直接提交失败运行。正常完成的脚本仍可通过工具结果记录验证失败。重试目前在监视器进程内进行；进程退出后恢复待提交轨迹需要持久化队列。

现有工作流活动快照是有长度上限的工具记录。普通暂停和恢复保留已有阶段，修改脚本重新启动可能重建阶段树。当前提交使用快照中保留的证据；需要完整审计轨迹或保留改脚本前的证据时，应另接持久化采集。

三仓尚未做真实暂停和恢复联调。首个联调用例应核对完成快照是否保留暂停前后的关键证据，并用相同存储配置验证写入后能查到。HTTP 经验搜索和轨迹写入仍需补专用客户端实现。

### 经验检索的能力清单（2026-11-19）

取消以 `experience_capabilities` 限制经验检索。未配置清单或清单与条目中的工具不一致时，条件动作与环境事实均可参与召回；继续保留经验类型、类别、org + space 范围、授权和相关性筛选。经验是参考资料，读到某个操作方法不代表获得执行对应工具的权限。技能同步仍可用非空清单跳过无法执行的技能。

验证：相关 15 项单测通过。回归通过 SDK 客户端实际生成的查询条件，使用内核真源匹配器验证空清单、匹配清单、不匹配清单均不会排除方法类经验，同时不混入普通对话及其他类别；原有经验工具及技能同步用例通过。该证据未包含真实模型端到端验证。


## 技能目录与草稿

已发布的试用、批准技能同步到 Swarm 工作区的 skills/distilled/<技能名>/SKILL.md。
手动模式的技能草稿留在经验空间的 KV 库，审批由有权限的人员主动查看和确认。
技能文件写入保留来源与正文摘要，仅清理本空间管理且正文无修改的文件。
