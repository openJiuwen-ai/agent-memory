"""Test doubles for the Continuity task connector policy (S09 §14)."""


class AllowAllConnectorPolicy:
    """Control-layer double: connector governance is exercised through the API PEP."""

    @staticmethod
    def owner_connector(connector_id: str) -> None:
        return None

    @staticmethod
    def owned_connector(connector_id: str) -> None:
        return None

    @staticmethod
    def available_connector(connector_id: str) -> None:
        return None

    @staticmethod
    def actor_connector(connector_id: str) -> None:
        return None
