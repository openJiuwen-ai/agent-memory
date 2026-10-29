# 跨 Agent 接续应用模块

规约：[S10](../../../docs/specs/S10-cross-agent-continuity.md)、[S03](../../../docs/specs/S03-control.md)。

## 模块地图

| 文件 | 职责 |
| --- | --- |
| base.py | Continuity 应用接口和 Producer；只定义契约 |
| repository.py | 具名 KV 上的单记录修订及回执；单写进程锁由后端持有 |
| service.py | 任务、事件、关系真源、视图委派与失败恢复；local 注册实现 |

## 行为铁律

- 不接收 security/identity，不执行 PEP；使用已判权读写回调及主体摘要。
- 不直接写记忆正文或图；正文走写回调，图走注入的 IndexBuilder。只有任务/操作回执属于本模块直接管理的控制数据。
- 真源先提交，再补图投影；unknown 不能按新事件重写。控制回执与修订同记录提交。
- 关系修订保留历史；自动裁决保存验证引用，读取时回查验证关系头。不得仅凭写入时间或模型置信选胜者。
- 管理用旧关系读取只用于已授权复核/撤回，不用于绕过视图的源权限和依赖核验。
- 产品只连接统一写服务；single_writer 标志不替代后端排他能力，ephemeral 只用于测试。

## 与其他子目录的边界

API 负责身份、项目权限、任务负责人和条目修改权。Construction 负责候选发现、冲突算法、真源写入和图投影。Retrieval 负责有界展开。Storage 负责持久性、Scope 隔离与写者排他；生命周期由装配根关闭。
