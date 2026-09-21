"""上一版公开加密 API 的一个发布周期兼容面。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

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


@pytest.mark.unit
def test_legacy_context_preserves_parent_defaults_and_copies_metadata() -> None:
    metadata = {"source": "legacy"}
    scope = Scope(org="acme")
    context = SecurityContext(scope, "value", metadata)
    metadata["source"] = "changed"
    assert context.scope is scope
    assert context.purpose == "value"
    assert context.object_id == ""
    assert context.format_version == 1
    assert context.metadata == {"source": "legacy"}
    assert SecurityContext().metadata == {}
    with pytest.raises(FrozenInstanceError):
        context.purpose = "changed"
