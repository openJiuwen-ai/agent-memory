"""凭据撤销状态注册表（F05 §认证不变量 6、§决策顺序 1）。

PEP（PR2 起的 :class:`~api.memory_api_impl.local_memory_api.LocalMemoryAPI`）持有本
注册表，在每次授权前按 ``AuthContext.credential_type`` 查注册的
:class:`~common.security.authentication.key_store.PrincipalKeyStore.is_revoked`，使
撤销前缓存的上下文在撤销后立即失效。

**设计要点**：

- 注册表是**显式装配 capability**，由 PEP / SecurityRuntime 持有，**不放进 Authorizer**--
  Authorizer 保持纯 PDP，不通过闭包访问 KeyStore。
- 注册表与 Authenticator **共享同一具名 Store 真源**（经 Factory 具名缓存）：认证签发
  与撤销复核读同一份事实，撤销后 PEP 立即看到。
- ``AuthContext`` 保持纯数据值对象，不在跨层/跨进程边界携带可执行闭包。
- 只有 ``AuthContext.credential_status_required=True`` 才进入在线复核。dev、ROOT key、
  trusted gateway 等凭据仍可携带非空 ``credential_id`` 做审计或委托绑定，但不会因此
  被误判为必须存在 ``PrincipalKeyStore`` 撤销真源。
- 撤销路由键是 ``(credential_type, credential_issuer)``。平行 Authenticator 可以使用
  相同协议与凭据类型，同时保持各自独立的真源；声明需要复核却缺少 id 或 issuer 注册时
  运行期 fail-closed，Store 漏实现 ``is_revoked`` 则启动期 fail-closed。
"""

from __future__ import annotations

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.types import AuthContext

from .key_store import PrincipalKeyStore


class CredentialStatusRegistry:
    """凭据撤销状态的在线复核入口。"""

    def __init__(self) -> None:
        self._stores: dict[tuple[str, str], PrincipalKeyStore] = {}

    def register(
        self, credential_type: str, authenticator_name: str, store: PrincipalKeyStore
    ) -> None:
        """把凭据类型与 Authenticator 实例绑定到其发证 Store。"""
        self._stores[(credential_type, authenticator_name)] = store

    def is_revoked(self, auth: AuthContext) -> bool:
        """该 AuthContext 的凭据是否已撤销。

        未声明 ``credential_status_required`` 时返回 ``False``。声明需要在线复核却没有
        ``credential_id``，或 issuer 未注册时抛出 ``ValidationError``（fail-closed）。
        """
        if not auth.credential_status_required:
            return False
        if not auth.credential_id:
            raise ValidationError("需要在线复核的凭据缺少 credential_id")
        store = self._stores.get((auth.credential_type, auth.credential_issuer))
        if store is None:
            raise ValidationError(
                f"credential_issuer {auth.credential_issuer!r} (type={auth.credential_type}) "
                "未注册到 CredentialStatusRegistry，无法复核撤销状态。"
            )
        return store.is_revoked(auth.credential_id)

    def health(self) -> None:
        """启动期校验：所有注册 Store 都覆盖了 is_revoked 且自身健康。

        未覆盖 is_revoked 的 Store 在认证期也会被 :class:`ApiKeyAuthenticator` 拒绝，
        这里是装配期的额外 fail-closed，避免「注册了一个无法复核撤销的 Store」延迟到
        运行期才暴露。
        """
        for (credential_type, authenticator_name), store in self._stores.items():
            if type(store).is_revoked is PrincipalKeyStore.is_revoked:
                raise ValidationError(
                    f"credential_status_registry 注册的 {credential_type!r} "
                    f"(authenticator={authenticator_name!r}) Store "
                    "未实现 is_revoked，无法在线复核撤销"
                )
            store.health()
