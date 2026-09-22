# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""标准启动入口 + 多身份 dev + 真实群体记忆组件的端到端回归。"""

import json
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from queue import Queue

import pytest
import yaml

from jiuwen_memory_entry.http_server import __main__ as http_server_module
from tests.integration.jiuwen_memory_entry.fixtures import (
    collective_settings,
    post_as,
    provision_spaces,
)

pytestmark = pytest.mark.integration


@pytest.fixture(name="collective_http_url", params=["json", "yaml"])
def collective_http_url_fixture(request, monkeypatch, tmp_path):
    ready: Queue[ThreadingHTTPServer] = Queue()

    class _ReadyHttpd(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.05) -> None:
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    monkeypatch.setattr(http_server_module, "ThreadingHTTPServer", _ReadyHttpd)
    settings = collective_settings()
    config_path = tmp_path / f"collective.{request.param}"
    serialized = json.dumps(settings) if request.param == "json" else yaml.safe_dump(settings)
    config_path.write_text(serialized, encoding="utf-8")
    argv = ["--auth-mode", "dev", "--host", "127.0.0.1", "--port", "0", str(config_path)]
    with ThreadPoolExecutor(max_workers=1) as executor:
        serving = executor.submit(http_server_module.main, argv)
        httpd = None
        try:
            httpd = ready.get(timeout=5)
            yield f"http://127.0.0.1:{httpd.server_port}"
        finally:
            if httpd is not None:
                httpd.shutdown()
            assert serving.result(timeout=5) == 0


@pytest.mark.parametrize("method", ["add", "add_async", "batch_add", "batch_add_async"])
def test_http_coords_write_preserves_authors_landing_and_user_isolation(
    collective_http_url, method
) -> None:
    provision_spaces(collective_http_url)
    payload = {
        "scope": {"org": "local", "user": "u1", "agent": "a1", "session": "s1"},
        "system_metadata": {"infer": "true", "coords": {"team": "t"}},
    }
    content = "我习惯用 Python 写代码，另外我们团队规定代码评审必须两人"
    if method.startswith("batch_"):
        payload["items"] = [{"content": content}]
    else:
        payload["content"] = content
    status, result = post_as(collective_http_url, "test-u1-agent", method, payload)

    assert status == 200, result
    if method.startswith("batch_"):
        outcome = result["outcomes"][0]
        assert not outcome["error"], outcome
        units = outcome["units"]
    else:
        units = result
    assert len(units) == 2, units
    by_class = {unit["system_metadata"]["memory_class"]: unit for unit in units}
    assert set(by_class) == {"user_pref", "team_convention"}
    for memory_class, space in (("user_pref", "u-u1"), ("team_convention", "team-t")):
        unit = by_class[memory_class]
        assert unit["scope"]["space"] == space
        metadata = unit["system_metadata"]
        assert metadata["author_principal"] == "user:u1"
        assert metadata["author_agent"] == "a1"
        assert {"agent_id", "session_id", "team_id"} <= metadata.keys()
        assert metadata["team_id"] == ("t" if memory_class == "team_convention" else "")
        assert "coords" not in metadata
        read_status, stored = post_as(collective_http_url, "test-u1", "get", {
            "unit_id": unit["id"], "scope": {"org": "local", "space": space},
        })
        assert read_status == 200, stored
        assert stored["system_metadata"] == metadata
    for user, expected in (("u1", {"user_pref", "team_convention"}), ("u2", {"team_convention"})):
        search_status, found = post_as(collective_http_url, f"test-{user}", "search", {
            "query": "Python 评审 约定 习惯", "top_k": 10,
            "context": {"scope": {"org": "local", "user": user}, "extensions": {"spaces": []}},
        })
        assert search_status == 200, found
        assert not found["errors"], found
        assert len(found["items"]) == len(expected), found
        assert {item["system_metadata"]["memory_class"] for item in found["items"]} == expected


def test_http_identity_map_preserves_authorization_denials(collective_http_url) -> None:
    provision_spaces(collective_http_url)
    status, body = post_as(collective_http_url, "test-u1", "create_space", {
        "spec": {"org": "local", "space": "not-authorized"},
    })
    assert status == 403, body
    status, body = post_as(collective_http_url, "test-u2", "get_space", {
        "org": "local", "space": "u-u1",
    })
    assert status == 403, body
    status, body = post_as(collective_http_url, "test-u2", "add", {
        "content": "forged writer", "scope": {"org": "local", "user": "u1"},
        "system_metadata": {"coords": {}},
    })
    assert status == 400, body
    assert "does not match the caller identity" in body["message"]


@pytest.mark.parametrize("token", [None, "unknown"])
def test_http_identity_map_rejects_missing_or_unknown_selector(collective_http_url, token) -> None:
    status, body = post_as(collective_http_url, token, "create_space", {
        "spec": {"org": "local", "space": "not-created"},
    })

    assert status == 401, body
    assert body["error"] == "AuthenticationError"
    status, body = post_as(collective_http_url, "test-ops", "create_space", {
        "spec": {"org": "local", "space": "not-created"},
    })
    assert status == 200, body
    assert body["space"] == "not-created", "rejected requests must not create the space"


def test_concurrent_http_identities_do_not_cross_requests(collective_http_url) -> None:
    provision_spaces(collective_http_url)

    def read_as(user):
        return post_as(collective_http_url, f"test-{user}", "get_space", {
            "org": "local", "space": f"u-{user}",
        })

    users = ["u1", "u2", "u3"] * 8
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(read_as, users))
    for expected_user, (status, body) in zip(users, results):
        assert status == 200, body
        assert body["space"] == f"u-{expected_user}"
