# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Discovery file, liveness probe and auto-start for the local service (F17 D2/D6).

Deliberately avoids kernel assembly so short-lived host hooks can import it and fail
fast. The only project imports are the small error module and the file-lock helper.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from jiuwen_memory_entry.local.locks import file_lock

PROTOCOL_VERSION = 1
RETRYABLE_EXIT = 3
_HEALTH_PATH = "/local/health"
_ENV_NAME = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-|\})")
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "WINDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
)
_DETACHED_ENV = "JIUWEN_MEMORY_DETACHED"


class ServiceUnavailable(RuntimeError):
    """The service could not be reached or started; callers degrade or retry."""


@dataclass(frozen=True)
class Discovery:
    """Contents of ``run/service.json``."""

    pid: int
    port: int
    protocol: int
    instance_nonce: str
    home: str
    started_at: float
    detached: str = "session"

    def payload(self) -> dict:
        return {
            "pid": self.pid,
            "port": self.port,
            "protocol": self.protocol,
            "instance_nonce": self.instance_nonce,
            "home": self.home,
            "started_at": self.started_at,
            "detached": self.detached,
        }

    @classmethod
    def parse(cls, data: object) -> Discovery | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                pid=int(data["pid"]),
                port=int(data["port"]),
                protocol=int(data["protocol"]),
                instance_nonce=str(data["instance_nonce"]),
                home=str(data["home"]),
                started_at=float(data["started_at"]),
                detached=str(data.get("detached", "session")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def read_discovery(home) -> Discovery | None:
    """Read the discovery file; ``None`` when absent, unreadable or malformed."""
    try:
        text = home.discovery_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return Discovery.parse(json.loads(text))
    except ValueError:
        return None


def write_discovery(home, discovery: Discovery) -> None:
    """Atomically publish the discovery file (owner-only)."""
    home.run_dir.mkdir(parents=True, exist_ok=True)
    temporary = home.discovery_file.with_name(home.discovery_file.name + ".tmp")
    temporary.write_text(
        json.dumps(discovery.payload(), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, home.discovery_file)


def clear_discovery(home, *, nonce: str | None = None) -> None:
    """Remove the discovery file, optionally only when it still carries ``nonce``."""
    if nonce is not None:
        current = read_discovery(home)
        if current is not None and current.instance_nonce != nonce:
            return
    try:
        home.discovery_file.unlink()
    except FileNotFoundError:
        return


def canonical_home(path: str | os.PathLike) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def probe(discovery: Discovery, *, timeout: float = 0.5) -> bool:
    """Whether this discovery file still points at *our* service.

    A generic HTTP 200 is not enough (Review Focus 1): the endpoint must echo the
    data-directory path and the nonce written by the same process.
    """
    url = f"http://127.0.0.1:{discovery.port}{_HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    if body.get("instance_nonce") != discovery.instance_nonce:
        return False
    return canonical_home(body.get("home", "")) == canonical_home(discovery.home)


def live_discovery(home) -> Discovery | None:
    """The discovery file when it still points at a running service."""
    discovery = read_discovery(home)
    if discovery is None:
        return None
    return discovery if probe(discovery) else None


def service_env(home) -> dict[str, str]:
    """Environment for the service process.

    Only variables the service configuration itself references are inherited from the
    launcher, plus a small bootstrap allowlist. Whoever starts the service therefore
    cannot decide the model or behaviour by exporting unrelated variables.
    """
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    try:
        text = home.service_config.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for name in sorted(set(_ENV_NAME.findall(text))):
        if name in os.environ:
            env[name] = os.environ[name]
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _interpreter(home) -> str:
    try:
        recorded = home.interpreter_file.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    return recorded or sys.executable


def service_log_path(home) -> Path:
    return home.run_dir / "service.log"


def _service_log(home) -> int:
    """Append-only descriptor for the service's stderr, so a failed start is readable.

    Discarding it left "exited during startup (code 1)" with no way to see why; the log is
    owner-only because it can contain paths and configuration diagnostics.
    """
    path = service_log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)


def _launch(
    home, *, detached: str, allow_systemd: bool = True
) -> tuple[subprocess.Popen | None, str]:
    """Start the service subprocess; returns ``(handle, detached_mode)``."""
    command = [_interpreter(home), "-m", "jiuwen_memory_entry.local.service", str(home.root)]
    env = service_env(home)
    env[_DETACHED_ENV] = detached
    log = _service_log(home)
    kwargs: dict = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": log,
        "close_fds": True,
    }
    try:
        return _spawn(home, command, kwargs, detached, allow_systemd)
    finally:
        os.close(log)


