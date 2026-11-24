# F09 — Team 场景文档记忆

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-20 |
| 影响范围 | JiuwenSwarm Adapter、文档读改、Markdown、Watchdog 与 Shadow |
| 设计依据 | D-20261113-2017-jiuwen-memory-team-upgrade、[F10 群体记忆](F10-collective-memory.md) |

## 背景

常规 Work/Code 集群和专家团共用 Team 文档场景。F09 原有项目日记读改、项目和团队文档预取兜底接入 F10 的授权与存储规则。成员治理、分享、落点和跨成员召回规则见 F10。

## 决策

- 宿主创建 Team Provider，绑定稳定身份和项目、Team、Group 坐标。SDK 和 HTTP 共用对外工具行为。SDK 按文档根复用 Runtime、Watchdog 和 Shadow，每个 Provider 使用自己的请求绑定。HTTP 由服务端配置模型、文档根、索引和看门狗。
- Team 共用四个工具：`team_memory_search`、`team_memory_write`、`team_memory_read` 和 `team_memory_edit`。显式提炼交给群体抽取。工具请求携带文档目标、文件名和正文范围，可信坐标由宿主提供。
- `USER.md` 按用户归属读取和定位修订块。同一 org、space、user 和 session 内共享画像，修订沿用 SUPERSEDE 保留原归属。普通 Work/Code 的显式搜索和自动预取补充宿主绑定项目下已授权的项目条目。项目支持群体长期文档 `MEMORY.md` 和沿用的项目日期文档 `YYYY-MM-DD.md`。Team、Group 和个人经验支持各自的 `MEMORY.md`。项目日记复用日期文件和 Shadow 索引。Team 的日期入口读取群体项目日记和已有的普通项目日记。
- 预取并行读取用户画像、项目与团队兜底文档及语义召回。项目与团队分别取语义命中，各自的搜索为空或失败时使用对应的已授权文档。某一维度已有语义命中时该维度采用命中内容。Group 和个人经验使用语义召回。后台回合只预取用户画像。
- 文档读改按可信坐标和文档授权定位条目。编辑唯一匹配的正文段，使用 `SUPERSEDE` 记录版本关系，保留原作者、Scope、维度及分享状态。实际发起编辑的人记录在审计里。底层持锁重新校验原块和版本一致性。
- Watchdog 沿用原有文件事件和增量对账。群体文档的目录规则可反查落点，并与 API 写入共用归属字段默认值。直接编辑磁盘时，Watchdog 按路径和同文件的既有 Scope 恢复能确定的字段。

## 方案取舍

群体长期文档和群体项目日记都置于 `memory/team_memory` 下，按 F10 的稳定维度定位。独立的文件路径使普通项目日记入口只处理自己的日记。复用 Markdown 与 Shadow 以及 SDK Runtime 持有者，使同一根目录的文件监听和索引协调集中处理。

## 验证

Team Provider 和真实 HTTP 回归覆盖项目日记、授权文档预取兜底、共享文档正文修订及撤权。配置一致性用例覆盖 SDK 与服务端示例，Core 与 Swarm 回归覆盖挂载、结束排空及实际的 Team 文档落盘。定向验证使用离线模型或模型替身。

## 已知遗留

- 文件由磁盘直接修改时，重建可能丢失无法从路径或原文件推导的业务元数据；API 编辑保留这些字段。
- 同一会话内切项目的限制及成员撤权失败后的补偿，见 [F10 已知遗留](F10-collective-memory.md#已知遗留)。
