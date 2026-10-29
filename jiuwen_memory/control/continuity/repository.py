"""Single-writer control records through the named StoreManager KV port.

Records and retry receipts are committed together. This is deliberately not a
multi-process compare-and-swap implementation.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.continuity import canonical, fingerprint
from jiuwen_memory.storage.kv import KVStore


class ContinuityRepository:
    def __init__(self, kv: KVStore, *, single_writer: bool, allow_ephemeral: bool = False):
        if not single_writer:
            raise ValidationError("continuity requires a single writer service")
        if not kv.persistent_atomic_records and not allow_ephemeral:
            raise ValidationError("continuity requires a fixed durable atomic-record KV port")
        if not allow_ephemeral:
            kv.claim_single_writer("continuity")
        self.kv = kv
        self.lock = threading.RLock()

    def get(self, scope: Scope, kind: str, key: str) -> dict:
        return json.loads(self.kv.get(scope, f"/continuity/{kind}/{key}"))

    def save(self, scope: Scope, kind: str, key: str, record: dict) -> None:
        path = f"/continuity/{kind}/{key}"
        data = canonical(record).encode()
        if self.kv.exists(scope, path):
            self.kv.update(scope, path, data)
        else:
            self.kv.insert(scope, path, data)

    def list(self, scope: Scope, kind: str) -> list[dict]:
        return [json.loads(value) for _, value in self.kv.scan(scope, f"/continuity/{kind}/")]

    def mutate(
        self,
        scope: Scope,
        kind: str,
        key: str,
        expected: int,
        request_id: str,
        payload: dict,
        change: Callable[[dict], None],
        *,
        initial: dict | None = None,
    ) -> dict:
        digest = fingerprint(payload)
        with self.lock:
            try:
                record = self.get(scope, kind, key)
            except NotFoundError:
                if initial is None:
                    raise
                record = deepcopy(initial)
            receipts = record.setdefault("receipts", {})
            receipt = receipts.get(request_id)
            if receipt:
                if receipt["digest"] != digest:
                    raise ConflictError(
                        "request", message="request_id reused with different content"
                    )
                return {"revision": receipt["revision"], "replayed": True}
            if record.get("revision", 0) != expected:
                raise ConflictError("revision", message="stale control revision")
            change(record)
            record["revision"] = expected + 1
            receipts[request_id] = {"digest": digest, "revision": record["revision"]}
            # Fixed receipt window. Old revisions still cannot be replayed.
            while len(receipts) > 256:
                oldest_request = min(receipts, key=lambda request: receipts[request]["revision"])
                del receipts[oldest_request]
            self.save(scope, kind, key, record)
            return {"revision": record["revision"], "replayed": False}
