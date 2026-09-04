"""绑定地址策略契约（F05 §Protection §BindingPolicy）。

无认证开发模式恒返回 ROOT 身份，绑到非 loopback 就是把全权限暴露给整个网络。
F05 要求这条约束由**统一 Server lifecycle 在实际 socket 绑定前执行**，不能只
存在于某个 CLI ``main()``——故本模块给出能力接口与 Producer，由 Server 在真正
``bind()`` 之前调用，而不是让每个 surface 的入口各自记得调。

策略**抛异常，不 ``sys.exit``**：exit 语义留在真正的进程入口，这样策略可被单测
直接断言，而不会让测试进程退出。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from jiuwen_memory.common.factory.factory import Factory


class BindingPolicyProducer(Factory):
    """BindingPolicy 的注册式工厂（与契约同处接口层）。

    各实现在 ``protection_impl`` 下以 ``@BindingPolicyProducer.register("<策略>")``
    自注册，由 :func:`common.security.bootstrap.register_security` 统一触发。
    """

    TOP_NAME = "binding_policy"


class BindingPolicy(ABC):
    """监听地址准入：在 socket 真正绑定前裁决。"""

    @abstractmethod
    def check(self, hosts: str | Sequence[str] | None, *, requires_loopback: bool) -> None:
        """校验监听地址；不合规抛 :class:`~common.errors.ValidationError`。

        ``requires_loopback`` 来自
        :meth:`~common.security.authentication.base.Authenticator.requires_loopback_binding`
        ——**认证能力自己声明**是否具备远程暴露所需的保护，策略据此裁决。这样第三方
        认证实现无需修改本模块即可参与判断，也不必按 target 名（``dev`` / ``api_key``）
        推断安全保证。

        返回 ``None`` 即通过。不返回 bool：绑定裁决只有「放行」与「拒绝启动」两种
        结果，返回 bool 会诱导调用方写 ``if not ok: log.warning(...)`` 这类 fail-open。
        """

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。与其他安全组件同构。"""
