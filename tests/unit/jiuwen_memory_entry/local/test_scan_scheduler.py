"""Periodic host scan started by the resident service (F16 D16).

The service never connects anything in its own process: ``connect`` asks the
service to reload, and a reload stops and joins the scan thread. A due check
starts ``jiuwen-memory scan run --background`` as a separate process instead.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from jiuwen_memory_entry.local import install as install_module
from jiuwen_memory_entry.local import service as service_module
from jiuwen_memory_entry.local.home import MemoryHome
from jiuwen_memory_entry.local.scan import (
    ScanGrant,
    change_scan_grant,
    load_scan_grant,
    save_scan_grant,
)
from jiuwen_memory_entry.local.service import LocalService

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0
DAY = 24 * 60 * 60.0


@pytest.fixture(autouse=True)
def host_locations(tmp_path, monkeypatch):
    """Keep every default host location inside the test directory, away from the real ``~``."""
    for variable in (
        "JIUWEN_CODEX_HOME",
        "JIUWEN_DSH_HOME",
        "JIUWEN_HERMES_HOME",
        "JIUWEN_WORKBUDDY_HOME",
        "JIUWEN_CODEBUDDY_HOME",
    ):
        name = variable.removeprefix("JIUWEN_").removesuffix("_HOME").lower()
        monkeypatch.setenv(variable, str(tmp_path / "hosts" / name))


@pytest.fixture
def home(tmp_path) -> MemoryHome:
    """A data directory whose user allowed the scan, auto-connect included."""
    memory = MemoryHome(tmp_path / "memory")
    save_scan_grant(memory, ScanGrant(allowed=True))
    return memory


@pytest.fixture
def spawned(monkeypatch) -> list[tuple[list[str], dict]]:
    """Every process the service tried to start, as ``(command, options)``; none really runs."""
    calls: list[tuple[list[str], dict]] = []

    def popen(command, **options):
        calls.append((list(command), options))

    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


def _last_scan(memory: MemoryHome) -> float:
    return float((memory.data_dir / "scan.last").read_text(encoding="utf-8"))


def _wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


@contextmanager
def running_scheduler(service: LocalService) -> Iterator[None]:
    """The scheduler thread of ``service``, stopped again however the block ends."""
    service.start_scan_scheduler()
    try:
        yield
    finally:
        service.stop_scan_scheduler()


@pytest.mark.parametrize(
    ("last", "due"),
    [(None, True), (NOW - DAY + 1.0, False), (NOW - DAY, True)],
    ids=["never-scanned", "less-than-a-day-ago", "a-day-ago"],
)
def test_scan_due(last, due):
    assert service_module.scan_due(last, NOW) is due, f"last scan at {last}: due should be {due}"


def test_scan_due_honours_the_interval():
    assert service_module.scan_due(NOW - 3600.0, NOW, interval=7200.0) is False, (
        "an interval of two hours is not over after one"
    )
    assert service_module.scan_due(NOW - 7200.0, NOW, interval=7200.0) is True


def test_scheduler_spawns_cli_subprocess_not_connect(home, spawned, monkeypatch):
    connected: list[tuple[tuple, dict]] = []

    def connect(*args, **kwargs):
        connected.append((args, kwargs))
        raise AssertionError("connect must not run inside the service process")

    monkeypatch.setattr(install_module, "connect", connect)

    LocalService(home).scan_tick(NOW)

    ((command, options),) = spawned
    assert command == [
        service_module.sys.executable,
        "-m",
        "jiuwen_memory_entry.local.cli",
        "scan",
        "run",
        "--background",
        "--home",
        str(home.root),
    ], "the scan is a separate process on this data directory, never a call in here"
    assert (options["stdin"], options["stdout"], options["stderr"]) == (
        (subprocess.DEVNULL,) * 3
    ), "nobody is there to answer or read it"
    assert options["start_new_session"] is True, "the scan outlives the service's session"
    assert options["env"] == os.environ.copy(), (
        "the scan needs the service's environment: PATH to find the dsh command, HOME for hosts"
    )
    assert connected == [], "nothing is connected inside the service process"
    assert _last_scan(home) == NOW, "the start of the scan is what counts as the last one"


def test_scan_is_not_repeated_within_a_day(home, spawned):
    service = LocalService(home)

    service.scan_tick(NOW)
    service.scan_tick(NOW + 60.0)

    assert len(spawned) == 1, "a check a minute later starts nothing"
    assert _last_scan(home) == NOW, "a check that starts nothing does not move the last scan"


def test_last_scan_time_outlives_the_service(home, spawned):
    LocalService(home).scan_tick(NOW)

    LocalService(home).scan_tick(NOW + 3600.0)

    assert len(spawned) == 1, "a reloaded or restarted service still knows when it last scanned"


def test_scan_is_due_again_a_day_later(home, spawned):
    service = LocalService(home)
    service.scan_tick(NOW)

    service.scan_tick(NOW + DAY)

    assert len(spawned) == 2, "a day after the last scan the next one is due"
    assert _last_scan(home) == NOW + DAY


def test_unreadable_last_scan_time_means_due(home, spawned):
    home.data_dir.mkdir(parents=True)
    (home.data_dir / "scan.last").write_text("yesterday", encoding="utf-8")

    LocalService(home).scan_tick(NOW)

    assert len(spawned) == 1, "a marker that is not a timestamp is no proof of a recent scan"
    assert _last_scan(home) == NOW, "it is replaced by a real one"


def test_failed_spawn_keeps_the_scan_due(home, monkeypatch):
    def refuse(_command, **_options):
        raise OSError("fork failed")

    monkeypatch.setattr(subprocess, "Popen", refuse)

    with pytest.raises(OSError, match="fork failed"):
        LocalService(home).scan_tick(NOW)

    assert not (home.data_dir / "scan.last").exists(), (
        "a scan that never started is still due at the next check"
    )


def test_scheduler_respects_auto_connect_off(tmp_path, spawned):
    memory = MemoryHome(tmp_path / "memory")
    save_scan_grant(memory, ScanGrant(allowed=True, auto_connect=False))

    LocalService(memory).scan_tick(NOW)

    assert spawned == [], "auto-connect off: the service starts no scan"
    assert not (memory.data_dir / "scan.last").exists(), (
        "nothing was scanned, so nothing is recorded"
    )


@pytest.mark.parametrize("granted_before", [True, False], ids=["revoked", "never-granted"])
def test_scheduler_scans_only_with_a_grant(tmp_path, spawned, granted_before):
    memory = MemoryHome(tmp_path / "memory")
    if granted_before:
        save_scan_grant(memory, change_scan_grant(ScanGrant(allowed=True), allowed=False))
    service = LocalService(memory)

    service.scan_tick(NOW)

    assert spawned == [], "without the user's grant nothing is looked at"
    assert not (memory.data_dir / "scan.last").exists(), (
        "nothing was scanned, so nothing is recorded"
    )

    save_scan_grant(memory, change_scan_grant(load_scan_grant(memory), allowed=True))
    service.scan_tick(NOW + 60.0)

    assert len(spawned) == 1, "allowing the scan is picked up by the next check"


def test_scan_scheduler_waits_then_checks_until_stopped(tmp_path, monkeypatch):
    ticks: list[float] = []
    monkeypatch.setattr(service_module, "SCAN_FIRST_DELAY", 0.3)
    monkeypatch.setattr(service_module, "SCAN_CHECK_EVERY", 0.01)
    monkeypatch.setattr(LocalService, "_scan_tick", lambda self, now: ticks.append(now))
    service = LocalService(MemoryHome(tmp_path / "memory"))

    with running_scheduler(service):
        time.sleep(0.1)
        assert ticks == [], (
            "the first check waits: a service that just started has other work first"
        )
        thread = getattr(service, "_scan_thread")
        _wait_until(lambda: len(ticks) >= 3)

    assert len(ticks) >= 3, "checks go on until the service stops them"
    assert thread is not None and thread.daemon, "the thread never keeps the process alive"
    assert not thread.is_alive(), "stopping joins the thread"
    assert getattr(service, "_scan_stop") is None and getattr(service, "_scan_thread") is None, (
        "a stopped scheduler leaves nothing behind"
    )


def test_scan_scheduler_survives_a_failed_check(tmp_path, monkeypatch, caplog):
    ticks: list[float] = []

    def tick(_self, now):
        ticks.append(now)
        if len(ticks) == 1:
            raise RuntimeError("grant file is locked")

    monkeypatch.setattr(service_module, "SCAN_FIRST_DELAY", 0.01)
    monkeypatch.setattr(service_module, "SCAN_CHECK_EVERY", 0.01)
    monkeypatch.setattr(LocalService, "_scan_tick", tick)
    service = LocalService(MemoryHome(tmp_path / "memory"))

    with caplog.at_level(logging.ERROR, logger="agent-memory.local"):
        with running_scheduler(service):
            _wait_until(lambda: len(ticks) >= 3)

    assert len(ticks) >= 3, "one failed check does not end the scheduler"
    assert any("periodic scan failed" in record.message for record in caplog.records), (
        "the failure is reported, not swallowed"
    )
