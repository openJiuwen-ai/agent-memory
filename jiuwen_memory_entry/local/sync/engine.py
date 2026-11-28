"""Uniform import pipeline: readers only read; this module filters and writes."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from jiuwen_memory.api import DeleteSelector, Scope
from jiuwen_memory_entry.local.capture_filter import strip_context_blocks
from jiuwen_memory_entry.local.locks import file_lock
from jiuwen_memory_entry.local.sync.ledger import (
    OutputRow,
    PendingCleanup,
    SourceRow,
    SyncLedger,
    can_delete,
)
from jiuwen_memory_entry.local.sync.model import ImportItem, RangeKey, SyncReport
from jiuwen_memory_entry.local.sync.space import SpaceRecoveryReport, space_lock

MAX_FILE_BYTES = 2 * 1024 * 1024


class SyncAuthorizationChanged(Exception):
    """The grant changed before a queued sync write could start."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _metadata(item: ImportItem, digest: str, source_key: str = "") -> dict[str, str]:
    metadata = {
        "source_app": item.source_key.split(":", 1)[0],
        "source_key": source_key or item.source_key,
        "source_digest": digest,
        "infer": "true" if item.kind == "session_turn" else "false",
    }
    if item.claimed_author:
        metadata["claimed_author"] = item.claimed_author
    return metadata


