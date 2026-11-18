"""Public read-only inspection of a Runtime's effective continuity catalog."""

from __future__ import annotations

from jiuwen_memory.api.memory_api import MemoryAPI
from jiuwen_memory.common.security.types import RequestSecurityContext
from jiuwen_memory.common.type_def import Scope


def _unavailable() -> dict:
    return {
        "mode": None,
        "role_catalog": {
            "available": False,
            "reason": "catalog_unavailable",
            "policy_version": None,
            "default_duty": None,
            "duties": [],
            "relation_types": [],
        },
    }


def read_continuity_catalog(
    api: MemoryAPI, scope: Scope, *, security: RequestSecurityContext,
) -> dict:
    """Return the catalog currently installed on this API Runtime.

    Implementations that expose the optional inspection capability perform their own
    trusted-context validation and project READ authorization. This helper does not open
    runtime resources, read configuration files or add another MemoryAPI operation.
    """
    reader = getattr(api, "continuity_catalog_snapshot", None)
    if not callable(reader):
        return _unavailable()
    snapshot = reader(scope, security=security)
    if not isinstance(snapshot, dict) or snapshot.get("mode") not in {"continuity", "role"}:
        raise TypeError("continuity catalog inspection returned an invalid snapshot")
    catalog = snapshot.get("role_catalog")
    if not isinstance(catalog, dict):
        raise TypeError("continuity catalog inspection returned no role catalog")
    return snapshot
