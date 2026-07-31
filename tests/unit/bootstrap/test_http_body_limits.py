"""HTTP surface 的两阶段准入与并发上限（审计验收 P1-HTTP / P2-4）。

`_parse_content_length` 只校验 header 不读 body（第一阶段）；`_read_body` 在
认证通过后按已校验长度读（第三阶段）。中间的 limiter/认证在 body 之前，慢连接
在读 body 前就被挡住。集成层起真实 HTTP server，端到端验证 413/400/503/正常，
并验证慢上传在占满全局连接额度后不能继续创建处理线程。
"""

from __future__ import annotations

import importlib
import io
import json
import os
import socket
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.unit

_BOOT_DIR = "bootstrap/http_server"
_CORE_DIR = os.path.join("bootstrap", "core")
for _p in (_BOOT_DIR, _CORE_DIR, "src"):
    if _p not in sys.path:
        sys.path.append(_p)

_mod = importlib.import_module("bootstrap.http_server.__main__")  # noqa: E402
_parse_content_length = _mod._parse_content_length
_read_body = _mod._read_body
_MAX = _mod._MAX_BODY_BYTES
BoundedServer = _mod._BoundedThreadingHTTPServer


class _Headers:
    def __init__(self, length: str | None):
        self._d = {} if length is None else {"Content-Length": length}

    def get(self, key, default=None):
        return self._d.get(key, default)


def test_parse_length_rejects_negative() -> None:
    assert _parse_content_length(_Headers("-1"))[0] == 400


def test_parse_length_rejects_non_numeric() -> None:
    assert _parse_content_length(_Headers("abc"))[0] == 400


def test_parse_length_rejects_oversized() -> None:
    assert _parse_content_length(_Headers(str(_MAX + 1)))[0] == 413


def test_parse_length_accepts_at_limit() -> None:
    status, length = _parse_content_length(_Headers(str(_MAX)))
    assert status == 200
    assert length == _MAX


def test_parse_length_zero_or_missing() -> None:
    assert _parse_content_length(_Headers("0")) == (200, 0)
    assert _parse_content_length(_Headers(None)) == (200, 0)


def test_read_body_returns_exact_bytes() -> None:
    data = b"payload"
    assert _read_body(io.BytesIO(data), len(data)) == data
    assert _read_body(io.BytesIO(b""), 0) == b""


# -- 集成：真实 HTTP server ------------------------------------------------- #


def _start_server():
    profiles = importlib.import_module("profiles")
    http_mod = importlib.import_module("bootstrap.http_server.__main__")
    srv = http_mod.HttpServer.build(profiles.load_config([profiles.OFFLINE]))
    httpd = http_mod._BoundedThreadingHTTPServer(("127.0.0.1", 0), srv._handler_cls())
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _post(port: int, body: bytes, content_length: str | None = None) -> tuple[int, dict]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        headers = "POST /v1/list HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        if content_length is not None:
            headers += f"Content-Length: {content_length}\r\n"
        else:
            headers += f"Content-Length: {len(body)}\r\n"
        headers += "Content-Type: application/json\r\n\r\n"
        s.sendall(headers.encode() + body)
        data = s.recv(65536)
    finally:
        s.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    code = int(status_line.split()[1])
    try:
        return code, json.loads(payload) if payload else {}
    except ValueError:
        return code, {"raw": payload}


def test_http_rejects_oversized_body() -> None:
    httpd, port = _start_server()
    try:
        code, _ = _post(port, b"x" * 10, content_length=str(_MAX + 1))
        assert code == 413
    finally:
        httpd.shutdown()


def test_http_rejects_invalid_length() -> None:
    httpd, port = _start_server()
    try:
        code, _ = _post(port, b"", content_length="-1")
        assert code == 400
    finally:
        httpd.shutdown()


def test_http_accepts_normal_request() -> None:
    httpd, port = _start_server()
    try:
        code, _ = _post(port, json.dumps({"scope": {"org": "acme", "user": "alice"}}).encode())
        assert code == 200
    finally:
        httpd.shutdown()


def test_http_concurrency_limit_rejects_excess() -> None:
    """审计验收 P1-HTTP：占满全局连接额度后，多余连接快速被拒（503）。

    用一个故意阻塞的 handler 钉住连接槽，开满 _MAX_CONCURRENT_REQUESTS + N 个，
    断言多余的被 503 拒而非无限创建线程。
    """
    profiles = importlib.import_module("profiles")
    http_mod = importlib.import_module("bootstrap.http_server.__main__")
    srv = http_mod.HttpServer.build(profiles.load_config([profiles.OFFLINE]))
    handler = srv._handler_cls()

    # 用小额度 server 避免开几百连接
    class _TinyServer(http_mod._BoundedThreadingHTTPServer):
        def __init__(self, *a, **kw):
            import threading

            super().__init__(*a, **kw)
            self._slots = threading.BoundedSemaphore(2)

    httpd = _TinyServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    gate = threading.Event()

    # 用阻塞的 do_POST 占住槽
    class _Block(handler):
        def do_POST(self):
            gate.wait(timeout=3)
            self.send_response(200)
            self.end_headers()

    # 替换 handler 为阻塞版
    httpd.RequestHandlerClass = _Block
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    results = []

    def fire():
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.sendall(b"POST /v1/x HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
            data = s.recv(256)
            results.append(int(data.split()[1]))
            s.close()
        except OSError:
            results.append(-1)

    # 开 5 个连接，额度 2，预期 2 个 200、3 个 503（或被拒）
    threads = [threading.Thread(target=fire) for _ in range(5)]
    for th in threads:
        th.start()
        time.sleep(0.05)  # 错开让前两个先进
    time.sleep(0.5)
    gate.set()  # 放行阻塞的
    for th in threads:
        th.join(timeout=3)

    httpd.shutdown()
    accepted = results.count(200)
    rejected = sum(1 for r in results if r in (503, -1))
    # 最多 2 个被处理，其余被拒
    assert accepted <= 2
    assert accepted + rejected == 5
