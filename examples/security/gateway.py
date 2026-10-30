# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""加载纯 Trusted 配置，在监听前通过公开 API 登记主体的本机测试网关。"""

import argparse
import hmac
import os
import secrets
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from _support import post, running

from jiuwen_memory.api import Scope, register_trusted_principal


@contextmanager
def gateway(*, client_key, backend_port=0, gateway_port=0):
    gateway_key = secrets.token_urlsafe(32)
    os.environ["AGENT_MEMORY_GATEWAY_KEY"] = gateway_key

    def initialize(service):
        register_trusted_principal(
            service.authenticator,
            Scope(org="demo", user="alice"),
            role="user",
        )

    with running("trusted.yml", port=backend_port, initialize=initialize) as (_, backend):
        backend_address = urlparse(backend)

        class Gateway(BaseHTTPRequestHandler):
            timeout = 10

            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != "/v1/add":
                    self.send_error(404)
                    return
                supplied = self.headers.get("Authorization", "").encode()
                if not hmac.compare_digest(supplied, f"Bearer {client_key}".encode()):
                    self.send_error(401)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if not 0 < size <= 65536:
                    self.send_error(413)
                    return
                body = self.rfile.read(size)
                headers = {
                    "Authorization": f"Bearer {gateway_key}",
                    "X-Org-Id": "demo",
                    "X-Principal-Type": "user",
                    "X-Principal-Id": "alice",
                    "Content-Type": "application/json",
                }
                connection = HTTPConnection(
                    backend_address.hostname, backend_address.port, timeout=10
                )
                try:
                    connection.request("POST", "/v1/add", body=body, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    self.send_response(response.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    connection.close()

        server = ThreadingHTTPServer(("127.0.0.1", gateway_port), Gateway)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", backend
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    client_key = secrets.token_urlsafe(32) if args.self_test else os.environ["DEMO_CLIENT_KEY"]
    if not client_key:
        raise ValueError("DEMO_CLIENT_KEY must not be empty")
    ports = {} if args.self_test else {"backend_port": 8137, "gateway_port": 8139}
    with gateway(client_key=client_key, **ports) as (base, backend):
        if args.self_test:
            body = {"content": "gateway test", "scope": {"org": "demo", "user": "alice"}}
            post(base, "add", client_key, body)
            post(base, "add", None, body, 401)
            units = post(
                base,
                "add",
                client_key,
                body,
                headers={
                    "X-Principal-Id": "bob",
                    "X-Role": "root",
                },
            )
            assert units[0]["system_metadata"]["author_principal"] == "user:alice"
            post(
                backend,
                "add",
                client_key,
                body,
                401,
                headers={
                    "X-Org-Id": "demo",
                    "X-Principal-Type": "user",
                    "X-Principal-Id": "alice",
                },
            )
            print("PASS gateway: valid/missing/spoofed/direct -> 200/401/200/401")
            post(
                base,
                "add",
                client_key,
                {
                    "content": "denied",
                    "scope": {"org": "demo", "user": "bob"},
                },
                403,
            )
            print("PASS gateway authorization: Alice cannot write Bob scope -> 403")
        else:
            print(f"Gateway {base}; backend {backend}; press Ctrl+C to stop", flush=True)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
