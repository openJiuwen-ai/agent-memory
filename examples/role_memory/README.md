# 职责关系记忆：配置与接入

本示例在一个服务内保存任务映射、事件和关系真源；前任离线后，其他已授权产品可直接查询任务背景。`duty` 选择关系遍历策略，不改变权限。

> **接口状态**：本文接口为 2026-11-05 设计修订后的形态（`add` / `batch_add` 的 `continuity` 参数、`search` 的 `duty` / `task_id` / `anchor`、`continuity_task` / `continuity_relation` / `continuity_recover`、治理 `quarantine_source` / `release_source`），代码已实现；剩余差距见 [F10 12.2](../../docs/features/construction/F10-role-memory.md#122-实现差距)。

## 服务配置

`continuity.default.params.mode` 必填：`role`（本示例，含关系登记与职责扩展，需要图存储与 `role_view`）或 `continuity`（只做任务接续、可靠写入与历史导入，不需要图存储；`server.yaml` 注释给出最小配置）。接续模式下配置的图与关系相关键会被忽略并在启动日志告警；角色模式缺图存储或 `role_view` 时启动失败。

从仓库根目录加载 `server.yaml`，并叠加部署方的认证、模型及业务存储配置。示例的三份 SQLite 文件分别保存正文、Registry、证据图；后两者是不同的具名端口。SQLite Registry 用文件锁拒绝第二个写服务，限单机文件系统部署，不宣称分布式多写。

部署方需要先创建 `demo/role-project` 项目空间、添加参与主体的内容权限，并为每个连接配置真实凭据。`globals.continuity_connectors` 是服务端身份映射，不是认证器，也不会自动授予空间权限。部署方须配置现有 API Key 或可信网关认证器及凭据真源；不得用 dev 身份替代生产验证。示例通过 `authorizer.default=space_aware` 装配空间成员判权，`permission` 配置不能替代 Authorizer。

`relation_types_path` 和 `duty_policies_path` 在服务启动时读取，示例路径相对启动目录。也可在 `continuity.default.params` 内使用 `relation_types` / `duty_policies` 字典；同一项不能同时指定文件和字典。新增类型需说明端点类型和语义；策略自动追加反证与前置条件检查。启用自动关系候选发现时，配置具名 `associator: evidence_llm` 及其 `llm` 引用，再设置 `relation_associator`。模型输出始终先作为候选，不能仅凭高置信升级为已验证。

## 产品工具接入

WorkBuddy、Hermes 可通过 MCP stdio 桥接入；OpenClaw、DeepSeek Harness 的接入方式待 G0 验证（见 F10 7.2）；所有产品使用同一个服务地址；各自使用独立凭据。启动命令：

```sh
# 环境变量由产品的受控凭据配置提供，不写进提示词或事件。
python -m jiuwen_memory_adapter.continuity.mcp
# 必需环境变量：AGENT_MEMORY_URL、AGENT_MEMORY_API_KEY
```

该桥只转发 HTTP，不在产品进程内另建 Registry。HTTPS 为远程默认要求；只允许 loopback 使用 HTTP。产品原生配置字段和工具注册能力按选定版本现场验证，不把通用 MCP 桥通过等同于各产品联调通过。

JiuwenSwarm 继续使用 `AgentMemoryMemoryProvider`，设置 `base_url`、`api_key`、`continuity_enabled=True`、与服务端 `mode` 一致的 `continuity_mode`（`role` 或 `continuity`，缺省 `role`）和可选 `default_duty`。它通过 `get_tool_schemas` 注册接续工具，并经 `handle_tool_call` 转发结构化结果。开启后 Rail 的 `sync_turn` 要求显式项目 Scope、任务、绑定和稳定 event_id 并返回持久回执，`prefetch` 返回结构化视图的 JSON 文本；未开启时保持既有原文写入和文本检索。关系复核仍需显式工具调用。

Agent 工具为：带接续参数的 `memory_add` / `memory_search`，以及 `memory_continuity_task`、`memory_continuity_recover`；角色模式另注册 `memory_continuity_relation`（内核 MCP 沿用 `memory_<方法名>`；JiuwenSwarm 适配器对应 `agent_memory_add` / `agent_memory_search` / `agent_memory_continuity_*`）。来源治理 `quarantine_source` / `release_source` 只在管理面使用，不进入 Agent 工具清单。嵌套参数 schema 来自公开 DTO。传输失败返回 `unknown`，不能当作未写入再换一个 event_id 重试。

## 最短使用链路

1. 负责人调用 `continuity_task(operation=create, request_id=稳定键)`，取得 task_id。
2. 负责人为每个连接调用 `bind`，提供 connector_id、context_id、expected_revision 和 request_id，取得独立 binding_id。调用方明确选择一个绑定，不按标题自动合并任务。
3. WorkBuddy 以自己的身份调用 `add(content, scope, continuity={task_id, binding_id, event_id, ...})`，原始时间放 `event_time`；保存返回单元并构造 MemoryRef。批量回写或历史导入用 `batch_add`，每项带 `continuity`。返回 `WriteIncompleteError` 时按其 `retry_action` 处理。
4. WorkBuddy 可以离线。Hermes 用 `search(query="", context, task_id=...)` 恢复已保存背景，检查 `gaps` 与 `view_meta`；无需 handoff/accept。
5. （角色模式）新内容先 `add`，再用真实 MemoryRef 调 `continuity_relation` 登记关系。对同一个 anchor 用 `search(anchor=..., duty=diagnosis/testing/execution)`，服务器选择不同类型和方向的边。复核通过新 revision 写回，必须携带 expected_revision。接续模式下带 duty 的 search 只返回普通结果与 `relation_expansion_disabled` 提示。

`continuation_ready=false` 是当前保守默认：系统未证明外部环境和全部历史已核验。事实陈述、关系确认和执行许可是不同的事。

## 存量与恢复

- 已有兼容 MemoryUnit：`history.attach_existing` 按原 Scope/ID 读取、计算摘要并登记背景引用；不改正文、不复制权限、不要求 task_id、职责标签或语义索引。
- 三方历史：`history.prepare_history` 预检 UTF-8 JSONL，保留产品/版本/上下文、源文件摘要、位置及缺失字段；`import_history` 按批调用 `batch_add`（每项带 `continuity`），逐项检查 `continuity_status`，存在非 committed 项时停止后续批次。相同批次可重试，相同文本不代表同一事件。
- 关系补建：构建侧 `relations.legacy.evidence_candidate` 可显式反转旧 caused_by 方向、保留 contradicts 为候选；similar_to/corefers 不进入证据图，其余旧类型拒绝隐式映射。`backfill_relations` 提交带 batch_id 的关系候选；`rollback_relations` 只撤回原批次的明确 revision，若后续已修改则冲突，不删除后续数据。
- 图投影丢失（角色模式）：对 projecting 状态的关系调用 `continuity_recover(recovery={kind: relation, operation_id: relation_id})`，从 Registry KV 关系头补投影，不重抽正文。旧的无前缀图投影不会继续作为角色关系使用，需要补建。
- 写入确认丢失：查询已保存原文后，使用 `continuity_recover(recovery={kind: event, operation_id, refs})` 核对事件摘要，再重试原写入请求。无法确定真实引用时保留 unknown，不能凭空“恢复成功”。恢复入口需相应来源身份及记录修改权。
- `_v<4` 使用既有离线迁移路径；运行进程、工作区及凭据不在本迁移范围。

JSONL 导入命令默认只校验；显式 `--apply` 才写入。报告文件不含凭据：

```sh
python -m jiuwen_memory_adapter.continuity.history history.jsonl \
  --batch-id batch-1 --task-id TASK --binding-id BINDING \
  --product hermes --version SELECTED_VERSION --context-id SESSION \
  --org demo --space role-project --report /tmp/import-report.json
```

## 验收边界

仓内测试覆盖接口契约、三主体权限、持久化、重试和职责路径。正式交付仍需逐产品记录：客户端版本、工具注册/调用、认证成功与拒绝、上下文 ID 来源、持久回执、无交接恢复结果；另外验证一批真实历史与已有 JiuwenSwarm 记忆。缺少真实产品和数据时，这两项保持未完成。
