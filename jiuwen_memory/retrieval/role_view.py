"""Role-conditioned view contract. Implementations register through retrieval bootstrap."""

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.retrieval.base import RetrievalOperator, RetrievalOperatorType


class RoleViewProducer(Factory):
    TOP_NAME = "role_view"


class RoleView(RetrievalOperator):
    def operator_type(self):
        return RetrievalOperatorType.ROLE_VIEW

    @abstractmethod
    def configure_relations(self, catalog) -> None:
        """Bind the shared, validated startup relation catalog."""

    @abstractmethod
    def build(
        self,
        scope,
        request,
        *,
        graph,
        read,
        discover,
        resolve_edge,
        background=None,
        registry_revision=None,
    ) -> dict:
        """Return authorized anchors, paths, items and explicit coverage gaps."""