class ScanSyncMixin:
    """Plan-compatible scan path: readers report items, this layer owns ledger writes."""

    def _finish_legacy_migration(
        self,
        install_id: str,
        root: str,
        target_space: str,
        legacy_key: str,
        legacy_item: str,
        unit_ids: list[str],
    ) -> None:
        for unit_id in unit_ids:
            self.ledger.release_refs(legacy_key, [unit_id])
            self.ledger.put_pending(
                PendingCleanup(
                    unit_id,
                    target_space,
                    legacy_key,
                    "replaced legacy USER file",
                    "",
                    _now(),
                )
            )
        self.ledger.delete(install_id, legacy_key)
        self.ledger.delete_source(install_id, root, legacy_item, target_space)

    def run_scan(
        self,
        security,
        install,
        reader,
        access,
        grant,
        *,
        dry_run: bool = False,
        since: datetime | None = None,
        limit: int | None = None,
        install_lock: bool = True,
    ) -> SyncReport:
        report = SyncReport()
        lock = None
        if install_lock and self.lock_path is not None:
            try:
                lock = file_lock(
                    self.lock_path,
                    timeout=0.05,
                    conflict_message="sync already running for this install",
                )
                lock.__enter__()
            except Exception:
                report.failed.append(("", "同步进行中"))
                return report
        try:
            if not self.grant_still_valid(grant):
                report.failed.append(("", "SyncAuthorizationChanged"))
                report.truncated.append("sync authorization changed")
                return report
            if not dry_run:
                # A crash leaves an unconfirmed op in the ledger; replay it before a
                # new scan can allocate another operation id and duplicate the write.
                try:
                    recovery = self.recover_space(
                        security,
                        grant.target_space,
                        data_dir=self.ledger.path.parent,
                        install_id=install.install_id,
                        pending_prefix=f"{grant.grant_id}|{grant.target_space}|",
                    )
                except Exception as exc:  # noqa: BLE001 - never scan after a failed recovery
                    report.failed.append(("", f"recovery: {type(exc).__name__}"))
                    return report
                if recovery.authorization_changed:
                    report.failed.append(("", "SyncAuthorizationChanged"))
                    report.truncated.append("sync authorization changed")
                    return report
                if recovery.unconfirmed:
                    report.truncated.append(
                        "sync deferred: unconfirmed operation requires recovery"
                    )
                    return report
            space = (
                space_lock(grant.target_space, self.ledger.path.parent)
                if self.lock_path is not None
                else nullcontext()
            )
            with space:
                return self._run_scan(
                    security, install, reader, access, grant,
                    dry_run=dry_run, since=since, limit=limit,
                )
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

    def _run_scan(
        self,
        security,
        install,
        reader,
        access,
        grant,
        *,
        dry_run: bool = False,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> SyncReport:
        report = SyncReport()
        run_start = _now()
        install_id = install.install_id
        actor = _install_actor(install)
        scope = Scope(org=actor.org, space=grant.target_space)
        try:
            result = reader.scan(
                access, types=grant.types, since=since, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            report.failed.append(("", type(exc).__name__))
            return report
        report.truncated.extend(access.truncated)
        report.skipped += len(result.skipped)
        if not result.items and not result.complete:
            report.unrecognized = True
        prefix = f"{grant.grant_id}|{grant.target_space}|"
        seen: set[str] = set()
        legacy_groups: dict[tuple[str, str], dict[str, object]] = {}
        for item in result.items:
            item_id = item.item_id or item.frozen_item_id
            root = item.root or (grant.roots[0] if grant.roots else "")
            key = f"{prefix}{root}|{item_id}"
            group: dict[str, object] | None = None
            if item_id.kind == "profile":
                parts = str(item_id).split("|", 2)
                collection = parts[1] if len(parts) == 3 else ""
                group_key = (root, collection)
                group = legacy_groups.get(group_key)
                if group is None:
                    legacy_key = _legacy_user_key(
                        grant.grant_id, grant.target_space, root, str(item_id)
                    )
                    legacy_item = f"profile|{collection}|file"
                    legacy_row = (
                        self.ledger.get(install_id, legacy_key) if legacy_key else None
                    )
                    legacy_source = (
                        self.ledger.get_source(
                            install_id, root, legacy_item, grant.target_space
                        )
                        if legacy_key
                        else None
                    )
                    if legacy_row is not None or legacy_source is not None:
                        legacy_units = (
                            list(legacy_row.unit_ids) if legacy_row is not None else []
                        )
                        if not legacy_units and legacy_source is not None:
                            legacy_units = list(legacy_source.refs)
                        group = {
                            "legacy_key": legacy_key,
                            "legacy_item": legacy_item,
                            "units": legacy_units,
                            "all": set(),
                            "ok": set(),
                        }
                        legacy_groups[group_key] = group
                        seen.add(legacy_key)
                if group is not None:
                    group["all"].add(key)
            text = strip_context_blocks(self.filter(item.text)).strip()
            if not text:
                report.skipped += 1
                if group is not None:
                    # A pure reflow entry has nothing to migrate, but it must not
                    # keep the legacy whole-file USER row from completing.
                    group["ok"].add(key)
                continue
            digest = _digest(text)
            report.writes[key] = digest
            row = self.ledger.get(install_id, key)
            stored = self.ledger.get_source(
                install_id, root, str(item_id), grant.target_space
            )
            old_sources: list[tuple[str, list[str]]] = []
            if row is not None and row.unit_ids:
                old_sources.append((key, list(row.unit_ids)))
            old_refs = [unit_id for _, unit_ids in old_sources for unit_id in unit_ids]
            seen.add(key)
            item_text = str(item_id)
            op_id = ""
            needs_write = (
                row is None
                or row.digest != digest
                or row.state not in {"ok", "cleanup_pending"}
                or (stored is not None and stored.state == "retryable")
            )
            if not dry_run and needs_write and not self.grant_still_valid(grant):
                report.failed.append((key, "SyncAuthorizationChanged"))
                report.truncated.append("sync authorization changed")
                return report
            source_row = None
            if not dry_run and needs_write:
                op_id = self.new_op_id()
                source_row = SourceRow(
                    install_id=install_id,
                    root=root,
                    item_id=item_text,
                    target_space=grant.target_space,
                    digest=digest,
                    op_id=op_id,
                    intent="replace" if old_refs else "new",
                    old_refs=old_refs,
                    refs=[],
                    grant_id=grant.grant_id,
                    grant_version=grant.version,
                    state="writing",
                    last_seen=_now(),
                )
            if (
                row is not None
                and row.digest == digest
                and row.state in {"ok", "cleanup_pending"}
            ):
                report.skipped += 1
                if group is not None:
                    group["ok"].add(key)
                if not dry_run:
                    self.ledger.touch(install_id, key, _now())
                continue
            row_changed = (
                row is not None
                and bool(row.unit_ids)
                and (row.digest != digest or row.state == "pending")
            )
            if row_changed and dry_run:
                report.updated += 1
                continue
            if dry_run:
                report.added += 1
                continue
            source_started = False
            try:
                with self.grant_boundary():
                    if not self.grant_still_valid(grant):
                        raise SyncAuthorizationChanged
                    if source_row is not None:
                        self.ledger.put_source(source_row)
                        source_started = True
                    units = _add_sync(
                        self.api, text, scope,
                        security=security, item=item, digest=digest,
                        source_key=key, host=install.host, op_id=op_id,
                        occurred_at=item.occurred_at,
                    )
                if not units:
                    raise RuntimeError("no added unit")
                refs = [unit.id for unit in units]
                for old_key, unit_ids in old_sources:
                    for unit_id in unit_ids:
                        self.ledger.release_refs(old_key, [unit_id])
                        self.ledger.put_pending(
                            PendingCleanup(
                                unit_id, grant.target_space, old_key,
                                "replaced old output", op_id, _now(),
                            )
                        )
                self.ledger.upsert(install_id, key, refs, digest, _now())
                self._record_outputs(
                    install_id, grant, op_id, digest, units, refs, key
                )
                self.ledger.put_source(
                    SourceRow(
                        install_id, root, item_text, grant.target_space, digest,
                        op_id, "replace" if old_refs else "new", old_refs, refs,
                        [], grant.grant_id, grant.version,
                        "cleanup_pending" if old_refs else "ok", _now(),
                    )
                )
                if group is not None:
                    group["ok"].add(key)
                report.updated += 1 if old_refs else 0
                report.added += 0 if old_refs else 1
                if not self.grant_still_valid(grant):
                    report.failed.append((key, "SyncAuthorizationChanged"))
                    report.truncated.append("sync authorization changed")
                    return report
            except SyncAuthorizationChanged:
                report.failed.append((key, "SyncAuthorizationChanged"))
                report.truncated.append("sync authorization changed")
                return report
            except Exception as exc:  # noqa: BLE001
                if not source_started:
                    report.failed.append((key, "SyncAuthorizationChanging"))
                    report.truncated.append("sync authorization is changing")
                    return report
                pending_source = SourceRow(
                    install_id, root, item_text, grant.target_space, digest,
                    op_id, "replace" if old_refs else "new", old_refs, [],
                    [], grant.grant_id, grant.version, "writing", _now(),
                )
                self.ledger.put_source(pending_source)
                verdict = _recover_source(self, pending_source, security)
                if verdict.kind == "receipt":
                    _register_receipt_refs(
                        self,
                        pending_source,
                        list(verdict.refs),
                        verdict.created,
                        security=security,
                    )
                    pending_source.refs = list(verdict.refs)
                    new_refs = set(pending_source.refs)
                    released: list[tuple[str, str]] = []
                    for old_key, unit_ids in old_sources:
                        for unit_id in unit_ids:
                            if old_key == key and unit_id in new_refs:
                                continue
                            released.append((old_key, unit_id))
                    pending_source.pending_refs = [unit_id for _, unit_id in released]
                    pending_source.state = "cleanup_pending" if released else "ok"
                    old_key = old_sources[0][0] if len(old_sources) == 1 else key
                    self.ledger.record_recovered(
                        pending_source,
                        list(verdict.refs),
                        digest,
                        _now(),
                        old_key=old_key,
                        pending_reason="recovered replace old output",
                        pending_op=op_id,
                    )
                    if group is not None:
                        group["ok"].add(key)
                    if old_refs:
                        report.updated += 1
                    else:
                        report.added += 1
                elif verdict.kind == "resend":
                    pending_source.state = "retryable"
                    self.ledger.put_source(pending_source)
                    report.failed.append((key, type(exc).__name__))
                else:
                    pending_source.state = "unconfirmed"
                    self.ledger.put_source(pending_source)
                    report.failed.append((key, type(exc).__name__))
        if not dry_run and since is None and limit is None:
            for (root, _collection), group in legacy_groups.items():
                if result.complete.get(RangeKey(root, "profile")) is not True:
                    continue
                if group["all"] != group["ok"]:
                    continue
                legacy_key = str(group["legacy_key"])
                legacy_item = str(group["legacy_item"])
                self._finish_legacy_migration(
                    install_id,
                    root,
                    grant.target_space,
                    legacy_key,
                    legacy_item,
                    list(group["units"]),
                )
                report.updated += 1
        if self.ledger.unconfirmed_ops(grant.target_space):
            report.truncated.append("deletion deferred: unconfirmed sync operation")
            return report
        if not dry_run:
            if not self.grant_still_valid(grant):
                report.failed.append(("", "SyncAuthorizationChanged"))
                report.truncated.append("sync authorization changed")
                return report
            # A preview must not mutate the ledger or the store. Cleanup is also
            # limited to this install's source keys, never the whole space.
            pending = [
                cleanup
                for cleanup in self.ledger.pending_for_space(grant.target_space)
                if cleanup.source_key.startswith(prefix)
            ]
            for cleanup in pending:
                try:
                    with self.grant_boundary():
                        if not self.grant_still_valid(grant):
                            report.failed.append(("", "SyncAuthorizationChanged"))
                            report.truncated.append("sync authorization changed")
                            return report
                        deleted, retained = self.delete_outputs(
                            security, [cleanup.unit_id], space=grant.target_space
                        )
                except Exception:  # noqa: BLE001 - another writer holds the boundary
                    report.truncated.append(
                        "cleanup deferred: authorization boundary is changing"
                    )
                    return report
                report.deleted_in_source.extend(
                    f"cleanup:{unit_id}" for unit_id in deleted
                )
                del retained
            remaining = {
                row.unit_id
                for row in self.ledger.pending_for_space(grant.target_space)
                if row.source_key.startswith(prefix)
            }
            for source in self.ledger.sources_for_space(grant.target_space):
                if (
                    source.install_id != install_id
                    or source.state != "cleanup_pending"
                    or f"{source.grant_id}|{source.target_space}|" != prefix
                ):
                    continue
                tracked = (
                    set(source.old_refs)
                    | set(source.refs)
                    | set(source.pending_refs)
                )
                if not (tracked & remaining):
                    self.ledger.update_source_state(source, "ok")
        incomplete_scan = (
            not result.complete or limit is not None or since is not None
        )
        if incomplete_scan or not all(result.complete.values()):
            return report
        if not self.grant_still_valid(grant):
            report.failed.append(("", "SyncAuthorizationChanged"))
            report.truncated.append("sync authorization changed")
            return report
        missing = []
        for source_key in self.ledger.unseen(install_id, run_start):
            if not source_key.startswith(prefix) or source_key in seen:
                continue
            source = self.ledger.get_source_by_key(install_id, source_key)
            if source is None or source.root not in grant.roots:
                continue
            if source.item_id.split("|", 1)[0] not in grant.types:
                continue
            missing.append(source_key)
        if missing and len(missing) > len(seen):
            report.truncated.append("deletion deferred: more than half of the source is missing")
            return report
        for source_key in missing:
            if not self.grant_still_valid(grant):
                report.failed.append(("", "SyncAuthorizationChanged"))
                report.truncated.append("sync authorization changed")
                return report
            source = self.ledger.get_source_by_key(install_id, source_key)
            row = self.ledger.get(install_id, source_key)
            report.deleted_in_source.append(source_key)
            report.deletes.add(source_key)
            unit_ids = list(source.refs) if source is not None else []
            if not unit_ids and row is not None:
                unit_ids = list(row.unit_ids)
            can_delete_missing = (
                not dry_run
                and grant.mirror_deletes
                and source is not None
                and bool(unit_ids)
            )
            if not can_delete_missing:
                continue
            try:
                with self.grant_boundary():
                    if not self.grant_still_valid(grant):
                        report.failed.append(("", "SyncAuthorizationChanged"))
                        report.truncated.append("sync authorization changed")
                        return report
                    tracked = [
                        unit_id for unit_id in unit_ids
                        if self.ledger.get_output(unit_id) is not None
                    ]
                    for unit_id in tracked:
                        self.ledger.release_refs(source_key, [unit_id])
                        self.ledger.put_pending(
                            PendingCleanup(
                                unit_id,
                                grant.target_space,
                                source_key,
                                "source entry missing",
                                "",
                                _now(),
                            )
                        )
                    deleted, _retained = self.delete_outputs(
                        security, tracked, space=grant.target_space
                    )
                legacy = [
                    unit_id for unit_id in unit_ids
                    if self.ledger.get_output(unit_id) is None
                ]
                if legacy:
                    # Rows written before output tracking carry no proven sync
                    # attribution; deleting them would guess ownership.
                    report.truncated.append(
                        f"{len(legacy)} legacy output(s) retained for {source_key}"
                    )
                if deleted:
                    self.ledger.delete(install_id, source_key)
            except Exception as exc:  # noqa: BLE001
                report.failed.append((source_key, type(exc).__name__))
        return report


    def _record_outputs(
        self, install_id, grant, op_id, digest, units, refs, source_key=""
    ):
        del install_id, digest, refs
        for unit in units:
            if self.ledger.get_output(unit.id) is None:
                metadata = getattr(unit, "system_metadata", {}) or {}
                origin = (
                    "sync" if metadata.get("sync_created_by") == op_id else "external"
                )
                content = str(getattr(unit, "content", ""))
                self.ledger.put_output(
                    OutputRow(
                        unit_id=unit.id,
                        target_space=grant.target_space,
                        origin=origin,
                        created_by_op=op_id if origin == "sync" else "",
                        initial_digest=_digest(content) if content else "",
                        refcount=0,
                    )
                )
            self.ledger.add_refs(source_key, [unit.id])


class SyncEngine(ScanSyncMixin):
    def __init__(
        self,
        api,
        ledger: SyncLedger,
        filter_text=None,
        *,
        lock_path: Path | None = None,
        receipt_epoch: str = "",
        grant_check=None,
    ) -> None:
        self._api = api
        self._ledger = ledger
        self._filter = filter_text or (lambda text: text)
        self._lock_path = lock_path
        self._receipt_epoch = receipt_epoch
        self._grant_check = grant_check

    @property
    def ledger(self) -> SyncLedger:
        """Read-only ledger view for module-level recovery helpers."""
        return self._ledger

    @property
    def api(self):
        """Read-only kernel API view for module-level recovery helpers."""
        return self._api

    @property
    def receipt_epoch(self) -> str:
        """Read-only operation-id prefix for module-level recovery helpers."""
        return self._receipt_epoch

    @property
    def filter(self):
        """Read-only text filter shared with the mixin scan path."""
        return self._filter

    @property
    def lock_path(self) -> Path | None:
        """Read-only install lock used by the mixin scan path."""
        return self._lock_path

    def grant_still_valid(self, grant) -> bool:
        """Public form of the grant check used by module-level helpers."""
        return self._grant_still_valid(grant)

    def grant_boundary(self):
        """Public form of the grant/write serialization boundary."""
        return self._grant_boundary()

    @contextmanager
    def _grant_boundary(self):
        """Serialize a final grant check with the write that follows it."""
        if self._lock_path is None:
            yield
            return
        with file_lock(
            self._lock_path.with_name("grant.lock"),
            timeout=0.05,
            conflict_message="sync authorization is changing",
        ):
            yield

    def _grant_still_valid(self, grant) -> bool:
        if self._grant_check is None:
            return True
        try:
            return bool(self._grant_check(grant))
        except Exception:  # noqa: BLE001 - an unreadable grant is not a valid grant
            return False

    def new_op_id(self) -> str:
        """Public operation-id allocator used by the mixin scan path."""
        return self._new_op_id()

    def _new_op_id(self) -> str:
        prefix = f"{self._receipt_epoch}:" if self._receipt_epoch else ""
        return f"{prefix}{secrets.token_hex(16)}"

    def recover_source(self, source, security):
        return _recover_source(self, source, security)

    def recover_space(
        self, security, space, *, data_dir=None, install_id=None, pending_prefix=None
    ):
        return _recover_space(
            self,
            security,
            space,
            data_dir=data_dir,
            install_id=install_id,
            pending_prefix=pending_prefix,
        )

    def resolve(self, *args, **kwargs):
        return _resolve(self, *args, **kwargs)

    def delete_outputs(self, security, unit_ids, *, space=None):
        return _delete_outputs(self, security, unit_ids, space=space)



def _now() -> float:
    import time

    return time.time()


def _sync_metadata(item: ImportItem, digest: str, source_key: str, host: str) -> dict[str, str]:
    metadata = _metadata(item, digest, source_key)
    metadata["source_app"] = host
    return metadata


def _add_sync(
    api,
    text: str,
    scope: Scope,
    *,
    security,
    item: ImportItem,
    digest: str,
    source_key: str,
    host: str,
    op_id: str,
    occurred_at=None,
):
    """Write one source item through the ordinary ``add`` port.

    Creation-only sync attribution travels in ``system_metadata`` like the other
    source keys: the kernel accepts it at the creation boundary and only preserves
    it afterwards (dedup merges and updates never rewrite it). ``op_id`` doubles as
    the receipt key, so a lost response can be resolved by ``operation_receipt``.
    """
    metadata = _sync_metadata(item, digest, source_key, host)
    if op_id:
        metadata["sync_created_by"] = op_id
        if item.kind != "session_turn":
            # Direct writes produce one unit whose content is the source text. Session
            # extraction produces derived units with different content, so the ledger
            # records each output's own initial digest instead of stamping the source
            # digest onto units it does not describe.
            metadata["sync_initial_digest"] = digest
    return api.add(
        text,
        scope,
        security=security,
        system_metadata=metadata,
        occurred_at=occurred_at,
        operation_id=op_id or None,
    )


def _install_actor(install):
    actor = getattr(install, "actor", None)
    return actor if actor is not None else Scope(org="local")




@dataclass(frozen=True)
class RecoveryVerdict:
    """Read-only classification of one interrupted sync source."""

    kind: Literal["receipt", "partial_clean", "unconfirmed", "resend"]
    refs: tuple[str, ...] = ()
    created: tuple[bool, ...] = ()
    partial_units: tuple[str, ...] = ()


def _receipt_outputs(receipt) -> list[tuple[str, bool]]:
    outputs = []
    for item in getattr(receipt, "outputs", ()):
        outputs.append((str(item["unit_id"]), bool(item.get("created", False))))
    return outputs


def _source_key(source: SourceRow) -> str:
    return f"{source.grant_id}|{source.target_space}|{source.root}|{source.item_id}"


def _legacy_user_key(
    grant_id: str, target_space: str, root: str, item_id: str
) -> str:
    """Return the old whole-file USER.md key for a split per-entry item."""
    kind, _, rest = item_id.partition("|")
    collection, sep, inner = rest.partition("|")
    if kind != "profile" or not sep:
        return ""
    if not collection.endswith(":USER") or inner == "file":
        return ""
    return f"{grant_id}|{target_space}|{root}|profile|{collection}|file"


def _old_refs_key(self, source: SourceRow) -> str:
    """Find the key that owns ``source.old_refs`` (normally its own source key)."""
    legacy = _legacy_user_key(
        source.grant_id, source.target_space, source.root, source.item_id
    )
    if legacy:
        parts = source.item_id.split("|", 2)
        legacy_item = f"profile|{parts[1]}|file"
        if (
            self.ledger.get(source.install_id, legacy) is not None
            or self.ledger.get_source(
                source.install_id, source.root, legacy_item, source.target_space
            )
            is not None
        ):
            return legacy
    return _source_key(source)


def _recover_source(self, source: SourceRow, security=None) -> RecoveryVerdict:
    if source.state in {"prepared", "retryable"}:
        return RecoveryVerdict("resend")
    if source.state in {"writing", "unconfirmed"}:
        probe_checked = False
        # 能力先声明再探测：supports_operation_receipt() 为真时三个回执成员必然可调。
        if source.op_id and self.api.supports_operation_receipt():
            probe = self.api.operation_receipt
            try:
                actor = getattr(getattr(security, "auth", None), "actor", None)
                org = getattr(actor, "org", "")
                try:
                    receipt = probe(
                        source.op_id,
                        Scope(org=org, space=source.target_space),
                        security=security,
                    )
                except TypeError:
                    receipt = probe(
                        source.op_id, Scope(org=org, space=source.target_space)
                    )
                probe_checked = True
            except Exception:  # noqa: BLE001 - read-only probe
                receipt = None
            if receipt is not None:
                outputs = _receipt_outputs(receipt)
                return RecoveryVerdict(
                    "receipt",
                    tuple(unit_id for unit_id, _ in outputs),
                    tuple(created for _, created in outputs),
                )
        outputs = self.ledger.outputs_by_op(source.op_id)
        if outputs:
            refs = tuple(row.unit_id for row in outputs)
            created = tuple(row.origin == "sync" for row in outputs)
            if all(created):
                return RecoveryVerdict("receipt", refs, created)
            return RecoveryVerdict(
                "partial_clean", refs=refs, created=created, partial_units=refs
            )
        if probe_checked:
            # 回执因容量上限被淘汰：不能证明未提交，同样按未知处理。
            try:
                if self.api.receipt_evicted(source.op_id, security=security):
                    return RecoveryVerdict("unconfirmed")
            except Exception:  # noqa: BLE001 - an unreadable eviction mark is unknown
                return RecoveryVerdict("unconfirmed")
        # A missing receipt is not proof that the write did not commit: the kernel
        # may have partially written before raising, and reload/restart clears the
        # in-process table. Only an explicit ``prepared``/``retryable`` row is safe
        # to resend.
        return RecoveryVerdict("unconfirmed")
    if source.state == "ok":
        return RecoveryVerdict("receipt", tuple(source.refs))
    return RecoveryVerdict("unconfirmed")


def _register_receipt_refs(
    self,
    source: SourceRow,
    refs: list[str],
    created: tuple[bool, ...] | None = None,
    *,
    security=None,
) -> None:
    for index, unit_id in enumerate(refs):
        is_created = True if created is None else bool(created[index])
        if self.ledger.get_output(unit_id) is None:
            content = ""
            if security is not None:
                try:
                    unit = self.api.get(
                        unit_id,
                        Scope(
                            org=security.auth.actor.org,
                            space=source.target_space,
                        ),
                        security=security,
                    )
                    content = str(getattr(unit, "content", ""))
                except Exception:  # noqa: BLE001 - attribution still comes from receipt
                    content = ""
            self.ledger.put_output(
                OutputRow(
                    unit_id=unit_id,
                    target_space=source.target_space,
                    origin="sync" if is_created else "external",
                    created_by_op=source.op_id if is_created else "",
                    initial_digest=_digest(content) if content else "",
                    refcount=0,
                )
            )
        # References are applied atomically by ``record_recovered``.


def _delete_outputs(self, security, unit_ids: list[str], *, space: str):
    deleted: list[str] = []
    retained: list[str] = []
    if space and self.ledger.unconfirmed_ops(space):
        # Unknown references may exist anywhere in this space; no destructive
        # cleanup is safe until every operation is conclusive.
        return deleted, list(unit_ids)
    org = security.auth.actor.org
    for unit_id in unit_ids:
        output = self.ledger.get_output(unit_id)
        if output is None:
            retained.append(unit_id)
            continue
        try:
            unit = self.api.get(unit_id, Scope(org=org, space=space), security=security)
        except Exception:  # noqa: BLE001 - transient read failure is not a delete verdict
            retained.append(unit_id)
            continue
        if unit is None:
            # The user already removed it, or another path did; cleanup is complete.
            self.ledger.delete_pending(unit_id, space)
            deleted.append(unit_id)
            continue
        metadata = getattr(unit, "system_metadata", {}) or {}
        current_digest = (
            hashlib.sha256(str(getattr(unit, "content", "")).strip().encode("utf-8")).hexdigest()
            if unit is not None else ""
        )
        if (
            unit is not None
            and output.initial_digest
            and current_digest != output.initial_digest
        ):
            self.ledger.protect([unit_id], "content changed after sync")
            retained.append(unit_id)
            continue
        allowed, reason = can_delete(
            output, {str(key): str(value) for key, value in (metadata or {}).items()}
        )
        if not allowed:
            # A temporary co-owner is not a permanent protection: leave it pending so
            # a later cleanup can delete it once the other source is gone.
            if reason != "unit still has references":
                self.ledger.protect([unit_id], reason)
            retained.append(unit_id)
            continue
        if not output.initial_digest:
            self.ledger.protect([unit_id], "missing initial digest")
            retained.append(unit_id)
            continue
        conditional = getattr(self.api, "supports_conditional_delete", None)
        if not callable(conditional) or not conditional():
            # Without compare-and-delete, a concurrent edit after the local digest
            # check could be deleted. Keep the unit and report the capability gap.
            self.ledger.mark_retained([unit_id], "conditional delete unsupported")
            retained.append(unit_id)
            continue
        selector = DeleteSelector(
            unit_ids=[unit_id],
            scope=Scope(
                org=security.auth.actor.org,
                space=space or output.target_space,
            ),
            expected_digests={unit_id: output.initial_digest},
        )
        try:
            removed = self.api.delete(selector, security=security)
        except Exception:  # noqa: BLE001 - transient failures stay pending, not protected
            retained.append(unit_id)
            continue
        if unit_id not in removed:
            # The delete result is authoritative: a raced content change leaves the
            # unit alive even though the local pre-check passed.
            retained.append(unit_id)
            continue
        self.ledger.delete_pending(unit_id, space)
        deleted.append(unit_id)
    return deleted, retained


def _recover_space(
    self,
    security,
    space: str,
    *,
    data_dir: Path | None = None,
    install_id: str | None = None,
    pending_prefix: str | None = None,
):
    report = SpaceRecoveryReport()
    if data_dir is None:
        data_dir = self.ledger.path.parent
    other_blocked = False
    with space_lock(space, data_dir):
        if not self.grant_still_valid(None):
            report.authorization_changed = True
            return report
        for op_id in self.ledger.unconfirmed_ops(space):
            unresolved = False
            other_install = False
            for source in self.ledger.sources_by_op(op_id):
                if not self.grant_still_valid(None):
                    report.authorization_changed = True
                    return report
                if install_id is not None and source.install_id != install_id:
                    other_install = True
                    continue
                # cleanup_pending is already a conclusive state. Replaying the
                # receipt here would add a new ref and release the old one again.
                if source.state in {"cleanup_pending", "ok"}:
                    continue
                verdict = _recover_source(self, source, security)
                if verdict.kind == "receipt":
                    _register_receipt_refs(
                        self, source, list(verdict.refs), verdict.created, security=security
                    )
                    source.refs = list(verdict.refs)
                    old_key = _old_refs_key(self, source)
                    released: list[str] = []
                    if source.intent == "replace" and source.old_refs:
                        new_refs = set(source.refs)
                        released = [
                            unit_id
                            for unit_id in source.old_refs
                            if old_key != _source_key(source) or unit_id not in new_refs
                        ]
                        source.pending_refs = list(released)
                        source.state = "cleanup_pending" if released else "ok"
                    else:
                        source.pending_refs = []
                        source.state = "ok"
                    self.ledger.record_recovered(
                        source,
                        list(verdict.refs),
                        source.digest,
                        _now(),
                        old_key=old_key,
                        pending_reason="recovered replace old output",
                        pending_op=op_id,
                    )
                    report.recovered.append(op_id)
                elif verdict.kind == "partial_clean":
                    source.pending_refs = list(verdict.partial_units)
                    source.state = "cleanup_pending"
                    self.ledger.put_source(source)
                    report.partial.append(op_id)
                elif verdict.kind == "resend":
                    source.state = "retryable"
                    self.ledger.put_source(source)
                else:
                    unresolved = True
                    report.unconfirmed.append(op_id)
            if other_install:
                other_blocked = True
            if unresolved:
                scoped_pending = []
                for row in self.ledger.pending_for_space(space):
                    if pending_prefix is None or row.source_key.startswith(pending_prefix):
                        scoped_pending.append(row)
                report.pending_cleanups = len(scoped_pending)
                report.retained = sorted({row.unit_id for row in scoped_pending})
                return report
        if report.unconfirmed or other_blocked:
            scoped_pending = []
            for row in self.ledger.pending_for_space(space):
                if pending_prefix is None or row.source_key.startswith(pending_prefix):
                    scoped_pending.append(row)
            report.pending_cleanups = len(scoped_pending)
            report.retained = sorted({row.unit_id for row in scoped_pending})
            return report
        for source in self.ledger.sources_for_space(space):
            if not self.grant_still_valid(None):
                report.authorization_changed = True
                return report
            if install_id is not None and source.install_id != install_id:
                continue
            if source.state != "cleanup_pending" or not source.pending_refs:
                continue
            key = _old_refs_key(self, source)
            for unit_id in source.pending_refs:
                self.ledger.put_pending(
                    PendingCleanup(
                        unit_id,
                        space,
                        key,
                        "recovered pending cleanup",
                        source.op_id,
                        _now(),
                    )
                )
        for cleanup in self.ledger.pending_for_space(space):
            try:
                with self.grant_boundary():
                    if not self.grant_still_valid(None):
                        report.authorization_changed = True
                        return report
                    if (
                        pending_prefix is not None
                        and not cleanup.source_key.startswith(pending_prefix)
                    ):
                        continue
                    deleted, retained = self.delete_outputs(
                        security, [cleanup.unit_id], space=space
                    )
            except Exception:  # noqa: BLE001 - another writer holds the boundary
                report.authorization_changed = True
                return report
            report.deleted.extend(deleted)
            report.retained.extend(retained)
        remaining_rows = [
            row
            for row in self.ledger.pending_for_space(space)
            if pending_prefix is None or row.source_key.startswith(pending_prefix)
        ]
        remaining = {row.unit_id for row in remaining_rows}
        for source in self.ledger.sources_for_space(space):
            if install_id is not None and source.install_id != install_id:
                continue
            if source.state != "cleanup_pending":
                continue
            tracked = set(source.old_refs) | set(source.refs) | set(source.pending_refs)
            if not (tracked & remaining):
                self.ledger.update_source_state(source, "ok")
        report.pending_cleanups = len(remaining)
    return report


def _resolve(
    self,
    security,
    op_id: str,
    action: Literal["adopt", "abandon", "discard"] | None = None,
    *,
    replace_choice: Literal["keep_old", "cleanup_old"] | None = None,
    space: str | None = None,
) -> list[str]:
    if action is None and op_id in {"adopt", "abandon", "discard"}:
        security, op_id, action = None, str(security), op_id
    if action is None:
        raise ValueError("resolve action is required")
    sources = self.ledger.sources_by_op(op_id)
    if not sources:
        raise ValueError(f"unknown sync operation: {op_id}")
    changed: list[str] = []
    for source in sources:
        if space is not None and source.target_space != space:
            continue
        if action == "adopt":
            verdict = _recover_source(self, source, security)
            refs = list(verdict.refs) or list(source.pending_refs) or list(source.refs)
            if source.intent == "replace" and replace_choice is None:
                raise ValueError("replace adopt requires replace_choice")
            if source.intent == "replace" and replace_choice == "keep_old":
                self.ledger.protect(source.old_refs, "replaced output kept")
            old_key = _old_refs_key(self, source)
            released: list[str] = []
            if source.intent == "replace" and replace_choice == "cleanup_old":
                new_refs = set(refs)
                released = [
                    unit_id
                    for unit_id in source.old_refs
                    if old_key != _source_key(source) or unit_id not in new_refs
                ]
            if refs:
                _register_receipt_refs(
                    self, source, refs, verdict.created, security=security
                )
            source.refs = refs
            source.pending_refs = list(released)
            source.state = "cleanup_pending" if released else "ok"
            self.ledger.record_recovered(
                source,
                refs,
                source.digest,
                _now(),
                old_key=old_key,
                release_old=replace_choice != "keep_old",
                pending_reason="replaced old output",
                pending_op=op_id,
            )
            changed.append(source.item_id)
        elif action == "abandon":
            self.ledger.protect(
                list(dict.fromkeys([*source.refs, *source.pending_refs])),
                "abandoned sync operation",
            )
            self.ledger.release_refs(_source_key(source), source.refs)
            source.pending_refs = []
            source.state = "abandoned"
            self.ledger.put_source(source)
            changed.append(source.item_id)
        elif action == "discard":
            key = _source_key(source)
            discarded = list(dict.fromkeys([*source.refs, *source.pending_refs]))
            self.ledger.release_refs(key, discarded)
            for unit_id in discarded:
                self.ledger.put_pending(
                    PendingCleanup(
                        unit_id, source.target_space, key, "discard",
                        op_id, _now(),
                    )
                )
            source.pending_refs = list(dict.fromkeys([*source.refs, *source.pending_refs]))
            source.refs = []
            source.state = "abandoned"
            self.ledger.put_source(source)
            changed.append(source.item_id)
        else:
            raise ValueError(f"unknown resolve action: {action}")
    return changed
