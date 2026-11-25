# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Process-local, read-only Redis snapshots for a single engine's shared KV store."""

from __future__ import annotations

import logging
import re
import struct
import tempfile
import threading
import zlib
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from time import monotonic

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.log import scope_for_log
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage._support import scope_segments, wrap_backend
from jiuwen_memory.storage.space_queries import UsageRow, belongs_to_space, metadata_owner

log = logging.getLogger(__name__)
DAY = 86400
BACKFILL_INTERVAL = 300
META = b":_:_:_:/space/info"
Group = tuple[bytes, bytes]
_HEADER = struct.Struct(">Ii")
_ABSENT = -1
_UNATTRIBUTED = -3  # Write observation only; never encoded in a scan partition.
_OWNER_PREFIXES = (b"/memory/", b"/messages/", b"/space/")


@dataclass(slots=True)
class _KeyWriter:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


def _zero():
    return [0, 0, 0]


def _add(row, logical, count, size):
    if logical.startswith(b"/memory/"):
        row[0] += count
    elif logical.startswith(b"/messages/"):
        row[1] += count
    row[2] += size


def _owned_size(name, raw, owner):
    if not isinstance(raw, bytes):
        return _UNATTRIBUTED
    _, _, user, agent, session, key = name.split(b":", 5)
    try:
        logical = key.decode()
    except UnicodeDecodeError:
        return _UNATTRIBUTED
    root = user == agent == session == b"_"
    return len(raw) if belongs_to_space(owner, logical, raw, root=root) else _UNATTRIBUTED


def _samples(db, keys, *, ttl_only, shared_owners, skipped):
    """Yield permanent size, -1 for absent, or -(2 + size) for a live TTL value."""
    from redis.exceptions import ResponseError

    pipe = db.pipeline(transaction=False)
    for key in keys:
        pipe.pttl(key)
    deadlines = pipe.execute()
    selected = [k for k, ttl in zip(keys, deadlines, strict=True)
                if ttl >= 0 or (ttl == -1 and not ttl_only)]
    for key in selected:
        if shared_owners and tuple(key.split(b":", 2)[:2]) in shared_owners:
            pipe.get(key)
        else:
            pipe.strlen(key)
    lengths = iter(pipe.execute(raise_on_error=False))
    for key, ttl in zip(keys, deadlines, strict=True):
        value = _ABSENT
        if ttl >= 0 or (ttl == -1 and not ttl_only):
            size = next(lengths)
            if isinstance(size, ResponseError):
                if not str(size).startswith("WRONGTYPE"):
                    raise size
            elif size is not None:
                if isinstance(size, bytes):
                    group = tuple(key.split(b":", 2)[:2])
                    size = _owned_size(key, size, shared_owners[group])
                    if size == _UNATTRIBUTED:
                        skipped.add(group)
                if size >= 0:
                    value = size if ttl == -1 else -size - 2
        yield key, value


def _write_image(
    db, name: bytes, literal_owner: bool
) -> tuple[int, tuple[str, str] | None]:
    """Inspect the existing business key in one round trip, using only reads."""
    org, space, tail = name.split(b":", 2)
    group = org, space
    read_owner = not literal_owner and _has_underscore(group)
    read_value = read_owner and name.split(b":", 5)[5].startswith(_OWNER_PREFIXES)
    if b"_" in group and not read_value:
        return _UNATTRIBUTED, None
    pipe = db.pipeline(transaction=False)
    pipe.pttl(name)
    if read_owner and tail != META[1:]:
        pipe.get(org + b":" + space + META)
    if read_value:
        pipe.get(name)
    else:
        pipe.strlen(name)
    ttl, *metadata, size = pipe.execute(raise_on_error=False)
    for reply in (ttl, *metadata):
        if isinstance(reply, Exception):
            raise reply
    if isinstance(size, Exception):
        if str(size).startswith("WRONGTYPE"):
            return _ABSENT, None
        raise size
    if ttl == -2 or size is None:
        return _ABSENT, None
    owner = None
    if read_owner:
        owner = metadata_owner(metadata[0] if metadata else size)
        if not _literal_owner(group, owner):
            size = _owned_size(name, size, owner)
            if size == _UNATTRIBUTED:
                return size, owner
        elif read_value:
            size = len(size)
    return (size if ttl == -1 else -2), owner


