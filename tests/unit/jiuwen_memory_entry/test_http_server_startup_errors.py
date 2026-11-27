# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP server startup failures are reported without a traceback."""

from __future__ import annotations

import argparse
import logging
import socket
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import MemoryRuntime
from jiuwen_memory_entry.http_server import __main__ as http_main
from jiuwen_memory_entry.http_server.__main__ import _server_port, main


def test_main_reports_occupied_port_as_clean_error(caplog):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        with caplog.at_level(logging.ERROR):
            rc = main(["--auth-mode", "dev", "--host", "127.0.0.1", "--port", str(port)])
    finally:
        sock.close()

    assert rc == 2
    assert "HTTP server refused to start" in caplog.text


@pytest.mark.parametrize("port", ["99999", "-1"])
def test_main_rejects_out_of_range_port(port):
    with pytest.raises(argparse.ArgumentTypeError):
        _server_port(port)


@pytest.mark.parametrize("failed_step", ["runtime start", "listener close"])
def test_serve_closes_runtime_even_if_start_or_listener_cleanup_fails(
    monkeypatch, failed_step
) -> None:
    runtime = Mock(spec=MemoryRuntime)
    server = http_main.HttpServer(http_main.load_config([http_main.OFFLINE]), runtime)
    listener = Mock()
    monkeypatch.setattr(http_main, "ThreadingHTTPServer", Mock(return_value=listener))
    error = OSError(failed_step)

    if failed_step == "runtime start":
        runtime.start.side_effect = error
    else:
        listener.server_close.side_effect = error

    with pytest.raises(OSError) as caught:
        server.serve("127.0.0.1", 0)
    assert caught.value is error, "serve() must report the original error"
    runtime.close.assert_called_once_with(wait=True)
