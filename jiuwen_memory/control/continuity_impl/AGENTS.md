# 跨 Agent 接续应用模块

规约：[S11](../../../docs/specs/S11-cross-agent-continuity.md)、[S03](../../../docs/specs/S03-control.md)。

## 模块地图

| 文件 | 职责 |
| --- | --- |
| ../continuity.py | Continuity 应用接口和 Producer；只定义契约 |
| __init__.py | 通过 import_optional 导入实现，触发 local 注册 |
| repository.py | 具名 KV 上的单记录修订及回执；单写进程锁由后端持有 |
| projection.py | 关系→图投影端口：`RelationProjector.project(scope, relation, revision)`；`IndexBuilderRelationProjector` 是唯一把关系包装成 MemoryUnit 的适配器，service 不再伪造条目 |
| derivation.py | 派生构建恢复核对（计划由 service 持久化）：输出标识、按 created / reused 逐项核对；写入前置能力（写后可读精确查询）以注入式回调承契约 |
| service.py | 任务、事件、关系真源、视图委派与失败恢复；local 注册实现 |

## 行为铁律

- 不接收 security/identity，不执行 PEP；使用已判权读写回调及主体摘要。
- 不直接写记忆正文或图；正文走写回调，图走注入的 IndexBuilder。只有任务/操作回执属于本模块直接管理的控制数据。
- 真源先提交，再补图投影；unknown 不能按新事件重写。控制回执与修订同记录提交。事件回执的幂等摘要覆盖整个请求（正文、来源、资产、标签、metadata、时间），指纹同时写进 `continuity_envelope` 以便从落盘记录复算。幂等键只由 `event_key(connector, binding, event)` 计算，其他层不得复制该算法。
- 批量写入顺序：`record_batch` 先把待写事件登记为 `status=unknown` 并落盘，**再**批量写正文，之后逐条转 `partial`、每任务追加一次背景并加一次修订号；已有回执的事件永不重写正文，无 refs 的停在 unknown 等 `recover`。
- 关系可用性：`connectors` 由装配作为构造参数注入（启用 + 项目覆盖 + 未隔离，隔离判断复用 `source_quarantine`）；配置缺失时对带连接的关系 fail closed；`resolve_edge`/`repair` 在解析前检查，不可用按「关系不存在」处理，不对外区分原因。`view_result` / `continuity_view_result` 共用 `_view_entry` + `_finalize_view`：条目统一为完整 S11 标注（`kind`/`status`/`discovery`/`required`/`classification_confidence`/`evidence_refs`/`conditions`/`duty_ids`/`valid_duties`/`common`/`path_ids`，缺值给空值不省略；角色档原样取构建器算出的值，只去掉条目级 `id`/`text`；`kind`/`status` 映射由 `common.type_def.continuity.view_kind`/`view_status` 两档共用；条目必需标记只来自必需背景与显式 anchor），按 background → anchor → relation → semantic_candidate 排序后再按 `top_k` 截断（溢出追加 `budget_exhausted`），两档都回 `meta.mode`；API 层只做类型转换与携带底层检索的 `errors`。
- 关系修订保留历史；自动裁决只依据同条件、已确认、带证据的 supports 保存裁决引用（verifies 不编码验证结果、不单独作为选胜依据），读取时回查验证关系头；不得仅凭写入时间或模型置信选胜者。
- 管理用旧关系读取只用于已授权复核/撤回，不用于绕过视图的源权限和依赖核验。
- 产品只连接统一写服务；single_writer 标志不替代后端排他能力，ephemeral 只用于测试。
- 投影器与视图必须使用同一 GraphStore 对象（`__init__` 以对象身份校验，不一致启动失败）。资源由装配根持有并关闭：`close()` 仅释放自身 Registry 单写锁；装配根按对象身份关闭本次创建的 Store，不关闭借用资源，关闭后同进程可重装。
- 两档模式：`mode=continuity` 不装配图、视图与关系组件，构造时不得要求 `supports_incident_edges`；`mode=role` 缺图或视图时装配失败。关系入口在接续模式返回 UnsupportedCapabilityError；`health()` 返回 `{continuity_mode}`；`task_background()` 是两档共用能力，接续模式据此恢复任务背景（不展开关系）；它返回 `{units, required_ids, gaps}`，任务缺失/背景不可读分别以缺口上报并带存储的 `required` 标记，不静默返回空。契约默认实现抛 `UnsupportedCapabilityError`。
- 关系真源：存 Registry KV 的关系头 `/continuity/relations/<id>`（`{revision, digest, payload_digest, status, connector, current}`）与不可变修订 `/continuity/relations/<id>/rev/<n>`，不写 MemoryUnit；图边属性存 relation_id 与 revision。并发只依赖单写者与 expected_revision，不依赖 KV CAS。存量迁移见 `scripts/migrate_relations_to_kv.py`。
- 批量接续写入：`batch_add` 中相邻、同项目的接续项合成一次 `record_batch`（事件幂等键与单条一致）；契约 `Continuity.record_batch` 的默认实现逐条回退到 `record`，新插件不实现也能工作；批量结果逐项给出 `operation_id` / `continuity_status`。混合批次按**输入顺序**推进：相邻普通项合成一批，遇接续项先冲刷再执行它；失败后剩余项标 skipped，`continue_on_error=False` 时不产生任何后续写入。
- 来源治理按范围隔离：`quarantine_source`/`release_source`/`source_quarantine` 持久化 `{reason, operator, quarantined_at}`，API 门禁对普通 get/search 与角色视图/交接同时检查连接开关与动态隔离；隔离不波及其他来源。`list` 以不可用连接集合作为系统过滤谓词下推、分页前生效。

## 与其他子目录的边界

API 负责身份、项目权限、任务负责人和条目修改权。Construction 负责候选发现、冲突算法、真源写入和图投影。Retrieval 负责有界展开。Storage 负责持久性、Scope 隔离与写者排他；生命周期由装配根关闭。

派生构建通过配置注入既有 Extractor/Dedup，规划 created/reused。写入结果与原始事件的依赖保存在 continuity_source_refs，API 每次读取重新检查；原文提交状态与派生状态独立。生命周期通过公开 retains 回调检查活动任务和关系头依赖，不能直接读取实现私有字段。

派生恢复按动作核对：created 按 output_key 采纳，reused 必须匹配冻结引用摘要。关系投影失败或状态不明时从 Registry 关系头按最新修订重建。
