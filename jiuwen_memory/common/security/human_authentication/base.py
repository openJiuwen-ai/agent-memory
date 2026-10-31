"""Interactive human authentication, separate from machine Credentials/Authenticator."""

from abc import ABC, abstractmethod
from typing import Any

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.types import AuthContext


class HumanAuthenticatorProducer(Factory):
    TOP_NAME = "human_authenticator"


class HumanAuthenticator(ABC):
    def health(self) -> None:
        """Check the human credential/challenge store."""

    @property
    @abstractmethod
    def origin(self) -> str:
        """Exact browser origin required by the human-only transport."""

    @abstractmethod
    def begin(self, binding: str, *, invite: str = "") -> dict[str, Any]:
        """Issue a short-lived, one-use challenge bound to one browser operation."""

    @abstractmethod
    def finish(self, challenge_id: str, binding: str, credential: dict) -> tuple[AuthContext, str]:
        """Consume proof and return the registered human plus operation (view/enroll)."""
