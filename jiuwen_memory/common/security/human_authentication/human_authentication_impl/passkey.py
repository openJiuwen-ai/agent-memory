"""Passkey user verification with trusted one-use enrollment invitations.

The primary API key cannot enroll a human or mint disclosure credentials.
Invitations are provisioned out of band as SHA-256 digests bound to fixed USER
actors. Public keys, counters and consumed invite digests are private SQLite
security state. A browser assertion never yields a reusable reveal bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security._sqlite_state import open_private_state
from jiuwen_memory.common.security.human_authentication.base import (
    HumanAuthenticator,
    HumanAuthenticatorProducer,
)
from jiuwen_memory.common.security.types import AuthContext, Role, validate_actor_form
from jiuwen_memory.common.type_def import Scope


class PasskeyAuthenticator(HumanAuthenticator):
    def __init__(self, *, rp_id: str, origin: str, path: str, invites: list[dict], ttl: int = 120):
        try:
            import webauthn
            from webauthn.helpers import structs
        except ImportError as exc:
            raise ValidationError("Passkey requires pip install JiuwenMemory[security]") from exc
        parsed = urlsplit(origin)
        if any((parsed.path, parsed.query, parsed.fragment, parsed.username, parsed.password)):
            raise ValidationError("Passkey requires an exact HTTPS origin matching rp_id")
        if parsed.hostname != rp_id or parsed.scheme not in {"https", "http"}:
            raise ValidationError("Passkey requires an exact HTTPS origin matching rp_id")
        if parsed.scheme == "http" and parsed.hostname != "localhost":
            raise ValidationError("Passkey requires an exact HTTPS origin matching rp_id")
        if not 10 <= ttl <= 300:
            raise ValidationError("Passkey requires an exact HTTPS origin matching rp_id")
        self._origin, self._rp_id, self._ttl = origin, rp_id, ttl
        self._web, self._structs = webauthn, structs
        self._invites: dict[str, Scope] = {}
        for invite in invites:
            digest = invite["digest"]
            actor = Scope(**invite["actor"])
            validate_actor_form(actor)
            if (
                len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise ValidationError("invalid human enrollment invitation")
            if not actor.user or actor.agent or digest in self._invites:
                raise ValidationError("invalid human enrollment invitation")
            self._invites[digest] = actor
        self._lock = threading.RLock()
        self._db = open_private_state(
            path,
            """
            CREATE TABLE IF NOT EXISTS human_credentials(
                id TEXT PRIMARY KEY, public_key BLOB NOT NULL, counter INTEGER NOT NULL,
                actor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS human_invites(digest TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS human_challenges(
                id TEXT PRIMARY KEY, binding TEXT NOT NULL, challenge BLOB NOT NULL,
                expiry REAL NOT NULL, actor TEXT NOT NULL, invite TEXT NOT NULL);
        """,
        )

    @property
    def origin(self) -> str:
        return self._origin

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def health(self) -> None:
        with self._lock:
            self._db.execute("SELECT 1")

    def begin(self, binding: str, *, invite: str = "") -> dict:
        challenge, challenge_id = secrets.token_bytes(32), secrets.token_urlsafe(32)
        digest, actor_json = "", ""
        with self._lock, self._db:
            self._db.execute("DELETE FROM human_challenges WHERE expiry < ?", (time.time(),))
            if self._db.execute("SELECT COUNT(*) FROM human_challenges").fetchone()[0] >= 256:
                raise AuthenticationError("human authentication unavailable")
            if invite:
                digest = hashlib.sha256(invite.encode()).hexdigest()
                actor = self._invites.get(digest)
                if (
                    len(invite) < 32
                    or actor is None
                    or self._db.execute(
                        "SELECT 1 FROM human_invites WHERE digest=?", (digest,)
                    ).fetchone()
                ):
                    raise AuthenticationError("human authentication failed")
                actor_json = json.dumps(asdict(actor), sort_keys=True)
                options = self._web.generate_registration_options(
                    rp_id=self._rp_id,
                    rp_name="Jiuwen Memory",
                    user_name=actor.user,
                    user_id=hashlib.sha256(actor_json.encode()).digest(),
                    challenge=challenge,
                    timeout=self._ttl * 1000,
                    authenticator_selection=self._structs.AuthenticatorSelectionCriteria(
                        user_verification=self._structs.UserVerificationRequirement.REQUIRED,
                        resident_key=self._structs.ResidentKeyRequirement.REQUIRED,
                    ),
                )
            else:
                if not self._db.execute("SELECT 1 FROM human_credentials LIMIT 1").fetchone():
                    raise AuthenticationError("human authentication unavailable")
                options = self._web.generate_authentication_options(
                    rp_id=self._rp_id,
                    challenge=challenge,
                    timeout=self._ttl * 1000,
                    user_verification=self._structs.UserVerificationRequirement.REQUIRED,
                )
            self._db.execute(
                "INSERT INTO human_challenges VALUES(?,?,?,?,?,?)",
                (
                    challenge_id,
                    hashlib.sha256(binding.encode()).hexdigest(),
                    challenge,
                    time.time() + self._ttl,
                    actor_json,
                    digest,
                ),
            )
        return {
            "challenge_id": challenge_id,
            "operation": "enroll" if invite else "view",
            "publicKey": json.loads(self._web.options_to_json(options)),
        }

    def finish(self, challenge_id: str, binding: str, credential: dict) -> tuple[AuthContext, str]:
        # Consume before expensive verification, including failed proofs. A
        # restart or competing worker cannot reuse a committed challenge.
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT binding,challenge,expiry,actor,invite FROM human_challenges WHERE id=?",
                (challenge_id,),
            ).fetchone()
            self._db.execute("DELETE FROM human_challenges WHERE id=?", (challenge_id,))
        if (
            not row
            or row[2] < time.time()
            or not hmac.compare_digest(row[0], hashlib.sha256(binding.encode()).hexdigest())
        ):
            raise AuthenticationError("human authentication failed")
        try:
            if row[4]:
                verified = self._web.verify_registration_response(
                    credential=credential,
                    expected_challenge=row[1],
                    expected_rp_id=self._rp_id,
                    expected_origin=self._origin,
                    require_user_verification=True,
                )
                from webauthn.helpers import bytes_to_base64url

                credential_id = bytes_to_base64url(verified.credential_id)
                with self._lock, self._db:
                    self._db.execute("INSERT INTO human_invites VALUES(?)", (row[4],))
                    self._db.execute(
                        "INSERT INTO human_credentials VALUES(?,?,?,?)",
                        (
                            credential_id,
                            verified.credential_public_key,
                            verified.sign_count,
                            row[3],
                        ),
                    )
                actor, operation = Scope(**json.loads(row[3])), "enroll"
            else:
                credential_id = credential["id"]
                with self._lock, self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    stored = self._db.execute(
                        "SELECT public_key,counter,actor FROM human_credentials WHERE id=?",
                        (credential_id,),
                    ).fetchone()
                    if stored is None:
                        raise ValueError("unknown credential")
                    verified = self._web.verify_authentication_response(
                        credential=credential,
                        expected_challenge=row[1],
                        expected_rp_id=self._rp_id,
                        expected_origin=self._origin,
                        credential_public_key=stored[0],
                        credential_current_sign_count=stored[1],
                        require_user_verification=True,
                    )
                    self._db.execute(
                        "UPDATE human_credentials SET counter=? WHERE id=?",
                        (verified.new_sign_count, credential_id),
                    )
                actor, operation = Scope(**json.loads(stored[2])), "view"
        except Exception as exc:
            raise AuthenticationError("human authentication failed") from exc
        now = datetime.now(UTC)
        return AuthContext(
            actor=actor,
            role=Role.USER,
            credential_type="passkey",
            auth_method="webauthn",
            credential_id=hashlib.sha256(credential_id.encode()).hexdigest(),
            credential_issuer="human_authenticator.default",
            authenticated_at=now,
            expires_at=now + timedelta(seconds=10),
        ), operation


@HumanAuthenticatorProducer.register("passkey")
def _build(config):
    if not config.get("credential_path"):
        raise ValidationError("Passkey requires a dedicated credential_path")
    return PasskeyAuthenticator(
        rp_id=str(config.get("rp_id", "")),
        origin=str(config.get("origin", "")),
        path=str(config.get("credential_path")),
        invites=config.get("enrollment_invites", []),
        ttl=int(config.get("challenge_ttl", 120)),
    )