def _spawn(home, command, kwargs, detached, allow_systemd):
    env = kwargs["env"]
    if os.name == "nt":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        )
        try:
            return subprocess.Popen(command, creationflags=flags, **kwargs), detached
        except OSError:
            degraded = subprocess.Popen(
                command, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x8), **kwargs
            )
            return degraded, "degraded"
    systemd_run = (
        shutil.which("systemd-run")
        if allow_systemd and sys.platform.startswith("linux")
        else None
    )
    if systemd_run:
        # A transient user unit escapes the host's session/cgroup; ``--scope`` would
        # keep it inside the launcher's cgroup and defeat detachment.
        env[_DETACHED_ENV] = "systemd"
        unit = f"jiuwen-memory-{home.root.name or 'local'}-{secrets.token_hex(4)}"
        launched = subprocess.Popen(
            [systemd_run, "--user", "--collect", "--quiet", "--unit", unit, "--", *command],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=kwargs["stderr"],
            close_fds=True,
        )
        return launched, "systemd"
    return subprocess.Popen(command, start_new_session=True, **kwargs), detached


def ensure_service(home, *, timeout: float = 120.0) -> Discovery:
    """Return the running service, starting one if necessary (D6).

    Multiple callers start at most one service: liveness is re-checked under the
    start lock before launching, and a stale discovery file is replaced.
    """
    discovery = live_discovery(home)
    if discovery is not None:
        return discovery
    deadline = time.monotonic() + timeout
    with file_lock(
        home.start_lock,
        timeout=max(timeout, 1.0),
        conflict_message="another process is already starting the memory service",
    ):
        discovery = live_discovery(home)
        if discovery is not None:
            return discovery
        clear_discovery(home)
        detached = "session"
        allow_systemd = True
        while time.monotonic() < deadline:
            process, detached = _launch(
                home, detached=detached, allow_systemd=allow_systemd
            )
            discovery = _wait_ready(home, process, deadline)
            if discovery is not None:
                if detached == "degraded":
                    write_discovery(
                        home,
                        Discovery(
                            **{**discovery.payload(), "detached": "degraded"},
                        ),
                    )
                    discovery = read_discovery(home) or discovery
                return discovery
            if process is not None and process.poll() is not None:
                code = process.returncode
                if detached == "systemd" and code != 0:
                    # The image has systemd-run but no usable user bus; fall back to
                    # a normal detached process instead of failing the whole start.
                    allow_systemd = False
                    detached = "session"
                    continue
                if code == RETRYABLE_EXIT:
                    # A crashed instance's KV TTL lease is still held; retry shortly.
                    time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
                    continue
                if code == 0:
                    # systemd-run style launcher exits after handing off; keep waiting.
                    time.sleep(0.2)
                    continue
                raise ServiceUnavailable(
                    f"memory service exited during startup (code {code}); "
                    f"see {service_log_path(home)}"
                )
        raise ServiceUnavailable(
            f"memory service did not become ready within {timeout:.0f}s: {home.discovery_file}"
        )


def _wait_ready(home, process, deadline: float) -> Discovery | None:
    while time.monotonic() < deadline:
        discovery = live_discovery(home)
        if discovery is not None:
            return discovery
        if process is not None and process.poll() is not None:
            return None
        time.sleep(0.05)
    return None
