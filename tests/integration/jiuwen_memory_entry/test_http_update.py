# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 HTTP 单条更新冲突回归：POST /v1/update 对非 ACTIVE 记录返回 409（Issue #345）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from queue import Queue
from typing import Any

import pytest

from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server import __main__ as http_server_module
from jiuwen_memory_entry.http_server.__main__ import HttpServer

pytestmark = pytest.mark.integration
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(name="http_url")
def http_url_fixture(monkeypatch):
    ready: Queue[ThreadingHTTPServer] = Queue()

    class _ReadyHttpd(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.05) -> None:
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    monkeypatch.setattr(http_server_module, "ThreadingHTTPServer", _ReadyHttpd)
    config = load_config([OFFLINE])
    server = HttpServer.build(with_local_dev_security(config))
    with ThreadPoolExecutor(max_workers=1) as executor:
        serving = executor.submit(server.serve, "127.0.0.1", 0)
        httpd = None
        try:
            httpd = ready.get(timeout=5)
            yield f"http://127.0.0.1:{httpd.server_port}"
        finally:
            if httpd is not None:
                httpd.shutdown()
            serving.result(timeout=5)


def _post(url: str, method: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http_request = urllib.request.Request(
        f"{url}/v1/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(http_request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_update_superseded_record_returns_409_and_preserves_single_active(http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    status, created = _post(http_url, "add", {"content": "fact A", "scope": scope})
    assert status == 200 and len(created) == 1, created
    unit_a_id = created[0]["id"]

    status, unit_b = _post(
        http_url,
        "update",
        {"unit_id": unit_a_id, "scope": scope, "patch": {"content": "fact B"}},
    )
    assert status == 200 and unit_b["supersedes"] == unit_a_id, unit_b

    status, conflict = _post(
        http_url,
        "update",
        {"unit_id": unit_a_id, "scope": scope, "patch": {"content": "fact C fork"}},
    )
    assert status == 409, conflict
    assert conflict["error"] == "ConflictError", conflict

    status, personal_page = _post(
        http_url,
        "list",
        {"extensions": {"personal": {"lifecycle": "active"}}},
    )
    assert status == 200, personal_page
    assert [item["id"] for item in personal_page["items"]] == [unit_b["id"]], (
        "默认 personal active 列表只应包含 B，不能出现分叉版本 C"
    )
