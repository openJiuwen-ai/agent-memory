# 跨 Agent 接续应用模块

规约：[S11](../../../docs/specs/S11-cross-agent-continuity.md)、[S03](../../../docs/specs/S03-control.md)。

## 模块地图

| 文件 | 职责 |
| --- | --- |
| ../continuity.py | Continuity 应用接口和 Producer；只定义契约 |
| __init__.py | 通过 import_optional 导入实现，触发 local 注册 |
| repository.py | 具名 KV 上的单记录修订及回执；单写进程锁由后端持有 |
| derivation.py | 派生构建恢复核对（计划由 service 持久化）：输出标识、按 created/reused/updated 逐项核对；写入前置能力（写后可读精确查询、期望摘要原子条件更新）以注入式回调承契约 |
| review.py | 影响传播与三档聚合复核：从变化节点沿已确认前向边有界遍历，按 required/candidate 聚合受影响引用，超界报告 unchecked_dependencies；service 以 `/continuity/changes` 变更日志 + `review_changes`/`propagate_review` 做增量失效传播 |
| service.py | 任务、事件、关系真源、视图委派与失败恢复；local 注册实现 |

## 行为铁律

- 不接收 security/identity，不执行 PEP；使用已判权读写回调及主体摘要。
- 不直接写记忆正文或图；正文走写回调，图走注入的 IndexBuilder。只有任务/操作回执属于本模块直接管理的控制数据。
- 真源先提交，再补图投影；unknown 不能按新事件重写。控制回执与修订同记录提交。
- 关系修订保留历史；自动裁决只依据同条件、已确认、带证据的 supports 保存裁决引用（verifies 不编码验证结果、不单独作为选胜依据），读取时回查验证关系头；不得仅凭写入时间或模型置信选胜者。
- 管理用旧关系读取只用于已授权复核/撤回，不用于绕过视图的源权限和依赖核验。
- 产品只连接统一写服务；single_writer 标志不替代后端排他能力，ephemeral 只用于测试。
- 投影器与视图必须使用同一 GraphStore 对象（`__init__` 以对象身份校验，不一致启动失败）。资源由装配根持有并关闭：`close()` 仅释放自身 Registry 单写锁；装配根按对象身份关闭本次创建的 Store，不关闭借用资源，关闭后同进程可重装。
- 来源治理按范围隔离：`quarantine_source`/`release_source`/`source_quarantine` 持久化 `{reason, operator, quarantined_at}`，API 门禁对普通 get/search 与角色视图/交接同时检查连接开关与动态隔离；隔离不波及其他来源。

## 与其他子目录的边界

API 负责身份、项目权限、任务负责人和条目修改权。Construction 负责候选发现、冲突算法、真源写入和图投影。Retrieval 负责有界展开。Storage 负责持久性、Scope 隔离与写者排他；生命周期由装配根关闭。

派生构建通过配置注入既有 Extractor/Dedup，规划 created/reused，并对显式新版本重建规划同源 updated。写入结果与原始事件的依赖保存在 continuity_source_refs，API 每次读取重新检查；原文提交状态与派生状态独立。生命周期通过公开 retains 回调检查活动任务和关系头依赖，不能直接读取实现私有字段。

sync.py 管理有序来源游标和 pending reservation；调用方持有 repository.lock，原文及启用的派生阶段提交后才推进。事件信封无游标时省略新增 null 字段以兼容旧摘要。

变更日志与水位分记录保存：新序号按持久日志最大值分配，重复登记修复落后水位；查询持有单写锁并从日志计算可续读游标。派生恢复按动作核对，reused 必须匹配冻结引用摘要，updated 匹配目标与更新后摘要，核对兼容历史旧批次标识；updated 写入回调同时更新正文、新批次/输出标识并清空旧 layers，三者以同次 CAS 提交。

显式版本重建的 updated 只规划同源、单段纯文本且无资产目标，结构化目标保留旧记录并规划 created；写入回调另行拒绝不满足限制的旧冻结计划。
