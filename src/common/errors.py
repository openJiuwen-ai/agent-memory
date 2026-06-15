"""跨层共用异常类型：接口错误契约的一部分（横切，所有层共享）。

各层接口的 docstring 以「报冲突 / 报缺失 / 不存在时抛出异常 / 报错拒绝」
描述失败语义，这些语义统一由本模块的异常承载：调用方可**跨后端、跨层**
用同一套异常捕获，而不依赖任何具体实现自带的异常（如某向量库的报错）。
异常即接口的一部分——实现负责抛出对应类型，调用方据类型分流处理。

层级：所有异常继承 :class:`BlinkMemError`（命名避开 Python 内置的
``MemoryError``），调用方可用它一把兜底；各子类对应一类可区分处理的失败。
"""

from __future__ import annotations


class BlinkMemError(Exception):
    """所有记忆系统异常的根类（便于调用方统一兜底）。"""


class NotFoundError(BlinkMemError):
    """目标实体/记录/键不存在：对应各接口的「报缺失」「不存在时抛出异常」
    （get/update/inspect/trace 等点定位失败）。"""

    def __init__(self, entity: str = "", key: str = "", message: str = "") -> None:
        self.entity = entity  # 实体类型：memory_unit / document / node / key ...
        self.key = key  # 缺失的 id / 键
        super().__init__(message or f"{entity or 'entity'} not found: {key!r}")


class ConflictError(BlinkMemError):
    """目标已存在、与现有记录冲突：对应各 Store ``insert`` 的「id 已存在时报冲突」。"""

    def __init__(self, entity: str = "", key: str = "", message: str = "") -> None:
        self.entity = entity  # 实体类型
        self.key = key  # 冲突的 id / 键
        super().__init__(message or f"{entity or 'entity'} already exists: {key!r}")


class PermissionDeniedError(BlinkMemError):
    """actor 无权对 target scope 执行该 action：API 层鉴权
    （``PermissionManager.check``）未通过时抛出。"""

    def __init__(self, action: str = "", message: str = "") -> None:
        self.action = action  # 被拒的动作：read / write / update / delete / share ...
        super().__init__(message or f"permission denied: {action or 'action'}")


class ValidationError(BlinkMemError):
    """入参非法或不满足约束：如 ``DeleteSelector`` 未给任何条件、参数越界、
    向量维度与索引不一致等。"""


class PolicyError(BlinkMemError):
    """运行时策略操作被拒：键未知，或试图修改不可变配置
    （``admin_set`` / ``PolicyManager.set`` 的「不可变配置报错拒绝」）。"""


class HealthCheckError(BlinkMemError):
    """``health()`` 存活探测失败：底层后端/插件不可用或未就绪。"""


class BackendError(BlinkMemError):
    """底层存储/插件后端的非预期失败（网络、IO、远端报错等），区别于上述
    可预期的业务异常。"""
