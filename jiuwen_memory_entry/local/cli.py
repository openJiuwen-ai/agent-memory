# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``jiuwen-memory`` command line: init, service lifecycle and host installation.

Other subcommands (``connect``, ``hook``, ``op``, ``task``, ``migrate``, ``space``)
register themselves through :func:`register_subcommand` from their own modules.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from jiuwen_memory.api import ConflictError, ValidationError
from jiuwen_memory_entry.local.client import ServiceClient  # noqa: F401  (re-export)
from jiuwen_memory_entry.local.discovery import (
    ServiceUnavailable,
    clear_discovery,
    ensure_service,
    live_discovery,
    read_discovery,
)
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home, resolve_home
from jiuwen_memory_entry.local.hosts.registry import HOOK_HOSTS, HOST_NAMES, HOSTS, SYNC_HOSTS
from jiuwen_memory_entry.local.install import connect, disconnect

_BUILDERS: dict[str, Callable[[argparse.ArgumentParser], None]] = {}
_DEFAULT_DOCUMENT_ROOT = Path.home() / "Documents" / "jiuwen-memory"
_STATUS_STOP_TIMEOUT = 30.0


def register_subcommand(name: str, builder: Callable[[argparse.ArgumentParser], None]) -> None:
    """Add a subcommand to the shared parser (used by the other local modules)."""
    _BUILDERS[name] = builder


def _home(args) -> MemoryHome:
    if getattr(args, "home", None):
        return MemoryHome(Path(args.home).expanduser())
    return resolve_home()


def _out(message: str) -> None:
    """CLI 的 stdout 唯一出口：用户可见结果，不是日志（G.LOG.02）。"""
    sys.stdout.buffer.write(f"{message}\n".encode())
    sys.stdout.buffer.flush()


def _err(message: str) -> None:
    """CLI 的 stderr 唯一出口：失败原因与诊断。"""
    sys.stderr.buffer.write(f"{message}\n".encode())
    sys.stderr.buffer.flush()


def _executable(name: str) -> str:
    """Resolve an external command to an absolute path (G.EDV.05)."""
    path = shutil.which(name)
    if path is None:
        raise FileNotFoundError(name)
    return path


def _not_initialized(home: MemoryHome) -> int:
    _out(f"not initialized: {home.service_config} is missing; run `jiuwen-memory init`")
    return 1


# -- init ------------------------------------------------------------------- #


def _init_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="data directory (defaults to JIUWEN_MEMORY_HOME)")
    parser.add_argument(
        "--document-root",
        default=str(_DEFAULT_DOCUMENT_ROOT),
        help="Markdown document directory owned by the service",
    )
    parser.add_argument("--org", default="local")
    parser.add_argument("--space", default="default")
    parser.add_argument("--llm-base-url", default="https://api.deepseek.com")
    parser.add_argument("--llm-model", default="deepseek-v4-pro")
    parser.add_argument(
        "--llm-api-key-ref",
        default="env:JIUWEN_MEMORY_LLM_API_KEY",
        help="environment reference such as env:NAME (never the key itself)",
    )
    parser.add_argument("--embedder-base-url", default="https://api.siliconflow.cn/v1")
    parser.add_argument("--embedder-model", default="BAAI/bge-m3")
    parser.add_argument(
        "--embedder-api-key-ref",
        default="env:JIUWEN_MEMORY_EMBED_API_KEY",
        help="environment reference such as env:NAME (never the key itself)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing configuration")


def _run_init(args) -> int:
    home = _home(args)
    init_home(
        home,
        document_root=Path(args.document_root),
        model=ModelSettings(
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            llm_api_key_ref=args.llm_api_key_ref,
            embedder_base_url=args.embedder_base_url,
            embedder_model=args.embedder_model,
            embedder_api_key_ref=args.embedder_api_key_ref,
        ),
        org=args.org,
        space=args.space,
        force=args.force,
    )
    _out(f"initialized: {home.service_config}")
    _out(
        "model keys are references; export the named variables in the service environment "
        "before starting it"
    )
    if not sys.stdin.isatty():
        _out('run "jiuwen-memory scan" to find and connect local agents')
        return 0
    return _run_scan(_scan_run_namespace(home))


# -- service ---------------------------------------------------------------- #


def _service_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "restart", "stop", "status", "ensure", "install"):
        command = sub.add_parser(action)
        command.add_argument("--home")
        if action == "stop":
            command.add_argument("--force", action="store_true")


def _run_service(args) -> int:
    home = _home(args)
    action = args.action
    if action != "install" and not home.service_config.exists():
        return _not_initialized(home)
    if action in ("start", "ensure", "restart"):
        if action == "restart":
            _stop(home, force=False)
        discovery = ensure_service(home)
        _out(f"running: pid={discovery.pid} port={discovery.port} home={home.root}")
        return 0
    if action == "stop":
        return _stop(home, force=args.force)
    if action == "status":
        discovery = live_discovery(home)
        if discovery is None:
            _out(f"not running (home={home.root})")
            return 1
        _out(
            f"running: pid={discovery.pid} port={discovery.port} "
            f"protocol={discovery.protocol} detached={discovery.detached}"
        )
        return 0
    if action == "install":
        return _install(home)
    return 2


