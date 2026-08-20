"""HTTP surface — a stdlib server subclassing :class:`~server.Server`.

``HttpServer`` extends the base :class:`Server` (kernel + shared dispatch) and
adds a socket: ``POST /v1/<verb>`` with a JSON body, ``GET /healthz``. The
:class:`HttpClient` in ``bootstrap/cli/client.py`` speaks exactly this protocol,
and one assembled kernel is held for the server's lifetime so state persists
across requests.

通过启动脚本运行，以便把仓库根与 ``bootstrap/core`` 放入 ``PYTHONPATH``::

    scripts/run-server.sh --port 8137
    scripts/run-server.sh [--host H] [--port P] [config.json ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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


logger = logging.getLogger("agent-memory.server")


class HttpServer(Server):
    """The HTTP/socket surface over the shared kernel dispatch."""

    def _handler_cls(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_get(self) -> None:
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
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw) if raw else {}
                except ValueError as exc:
                    self._send(400, {"error": "BadRequest", "message": str(exc)})
                    return
                status, body = srv.dispatch(verb, payload)  # 复用基类 dispatch
                self._send(status, body)

            def log_message(self, *args) -> None:  # quiet by default
                pass

        setattr(Handler, "do_GET", Handler.handle_get)
        setattr(Handler, "do_POST", Handler.handle_post)
        return Handler

    def serve(self, host: str, port: int) -> None:
        httpd = ThreadingHTTPServer((host, port), self._handler_cls())
        logger.info(
            "agent-memory server (profile=%s) on http://%s:%s",
            self.config.profile, host, port,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("agent-memory server stopped")
        finally:
            httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="agent-memory-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("config", nargs="*", help="JSON/YAML config layers stacked on OFFLINE")
    args = parser.parse_args(argv)

    layers = [OFFLINE]
    for path in args.config:
        layers.append(load_layer(path))
    srv = HttpServer.build(load_config(layers))  # 基类 build → HttpServer 实例
    srv.serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
