# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""跨层共用异常类型：接口错误契约的一部分（横切，所有层共享）。

各层接口的 docstring 以「报冲突 / 报缺失 / 不存在时抛出异常 / 报错拒绝」
描述失败语义，这些语义统一由本模块的异常承载：调用方可**跨后端、跨层**
用同一套异常捕获，而不依赖任何具体实现自带的异常（如某向量库的报错）。
异常即接口的一部分——实现负责抛出对应类型，调用方据类型分流处理。

层级：所有异常继承 :class:`AgentMemoryError`（命名避开 Python 内置的
``MemoryError``），调用方可用它一把兜底；各子类对应一类可区分处理的失败。
"""

from __future__ import annotations

import re

_ERROR_CREDENTIAL_RE = re.compile(r"//[^:/@\s]*:[^@\s]+@")
_ERROR_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bauthorization\b['\"]?\s*[:=]\s*['\"]?(?:bearer|basic)\s+)[^'\",\s;&]+"
)
_ERROR_AUTH_VALUE_RE = re.compile(
    r"(?i)(\bauthorization\b['\"]?\s*[:=]\s*['\"]?)(?!(?:bearer|basic)\s+)[^'\",\s;&]+"
)
_ERROR_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\b\s*[:=]\s*[^,;&]+"
)


def safe_error_message(exc: Exception, *, limit: int = 200) -> str:
    """规范化并脱敏可进入日志或结构化错误的异常文本。"""
    text = " ".join(str(exc).split())
    text = _ERROR_CREDENTIAL_RE.sub("//<redacted>:<redacted>@", text)
    text = _ERROR_AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _ERROR_AUTH_VALUE_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _ERROR_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[:limit]


class AgentMemoryError(Exception):
    """所有记忆系统异常的根类（便于调用方统一兜底）。"""


class NotFoundError(AgentMemoryError):
    """
    目标实体/记录/键不存在：对应各接口的「报缺失」「不存在时抛出异常」
    （get/update/inspect/trace 等点定位失败）。
    """

    def __init__(self, entity: str = "", key: str = "", message: str = "") -> None:
        self.entity = entity  # 实体类型：memory_unit / document / node / key ...
        self.key = key  # 缺失的 id / 键
        super().__init__(message or f"{entity or 'entity'} not found: {key!r}")


class ConflictError(AgentMemoryError):
    """目标已存在、与现有记录冲突：对应各 Store ``insert`` 的「id 已存在时报冲突」。"""

    def __init__(self, entity: str = "", key: str = "", message: str = "") -> None:
        self.entity = entity  # 实体类型
        self.key = key  # 冲突的 id / 键
        super().__init__(message or f"{entity or 'entity'} already exists: {key!r}")


class PermissionDeniedError(AgentMemoryError):
    """
    actor 无权对 target scope 执行该 action：API 层鉴权
    （``PermissionManager.check``）未通过时抛出。
    """

    def __init__(self, action: str = "", message: str = "") -> None:
        self.action = action  # 被拒的动作：read / write / update / delete / share ...
        super().__init__(message or f"permission denied: {action or 'action'}")


class AuthenticationError(AgentMemoryError):
    """
    凭据缺失、格式非法或校验不通过：认证能力（``jiuwen_memory/common/security/authentication/``）产出。

    与 :class:`PermissionDeniedError` 的区别是「不知道你是谁」（401）对
    「知道你是谁但不许做」（403）——两者必须可分，否则 HTTP 层无法映射
    正确状态码，调用方也无法区分「该带凭据」与「该申请授权」。

    对外错误消息一律笼统，不区分「主体不存在」与「凭据错误」：区分了就
    成为主体枚举的侧信道。具体原因写进审计事件的 ``detail``。
    """


class RateLimitedError(AgentMemoryError):
    """
    调用方超出速率上限：资源保护（``jiuwen_memory/common/security/protection/``）产出。

    与 :class:`AuthenticationError` 必须可分（429 对 401）：限流发生在认证
    **之前**，此时还不知道凭据对不对——把它报成 401 会让「你被限流了」和
    「你的 key 错了」混在一起，运维排障时无法区分，客户端也不知道该重试
    还是该换凭据。

    对外消息同样笼统：不透露桶容量、剩余令牌、已计数的请求数——那些都能
    用来反推限流参数并贴着阈值发请求。
    """


class ValidationError(AgentMemoryError):
    """
    入参非法或不满足约束：如 ``DeleteSelector`` 未给任何条件、参数越界、
    向量维度与索引不一致等。
    """


class PolicyError(AgentMemoryError):
    """
    运行时策略操作被拒：键未知，或试图修改不可变配置
    （``admin_set`` / ``PolicyManager.set`` 的「不可变配置报错拒绝」）。
    """


class HealthCheckError(AgentMemoryError):
    """``health()`` 存活探测失败：底层后端/插件不可用或未就绪。"""


class BackendError(AgentMemoryError):
    """
    底层存储/插件后端的非预期失败（网络、IO、远端报错等），区别于上述
    可预期的业务异常。
    """


class UnsupportedCapabilityError(AgentMemoryError):
    """组件不支持调用方请求的某项能力。"""

    def __init__(
        self,
        capability: str,
        value: str,
        component: str,
        message: str = "",
    ) -> None:
        """记录不受支持的能力、请求值和组件名。"""
        self.capability = capability
        self.value = value
        self.component = component
        super().__init__(
            message
            or f"{component or 'component'!r} does not support "
            f"{capability or 'capability'} {value!r}"
        )


class UnsupportedStorageCapabilityError(AgentMemoryError):
    """Storage 未声明调用方请求的底层端口能力。"""


class StorageRetrievalError(AgentMemoryError):
    """所有选中召回入口均失败。"""

    def __init__(self, errors: list[object]) -> None:
        self.errors = errors
        super().__init__(f"all selected retrieval sources failed: {len(errors)} error(s)")


class PartialFailureError(AgentMemoryError):
    """多步骤操作部分成功：不得报告为完整成功，调用方应按 retry_action 重试。"""

    def __init__(
        self,
        *,
        completed: tuple[str, ...],
        failed: str,
        retry_action: str,
        message: str = "",
    ) -> None:
        self.completed = completed
        self.failed = failed
        self.retry_action = retry_action
        super().__init__(
            message
            or (
                f"{failed} failed after {', '.join(completed)}; "
                f"retry {retry_action}"
            )
        )