def _stop(home: MemoryHome, *, force: bool) -> int:
    discovery = read_discovery(home)
    if discovery is None:
        _out("not running")
        return 0
    # A crashed service can leave a stale discovery file; the PID may belong to an
    # unrelated process by now. Probe and compare the nonce before signalling it.
    live = live_discovery(home)
    if live is None or live.pid != discovery.pid or live.instance_nonce != discovery.instance_nonce:
        clear_discovery(home, nonce=discovery.instance_nonce)
        _out("not running (stale discovery removed)")
        return 0
    if os.name == "nt":
        taskkill = _executable("taskkill")
        subprocess.run(
            [taskkill, "/PID", str(discovery.pid), *(["/F"] if force else [])],
            check=False,
            capture_output=True,
        )
    else:
        os.kill(discovery.pid, signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.monotonic() + _STATUS_STOP_TIMEOUT
    while time.monotonic() < deadline:
        if live_discovery(home) is None:
            _out("stopped")
            return 0
        time.sleep(0.2)
    _out("service did not stop; retry with --force")
    return 1


def _install(home: MemoryHome) -> int:
    """Register the service for login auto-start (best effort, no privilege escalation)."""
    if not home.service_config.exists():
        return _not_initialized(home)
    interpreter = Path(sys.executable)
    try:
        recorded = home.interpreter_file.read_text(encoding="utf-8").strip()
        if recorded:
            interpreter = Path(recorded)
    except OSError:
        pass
    arguments = [str(interpreter), "-m", "jiuwen_memory_entry.local.service", str(home.root)]
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / "com.jiuwen.memory.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            plistlib.dump(
                {
                    "Label": "com.jiuwen.memory",
                    "ProgramArguments": [str(item) for item in arguments],
                    "RunAtLoad": True,
                },
                handle,
            )
        launchctl = _executable("launchctl")
        subprocess.run(
            [launchctl, "bootstrap", f"gui/{os.getuid()}", str(path)], check=False
        )
        _out(f"installed LaunchAgent: {path}")
        return 0
    if sys.platform.startswith("linux"):
        path = Path.home() / ".config" / "systemd" / "user" / "jiuwen-memory.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        quoted = " ".join(_systemd_quote(item) for item in arguments)
        path.write_text(
            "[Unit]\n"
            "Description=Jiuwen Memory local service\n"
            "[Service]\n"
            f"ExecStart={quoted}\n"
            "Restart=on-failure\n"
            "[Install]\n"
            "WantedBy=default.target\n",
            encoding="utf-8",
        )
        systemctl = _executable("systemctl")
        subprocess.run([systemctl, "--user", "daemon-reload"], check=False)
        subprocess.run([systemctl, "--user", "enable", "--now", "jiuwen-memory"], check=False)
        _out(f"installed systemd user unit: {path} (best effort)")
        return 0
    if os.name == "nt":
        command = " ".join(f'"{item}"' for item in arguments)
        schtasks = _executable("schtasks")
        subprocess.run(
            [
                schtasks,
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                "JiuwenMemory",
                "/TR",
                command,
            ],
            check=False,
        )
        _out("installed scheduled task: JiuwenMemory")
        return 0
    _out("service install is not supported on this platform")
    return 1


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- connect / disconnect --------------------------------------------------- #


def _connect_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "host", choices=HOST_NAMES
    )
    parser.add_argument("--home")
    parser.add_argument("--capture", choices=("off", "task"), default=None)
    parser.add_argument("--project")
    parser.add_argument("--org")
    parser.add_argument("--profile", default="")
    parser.add_argument("--edition", choices=("community", "enterprise"), default="")
    parser.add_argument("--scope", choices=("user", "project"), default="")
    parser.add_argument("--project-root", default="")
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument("--hooks", dest="hooks", action="store_true", default=None)
    hooks.add_argument("--no-hooks", dest="hooks", action="store_false")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="accept capture=task and plugin/hook prompts without interaction",
    )


def _run_connect(args) -> int:
    from jiuwen_memory_entry.local.install import select_install

    home = _home(args)
    if not home.service_config.exists():
        return _not_initialized(home)
    if args.hooks is True:
        from jiuwen_memory_entry.local.hosts import CAPABILITIES

        capability = CAPABILITIES.get(args.host)
        if capability is not None and capability.auto_recall != "supported":
            _err(
                f"{args.host} auto recall/capture is {capability.auto_recall}; "
                "MCP works without --hooks"
            )
            return 3
    capture = args.capture
    if capture is None:
        existing = select_install(
            home, args.host,
            profile=args.profile or None,
            edition=args.edition or None,
        )
        capture = existing.capture if existing is not None else "task"
    args.capture = capture
    result = connect(
        home,
        args.host,
        capture=capture,
        project=args.project,
        org=args.org,
        hooks=args.hooks,
        profile=args.profile,
        edition=args.edition,
        scope=args.scope,
        project_root=args.project_root,
        confirm=_connect_confirm(args),
    )
    _out(f"{result.state}: install={result.install_id} {result.message}".strip())
    return 0 if result.state == "complete" else 1


def _connect_confirm(args):
    """Non-interactive capture=off runs unattended; capture=task still needs --yes."""
    if args.yes:
        return _auto_confirm
    if args.capture == "off" and not sys.stdin.isatty():
        return lambda _text: True
    return _confirm_capture


def _auto_confirm(text: str) -> bool:
    _out(text)
    return True


def _confirm_capture(text: str) -> bool:
    _out(text)
    if not sys.stdin.isatty():
        _out("non-interactive: pass --yes to continue")
        return False
    answer = input("continue? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _disconnect_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("install_id")
    parser.add_argument("--home")


def _run_disconnect(args) -> int:
    home = _home(args)
    kept = disconnect(home, args.install_id)
    _out(f"disconnected: {args.install_id}")
    for entry in kept:
        _out(f"kept (modified by user): {entry.path} {entry.section}")
    return 0


# -- host hooks -------------------------------------------------------------- #


def _hook_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("host", choices=("codex",))
    parser.add_argument("event", choices=("UserPromptSubmit", "Stop"))
    parser.add_argument("--install")
    parser.add_argument("--home")


def _install_for_host(home: MemoryHome, host: str) -> str:
    from jiuwen_memory_entry.local.install import _load_install

    matches = []
    if home.installs_dir.is_dir():
        for record in sorted(home.installs_dir.glob("*/install.json")):
            install = _load_install(home, record.parent.name)
            if install is not None and install.host == host and install.state == "connected":
                matches.append(install.install_id)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one connected {host} install, found {len(matches)}; "
            "pass --install"
        )
    return matches[0]


