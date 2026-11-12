"""Validate supported inputs and retain classification without masking trusted processing."""

from jiuwen_memory.common.security.privacy.base import PrivacyProtection
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload
from jiuwen_memory.ingest.ingestor import Ingestor


class PrivacyIngestor(Ingestor):
    def __init__(self, delegate: Ingestor, privacy: PrivacyProtection) -> None:
        self._delegate, self._privacy = delegate, privacy

    def operator_type(self):
        return self._delegate.operator_type()

    def health(self) -> None:
        self._privacy.health()
        self._delegate.health()

    def ingest(self, payloads: list[RawPayload]) -> list[MemoryUnit]:
        with self._privacy.staging_scope(retain_on_success=True):
            classified = [self._privacy.ingress(payload) for payload in payloads]
            units = self._delegate.ingest(classified)
            for unit in units:
                self._privacy.inherit_classification(unit, classified)
            return units
