"""Join a protected snapshot and fresh human proof before the engine's terminal response."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from jiuwen_memory.common.audit.base import AuditLogger
from jiuwen_memory.common.errors import AuthenticationError, RateLimitedError, ValidationError
from jiuwen_memory.common.security.human_authentication.base import HumanAuthenticator
from jiuwen_memory.common.security.privacy.base import HumanViewContent, PrivacyProtection
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import AuthContext, RequestSecurityContext, Surface
from jiuwen_memory.common.type_def import AuditEvent, LifecycleState, MemoryUnit, Scope


@dataclass(frozen=True)
class _PendingView:
    binding: str
    proof_binding: str
    operation: str
    expiry: float


class HumanDisclosure:
    def __init__(
        self,
        privacy: PrivacyProtection,
        human: HumanAuthenticator,
        get: Callable[..., MemoryUnit],
        audit: AuditLogger,
        limiter: RateLimiter,
        guard: WorkloadGuard,
        render: Callable[[str, MemoryUnit], Awaitable[HumanViewContent]],
    ) -> None:
        self._privacy, self._human, self._get, self._audit = privacy, human, get, audit
        self._limiter, self._guard = limiter, guard
        self._render = render
        self._pending: dict[str, _PendingView] = {}
        self._lock = threading.RLock()

    @contextmanager
    def admission(self, peer: str):
        if not self._limiter.allow(peer) or not self._guard.acquire():
            raise RateLimitedError("human authentication budget exhausted")
        try:
            yield
        finally:
            self._guard.release()

    @property
    def origin(self) -> str:
        return self._human.origin

    def security_dependencies(self) -> tuple:
        return self._privacy, self._human, self._audit, self._limiter, self._guard

    @staticmethod
    def _binding(payload: dict, browser: str) -> tuple[str, Scope, str, str]:
        if not browser or len(browser) > 128:
            raise AuthenticationError("human authentication failed")
        scope = Scope(**payload.get("scope", {}))
        uid, revision = payload.get("unit_id", ""), payload.get("revision", "")
        if not isinstance(uid, str) or not isinstance(revision, str):
            raise ValidationError("invalid disclosure target")
        if len(uid) > 256 or len(revision) > 64:
            raise ValidationError("invalid disclosure target")
        binding = json.dumps(
            {
                "browser": browser,
                "scope": asdict(scope),
                "unit_id": uid,
                "revision": revision,
            },
            sort_keys=True,
        )
        return binding, scope, uid, revision

    def _record(
        self, action: str, decision: str, scope: Scope, uid: str, auth: AuthContext | None = None
    ) -> None:
        self._audit.record(
            AuditEvent(
                actor=auth.actor if auth else Scope(),
                target=scope,
                action=action,
                target_id=uid,
                decision=decision,
                layer="security",
                occurred_at=datetime.now(UTC),
                detail={"auth_method": "webauthn"},
            )
        )

    def begin(
        self, payload: dict, browser: str, *, security: RequestSecurityContext | None = None
    ) -> dict[str, Any]:
        binding, scope, uid, revision = self._binding(payload, browser)
        operation = ""
        challenge_id = ""
        try:
            if payload.get("invite"):
                result = self._human.begin(binding, invite=payload["invite"])
            else:
                if security is None:
                    raise AuthenticationError("primary authentication required")
                if not uid or not scope.org:
                    raise ValidationError("disclosure requires a scope and unit_id")
                unit = self._get(uid, scope, security=security)
                actual_revision = unit.system_metadata.get("privacy_revision", "")
                if revision and revision != actual_revision:
                    raise ValidationError("memory changed; request a current snapshot")
                target = {
                    "scope": asdict(unit.scope),
                    "unit_id": unit.id,
                    "revision": actual_revision,
                }
                binding, scope, uid, revision = self._binding(target, browser)
                # The requester is server-derived; the final viewer is independently
                # established by Passkey and must pass the normal resource PEP again.
                proof_binding = binding + json.dumps(
                    {
                        "requester": asdict(security.auth.actor),
                        "credential_id": security.auth.credential_id,
                    },
                    sort_keys=True,
                )
                with self._lock:
                    self._expire()
                    if len(self._pending) >= 256:
                        raise RateLimitedError("human snapshot capacity exhausted")
                    operation = self._privacy.stage_disclosure(unit)
                    result = self._human.begin(proof_binding)
                    challenge_id = result["challenge_id"]
                    timeout = min(300, max(1, result["publicKey"]["timeout"] / 1000))
                    self._pending[result["challenge_id"]] = _PendingView(
                        binding, proof_binding, operation, time.monotonic() + timeout
                    )
                result["target"] = target
            self._record(
                "human_challenge", "allow", scope, uid, security.auth if security else None
            )
        except Exception:
            if operation:
                with self._lock:
                    self._pending.pop(challenge_id, None)
                self._privacy.cancel_disclosure(operation)
            self._record("human_authentication", "deny", scope, uid)
            raise
        return result

    def _expire(self) -> None:
        for key, pending in list(self._pending.items()):
            if pending.expiry <= time.monotonic():
                self._pending.pop(key)
                self._privacy.cancel_disclosure(pending.operation)

    def finish(self, payload: dict, browser: str, *, peer: str = "") -> dict[str, Any]:
        # Pop even malformed/failed attempts; neither proof nor snapshot is reusable.
        with self._lock:
            self._expire()
            pending = self._pending.pop(payload.get("challenge_id", ""), None)
        auth = None
        scope, uid = Scope(), ""
        try:
            binding, scope, uid, revision = self._binding(payload, browser)
            self._record("human_verification", "attempt", scope, uid)
            proof_binding = pending.proof_binding if pending else binding
            if pending and pending.binding != binding:
                raise AuthenticationError("human authentication failed")
            auth, operation = self._human.finish(
                payload["challenge_id"], proof_binding, payload["credential"]
            )
            if not auth.actor.user or auth.actor.agent or operation not in {"view", "enroll"}:
                raise AuthenticationError("human authentication failed")
            if operation == "enroll":
                if pending:
                    raise AuthenticationError("human authentication failed")
                self._record("human_enrollment", "allow", scope, uid, auth)
                return {"enrolled": True}
            if pending is None:
                raise AuthenticationError("human snapshot expired or unavailable")
            if pending.expiry <= time.monotonic():
                raise AuthenticationError("human snapshot expired or unavailable")
            security = new_request_context(auth, surface=Surface.HTTP, peer=peer)
            # The regular PEP rechecks authoritative Scope, current grants,
            # space membership and the actual snapshot. No second PDP here.
            unit = self._get(uid, scope, security=security)
            if unit.lifecycle is not LifecycleState.ACTIVE:
                raise ValidationError("memory is not active; request a current snapshot")
            if unit.system_metadata.get("privacy_revision") != revision:
                raise ValidationError("memory changed; request a new human verification")
            # Audit failure must prevent release of plaintext.
            self._record("human_disclosure", "allow", unit.scope, unit.id, auth)
            result = asyncio.run(self._render(pending.operation, unit))
            return {"unit_id": result.unit_id, "content": result.content}
        except Exception:
            self._record("human_disclosure", "deny", scope, uid, auth)
            raise
        finally:
            if pending:
                self._privacy.cancel_disclosure(pending.operation)