def _run_hook(args) -> int:
    from jiuwen_memory_adapter.codex import hooks as codex_hooks

    payload = codex_hooks.read_payload(sys.stdin.buffer)
    event = codex_hooks.parse_event(args.event, payload)
    if event is None:
        return 2
    home = _home(args)
    try:
        install_id = args.install or _install_for_host(home, args.host)
        token = (home.installs_dir / install_id / "token").read_text(encoding="utf-8").strip()
        discovery = live_discovery(home)
        if discovery is None:
            # The hook may be the only process running: pull the service up once, with
            # a short budget so a slow model load cannot stall the turn.
            try:
                discovery = ensure_service(home, timeout=5.0)
            except Exception:  # noqa: BLE001 - recall is optional
                return 2
        client = ServiceClient(discovery, token)
        body = {
            "host_session_id": event["host_session_id"],
            "turn_id": event["turn_id"],
            "prompt": event["prompt"],
        }
        if event["action"] == "recall":
            status, response = client.post("/local/hook/recall", body)
            if status != 200:
                return 1
            text = codex_hooks.render_recall(
                response.get("view"),
                list(response.get("candidates") or []),
                response.get("notice"),
            )
            if text:
                _out(json.dumps(codex_hooks.output(args.event, text), ensure_ascii=False))
            return 0
        body["answer"] = event["answer"]
        client.post("/local/hook/capture", body)
        return 0
    except Exception as exc:  # noqa: BLE001 - host must not crash on memory trouble
        _err(f"jiuwen-memory hook: {type(exc).__name__}: {exc}")
        return 1


# -- operation lookup -------------------------------------------------------- #


def _op_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    show = actions.add_parser("show", help="show one frozen turn operation by local ID")
    show.add_argument("local_op_id")
    show.add_argument("--home")
    show.add_argument("--install", help="only try this install's token")


def _run_op(args) -> int:

    home = _home(args)
    try:
        discovery = live_discovery(home) or ensure_service(
            home, timeout=10.0
        )
    except Exception as exc:  # noqa: BLE001 - service unavailable
        _err(f"op show failed: {type(exc).__name__}")
        return 1
    installs = (
        [args.install]
        if args.install
        else sorted(path.name for path in home.installs_dir.glob("*") if path.is_dir())
    )
    for install_id in installs:
        token_path = home.installs_dir / install_id / "token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            status, body = ServiceClient(discovery, token).post(
                "/local/op", {"local_op_id": args.local_op_id}
            )
        except Exception:  # noqa: BLE001 - try the next install
            continue
        if status == 200:
            _out(json.dumps(body, ensure_ascii=False))
            return 0
        if status != 404:
            _err(f"op show failed: HTTP {status} {body.get('error', '')}")
            return 1
    _err(f"unknown operation: {args.local_op_id}")
    return 1


# -- sync -------------------------------------------------------------------- #


def _sync_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="sync_action", required=True)
    grant = actions.add_parser("grant", help="authorize host memory import")
    grant.add_argument("host", choices=SYNC_HOSTS)
    grant.add_argument("--install")
    grant.add_argument("--profile")
    grant.add_argument("--edition")
    grant.add_argument("--scope")
    grant.add_argument("--home")
    grant.add_argument("--dir", action="append", required=True)
    grant.add_argument("--types", default="profile,fact,daily_log")
    grant.add_argument("--sessions", action="store_true")
    grant.add_argument("--project")
    grant.add_argument("--mirror-deletes", action="store_true")
    grant.add_argument("--interval", type=int, default=0)
    grant.add_argument("--yes", action="store_true")
    run = actions.add_parser("run", help="import host memory once")
    run.add_argument("host", choices=SYNC_HOSTS)
    run.add_argument("--install")
    run.add_argument("--profile")
    run.add_argument("--edition")
    run.add_argument("--scope")
    run.add_argument("--home")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--since")
    run.add_argument("--limit", type=int)
    run.add_argument("--mirror-deletes", action="store_true")
    run.add_argument("--yes", action="store_true")
    status = actions.add_parser("status")
    status.add_argument("host", choices=SYNC_HOSTS)
    status.add_argument("--install")
    status.add_argument("--profile")
    status.add_argument("--edition")
    status.add_argument("--scope")
    status.add_argument("--home")
    for name in ("recheck",):
        action = actions.add_parser(name)
        action.add_argument("host", choices=SYNC_HOSTS)
        action.add_argument("--install")
        action.add_argument("--profile")
        action.add_argument("--edition")
        action.add_argument("--scope")
        action.add_argument("--home")
        action.add_argument("--op")
        action.add_argument("--yes", action="store_true")
    revoke = actions.add_parser("revoke", help="revoke sync authorization")
    revoke.add_argument("host", choices=SYNC_HOSTS)
    for name in ("--install", "--profile", "--edition", "--scope", "--home"):
        revoke.add_argument(name)
    revoke.add_argument("--delete", choices=("none", "current", "all"), default="none")
    revoke.add_argument("--yes", action="store_true")
    resolve = actions.add_parser("resolve")
    resolve.add_argument("host", choices=SYNC_HOSTS)
    resolve.add_argument("--install")
    resolve.add_argument("--profile")
    resolve.add_argument("--edition")
    resolve.add_argument("--scope")
    resolve.add_argument("--home")
    resolve.add_argument("--op", required=True)
    resolve.add_argument("--action", choices=("adopt", "abandon", "discard"), required=True)
    resolve.add_argument("--replace-choice", choices=("keep_old", "cleanup_old"))
    schedule = actions.add_parser("schedule")
    schedule.add_argument("host", choices=SYNC_HOSTS)
    schedule.add_argument("--install")
    schedule.add_argument("--profile")
    schedule.add_argument("--edition")
    schedule.add_argument("--scope")
    schedule.add_argument("--interval", type=int, required=True)
    schedule.add_argument("--home")


