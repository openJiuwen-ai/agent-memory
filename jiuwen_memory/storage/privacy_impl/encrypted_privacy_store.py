# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Transparent private-state encryption; algorithms belong to CryptographyProvider."""

from __future__ import annotations

import json
from dataclasses import replace

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.cryptography import CryptographyProducer, CryptographyProvider
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.privacy import (
    FragmentReference,
    PrivacySnapshot,
    PrivacyStore,
    PrivacyStoreProducer,
)
from jiuwen_memory.storage.security import (
    EnabledStoreSecurity,
    StoreEncryptionBinding,
    StoreSecurity,
)


def _context(scope: Scope, object_id: str, purpose: str, level: str = "") -> CryptoContext:
    return CryptoContext(
        scope=scope,
        purpose=purpose,
        object_id=object_id,
        format_version=1,
        metadata={"level": level},
    )


class EncryptedPrivacyStore(PrivacyStore):
    """Encrypt staged fragments/manifests; copy opaque references without decryption."""

    def __init__(self, raw: PrivacyStore, encryption: CryptographyProvider) -> None:
        if raw.security.enabled():
            raise ValidationError("privacy encryption cannot wrap an already protected Store")
        self._raw, self._encryption = raw, encryption
        self._security = EnabledStoreSecurity(
            encryption.health,
            binding=StoreEncryptionBinding(raw, encryption, "ENC1/privacy/aad-v1"),
        )

    @property
    def security(self) -> StoreSecurity:
        return self._security

    def security_dependencies(self) -> tuple[object, ...]:
        return (self._encryption, *self._raw.security_dependencies())

    def transaction(self, scope: Scope):
        return self._raw.transaction(scope)

    def stage_fragment(
        self, scope: Scope, token: str, value: bytes, level: str
    ) -> FragmentReference:
        cipher = self._encryption.encrypt(
            value, context=_context(scope, token, "privacy-fragment", level)
        )
        return self._raw.stage_fragment(scope, token, cipher, level)

    def reference(self, scope: Scope, token: str) -> FragmentReference:
        return self._raw.reference(scope, token)

    def get_fragment(self, scope: Scope, token: str) -> bytes:
        ref = self._raw.reference(scope, token)
        return self._encryption.decrypt(
            ref.value,
            context=_context(Scope(**json.loads(ref.origin)), token, "privacy-fragment", ref.level),
        )

    def _encode(self, scope: Scope, snapshot: PrivacySnapshot) -> PrivacySnapshot:
        return replace(
            snapshot,
            value=self._encryption.encrypt(
                snapshot.value,
                context=_context(
                    scope, snapshot.unit + ":" + snapshot.revision, "privacy-manifest"
                ),
            ),
        )

    def _decode(self, scope: Scope, snapshot: PrivacySnapshot) -> PrivacySnapshot:
        return replace(
            snapshot,
            value=self._encryption.decrypt(
                snapshot.value,
                context=_context(
                    scope, snapshot.unit + ":" + snapshot.revision, "privacy-manifest"
                ),
            ),
        )

    def get(self, scope: Scope, unit: str) -> PrivacySnapshot:
        return self._decode(scope, self._raw.get(scope, unit))

    def search(
        self, scope: Scope, units: list[str], *, revision: str | None = None
    ) -> list[PrivacySnapshot]:
        return [
            self._decode(scope, row) for row in self._raw.search(scope, units, revision=revision)
        ]

    def insert(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        self._raw.insert(scope, self._encode(scope, snapshot), fragments)

    def update(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        self._raw.update(scope, self._encode(scope, snapshot), fragments)

    def checkpoint(self, scope: Scope, unit: str) -> None:
        self._raw.checkpoint(scope, unit)

    def confirm(self, scope: Scope, unit: str) -> None:
        self._raw.confirm(scope, unit)

    def reconcile(self, scope: Scope, unit: str, revision: str, digest: str) -> None:
        self._raw.reconcile(scope, unit, revision, digest)

    def delete(self, scope: Scope, units: list[str] | None) -> set[str]:
        return self._raw.delete(scope, units)

    def scopes(self) -> list[Scope]:
        return self._raw.scopes()

    def health(self) -> None:
        self._raw.health()
        self._encryption.health()

    def close(self) -> None:
        self._raw.close()


@PrivacyStoreProducer.register("encrypted")
def _build(config):
    raw = config.params.get("raw_privacy_store")
    if not raw or (config.name and raw == config.name):
        raise ValidationError("privacy_store.encrypted requires a separate raw_privacy_store")
    return EncryptedPrivacyStore(
        PrivacyStoreProducer.dep(config, "raw_privacy_store"), CryptographyProducer.dep(config)
    )
