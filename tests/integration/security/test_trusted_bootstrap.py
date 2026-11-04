# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trusted 公开初始化 → 实际 HTTP 认证和业务授权，零外部服务。"""

import json
import secrets
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from jiuwen_memory.api import Scope, register_trusted_principal

pytestmark = pytest.mark.integration


def test_pure_trusted_service_bootstraps_and_enforces_http_permissions():
    # 入口导入仅在执行集成测试时发生，不能影响无 PYTHONPATH 的全量收集。
    from jiuwen_memory_entry.core.profiles import load_config
    from jiuwen_memory_entry.http_server.__main__ import HttpServer

    key = secrets.token_urlsafe(32)
    service = HttpServer.build(
        load_config(
            [
                {
                    "memory_api": {
                        "security": {
                            "default": {
                                "target": "standard",
                                "params": {
                                    "authenticator": "gateway",
                                    "authorizer": "default",
                                },
                            }
                        },
                        "authenticator": {
                            "gateway": {
                                "target": "trusted",
                                "params": {
                                    "key_store": "principals",
                                    "gateway_key": key,
                                },
                            }
                        },
                        "key_store": {"principals": {"target": "memory"}},
                        "authorizer": {
                            "default": {
                                "target": "standard",
                                "params": {
                                    "grant_store": "default",
                                    "delegation_store": "default",
                                },
                            }
                        },
                        "grant_store": {"default": {"target": "memory"}},
                        "delegation_store": {"default": {"target": "memory"}},
                    }
                }
            ]
        )
    )
    server = None
    worker = None
    try:
        register_trusted_principal(service.authenticator, Scope(org="demo", user="alice"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.handler_cls())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(method, body, *, principal="alice", supplied_key=key):
            headers = {
                "Content-Type": "application/json",
                "X-Org-Id": "demo",
                "X-Principal-Type": "user",
                "X-Principal-Id": principal,
                "X-Role": "root",
            }
            if supplied_key is not None:
                headers["Authorization"] = f"Bearer {supplied_key}"
            request = Request(
                f"{base}/v1/{method}", data=json.dumps(body).encode(), headers=headers
            )
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                error.close()
                return error.code, None

        own = {"content": "Trusted HTTP test", "scope": {"org": "demo", "user": "alice"}}
        status, units = post("add", own)
        assert status == 200
        status, unit = post("get", {"unit_id": units[0]["id"], "scope": own["scope"]})
        assert status == 200 and unit["segments"][0]["content"] == "Trusted HTTP test"
        assert post("add", own, principal="unknown")[0] == 401
        assert post("add", own, supplied_key="wrong")[0] == 401
        assert post("add", own, supplied_key=None)[0] == 401
        assert (
            post("add", {"content": "forbidden", "scope": {"org": "demo", "user": "bob"}})[0] == 403
        )
        assert (
            post("register_trusted_principal", {"actor": {"org": "demo", "user": "bob"}})[0] == 404
        )
    finally:
        if worker is not None:
            server.shutdown()
            worker.join()
        if server is not None:
            server.server_close()
        service.close(wait=True)