def _sync_confirm(args):
    if getattr(args, "yes", False):
        return _auto_confirm
    return _confirm_capture


def _sync_install(home, host, install_id="", args=None):
    from jiuwen_memory_entry.local.install import select_install

    return select_install(
        home,
        host,
        install_id=install_id or None,
        profile=(getattr(args, "profile", None) or None) if args else None,
        edition=(getattr(args, "edition", None) or None) if args else None,
        scope=(getattr(args, "scope", None) or None) if args else None,
    )


def _sync_egress(home, install) -> str:
    from jiuwen_memory_entry.local.install import _egress_rows, _egress_text

    discovery = live_discovery(home)
    if discovery is None:
        return "service is not running; start it before syncing"
    token = (home.installs_dir / install.install_id / "token").read_text(
        encoding="utf-8"
    ).strip()
    return _egress_text(_egress_rows(home, install, token, discovery))


def _run_sync(args) -> int:
    from jiuwen_memory_entry.local.install import set_sync_grant
    from jiuwen_memory_entry.local.scan import resolve_pending_sync
    from jiuwen_memory_entry.local.sync.grant import SyncGrant, change_grant, new_grant

    home = _home(args)
    install = _sync_install(
        home, args.host, getattr(args, "install", "") or "", args
    )
    if install is None:
        _err(f"no connected install for {args.host}")
        return 1
    grant = SyncGrant.from_dict(install.sync or {})
    if args.sync_action == "status":
        discovery = live_discovery(home)
        if discovery is not None:
            token = (home.installs_dir / install.install_id / "token").read_text(
                encoding="utf-8"
            ).strip()
            status, result = ServiceClient(discovery, token).post(
                "/local/sync/status", {}
            )
            if status == 200:
                _out(json.dumps(result, ensure_ascii=False))
                return 0
        _out(json.dumps(install.sync or {}, ensure_ascii=False))
        return 0
    if args.sync_action == "schedule":
        if grant is None:
            _err("grant sync first")
            return 1
        set_sync_grant(home, install.install_id, change_grant(grant, interval=args.interval))
        return 0
    if args.sync_action == "grant":
        from jiuwen_memory_entry.local.hosts import CAPABILITIES

        types = {item.strip() for item in args.types.split(",") if item.strip()}
        if args.sessions:
            types.add("session_turn")
        if "session_turn" in types:
            capability = CAPABILITIES.get(args.host)
            if capability is not None and capability.session_import != "supported":
                _err(
                    f"{args.host} session import is {capability.session_import}; "
                    "grant without --sessions to import supported types"
                )
                return 3
        project = args.project or install.project
        try:
            candidate = new_grant(
                args.dir, types=types, target_space=project,
                mirror_deletes=args.mirror_deletes, interval=args.interval,
            )
        except ValueError as exc:
            _err(str(exc))
            return 1
        message = (
            _sync_egress(home, install)
            + "\nwill read: "
            + ", ".join(args.dir)
            + f"\ntypes: {', '.join(sorted(types))}"
            + f"\ntarget space: {project}"
            + "\nprofile entries contain personal information and become visible to "
            "the space's members"
        )
        if not _sync_confirm(args)(message):
            return 1
        set_sync_grant(home, install.install_id, candidate)
        resolve_pending_sync(home, install.install_id)
        _out(f"grant {candidate.grant_id} v{candidate.version}")
        return 0
    if grant is None:
        _err("sync is not granted; run `jiuwen-memory sync grant <host> --dir <dir>`")
        return 1
    if args.sync_action in {"revoke", "resolve", "recheck"}:
        discovery = live_discovery(home) or ensure_service(home, timeout=10.0)
        token = (home.installs_dir / install.install_id / "token").read_text(
            encoding="utf-8"
        ).strip()
        client = ServiceClient(discovery, token)
        if args.sync_action == "revoke":
            delete = getattr(args, "delete", "none")
            if delete != "none":
                status, result = client.post("/local/sync/revoke", {"delete": delete})
                if status != 200:
                    _err(f"sync revoke cleanup failed: HTTP {status} {result.get('error', '')}")
                    return 1
                _out(json.dumps(result, ensure_ascii=False))
            set_sync_grant(home, install.install_id, change_grant(grant, revoked=True))
            _out("sync revoked")
            return 0
        if args.sync_action == "resolve":
            status, result = client.post(
                "/local/sync/resolve",
                {
                    "op": args.op,
                    "action": args.action,
                    "replace_choice": getattr(args, "replace_choice", None),
                },
            )
            if status != 200:
                _err(f"sync resolve failed: HTTP {status} {result.get('error', '')}")
                return 1
            _out(json.dumps(result, ensure_ascii=False))
            return 0
        status, result = client.post("/local/sync/recover", {})
        if status != 200:
            _err(f"sync recheck failed: HTTP {status} {result.get('error', '')}")
            return 1
        _out(json.dumps(result, ensure_ascii=False))
        return 0
    if grant.revoked:
        _err("sync is revoked; grant again before running")
        return 1
    if getattr(args, "mirror_deletes", False) and not grant.mirror_deletes:
        _err("mirror deletes must be enabled by `sync grant --mirror-deletes`")
        return 3
    body = {
        "dry_run": bool(getattr(args, "dry_run", False)),
        "since": getattr(args, "since", None),
        "limit": getattr(args, "limit", None),
    }
    body = {key: value for key, value in body.items() if value is not None}
    discovery = live_discovery(home) or ensure_service(home, timeout=10.0)
    token = (home.installs_dir / install.install_id / "token").read_text(
        encoding="utf-8"
    ).strip()
    client = ServiceClient(discovery, token)
    if not body.get("dry_run"):
        status, preview = client.post("/local/sync", {**body, "dry_run": True})
        if status != 200:
            _err(f"sync preview failed: HTTP {status} {preview.get('error', '')}")
            return 1
        _out(json.dumps(preview, ensure_ascii=False))
        if not getattr(args, "yes", False) and not _sync_confirm(args)("apply this sync?"):
            return 1
        if preview.get("plan_id"):
            body["plan_id"] = preview["plan_id"]
    status, report = client.post("/local/sync", body)
    if status != 200:
        _err(f"sync failed: HTTP {status} {report.get('error', '')}")
        return 1
    _out(json.dumps(report, ensure_ascii=False))
    return 0


