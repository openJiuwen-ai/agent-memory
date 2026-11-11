"""Evidence conflict resolution contract; resolvers propose decisions, never mutate memory."""

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.construction.base import ConstructionOperator, OperatorType


class ConflictResolution(str, Enum):
    PREFER_A = "prefer_a"
    PREFER_B = "prefer_b"
    BOTH_VALID = "both_valid"
    ESCALATE = "escalate"
    DEFER = "defer"


@dataclass(frozen=True)
class Resolution:
    decision: ConflictResolution
    reason: str
    candidate: bool = False


class ConflictResolverProducer(Factory):
    TOP_NAME = "conflict_resolver"


class ConflictResolver(ConstructionOperator):
    def operator_type(self):
        return OperatorType.CONFLICT_RESOLVER

    def health(self):
        pass

    @abstractmethod
    def resolve(self, memory_a, memory_b, edge, context) -> Resolution:
        """Context is verified evidence supplied by construction, not model confidence."""
