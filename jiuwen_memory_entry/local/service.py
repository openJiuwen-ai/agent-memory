# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The resident local memory service process (F17 D2 / D5 / D7).

One process per data directory. It owns the single-writer Registry, serves the
existing ``/v1`` contract on loopback plus the ``/local/*`` management endpoints,
publishes a discovery file and stays resident while document watching is enabled.

Run as ``python -m jiuwen_memory_entry.local.service <data-dir>`` (started by
:mod:`jiuwen_memory_entry.local.discovery`, not by users directly).
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jiuwen_memory_entry.local.client import ServiceClient  # noqa: F401  (re-export convenience)
from jiuwen_memory_entry.local.home import MemoryHome, resolve_home

_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.append(str(_src))

from jiuwen_memory.api import (  # noqa: E402
    AgentMemoryError,
    AuthenticationError,
    LocalTokenStore,
    Scope,
    SpaceContentRole,
    SpaceGovernanceRole,
    SpaceMember,
    Surface,
    ValidationError,
)
from jiuwen_memory_entry.core.auth_middleware import (  # noqa: E402
    authenticated,
    credentials_from_headers,
)
from jiuwen_memory_entry.core.config_loader import load_layer  # noqa: E402
from jiuwen_memory_entry.core.profiles import load_config  # noqa: E402
from jiuwen_memory_entry.http_server.__main__ import (  # noqa: E402
    HttpServer,
    _BoundedThreadingHTTPServer,
    _parse_content_length,
    _read_body,
)
from jiuwen_memory_entry.local import discovery  # noqa: E402
from jiuwen_memory_entry.local.hooks import (  # noqa: E402
    CaptureRequest,
    HookService,
    InstallPolicy,
    RecallExecutor,
    RecallRequest,
)
from jiuwen_memory_entry.local.install import _install_dir, _read_record  # noqa: E402
from jiuwen_memory_entry.local.turns import TurnStore  # noqa: E402

logger = logging.getLogger("agent-memory.local")

LEASE_SECONDS = 60.0
IDLE_EXIT_SECONDS = 600.0
MAX_LOCAL_BODY_BYTES = 1024 * 1024


class _LocalHttpServer(HttpServer):
    """The shared HTTP surface plus ``/local/*`` management routes."""

    def __init__(self, config, runtime, security_runtime, *, service: LocalService) -> None:
        super().__init__(config, runtime, security_runtime)
        self._service = service

    def handler_cls(self):
        base = super().handler_cls()
        srv = self
        service = self._service

        class Handler(base):
            def handle_one_request(self) -> None:
                service.touch()
                with service.in_flight():
                    super().handle_one_request()

            def handle_get(self) -> None:
                path = self.path.rstrip("/")
                if path == "/local/health":
                    return srv.local_health(self)
                if path == "/local/status":
                    return srv.local_status(self)
                if path == "/local/egress":
                    return srv.local_egress(self)
                if service.reloading:
                    return self.send(
                        503, {"error": "Reloading"}, request_id=uuid.uuid4().hex
                    )
                super().handle_get()
                return None

            def handle_post(self) -> None:
                path = self.path.rstrip("/")
                if path == "/local/lease":
                    return srv.local_lease(self)
                if path == "/local/reload":
                    return srv.local_reload(self)
                if path == "/local/egress":
                    return srv.local_egress(self)
                if path == "/local/recover-admin":
                    return srv.local_recover_admin(self)
                if path == "/local/op":
                    return srv.local_op(self)
                if path == "/local/sync":
                    return srv.local_sync(self)
                if path == "/local/sync/mirror":
                    return srv.local_sync_mirror(self)
                if path == "/local/sync/resolve":
                    return srv.local_sync_resolve(self)
                if path == "/local/sync/recover":
                    return srv.local_sync_recover(self)
                if path == "/local/sync/status":
                    return srv.local_sync_status(self)
                if path == "/local/sync/revoke":
                    return srv.local_sync_revoke(self)
                if path in {
                    "/local/hook/recall",
                    "/local/hook/capture",
                    "/local/hook/retry",
                }:
                    return srv.local_hook(self)
                if service.reloading:
                    return self.send(
                        503, {"error": "Reloading"}, request_id=uuid.uuid4().hex
                    )
                super().handle_post()
                return None

            def handle_delete(self) -> None:
                if self.path.rstrip("/") == "/local/lease":
                    return srv.local_release(self)
                super().handle_unsupported()
                return None

        setattr(Handler, "do_GET", Handler.handle_get)
        setattr(Handler, "do_POST", Handler.handle_post)
        setattr(Handler, "do_DELETE", Handler.handle_delete)
        return Handler

    # -- public route facade for the nested handler (G.CLS.11) ----------------- #
    # ``Handler`` lives outside this class body, so it may only call public
    # methods; each entry delegates to the private implementation unchanged.

    def local_health(self, handler) -> None:
        self._local_health(handler)

    def local_status(self, handler) -> None:
        self._local_status(handler)

    def local_lease(self, handler) -> None:
        self._local_lease(handler)

    def local_release(self, handler) -> None:
        self._local_release(handler)

    def local_reload(self, handler) -> None:
        self._local_reload(handler)

    def local_recover_admin(self, handler) -> None:
        self._local_recover_admin(handler)

    def local_op(self, handler) -> None:
        self._local_op(handler)

    def local_sync(self, handler) -> None:
        self._local_sync(handler)

    def local_sync_mirror(self, handler) -> None:
        self._local_sync_mirror(handler)

    def local_sync_resolve(self, handler) -> None:
        self._local_sync_resolve(handler)

    def local_sync_recover(self, handler) -> None:
        self._local_sync_recover(handler)

    def local_sync_status(self, handler) -> None:
        self._local_sync_status(handler)

    def local_sync_revoke(self, handler) -> None:
        self._local_sync_revoke(handler)

    def local_egress(self, handler) -> None:
        self._local_egress(handler)

    def local_hook(self, handler) -> None:
        self._local_hook(handler)

    # -- local endpoint helpers ------------------------------------------------ #

    def _request_id(self) -> str:
        return uuid.uuid4().hex

    @contextmanager
    def _security(self, handler, request_id: str):
        runtime = self.security_runtime
        if runtime is None:
            raise AuthenticationError("authentication unavailable")
        peer = handler.client_address[0] if handler.client_address else ""
        with authenticated(
            runtime.authenticator,
            credentials_from_headers(handler.headers, peer_address=peer),
            audit=getattr(runtime, "audit", None) or self.audit,
            limiter=getattr(runtime, "rate_limiter", None),
            workload_guard=getattr(runtime, "workload_guard", None),
            surface=Surface.HTTP,
            request_id=request_id,
        ) as security:
            yield security
            self.register_profile_owner(security)

    def _json_body(self, handler) -> dict | None:
        request_id = self._request_id()
        length_status, length = _parse_content_length(
            handler.headers, max_body_bytes=MAX_LOCAL_BODY_BYTES
        )
        if length_status == 413:
            handler.send(413, {"error": "PayloadTooLarge"}, request_id=request_id)
            return None
        if length_status == 400:
            handler.send(400, {"error": "BadRequest"}, request_id=request_id)
            return None
        raw = _read_body(handler.rfile, length)
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            handler.send(400, {"error": "BadRequest"}, request_id=request_id)
            return None
        if not isinstance(body, dict):
            handler.send(400, {"error": "BadRequest"}, request_id=request_id)
            return None
        return body

    def _local_health(self, handler) -> None:
        """Unauthenticated liveness probe carrying the discovery nonce (Review Focus 1)."""
        handler.send(
            200,
            {
                "home": self._service.home.canonical,
                "instance_nonce": self._service.nonce,
                "protocol": discovery.PROTOCOL_VERSION,
                "pid": os.getpid(),
            },
            request_id=self._request_id(),
        )

    def _local_status(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                handler.send(
                    200,
                    {
                        "home": self._service.home.canonical,
                        "instance_nonce": self._service.nonce,
                        "protocol": discovery.PROTOCOL_VERSION,
                        "detached": self._service.detached,
                        "reloading": self._service.reloading,
                    },
                    request_id=security.request_id or request_id,
                )
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)

    def _local_lease(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                client_id = str(body.get("client_id", "")).strip()
                if not client_id:
                    raise ValidationError("client_id is required")
                self._service.lease(client_id)
                handler.send(
                    200, {"expires_in": LEASE_SECONDS}, request_id=security.request_id
                )
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except ValidationError as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)

    def _local_release(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                self._service.release(str(body.get("client_id", "")).strip())
                handler.send(200, {"released": True}, request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)

    def _local_reload(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                self._service.request_reload()
                handler.send(202, {"reloading": True}, request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)

    def _local_recover_admin(self, handler) -> None:
        """Local break-glass maintenance: same space component, no token elevation.

        The caller proves filesystem access with a one-time 0600 secret written by the
        CLI; the service then calls the space manager's member maintenance operation and
        invalidates its fact cache. Deliberately not authenticated with an install token:
        the recovering install has no space governance right by definition.
        """
        request_id = self._request_id()
        body = self._json_body(handler)
        if body is None:
            return
        secret = str(body.get("secret", ""))
        path = self._service.home.data_dir / "recovery.secret"
        try:
            expected = path.read_text(encoding="utf-8").strip()
        except OSError:
            expected = ""
        if not expected or not hmac.compare_digest(secret, expected):
            handler.send(403, {"error": "Forbidden"}, request_id=request_id)
            return
        try:
            org = str(body["org"])
            space = str(body["space"])
            scope_data = dict(body["member"]["scope"])
            # Space membership is a user/agent principal in the target space; session
            # is a runtime dimension and is deliberately not stored in the member row.
            scope_data["space"] = space
            scope_data["session"] = ""
            scope = Scope(**scope_data)
            member = SpaceMember(
                scope=scope,
                content_role=SpaceContentRole.CONTRIBUTOR,
                governance_role=SpaceGovernanceRole.MANAGER,
            )
            self._service.add_space_member(org, space, member)
            path.unlink(missing_ok=True)
            handler.send(
                200, {"state": "complete", "space": space}, request_id=request_id
            )
        except (KeyError, TypeError, ValidationError) as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001 - report, never drop the connection
            logger.exception("recover-admin failed")
            handler.send(
                500, {"error": f"{type(exc).__name__}: {exc}"}, request_id=request_id
            )

    def _local_op(self, handler) -> None:
        """Serve op show through the authenticated service so read permission applies."""
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                local_op_id = str(body.get("local_op_id", ""))
                policy = self._service.install_policy(security)
                operation = self._service.turn_store().get_operation_by_id(local_op_id)
                if operation is None or operation.install_id != policy.install_id:
                    handler.send(404, {"error": "NotFound"}, request_id=request_id)
                    return
                receipt = operation.receipt or {}
                visible = {
                    "local_op_id": operation.local_op_id,
                    "status": operation.status,
                }
                api = self._service.server.api
                refs = [
                    item for item in receipt.get("refs") or []
                    if isinstance(item, dict) and item.get("unit_id")
                ]
                readable = bool(refs)
                for item in refs:
                    try:
                        api.get(
                            str(item["unit_id"]),
                            Scope(**dict(item.get("scope") or {})),
                            security=security,
                        )
                    except Exception:  # noqa: BLE001 - permission gate, hide details
                        readable = False
                        break
                if readable:
                    visible.update(
                        {
                            "event_id": operation.event_id,
                            "kernel_operation_id": receipt.get("operation_id", ""),
                            "task_id": operation.task_id,
                            "binding_id": operation.binding_id,
                            "refs": [item["unit_id"] for item in refs],
                        }
                    )
                handler.send(200, visible, request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except ValidationError as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001 - never drop the connection
            logger.exception("op show failed")
            handler.send(
                500, {"error": f"{type(exc).__name__}: {exc}"}, request_id=request_id
            )

    def _local_sync(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                from jiuwen_memory_entry.local.install import _load_install

                policy = self._service.install_policy(security)
                install = _load_install(self._service.home, policy.install_id)
                if install is None:
                    handler.send(404, {"error": "NotFound"}, request_id=request_id)
                    return
                from jiuwen_memory_entry.local.sync.grant import SyncGrant

                grant = SyncGrant.from_dict(install.sync or {})
                if grant is None or grant.revoked or not grant.roots:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return
                forbidden = {"dir", "project", "sessions", "types", "mirror_deletes", "roots"}
                if forbidden & set(body):
                    handler.send(
                        400, {"error": "sync scope is fixed by the grant"}, request_id=request_id
                    )
                    return
                from jiuwen_memory_entry.local.sync.access import SourceAccess
                from jiuwen_memory_entry.local.sync.engine import SyncEngine
                from jiuwen_memory_entry.local.sync.ledger import SyncLedger
                from jiuwen_memory_entry.local.sync.model import SyncReport
                from jiuwen_memory_entry.local.sync.readers import READERS

                reader_factory = READERS.get(install.host)
                if reader_factory is None:
                    handler.send(
                        400, {"error": f"no reader for {install.host}"}, request_id=request_id
                    )
                    return
                project = grant.target_space or str(
                    (self._service.settings.get("local") or {}).get(
                        "default_space", "default"
                    )
                )

                plan = None
                if not body.get("dry_run") and not body.get("plan_id"):
                    handler.send(
                        400,
                        {"error": "sync apply requires a dry-run plan_id"},
                        request_id=request_id,
                    )
                    return
                if body.get("plan_id"):
                    from jiuwen_memory_entry.local.sync.plan import PLANS

                    plan = PLANS.get(str(body["plan_id"]))
                    if plan is None:
                        handler.send(
                            400,
                            {"error": "unknown or expired sync plan"},
                            request_id=request_id,
                        )
                        return
                    plan_matches = (
                        plan.install_id == install.install_id
                        and plan.grant_id == grant.grant_id
                        and plan.grant_version == grant.version
                        and plan.target_space == project
                    )
                    if not plan_matches:
                        handler.send(
                            400,
                            {"error": "sync plan no longer matches grant"},
                            request_id=request_id,
                        )
                        return
                since = None
                if body.get("since"):
                    from datetime import datetime

                    since = datetime.fromisoformat(str(body["since"]))
                ledger = SyncLedger(self._service.home.data_dir / "local_sync.db")
                try:
                    engine = SyncEngine(
                        self._service.api,
                        ledger,
                        lock_path=self._service.sync_lock_path(install.install_id),
                        receipt_epoch=self._service.api.receipt_epoch,
                        grant_check=self._service.sync_grant_check(install, grant),
                    )
                    report = SyncReport()
                    reader = reader_factory()
                    if plan is not None and hasattr(reader, "scan"):
                        from jiuwen_memory_entry.local.sync.plan import (
                            PlanChanged,
                            diff_plan,
                        )

                        try:
                            preview = engine.run_scan(
                                security,
                                install,
                                reader,
                                SourceAccess(grant),
                                grant,
                                dry_run=True,
                                since=since,
                                limit=int(body["limit"]) if body.get("limit") else None,
                            )
                        except PlanChanged as exc:
                            logger.warning("sync plan changed during scan: %s", exc)
                            raise
                        try:
                            diff_plan(plan, preview.writes, frozenset(preview.deletes))
                        except PlanChanged as exc:
                            handler.send(
                                409,
                                {"error": str(exc), "changed": True},
                                request_id=request_id,
                            )
                            return
                    if not hasattr(reader, "scan"):
                        handler.send(
                            400,
                            {"error": f"reader for {install.host} has no scan port"},
                            request_id=request_id,
                        )
                        return
                    report = engine.run_scan(
                        security,
                        install,
                        reader,
                        SourceAccess(grant),
                        grant,
                        dry_run=bool(body.get("dry_run", False)),
                        since=since,
                        limit=int(body["limit"]) if body.get("limit") else None,
                    )
                finally:
                    ledger.close()
                if plan is not None:
                    from jiuwen_memory_entry.local.sync.plan import PLANS

                    PLANS.pop(plan.plan_id)
                if body.get("dry_run"):
                    from jiuwen_memory_entry.local.sync.plan import PLANS, SyncPlan

                    plan = PLANS.put(
                        SyncPlan(
                            install_id=install.install_id,
                            grant_id=grant.grant_id,
                            grant_version=grant.version,
                            target_space=project,
                            writes=dict(report.writes),
                            deletes=frozenset(report.deletes),
                        )
                    )
                    report.plan_id = plan.plan_id
                handler.send(200, report.as_dict(), request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except (ValueError, TypeError) as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001 - never drop the connection
            logger.exception("sync failed")
            handler.send(
                500, {"error": f"{type(exc).__name__}: {exc}"}, request_id=request_id
            )


    def _local_sync_resolve(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                from jiuwen_memory_entry.local.install import _load_install
                from jiuwen_memory_entry.local.sync.engine import SyncEngine
                from jiuwen_memory_entry.local.sync.grant import SyncGrant
                from jiuwen_memory_entry.local.sync.ledger import SyncLedger

                policy = self._service.install_policy(security)
                install = _load_install(self._service.home, policy.install_id)
                grant = SyncGrant.from_dict(install.sync or {}) if install else None
                if install is None or grant is None:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return
                action = str(body.get("action") or "")
                if action not in {"adopt", "abandon", "discard"}:
                    handler.send(400, {"error": "unknown resolve action"}, request_id=request_id)
                    return
                choice = body.get("replace_choice")
                if choice is not None and choice not in {"keep_old", "cleanup_old"}:
                    handler.send(400, {"error": "unknown replace_choice"}, request_id=request_id)
                    return
                ledger = SyncLedger(self._service.home.data_dir / "local_sync.db")
                try:
                    from jiuwen_memory_entry.local.sync.space import space_lock

                    engine = SyncEngine(
                        self._service.api, ledger, receipt_epoch=self._service.api.receipt_epoch
                    )
                    with space_lock(grant.target_space, self._service.home.data_dir):
                        changed = engine.resolve(
                            security,
                            str(body.get("op") or ""),
                            action,
                            replace_choice=choice,
                            space=grant.target_space,
                        )
                finally:
                    ledger.close()
                handler.send(200, {"changed": changed}, request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except (ValueError, TypeError) as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync resolve failed")
            handler.send(500, {"error": type(exc).__name__}, request_id=request_id)

    def _local_sync_recover(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                self._json_body(handler)
                from dataclasses import asdict

                from jiuwen_memory_entry.local.install import _load_install
                from jiuwen_memory_entry.local.sync.engine import SyncEngine
                from jiuwen_memory_entry.local.sync.grant import SyncGrant
                from jiuwen_memory_entry.local.sync.ledger import SyncLedger

                policy = self._service.install_policy(security)
                install = _load_install(self._service.home, policy.install_id)
                grant = SyncGrant.from_dict(install.sync or {}) if install else None
                if install is None or grant is None:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return
                ledger = SyncLedger(self._service.home.data_dir / "local_sync.db")
                try:
                    engine = SyncEngine(
                        self._service.api,
                        ledger,
                        lock_path=self._service.sync_lock_path(install.install_id),
                        receipt_epoch=self._service.api.receipt_epoch,
                        grant_check=self._service.sync_grant_check(install, grant),
                    )
                    report = engine.recover_space(
                        security,
                        grant.target_space,
                        data_dir=self._service.home.data_dir,
                        install_id=install.install_id,
                        pending_prefix=f"{grant.grant_id}|{grant.target_space}|",
                    )
                finally:
                    ledger.close()
                handler.send(200, asdict(report), request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync recover failed")
            handler.send(500, {"error": type(exc).__name__}, request_id=request_id)


    def _local_sync_status(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                self._json_body(handler)
                from jiuwen_memory_entry.local.install import _load_install
                from jiuwen_memory_entry.local.sync.grant import SyncGrant
                from jiuwen_memory_entry.local.sync.ledger import SyncLedger

                policy = self._service.install_policy(security)
                install = _load_install(self._service.home, policy.install_id)
                grant = SyncGrant.from_dict(install.sync or {}) if install else None
                if install is None:
                    handler.send(404, {"error": "NotFound"}, request_id=request_id)
                    return
                ledger = SyncLedger(self._service.home.data_dir / "local_sync.db")
                current_keys: set[str] = set()
                if grant is not None and not grant.revoked:
                    from jiuwen_memory_entry.local.sync.access import SourceAccess
                    from jiuwen_memory_entry.local.sync.engine import SyncEngine
                    from jiuwen_memory_entry.local.sync.readers import READERS

                    reader_factory = READERS.get(install.host)
                    if reader_factory is not None:
                        reader = reader_factory()
                        if hasattr(reader, "scan"):
                            engine = SyncEngine(
                                self._service.api,
                                ledger,
                                receipt_epoch=self._service.api.receipt_epoch,
                            )
                            report = engine.run_scan(
                                security,
                                install,
                                reader,
                                SourceAccess(grant),
                                grant,
                                dry_run=True,
                            )
                            current_keys = set(report.writes)
                try:
                    space = grant.target_space if grant is not None else ""
                    ops = []
                    for op in ledger.unconfirmed_ops(space) if space else []:
                        if any(
                            source.install_id == install.install_id
                            for source in ledger.sources_by_op(op)
                        ):
                            ops.append(op)
                    prefix = f"{grant.grant_id}|{space}|" if grant is not None else ""
                    pending = [
                        row
                        for row in ledger.pending_for_space(space)
                        if prefix and row.source_key.startswith(prefix)
                    ] if space else []
                    sources = [
                        source for source in ledger.sources_for_install(install.install_id)
                        if source.target_space == space
                    ]
                    unconfirmed = [
                        {
                            "op": op,
                            "sources": [
                                {
                                    "item_id": source.item_id,
                                    "root": source.root,
                                    "state": source.state,
                                    "intent": source.intent,
                                    "refs": source.refs,
                                    "pending_refs": source.pending_refs,
                                }
                                for source in ledger.sources_by_op(op)
                            ],
                        }
                        for op in ops
                    ]
                finally:
                    ledger.close()

                def source_present(item_id: str) -> bool:
                    suffix = f"|{item_id}"
                    return any(key.endswith(suffix) for key in current_keys)

                source_ids = {source.item_id for source in sources}

                def key_is_known(key: str) -> bool:
                    return any(key.endswith(f"|{item_id}") for item_id in source_ids)

                handler.send(
                    200,
                    {
                        "grant": install.sync or {},
                        "unconfirmed": unconfirmed,
                        "pending_cleanups": len(pending),
                        "retained": sorted({row.unit_id for row in pending}),
                        "missing_from_source": [
                            source.item_id
                            for source in sources
                            if not source_present(source.item_id)
                        ],
                        "new_in_source": sorted(
                            key for key in current_keys if not key_is_known(key)
                        ),
                    },
                    request_id=security.request_id,
                )
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync status failed")
            handler.send(500, {"error": type(exc).__name__}, request_id=request_id)

    def _local_sync_revoke(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler) or {}
                selection = str(body.get("delete") or "keep")
                if selection not in {"keep", "current", "all"}:
                    handler.send(
                        400,
                        {"error": "delete must be keep, current or all"},
                        request_id=request_id,
                    )
                    return
                from jiuwen_memory_entry.local.install import _load_install
                from jiuwen_memory_entry.local.sync.engine import SyncEngine
                from jiuwen_memory_entry.local.sync.grant import SyncGrant
                from jiuwen_memory_entry.local.sync.ledger import SyncLedger

                policy = self._service.install_policy(security)
                install = _load_install(self._service.home, policy.install_id)
                grant = SyncGrant.from_dict(install.sync or {}) if install else None
                if install is None or grant is None:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return
                ledger = SyncLedger(self._service.home.data_dir / "local_sync.db")
                deleted: list[str] = []
                retained: list[str] = []
                try:
                    engine = SyncEngine(
                        self._service.api, ledger, receipt_epoch=self._service.api.receipt_epoch
                    )
                    recovery = engine.recover_space(
                        security,
                        grant.target_space,
                        data_dir=self._service.home.data_dir,
                        install_id=install.install_id,
                        pending_prefix=f"{grant.grant_id}|{grant.target_space}|",
                    )
                    blocked = bool(recovery.unconfirmed)
                    if selection == "keep":
                        sources = []
                    elif selection == "current":
                        sources = [
                            source for source in ledger.sources_for_grant(grant.grant_id)
                            if source.grant_version == grant.version
                        ]
                    else:
                        sources = ledger.sources_for_install(install.install_id)
                    from jiuwen_memory_entry.local.sync.space import space_lock

                    with space_lock(grant.target_space, self._service.home.data_dir):
                        for source in sources:
                            unit_ids = list(dict.fromkeys([*source.refs, *source.pending_refs]))
                            if blocked:
                                retained.extend(unit_ids)
                                continue
                            key = (
                                f"{source.grant_id}|{source.target_space}|"
                                f"{source.root}|{source.item_id}"
                            )
                            for unit_id in unit_ids:
                                ledger.release_refs(key, [unit_id])
                            part_deleted, part_retained = engine.delete_outputs(
                                security, unit_ids, space=source.target_space
                            )
                            deleted.extend(part_deleted)
                            retained.extend(part_retained)
                finally:
                    ledger.close()
                handler.send(
                    200,
                    {"deleted": deleted, "retained": retained},
                    request_id=security.request_id,
                )
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync revoke cleanup failed")
            handler.send(500, {"error": type(exc).__name__}, request_id=request_id)

    def _local_sync_mirror(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                from types import SimpleNamespace

                from jiuwen_memory_entry.local.install import _load_install
                from jiuwen_memory_entry.local.locks import file_lock
                from jiuwen_memory_entry.local.sync.engine import SyncEngine
                from jiuwen_memory_entry.local.sync.grant import SyncGrant
                from jiuwen_memory_entry.local.sync.ledger import (
                    PendingCleanup,
                    SyncLedger,
                )
                from jiuwen_memory_entry.local.sync.model import (
                    ImportItem,
                    ItemId,
                    RangeKey,
                    ScanResult,
                )
                from jiuwen_memory_entry.local.sync.readers import normalized_digest
                from jiuwen_memory_entry.local.sync.space import space_lock

                policy = self._service.install_policy(security)
                install = _load_install(self._service.home, policy.install_id)
                grant = SyncGrant.from_dict(install.sync or {}) if install else None
                if install is None or grant is None or grant.revoked:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return
                content = str(body.get("content") or "").strip()
                target = str(body.get("target") or "memory")
                profile = str(body.get("profile") or "default")
                if target not in {"user", "memory"}:
                    handler.send(400, {"error": "unknown mirror target"}, request_id=request_id)
                    return
                valid_profile = all(
                    (
                        profile,
                        "/" not in profile,
                        "\\" not in profile,
                        profile not in {".", ".."},
                    )
                )
                if not valid_profile:
                    handler.send(400, {"error": "invalid mirror profile"}, request_id=request_id)
                    return
                required_type = "profile" if target == "user" else "fact"
                if required_type not in grant.types:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return
                action = str(body.get("action") or "add")
                if action not in {"add", "delete"}:
                    handler.send(400, {"error": "unknown mirror action"}, request_id=request_id)
                    return
                if not content:
                    handler.send(400, {"error": "missing content"}, request_id=request_id)
                    return
                if not grant.roots:
                    handler.send(403, {"error": "SyncNotAllowed"}, request_id=request_id)
                    return

                def profile_root() -> str:
                    for raw in grant.roots:
                        directory = (
                            Path(raw) / "memories"
                            if profile == "default"
                            else Path(raw) / "profiles" / profile / "memories"
                        )
                        if directory.is_dir():
                            return str(Path(raw))
                    return grant.roots[0]

                root = profile_root()
                if target == "memory":
                    item_id = ItemId(
                        "fact", f"{profile}:MEMORY", normalized_digest(content)
                    )
                else:
                    item_id = ItemId(
                        "profile", f"{profile}:USER", normalized_digest(content)
                    )
                source_key = (
                    f"{grant.grant_id}|{grant.target_space}|{root}|{item_id}"
                )
                item = ImportItem(
                    item_id=item_id,
                    text=content,
                    locator=str(item_id),
                    root=root,
                )
                ledger = SyncLedger(self._service.home.data_dir / "local_sync.db")
                try:

                    def current_grant() -> bool:
                        fresh = _load_install(self._service.home, install.install_id)
                        fresh_grant = (
                            SyncGrant.from_dict(fresh.sync or {}) if fresh else None
                        )
                        return bool(
                            fresh is not None
                            and fresh_grant is not None
                            and not fresh_grant.revoked
                            and fresh_grant.grant_id == grant.grant_id
                            and fresh_grant.version == grant.version
                            and fresh_grant.target_space == grant.target_space
                        )

                    if action == "delete":
                        with file_lock(
                            self._service.sync_lock_path(install.install_id),
                            timeout=0.05,
                            conflict_message="sync already running for this install",
                        ):
                            with space_lock(
                                grant.target_space, self._service.home.data_dir
                            ):
                                if not current_grant():
                                    handler.send(
                                        409,
                                        {"error": "sync grant changed"},
                                        request_id=request_id,
                                    )
                                    return
                                row = ledger.get(install.install_id, source_key)
                                source = ledger.get_source(
                                    install.install_id,
                                    root,
                                    str(item_id),
                                    grant.target_space,
                                )
                                unit_ids = list(row.unit_ids) if row else []
                                if source is not None:
                                    unit_ids.extend(source.refs)
                                    unit_ids.extend(source.pending_refs)
                                unit_ids = list(dict.fromkeys(unit_ids))
                                for unit_id in unit_ids:
                                    ledger.release_refs(source_key, [unit_id])
                                    ledger.put_pending(
                                        PendingCleanup(
                                            unit_id,
                                            grant.target_space,
                                            source_key,
                                            "mirror delete",
                                            "",
                                            time.time(),
                                        )
                                    )
                                engine = SyncEngine(
                                    self._service.api,
                                    ledger,
                                    receipt_epoch=self._service.api.receipt_epoch,
                                )
                                deleted, retained = engine.delete_outputs(
                                    security, unit_ids, space=grant.target_space
                                )
                                ledger.delete(install.install_id, source_key)
                                ledger.delete_source(
                                    install.install_id,
                                    root,
                                    str(item_id),
                                    grant.target_space,
                                )
                                handler.send(
                                    200,
                                    {"deleted": deleted, "retained": retained},
                                    request_id=request_id,
                                )
                                return

                    reader = SimpleNamespace(
                        scan=lambda access, *, types, since, limit: ScanResult(
                            items=[item],
                            complete={RangeKey(root, item.kind or "fact"): True},
                        )
                    )
                    access = SimpleNamespace(truncated=(), skipped=())
                    engine = SyncEngine(
                        self._service.api,
                        ledger,
                        lock_path=self._service.sync_lock_path(install.install_id),
                        receipt_epoch=self._service.api.receipt_epoch,
                        grant_check=lambda _grant: current_grant(),
                    )
                    with file_lock(
                        self._service.sync_lock_path(install.install_id),
                        timeout=0.05,
                        conflict_message="sync already running for this install",
                    ):
                        if not current_grant():
                            handler.send(
                                409,
                                {"error": "sync grant changed"},
                                request_id=request_id,
                            )
                            return
                        report = engine.run_scan(
                            security,
                            install,
                            reader,
                            access,
                            grant,
                            limit=1,
                            install_lock=False,
                        )
                    if report.failed or report.truncated:
                        reason = (
                            report.failed[0][1]
                            if report.failed
                            else report.truncated[0]
                        )
                        handler.send(
                            409,
                            {"error": reason, "conflict": True},
                            request_id=request_id,
                        )
                        return
                    row = ledger.get(install.install_id, source_key)
                    unit_ids = list(row.unit_ids) if row is not None else []
                    if not unit_ids:
                        source = ledger.get_source(
                            install.install_id,
                            root,
                            str(item_id),
                            grant.target_space,
                        )
                        if source is not None:
                            unit_ids = list(source.refs)
                    if not unit_ids:
                        handler.send(
                            500,
                            {"error": "mirror write produced no unit"},
                            request_id=request_id,
                        )
                        return
                    handler.send(
                        200,
                        {"unit_ids": unit_ids},
                        request_id=security.request_id,
                    )
                finally:
                    ledger.close()
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except (ValueError, TypeError) as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync mirror failed")
            handler.send(500, {"error": type(exc).__name__}, request_id=request_id)

    def _local_egress(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                report = self._service.egress_report()
                handler.send(200, {"components": report}, request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except AgentMemoryError as exc:
            handler.send(400, {"error": type(exc).__name__}, request_id=request_id)

    def _local_hook(self, handler) -> None:
        request_id = self._request_id()
        try:
            with self._security(handler, request_id) as security:
                body = self._json_body(handler)
                if body is None:
                    return
                status, payload = self._service.handle_hook(handler.path, body, security)
                handler.send(status, payload, request_id=security.request_id)
        except AuthenticationError:
            handler.send(401, {"error": "AuthenticationError"}, request_id=request_id)
        except ValidationError as exc:
            handler.send(400, {"error": str(exc)}, request_id=request_id)
        except Exception as exc:  # noqa: BLE001 - a hook must never reset the connection
            logger.exception("hook request failed: %s", type(exc).__name__)
            handler.send(
                500,
                {"error": "InternalError", "message": type(exc).__name__},
                request_id=request_id,
            )


def harden_private_paths(home: MemoryHome) -> None:
    """Make the data tree owner-only, including installs created by older versions.

    ``init_home`` writes 0700 directories for new installs, but an upgraded install keeps
    its previous modes. The service is the one place every installation passes through,
    so repair the tree here: the umask keeps future SQLite/WAL files private, and the
    explicit chmods fix what already exists.
    """
    if os.name != "nt":
        os.umask(0o077)
    for directory in (home.root, home.run_dir, home.installs_dir, home.data_dir):
        _restrict(directory, 0o700)
    if not home.data_dir.is_dir():
        return
    for path in home.data_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        _restrict(path, 0o600)


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:  # a missing or read-only path must not stop startup
        logger.warning("cannot restrict %s: %s", path, exc)


class LocalService:
    """One resident service process for a data directory."""

    def __init__(self, home: MemoryHome, *, clock=time.monotonic, port: int = 0) -> None:
        self.home = home
        self.nonce = secrets.token_hex(16)
        self.detached = os.environ.get("JIUWEN_MEMORY_DETACHED", "session")
        self._clock = clock
        self._port = port
        self._server: HttpServer | None = None
        self._settings: dict = {}
        self._leases: dict[str, float] = {}
        self._lock = threading.Lock()
        self._in_flight = 0
        self._idle_since = clock()
        self._reloading = False
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._recovery_stop: threading.Event | None = None
        self._recovery_thread: threading.Thread | None = None
        self._recall_executor: RecallExecutor | None = None
        self._sync_stop: threading.Event | None = None
        self._sync_thread: threading.Thread | None = None
        self._sync_last: dict[str, float] = {}
        self._httpd: object | None = None
        self._turns: TurnStore | None = None

    # -- state helpers --------------------------------------------------------- #

    @property
    def api(self):
        return self._server.api if self._server is not None else None

    @property
    def server(self):
        return self._server

    @property
    def settings(self):
        return self._settings

    def install_policy(self, security):
        return self._install_policy(security)

    def add_space_member(self, org: str, space: str, member: SpaceMember) -> None:
        api = self.api
        manager = getattr(api, "_space", None)
        if manager is None:
            raise ValidationError("space manager is not assembled")
        manager.add_member(org, space, member)
        invalidate = getattr(api, "_invalidate_space_facts", None)
        if callable(invalidate):
            invalidate(org, space)

    def sync_lock_path(self, install_id: str) -> Path:
        return self.home.installs_dir / install_id / "sync.lock"

    def sync_grant_check(self, install, grant):
        from jiuwen_memory_entry.local.install import _load_install
        from jiuwen_memory_entry.local.sync.grant import SyncGrant

        def check(_current=None) -> bool:
            fresh = _load_install(self.home, install.install_id)
            fresh_grant = SyncGrant.from_dict(fresh.sync or {}) if fresh else None
            return bool(
                fresh_grant is not None
                and not fresh_grant.revoked
                and fresh_grant.grant_id == grant.grant_id
                and fresh_grant.version == grant.version
                and fresh_grant.target_space == grant.target_space
            )

        return check

    @property
    def reloading(self) -> bool:
        return self._reloading

    def touch(self) -> None:
        with self._lock:
            self._idle_since = self._clock()

    @contextmanager
    def in_flight(self) -> Iterator[None]:
        """Count one in-flight request for the lifetime of the ``with`` block."""
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)

    def lease(self, client_id: str) -> None:
        with self._lock:
            self._leases[client_id] = self._clock() + LEASE_SECONDS

    def release(self, client_id: str) -> None:
        with self._lock:
            self._leases.pop(client_id, None)

    def _live_leases(self) -> int:
        now = self._clock()
        with self._lock:
            expired = [key for key, expiry in self._leases.items() if expiry <= now]
            for key in expired:
                del self._leases[key]
            return len(self._leases)

    def request_reload(self) -> None:
        self._reload.set()

    def request_stop(self) -> None:
        self._stop.set()

    def egress_report(self) -> list[dict]:
        from jiuwen_memory.api import EgressInventory

        return EgressInventory.from_settings(
            self._settings.get("memory_api") or {}
        ).as_list()

    def _token_db(self) -> str:
        params = (
            (self._settings.get("memory_api") or {})
            .get("authenticator", {})
            .get("default", {})
            .get("params", {})
        )
        return str(params.get("db_path") or (self.home.data_dir / "tokens.db"))

    def _install_policy(self, security) -> InstallPolicy:
        store = LocalTokenStore(self._token_db())
        try:
            install_id = store.install_for_digest(security.auth.credential_id)
        finally:
            store.close()
        if not install_id:
            raise ValidationError("the presented token is not registered to an install")
        record = _read_record(_install_dir(self.home, install_id) / "install.json")
        if not record:
            raise ValidationError("install record is missing")
        org = str((self._settings.get("local") or {}).get("org", "local"))
        project = str(record.get("project") or "")
        return InstallPolicy(
            install_id=install_id,
            connector_id=str(record.get("connector_id", install_id)),
            capture=str(record.get("capture", "task")),
            fixed_project=project or None,
            projects=(project,) if project else (),
            org=org,
            actor=security.auth.actor,
        )

    def turn_store(self) -> TurnStore:
        if self._turns is None:
            self._turns = TurnStore(self.home.data_dir / "local_turns.db")
        return self._turns

    def handle_hook(self, path: str, body: dict, security) -> tuple[int, dict]:
        api = self._server.api if self._server is not None else None
        if api is None:
            raise ValidationError("service is not assembled")
        hook = HookService(
            api,
            self.turn_store(),
            self._install_policy(security),
            executor=self._recall_executor,
        )
        if path.endswith("/retry"):
            return 200, self._capture_payload(
                hook.retry(security, str(body.get("local_op_id", "")))
            )
        host_session_id = str(body.get("host_session_id", "")).strip()
        turn_id = str(body.get("turn_id", "")).strip()
        prompt = body.get("prompt")
        if not host_session_id or not turn_id or not isinstance(prompt, str):
            raise ValidationError("host_session_id, turn_id and prompt are required")
        if path.endswith("/recall"):
            result = hook.recall(
                security,
                RecallRequest(
                    host_session_id=host_session_id,
                    turn_id=turn_id,
                    prompt=prompt,
                    project=body.get("project"),
                    duty=body.get("duty"),
                    top_k=body.get("top_k"),
                ),
            )
            return 200, {
                "status": result.status,
                "view": result.view,
                "candidates": result.candidates,
                "notice": result.notice,
            }
        answer = body.get("answer")
        if not isinstance(answer, str):
            raise ValidationError("answer is required for capture")
        result = hook.capture(
            security,
            CaptureRequest(
                host_session_id=host_session_id,
                turn_id=turn_id,
                prompt=prompt,
                answer=answer,
                project=body.get("project"),
            ),
        )
        return 200, self._capture_payload(result)

    @staticmethod
    def _capture_payload(result) -> dict:
        return {
            "status": result.status,
            "local_op_id": result.local_op_id,
            "refs": result.refs,
            "operation_id": result.operation_id,
            "reason": result.reason,
            "next_actions": result.next_actions,
        }

    # -- assembly and lifecycle ------------------------------------------------ #

    def _assemble(self) -> HttpServer:
        if not self.home.service_config.exists():
            raise ValidationError(
                f"service is not initialized: run `jiuwen-memory init` first ({self.home.root})"
            )
        settings = load_layer(str(self.home.service_config))
        self._settings = settings
        config = HttpServer.resident_config(load_config([settings]))
        from jiuwen_memory.api import assemble_server_runtime

        runtime = assemble_server_runtime(config=config.settings.get("memory_api"))
        try:
            server = _LocalHttpServer(
                config, runtime, runtime.security_runtime, service=self
            )
        except BaseException:
            runtime.close(wait=True)
            raise
        server.portal_enabled = True
        return server

    def _bind(self, server: HttpServer):
        httpd = _BoundedThreadingHTTPServer(
            ("127.0.0.1", self._port), server.handler_cls()
        )
        httpd.daemon_threads = True
        self._port = httpd.server_address[1]
        return httpd

    def _publish(self) -> None:
        discovery.write_discovery(
            self.home,
            discovery.Discovery(
                pid=os.getpid(),
                port=self._port,
                protocol=discovery.PROTOCOL_VERSION,
                instance_nonce=self.nonce,
                home=self.home.canonical,
                started_at=time.time(),
                detached=self.detached,
            ),
        )

    def _resident(self) -> bool:
        globals_ = (self._settings.get("memory_api") or {}).get("globals") or {}
        write_document = globals_.get("write_document")
        watch_document = globals_.get("watch_document")
        if write_document and watch_document is not False:
            return True
        return bool(self._settings.get("dreaming"))

    def _should_exit_idle(self) -> bool:
        if self._resident():
            return False
        if self._live_leases():
            return False
        if self._in_flight:
            return False
        with self._lock:
            idle_for = self._clock() - self._idle_since
        return idle_for >= IDLE_EXIT_SECONDS

    def _serve_cycle(self, httpd) -> None:
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )
        thread.start()
        try:
            while not self._stop.is_set():
                if self._reload.is_set():
                    self._reload.clear()
                    self._reloading = True
                    self._drain()
                    return
                if self._should_exit_idle():
                    logger.info("local service idle for %.0fs, exiting", IDLE_EXIT_SECONDS)
                    self._stop.set()
                    return
                time.sleep(0.1)
        finally:
            self._reloading = True
            httpd.shutdown()
            thread.join(timeout=10.0)

    def _drain(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._in_flight
            if not pending:
                return
            time.sleep(0.02)
        logger.warning("local service drain timed out with %s request(s) in flight", pending)

    def serve(self) -> int:
        """Run until stopped; returns a process exit code."""
        if not self.home.service_config.exists():
            logger.error("service is not initialized: %s", self.home.service_config)
            return 2
        harden_private_paths(self.home)
        self._install_signals()
        exit_code = 0
        try:
            while not self._stop.is_set():
                server = self._assemble()
                self._server = server
                httpd = self._bind(server)
                self._httpd = httpd
                self._publish()
                try:
                    server.start()
                except BaseException:
                    self._close_cycle(httpd, server)
                    raise
                try:
                    server.restore_dreaming()
                except RuntimeError:
                    # A crashed instance's dream KV TTL lease may still hold the lock.
                    logger.warning("dreaming lock is held by a previous instance; retrying")
                    self._close_cycle(httpd, server)
                    return discovery.RETRYABLE_EXIT
                self._reloading = False
                self._recall_executor = RecallExecutor()
                self._start_recovery()
                self._start_sync_scheduler()
                try:
                    self._serve_cycle(httpd)
                finally:
                    self._close_cycle(httpd, server)
        finally:
            discovery.clear_discovery(self.home, nonce=self.nonce)
        return exit_code

    def _start_recovery(self) -> None:
        try:
            from jiuwen_memory_entry.local.recovery import OperationRecovery

            org = str((self._settings.get("local") or {}).get("org", "local"))
            api = self._server.api if self._server is not None else None

            def probe(operation):
                if api is None or not operation.request:
                    return None
                scope = Scope(org=org, space=str(operation.request.get("project", "")))
                return api.continuity_receipt_status(
                    scope, operation.task_id, operation.binding_id, operation.event_id
                )

            self._recovery_stop = threading.Event()
            _recovery, self._recovery_thread = OperationRecovery.start(
                self.home,
                lambda: discovery.live_discovery(self.home),
                self.turn_store(),
                self._recovery_stop,
                receipt_probe=probe,
            )
        except Exception as exc:  # noqa: BLE001 - recovery is best effort
            logger.warning("recovery loop unavailable: %s", type(exc).__name__)

    def _start_sync_scheduler(self) -> None:
        self._sync_stop = threading.Event()

        def run() -> None:
            while not self._sync_stop.wait(5.0):
                found = discovery.live_discovery(self.home)
                if found is None:
                    continue
                for record in self.home.installs_dir.glob("*/install.json"):
                    data = _read_record(record)
                    sync = data.get("sync") if isinstance(data.get("sync"), dict) else {}
                    interval = int(sync.get("interval", 0) or 0)
                    if interval <= 0 or not sync.get("allowed"):
                        continue
                    install_id = str(data.get("install_id", record.parent.name))
                    last = self._sync_last.get(install_id, 0.0)
                    if time.time() - last < interval:
                        continue
                    token_path = self.home.installs_dir / install_id / "token"
                    try:
                        token = token_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        continue
                    self._sync_last[install_id] = time.time()
                    try:
                        client = ServiceClient(found, token)
                        status, preview = client.post("/local/sync", {"dry_run": True})
                        if status == 200 and preview.get("plan_id"):
                            client.post(
                                "/local/sync", {"plan_id": preview["plan_id"]}
                            )
                    except Exception:  # noqa: BLE001 - next interval retries
                        continue

        self._sync_thread = threading.Thread(target=run, daemon=True)
        self._sync_thread.start()

    def _stop_sync_scheduler(self) -> None:
        stop, thread = self._sync_stop, self._sync_thread
        self._sync_stop = None
        self._sync_thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=5.0)

    def stop_recovery(self) -> None:
        self._stop_recovery()

    def _stop_recovery(self) -> None:
        stop, thread = self._recovery_stop, self._recovery_thread
        self._recovery_stop = None
        self._recovery_thread = None
        if stop is None:
            return
        stop.set()
        if thread is not None:
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("recovery thread did not stop within 10s")

    def _close_cycle(self, httpd, server) -> None:
        # Stop the runtime's background workers before its API/TurnStore are closed.
        self._stop_recovery()
        self._stop_sync_scheduler()
        if self._recall_executor is not None:
            self._recall_executor.close()
            self._recall_executor = None
        if self._turns is not None:
            self._turns.close()
            self._turns = None
        try:
            httpd.server_close()
        finally:
            server.close(wait=True)
            self._httpd = None
            self._server = None

    def _install_signals(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):
                continue

    def _on_signal(self, _signum, _frame) -> None:
        self._stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jiuwen Memory local service")
    parser.add_argument("home", nargs="?", help="data directory")
    args = parser.parse_args(argv)
    home = MemoryHome(Path(args.home).expanduser()) if args.home else resolve_home()
    logging.basicConfig(level=logging.INFO)
    return LocalService(home).serve()


if __name__ == "__main__":
    main()