def _has_underscore(group: Group) -> bool:
    return any(b"_" in segment for segment in group)


def _literal_owner(group: Group, owner: tuple[str, str] | None) -> bool:
    """An ordinary registered name such as team_ops owns its physical prefix."""
    return (owner is not None and b"_" not in group
            and group == (owner[0].encode("utf-8", "surrogatepass"),
                          owner[1].encode("utf-8", "surrogatepass")))


class _Partitions:
    """Deduplicate SCAN in bounded batches; temporary files belong to this build."""
    def __init__(self, stack: ExitStack):
        self.files = [stack.enter_context(tempfile.TemporaryFile()) for _ in range(64)]
        self.bytes_written = 0

    def append(self, key, value):
        if value != _ABSENT:
            record = _HEADER.pack(len(key), value) + key
            self.files[zlib.crc32(key) % len(self.files)].write(record)
            self.bytes_written += len(record)

    def totals(self, before, check):
        perm, ttl = defaultdict(_zero), defaultdict(_zero)
        for f in self.files:
            check()
            f.seek(0)
            unique = {}
            for header in iter(lambda: f.read(_HEADER.size), b""):
                length, size = _HEADER.unpack(header)
                unique[f.read(length)] = size
            for key, size in unique.items():
                org, space, _, _, _, logical = key.split(b":", 5)
                if size < _ABSENT:
                    _add(ttl[org, space], logical, 1, -size - 2)
                elif key not in before:
                    _add(perm[org, space], logical, 1, size)
        # A concurrent delete can make a key disappear from SCAN completely.
        for key, size in before.items():
            if size >= 0:
                org, space, _, _, _, logical = key.split(b":", 5)
                _add(perm[org, space], logical, 1, size)
        return perm, ttl


