# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP server startup failures are reported without a traceback."""

from __future__ import annotations

import logging
import socket

import pytest

from jiuwen_memory_entry.http_server.__main__ import main


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
    assert "Address already in use" in caplog.text


@pytest.mark.parametrize("port", ["99999", "-1"])
def test_main_rejects_out_of_range_port(port):
    with pytest.raises(SystemExit) as excinfo:
        main(["--port", port])

    assert excinfo.value.code == 2