# -- scan -------------------------------------------------------------------- #

_SCAN_ACTIONS = frozenset({"run", "status", "include", "auto-connect", "revoke"})


def _scan_depth(text: str) -> int:
    """``--depth`` value: a positive number of directory levels."""
    try:
        depth = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("depth must be an integer") from exc
    if depth < 1:
        raise argparse.ArgumentTypeError("depth must be at least 1")
    return depth


def _scan_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="scan_action", required=True)
    run = actions.add_parser("run", help="scan the allowed locations and connect what is found")
    run.add_argument("--home")
    run.add_argument(
        "--add-dir", action="append", default=[], metavar="DIR",
        help="project directory to scan as well (repeatable)",
    )
    run.add_argument(
        "--remove-dir", action="append", default=[], metavar="DIR",
        help="user-added directory to stop scanning (repeatable)",
    )
    run.add_argument(
        "--depth", type=_scan_depth, help="levels to descend in user-added directories"
    )
    run.add_argument("--dry-run", action="store_true", help="list only, connect nothing")
    run.add_argument("--yes", action="store_true", help="allow the scan without interaction")
    run.add_argument(
        "--background", action="store_true",
        help="unattended run: asks nothing and never grants the scan itself",
    )
    status = actions.add_parser("status", help="show the scan grant and what is waiting")
    status.add_argument("--home")
    include = actions.add_parser(
        "include", help="make an ignored instance eligible for auto-connect again"
    )
    include.add_argument("key", help="instance key as listed by `scan status`")
    include.add_argument("--home")
    auto = actions.add_parser(
        "auto-connect", help="turn automatic connecting of new instances on or off"
    )
    auto.add_argument("state", choices=("on", "off"))
    auto.add_argument("--home")
    revoke = actions.add_parser(
        "revoke", help="stop scanning and auto-connecting; installs and sync grants stay"
    )
    revoke.add_argument("--home")


def _scan_run_namespace(home: MemoryHome) -> argparse.Namespace:
    """Arguments of ``scan run`` with every option at its default."""
    return argparse.Namespace(
        scan_action="run",
        home=str(home.root),
        add_dir=[],
        remove_dir=[],
        depth=None,
        dry_run=False,
        yes=False,
        background=False,
    )


def _ask(text: str, prompt: str, *, default: bool) -> bool:
    """Show ``text``, then ask ``prompt``; without a terminal ``default`` answers."""
    if text:
        _out(text)
    if not sys.stdin.isatty():
        return default
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def _instance_label(instance: dict) -> str:
    """Readable form of a scanned instance."""
    fields = [
        f"{name}={instance[name]}"
        for name in ("profile", "edition", "scope", "project_root")
        if instance.get(name)
    ]
    return " ".join((*fields, f"config_root={instance.get('config_root', '')}"))


def _scan_candidate_text(result) -> str:
    from jiuwen_memory_entry.local.scan import instance_key

    lines = [
        f"{result.state}: {result.host} {_instance_label(result.instance)}",
        f"  key: {instance_key(result.host, result.instance)}",
        f"  evidence: {', '.join(result.evidence)}",
        f"  hooks: {'on' if HOSTS[result.host].default_hooks else 'off'} by default",
        f"  importable: {', '.join(result.importable) or 'none'}",
    ]
    if result.reason:
        lines.append(f"  reason: {result.reason}")
    return "\n".join(lines)


def _scan_consent(extra_dirs: Sequence[str], depth: int) -> str:
    """What the scan will do, shown before the user allows it (nothing was looked at yet)."""
    from jiuwen_memory_entry.local.scan import default_locations

    lines = ["scan authorization: agents are looked for in"]
    lines.extend(f"  {path}" for path in default_locations())
    lines.extend(f"  {path} (user-added)" for path in extra_dirs)
    if extra_dirs:
        lines.append(f"user-added directories are descended at most {depth} levels")
    lines.extend(
        (
            "only paths are checked (stat and directory listing); no file content is read",
            "found instances are connected automatically, and so are instances that appear later",
        )
    )
    return "\n".join(lines)


def _scan_extra_dirs(grant, added: list[str], removed: list[str]) -> tuple[str, ...]:
    """The user-added directories after ``--add-dir`` and ``--remove-dir``."""
    kept = list(grant.extra_dirs)
    for item in removed:
        target = str(Path(item).expanduser().resolve())
        kept = [path for path in kept if path != target]
    return tuple(dict.fromkeys((*kept, *added)))


def _scan_notes(report) -> None:
    for note in report.truncated:
        _out(f"not fully scanned: {note}")
    for note in report.skipped:
        _out(f"skipped: {note}")