class RedisSpaceCache:
    """Track one engine KV view. Redis holds only the original business records.

    A build samples values as it scans. Local writes collect each touched key's
    first permanent image and all subsequent deltas. Once the scan has finished,
    those images replace its observations, including keys it missed after DELETE.
    Adding the deltas under the publish barrier brings the baseline up to date.
    TTL observations are published separately and stay fixed between daily scans.
    """
    def __init__(self, client: Callable, batch_size: int):
        self._client = client
        self._batch = batch_size
        self._lock = threading.RLock()
        self._phase = threading.Condition(self._lock)
        self._writers: dict[bytes, _KeyWriter] = {}
        self._publishing = False
        self._lifecycle = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._users = 0
        self._worker = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._stamp = None
        self._reset()

    def _reset(self):
        self._generation = object()
        self._directory: dict[bytes, dict[bytes, object]] | None = None
        self._directory_changes: dict[Group, bool] | None = None
        self._permanent: dict[Group, list[int]] = {}
        self._ttl: dict[Group, list[int]] = {}
        self._building: set[Group] = set()
        self._before: dict[bytes, int] | None = None
        self._delta = defaultdict(_zero)
        self._pending: set[Group] = set()
        self._backfill_at = 0.0
        self._eviction_retry_at = 0.0
        self._next_daily = 0.0
        self._logical_owners: dict[Group, tuple[str, str] | None] = {}
        self._warned: set[Group] = set()
        self._live_reads: dict[Group, tuple[int, Future[UsageRow]]] = {}

    @contextmanager
    def _barrier(self):
        with self._phase:
            self._phase.wait_for(lambda: not self._publishing)
            self._publishing = True
            try:
                self._phase.wait_for(lambda: not self._writers)
                yield
            finally:
                self._publishing = False
                self._phase.notify_all()

    @contextmanager
    def _key_writer(self, name):
        with self._phase:
            self._phase.wait_for(lambda: not self._publishing)
            writer = self._writers.get(name)
            if writer is None:
                writer = self._writers[name] = _KeyWriter()
            writer.users += 1
        try:
            with writer.lock:
                yield
        finally:
            with self._phase:
                writer.users -= 1
                if not writer.users:
                    del self._writers[name]
                if self._publishing and not self._writers:
                    self._phase.notify_all()

    def start(self):
        with self._lifecycle:
            self._users += 1
            if self._worker is None:
                self._stop.clear()
                self._worker = threading.Thread(target=self._run, name="space-cache", daemon=True)
                self._worker.start()

    def release(self):
        with self._lifecycle:
            self._users = max(0, self._users - 1)
            if not self._users:
                self._shutdown()

    def close(self):
        with self._lifecycle:
            self._users = 0
            self._shutdown()

    def _shutdown(self):
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join()
            self._worker = None
        with self._barrier():
            self._reset()
            self._stamp = None

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.refresh()
            except Exception as exc:
                if not self._stop.is_set():
                    log.warning("Redis space cache refresh unavailable (%s)", type(exc).__name__)
            self._wake.wait(30)

    def _source(self):
        # Redis can restart, change binding or evict a permanent key without a KV call.
        db = self._client()
        pipe = db.pipeline(transaction=False)
        pipe.info("server")
        pipe.info("stats")
        server, stats = pipe.execute()
        return db, server["run_id"], stats["evicted_keys"]

    def _current_source(self):
        try:
            stamp = self._source()
        except Exception:
            stamp = None
        with self._lock:
            if stamp is not None and self._stamp is not None and stamp[:2] == self._stamp[:2]:
                return stamp
            self._reset()
            self._stamp = None
            self._wake.set()
            return None

    def metadata(self, org: str) -> list[bytes] | None:
        prefix = scope_segments(Scope(org=org))[0].encode()
        with self._lock:
            if self._directory is None:
                return None
        if self._current_source() is None:
            return None
        with self._lock:
            if self._directory is None:
                return None
            keys = [prefix + b":" + name + META for name in self._directory.get(prefix, ())]
            db = self._stamp[0]
        try:
            values = []
            for i in range(0, len(keys), self._batch):
                values.extend(raw for raw in db.mget(keys[i:i + self._batch]) if raw is not None)
            return values
        except Exception:
            return None

    def usage(
        self, org: str, space: str, *, read_current: Callable[[], UsageRow] | None = None
    ) -> UsageRow | None:
        org_segment, space_segment, *_ = scope_segments(Scope(org=org, space=space))
        group = org_segment.encode(), space_segment.encode()
        with self._lock:
            if group not in self._permanent:
                return None
        stamp = self._current_source()
        if stamp is None:
            return None
        with self._lock:
            permanent = self._permanent.get(group)
            if group in self._logical_owners and self._logical_owners[group] != (org, space):
                return None
            if permanent is None:
                return None
            if stamp == self._stamp and monotonic() < self._next_daily:
                return tuple(a + b for a, b in zip(permanent, self._ttl[group], strict=True))
            if read_current is None:
                return None
            flight = self._live_reads.get(group)
            if flight is None or flight[0] != stamp[2]:
                pending = Future()
                flight = (stamp[2], pending)
                self._live_reads[group] = flight
                leader = True
            else:
                pending = flight[1]
                leader = False
        if not leader:
            return pending.result()
        try:
            row = read_current()
            pending.set_result(row)
            return row
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._live_reads.get(group) is flight:
                    del self._live_reads[group]

    def _membership(self, group, exists):
        org, space = group
        if self._directory_changes is not None:
            self._directory_changes[group] = exists
        if self._directory is None:
            return
        if exists:
            names = self._directory.setdefault(org, {})
            if space not in names:
                names[space] = object()
                if not self._pending:
                    self._backfill_at = monotonic() + BACKFILL_INTERVAL
                self._pending.add(group)
        else:
            names = self._directory.get(org, {})
            names.pop(space, None)
            if not names:
                self._directory.pop(org, None)
            self._pending.discard(group)
            self._warned.discard(group)
            self._logical_owners.pop(group, None)
            self._permanent.pop(group, None)
            self._ttl.pop(group, None)

    @contextmanager
    def mutation(
        self, namespaced: Callable[[Scope, str], str], scope: Scope, key: str,
        *, create=False, value=None, ttl=0.0, delete=False,
    ):
        if self._stamp is None:
            # A first build may start while this unobserved business operation is in flight.
            try:
                yield
            finally:
                if self._stamp is not None:
                    with self._lock:
                        self._invalidate(self._generation)
            return
        try:
            name = namespaced(scope, key).encode()
        except (AttributeError, TypeError, UnicodeError):
            yield
            return
        org, space, tail = name.split(b":", 2)
        group = org, space
        metadata = tail == META[1:]
        with self._key_writer(name):
            with self._lock:
                track = group in self._permanent or group in self._building
                membership = metadata and (
                    self._directory is not None or self._directory_changes is not None
                )
                literal = not metadata and _literal_owner(group, self._logical_owners.get(group))
                gen = self._generation
            if not track and not membership:
                yield
                return
            try:
                db = self._client()
                # Ambiguous prefixes and metadata need the actual value to establish ownership.
                ordinary = not metadata and (literal or not _has_underscore(group))
                if ordinary and create:
                    before, old_owner = _ABSENT, None  # SET NX succeeds only for an absent key.
                else:
                    before, old_owner = _write_image(db, name, literal)
            except Exception:
                self._invalidate(gen)
                yield
                return
            try:
                yield  # Original command, exceptions and conditional-write result.
            except (ConflictError, NotFoundError):
                raise
            except BaseException:
                self._invalidate(gen)  # A connection failure can hide a committed write.
                raise
            try:
                if ordinary and delete:
                    after, new_owner = _ABSENT, None
                elif ordinary and isinstance(value, bytes):
                    after, new_owner = (-2 if ttl > 0 else len(value)), None
                else:
                    after, new_owner = _write_image(db, name, literal)
                logical = name.split(b":", 5)[5]
                with self._lock:
                    # A request following this write must not join an earlier Redis scan.
                    self._live_reads.pop(group, None)
                    if gen is not self._generation:
                        return
                    if membership:
                        # A metadata key may have expired before a same-name create.
                        if after != _ABSENT and (before == _ABSENT or new_owner != old_owner):
                            self._membership(group, False)
                        self._membership(group, after != _ABSENT)
                    if track:
                        if _UNATTRIBUTED in (before, after):
                            self._warn_skipped(group)
                        delta_count = int(after >= 0) - int(before >= 0)
                        delta_size = max(0, after) - max(0, before)
                        if group in self._building:
                            if self._before is not None:
                                self._before.setdefault(name, before)
                            _add(self._delta[group], logical, delta_count, delta_size)
                        if group in self._permanent:
                            _add(self._permanent[group], logical, delta_count, delta_size)
            except Exception:
                self._invalidate(gen)  # Accounting failure cannot fail a business write.

    def _warn_skipped(self, group):
        if group not in self._warned:
            self._warned.add(group)
            org, space = (segment.decode(errors="replace") for segment in group)
            log.warning("Redis space usage skipped records without verified space ownership; "
                        "scope=%s", scope_for_log(Scope(org=org, space=space)))

    def _invalidate(self, gen):
        with self._lock:
            if self._generation is gen:
                self._reset()
                self._stamp = None
                self._wake.set()

    def _check(self, gen):
        if self._stop.is_set() or self._generation is not gen:
            raise RuntimeError("Redis space cache build superseded")

    def _pages(self, db, pattern, gen):
        cursor = 0
        while True:
            self._check(gen)
            cursor, page = db.scan(cursor, match=pattern, count=self._batch)
            yield page
            if not cursor:
                break

    def refresh(self):
        with self._refresh_lock:
            stamp = self._source()
            db = stamp[0]
            with self._lock:
                directory_needed = (self._directory is None or self._stamp is None
                                    or stamp[:2] != self._stamp[:2])
                evicted = not directory_needed and stamp != self._stamp
                now = monotonic()
                backfill_due = bool(self._pending) and now >= self._backfill_at
                if evicted and now < self._eviction_retry_at:
                    return
                usage_due = evicted or now >= self._next_daily or backfill_due
                if not directory_needed and not usage_due:
                    return  # Idle polling never waits for in-flight writes.
                gen = self._generation
            if directory_needed:
                with self._barrier():
                    if stamp != self._stamp:
                        self._reset()
                        self._stamp = stamp
                    gen = self._generation
                    self._directory_changes = {}
                found = set()
                for page in self._pages(db, b"*:*" + META, gen):
                    for key in page:
                        parts = key.split(b":", 2)
                        if len(parts) == 3 and parts[2] == META[1:]:
                            found.add((parts[0], parts[1]))
                with self._barrier():
                    self._check(gen)
                    changes = self._directory_changes
                    self._directory_changes = None
                    self._directory = {}
                    for group in found:
                        if changes.get(group, True):
                            self._membership(group, True)
                    for group, exists in changes.items():
                        if exists:
                            self._membership(group, True)
                log.info("Redis local space directory ready")
            with self._barrier():
                self._check(gen)
                started = monotonic()
                daily = evicted or started >= self._next_daily
                if not daily and (not self._pending or started < self._backfill_at):
                    return
                missing = self._pending.copy()
                if daily:
                    groups = {(org, space): token for org, names in self._directory.items()
                              for space, token in names.items()}
                    if evicted:
                        missing = set(groups)
                else:
                    groups = {group: self._directory[group[0]][group[1]] for group in missing}
                if evicted:
                    self._eviction_retry_at = started + BACKFILL_INTERVAL
                self._building = missing
                self._before = {} if missing else None
                self._delta.clear()
            try:
                with ExitStack() as stack:
                    data = _Partitions(stack)
                    aliases = [g for g in groups if _has_underscore(g)]
                    owners = {}
                    for offset in range(0, len(aliases), self._batch):
                        batch = aliases[offset:offset + self._batch]
                        raw = db.mget([org + b":" + space + META for org, space in batch])
                        owners.update(zip(batch, map(metadata_owner, raw), strict=True))
                    shared = {group: owner for group, owner in owners.items()
                              if not _literal_owner(group, owner)}
                    skipped = set()
                    pages = self._pages(db, b"*:*:*:*:*:*", gen) if groups else ()
                    for page in pages:
                        initial, sampled = [], []
                        for key in page:
                            org, space, _ = key.split(b":", 2)
                            group = org, space
                            if group not in groups:
                                continue
                            if group in shared and not key.split(b":", 5)[5].startswith(
                                _OWNER_PREFIXES
                            ):
                                skipped.add(group)
                                continue
                            (initial if group in missing else sampled).append(key)
                        for keys, ttl_only in ((initial, False), (sampled, True)):
                            if keys:
                                for key, value in _samples(
                                    db, keys, ttl_only=ttl_only,
                                    shared_owners=shared, skipped=skipped
                                ):
                                    data.append(key, value)
                    with self._barrier():
                        self._check(gen)
                        before = self._before or {}
                        self._before = None
                    perm, ttl = data.totals(before, lambda: self._check(gen))
                current = self._source()
                if current[:2] != stamp[:2]:
                    self._invalidate(gen)
                    return
                if current != stamp:
                    # INFO cannot identify the evicted key. Keep the directory, whose entries
                    # are re-read, and throttle full usage rebuilds under sustained pressure.
                    with self._lock:
                        self._check(gen)
                        self._eviction_retry_at = monotonic() + BACKFILL_INTERVAL
                    return
                with self._barrier():
                    self._check(gen)
                    for (org, space), token in groups.items():
                        group = org, space
                        if self._directory.get(org, {}).get(space) is not token:
                            continue
                        if group in owners:
                            self._logical_owners[group] = owners[group]
                            if group in skipped:
                                self._warn_skipped(group)
                        self._ttl[group] = ttl[group]
                        if group in missing:
                            self._pending.discard(group)
                            self._permanent[group] = [
                                a + b for a, b in zip(perm[group], self._delta[group], strict=True)
                            ]
                    self._stamp = stamp
                    if daily:
                        self._next_daily = started + DAY
                log.info("Redis local space usage ready; temporary_bytes=%d", data.bytes_written)
            finally:
                with self._barrier():
                    if self._generation is gen:
                        if self._pending:
                            self._backfill_at = monotonic() + BACKFILL_INTERVAL
                        self._building = set()
                        self._before = None
                        self._delta.clear()


