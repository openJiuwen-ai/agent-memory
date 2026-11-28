# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``connect`` / ``disconnect``: one install per host, idempotent and resumable (F17 D5).

Every step derives its work from actual state (token file, service.yaml, install
record), so a crash between a side effect and its bookkeeping is repaired by re-running
``connect``. The install record is a progress cache, never the source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from jiuwen_memory.api import (
    LocalTokenStore,
    Role,
    Scope,
    ValidationError,
    issue_token,
    token_digest,
)
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local.client import ServiceClient
from jiuwen_memory_entry.local.home import MemoryHome
from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall
from jiuwen_memory_entry.local.hosts.registry import HOSTS
from jiuwen_memory_entry.local.locks import file_lock

SUPPORTED_HOSTS = set(HOSTS)
CAPTURES = ("off", "task")
STATE_INSTALLING = "installing"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
SETTINGS_LOCK_TIMEOUT = 10.0


def _settings_lock(home: MemoryHome):
    return file_lock(
        home.service_config.with_name("service.yaml.lock"),
        timeout=SETTINGS_LOCK_TIMEOUT,
        conflict_message="another connect is updating service.yaml",
    )


@dataclass(frozen=True)
class InstanceRef:
    kind: str
    config_root: str
    profile: str = ""
    edition: str = ""
    scope: str = ""
    project_root: str = ""

    def matches(self, other: InstanceRef) -> bool:
        return all(
            not getattr(self, name) or not getattr(other, name)
            or getattr(self, name) == getattr(other, name)
            for name in ("kind", "config_root", "profile", "edition", "scope", "project_root")
        )


@dataclass(frozen=True)
class Installation:
    install_id: str
    host: str
    connector_id: str
    actor: Scope
    project: str
    capture: str = "task"
    state: str = STATE_INSTALLING
    entries: list[dict] = field(default_factory=list)
    created_at: float = 0.0
    hooks: bool = True
    sync: dict = field(default_factory=dict)
    profile: str = ""
    edition: str = ""
    instance: InstanceRef | None = None


@dataclass(frozen=True)
class ConnectResult:
    install_id: str
    state: str
    failed_step: int | None = None
    message: str = ""


def _settings(home: MemoryHome) -> dict:
    from jiuwen_memory_entry.core.config_loader import load_layer

    return load_layer(str(home.service_config))


def _token_db(home: MemoryHome, settings: dict) -> str:
    params = (
        settings.get("memory_api", {})
        .get("authenticator", {})
        .get("default", {})
        .get("params", {})
    )
    path = str(params.get("db_path", "")).strip()
    return path or str(home.data_dir / "tokens.db")


def _install_dir(home: MemoryHome, install_id: str) -> Path:
    return home.installs_dir / install_id


def _record_path(home: MemoryHome, install_id: str) -> Path:
    return _install_dir(home, install_id) / "install.json"


