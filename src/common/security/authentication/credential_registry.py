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
- 未注册的 ``credential_type``（dev/trusted 等不走可撤销凭据的路径）返回 ``False``--
  它们的撤销语义由各自认证边界另行定义；走可撤销凭据的认证器（如 api_key）必须在装配
  时注册，且其 Store 必须覆盖 :meth:`PrincipalKeyStore.is_revoked`，否则 :meth:`health`
  在启动期 fail-closed。
"""

from __future__ import annotations

from common.errors import ValidationError
from common.security.types import AuthContext

from .key_store import PrincipalKeyStore


class CredentialStatusRegistry:
    """凭据撤销状态的在线复核入口。"""

    def __init__(self) -> None:
        self._stores: dict[str, PrincipalKeyStore] = {}

    def register(self, credential_type: str, store: PrincipalKeyStore) -> None:
        """把一种凭据类型绑定到其发证 Store（供 PEP 复核撤销）。"""
        self._stores[credential_type] = store

    def is_revoked(self, auth: AuthContext) -> bool:
        """该 AuthContext 的凭据是否已撤销。

        无 ``credential_id``（未走可撤销凭据）或凭据类型未注册时返回 ``False``：
        前者本就不参与在线撤销，后者意味着该认证路径未声明可撤销凭据（由装配保证
        api_key 这类路径已注册）。注册的 Store 调其 :meth:`is_revoked`。
        """
        if not auth.credential_id:
            return False
        store = self._stores.get(auth.credential_type)
        if store is None:
            return False
        return store.is_revoked(auth.credential_id)

    def health(self) -> None:
        """启动期校验：所有注册 Store 都覆盖了 is_revoked 且自身健康。

        未覆盖 is_revoked 的 Store 在认证期也会被 :class:`ApiKeyAuthenticator` 拒绝，
        这里是装配期的额外 fail-closed，避免「注册了一个无法复核撤销的 Store」延迟到
        运行期才暴露。
        """
        for credential_type, store in self._stores.items():
            if type(store).is_revoked is PrincipalKeyStore.is_revoked:
                raise ValidationError(
                    f"credential_status_registry 注册的 {credential_type!r} Store "
                    "未实现 is_revoked，无法在线复核撤销"
                )
            store.health()
