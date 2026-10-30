# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Private fragments and authenticated snapshots, independent of ordinary recall.

References are opaque storage values: processing may copy them without disclosing
their contents. Only ``get_fragment`` returns plaintext. Its caller must first
complete human authentication and resource authorization.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.base import BaseStore, StoreType


def scope_key(scope: Scope) -> str:
    """Canonical complete namespace, also used to bind fragment encryption origins."""
    return json.dumps(asdict(scope), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PrivacyStoreProducer(Factory):
    """Factory for isolated private storage; not a general recall port."""

    TOP_NAME = "privacy_store"

    @classmethod
    def for_privacy(cls, config) -> PrivacyStore:
        """Resolve an explicit Store or adapt the previous vault_path configuration."""
        from jiuwen_memory.common.errors import ValidationError

        if config.get("privacy_store"):
            return cls.dep(config)
        if not config.get("cryptography"):
            raise ValidationError("privacy requires explicit cryptography configuration")
        path = config.get("vault_path")
        if not path:
            raise ValidationError("privacy requires a dedicated vault_path or privacy_store")
        return cls.build(
            "encrypted",
            {
                "raw_privacy_store": {"target": "sqlite", "params": {"db_path": str(path)}},
                "cryptography": config.get("cryptography"),
            },
            config.ctx,
        )


def encrypted_sqlite_privacy_store(encryption, path: str) -> PrivacyStore:
    """Compatibility assembly for direct callers of the former privacy constructor."""
    from jiuwen_memory.storage.privacy_impl import EncryptedPrivacyStore, SQLitePrivacyStore

    if encryption is None or not path:
        from jiuwen_memory.common.errors import ValidationError

        raise ValidationError("privacy requires cryptography and a dedicated path")
    return EncryptedPrivacyStore(SQLitePrivacyStore(path), encryption)


@dataclass(frozen=True)
class FragmentReference:
    """Opaque, copyable stored representation; origin binds its encryption context."""

    value: bytes = field(repr=False)
    level: str
    origin: str


@dataclass(frozen=True)
class PrivacySnapshot:
    """Manifest data, identified within the explicit Scope passed to the Store."""

    unit: str
    revision: str
    digest: str
    value: bytes = field(repr=False)


class PrivacyStore(BaseStore):
    """Atomic private-state operations, with complete Scope isolation.

    ``insert`` conflicts and ``update`` requires an existing snapshot. Snapshot
    writes include fragment references and bindings in the same transaction.
    Search is exact lookup by record IDs/revision, never plaintext fulltext/ANN.
    Recovery operations retain ciphertext until the external truth outcome is known;
    they do not establish a transaction with the business Store.
    """

    @staticmethod
    def store_type() -> StoreType:
        return StoreType.PRIVACY

    @staticmethod
    def security_dependencies() -> tuple[object, ...]:
        """Declare shared security dependencies for the assembly resource owner."""
        return ()

    @abstractmethod
    def transaction(self, scope: Scope) -> AbstractContextManager[None]:
        """Serialize and atomically commit private operations; nesting is supported."""

    @abstractmethod
    def stage_fragment(
        self, scope: Scope, token: str, value: bytes, level: str
    ) -> FragmentReference:
        """Protect a volatile fragment without persisting it."""

    @abstractmethod
    def reference(self, scope: Scope, token: str) -> FragmentReference:
        """Read an opaque reference without decrypting its sensitive value."""

    @abstractmethod
    def get_fragment(self, scope: Scope, token: str) -> bytes:
        """Read plaintext after the caller's disclosure proof and resource PEP."""

    @abstractmethod
    def get(self, scope: Scope, unit: str) -> PrivacySnapshot:
        """Read the active manifest; missing records raise NotFoundError."""

    @abstractmethod
    def search(
        self, scope: Scope, units: list[str], *, revision: str | None = None
    ) -> list[PrivacySnapshot]:
        """Read matching active/recovery manifests without decrypting fragments."""

    @abstractmethod
    def insert(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        """Insert a manifest and its fragment references atomically."""

    @abstractmethod
    def update(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        """Replace an existing manifest and bindings atomically."""

    @abstractmethod
    def checkpoint(self, scope: Scope, unit: str) -> None:
        """Retain the current manifest and bindings before an external truth write."""

    @abstractmethod
    def confirm(self, scope: Scope, unit: str) -> None:
        """Drop recovery state after a confirmed truth write; collect unbound fragments."""

    @abstractmethod
    def reconcile(self, scope: Scope, unit: str, revision: str, digest: str) -> None:
        """Restore the snapshot matching surviving truth; retain unknown outcomes."""

    @abstractmethod
    def delete(self, scope: Scope, units: list[str] | None) -> set[str]:
        """Delete active/recovery state in exact Scope; None selects all records."""

    @abstractmethod
    def scopes(self) -> list[Scope]:
        """Enumerate exact namespaces for controlled lifecycle cleanup."""

    @abstractmethod
    def close(self) -> None:
        """Release backend resources."""
