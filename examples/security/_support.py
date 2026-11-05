# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""安全示例共用的本机 HTTP 启停与请求辅助；不提供产品管理接口。"""

import json
import logging
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jiuwen_memory_entry.core.config_loader import load_layer
from jiuwen_memory_entry.core.profiles import load_config
from jiuwen_memory_entry.http_server.__main__ import HttpServer

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("security_examples")

EXAMPLE_DIR = Path(__file__).resolve().parent


def config_layer(filename):
    return load_layer(str(EXAMPLE_DIR / filename))


def kernel_config(filename):
    return config_layer(filename)["memory_api"]


@contextmanager
def running(filename=None, *, config=None, port=0, initialize=None):
    service = HttpServer.build(load_config([config_layer(filename) if filename else config]))
    server = None
    worker = None
    try:
        if initialize is not None:
            initialize(service)
        server = ThreadingHTTPServer(("127.0.0.1", port), service.handler_cls())
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        yield service, f"http://127.0.0.1:{server.server_port}"
    finally:
        if worker is not None:
            server.shutdown()
            worker.join()
        if server is not None:
            server.server_close()
        service.close(wait=True)


def post(base, method, key, body, expected=200, *, headers=None):
    supplied = {"Content-Type": "application/json", **(headers or {})}
    if key is not None:
        supplied["Authorization"] = f"Bearer {key}"
    request = Request(f"{base}/v1/{method}", data=json.dumps(body).encode(), headers=supplied)
    try:
        with urlopen(request, timeout=20) as response:
            status, result = response.status, json.load(response)
    except HTTPError as error:
        status, result = error.code, None
        error.close()
    if status != expected:
        raise RuntimeError(f"{method}: expected {expected}, got {status}")
    return result
