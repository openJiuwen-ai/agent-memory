"""Real HTTP transport for human-only display, origin checks and protected API results."""

import http.client
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jiuwen_memory_entry.core.profiles import load_config
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from tests.integration.security.test_memory_privacy import INVITE, SCOPE, TEXT, config, security
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration


@pytest.fixture
def http_view(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    origin = f"http://localhost:{port}"
    settings = config(tmp_path, human=True, origin=origin)
    settings["security"] = {
        "default": {
            "target": "standard",
            "params": {
                "authenticator": {
                    "target": "dev",
                    "params": {
                        "identities": {
                            "test-machine": {"actor": asdict(SCOPE), "role": "user"},
                        }
                    },
                },
                "authorizer": "default",
            },
        }
    }
    server = HttpServer.build(load_config([{"memory_api": settings}]))
    httpd.RequestHandlerClass = server.handler_cls()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(path, payload=None, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            merged = {"Host": f"localhost:{port}", **(headers or {})}
            body = None if payload is None else json.dumps(payload)
            if payload is not None:
                merged.setdefault("Content-Type", "application/json")
                merged.setdefault("Origin", origin)
            connection.request("GET" if payload is None else "POST", path, body, merged)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    yield server, request, origin
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)
    server.close()


def test_browser_only_full_sentence_and_primary_api_stays_masked(http_view):
    server, request, origin = http_view
    unit = server.api.add(TEXT, SCOPE, security=security())[0]
    target = {
        "scope": asdict(SCOPE),
        "unit_id": unit.id,
        "revision": unit.system_metadata["privacy_revision"],
    }
    status, headers, page = request("/security/privacy")
    assert status == 200 and "验证并查看".encode() in page
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
    key = VirtualPasskey(origin=origin)
    status, _, body = request(
        "/security/privacy/begin", {**target, "invite": INVITE}, headers=cookie
    )
    assert status == 200
    start = json.loads(body)
    status, _, body = request(
        "/security/privacy/finish",
        {
            **target,
            "challenge_id": start["challenge_id"],
            "credential": key.response(start, enroll=True),
        },
        headers=cookie,
    )
    assert status == 200 and json.loads(body)["enrolled"]
    status, _, body = request("/security/privacy/begin", target, headers=cookie)
    assert status == 200
    start = json.loads(body)
    payload = {
        **target,
        "challenge_id": start["challenge_id"],
        "credential": key.response(start, counter=2),
    }
    status, headers, body = request("/security/privacy/finish", payload, headers=cookie)
    assert status == 200 and json.loads(body)["content"] == TEXT
    assert headers["Cache-Control"] == "no-store"
    status, _, body = request("/security/privacy/finish", payload, headers=cookie)
    assert status == 403 and b"private-demo-value" not in body
    status, _, body = request(
        "/v1/get",
        {"unit_id": unit.id, "scope": asdict(SCOPE)},
        headers={"Authorization": "Bearer test-machine"},
    )
    assert status == 200 and b"private-demo-value" not in body
    assert b"JW-PRIVATE" in body


@pytest.mark.parametrize("attack", ["origin", "host", "cookie"])
def test_transport_rejects_unbound_browser_requests(http_view, attack):
    _, request, _ = http_view
    headers = {"Cookie": "jiuwen_human_browser=browser"}
    if attack == "origin":
        headers["Origin"] = "https://attacker.example"
    elif attack == "host":
        headers["Host"] = "attacker.example"
    else:
        headers = {}
    status, _, body = request("/security/privacy/begin", {"invite": INVITE}, headers=headers)
    assert status == 403
    assert b"private-demo-value" not in body
