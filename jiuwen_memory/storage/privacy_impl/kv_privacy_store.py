# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Backend-independent private state with atomic per-Scope KV snapshots.

Single-key CAS publishes fragments, manifests and recovery references together.
No multi-key transaction or unsafe read/update emulation is assumed. Concurrent
publication conflicts fail explicitly; the external truth-write protocol owns
reconciliation. Ciphertext fields remain opaque to this adapter.
"""

from __future__ import annotations

import base64
import copy
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.security.capabilities import CloseableSecurityCapability
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.privacy import (
    FragmentReference,
    PrivacySnapshot,
    PrivacyStore,
    PrivacyStoreProducer,
    scope_key,
)

STATE_KEY = "/privacy/state/v1"


@dataclass
class _Record:
    snapshot: PrivacySnapshot
    tokens: tuple[str, ...]


@dataclass
class _State:
    fragments: dict[str, FragmentReference] = field(default_factory=dict)
    active: dict[str, _Record] = field(default_factory=dict)
    recovery: dict[str, dict[str, _Record]] = field(default_factory=dict)


@dataclass
class _Frame:
    expected: bytes | None
    state: _State


def _encode_record(record: _Record) -> dict:
    snapshot = record.snapshot
    return {
        "revision": snapshot.revision,
        "digest": snapshot.digest,
        "value": base64.b64encode(snapshot.value).decode(),
        "tokens": list(record.tokens),
    }


def _decode_record(unit: str, value: dict) -> _Record:
    snapshot = PrivacySnapshot(
        unit, value["revision"], value["digest"], base64.b64decode(value["value"], validate=True)
    )
    if not all(isinstance(item, str) for item in (unit, snapshot.revision, snapshot.digest)):
        raise ValueError("invalid private record identity")
    tokens = value["tokens"]
    if not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens):
        raise ValueError("invalid private record")
    return _Record(snapshot, tuple(tokens))


def _decode(value: bytes | None, limit: int) -> _State:
    if value is None:
        return _State()
    if len(value) > limit:
        raise BackendError("privacy KV state exceeds configured limit")
    try:
        data = json.loads(value)
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unknown private state format")
        fragments = {}
        for token, fields in data["fragments"].items():
            cipher, level, origin = fields
            if not all(isinstance(item, str) for item in (token, level, origin)):
                raise ValueError("invalid fragment reference")
            fragments[token] = FragmentReference(
                base64.b64decode(cipher, validate=True), level, origin
            )
        active = {unit: _decode_record(unit, record) for unit, record in data["active"].items()}
        recovery = {
            unit: {rev: _decode_record(unit, record) for rev, record in records.items()}
            for unit, records in data["recovery"].items()
        }
        return _State(fragments, active, recovery)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise BackendError("invalid privacy KV state") from exc


def _encode(state: _State, limit: int) -> bytes | None:
    if not state.active and not state.recovery and not state.fragments:
        return None
    data = {
        "version": 1,
        "fragments": {
            token: [base64.b64encode(ref.value).decode(), ref.level, ref.origin]
            for token, ref in state.fragments.items()
        },
        "active": {unit: _encode_record(record) for unit, record in state.active.items()},
        "recovery": {
            unit: {rev: _encode_record(record) for rev, record in records.items()}
            for unit, records in state.recovery.items()
        },
    }
    value = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(value) > limit:
        raise ValidationError("privacy KV state exceeds configured limit")
    return value


class KVPrivacyStore(PrivacyStore):
    """Private record adapter over any KVStore with native conditional replacement."""

    def __init__(self, kv: KVStore, *, max_state_bytes: int = 67108864, owns_kv: bool = False):
        if not kv.supports_compare_and_swap():
            raise ValidationError("privacy KV requires native compare_and_swap support")
        if (
            not isinstance(max_state_bytes, int)
            or isinstance(max_state_bytes, bool)
            or max_state_bytes <= 0
        ):
            raise ValidationError("invalid privacy KV state limit")
        self._kv, self._limit = kv, max_state_bytes
        self._lock = threading.RLock()
        self._frames: dict[str, _Frame] = {}
        self._owned_stores: list[KVStore] = []
        self._providers: list[object] = []
        if owns_kv:
            current = kv
            seen = set()
            while True:
                if id(current) in seen:
                    raise ValidationError("cyclic private KV encryption dependencies")
                seen.add(id(current))
                self._owned_stores.append(current)
                binding = current.security.encryption_binding
                if binding is None:
                    break
                self._providers.append(binding.provider)
                current = binding.inner

    def _frame(self, scope: Scope) -> _Frame:
        return self._frames[scope_key(scope)]

    @contextmanager
    def transaction(self, scope: Scope):
        with self._lock:
            namespace = scope_key(scope)
            nested = self._frames.get(namespace)
            if nested is not None:
                before = copy.deepcopy(nested.state)
                try:
                    yield
                except BaseException:
                    nested.state = before
                    raise
                return
            try:
                expected = self._kv.get(scope, STATE_KEY)
            except NotFoundError:
                expected = None
            frame = _Frame(expected, _decode(expected, self._limit))
            self._frames[namespace] = frame
            try:
                yield
                value = _encode(frame.state, self._limit)
                if value != expected:
                    if not self._kv.compare_and_swap(scope, STATE_KEY, expected, value):
                        raise ConflictError("privacy state changed; retry the operation")
            finally:
                self._frames.pop(namespace, None)

    @staticmethod
    def stage_fragment(scope: Scope, token: str, value: bytes, level: str) -> FragmentReference:
        return FragmentReference(value, level, scope_key(scope))

    def reference(self, scope: Scope, token: str) -> FragmentReference:
        with self.transaction(scope):
            try:
                return self._frame(scope).state.fragments[token]
            except KeyError as exc:
                raise NotFoundError("privacy fragment is unavailable") from exc

    def get_fragment(self, scope: Scope, token: str) -> bytes:
        return self.reference(scope, token).value

    def get(self, scope: Scope, unit: str) -> PrivacySnapshot:
        with self.transaction(scope):
            try:
                return self._frame(scope).state.active[unit].snapshot
            except KeyError as exc:
                raise NotFoundError("privacy snapshot is unavailable") from exc

    def search(self, scope: Scope, units: list[str], *, revision: str | None = None):
        with self.transaction(scope):
            state = self._frame(scope).state
            found = []
            for unit in dict.fromkeys(units):
                records = [state.active[unit]] if unit in state.active else []
                records += list(state.recovery.get(unit, {}).values())
                found.extend(
                    record.snapshot
                    for record in records
                    if revision is None or record.snapshot.revision == revision
                )
            return found

    def insert(self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict) -> None:
        with self.transaction(scope):
            if snapshot.unit in self._frame(scope).state.active:
                raise ConflictError("privacy snapshot already exists")
            self._replace(scope, snapshot, fragments)

    def update(self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict) -> None:
        with self.transaction(scope):
            if snapshot.unit not in self._frame(scope).state.active:
                raise NotFoundError("privacy snapshot is unavailable")
            self._replace(scope, snapshot, fragments)

    def _replace(self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict) -> None:
        state = self._frame(scope).state
        for token, ref in fragments.items():
            state.fragments.setdefault(token, ref)
        state.active[snapshot.unit] = _Record(snapshot, tuple(sorted(fragments)))
        self._collect(scope)

    def checkpoint(self, scope: Scope, unit: str) -> None:
        with self.transaction(scope):
            state = self._frame(scope).state
            if unit in state.active:
                record = state.active[unit]
                state.recovery.setdefault(unit, {}).setdefault(record.snapshot.revision, record)

    def confirm(self, scope: Scope, unit: str) -> None:
        with self.transaction(scope):
            self._frame(scope).state.recovery.pop(unit, None)
            self._collect(scope)

    def reconcile(self, scope: Scope, unit: str, revision: str, digest: str) -> None:
        with self.transaction(scope):
            state = self._frame(scope).state
            current = state.active.get(unit)
            if current is None or (current.snapshot.revision, current.snapshot.digest) != (
                revision,
                digest,
            ):
                recovered = state.recovery.get(unit, {}).get(revision)
                if recovered is None or recovered.snapshot.digest != digest:
                    return
                state.active[unit] = recovered
            self.confirm(scope, unit)

    def delete(self, scope: Scope, units: list[str] | None) -> set[str]:
        with self.transaction(scope):
            state = self._frame(scope).state
            selected = set(state.active) | set(state.recovery) if units is None else units
            tokens = set()
            for unit in selected:
                active = state.active.pop(unit, None)
                if active:
                    tokens.update(active.tokens)
                for record in state.recovery.pop(unit, {}).values():
                    tokens.update(record.tokens)
            self._collect(scope)
            return tokens

    def _collect(self, scope: Scope) -> None:
        state = self._frame(scope).state
        referenced = {token for record in state.active.values() for token in record.tokens}
        for records in state.recovery.values():
            for record in records.values():
                referenced.update(record.tokens)
        state.fragments = {
            token: ref for token, ref in state.fragments.items() if token in referenced
        }

    def scopes(self) -> list[Scope]:
        with self._lock:
            return [scope for scope in self._kv.scopes() if self._kv.exists(scope, STATE_KEY)]

    def health(self) -> None:
        self._kv.health()

    def security_dependencies(self) -> tuple[object, ...]:
        return tuple(self._providers)

    def close(self) -> None:
        for store in self._owned_stores:
            if isinstance(store, CloseableSecurityCapability):
                store.close()


@PrivacyStoreProducer.register("kv")
def _build(config):
    kv = KvProducer.build_private(config)
    return KVPrivacyStore(kv, max_state_bytes=config.get("max_state_bytes", 67108864), owns_kv=True)
