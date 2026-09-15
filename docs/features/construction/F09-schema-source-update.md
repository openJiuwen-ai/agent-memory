# Schema source 更新与实体索引一致性

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-09-15 |
| 影响范围 | construction、control、api；S02、S03、S05 |
| 依据 | 208-update-design-v3.md |
| 验证基线 | 提交范围回归 851 passed、1 skipped；含本地新增用例的回归 955 passed、3 skipped；更新回归 86 passed、2 skipped；主线隔离回归 18 passed |
| Refs | #208 |

## 背景

原 update 直接应用 patch 并刷新索引，正文更换人物时仍携带旧 entities。索引构建器只消费已有 entities，因此正文由“陈静负责推荐算法迭代”变成“李红负责推荐算法迭代”后，仍可能挂在陈静的实体反向索引中。Schema property 也没有随 source 的显式更正协调更新。

## 决策

仅对当前 pipeline 支持 Schema 更新、目标明确是 Schema source、且传入正文发生变化的操作启用新分支。普通记忆、直接改 property、仅改标签/元数据/时间和正文相同的更新保持原协议。实体索引开关不参与 Schema 能力判断。

主线隔离检查发现，无条件执行准备阶段会让普通 API update 多读两次真源，并在 CloudEngine 中重复复制 patch、解析路由。因此 API 和 Engine 先在已读出的 unit 上做无 I/O 的候选判断，普通记忆直接进入原更新路径。实体索引的严格错误反馈通过 ContextVar 限定到 Schema 提交，并在成功或失败时恢复，普通写入继续沿用增强索引失败只记日志的行为。

先完整抽取、匹配并形成冻结动作，再逐条校验涉及记录的 UPDATE、WRITE、DELETE 权限，最后提交。更新专用抽取使用完整 Schema 和当前新正文，不附加历史上下文、不执行常规演进持久化。它要求模型明确给出完整响应和未解决撤销列表；部分合法结果、缺字段、截断、解析失败和无法可靠表达的撤销均报错。可表达的否定保存为明确否定事实。合法空结果可以清空旧实体。

source 的 entities 整体替换，property 名不写入实体列表；没有 property 的合法实体也保留。这里只刷新当前 source 的实体关联，保留同实体关联的其他记忆。成功后不改原始 /messages 对话缓存。

property 在当前 source 的同 scope 关联集合内，按 Schema、实体类型、实体名和属性名定位。优先匹配相同事实及事件时间，再接受同定位一旧一新的改值；多条候选存在歧义时拒绝。换人物不凭相同属性名建立版本关系。

明确对应的多源 property 以本次更正为准，新值不继承不能支持它的其他来源。事实未变时保留其他有效来源。新正文未提及的多源 property 仅撤回当前来源，保留其他来源支持的内容；不推断剩余证据是否充分。

OVERWRITE 保持对应 source/property 的 ID，新增 property 使用新 ID，完全撤回的单源 property 删除本体与索引。SUPERSEDE 分别生成新 source/property ID，并建立各自的 supersedes；旧版保留原 provenance，关闭有效期。没有继任者的旧 property 仅关闭有效期，不伪造 supersedes。失效区间不可倒置，旧版已有更早失效边界不延长；仅由新 source 支持的属性不超出该 source 的失效边界。

Schema source 记录所用 Schema 名和版本。已有数据缺此记录时，关联 property 的配置也必须与所选配置一致。跨 pipeline 更新沿用旧 Schema 解释正文，目标 pipeline 必须支持相同 Schema，索引按各记录的新旧路由迁移；不把一次正文修改当作 Schema 迁移工具。

旧证据防回放同时检查 source 输入和候选 ADD 入口。更新记录关联旧 source ID、正文及修订标记；多源更正额外记录对应属性的旧来源修订。不同属性及真正的新证据仍可写入。每次显式更新分配新的 source 修订标记，区分 A→B→A 的最新 A 与原始 A 的回放。

