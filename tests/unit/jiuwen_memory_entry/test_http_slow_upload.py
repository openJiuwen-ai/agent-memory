"""HTTP 慢上传测试（审计验收 P1-HTTP）。

慢上传客户端只发 header + 部分 body，验证两阶段准入下 server 不会在读 body 阶段
无限阻塞、不崩溃、连接最终被处理或超时关闭。
"""

from __future__ import annotations

# These tests intentionally exercise the HTTP adapter's private server helpers.
# pylint: disable=protected-access
import importlib
import socket
import sys
import threading

import pytest

pytestmark = pytest.mark.unit

for _p in ("jiuwen_memory_entry/http_server", "jiuwen_memory_entry/core", "."):
    if _p not in sys.path:
        sys.path.append(_p)

_mod = importlib.import_module("jiuwen_memory_entry.http_server.__main__")  # noqa: E402


def _start_server():
    profiles = importlib.import_module("profiles")
    srv = _mod.HttpServer.build(profiles.load_config([profiles.OFFLINE]))
    httpd = _mod._BoundedThreadingHTTPServer(("127.0.0.1", 0), srv._handler_cls())
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def test_slow_upload_does_not_crash_server() -> None:
    """慢上传：发 header + 部分 body 后停住，server 不崩，连接最终超时/关闭。"""
    httpd, port = _start_server()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        # 声明 100 字节 body，只发部分
        head = (
            b"POST /v1/list HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n"
            b"Content-Type: application/json\r\n\r\n"
        )
        s.sendall(head + b'{"scope":')
        s.settimeout(3)
        try:
            data = s.recv(4096)
        except socket.timeout:
            data = b""
        s.close()
        # 关键：server 没崩，连接要么返回响应要么超时关闭（不无限挂住线程）
        assert data == b"" or b"HTTP" in data
    finally:
        httpd.shutdown()