def _scan_run(home: MemoryHome, args) -> int:
    from jiuwen_memory_entry.local.scan import (
        candidates,
        change_scan_grant,
        connect_results,
        dismiss_instance,
        instance_key,
        load_scan_grant,
        probe_all,
        save_scan_grant,
    )

    grant = load_scan_grant(home)
    changes: dict = {}
    if args.add_dir or args.remove_dir:
        changes["extra_dirs"] = _scan_extra_dirs(grant, args.add_dir, args.remove_dir)
    if args.depth is not None:
        changes["depth"] = args.depth
    if not grant.allowed:
        if args.background:
            _out("scan is not allowed: nothing was looked at")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                _out(
                    "scan is not allowed: run \"jiuwen-memory scan\" in an interactive "
                    "terminal to allow it"
                )
                return 0
            consent = _scan_consent(
                changes.get("extra_dirs", grant.extra_dirs),
                changes.get("depth", grant.depth),
            )
            if not _ask(consent, "allow scanning?", default=True):
                _out("scan not allowed: nothing was looked at")
                return 0
        changes["allowed"] = True
    if changes:
        grant = change_scan_grant(grant, **changes)
        save_scan_grant(home, grant)
    report = probe_all(grant)
    found = candidates(home, report, grant)
    listed = [result for result in report.results if result.state != "found"]
    _out(f"scan: {len(found)} instance(s) to connect, {len(listed)} only listed")
    for result in (*found, *listed):
        _out(_scan_candidate_text(result))
    if args.dry_run:
        _scan_notes(report)
        return 0
    chosen = found
    if args.background:
        if not grant.auto_connect:
            _out("auto-connect is off: nothing was connected")
            _scan_notes(report)
            return 0
    else:
        chosen = []
        asked = sys.stdin.isatty()
        for result in found:
            label = f"{result.host} {_instance_label(result.instance)}"
            if _ask("", f"connect {label}?", default=grant.auto_connect):
                chosen.append(result)
                continue
            if not asked:
                continue
            dismiss_instance(home, result.host, result.instance)
            key = instance_key(result.host, result.instance)
            _out(f"dismissed: {label}; `jiuwen-memory scan include {key}` restores it")
        if not asked and not grant.auto_connect and found:
            _out("auto-connect is off: nothing was connected")
    outcomes = (
        connect_results(home, chosen, confirm=_auto_confirm, background=args.background)
        if chosen
        else []
    )
    failed = False
    for outcome in outcomes:
        text = f"{outcome.state}: {outcome.host}"
        if outcome.install_id:
            text += f" install={outcome.install_id}"
        if outcome.message:
            text += f" ({outcome.message})"
        _out(text)
        failed = failed or outcome.state in {"incomplete", "aborted"}
    if not args.background:
        failed = not _scan_sync_prompts(home, chosen, outcomes) or failed
    _scan_notes(report)
    return 1 if failed else 0


def _scan_sync_prompts(home: MemoryHome, chosen, outcomes) -> bool:
    """Offer, per freshly connected install, to authorize syncing the host's own memory.

    The scan never grants sync itself (F16 D16): the user decides per install,
    and the grant flow shows what would be read and where it goes. Sessions are
    left out by default, as in ``sync grant``.
    """
    from jiuwen_memory_entry.local.scan import instance_key

    by_key = {instance_key(result.host, result.instance): result for result in chosen}
    granted = True
    for outcome in outcomes:
        if outcome.state != "complete":
            continue
        result = by_key.get(outcome.key)
        if result is None:
            continue
        types = [item for item in result.importable if item != "session_turn"]
        if not result.memory_roots or not types:
            continue
        question = f"authorize syncing {outcome.host}'s existing memory now?"
        if not _ask("", question, default=False):
            continue
        granted = _run_sync(
            argparse.Namespace(
                sync_action="grant",
                host=outcome.host,
                install=outcome.install_id,
                home=str(home.root),
                dir=list(result.memory_roots),
                types=",".join(types),
                sessions=False,
                profile=None,
                edition=None,
                scope=None,
                project=None,
                mirror_deletes=False,
                interval=0,
                yes=False,
            )
        ) == 0 and granted
    return granted


def _scan_status(home: MemoryHome, grant) -> None:
    from jiuwen_memory_entry.local.install import _load_install
    from jiuwen_memory_entry.local.scan import default_locations

    _out(f"scan: {'allowed' if grant.allowed else 'not allowed'} (v{grant.version})")
    _out("locations:")
    for path in default_locations():
        _out(f"  {path}")
    for path in grant.extra_dirs:
        _out(f"  {path} (user-added)")
    _out(f"depth: {grant.depth}")
    _out(f"auto-connect: {'on' if grant.auto_connect else 'off'}")
    _out(f"dismissed: {', '.join(grant.dismissed) or 'none'}")
    _out(f"included again: {', '.join(grant.included) or 'none'}")
    _out(f"needs manual action: {', '.join(grant.manual) or 'none'}")
    _out("waiting for sync authorization:")
    if not grant.pending_sync:
        _out("  none")
    for install_id in grant.pending_sync:
        install = _load_install(home, install_id)
        state = f"{install.host}, {install.state}" if install is not None else "no install record"
        _out(f"  {install_id} ({state})")


