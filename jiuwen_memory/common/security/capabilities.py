# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""增量安全能力契约；结构化公开协议，不改变既有 ABC 的抽象方法集合。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
from jiuwen_memory.common.security.authentication.key_store import PrincipalKeyStore
from jiuwen_memory.common.security.authorization.base import Authorizer
from jiuwen_memory.common.security.authorization.store import DelegationStore, GrantStore
from jiuwen_memory.common.security.types import Grant
from jiuwen_memory.common.type_def.scope import Scope

_LOG = logging.getLogger(__name__)
CredentialSource = tuple[str, str, PrincipalKeyStore]


@runtime_checkable
class GrantManagementCapability(Protocol):
    """授权管理增量能力；只读 GrantStore 不必实现。

    create_grant 只创建新 ID；相同记录重投幂等、冲突和撤销重放拒绝。
    成功返回前完成原子提交；抛异常不得留下此次创建的有效权限。不能做到这一点的
    后端不得声明本能力，须在后端实现明确的事务/恢复机制后才用于管理写入。
    """

    def create_grant(self, grant: Grant) -> None:
        """原子创建授权；失败不留下新权限，同 ID 同记录重投幂等。"""
        ...

    def get_grant(self, grant_id: str) -> Grant | None:
        """读取原始记录快照（含撤销/过期）；不存在返回 None。"""
        ...

    def revoke_if_grantor(self, grant_id: str, expected_grantor: Scope) -> None:
        """原子比较真实 grantor 并软撤销；目标变化报后端冲突，未知 ID 幂等。"""
        ...


@runtime_checkable
class DelegationSourcesCapability(Protocol):
    """生命周期内稳定的实际委托真源，组合策略必须完整透传。"""

    def delegation_stores(self) -> tuple[DelegationStore, ...]:
        """返回完整且稳定的委托真源。"""
        ...


@runtime_checkable
class SpaceFactsCapability(Protocol):
    """声明判定是否需要服务端空间事实；未声明者不启用空间链。"""

    def requires_space_facts(self) -> bool:
        """声明是否需要空间真源事实。"""
        ...


@runtime_checkable
class EntryRoutingCapability(Protocol):
    """声明特定入口实际使用的路由字段，避免空间事实需求误关闭外层路由。"""

    def routing_fields_for_entry(self, entry: str) -> tuple[str, ...]:
        """返回该入口仍需绑定系统过滤谓词的路由字段。"""
        ...


@runtime_checkable
class CredentialSourcesCapability(Protocol):
    """认证签发和在线复核的同源声明；不得返回原始凭据。"""

    def credential_sources(self) -> tuple[CredentialSource, ...]:
        """返回凭据类型、签发域与复核 Store 的绑定。"""
        ...


@runtime_checkable
class SecurityDependenciesCapability(Protocol):
    """声明交由同一装配根拥有的安全依赖；借用外部资源不列入。

    返回稳定 tuple；组合对象不级联 close 这些依赖，由装配根按对象身份去重关闭。
    一个对象的 close 只释放自身资源。不同运行时借用同一资源时应复用其生命周期根。
    """

    def security_dependencies(self) -> tuple[object, ...]:
        """只列出本装配根拥有的直接依赖。"""
        ...


@runtime_checkable
class CloseableSecurityCapability(Protocol):
    """可关闭资源；重复关闭幂等，不释放借用资源。"""

    def close(self) -> None:
        """关闭自身资源；依赖交由统一装配根处理。"""
        ...


@runtime_checkable
class SecurityLifecycleCapability(Protocol):
    """装配期间显式共享生命周期根；不改变旧 Runtime 公共方法集合。"""

    def security_resource_owner(self) -> SecurityResourceOwner:
        """返回可在装配阶段显式共享的生命周期根。"""
        ...


