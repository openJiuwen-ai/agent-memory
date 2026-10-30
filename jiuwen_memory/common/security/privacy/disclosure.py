"""Human-only terminal disclosure; no plaintext enters MemoryAPI or recall."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from jiuwen_memory.common.audit.base import AuditLogger
from jiuwen_memory.common.errors import AuthenticationError, RateLimitedError, ValidationError
from jiuwen_memory.common.security.human_authentication.base import HumanAuthenticator
from jiuwen_memory.common.security.privacy.base import PrivacyProtection
from jiuwen_memory.common.security.protection.rate_limit import RateLimiter
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import AuthContext, Surface
from jiuwen_memory.common.type_def import AuditEvent, LifecycleState, MemoryUnit, Scope


class HumanDisclosure:
    def __init__(
        self,
        privacy: PrivacyProtection,
        human: HumanAuthenticator,
        get: Callable[..., MemoryUnit],
        audit: AuditLogger,
        limiter: RateLimiter,
        guard: WorkloadGuard,
    ) -> None:
        self._privacy, self._human, self._get, self._audit = privacy, human, get, audit
        self._limiter, self._guard = limiter, guard

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
                "scope": payload.get("scope", {}),
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

    def begin(self, payload: dict, browser: str) -> dict[str, Any]:
        binding, scope, uid, revision = self._binding(payload, browser)
        has_target = all((uid, revision, scope.org))
        if not payload.get("invite") and not has_target:
            raise ValidationError("disclosure requires a scope, unit_id and revision")
        try:
            result = self._human.begin(binding, invite=payload.get("invite", ""))
        except Exception:
            self._record("human_authentication", "deny", scope, uid)
            raise
        self._record("human_challenge", "allow", scope, uid)
        return result

    def finish(self, payload: dict, browser: str, *, peer: str = "") -> dict[str, Any]:
        binding, scope, uid, revision = self._binding(payload, browser)
        auth = None
        try:
            self._record("human_verification", "attempt", scope, uid)
            auth, operation = self._human.finish(
                payload["challenge_id"], binding, payload["credential"]
            )
            if not auth.actor.user or auth.actor.agent or operation not in {"view", "enroll"}:
                raise AuthenticationError("human authentication failed")
            if operation == "enroll":
                self._record("human_enrollment", "allow", scope, uid, auth)
                return {"enrolled": True}
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
            content = self._privacy.render(unit)
            return {"unit_id": unit.id, "content": content}
        except Exception:
            self._record("human_disclosure", "deny", scope, uid, auth)
            raise
