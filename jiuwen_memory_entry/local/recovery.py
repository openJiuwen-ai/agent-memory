# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Background advancement of non-terminal turns (F17 D8 / Task 12).

The service owns recovery: every few seconds it retries each non-terminal operation
through its own loopback API with the *install's* token, so the normal PEP decides
whether the write may still proceed. Requests past retention are discarded first; an
operation without its frozen request can only be queried, never replayed.
"""

from __future__ import annotations

import logging
import threading

from jiuwen_memory_entry.local.client import ServiceClient
from jiuwen_memory_entry.local.turns import Operation, TurnStore

logger = logging.getLogger("agent-memory.local")

NON_TERMINAL = ("prepared", "partial", "unknown")
RECOVERY_INTERVAL_SECONDS = 5.0


class OperationRecovery:
    def __init__(
        self,
        home,
        discovery_provider,
        turns: TurnStore,
        *,
        interval: float = RECOVERY_INTERVAL_SECONDS,
        receipt_probe=None,
    ) -> None:
        self._home = home
        self._discovery = discovery_provider
        self._turns = turns
        self._receipt_probe = receipt_probe
        self.interval = interval

    def run_once(self) -> int:
        """Advance every non-terminal operation once; returns attempts made."""
        tries = 0
        self._turns.purge_expired()
        self._turns.expire_requests()
        discovery = self._discovery()
        if discovery is None:
            return 0
        for operation in self._turns.list_operations(NON_TERMINAL):
            token = self._token(operation)
            if not token:
                # A missing credential is a permanent local condition; keep the receipt
                # and stop partial writes just like an explicit revocation.
                self._probe(operation, permission_lost=True)
                continue
            tries += 1
            try:
                status, _body = ServiceClient(discovery, token).post(
                    "/local/hook/retry", {"local_op_id": operation.local_op_id}
                )
            except Exception as exc:  # noqa: BLE001 - transient; retry next tick
                logger.debug(
                    "recovery attempt failed local_op=%s error=%s",
                    operation.local_op_id,
                    type(exc).__name__,
                )
                self._probe(operation, permission_lost=False)
                continue
            if status == 200:
                continue
            if status in (401, 403):
                # The token file still exists but the credential was revoked.
                self._probe(operation, permission_lost=True)
                continue
            # 409/5xx/validation are transient: never turn them into a terminal stop;
            # only a read-only receipt refresh is allowed here.
            self._probe(operation, permission_lost=False)
        return tries

    def _probe(self, operation, *, permission_lost: bool = False) -> None:
        if self._receipt_probe is None:
            return
        if not operation.request:
            if permission_lost:
                # No frozen request means there is no scope to probe; a lost credential
                # still makes this operation permanently non-writable. Keep the previous
                # receipt IDs/refs: they are the only remaining trace of the partial
                # write, and op show gates their visibility on read permission.
                previous = dict(operation.receipt or {})
                previous.update(
                    {
                        "status": "stopped",
                        "reason": "write permission revoked; request expired",
                    }
                )
                self._turns.update_operation(
                    operation.local_op_id, status="stopped", receipt=previous
                )
            return
        try:
            receipt = self._receipt_probe(operation)
        except Exception:  # noqa: BLE001 - read-only check, retried next tick
            return
        if receipt is None:
            return
        status = str(getattr(receipt, "status", "unknown"))
        refs = list(getattr(receipt, "refs", None) or [])
        receipt_id = str(getattr(receipt, "operation_id", ""))
        if permission_lost and status in {"partial", "unknown"}:
            # F17: a revoked credential cannot drive retry_action; stop and keep the
            # receipt so only status/refs remain queryable.
            status = "stopped"
        self._turns.update_operation(
            operation.local_op_id,
            status=status,
            receipt={
                "status": status,
                "operation_id": receipt_id,
                "refs": refs,
                "retry_action": str(getattr(receipt, "retry_action", "")),
                "reason": "write permission revoked" if permission_lost else "",
            },
        )

    def _token(self, operation: Operation) -> str:
        path = self._home.installs_dir / operation.install_id / "token"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - never kill the service
                logger.warning("recovery tick failed: %s", type(exc).__name__)
        # One last chance for operations frozen just before shutdown.
        try:
            self.run_once()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def start(
        home, discovery_provider, turns: TurnStore, stop: threading.Event, *,
        receipt_probe=None,
    ):
        recovery = OperationRecovery(
            home, discovery_provider, turns, receipt_probe=receipt_probe
        )
        thread = threading.Thread(target=recovery.loop, args=(stop,), daemon=True)
        thread.start()
        return recovery, thread