def _run_scan(args) -> int:
    from jiuwen_memory_entry.local.scan import (
        change_scan_grant,
        include_instance,
        load_scan_grant,
        save_scan_grant,
    )

    home = _home(args)
    if not home.service_config.exists():
        return _not_initialized(home)
    if args.scan_action == "run":
        return _scan_run(home, args)
    grant = load_scan_grant(home)
    if args.scan_action == "status":
        _scan_status(home, grant)
        return 0
    if args.scan_action == "include":
        if args.key not in grant.dismissed and args.key not in grant.manual:
            _err(f"not ignored: {args.key}")
            return 1
        include_instance(home, args.key)
        _out(f"included: {args.key}")
        return 0
    if args.scan_action == "auto-connect":
        save_scan_grant(home, change_scan_grant(grant, auto_connect=args.state == "on"))
        _out(f"auto-connect: {args.state}")
        return 0
    if args.scan_action == "revoke":
        if grant.allowed:
            save_scan_grant(home, change_scan_grant(grant, allowed=False))
        _out(
            "scan revoked: no more scanning or auto-connecting; "
            "existing installs and sync grants are unchanged"
        )
        return 0
    return 2


def _pending_sync_hint(args) -> None:
    """Remind about installs connected by scan whose sync is not authorized yet."""
    if args.command in {"hook", "scan"} or not sys.stdin.isatty():
        return
    from jiuwen_memory_entry.local.scan import load_scan_grant

    try:
        pending = load_scan_grant(_home(args)).pending_sync
    except Exception:
        return
    if pending:
        _err(
            f"{len(pending)} agent(s) connected by scan are waiting for sync authorization: "
            'run "jiuwen-memory scan status"'
        )


# -- hooks ------------------------------------------------------------------- #


def _hooks_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("enable", "disable", "status"):
        action = actions.add_parser(name)
        action.add_argument("host", choices=HOOK_HOSTS)
        action.add_argument("--install")
        action.add_argument("--profile")
        action.add_argument("--home")
        action.add_argument("--yes", action="store_true")


def _hooks_confirm(args):
    if args.yes:
        return _auto_confirm
    return _confirm_capture


def _run_hooks(args) -> int:
    from jiuwen_memory_entry.local.install import select_install, set_hooks

    home = _home(args)
    install = select_install(
        home,
        args.host,
        install_id=getattr(args, "install", None) or None,
        profile=getattr(args, "profile", None) or None,
    )
    if install is None:
        _err(f"no connected install for {args.host}")
        return 1
    if args.action == "status":
        _out("enabled" if install.hooks else "disabled")
        return 0
    result = set_hooks(
        home,
        install.install_id,
        args.action == "enable",
        confirm=_hooks_confirm(args) if args.action == "enable" else None,
    )
    _out(f"{result.state}: {result.message}")
    return 0 if result.state == "complete" else 1


# -- takeover ---------------------------------------------------------------- #


def _takeover_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="takeover_action", required=True)
    on = actions.add_parser(
        "on", help="switch the host's own memory off: agent memory takes over"
    )
    off = actions.add_parser("off", help="switch the host's own memory back on")
    status = actions.add_parser("status", help="show which installs are taken over")
    for action in (on, off, status):
        action.add_argument("host", choices=HOST_NAMES)
        action.add_argument("--install")
        action.add_argument("--profile")
        action.add_argument("--home")
        action.add_argument("--yes", action="store_true")


def _takeover_confirm(args):
    if args.yes:
        return _auto_confirm
    return _confirm_capture


def _takeover_sync_ok(home: MemoryHome, install) -> bool:
    """Whether ``install`` may be taken over: a live sync grant and a finished sync.

    The service knows whether the last sync finished; an operation still
    unconfirmed means it did not. A service that cannot be asked counts the
    same way: takeover waits instead of guessing.
    """
    from jiuwen_memory_entry.local.sync.grant import SyncGrant

    grant = SyncGrant.from_dict(install.sync or {})
    if grant is None or grant.revoked:
        return False
    discovery = live_discovery(home)
    if discovery is None:
        return False
    try:
        token = (home.installs_dir / install.install_id / "token").read_text(
            encoding="utf-8"
        ).strip()
        status, result = ServiceClient(discovery, token).post("/local/sync/status", {})
    except (OSError, ServiceUnavailable):
        return False
    return status == 200 and not result.get("unconfirmed")


def _takeover_status_text(home: MemoryHome, install) -> str:
    from jiuwen_memory_entry.local.install import takeover_blocker, takeover_current

    entry = install.takeover.get("entry") or {}
    lines = [
        f"takeover: {'on' if entry else 'off'}: {install.host} install={install.install_id}"
    ]
    if entry:
        lines.append(f"  setting: {entry.get('path', '')} {entry.get('section', '')}")
        lines.append(f"  original value: {entry.get('value')!r}")
        lines.append(f"  own memory setting now: {takeover_current(home, install)!r}")
        if not install.hooks:
            lines.append(
                "  hooks are off, so nothing records this host: turn its own memory "
                f"back on with `jiuwen-memory takeover off {install.host}`"
            )
    else:
        reason = takeover_blocker(install)
        if reason:
            lines.append(f"  not offered: {reason}")
    return "\n".join(lines)


def _takeover_status(home: MemoryHome, args) -> int:
    from jiuwen_memory_entry.local.install import STATE_CONNECTED, _load_install

    shown = False
    if home.installs_dir.is_dir():
        for record in sorted(home.installs_dir.glob("*/install.json")):
            install = _load_install(home, record.parent.name)
            if install is None:
                continue
            if install.host != args.host or install.state != STATE_CONNECTED:
                continue
            if args.install and install.install_id != args.install:
                continue
            if args.profile and install.profile != args.profile:
                continue
            shown = True
            _out(_takeover_status_text(home, install))
    if not shown:
        _err(f"no connected install for {args.host}")
        return 1
    return 0


