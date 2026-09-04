"""授权能力：契约、真源与内置实现（F05 §Authorization）。"""

from .base import (
    AuthorizationDecision,
    AuthorizationProducer,
    Authorizer,
    RoutingFieldsProvider,
)
from .scope_rules import PrincipalPath, scope_covers
from .store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)

__all__ = [
    "AuthorizationDecision",
    "AuthorizationProducer",
    "Authorizer",
    "RoutingFieldsProvider",
    "DelegationStore",
    "DelegationStoreProducer",
    "GrantStore",
    "GrantStoreProducer",
    "PrincipalPath",
    "scope_covers",
]
