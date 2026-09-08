# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 HTTP 异步写入：不替换 MemoryAPI、Engine、索引或内存存储实现。

覆盖普通写入及批量写入在多次请求间的可读性。定时中期转长期任务需要常驻
事件循环，当前限制和复现条件记录在 API F05「已知遗留」，不属于成功基线。
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

from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server import __main__ as http_server_module
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

pytestmark = pytest.mark.integration
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(name="async_http_url", params=["in_process", "async_timer"])
def async_http_url_fixture(request, monkeypatch):
    ready: Queue[ThreadingHTTPServer] = Queue()

    class _ReadyHttpd(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.05) -> None:
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    monkeypatch.setattr(http_server_module, "ThreadingHTTPServer", _ReadyHttpd)
    config = load_config(
        [OFFLINE, {"memory_api": {"scheduler": {"default": {"target": request.param}}}}]
    )
    server = HttpServer.build(config, security_runtime=build_dev_security_runtime())
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


def test_real_add_async_remains_readable_across_requests(async_http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    written_ids: set[str] = set()
    for content in ("Alice likes coffee", "Bob likes tea"):
        status, units = _post(async_http_url, "add_async", {"content": content, "scope": scope})

        assert status == 200, units
        assert isinstance(units, list) and units
        for unit in units:
            written_ids.add(unit["id"])
            read_status, stored = _post(
                async_http_url, "get", {"unit_id": unit["id"], "scope": scope}
            )
            assert read_status == 200, stored
            assert stored["id"] == unit["id"]
            assert stored["segments"][0]["content"] == content

    status, page = _post(async_http_url, "list", {"scope": scope})

    assert status == 200, page
    assert page["count"] == len(written_ids)
    assert {unit["id"] for unit in page["items"]} == written_ids


def test_real_batch_add_async_returns_persisted_outcomes(async_http_url) -> None:
    scope = {"org": "local", "user": "developer"}
    written_ids: set[str] = set()
    for batch_number in range(2):
        items = [
            {"content": f"batch {batch_number}: Alice likes coffee"},
            {"content": f"batch {batch_number}: Bob likes tea"},
        ]
        status, result = _post(
            async_http_url, "batch_add_async", {"items": items, "scope": scope}
        )

        assert status == 200, result
        assert len(result["outcomes"]) == len(items)
        for index, outcome in enumerate(result["outcomes"]):
            assert outcome["index"] == index
            assert outcome["error"] == "", outcome
            assert outcome["error_type"] == "", outcome
            assert outcome["units"]
            for unit in outcome["units"]:
                written_ids.add(unit["id"])
                read_status, stored = _post(
                    async_http_url, "get", {"unit_id": unit["id"], "scope": scope}
                )
                assert read_status == 200, stored
                assert stored["segments"][0]["content"] == items[index]["content"]

    status, page = _post(async_http_url, "list", {"scope": scope})

    assert status == 200, page
    assert page["count"] == len(written_ids)
    assert {unit["id"] for unit in page["items"]} == written_ids
