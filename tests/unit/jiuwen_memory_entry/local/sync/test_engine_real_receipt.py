"""The sync engine's receipt probe must work against a real kernel.

The doubles in the neighbouring test files declare ``supports_operation_receipt`` themselves,
so they cannot catch a kernel that forgets to declare it. Here the kernel is assembled from
the same rendered service template the local service uses (space-aware authorization; the
default ``assemble(config={})`` cannot authorize the engine's ``Scope(org, space)`` target).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwen_memory.api import Scope, SpaceSpec, assemble
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory_entry.core.profiles import load_config
from jiuwen_memory_entry.local.home import MemoryHome, init_home
from jiuwen_memory_entry.local.sync.engine import SyncEngine, _add_sync
from jiuwen_memory_entry.local.sync.ledger import SourceRow, SyncLedger
from jiuwen_memory_entry.local.sync.model import ImportItem, ItemId
from tests.integration.jiuwen_memory_entry.local.test_sync_endpoints import MODEL, _local_only
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

_ORG = "local"
_SPACE = "sync-project"
_DIGEST = "d" * 64


def _template_api(home: MemoryHome):
    init_home(home, document_root=home.root / "docs", model=MODEL)
    _local_only(home.service_config)
    settings = yaml.safe_load(home.service_config.read_text(encoding="utf-8"))
    return assemble(config=load_config([settings]).settings.get("memory_api"))


def test_recover_source_reads_kernel_receipt_after_lost_response(tmp_path: Path):
    api = _template_api(MemoryHome(tmp_path / "home"))
    # Same shape as a real install: an agent actor that owns the target space writes to
    # org + space, i.e. no user dimension on the target.
    actor = Scope(org=_ORG, agent="codex-sync")
    sec_root = internal_context(ScopedAuthenticator(Scope(org=_ORG, user="ops"), role=Role.ROOT))
    security = internal_context(ScopedAuthenticator(actor, role=Role.ADMIN))
    api.create_space(SpaceSpec(org=_ORG, space=_SPACE, owner=actor), security=sec_root)
    assert api.supports_operation_receipt() is True
    scope = Scope(org=_ORG, space=_SPACE)
    units = _add_sync(
        api,
        "hello",
        scope,
        security=security,
        item=ImportItem(item_id=ItemId("fact", "MEMORY", "a"), text="hello"),
        digest=_DIGEST,
        source_key="fact|MEMORY|a",
        host="codex",
        op_id="op-1",
    )
    # The response never reached the engine: the ledger only knows "writing" + op id.
    source = SourceRow(
        install_id="install",
        root=str(tmp_path),
        item_id="fact|MEMORY|a",
        target_space=_SPACE,
        digest=_DIGEST,
        op_id="op-1",
        state="writing",
    )
    ledger = SyncLedger(tmp_path / "l.db")
    try:
        verdict = SyncEngine(api, ledger).recover_source(source, security)
    finally:
        ledger.close()
    assert verdict.kind == "receipt", (
        "a committed write must be recovered from the kernel receipt, not left unconfirmed"
    )
    assert verdict.refs == (units[0].id,)