class RedisSpaceReader:
    """Read the two space projections from this plain Redis view, with an optional cache."""

    def __init__(self, client: Callable, batch_size: int, cache: RedisSpaceCache | None):
        self._client = client
        self._batch = batch_size
        self._cache = cache

    def start(self) -> None:
        if self._cache is not None:
            self._cache.start()

    def release(self) -> None:
        if self._cache is not None:
            self._cache.release()

    def _pages(self, db, prefix: bytes, suffix: bytes):
        # Only query-owned scans use literal Redis coordinates. SCAN may repeat keys.
        pattern = re.sub(rb"([\\*?\[\]])", rb"\\\1", prefix) + suffix
        seen = set()
        cursor = 0
        while True:
            cursor, page = db.scan(cursor, match=pattern, count=self._batch)
            keys = [key for key in dict.fromkeys(page) if key not in seen]
            seen.update(keys)
            if keys:
                yield keys
            if not cursor:
                break

    def metadata(self, org: str) -> list[bytes]:
        if self._cache is not None:
            rows = self._cache.metadata(org)
            if rows is not None:
                return rows
        prefix = scope_segments(Scope(org=org))[0].encode() + b":"
        with wrap_backend("redis space directory"):
            db = self._client()
            rows = []
            for page in self._pages(db, prefix, b"*" + META):
                # A nested business key may have the same suffix.
                pipe = db.pipeline(transaction=False)
                for key in page:
                    if key.split(b":", 2)[2] == META[1:]:
                        pipe.get(key)
                rows.extend(value for value in pipe.execute() if value is not None)
            return rows

    def usage(self, org: str, space: str) -> UsageRow:
        if self._cache is not None:
            row = self._cache.usage(org, space, read_current=lambda: self._scan_usage(org, space))
            if row is not None:
                return row
        return self._scan_usage(org, space)

    def _scan_usage(self, org: str, space: str) -> UsageRow:
        logical_scope = Scope(org=org, space=space)
        first, second, *_ = scope_segments(logical_scope)
        group = first.encode(), second.encode()
        prefix = b":".join(group)
        shared = _has_underscore(group)
        owner = org, space
        row = _zero()
        skipped = False
        with wrap_backend("redis space usage"):
            db = self._client()
            if shared:
                raw, = db.mget([prefix + META])
                stored_owner = metadata_owner(raw)
                if stored_owner is not None and stored_owner != owner:
                    return 0, 0, 0
                shared = not _literal_owner(group, owner)
            for keys in self._pages(db, prefix + b":", b"*:*:*:*"):
                for key, raw in zip(keys, db.mget(keys), strict=True):
                    if raw is None:
                        continue
                    if shared and _owned_size(key, raw, owner) == _UNATTRIBUTED:
                        skipped = True
                        continue
                    _add(row, key.split(b":", 5)[5], 1, len(raw))
        if skipped:
            log.warning("Redis space usage skipped records without verified space ownership; "
                        "scope=%s", scope_for_log(logical_scope))
        return tuple(row)
