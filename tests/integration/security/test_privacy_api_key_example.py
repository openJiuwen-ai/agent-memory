"""Verify the complete API Key/privacy configuration and the final viewer's permissions."""

import hashlib
import http.client
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jiuwen_memory.api import Credentials, Scope
from jiuwen_memory.common.security.types import Role
from jiuwen_memory_entry.core.config_loader import load_layer
from jiuwen_memory_entry.core.profiles import load_config
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "examples/security/privacy-api-key.yml"


@pytest.fixture
def api_key_privacy(tmp_path, monkeypatch):
    root_key = secrets.token_urlsafe(32)
    invites = {name: secrets.token_urlsafe(32) for name in ("alice", "bob")}
    monkeypatch.setenv("AGENT_MEMORY_ROOT_API_KEY", root_key)
    monkeypatch.setenv("JIUWEN_PRIVACY_MASTER_KEY", secrets.token_hex(32))
    monkeypatch.setenv("JIUWEN_PRIVACY_STATE_DIR", str(tmp_path))
    for name, invite in invites.items():
        monkeypatch.setenv(
            f"JIUWEN_PRIVACY_{name.upper()}_INVITE_SHA256",
            hashlib.sha256(invite.encode()).hexdigest(),
        )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    httpd.daemon_threads = True
    origin = f"http://localhost:{httpd.server_port}"
    service, worker = None, None

    def request(path, payload=None, *, key=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
        try:
            headers = {"Host": f"localhost:{httpd.server_port}"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            if cookie:
                headers["Cookie"] = cookie
            body = None
            if payload is not None:
                headers.update({"Content-Type": "application/json", "Origin": origin})
                body = json.dumps(payload).encode()
            connection.request("GET" if payload is None else "POST", path, body, headers)
            response = connection.getresponse()
            raw = response.read()
            result = (
                json.loads(raw)
                if response.headers.get_content_type() == "application/json"
                else raw
            )
            return response.status, dict(response.getheaders()), result
        finally:
            connection.close()

    try:
        settings = load_layer(str(CONFIG))
        settings["memory_api"]["human_authenticator"]["default"]["params"]["origin"] = origin
        service = HttpServer.build(load_config([settings]))
        admin = service.authenticator.authenticate(Credentials(api_key=root_key))
        assert admin.role is Role.ROOT
        users = {}
        for name in invites:
            scope = {"org": "demo", "user": name}
            users[name] = {
                "scope": scope,
                "key": service.authenticator.key_store.issue(Scope(**scope), Role.USER),
                "passkey": VirtualPasskey(origin=origin),
            }
        httpd.RequestHandlerClass = service.handler_cls()
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        for name, user in users.items():
            status, headers, _ = request("/security/privacy")
            assert status == 200
            user["cookie"] = headers["Set-Cookie"].split(";", 1)[0]
            status, _, start = request(
                "/security/privacy/begin", {"invite": invites[name]}, cookie=user["cookie"]
            )
            assert status == 200
            status, _, result = request(
                "/security/privacy/finish",
                {
                    "challenge_id": start["challenge_id"],
                    "credential": user["passkey"].response(start, enroll=True),
                },
                cookie=user["cookie"],
            )
            assert status == 200 and result["enrolled"]
        yield request, users
    finally:
        if worker is not None:
            httpd.shutdown()
            worker.join(5)
        httpd.server_close()
        if service is not None:
            service.close(wait=True)


def _write(request, user):
    name = user["scope"]["user"]
    text = f"privacy contact {name} 13800138000 api_key={name}-guide-secret"
    status, _, units = request(
        "/v1/add", {"scope": user["scope"], "content": text}, key=user["key"]
    )
    assert status == 200
    return units[0], text


def _begin(request, user, unit):
    status, _, start = request(
        "/security/privacy/begin",
        {
            "scope": unit["scope"],
            "unit_id": unit["id"],
            "revision": unit["system_metadata"]["privacy_revision"],
        },
        key=user["key"],
        cookie=user["cookie"],
    )
    assert status == 200
    return start


def _finish(request, browser_user, signer, start, *, counter=2):
    return request(
        "/security/privacy/finish",
        {
            **start["target"],
            "challenge_id": start["challenge_id"],
            "credential": signer["passkey"].response(start, counter=counter),
        },
        cookie=browser_user["cookie"],
    )


@pytest.mark.parametrize("name", ["alice", "bob"])
def test_user_can_disclose_own_memory_but_get_and_search_stay_redacted(api_key_privacy, name):
    request, users = api_key_privacy
    user = users[name]
    unit, original = _write(request, user)
    start = _begin(request, user, unit)
    status, _, result = _finish(request, user, user, start)
    assert status == 200 and result == {"unit_id": unit["id"], "content": original}
    for method, payload in (
        ("get", {"scope": user["scope"], "unit_id": unit["id"]}),
        ("search", {"query": "privacy", "context": {"scope": user["scope"]}}),
    ):
        status, _, result = request(f"/v1/{method}", payload, key=user["key"])
        assert status == 200
        encoded = json.dumps(result)
        assert "JW-PRIVATE" in encoded
        assert "13800138000" not in encoded and f"{name}-guide-secret" not in encoded


def test_valid_bob_passkey_cannot_disclose_alice_memory(api_key_privacy):
    request, users = api_key_privacy
    alice, bob = users["alice"], users["bob"]
    alice_unit, alice_text = _write(request, alice)
    bob_unit, bob_text = _write(request, bob)
    start = _begin(request, alice, alice_unit)
    status, _, result = _finish(request, alice, bob, start)
    assert status == 403 and alice_text not in json.dumps(result)
    status, _, result = request(
        "/security/privacy/begin", start["target"], key=bob["key"], cookie=bob["cookie"]
    )
    assert status == 403
    status, _, result = request(
        "/v1/get", {"scope": alice["scope"], "unit_id": alice_unit["id"]}, key=bob["key"]
    )
    assert status == 403
    # The same Bob credential succeeds for Bob, proving the earlier denial was resource PEP.
    bob_start = _begin(request, bob, bob_unit)
    status, _, result = _finish(request, bob, bob, bob_start, counter=3)
    assert status == 200 and result["content"] == bob_text
    alice_start = _begin(request, alice, alice_unit)
    status, _, result = _finish(request, alice, alice, alice_start)
    assert status == 200 and result["content"] == alice_text
