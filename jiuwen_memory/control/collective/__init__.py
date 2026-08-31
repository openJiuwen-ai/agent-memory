"""群体记忆的控制层纯逻辑（F07「多空间读写」「结论直写」）。

三个模块，一个共同点：都不是算子——无 Producer 注册、实现不可替换、不访问存储与模型，
因此不进工厂，也不由 :func:`control.bootstrap.register_controllers` 触发注册。

| 模块 | 内容 | 与鉴权点的关系 |
|---|---|---|
| :mod:`.write_targets` | 写入候选空间集合的渲染、排序、截断与取交
  | 收 ``can_write`` 回调，不收 ``identity``；不持有 ``PermissionManager``，
    也不抛权限异常 |
| :mod:`.routing` | 结论直写路径的归属判定调用与结果归一
  | 收 API 层判权后给出的成品候选集，不向上索要判权函数 |
| :mod:`.cross_space_recall` | 跨空间召回的取数摊配、扇出与合并
  | 收 ``recall`` 回调与已判权的空间目标，不做任何裁决；扇出失败单独返回，
    不并进 ``merged.errors`` |

三者的共同形态：**裁决留 PEP，裁决之后的机械换算与 I/O 编排落本层**，上下之间经回调
（``can_write`` / ``recall``）或成品数据（``RouteContext.candidates`` /
``SpaceRecallTarget``）衔接，本层一律不收 ``identity``。

**为什么收在子包而不放控制层顶层**：``control/AGENTS.md``「文件关系」第一条要求顶层
``.py`` 只定义抽象接口、零实现逻辑。三个模块都带实现，逐个放顶层会使该约定名存实亡。

消费方统一写 ``from jiuwen_memory.control import collective``，取本模块再导出的名字，
不深入子模块——两种写法并存时同一个函数会有两条引用路径。
"""

from __future__ import annotations

from .cross_space_recall import RecallCallback, SpaceRecallTarget, recall_spaces
from .routing import decision_metadata, route_many
from .write_targets import SPACE_FANOUT_LIMIT, plan_write_targets

__all__ = [
    "SPACE_FANOUT_LIMIT",
    "RecallCallback",
    "SpaceRecallTarget",
    "decision_metadata",
    "plan_write_targets",
    "recall_spaces",
    "route_many",
]