def _read_record(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_record(home: MemoryHome, install: Installation) -> None:
    path = _record_path(home, install.install_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(install)
    payload["actor"] = asdict(install.actor)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_install(home: MemoryHome, install_id: str) -> Installation | None:
    data = _read_record(_record_path(home, install_id))
    if not data:
        return None
    return Installation(
        install_id=install_id,
        host=str(data.get("host", "")),
        connector_id=str(data.get("connector_id", "")),
        actor=Scope(**data.get("actor", {})),
        project=str(data.get("project", "")),
        capture=str(data.get("capture", "task")),
        state=str(data.get("state", STATE_INSTALLING)),
        entries=list(data.get("entries", [])),
        created_at=float(data.get("created_at", 0.0)),
        hooks=bool(
            data.get(
                "hooks",
                HOSTS[str(data.get("host", ""))].default_hooks
                if str(data.get("host", "")) in HOSTS
                else False,
            )
        ),
        sync=dict(data.get("sync") or {}),
        profile=str(data.get("profile", "")),
        edition=str(data.get("edition", "")),
        instance=InstanceRef(
            kind=str((data.get("instance") or {}).get("kind", "")),
            config_root=str((data.get("instance") or {}).get("config_root", "")),
            profile=str((data.get("instance") or {}).get("profile", "")),
            edition=str((data.get("instance") or {}).get("edition", "")),
            scope=str((data.get("instance") or {}).get("scope", "")),
            project_root=str((data.get("instance") or {}).get("project_root", "")),
        ),
    )


def select_install(
    home: MemoryHome,
    host: str,
    *,
    install_id: str | None = None,
    profile: str | None = None,
    edition: str | None = None,
    scope: str | None = None,
) -> Installation | None:
    """Select one live install; ambiguous host-only selection is an error."""
    if install_id:
        install = _load_install(home, install_id)
        if install is None or install.host != host or install.state == STATE_DISCONNECTED:
            return None
        return install
    candidates: list[Installation] = []
    if home.installs_dir.is_dir():
        for record in sorted(home.installs_dir.glob("*/install.json")):
            data = _read_record(record)
            if data.get("host") != host or data.get("state") == STATE_DISCONNECTED:
                continue
            install = _load_install(home, record.parent.name)
            if install is None:
                continue
            chosen = install
            if profile is not None and chosen.profile != profile:
                continue
            if edition is not None and chosen.edition != edition:
                continue
            if scope is not None and (
                chosen.instance is None or chosen.instance.scope != scope
            ):
                continue
            candidates.append(chosen)
    if len(candidates) > 1:
        descriptions = ", ".join(
            f"{item.install_id}({item.instance})" if item.instance else item.install_id
            for item in candidates
        )
        raise ValidationError(
            f"multiple {host} installs match; pass --install or --profile: {descriptions}"
        )
    return candidates[0] if candidates else None


def select_installation(
    home: MemoryHome, host: str, *, install_id: str = "", instance: InstanceRef | None = None
) -> Installation | None:
    if install_id:
        return select_install(home, host, install_id=install_id)
    return select_install(
        home, host,
        profile=instance.profile or None if instance else None,
        edition=instance.edition or None if instance else None,
        scope=instance.scope or None if instance else None,
    )


def _find_install(home: MemoryHome, host: str) -> Installation | None:
    """Compatibility helper; host-only commands should call ``select_install``."""
    return select_install(home, host)


def _actor(host: str, install_id: str, org: str) -> Scope:
    """Stable per-install principal; the digest keeps reconnects distinct.

    Using an install-id prefix would collide for every ``jiuwenswarm-*`` install,
    making a reinstall inherit the previous install's memberships and task ownership.
    """
    suffix = hashlib.sha256(install_id.encode("utf-8")).hexdigest()[:16]
    return Scope(org=org, agent=f"{host}-{suffix}")


# -- steps ------------------------------------------------------------------ #


def step_install_id(
    home: MemoryHome,
    host: str,
    *,
    capture: str | None,
    project: str | None,
    org: str,
    hooks: bool | None = None,
    profile: str = "",
    edition: str = "",
    scope: str = "",
    project_root: str = "",
    instance: InstanceRef | None = None,
) -> Installation:
    """Step 0: stable install id, persisted before any side effect."""
    if host not in SUPPORTED_HOSTS:
        raise ValidationError(f"unsupported host: {host}")
    if capture is not None and capture not in CAPTURES:
        raise ValidationError(f"unknown capture mode: {capture}")
    with _settings_lock(home):
        existing = select_installation(home, host, instance=instance)
        if existing is not None:
            # Reconnect must not mutate the live install before confirmation: the
            # service reads this record per request, so changing capture here would
            # take effect while the user is still deciding.
            return existing
        install_id = f"{host}-{secrets.token_hex(8)}"
        settings = _settings(home)
        project_name = project or str(
            (settings.get("local") or {}).get("default_space", "default")
        )
        install = Installation(
            install_id=install_id,
            host=host,
            connector_id=install_id,
            actor=_actor(host, install_id, org),
            project=project_name,
            capture=capture or "task",
            created_at=time.time(),
            hooks=HOSTS[host].default_hooks if hooks is None else hooks,
            profile=profile,
            edition=edition,
            instance=instance,
        )
        _write_record(home, install)
        return install


def step_policy(
    home: MemoryHome,
    install: Installation,
    *,
    capture: str,
    project: str,
    hooks: bool | None = None,
    profile: str | None = None,
    edition: str | None = None,
) -> Installation:
    """Apply the candidate policy only after the user confirms."""
    if capture not in CAPTURES:
        raise ValidationError(f"unknown capture mode: {capture}")
    with _settings_lock(home):
        current = _load_install(home, install.install_id) or install
        updated = replace(
            current,
            capture=capture,
            project=project,
            hooks=current.hooks if hooks is None else hooks,
            profile=current.profile if profile is None else profile,
            edition=current.edition if edition is None else edition,
        )
        if updated != current:
            _write_record(home, updated)
        return updated


def step_token(home: MemoryHome, install: Installation) -> str:
    """Step 1: the token file is authoritative; the digest registration follows it."""
    settings = _settings(home)
    token_path = _install_dir(home, install.install_id) / "token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValidationError(f"empty install token file: {token_path}")
    else:
        token = issue_token()
        temporary = token_path.with_name("token.tmp")
        temporary.write_text(token + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, token_path)
    digest = token_digest(token)
    store = LocalTokenStore(_token_db(home, settings))
    try:
        # A migrated install keeps its verified full actor (space/session); ordinary
        # installs have those empty and must stay under the stricter rule.
        store.register(
            install.install_id,
            digest,
            install.actor,
            Role.USER,
            allow_scope=bool(install.actor.space or install.actor.session),
        )
    finally:
        store.close()
    return token


def step_connector(home: MemoryHome, install: Installation) -> None:
    """Step 2: register the connection entry in ``service.yaml``.

    Two hosts may connect at the same time: read-modify-write is serialized by a file
    lock so neither connection entry is lost.
    """
    with _settings_lock(home):
        settings = _settings(home)
        memory_api = settings.setdefault("memory_api", {})
        globals_ = memory_api.setdefault("globals", {})
        connectors = globals_.setdefault("continuity_connectors", {})
        scope = {
            "org": install.actor.org,
            "space": install.actor.space,
            "user": install.actor.user,
            "agent": install.actor.agent,
            "session": install.actor.session,
        }
        connectors[install.connector_id] = {
            "actor": scope,
            "projects": [install.project],
            "enabled": True,
        }
        _write_settings(home, settings)


def _write_settings(home: MemoryHome, settings: dict) -> None:
    import yaml

    temporary = home.service_config.with_name("service.yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, home.service_config)


def _space_payload(org: str, space: str, actor: Scope) -> dict:
    return {"org": org, "space": space, "member": {"scope": asdict(actor)}}


def _ensure_space(
    home: MemoryHome,
    install: Installation,
    token: str,
    discovery,
) -> None:
    """Step 3: ensure the project space exists and this install is a member."""
    org, project = install.actor.org, install.project
    client = ServiceClient(discovery, token)
    status, body = client.post("/v1/get_space", {"org": org, "space": project})
    if status == 200:
        return _add_member_if_needed(home, install, discovery)
    if status != 404:
        raise ValidationError(f"cannot read project space: HTTP {status} {body.get('error')}")
    settings = _settings(home)
    store = LocalTokenStore(_token_db(home, settings))
    # Creating a space needs organization ADMIN, but the install token must never hold
    # it (other concurrent requests would inherit the role). Use a throwaway admin
    # credential that is revoked as soon as the call returns.
    admin_id = f"space-admin-{uuid.uuid4().hex[:12]}"
    admin_token = issue_token()
    try:
        store.register(admin_id, token_digest(admin_token), install.actor, Role.ADMIN)
        admin = ServiceClient(discovery, admin_token)
        try:
            status, body = admin.post(
                "/v1/create_space",
                {"spec": {"org": org, "space": project, "owner": asdict(install.actor)}},
            )
        finally:
            store.revoke(admin_id)
            store.purge(admin_id)
    finally:
        store.close()
    if status == 200:
        return _add_member_if_needed(home, install, discovery)
    if status == 409:
        # A concurrent first connect won the space id; fall through to membership.
        return _add_member_if_needed(home, install, discovery)
    raise ValidationError(f"cannot create project space: HTTP {status} {body.get('error')}")


def _library(home: MemoryHome) -> list[tuple[Installation, str]]:
    """Every connected install and its token (same-account convenience, see D4)."""
    result: list[tuple[Installation, str]] = []
    if not home.installs_dir.is_dir():
        return result
    for record in sorted(home.installs_dir.glob("*/install.json")):
        install = _load_install(home, record.parent.name)
        if install is None or install.state == STATE_DISCONNECTED:
            continue
        token_path = _install_dir(home, install.install_id) / "token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            result.append((install, token))
    return result


def _add_member_if_needed(home: MemoryHome, install: Installation, discovery) -> None:
    org, project = install.actor.org, install.project
    payload = {"org": org, "space": project}
    own = ServiceClient(discovery, _token_for(home, install.install_id))
    status, members = own.post("/v1/list_space_members", payload)
    if status == 200 and _is_member(members, install.actor):
        return
    for other, token in _library(home):
        if other.install_id == install.install_id:
            continue
        admin = ServiceClient(discovery, token)
        probe, _body = admin.post("/v1/list_space_members", payload)
        if probe != 200:
            continue
        added, body = admin.post(
            "/v1/add_space_member", _space_payload(org, project, install.actor)
        )
        if added == 200 or (added == 409 and _is_member(body, install.actor)):
            return
    raise ValidationError(
        "no administrator install can add this host to the project space; "
        f"run `jiuwen-memory connect {install.host} --as <administrator-install>` or "
        "grant membership with the CLI"
    )


def _is_member(body, actor: Scope) -> bool:
    members = body.get("members") if isinstance(body, dict) else body
    if not isinstance(members, list):
        return False
    target = asdict(actor)
    for entry in members:
        scope = entry.get("scope") if isinstance(entry, dict) else None
        if isinstance(scope, dict) and all(
            str(scope.get(key, "")) == target[key] for key in target
        ):
            return True
    return False


def _token_for(home: MemoryHome, install_id: str) -> str:
    return (_install_dir(home, install_id) / "token").read_text(encoding="utf-8").strip()


def step_reload(home: MemoryHome, install: Installation, token: str, discovery) -> None:
    """Step 4: let the running service load the new connection."""
    ServiceClient(discovery, token).reload()
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        try:
            status = ServiceClient(discovery, token).status()
        except Exception:
            time.sleep(0.2)
            continue
        if not status.get("reloading"):
            return
        time.sleep(0.2)
    raise ValidationError("service did not finish loading the new connection")


def _egress_rows(home: MemoryHome, install: Installation, token: str, discovery) -> list[dict]:
    try:
        return ServiceClient(discovery, token).egress()
    except Exception:  # noqa: BLE001 - the inventory is informational
        return []


def _egress_text(rows: list[dict]) -> str:
    if not rows:
        return "egress inventory unavailable; data destinations could not be read"
    remote = [row for row in rows if row.get("destination") != "local"]
    if not remote:
        return "outbound: no component sends memory content off this machine"
    lines = ["outbound data destinations:"]
    for row in remote:
        kinds = ", ".join(row.get("sends") or []) or "nothing"
        lines.append(
            f"  - {row.get('component')} -> {row.get('destination') or 'unknown'} ({kinds})"
        )
    return "\n".join(lines)


def step_status(home: MemoryHome, install: Installation, token: str, discovery) -> dict:
    """Step 5: confirm the service recognizes this install."""
    status = ServiceClient(discovery, token).status()
    if status.get("instance_nonce") != discovery.instance_nonce:
        raise ValidationError("service instance changed during connect")
    return status


def step_hosts(home: MemoryHome, install: Installation) -> list[EntryRef]:
    """Step 6: MCP first, then the optional hook layer, verifying each independently."""

    interpreter = ""
    try:
        interpreter = home.interpreter_file.read_text(encoding="utf-8").strip()
    except OSError:
        interpreter = ""
    host_install = HostInstall(
        install_id=install.install_id,
        host=install.host,
        connector_id=install.connector_id,
        project=install.project,
        interpreter=interpreter,
        profile=install.profile,
        edition=install.edition,
        scope=install.instance.scope if install.instance else "",
        project_root=install.instance.project_root if install.instance else "",
    )
    host_spec = HOSTS[install.host]
    entries: list[EntryRef] = []
    mcp = host_spec.mcp_writer()
    mcp_entries = mcp.merge(host_install)
    if not mcp.verify(host_install):
        manual = host_spec.manual_ok and all(
            entry.section == "manual" for entry in mcp_entries
        )
        if manual:
            snippet_path = _install_dir(home, install.install_id) / "jiuwenswarm.json"
            for entry in mcp_entries:
                if entry.section == "manual":
                    snippet_path.write_text(
                        json.dumps(entry.value, indent=2) + "\n", encoding="utf-8"
                    )
        else:
            mcp.rollback(host_install, mcp_entries)
            raise ValidationError(f"{install.host} MCP configuration did not verify")
    entries.extend(mcp_entries)
    if install.hooks and not host_spec.native_hooks:
        if host_spec.hook_writer is None:
            raise ValidationError(f"{install.host} has no hook layer")
        hook = host_spec.hook_writer()
        available = getattr(hook, "available", None)
        if callable(available) and not available():
            raise ValidationError(f"{install.host} hook is not available")
        hook_entries = hook.merge(host_install)
        if not hook.verify(host_install):
            hook.rollback(host_install, hook_entries)
            raise ValidationError(f"{install.host} hook configuration did not verify")
        entries.extend(hook_entries)
    return entries


def connect(
    home: MemoryHome,
    host: str,
    *,
    capture: str | None = None,
    project: str | None = None,
    org: str | None = None,
    hooks: bool | None = None,
    profile: str = "",
    edition: str = "",
    scope: str = "",
    project_root: str = "",
    confirm=None,
) -> ConnectResult:
    """Run D5 steps 0–6; every step is idempotent and re-runnable."""
    settings = _settings(home)
    org_name = org or str((settings.get("local") or {}).get("org", "local"))
    install = None
    was_connected = False
    confirmed = False
    try:
        host_spec = HOSTS[host]
        detected = host_spec.instance(
            profile=profile, edition=edition, scope=scope, project_root=project_root
        )
        instance = InstanceRef(host, **detected)
        install = step_install_id(
            home,
            host,
            capture=capture,
            project=project,
            org=org_name,
            hooks=hooks,
            profile=profile,
            edition=edition,
            instance=instance,
        )
        was_connected = install.state == STATE_CONNECTED
        candidate_capture = capture or install.capture
        candidate_project = project or install.project
        candidate_hooks = install.hooks if hooks is None else hooks
        candidate_profile = profile or install.profile
        candidate_edition = edition or install.edition
        token = step_token(home, install)
        discovery = discovery_module.ensure_service(home)
        # Confirm before any space ownership/membership is created: a "no" must leave
        # no service-side residue, and undoing space provisioning is not reliable.
        rows = _egress_rows(home, install, token, discovery)
        prompt = _egress_text(rows)
        if candidate_hooks and HOSTS[host].hook_writer is not None:
            hook = HOSTS[host].hook_writer()
            available = getattr(hook, "available", None)
            if callable(available) and not available():
                raise ValidationError(f"{host} hook is not available")
            host_install = HostInstall(
                install_id=install.install_id,
                host=host,
                connector_id=install.connector_id,
                project=candidate_project,
                profile=candidate_profile,
                edition=candidate_edition,
            )
            preflight = getattr(hook, "preflight", None)
            if callable(preflight):
                messages = preflight(host_install)
                if messages:
                    prompt += "\n" + "\n".join(messages)
        if candidate_capture == "task":
            prompt += (
                f"\ncapture=task will persist this host's bound turns under {home.data_dir}"
                " (the user prompt and final answer above are what gets stored)."
            )
        if confirm is not None and not confirm(
            f"{prompt}\ncontinue and write host configuration?"
        ):
            if not was_connected:
                _abort_connection(home, install, discovery, token)
            return ConnectResult(
                install_id=install.install_id,
                state="aborted",
                message="aborted on request; the existing connection was left unchanged"
                if was_connected
                else "aborted on request; no host configuration was written",
            )
        confirmed = True
        install = step_policy(
            home,
            install,
            capture=candidate_capture,
            project=candidate_project,
            hooks=candidate_hooks,
            profile=candidate_profile,
            edition=candidate_edition,
        )
        step_connector(home, install)
        _ensure_space(home, install, token, discovery)
        step_reload(home, install, token, discovery)
        status = step_status(home, install, token, discovery)
        entries = step_hosts(home, install)
        install = replace(
            install,
            state=STATE_CONNECTED,
            entries=[_entry_record(entry) for entry in entries],
        )
        _write_record(home, install)
        return ConnectResult(
            install_id=install.install_id,
            state="complete",
            message=f"connected {host}; service on port {status.get('port', discovery.port)}",
        )
    except Exception as exc:  # noqa: BLE001 - reported as "incomplete, retry"
        if install is not None and (confirmed or not was_connected):
            # A pre-confirmation failure on a live reconnect changed nothing; rewriting
            # its state to installing would make a later cancel take the cleanup path
            # and revoke the still-valid connection.
            _write_record(home, replace(install, state=STATE_INSTALLING))
        return ConnectResult(
            install_id=install.install_id if install is not None else "",
            state="incomplete",
            message=str(exc),
        )


def _abort_connection(
    home: MemoryHome, install: Installation, discovery, token: str
) -> None:
    """A "no" must leave no service-side residue and stop serving the connection."""
    with _settings_lock(home):
        current = _settings(home)
        connectors = (
            (current.get("memory_api") or {}).get("globals") or {}
        ).get("continuity_connectors") or {}
        if connectors.pop(install.connector_id, None) is not None:
            _write_settings(home, current)
    try:
        ServiceClient(discovery, token).reload()
    except Exception:  # noqa: BLE001 - revocation still takes effect
        pass
    try:
        store = LocalTokenStore(_token_db(home, _settings(home)))
        try:
            store.revoke(install.install_id)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass
    _write_record(home, replace(install, state=STATE_DISCONNECTED))


def _entry_record(entry: EntryRef) -> dict:
    return {
        "path": str(entry.path),
        "section": entry.section,
        "layer": entry.layer,
        "created_file": entry.created_file,
        "raw": entry.raw,
        "value": entry.value,
    }


def _entry_from_record(record: dict) -> EntryRef:
    return EntryRef(
        path=Path(record.get("path", "-")),
        section=str(record.get("section", "")),
        value=record.get("value"),
        raw=str(record.get("raw", "")),
        layer=str(record.get("layer", "mcp")),
        created_file=bool(record.get("created_file", False)),
    )


def disconnect(home: MemoryHome, install_id: str) -> list[EntryRef]:
    """Revoke the token, remove config entries, reload, then roll back host entries."""
    install = _load_install(home, install_id)
    if install is None:
        raise ValidationError(f"unknown install: {install_id}")
    settings = _settings(home)
    store = LocalTokenStore(_token_db(home, settings))
    try:
        store.revoke(install.install_id)
    finally:
        store.close()
    with _settings_lock(home):
        settings = _settings(home)
        memory_api = settings.setdefault("memory_api", {})
        connectors = (memory_api.get("globals") or {}).get("continuity_connectors") or {}
        if connectors.pop(install.connector_id, None) is not None:
            _write_settings(home, settings)
    discovery = discovery_module.live_discovery(home)
    if discovery is not None:
        # The install token is already revoked; another install's token reloads the
        # service so the removed connection stops being served.
        candidate = next((token for other, token in _library(home)
                          if other.install_id != install.install_id), "")
        if candidate:
            try:
                ServiceClient(discovery, candidate).reload()
            except Exception:  # noqa: BLE001 - revocation already took effect
                pass
    entries = [_entry_from_record(item) for item in install.entries]
    host_install = HostInstall(
        install_id=install.install_id,
        host=install.host,
        connector_id=install.connector_id,
        project=install.project,
        profile=install.profile,
        edition=install.edition,
        scope=install.instance.scope if install.instance else "",
        project_root=install.instance.project_root if install.instance else "",
    )
    kept = _rollback_layers(install.host, host_install, entries)
    _write_record(home, replace(install, state=STATE_DISCONNECTED, entries=[]))
    return kept


def _is_hook_entry(entry: EntryRef) -> bool:
    return (
        entry.layer == "hook"
        or entry.section.startswith("hooks.")
        or entry.section in {"memory.provider", "plugin"}
    )


def _rollback_layers(
    host: str, host_install: HostInstall, entries: list[EntryRef]
) -> list[EntryRef]:
    kept: list[EntryRef] = []
    mcp_entries = [entry for entry in entries if not _is_hook_entry(entry)]
    hook_entries = [entry for entry in entries if _is_hook_entry(entry)]
    host_spec = HOSTS[host]
    if mcp_entries:
        kept.extend(host_spec.mcp_writer().rollback(host_install, mcp_entries))
    if hook_entries and host_spec.hook_writer is not None:
        kept.extend(host_spec.hook_writer().rollback(host_install, hook_entries))
    elif hook_entries:
        kept.extend(hook_entries)
    return kept


def set_hooks(
    home: MemoryHome, install_id: str, enabled: bool, *, confirm=None
) -> ConnectResult:
    """Enable/disable only the optional hook layer; MCP entries stay untouched."""
    install = _load_install(home, install_id)
    if install is None:
        raise ValidationError(f"unknown install: {install_id}")
    if HOSTS[install.host].native_hooks:
        return ConnectResult(
            install_id=install_id, state="complete",
            message=f"{install.host} uses its native plugin; hooks are always on",
        )
    if HOSTS[install.host].hook_writer is None:
        return ConnectResult(
            install_id=install_id, state="incomplete",
            message=f"{install.host} has no hook writer",
        )
    interpreter = ""
    try:
        interpreter = home.interpreter_file.read_text(encoding="utf-8").strip()
    except OSError:
        interpreter = ""
    host_install = HostInstall(
        install_id=install.install_id,
        host=install.host,
        connector_id=install.connector_id,
        project=install.project,
        interpreter=interpreter,
        profile=install.profile,
        edition=install.edition,
        scope=install.instance.scope if install.instance else "",
        project_root=install.instance.project_root if install.instance else "",
    )
    writer = HOSTS[install.host].hook_writer()
    try:
        if enabled:
            available = getattr(writer, "available", None)
            if callable(available) and not available():
                return ConnectResult(
                    install_id=install_id, state="incomplete",
                    message=f"{install.host} hook is not available",
                )
            preflight = getattr(writer, "preflight", None)
            messages = preflight(host_install) if callable(preflight) else []
            if confirm is not None and messages and not confirm("\n".join(messages)):
                return ConnectResult(
                    install_id=install_id, state="aborted",
                    message="hook enable aborted; host configuration was not changed",
                )
            new_hook_entries = writer.merge(host_install)
            if not writer.verify(host_install):
                writer.rollback(host_install, new_hook_entries)
                return ConnectResult(
                    install_id=install_id, state="incomplete",
                    message=f"{install.host} hook configuration did not verify",
                )
            records = [
                item for item in install.entries if str(item.get("layer", "mcp")) != "hook"
            ]
            records.extend(_entry_record(entry) for entry in new_hook_entries)
            install = replace(install, hooks=True, entries=records)
        else:
            old = [_entry_from_record(item) for item in install.entries]
            hook_entries = [entry for entry in old if _is_hook_entry(entry)]
            kept = writer.rollback(host_install, hook_entries)
            records = [
                item for item in install.entries if str(item.get("layer", "mcp")) != "hook"
            ]
            records.extend(_entry_record(entry) for entry in kept)
            install = replace(install, hooks=False, entries=records)
        _write_record(home, install)
        return ConnectResult(
            install_id=install_id, state="complete",
            message=f"hooks {'enabled' if enabled else 'disabled'} for {install.host}",
        )
    except Exception as exc:  # noqa: BLE001 - reported to the CLI
        return ConnectResult(install_id=install_id, state="incomplete", message=str(exc))


def set_sync_policy(
    home: MemoryHome,
    install_id: str,
    *,
    allowed: bool | None = None,
    root: str | None = None,
    interval: int | None = None,
    project: str | None = None,
) -> Installation:
    """Update only the sync authorization fields; no host file is touched."""
    with _settings_lock(home), file_lock(
        home.data_dir / "sync-policy.lock",
        timeout=10.0,
        conflict_message="another process is updating sync policy",
    ):
        install = _load_install(home, install_id)
        if install is None:
            raise ValidationError(f"unknown install: {install_id}")
        sync = dict(install.sync or {})
        if allowed is not None:
            sync["allowed"] = bool(allowed)
        if root is not None:
            sync["root"] = str(root)
        if interval is not None:
            if interval < 0:
                raise ValidationError("sync interval must be >= 0")
            sync["interval"] = int(interval)
        if project is not None:
            sync["project"] = str(project)
        updated = replace(install, sync=sync)
        _write_record(home, updated)
        return updated


def set_sync_grant(home: MemoryHome, install_id: str, grant) -> Installation:
    with _settings_lock(home), file_lock(
        home.installs_dir / install_id / "grant.lock",
        timeout=10.0,
        conflict_message="sync authorization is changing",
    ):
        install = _load_install(home, install_id)
        if install is None:
            raise ValidationError(f"unknown install: {install_id}")
        updated = replace(install, sync=grant.as_dict())
        _write_record(home, updated)
        return updated


@dataclass(frozen=True)
class UpgradeResult:
    state: str
    install_id: str
    message: str = ""


def upgrade_install(home: MemoryHome, install_id: str, *, choose_profile) -> UpgradeResult:
    """Fill ``instance`` for records written before instance-aware selection."""
    with _settings_lock(home):
        install = _load_install(home, install_id)
        if install is None:
            raise ValidationError(f"unknown install: {install_id}")
        if install.instance is not None:
            return UpgradeResult("unchanged", install_id)
        host_spec = HOSTS.get(install.host)
        if host_spec is None:
            return UpgradeResult("unchanged", install_id)
        if host_spec.upgrade_instance is None:
            detected = host_spec.instance(
                profile=install.profile,
                edition=install.edition,
                scope=install.instance.scope if install.instance else "",
            )
            instance = InstanceRef(install.host, **detected)
        else:
            upgraded = host_spec.upgrade_instance(
                [dict(entry) for entry in install.entries], choose_profile
            )
            if upgraded is None:
                return UpgradeResult("pending_choice", install_id)
            instance = InstanceRef(install.host, **upgraded)
        _write_record(home, replace(install, instance=instance))
        return UpgradeResult("upgraded", install_id)
