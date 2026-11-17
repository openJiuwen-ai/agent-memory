"""Real HTTP transport for human-only display, origin checks and protected API results."""

import http.client
import json
import threading
from dataclasses import asdict, replace
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from jiuwen_memory_entry.core.profiles import load_config
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from jiuwen_memory_entry.portal.sessions import BROWSER_HEADER, SESSION_HEADER
from tests.integration.security.test_memory_privacy import INVITE, SCOPE, TEXT, config, security
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration
BOB = replace(SCOPE, user="bob")


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
                            "test-bob": {"actor": asdict(BOB), "role": "user"},
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

    def request(path, payload=None, *, headers=None, method=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            merged = {"Host": f"localhost:{port}", **(headers or {})}
            body = None if payload is None else json.dumps(payload)
            method = method or ("GET" if payload is None else "POST")
            if method == "POST":
                merged.setdefault("Content-Type", "application/json")
                merged.setdefault("Origin", origin)
            connection.request(method, path, body, merged)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    yield server, request, origin
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)
    server.close()


def _portal_headers(request, *, credential="test-machine"):
    status, _, body = request(
        "/portal/api/launch", {}, headers={"Authorization": f"Bearer {credential}"}
    )
    assert status == 200
    url = urlsplit(json.loads(body)["portal_url"])
    token = parse_qs(url.fragment)["access"][0]
    status, headers, _ = request("/portal/api/bootstrap", {"token": token, "embedded": False})
    assert status == 200
    return {"Cookie": headers["Set-Cookie"].split(";", 1)[0], BROWSER_HEADER: "1"}


def test_portal_passkey_uses_same_authenticated_session_and_preserves_masked_record(http_view):
    server, request, origin = http_view
    unit = server.api.add(TEXT, SCOPE, security=security())[0]
    target = {
        "scope": asdict(SCOPE), "unit_id": unit.id,
        "revision": unit.system_metadata["privacy_revision"],
    }
    status, _, page = request("/portal/")
    assert status == 200 and b'data-privacy="on"' in page
    status, _, script = request("/security/privacy/passkey.js")
    assert status == 200 and b"export async function verifyPasskey" in script

    alice = _portal_headers(request)
    alice_other_session = _portal_headers(request)
    bob = _portal_headers(request, credential="test-bob")
    assert request("/security/privacy/begin", {"invite": INVITE}, headers=bob)[0] == 403
    assert request("/security/privacy/begin", target, headers={BROWSER_HEADER: "1"})[0] == 403
    assert request("/security/privacy/begin", target, headers={
        **alice, SESSION_HEADER: "embedded",
    })[0] == 403

    key = VirtualPasskey(origin=origin)
    status, _, body = request("/security/privacy/begin", {"invite": INVITE}, headers=alice)
    assert status == 200
    start = json.loads(body)
    assert start["operation"] == "enroll"
    status, _, body = request("/security/privacy/finish", {
        "challenge_id": start["challenge_id"], "credential": key.response(start, enroll=True),
    }, headers=alice)
    assert status == 200 and json.loads(body) == {"enrolled": True}

    status, _, body = request("/security/privacy/begin", target, headers=alice)
    assert status == 200
    start = json.loads(body)
    assert start["operation"] == "view"
    status, _, body = request("/security/privacy/finish", {
        **start["target"], "challenge_id": start["challenge_id"],
        "credential": key.response(start, counter=1),
    }, headers=alice_other_session)
    assert status == 403 and TEXT.encode() not in body

    status, _, body = request("/security/privacy/begin", target, headers=alice)
    assert status == 200
    start = json.loads(body)
    status, headers, body = request("/security/privacy/finish", {
        **start["target"], "challenge_id": start["challenge_id"],
        "credential": key.response(start, counter=2),
    }, headers=alice)
    assert status == 200 and json.loads(body)["content"] == TEXT
    assert headers["Cache-Control"] == "no-store"
    status, _, body = request("/v1/get", {
        "scope": asdict(SCOPE), "unit_id": unit.id,
    }, headers=alice)
    assert status == 200 and b"JW-PRIVATE" in body and b"private-demo-value" not in body