提交前冻结操作 ID、候选 ID、前后快照和全部动作，写入私有恢复记录；每步成功后保存进度。提交时重读输入，发现已变化则拒绝；索引失败通过 PartialFailureError 暴露，同一目标和 patch 重试复用冻结动作。实体索引在此操作中启用严格失败反馈，正常 add 的错误策略保持原样。完成后压缩恢复记录，移除正文快照，留下 ID、哈希和防回放关系；API 审计记录操作 ID 和涉及的 unit ID。

## 拒绝的方案

- 直接重跑完整 evolve：会重新保存消息、追加独立 property、执行上下文抽取，无法表达对应记录的覆盖与版本关系。
- 把 entity_enabled 当成 Schema 开关：会让普通记忆进入未经配置的 Schema 路径。
- 新旧 entities 合并：旧人物无法解绑，#208 仍存在。
- 多源全部拒绝或把其他来源强绑到新值：分别阻断已确认的显式修改规则、制造错误证据。
- 部分抽取成功就撤销未出现属性：不能区分业务删除与模型漏项。
- 只写“显式修改”标志：后续 Schema ADD 不读取它时，旧证据仍可重新生成冲突 property。
- 用进程锁宣称云端事务：本次未引入跨进程互斥或跨后端事务能力。

## 验证

本次 PR 保留现有 Schema 测试存储的 `scan()` 接口适配；为缩小评审范围，新增更新回归、主线隔离回归及其夹具保留在本地，不随 PR 提交。以下结果包含这些本地用例，并不表示仅检出 PR 即可复现全部 955 项通过结果。

排除两份本地新增测试后，同一组 construction/control/api 回归为 851 passed、1 skipped。该结果对应本次提交范围内的测试；跳过项是仓库默认禁用的真实 Redis 双实例用例。

本地更新回归使用真实 API、本地/云端 Engine、Schema normalizer、KV、全文/向量索引和实体链接服务，仅模型回复与外部实体存储使用确定性替身。

覆盖 #208 的陈静→李红原始正文案例，并加入“陈静上周评审了召回方案的设计文档”作为共享实体哨兵；在语料数量大于 top_k 时检查新旧检索，并检查实体哈希、关联 ID 和消息缓存。另覆盖两模式、多源改值/省略、版本回溯、负向事实、完整空结果、歧义、权限、跨 pipeline 迁移、配置失效、截断/超时、提交中断/重建协调器重试、完成后重试和正文改回旧值。

包含本地新增用例的相关模块回归命令：

```powershell
.venv/Scripts/python.exe -X utf8 -m pytest tests/unit/construction tests/unit/control tests/unit/api --ignore=tests/unit/control/test_middle_e2e_real_llm.py -o addopts=-ra -q
```

真实 LLM 用例需要外部服务与额外依赖，未作为确定性单测执行。双 Redis 实例用例沿用仓库默认跳过；本地 Engine 不执行云端专属迁移用例。修改过的 Python 文件通过 ruff check。

本地另有 18 个主线隔离用例，使用
schema_enabled=false 的实际装配，覆盖两 Engine、两 mode、实体索引开/关、普通 add、
API 与 Engine 直接 update、真源读取/路由/索引调用次数，以及实体后端异常、部分失败和
Schema 异常之后普通写入的失败策略。

## 已知遗留

- 沿用现有跨后端顺序写入与并发边界。恢复日志提供可重试流程，不提供跨 KV、全文、向量、实体存储的原子回滚或跨进程隔离；部分失败期间读者可能看到部分结果。
- 未完成操作需使用相同目标、patch 和相容配置重试。同 scope 存在未完成的 Schema 更新时，其他 Schema 正文更新先报冲突；没有后台自动恢复任务。
- 恢复可靠性取决于所注入 KV 的持久性；纯内存部署的进程退出不会保留状态。
- 防回放只覆盖可识别的旧 source 修订；把旧正文换成全新 ID 重新导入，或历史数据丢失修订信息，不保证识别为旧证据。
- 当前按 scope 扫描关联 property 和恢复关系。大规模部署后可增加专用反向索引；本次不宣称完成性能优化。
- 不处理普通记忆实体刷新、全库旧脏数据回填、property 反向改 source/对话、通用人工修订保护、递归重建所有下游派生记忆、多源证据充分性推理。
