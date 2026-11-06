# F10 角色记忆单元测试实施清单

本清单对应 F10 §12.2；“待实现”不表示已有测试覆盖，不使用空测试、skip 或 xfail 把缺失功能计入通过数。
测试采用 pytest 函数、显式故障注入与确定性样本；并发测试用 Barrier/Event，超时测试注入时钟，不靠 sleep。

## 已有覆盖与本轮新增

- `test_role_memory.py`：单职责差异、撤回真源回查、任务重试与重启。
- `test_latest_design.py`：事件确认丢失、并发任务修订、旧记忆关联及来源条件；其中验证裁决用例随 supports 契约修正，不能把旧 verifies 行为作为新设计验收。
- `test_review_recovery.py`：partial 回执恢复、关联失败可观测、带条件支持与反证优先。
- `tests/unit/api/test_continuity_api.py`：三主体无交接恢复、来源整体隔离。
- `tests/unit/adapter/test_continuity.py`：传输错误、历史导入、schema、职责透传及批次撤回。
- 新增 `tests/unit/construction/conflict_resolver_impl/test_strategies.py`：裁决前置拒绝、同源显式纠正、模型不得选胜者。
- 新增 `tests/unit/storage/test_role_graph_contract.py`：有向多边、五维 Scope、种子与遍历、内容更新、删除隔离及 CRUD 错误。

## 待实现能力的用例与断言

| F10 差距 | 计划测试位置（tests/unit/ 下） | 场景与必须断言 |
|---|---|---|
| 1 统一构建 | control/continuity_impl/test_derivation_record.py | 零条、多条、复用结果；原文不变、来源可追踪、零条结果可提交 |
| 2 关系保护 | construction/test_relation_preservation.py | 关系记录进入 Evolver/dreaming；不得改写正文、摘要和版本 |
| 3 消解集成 | construction/test_conflict_pipeline.py | 巩固调用注入的 resolver；候选冲突不自动成为 confirmed |
| 4 Jiuwen 接续 | adapter/test_continuity.py | sync_turn 返回持久回执，prefetch 保留路径及 gaps；旧模式回归 |
| 5 产品转换 | adapter/test_history_formats.py | 各产品脱敏样本保留作者、事件 ID、时间；重复导入不增独立来源，畸形输入拒绝 |
| 6 恢复工具 | adapter/test_continuity.py | schema 与转发包含恢复入口，禁止冒用连接及越权恢复 |
| 7 认证 | api/test_continuity_api.py | 缺凭据、错误凭据、伪造连接、跨项目请求均拒绝；不使用开发身份代替生产认证 |
| 8 增量同步 | F10 §8.9 协议已定义 | 游标、源内顺序、`event_id+摘要` 幂等冲突与重放、断点/unknown 边界、双写切换水位已定；乱序/断点恢复的断言待实现（协议级单测：游标单调推进、同 id 同内容幂等、同 id 异内容冲突、unknown 停止） |
| 9 存量 | adapter/test_history_formats.py | 原 Scope 不变，无 duty 旧记忆可作为背景，摘要变化拒绝；真实历史另做集成验收 |
| 10 经验状态 | retrieval/role_view_impl/test_experience_states.py | 支持、反例、撤回、修订、停用驱动状态变化；条件不可丢失 |
| 11 memory tree | 无必需新增测试 | 树结构已存在；角色接续未使用，不作为本轮未实现功能 |
| 12 索引补建 | construction/index_builder_impl/test_rebuild.py | 替身证明 RETRIEVAL_ONLY 不重抽/不写正文；真实后端证明索引可检索 |
| 13 收益 | 非单元测试 | 真实轨迹、标注成本、证据召回与延迟放 evaluation，不断言模型收益必然提升 |
| 14 复核传播 | control/continuity_impl/test_review_impact.py | 只影响依赖子图、三档聚合保留引用、超出传播范围报告缺口 |
| 15 来源治理 | api/test_continuity_governance.py | 指定范围隔离不波及其他来源；普通 get/search 与角色视图都阻断派生泄漏；发布验证权限与审计 |
| 16 证据保留 | control/continuity_impl/test_evidence_retention.py | 活动引用阻止清理；解除引用后按原生命周期处理 |
| 17 覆盖清单 | control/continuity_impl/test_relation_coverage.py | 部分构建不推进完整水位，补建只更新已完成范围，遍历结束不代表覆盖完整 |
| 18 多职责 | retrieval/role_view_impl/test_multi_duty.py | 见下文多职责场景 |
| 19 验证修复 | control/continuity_impl/test_latest_design.py | 用 supports 替换旧选胜前提；失败 verifies 不选胜、条件不同不选胜、依据撤回裁决失效 |
| 20 派生恢复 | control/continuity_impl/test_derivation.py | 见下文故障矩阵；依赖真源精确查询与原子条件更新的公开能力 |
| 21 生命周期 | api/test_continuity_lifecycle.py | 公开 runtime.close 后可重装；资源按身份仅关闭一次，失败释放单写锁，借用资源不关闭 |
| 22 装配 | control/continuity_impl/test_assembly.py | 同名不同对象拒绝；按能力拒绝部分构建器；替换子插件无需核心分支；缺能力启动失败 |
| 23 Neo4j | storage/test_neo4j_graph_store.py | 复用图契约，驱动替身测试编解码、Scope 参数、错误映射与关闭；不声称替身证明事务 |
| 24 条件投影 | construction/index_builder_impl/test_conditional_projection.py | 同修订同摘要幂等，同修订异摘要冲突；撤回 3 后重放 2 不复活；旧修订不覆盖新修订 |

