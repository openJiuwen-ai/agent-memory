"""Authenticator — 认证契约（security.md §2.1 / §2.2）。

把一次请求的凭据材料校验成 :class:`~common.type_def.auth.AuthContext`。
实现按认证模式区分（dev / trusted / api_key），由配置在装配期选定；运行期
不再分流——参考 demo 里 ``AuthDispatcher`` 一个类里 if/else 三种模式的写法，
在此拆成三个各自只做一件事的实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from common.factory.factory import Factory
from common.type_def.auth import AuthContext

from .types import AuthMode, Credentials


class AuthProducer(Factory):
    """Authenticator 的注册式工厂（与契约同处接口层）。

    ``target`` 即认证模式名。各实现在 ``authenticator_impl`` 下以
    ``@AuthProducer.register("<模式>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`security.bootstrap.register_security` 统一触发。
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
    def mode(self) -> AuthMode:
        """自描述当前认证模式，供启动期 guard（DEV 的 localhost 强制绑定）与审计。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与 ``ControlOperator`` 同构。"""