def delegation_sources(authorizer: Authorizer) -> tuple[DelegationStore, ...]:
    if not isinstance(authorizer, DelegationSourcesCapability):
        return ()
    stores = authorizer.delegation_stores()
    if not isinstance(stores, tuple):
        raise ValidationError("delegation_stores must return a stable tuple")
    unique = {}
    for store in stores:
        if not isinstance(store, DelegationStore):
            raise ValidationError("delegation_stores must contain DelegationStore instances")
        unique[id(store)] = store
    return tuple(unique.values())


def needs_space_facts(authorizer: object) -> bool:
    if not isinstance(authorizer, SpaceFactsCapability):
        return False
    required = authorizer.requires_space_facts()
    if not isinstance(required, bool):
        raise ValidationError("requires_space_facts must return bool")
    return required


def credential_sources(authenticator: object) -> tuple[CredentialSource, ...]:
    if not isinstance(authenticator, CredentialSourcesCapability):
        return ()
    sources = authenticator.credential_sources()
    if not isinstance(sources, tuple):
        raise ValidationError("credential_sources must return a stable tuple")
    bindings = {}
    for source in sources:
        if not isinstance(source, tuple) or len(source) != 3:
            raise ValidationError("invalid credential source declaration")
        kind, issuer, store = source
        if not isinstance(kind, str) or not isinstance(issuer, str):
            raise ValidationError("credential source type and issuer must be strings")
        if not kind or not issuer or not isinstance(store, PrincipalKeyStore):
            raise ValidationError(
                "credential source must name a type, issuer and PrincipalKeyStore"
            )
        key = (kind, issuer)
        if key in bindings and bindings[key] is not store:
            raise ValidationError("credential issuer maps to multiple stores")
        bindings[key] = store
    return sources


def management_store(authorizer: Authorizer) -> GrantStore | None:
    """规范化旧 singular/plural 入口；一个管理域只有一个可写真源。"""
    stores = authorizer.management_grant_stores()
    if not isinstance(stores, list):
        raise ValidationError("management_grant_stores must return a list of GrantStore")
    unique = {}
    for store in stores:
        if not isinstance(store, GrantStore):
            raise ValidationError("management source must be a GrantStore")
        unique[id(store)] = store
    if len(unique) > 1:
        raise ValidationError("authorization management requires one shared GrantStore")
    single = authorizer.management_grant_store()
    canonical = next(iter(unique.values()), None)
    if single is not None and single is not canonical:
        raise ValidationError("singular/plural GrantStore declarations disagree")
    return canonical


def managed_store(authorizer: Authorizer) -> GrantManagementCapability:
    store = management_store(authorizer)
    if not isinstance(store, GrantManagementCapability):
        raise UnsupportedCapabilityError(
            capability="grant_management",
            value="grant/revoke",
            component="authorizer",
            message="GrantStore does not declare atomic grant management capability",
        )
    _validate_management_methods(store)
    return store


def _validate_management_methods(store: GrantManagementCapability) -> None:
    operations = (store.create_grant, store.get_grant, store.revoke_if_grantor)
    if any(not callable(operation) for operation in operations):
        raise ValidationError("Grant management capability must declare callable methods")


def validate_authorization_capabilities(authorizer: Authorizer, *, required: bool = False) -> None:
    store = management_store(authorizer)
    if isinstance(store, GrantManagementCapability):
        _validate_management_methods(store)
    delegation_sources(authorizer)
    needs_space_facts(authorizer)
    if required:
        try:
            managed_store(authorizer)
        except UnsupportedCapabilityError as exc:
            raise ValidationError("deployment requires grant management capability") from exc


