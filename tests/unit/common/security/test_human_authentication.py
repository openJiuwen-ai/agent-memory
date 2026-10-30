"""Exercise real WebAuthn registration/assertion signatures and replay defenses."""

import hashlib

import pytest

from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security.human_authentication.human_authentication_impl.passkey import (
    PasskeyAuthenticator,
)
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.unit
INVITE = "trusted-out-of-band-invitation-" + "a" * 32


@pytest.fixture
def human(tmp_path):
    auth = PasskeyAuthenticator(
        rp_id="localhost",
        origin="http://localhost",
        path=str(tmp_path / "human.sqlite"),
        invites=[
            {
                "digest": hashlib.sha256(INVITE.encode()).hexdigest(),
                "actor": {"org": "o", "user": "alice"},
            }
        ],
    )
    yield auth
    auth.close()


def enroll(human):
    key = VirtualPasskey()
    begin = human.begin("browser+enrollment", invite=INVITE)
    auth, operation = human.finish(
        begin["challenge_id"], "browser+enrollment", key.response(begin, enroll=True)
    )
    assert auth.actor.user == "alice" and operation == "enroll"
    return key


def test_verified_human_and_invite_are_not_machine_credentials(human):
    key = enroll(human)
    with pytest.raises(AuthenticationError):
        human.begin("other browser", invite=INVITE)
    challenge = human.begin("browser+scope+unit+revision")
    proof = key.response(challenge, counter=2)
    auth, operation = human.finish(challenge["challenge_id"], "browser+scope+unit+revision", proof)
    assert operation == "view" and auth.auth_method == "webauthn"
    assert auth.actor.user == "alice" and not auth.actor.agent
    with pytest.raises(AuthenticationError):
        human.finish(challenge["challenge_id"], "browser+scope+unit+revision", proof)


@pytest.mark.parametrize("attack", ["uv", "origin", "binding", "signature", "counter"])
def test_invalid_or_retargeted_assertion_is_consumed(human, attack):
    key = enroll(human)
    challenge = human.begin("browser+scope+unit+revision")
    proof = key.response(
        challenge,
        uv=attack != "uv",
        counter=1 if attack == "counter" else 2,
        origin="https://attacker.example" if attack == "origin" else None,
    )
    if attack == "signature":
        proof["response"]["signature"] = "AAAA"
    binding = (
        "different-browser-or-resource" if attack == "binding" else "browser+scope+unit+revision"
    )
    with pytest.raises(AuthenticationError):
        human.finish(challenge["challenge_id"], binding, proof)
    with pytest.raises(AuthenticationError):
        human.finish(
            challenge["challenge_id"], "browser+scope+unit+revision", key.response(challenge)
        )


def test_primary_key_cannot_bootstrap_a_human(human):
    with pytest.raises(AuthenticationError):
        human.begin("browser", invite="ordinary-machine-api-key")


@pytest.mark.parametrize(
    "origin,ttl",
    [
        ("http://server", 120),
        ("ftp://server", 120),
        ("https://other", 120),
        ("https://server/", 120),
        ("https://server?query=value", 120),
        ("https://server#fragment", 120),
        ("https://user@server", 120),
        ("https://user:password@server", 120),
        ("https://server", 9),
        ("https://server", 301),
    ],
)
def test_origin_configuration_fails_closed(tmp_path, origin, ttl):
    with pytest.raises(ValidationError):
        PasskeyAuthenticator(
            rp_id="server",
            origin=origin,
            ttl=ttl,
            path=str(tmp_path / "unused.sqlite"),
            invites=[],
        )


@pytest.mark.parametrize("digest", ["a" * 63, "g" * 64])
def test_invalid_invitation_digest_fails_before_state_creation(tmp_path, digest):
    path = tmp_path / "unused.sqlite"
    with pytest.raises(ValidationError, match="invalid human enrollment invitation"):
        PasskeyAuthenticator(
            rp_id="localhost",
            origin="http://localhost",
            path=str(path),
            invites=[{"digest": digest, "actor": {"org": "o", "user": "alice"}}],
        )
    assert not path.exists()


def test_expired_challenge_is_consumed(human, monkeypatch):
    key = enroll(human)
    start = human.begin("browser+target")
    import time

    now = time.time()
    monkeypatch.setattr(
        "jiuwen_memory.common.security.human_authentication."
        "human_authentication_impl.passkey.time.time",
        lambda: now + 500,
    )
    with pytest.raises(AuthenticationError):
        human.finish(start["challenge_id"], "browser+target", key.response(start, counter=2))


def test_challenge_is_single_use_across_store_connections(human, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    key = enroll(human)
    other = PasskeyAuthenticator(
        rp_id="localhost",
        origin="http://localhost",
        path=str(tmp_path / "human.sqlite"),
        invites=[],
    )
    start = human.begin("browser+target")
    proof = key.response(start, counter=2)

    def verify(auth):
        try:
            auth.finish(start["challenge_id"], "browser+target", proof)
            return True
        except AuthenticationError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(verify, [human, other])) == [False, True]
    finally:
        other.close()