def _run_takeover(args) -> int:
    from jiuwen_memory_entry.local.install import select_install, set_takeover

    home = _home(args)
    if not home.service_config.exists():
        return _not_initialized(home)
    if args.takeover_action == "status":
        return _takeover_status(home, args)
    install = select_install(
        home,
        args.host,
        install_id=args.install or None,
        profile=args.profile or None,
    )
    if install is None:
        _err(f"no connected install for {args.host}")
        return 1
    enabled = args.takeover_action == "on"
    result = set_takeover(
        home,
        install.install_id,
        enabled,
        confirm=_takeover_confirm(args) if enabled else None,
        sync_ok=lambda current: _takeover_sync_ok(home, current),
    )
    _out(f"{result.state}: install={result.install_id} {result.message}".strip())
    return {"complete": 0, "unsupported": 3}.get(result.state, 1)


# -- migrate ----------------------------------------------------------------- #


def _migrate_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("host", choices=("jiuwenswarm",))
    parser.add_argument("--config", required=True)
    parser.add_argument("--actor-from-host")
    parser.add_argument("--home")
    parser.add_argument("--yes", action="store_true")


def _run_migrate(args) -> int:
    from jiuwen_memory_entry.local.migrate import (
        MigrationError,
        apply_migration,
        plan_migration,
    )

    home = _home(args)
    if not home.service_config.exists():
        return _not_initialized(home)
    try:
        plan = plan_migration(Path(args.config), actor_export=args.actor_from_host)
        report = apply_migration(
            home, plan, confirm=_confirm_capture if not args.yes else (lambda _text: True)
        )
    except MigrationError as exc:
        _err(f"migrate failed: {exc}")
        return 1
    _out(f"{report.state}: install={report.install_id} verified={','.join(report.verified)}")
    for loss in report.losses:
        _out(f"loss: {loss}")
    return 0 if report.state == "complete" else 1


def _space_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    recover = actions.add_parser("recover-admin", help="grant one install space administration")
    recover.add_argument("--space", required=True)
    recover.add_argument("--as", dest="install_id", required=True)
    recover.add_argument("--home")
    recover.add_argument("--yes", action="store_true")


def _recover_confirm(args):
    """Space recovery has no capture choice; do not read connect-only attributes."""
    if args.yes:
        return _auto_confirm
    return _confirm_capture


def _run_space(args) -> int:
    from jiuwen_memory_entry.local.migrate import MigrationError, recover_admin

    if args.action != "recover-admin":
        return 2
    home = _home(args)
    try:
        result = recover_admin(
            home,
            args.space,
            args.install_id,
            confirm=_recover_confirm(args),
        )
    except (MigrationError, ValidationError) as exc:
        _err(f"space recover-admin failed: {exc}")
        return 1
    _out(f"{result['state']}: {result.get('space', '')} -> {result.get('install_id', '')}")
    return 0 if result["state"] == "complete" else 1


# -- parser ----------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiuwen-memory",
        description="Jiuwen Memory local service: one resident memory per data directory",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create the data directory and service.yaml")
    _init_parser(init)
    service = sub.add_parser("service", help="start/stop/status/install the resident service")
    _service_parser(service)
    connect_parser = sub.add_parser(
        "connect", help="install a host against the local service"
    )
    _connect_parser(connect_parser)
    leave = sub.add_parser("disconnect", help="remove one host installation")
    _disconnect_parser(leave)
    hook = sub.add_parser("hook", help="host hook entry point (reads JSON on stdin)")
    _hook_parser(hook)
    migrate = sub.add_parser("migrate", help="switch a host from the old Runtime to this service")
    _migrate_parser(migrate)
    space = sub.add_parser("space", help="space administration recovery")
    _space_parser(space)
    op = sub.add_parser("op", help="inspect a frozen turn operation")
    _op_parser(op)
    hooks = sub.add_parser("hooks", help="enable/disable/status a host hook layer")
    _hooks_parser(hooks)
    sync = sub.add_parser("sync", help="import host-owned memory into agent memory")
    _sync_parser(sync)
    scan = sub.add_parser("scan", help="find installed local agents and connect them")
    _scan_parser(scan)
    takeover = sub.add_parser(
        "takeover", help="let agent memory replace a host's own memory, or give it back"
    )
    _takeover_parser(takeover)
    for name, builder in _BUILDERS.items():
        extra = sub.add_parser(name)
        builder(extra)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    # ``sync <host>`` is the documented shorthand for ``sync run <host>``.
    if (
        len(raw) >= 2
        and raw[0] == "sync"
        and raw[1] not in {
            "run", "status", "grant", "revoke", "recheck", "resolve", "schedule",
        }
    ):
        raw.insert(1, "run")
    scan_action = raw[1] if len(raw) > 1 else ""
    if raw and raw[0] == "scan":
        unknown = scan_action not in _SCAN_ACTIONS and scan_action not in {"-h", "--help"}
        if not scan_action or unknown:
            raw.insert(1, "run")
    args = parser.parse_args(raw)
    _pending_sync_hint(args)
    handlers = {
        "init": _run_init,
        "service": _run_service,
        "connect": _run_connect,
        "disconnect": _run_disconnect,
        "hook": _run_hook,
        "migrate": _run_migrate,
        "space": _run_space,
        "op": _run_op,
        "hooks": _run_hooks,
        "sync": _run_sync,
        "scan": _run_scan,
        "takeover": _run_takeover,
    }
    registered = _REGISTERED_HANDLERS.get(args.command)
    if registered is not None:
        return registered(args)
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except (ServiceUnavailable, ConflictError, ValidationError, OSError) as exc:
        _err(f"error: {exc}")
        return 1


_REGISTERED_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {}


def register_handler(name: str, handler: Callable[[argparse.Namespace], int]) -> None:
    """Pair a subcommand with its handler (both registration helpers are idempotent)."""
    _REGISTERED_HANDLERS[name] = handler


if __name__ == "__main__":
    main()