## 派生恢复故障矩阵

1. 模型完成、计划尚未保存：无派生写入，恢复可重新生成。
2. 并发保存计划：只冻结一份，失败竞争者采用已存计划。
3. 已冻结计划：恢复不再调用模型，用会返回另一结果的替身验证。
4. created 写成但回执丢失：精确查询采用既有记录，只保留一份。
5. 两个 created 重试并发：用屏障制造竞争，输出只创建一次。
6. 相同输出标识、不同摘要：partial 冲突，原记录不覆盖。
7. reused 重试：来源关联集合不增加重复项，来源身份不等同于独立证据。
8. updated 当前摘要为前摘要/后摘要/第三方摘要：分别条件更新/采用/冲突。
9. 核对后第三方修改：CAS 拒绝，不产生丢失更新。
10. 缺精确查询能力拒绝启用，缺 CAS 时 updated 转 partial。
11. derivation 核对入口不创建或更新业务正文；完成状态和 derived 必须与计划一致。

## 多职责场景

- 单职责兼容、列表去重、未知职责拒绝、超过四个拒绝。
- A 一跳、B 三跳：第三跳只归 B，第一跳不因 off-by-one 被拒绝。
- A 边接 B 边：跨职责路径返回，valid_duties 为空。
- 菱形图同节点不同职责到达：正文一份，路径与归属保留。
- 同节点长短路径：较短路径剩余预算可继续探索，不被先访问状态阻止。
- 环与路径爆炸：max_paths、单节点状态、深度均有界，截断显式报告。
- B<k：未获预算职责 not_started；B>=k 且有合法边与足够其他预算时，各职责获得探索机会。
- 共享边计费一次、覆盖归属多方；共同背景/反证/风险/gaps 不随职责过滤消失。
- 无权端点不可穿过，错误不得被当空图；测试答案手写，不使用被测算法生成 oracle。

## 真实集成验收（不计入单元测试通过）

真实 Neo4j 验证约束、事务、确认丢失、断连、撤回重放及关闭重装；产品 G0 验证真实认证与工具交互；
真实存量验证迁移与索引补建；收益/成本由 evaluation 验证。未提供环境时明确记未验证，不以跳过算通过。

## 已验证边界

派生构建公开入口与恢复：test_derivation_record.py、api/test_continuity_governance.py；包括冻结计划、确认丢失、并发重试、原文权限撤销。当前内置计划只生成 created/reused，updated 的 CAS 核对是注入契约测试，不表示生产 DomainStore 已具备原子条件更新。

视图内依赖传播与 required/candidate/unchecked 聚合由 retrieval/role_view_impl/test_experience_states.py 覆盖；全图变更驱动传播仍未验收。Neo4j 真后端入口是 tests/integration/storage/test_role_neo4j.py，配置 F10_NEO4J_URI、F10_NEO4J_USER、F10_NEO4J_PASSWORD；缺环境跳过不计通过。产品原生导出样本、持续同步与 G0 仍需独立验收。

补充：test_sync.py 通过 role_record 验证单调游标、乱序拒绝、unknown 边界、重建 repository 后恢复；storage/test_conditional_update.py 使用两个独立 SQLite 连接验证 CAS 竞争；construction/index_builder_impl/test_rebuild.py 验证 SQLite 正文不变与检索投影补建（检索后端仍为测试实现）。
