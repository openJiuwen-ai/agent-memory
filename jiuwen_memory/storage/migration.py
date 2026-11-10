# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional public memory-maintenance contract for protected storage plugins."""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from jiuwen_memory.common.type_def import Scope


@dataclass(frozen=True)
class MigrationRecord:
    """One bounded read; value=None marks an oversized record without loading its body.

    cursor is backend-owned progress immediately after this record, never an authorization.
    """

    key: str
    value: bytes | None
    cursor: str


def opaque_location(kind: str, *parts: object) -> tuple[str, bytes]:
    """Deduplication token; never return configured connection credentials or paths."""
    return kind, hashlib.sha256(repr(parts).encode()).digest()


class PlaintextMigrationTarget(Protocol):
    def migration_validate_scope(self, scope: Scope) -> None:
        """Refuse lossy namespace interpretation before selecting this location."""
        ...

    def migration_identity(self) -> object:
        """Opaque physical identity; never exposed as a connection string."""
        ...

    def migration_scopes(self) -> list[Scope]:
        ...

    def migration_scan_supported(self) -> bool:
        ...

    def migration_records(
        self, scope: Scope, *, after: str = "", max_value_bytes: int = 134217728
    ) -> Iterator[MigrationRecord]:
        """Bounded iteration in the authorized Scope, without falling back to full scan."""
        ...

    def migration_key(self, unit_id: str) -> str:
        """Map logical memory identity to this plugin's actual stable record key."""
        ...

    def migrate_plaintext(
        self,
        scope: Scope,
        key: str,
        expected: bytes,
        *,
        operation: str,
        existing_only: bool = False,
    ) -> str:
        """Encrypt/verify/CAS or compare-delete; retain expiry and reject changed data."""
        ...
