"""Local service lifecycle: one process, discovery, status, reload, survival (F17 D2/D7)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from jiuwen_memory.common.security.authentication.authentication_impl.local_token_authenticator import (  # noqa: E501
    LocalTokenStore,
    issue_token,
    token_digest,
)
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory_entry.local.client import ServiceClient
from jiuwen_memory_entry.local.discovery import ensure_service, live_discovery
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home

pytestmark = pytest.mark.integration

MODEL = ModelSettings(
    llm_base_url="https://api.deepseek.com",
    llm_model="deepseek-v4-pro",
    llm_api_key_ref="env:JIUWEN_MEMORY_LLM_KEY",
    embedder_base_url="https://api.siliconflow.cn/v1",
    embedder_model="BAAI/bge-m3",
    embedder_api_key_ref="env:JIUWEN_MEMORY_EMBED_KEY",
)

_ENSURE_SCRIPT = """
import json, sys
from pathlib import Path
from jiuwen_memory_entry.local.discovery import ensure_service
from jiuwen_memory_entry.local.home import MemoryHome

discovery = ensure_service(MemoryHome(Path(sys.argv[1])), timeout=90)
print(json.dumps({"pid": discovery.pid, "nonce": discovery.instance_nonce}))
"""


@pytest.fixture
def home(tmp_path) -> MemoryHome:
    memory_home = MemoryHome(tmp_path / "home")
    init_home(memory_home, document_root=tmp_path / "docs", model=MODEL)
    token = issue_token()
    store = LocalTokenStore(str(memory_home.data_dir / "tokens.db"))
    store.register("test-install", token_digest(token), Scope(org="local", agent="test"), Role.USER)
    store.close()
    yield memory_home
    discovery = live_discovery(memory_home)
    if discovery is not None:
        try:
            os.kill(discovery.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and live_discovery(memory_home) is not None:
            time.sleep(0.1)


def _issue(home: MemoryHome) -> str:
    token = issue_token()
    store = LocalTokenStore(str(home.data_dir / "tokens.db"))
    try:
        store.register(
            "test-install", token_digest(token), Scope(org="local", agent="test"), Role.USER
        )
    finally:
        store.close()
    return token


def test_concurrent_launchers_start_one_detached_service(home):
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _ENSURE_SCRIPT, str(home.root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    results = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=120)
            assert process.returncode == 0, err
            results.append(json.loads(out))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
    assert len({item["pid"] for item in results}) == 1, results
    assert len({item["nonce"] for item in results}) == 1, results
    # The launchers exited; the detached service still answers (D6).
    discovery = live_discovery(home)
    assert discovery is not None, "service did not survive its launcher"
    assert discovery.pid == results[0]["pid"]

    client = ServiceClient(discovery, _issue(home))
    status = client.status()
    assert status["instance_nonce"] == discovery.instance_nonce
    assert status["home"] == home.canonical
    client.lease("client-1")
    client.reload()
    # The old listener keeps answering ``/local/status`` with ``reloading: true`` until it
    # has drained, so the first successful poll is not the end of the reload.
    deadline = time.monotonic() + 60
    after = None
    while time.monotonic() < deadline:
        try:
            state = client.status()
        except Exception:
            time.sleep(0.3)
            continue
        if state.get("reloading") is False:
            after = state
            break
        time.sleep(0.3)
    else:
        pytest.fail("service did not finish reloading")
    assert after["instance_nonce"] == discovery.instance_nonce, "reload changed the instance"


def test_status_requires_a_token(home):
    discovery = ensure_service(home, timeout=90)
    client = ServiceClient(discovery, "not-a-token")
    with pytest.raises(Exception):
        client.status()


def test_uninitialized_home_fails_fast(tmp_path):
    from jiuwen_memory_entry.local.discovery import ServiceUnavailable

    memory_home = MemoryHome(tmp_path / "missing")
    memory_home.run_dir.mkdir(parents=True)
    with pytest.raises(ServiceUnavailable):
        ensure_service(memory_home, timeout=30)
