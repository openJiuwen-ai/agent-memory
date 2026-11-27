# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Serve the Portal page and its packaged artwork from either HTTP host."""

import re
from http.server import BaseHTTPRequestHandler
from importlib.resources import files


def serve_portal_file(handler: BaseHTTPRequestHandler, path: str) -> bool:
    package = files("jiuwen_memory_entry.portal")
    if path.rstrip("/") == "/portal":
        resource = package.joinpath("index.html")
        content_type = "text/html; charset=utf-8"
    else:
        match = re.fullmatch(
            r"/portal/assets/(agent-[a-z0-9_-]{1,128}\.png|overview-emblem\.png|memory-empty\.svg)",
            path.partition("?")[0],
        )
        if not match:
            return False
        resource = package.joinpath("assets", match[1])
        content_type = "image/svg+xml" if match[1].endswith(".svg") else "image/png"

    try:
        data = resource.read_bytes()
    except FileNotFoundError:
        return False
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)
    return True
