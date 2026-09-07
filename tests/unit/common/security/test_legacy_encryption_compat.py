"""上一版公开加密 API 的一个发布周期兼容面。"""

from __future__ import annotations

from jiuwen_memory.common.security import (
    KeySource,
    SecurityContext,
    SecurityProducer,
    SecurityProvider,
)
from jiuwen_memory.common.security.cryptography import (
    CryptographyProducer,
    CryptographyProvider,
)
from jiuwen_memory.common.type_def import Scope


class _KeySource(KeySource):
    def fetch_key(self, key_name: str) -> bytes:
        return key_name.encode()


def test_legacy_public_names_remain_importable() -> None:
    assert SecurityProducer is CryptographyProducer
    assert SecurityProvider is CryptographyProvider
    assert SecurityContext().scope == Scope()
    context = SecurityContext(Scope(org="acme"), "value", {"source": "legacy"})
    assert context.metadata == {"source": "legacy"}
    assert context.object_id == ""
    assert _KeySource().fetch_key("key") == b"key"
