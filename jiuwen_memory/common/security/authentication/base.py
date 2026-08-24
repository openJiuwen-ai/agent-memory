"""认证能力契约：Authenticator 与注册式 Producer（F05 §Authentication）。

把一次请求的凭据材料校验成 :class:`~common.security.types.AuthContext`。
实现按认证模式区分（dev / trusted / api_key），由配置在装配期选定；运行期
不再分流——参考 demo 里 ``AuthDispatcher`` 一个类里 if/else 三种模式的写法，
在此拆成三个各自只做一件事的实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.types import AuthContext, Credentials


class AuthProducer(Factory):
    """Authenticator 的注册式工厂（与契约同处接口层）。

    ``target`` 即认证模式名。各实现在 ``authentication_impl`` 下以
    ``@AuthProducer.register("<模式>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`common.security.bootstrap.register_security` 统一触发。
    """

    TOP_NAME = "authenticator"


class Authenticator(ABC):
    """凭据 → 可信身份。"""

    @abstractmethod
    def authenticate(self, credentials: Credentials) -> AuthContext:
        """校验凭据，返回可信身份；失败抛 :class:`~common.errors.AuthenticationError`。

        **不返回 None**：认证只有「成功」与「失败」两种结果。返回 None 会诱导
        调用方写 ``if ctx is None: ctx = default`` 这类 fail-open 分支。

        对外错误消息一律笼统（``"authentication failed"``），不区分「凭据缺失」
        「主体不存在」「凭据错误」——区分即主体枚举侧信道（§2.3.2）。具体原因
        写进审计事件的 ``detail``，不进异常消息。
        """

    @abstractmethod
    def mode(self) -> str:
        """自描述当前认证模式名，供审计与诊断展示。

        返回开放字符串而不是封闭枚举：第三方实现无需修改核心即可声明自己的模式名。
        核心不得按此值分支——需要分支的行为差异由 capability 方法显式声明。
        """

    def requires_loopback_binding(self) -> bool:
        """是否必须只监听 loopback。

        未覆写时返回 ``True``（fail closed）：第三方认证实现只有显式声明自身具备
        远程暴露所需的认证保护后，surface 才能绑定非本机地址。
        """
        return True

    def requires_concurrency_guard(self) -> bool:
        """认证是否包含需要进程级并发保护的重型校验。

        未覆写时返回 ``True``，避免第三方密码校验器在未声明成本模型时绕过并发保护。
        轻量实现可显式返回 ``False``。
        """
        return True

    def bind_instance_name(self, name: str) -> None:
        """绑定装配期实例名，供需要具名撤销路由的认证器使用。"""
        return None

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与 ``ControlOperator`` 同构。"""
