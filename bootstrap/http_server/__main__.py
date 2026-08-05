"""HTTP surface — a stdlib server subclassing :class:`~server.Server`.

``HttpServer`` extends the base :class:`Server` (kernel + shared dispatch) and
adds a socket: ``POST /v1/<verb>`` with a JSON body, ``GET /healthz``. The
:class:`HttpClient` in ``bootstrap/cli/client.py`` speaks exactly this protocol,
and one assembled kernel is held for the server's lifetime so state persists
across requests.

通过启动脚本运行，以便把 ``src`` 与 ``bootstrap/core`` 放入 ``PYTHONPATH``::

    scripts/run-server.sh --port 8137
    scripts/run-server.sh [--host H] [--port P] [config.json ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module

# 共享应用核（server / profiles / handler / config_loader）住在 bootstrap/core；
# 加入 sys.path 后 flat-import 复用——本 surface 只做 HTTP 传输。
_CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

load_layer = import_module("config_loader").load_layer
_profiles_module = import_module("profiles")
OFFLINE = _profiles_module.OFFLINE
load_config = _profiles_module.load_config
Server = import_module("server").Server

_auth_middleware = import_module("auth_middleware")
authenticated = _auth_middleware.authenticated
credentials_from_headers = _auth_middleware.credentials_from_headers

# ``server`` / ``auth_middleware`` 导入时已把仓库 src/ 追加进 sys.path。
_errors = import_module("common.errors")
AuthenticationError = _errors.AuthenticationError
RateLimitedError = _errors.RateLimitedError
ValidationError = _errors.ValidationError
check_dev_binding = import_module("common.authentication.binding").check_dev_binding

# 请求体大小硬上限（审计 P2-4）：无上限意味着超大或慢速上传能吃满内存与线程。
# 4 MiB 覆盖任何合理的记忆写入请求；超大资产本就该走 FS + 分片而非塞进单次 POST。
_MAX_BODY_BYTES = 4 * 1024 * 1024
# 读/写超时（秒）：慢速上传与慢客户端会长期占住 ThreadingHTTPServer 的线程。
_READ_TIMEOUT = 30
# 并发连接/线程硬上限（审计验收 P1-HTTP）：timeout 只限单连接占用时长，攻击者持续
# 补充连接即可维持线程耗尽。有界 semaphore 让超出上限的连接快速被拒（503），在
# limiter/认证之前生效--未认证来源不能靠慢上传占满处理容量。
_MAX_CONCURRENT_REQUESTS = 256


def _parse_content_length(headers) -> tuple[int, int]:
    """只校验 Content-Length，不读 body。返回 (status, length)。

    status != 200 时 length 无意义。两阶段准入的第一阶段（审计验收 P1-HTTP）：
    只依赖 header，在 limiter/认证之前，通过后才由调用方按 length 读 body。
    """
    raw_len = headers.get("Content-Length", "0")
    try:
        length = int(raw_len)
    except ValueError:
        return 400, 0
    if length < 0:
        return 400, 0
    if length > _MAX_BODY_BYTES:
        return 413, 0
    return 200, length


def _read_body(rfile, length: int) -> bytes:
    """按已校验的 length 读 body。length 已由 _parse_content_length 约束。"""
    return rfile.read(length) if length else b""


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """有界并发的 ThreadingHTTPServer（审计验收 P1-HTTP）。

    process_request 入口用 semaphore 限并发：耗尽时直接拒绝（503），不进 handle
    路径、不占处理线程的认证/读 body 预算。把慢连接攻击的容量从无界线程收束到
    ``_MAX_CONCURRENT_REQUESTS``。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                self._send_503(request)
            except OSError:
                pass  # 客户端已断开
            self.shutdown_request(request)
            return
        # 不在这里 release：ThreadingHTTPServer.process_request 会 spawn 线程后立即
        # 返回，若在这里 release 等于没限。release 下移到 process_request_thread
        # （处理线程真正结束时）。
        t = threading.Thread(target=self._process_and_release, args=(request, client_address))
        t.daemon = self.daemon_threads
        t.start()

    def _process_and_release(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._slots.release()

    @staticmethod
    def _send_503(request) -> None:
        body = b'{"error":"ServiceUnavailable","message":"too many connections"}'
        crlf = bytes([13, 10])
        request.sendall(
            b"HTTP/1.0 503 Service Unavailable"
            + crlf
            + b"Content-Length: "
            + str(len(body)).encode()
            + crlf
            + b"Content-Type: application/json"
            + crlf
            + crlf
            + body
        )


class HttpServer(Server):
    """The HTTP/socket surface over the shared kernel dispatch."""

    def _handler_cls(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            # 慢速上传/慢客户端的读写超时（审计 P2-4）：无超时会让一个慢连接
            # 长期占住 ThreadingHTTPServer 的线程。
            timeout = _READ_TIMEOUT

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_get(self) -> None:
                # /healthz 不认证（§2.1 原则 2 的明文例外）。响应体只含 status +
                # profile 名——profile 名是部署配置的一部分但不是秘密，且改它会破坏
                # 现有客户端的 healthz() 契约，第一期保持原样。
                if self.path.rstrip("/") == "/healthz":
                    self._send(200, {"status": "ok", "profile": srv.config.profile})
                else:
                    self._send(404, {"error": "NotFound", "message": self.path})

            def handle_post(self) -> None:
                if not self.path.startswith("/v1/"):
                    self._send(404, {"error": "NotFound", "message": self.path})
                    return
                prefix_len = len("/v1/")
                verb = self.path[prefix_len:].strip("/")
                # 两阶段准入（审计验收 P1-HTTP）：
                # 1) 只校验 Content-Length，不读 body；
                # 2) 提凭据 + limiter/认证（慢连接在读 body 前就被挡住）；
                # 3) 通过后才按已校验长度读 body。
                status, length = _parse_content_length(self.headers)
                if status == 413:
                    self._send(
                        413,
                        {
                            "error": "PayloadTooLarge",
                            "message": f"body exceeds {_MAX_BODY_BYTES}B limit",
                        },
                    )
                    return
                if status == 400:
                    self._send(400, {"error": "BadRequest", "message": "invalid Content-Length"})
                    return
                creds = credentials_from_headers(self.headers, self.client_address[0])
                try:
                    # 认证在读 body 之前：未认证/被限流的请求不占读 body 的内存预算。
                    # 中间件负责退出时 reset ContextVar。
                    with authenticated(
                        srv.authenticator,
                        creds,
                        srv.audit,
                        srv.rate_limiter,
                        argon2_guard=srv.argon2_guard,
                    ):
                        raw = _read_body(self.rfile, length)
                        try:
                            payload = json.loads(raw) if raw else {}
                        except ValueError as exc:
                            self._send(400, {"error": "BadRequest", "message": str(exc)})
                            return
                        status, body = srv.dispatch(verb, payload)
                except AuthenticationError as exc:
                    status, body = 401, {"error": type(exc).__name__, "message": str(exc)}
                except RateLimitedError as exc:
                    # 429 而非 401：限流发生在认证之前，此时还不知道凭据对不对。
                    status, body = 429, {"error": type(exc).__name__, "message": str(exc)}
                self._send(status, body)

            def log_message(self, *args) -> None:  # quiet by default
                pass

        setattr(Handler, "do_GET", Handler.handle_get)
        setattr(Handler, "do_POST", Handler.handle_post)
        return Handler

    def serve(self, host: str, port: int) -> None:
        # 绑定 guard 必须位于公开 serve() 内，而不是只放 CLI main()：嵌入式调用方
        # 直接调用 serve() 也不能把 DEV/未知认证实现暴露到非 loopback 网络。
        if self.authenticator is None or self.authenticator.requires_loopback_binding():
            check_dev_binding(host)
        httpd = _BoundedThreadingHTTPServer((host, port), self._handler_cls())
        # daemon_threads：serve_forever 退出时（KeyboardInterrupt）不等待慢请求线程，
        # 否则一个挂住的连接能让进程退不掉（审计 P2-4）。并发上限由
        # _BoundedThreadingHTTPServer 的 semaphore 管（审计验收 P1-HTTP）。
        httpd.daemon_threads = True
        sys.stderr.write(
            f"agent-memory server (profile={self.config.profile}) on http://{host}:{port}\n"
        )
        sys.stderr.flush()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("\nagent-memory server stopped\n")
        finally:
            httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-memory-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("config", nargs="*", help="JSON/YAML config layers stacked on OFFLINE")
    args = parser.parse_args(argv)

    layers = [OFFLINE]
    for path in args.config:
        layers.append(load_layer(path))
    srv = HttpServer.build(load_config(layers))  # 基类 build → HttpServer 实例

    try:
        srv.serve(args.host, args.port)
    except ValidationError as exc:
        logging.error("FATAL: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
