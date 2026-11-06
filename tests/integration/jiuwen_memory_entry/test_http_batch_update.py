# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 HTTP 批量更新：POST /v1/batch_update 端到端验证。

覆盖统一 patch 模式、逐条 patch 模式、错误隔离与 list 可读性。
"""

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


def _add(url: str, scope: dict, content: str, tags: list[str] | None = None) -> str:
    payload: dict[str, Any] = {"content": content, "scope": scope}
    if tags:
        payload["tags"] = tags
    status, units = _post(url, "add", payload)
    assert status == 200, units
    assert isinstance(units, list) and units
    return units[0]["id"]


def test_http_batch_update_unified_patch_tags(http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    ids = [_add(http_url, scope, f"content-{i}") for i in range(3)]

    status, result = _post(http_url, "batch_update", {
        "selector": {"unit_ids": ids, "scope": scope},
        "patches": [{"tags": ["batch-updated"]}],
    })

    assert status == 200, result
    assert len(result["outcomes"]) == 3
    for outcome in result["outcomes"]:
        assert outcome["error"] == ""
        assert outcome["updated_unit"] is not None
        assert "batch-updated" in outcome["updated_unit"]["tags"]

    for original_id in ids:
        _, stored = _post(http_url, "get", {"unit_id": original_id, "scope": scope})
        assert stored["lifecycle"] == "superseded"

    _, page = _post(http_url, "list", {"scope": scope})
    assert page["count"] >= 3
    active_tags = {
        tag for unit in page["items"] for tag in unit.get("tags", [])
    }
    assert "batch-updated" in active_tags


def test_http_batch_update_per_item_content(http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    id_a = _add(http_url, scope, "original-A")
    id_b = _add(http_url, scope, "original-B")

    status, result = _post(http_url, "batch_update", {
        "selector": {"unit_ids": [id_a, id_b], "scope": scope},
        "patches": [
            {"content": "patched-A"},
            {"content": "patched-B"},
        ],
    })

    assert status == 200, result
    assert len(result["outcomes"]) == 2
    contents = {
        outcome["updated_unit"]["segments"][0]["content"]
        for outcome in result["outcomes"]
        if outcome["updated_unit"]
    }
    assert contents == {"patched-A", "patched-B"}


def test_http_batch_update_continue_on_error(http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    id_ok = _add(http_url, scope, "will-succeed")
    id_bad = _add(http_url, scope, "will-fail")

    _post(http_url, "delete", {
        "selector": {"unit_ids": [id_bad], "scope": scope, "mode": "forget"},
    })

    status, result = _post(http_url, "batch_update", {
        "selector": {"unit_ids": [id_ok, id_bad], "scope": scope},
        "patches": [{"tags": ["retagged"]}],
        "continue_on_error": True,
    })

    assert status == 200, result
    assert len(result["outcomes"]) == 2
    errors = [outcome for outcome in result["outcomes"] if outcome["error"]]
    successes = [outcome for outcome in result["outcomes"] if not outcome["error"]]
    assert len(errors) == 1
    assert errors[0]["unit_id"] == id_bad
    assert len(successes) == 1
    assert successes[0]["unit_id"] == id_ok


def test_http_batch_update_validation_error_count_mismatch(http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    id1 = _add(http_url, scope, "unit-1")
    id2 = _add(http_url, scope, "unit-2")
    id3 = _add(http_url, scope, "unit-3")

    status, result = _post(http_url, "batch_update", {
        "selector": {"unit_ids": [id1, id2, id3], "scope": scope},
        "patches": [{"tags": ["a"]}, {"tags": ["b"]}],
    })

    assert status == 400
    assert "must equal" in result.get("error", "") or "must equal" in str(result)


def test_http_batch_update_empty_selector_rejected(http_url) -> None:
    scope = {"org": "local", "user": "developer"}

    status, result = _post(http_url, "batch_update", {
        "selector": {"scope": scope},
        "patches": [{"tags": ["x"]}],
    })

    assert status == 400