def test_portal_privacy_receives_path_scoped_cookies_without_manual_headers(http_view):
    server, _, origin = http_view
    unit = server.api.add(TEXT, SCOPE, security=security())[0]
    jar = CookieJar()
    browser = build_opener(HTTPCookieProcessor(jar))

    def post(path, payload, *, launch=False):
        headers = {"Content-Type": "application/json", "Origin": origin}
        if launch:
            headers["Authorization"] = "Bearer test-machine"
        else:
            headers[BROWSER_HEADER] = "1"
        req = Request(origin + path, json.dumps(payload).encode(), headers)
        with browser.open(req, timeout=5) as response:
            return json.loads(response.read())

    launch = post("/portal/api/launch", {}, launch=True)
    token = parse_qs(urlsplit(launch["portal_url"]).fragment)["access"][0]
    post("/portal/api/bootstrap", {"token": token, "embedded": False})
    assert {cookie.path for cookie in jar} == {"/v1/", "/security/privacy"}
    assert len({cookie.value for cookie in jar}) == 1
    assert all(cookie.has_nonstandard_attr("HttpOnly") for cookie in jar)
    assert all(cookie.get_nonstandard_attr("SameSite") == "Strict" for cookie in jar)

    key = VirtualPasskey(origin=origin)
    start = post("/security/privacy/begin", {"invite": INVITE})
    assert start["operation"] == "enroll"
    assert post("/security/privacy/finish", {
        "challenge_id": start["challenge_id"], "credential": key.response(start, enroll=True),
    }) == {"enrolled": True}
    target = {"scope": asdict(SCOPE), "unit_id": unit.id}
    start = post("/security/privacy/begin", target)
    assert start["operation"] == "view"
    result = post("/security/privacy/finish", {
        **start["target"], "challenge_id": start["challenge_id"],
        "credential": key.response(start, counter=2),
    })
    assert result["content"] == TEXT
    ordinary = json.dumps(post("/v1/get", target))
    assert "JW-PRIVATE" in ordinary and "private-demo-value" not in ordinary


@pytest.mark.parametrize("invite", [{"code": "invalid"}, 123, [], 0, False, None])
def test_begin_rejects_non_string_invite(http_view, invite):
    server, request, _ = http_view
    unit = server.api.add(TEXT, SCOPE, security=security())[0]
    _, headers, _ = request("/security/privacy")
    status, _, body = request(
        "/security/privacy/begin",
        {"scope": asdict(SCOPE), "unit_id": unit.id, "invite": invite},
        headers={
            "Cookie": headers["Set-Cookie"].split(";", 1)[0],
            "Authorization": "Bearer test-machine",
        },
    )
    assert status == 403
    assert json.loads(body) == {"error": "human verification or resource authorization failed"}


def test_browser_only_full_sentence_and_primary_api_stays_masked(http_view, monkeypatch):
    server, request, origin = http_view
    entries = []
    original_begin = server.api.begin_human_view
    original_finish = server.api.finish_human_view

    def begin_via_api(*args, **kwargs):
        entries.append("begin")
        return original_begin(*args, **kwargs)

    def finish_via_api(*args, **kwargs):
        entries.append("finish")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(server.api, "begin_human_view", begin_via_api)
    monkeypatch.setattr(server.api, "finish_human_view", finish_via_api)
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
    status, _, body = request(
        "/security/privacy/begin",
        target,
        headers={**cookie, "Authorization": "Bearer test-machine"},
    )
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
    assert entries == ["begin", "finish", "begin", "finish", "finish"]


@pytest.mark.parametrize("method", ["begin_human_view", "finish_human_view", "bind_human_view"])
def test_human_entries_are_excluded_from_machine_routes(http_view, method):
    from jiuwen_memory_entry.core.api_contract import api_method_names

    _, request, _ = http_view
    assert method not in api_method_names()
    # Unknown routes are rejected before the server reads the request body.
    status, _, body = request(
        f"/v1/{method}", headers={"Authorization": "Bearer test-machine"}, method="POST"
    )
    assert status == 404
    assert json.loads(body)["error"] == "UnknownVerb"


@pytest.mark.parametrize("attack", ["origin", "host", "cookie"])
def test_transport_rejects_unbound_browser_requests(http_view, attack):
    _, request, _ = http_view
    headers = {"Cookie": "jiuwen_human_browser=browser"}
    if attack == "origin":
        headers["Origin"] = "https://attacker.example"
    elif attack == "host":
        headers["Host"] = "attacker.example"
    else:
        status, _, body = request("/security/privacy/begin", {"invite": INVITE})
        assert status == 403
        assert json.loads(body) == {"error": "human verification or resource authorization failed"}
        return
    # These guards run before the body is read. A bodyless POST isolates the header rejection.
    status, _, body = request("/security/privacy/begin", headers=headers, method="POST")
    assert status == 403
    assert json.loads(body) == {"error": "origin rejected"}


def test_view_begin_requires_primary_auth_and_returns_server_target(http_view):
    server, request, _ = http_view
    unit = server.api.add(TEXT, SCOPE, security=security())[0]
    _, headers, _ = request("/security/privacy")
    cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
    target = {"scope": asdict(SCOPE), "unit_id": unit.id}
    status, _, body = request("/security/privacy/begin", target, headers=cookie)
    assert status == 403 and b"private-demo-value" not in body

    view = server.human_view
    start = view.begin({"invite": INVITE}, "test-browser")
    # The HTTP fixture uses a random localhost port; use its configured origin.
    key = VirtualPasskey(origin=view.origin)
    view.finish(
        {"challenge_id": start["challenge_id"], "credential": key.response(start, enroll=True)},
        "test-browser",
    )
    status, _, body = request(
        "/security/privacy/begin",
        target,
        headers={**cookie, "Authorization": "Bearer test-machine"},
    )
    result = json.loads(body)
    assert status == 200 and result["target"]["unit_id"] == unit.id
    assert result["target"]["revision"] == unit.system_metadata["privacy_revision"]
    assert "private-demo-value" not in body.decode()
