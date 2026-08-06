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

**Round3 P1-3 修复**：

- 撤销定位键改为 ``(credential_type, authenticator_name)`` 复合键，支持平行 Authenticator
  实例各自独立的撤销路由。两个平行 ApiKey Authenticator 都返回 ``api_key``，但通过不同
  authenticator 名称区分，第一套签发的凭据撤销后不会误查第二套 Store。

**Round4 P1-4 修复**：

- Registry 路由键改用 ``credential_issuer`` 字段（具名 Authenticator 实例名），
  保留 ``auth_method`` 的协议/认证方法语义（"api_key" / "trusted" / "dev"）。
"""

from __future__ import annotations

from common.errors import ValidationError
from common.security.types import AuthContext

from .key_store import PrincipalKeyStore


class CredentialStatusRegistry:
    """凭据撤销状态的在线复核入口。"""

    def __init__(self) -> None:
        # Round3: 改用 (credential_type, authenticator_name) 复合键
        self._stores: dict[tuple[str, str], PrincipalKeyStore] = {}

    def register(
        self, credential_type: str, authenticator_name: str, store: PrincipalKeyStore
    ) -> None:
        """把一种凭据类型 + Authenticator 实例绑定到其发证 Store（供 PEP 复核撤销）。

        Round3: 使用 (credential_type, authenticator_name) 复合键，支持平行 Authenticator。
        """
        self._stores[(credential_type, authenticator_name)] = store

    def is_revoked(self, auth: AuthContext) -> bool:
        """该 AuthContext 的凭据是否已撤销。

        无 ``credential_id`` 时返回 ``False``（不可撤销凭据，如匿名访问）。
        有 ``credential_id`` 但 issuer 未注册时**抛出异常**（fail-closed）：未注册可能是
        装配遗漏、issuer 名称漂移、或第三方 Authenticator 未声明 capability，这些情况
        必须拒绝而非放行，否则撤销机制失效。

        Round3: 按 (credential_type, authenticator_name) 复合键查找 Store。
        Round4 P1-4: 使用 credential_issuer 字段而非 auth_method 作为路由键。
        credential_issuer 是 Authenticator 装配时的具名实例名称，区分平行 Authenticator。

        Round7 P1-3: 未知 issuer 从返回 False（fail-open）改为抛出 ValidationError（fail-closed）。

        :raises ValidationError: credential_issuer 未注册到 Registry（装配错误或配置漂移）
        """
        if not auth.credential_id:
            return False  # 不可撤销凭据（如匿名）

        # Round4: 使用 credential_issuer（具名实例名称）而非 auth_method（协议标识）
        key = (auth.credential_type, auth.credential_issuer)
        store = self._stores.get(key)
        if store is None:
            # Round7 P1-3: fail-closed，未注册的 issuer 必须拒绝
            from common.errors import ValidationError

            raise ValidationError(
                f"credential_issuer {auth.credential_issuer!r} (type={auth.credential_type}) "
                f"未注册到 CredentialStatusRegistry，无法复核撤销状态。"
                f"这可能是装配错误、Runtime 名称漂移、或 Authenticator 未声明撤销 capability。"
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