class SecurityResourceOwner:
    """装配根的安全资源清单；固定拓扑、使用者先关、共享依赖只关一次。"""

    def __init__(self, roots: tuple[object, ...]):
        self._resources: list[object] = []
        self._closed: set[int] = set()
        self._lock = threading.RLock()
        self._shared: SecurityResourceOwner | None = None
        self._sealed = False
        self._close_callbacks: list[Callable[[], None]] = []
        visited: set[int] = set()
        visiting: set[int] = set()
        for root in roots:
            self._collect(root, visited, visiting)

    def _collect(self, resource: object, visited: set[int], visiting: set[int]) -> None:
        identity = id(resource)
        if identity in visiting:
            raise ValidationError("cyclic security dependency topology")
        if identity in visited:
            return
        visiting.add(identity)
        if isinstance(resource, SecurityLifecycleCapability):
            raise ValidationError("nested lifecycle owners must be shared explicitly")
        if isinstance(resource, SecurityDependenciesCapability):
            dependencies = resource.security_dependencies()
            if not isinstance(dependencies, tuple) or any(item is None for item in dependencies):
                raise ValidationError("security_dependencies must return a stable resource tuple")
            for dependency in dependencies:
                self._collect(dependency, visited, visiting)
        visiting.remove(identity)
        visited.add(identity)
        self._resources.append(resource)

    def _root(self) -> SecurityResourceOwner:
        # 同一所有者类实例间解析显式共享链接，不跨插件读取私有状态。
        # pylint: disable=protected-access
        owner = self
        while owner._shared is not None:
            owner = owner._shared
        return owner

    def share_with(self, other: SecurityResourceOwner) -> None:
        """仅装配阶段合并根；显式 Runtime 替换时不重复拥有同一依赖。"""
        # 本类两个实例须在双方锁内合并图；不为静态告警公开内部锁/可变清单。
        # pylint: disable=protected-access
        left, right = self._root(), other._root()
        if left is right:
            return
        first, second = sorted((left, right), key=id)
        with first._lock, second._lock:
            if left._shared is not None or right._shared is not None:
                raise ValidationError("security lifecycle changed during assembly; retry required")
            if left._sealed or right._sealed:
                raise ValidationError("cannot attach a closed security lifecycle")
            resources = {id(resource): resource for resource in left._resources}
            for resource in right._resources:
                resources[id(resource)] = resource
            left._resources = list(resources.values())
            left._close_callbacks.extend(right._close_callbacks)
            right._close_callbacks = []
            right._shared = left

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """装配期注册一次性关闭通知，如按纪元失效共享缓存。"""
        root = self._root()
        with root._lock:  # pylint: disable=protected-access
            if root.is_closed():
                raise ValidationError("cannot attach a closed security lifecycle")
            root._close_callbacks.append(callback)  # pylint: disable=protected-access

    def is_closed(self) -> bool:
        owner = self._root()
        if owner is not self:
            return owner.is_closed()
        with self._lock:
            return self._sealed

    def close(self) -> None:
        root = self._root()
        if root is not self:
            root.close()
            return
        with self._lock:
            if self._shared is not None:
                self._shared.close()
                return
            self._sealed = True
            interrupted: BaseException | None = None
            pending_callbacks = []
            for callback in self._close_callbacks:
                try:
                    callback()
                except BaseException as exc:
                    _LOG.error("security close notification failed", exc_info=True)
                    pending_callbacks.append(callback)
                    if not isinstance(exc, Exception) and interrupted is None:
                        interrupted = exc
            self._close_callbacks = pending_callbacks
            for resource in reversed(self._resources):
                identity = id(resource)
                if identity in self._closed:
                    continue
                if isinstance(resource, CloseableSecurityCapability):
                    try:
                        resource.close()
                    except BaseException as exc:
                        # 继续清理其他资源；失败项不标完成，下一次 close 可重试。
                        _LOG.error("closing security resource failed", exc_info=True)
                        if not isinstance(exc, Exception) and interrupted is None:
                            interrupted = exc
                        continue
                self._closed.add(identity)
            if interrupted is not None:
                # 清理边界不吞取消/中断：其余资源处理后原样传播，失败项仍可重试。
                raise interrupted


def share_security_lifecycle(left: object, right: object) -> None:
    """兼容旧句柄；声明新能力的两个装配包装显式共用一个生命周期根。"""
    if isinstance(left, SecurityLifecycleCapability) and isinstance(
        right, SecurityLifecycleCapability
    ):
        left.security_resource_owner().share_with(right.security_resource_owner())
