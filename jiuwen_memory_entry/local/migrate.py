# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``migrate jiuwenswarm``: move a JiuwenSwarm install onto the local service (F17 D12).

Authorization for the migrated actor comes only from the old configuration or a trusted
host export — never from guesses. A missing actor is ``unverified`` and the affected
tasks are listed as losses before anything is written. Verification failures roll back
to ``service.yaml.failed`` so the old Runtime can be started again unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from jiuwen_memory.api import (
    ConflictError,
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
from jiuwen_memory_entry.local.install import (
    Installation,
    _install_dir,
    _read_record,
    _settings_lock,
    _token_db,
    _write_record,
)
from jiuwen_memory_entry.local.locks import acquire_file_lock, release_file_lock

_LOCK_WAIT = 2.0


class MigrationError(RuntimeError):
    """Migration refused or failed; the old configuration stays usable."""


@dataclass(frozen=True)
class MigrationPlan:
    actor: Scope | None
    actor_source: Literal["config", "host", "unverified"]
    record_scopes: list[Scope]
    connectors: list[str]
    owned_tasks: dict[str, bool]
    spaces_without_grants: list[str]
    storages: dict[str, str]
    losses: list[str] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)
    connector_specs: dict[str, dict] = field(default_factory=dict)


@dataclass
class MigrationReport:
    state: str
    install_id: str = ""
    verified: list[str] = field(default_factory=list)
    losses: list[str] = field(default_factory=list)
    message: str = ""


def load_legacy_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yml", ".yaml"}:
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise MigrationError(f"{path}: configuration must be an object")
    return data


def _legacy_block(config: dict) -> dict:
    """Find the memory provider block; accept the shapes JiuwenSwarm writes."""
    for key in ("memory", "agent_memory", "external_memory", "jiuwen_memory"):
        block = config.get(key)
        if not isinstance(block, dict):
            continue
        merged = dict(block)
        for sub in ("provider", "options", "jiuwen", "builtin", "agent_memory"):
            nested = block.get(sub)
            if isinstance(nested, dict):
                merged.update(nested)
        return merged
    return {}


def _export_actor(path: Path) -> Scope:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MigrationError(f"{path}: actor export must be an object")
    return Scope(
        org=str(data.get("org", "local")),
        space=str(data.get("space", "")),
        user=str(data.get("user", "")),
        agent=str(data.get("agent", "")),
        session=str(data.get("session", "")),
    )


def _read_registry_samples(path: str, *, limit: int = 5) -> tuple[list[dict], str]:
    """Read task/binding/unit IDs from the legacy continuity SQLite registry.

    Each sample keeps the task owner as it was before migration. For event samples the
    owner is resolved by the event's task ID even when that task is outside the first
    task page; an absent owner is ``None`` (unverified), never mistaken for "owned".
    """
    if not path or not Path(path).exists():
        return [], "registry database not found; existing tasks cannot be sampled"
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:
        return [], "registry database is not readable; tasks and units were not sampled"
    try:
        try:
            tasks = conn.execute(
                'SELECT org, space, "user", agent, session, key, value FROM kv '
                "WHERE key LIKE '/continuity/tasks/%' LIMIT ?",
                (limit,),
            ).fetchall()
            events = conn.execute(
                'SELECT org, space, "user", agent, session, key, value FROM kv '
                "WHERE key LIKE '/continuity/events/%' LIMIT ?",
                (limit * 3,),
            ).fetchall()
        except Exception:
            return [], "registry database is not readable; tasks and units were not sampled"
        samples: list[dict] = []
        owners: dict[str, str | None] = {}
        for org, space, user, agent, session, _key, value in tasks:
            try:
                record = json.loads(value)
            except ValueError:
                continue
            scope = {"org": org, "space": space, "user": user, "agent": agent,
                     "session": session}
            bindings = [
                {"binding_id": item.get("binding_id", ""),
                 "connector_id": item.get("connector_id", "")}
                for item in record.get("bindings") or []
                if isinstance(item, dict)
            ]
            units = []
            for item in record.get("background") or []:
                ref = item.get("ref") if isinstance(item, dict) else None
                if isinstance(ref, dict) and ref.get("unit_id"):
                    units.append(ref["unit_id"])
            task_id = str(record.get("task_id", ""))
            owner = str(record.get("owner", "")) or None
            owners[task_id] = owner
            samples.append(
                {"scope": scope, "task_id": task_id, "owner": owner,
                 "owner_known": owner is not None, "bindings": bindings,
                 "unit_ids": units[:3]}
            )
        for org, space, user, agent, session, _key, value in events:
            try:
                record = json.loads(value)
            except ValueError:
                continue
            units = [
                item.get("unit_id") for item in record.get("refs") or []
                if isinstance(item, dict) and item.get("unit_id")
            ]
            if not units:
                continue
            task_id = str(record.get("task_id", ""))
            if task_id and task_id not in owners:
                row = conn.execute(
                    "SELECT value FROM kv WHERE key = ? LIMIT 1",
                    (f"/continuity/tasks/{task_id}",),
                ).fetchone()
                owner = None
                if row:
                    try:
                        owner = str(json.loads(row[0]).get("owner", "")) or None
                    except (ValueError, AttributeError):
                        owner = None
                owners[task_id] = owner
            owner = owners.get(task_id) if task_id else None
            samples.append(
                {"scope": {"org": org, "space": space, "user": user, "agent": agent,
                           "session": session},
                 "task_id": task_id, "owner": owner,
                 "owner_known": owner is not None, "bindings": [],
                 "unit_ids": units[:3]}
            )
        return samples, ""
    finally:
        conn.close()


def _read_permission_db(path: str) -> tuple[list[Scope], dict[str, bool], str]:
    """Read record scopes/owned tasks from the old permission SQLite, fail closed."""
    if not path or not Path(path).exists():
        return [], {}, "permission database not found; record scopes cannot be recreated"
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT * FROM space_members").fetchall()
            description = conn.execute("SELECT * FROM space_members LIMIT 0").description
            columns = [item[0] for item in description]
        finally:
            conn.close()
    except Exception:
        return [], {}, "permission database is not readable; record scopes were not imported"
    scopes: list[Scope] = []
    positions = {name: index for index, name in enumerate(columns)}
    for row in rows:
        try:
            scopes.append(
                Scope(
                    org=str(row[positions["org"]]) if "org" in positions else "local",
                    space=str(row[positions["space"]]) if "space" in positions else "",
                    user=str(row[positions["user"]]) if "user" in positions else "",
                    agent=str(row[positions["agent"]]) if "agent" in positions else "",
                    session="",
                )
            )
        except (IndexError, TypeError):
            continue
    return scopes, {}, ""


def plan_migration(config_path: Path, *, actor_export: Path | None) -> MigrationPlan:
    config = load_legacy_config(config_path)
    block = _legacy_block(config)
    losses: list[str] = []
    if actor_export is not None:
        actor = _export_actor(actor_export)
        source: Literal["config", "host", "unverified"] = "host"
    else:
        org = str(block.get("identity_org") or "local")
        user = str(block.get("user_id") or block.get("identity_user") or "")
        agent = str(block.get("agent_id") or block.get("identity_agent") or "")
        space = str(block.get("space") or block.get("project") or "default")
        if not user and not agent:
            actor = None
            source = "unverified"
            losses.append(
                "no actor in the legacy configuration and no --actor-from-host export: "
                "tasks visible only to that actor will be unreachable"
            )
        else:
            actor = Scope(org=org, space=space, user=user, agent=agent)
            source = "config"
    permission_db = str(block.get("permission_db") or block.get("permission_path") or "")
    record_scopes, owned_tasks, permission_loss = _read_permission_db(permission_db)
    if permission_loss:
        losses.append(permission_loss)
    connector_specs: dict[str, dict] = {}
    connectors = block.get("connectors")
    if isinstance(connectors, dict):
        for name, spec in connectors.items():
            connector_specs[str(name)] = dict(spec) if isinstance(spec, dict) else {}
    elif isinstance(connectors, list):
        for item in connectors:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("connector_id") or "")
                if name:
                    connector_specs[name] = dict(item)
            else:
                connector_specs[str(item)] = {}
    connector_names = list(connector_specs)
    spaces_without_grants: list[str] = []
    if actor is not None:
        actor_space = actor.space or "default"
        if not any(scope.space == actor_space for scope in record_scopes):
            spaces_without_grants.append(actor_space)
    storages = {}
    for key in (
        "data_dir",
        "document_root",
        "registry_store",
        "storage_path",
        "permission_db",
        "permission_path",
    ):
        if block.get(key):
            storages[key] = str(block[key])
    storages["config"] = str(config_path)
    registry = str(block.get("registry_store") or "")
    samples, sample_loss = _read_registry_samples(registry)
    if sample_loss:
        losses.append(sample_loss)
    return MigrationPlan(
        actor=actor,
        actor_source=source,
        record_scopes=record_scopes,
        connectors=connector_names,
        owned_tasks=owned_tasks,
        spaces_without_grants=spaces_without_grants,
        storages=storages,
        losses=losses,
        samples=samples,
        connector_specs=connector_specs,
    )


def _connector_id(actor: Scope) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", actor.agent or actor.user or "user").strip("-")
    return f"jiuwenswarm-{slug or 'user'}"


def _write_settings(home: MemoryHome, settings: dict) -> None:
    temporary = home.service_config.with_name("service.yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, home.service_config)


def _stop_instance(discovery) -> None:
    try:
        os.kill(discovery.pid, signal.SIGTERM)
    except OSError:
        pass


def _wait_ready(service, *, timeout: float = 60.0) -> None:
    """Reload is asynchronous; do not verify against the old/not-yet-assembled service."""
    if not hasattr(service, "status"):
        return  # test double without a status endpoint
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not service.status().get("reloading"):
                return
        except Exception:  # noqa: BLE001 - the listener is briefly unavailable
            pass
        time.sleep(0.05)
    raise MigrationError("the local service did not finish reloading")


def _post_ready(service, path: str, payload: dict, *, timeout: float = 60.0):
    """Requests sent right after reload can meet the old server's 503 gate."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, body = service.post(path, payload)
        except Exception:  # noqa: BLE001 - listener gap during reload
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
            continue
        if status == 503 and isinstance(body, dict) and body.get("error") == "Reloading":
            if time.monotonic() >= deadline:
                return status, body
            time.sleep(0.05)
            continue
        return status, body


def _fingerprint(actor: dict) -> str:
    canonical = json.dumps(actor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify(
    service,
    plan: MigrationPlan,
    install_id: str,
    actor: Scope,
    connector: str,
    project: str,
) -> list[str]:
    """Read sampled legacy IDs only; the write probe uses an isolated verification task."""
    verified: list[str] = []
    owner = _fingerprint(asdict(actor))
    owned_read = False
    for sample in plan.samples:
        scope = Scope(**sample["scope"])
        task_id = str(sample.get("task_id") or "")
        owner_known = bool(sample.get("owner_known"))
        sample_owner = sample.get("owner") if owner_known else None
        owned = owner_known and sample_owner == owner
        if task_id:
            status, view = _post_ready(
                service,
                "/v1/continuity_task",
                {"scope": asdict(scope),
                 "request": {"operation": "get", "task_id": task_id}},
            )
            if status != 200:
                if status == 403 and owner_known and not owned:
                    # Another agent legally owns this task; its unreadability is not a
                    # storage-loss signal for this install.
                    continue
                if status == 403 and not owner_known:
                    # Owner resolution failed; do not call this a data loss or a
                    # success. The fallback read probe below must still pass.
                    continue
                raise MigrationError(
                    f"cannot read migrated task {task_id!r}: HTTP {status} "
                    f"{view.get('error', '')} {view.get('message', '')}"
                )
            if owner_known and view.get("owner") != sample_owner:
                raise MigrationError(
                    f"task {task_id!r} changed owner across migration"
                )
            if owned:
                owned_read = True
            verified.append(f"task:{task_id}")
        for unit_id in sample.get("unit_ids") or []:
            status, _body = _post_ready(
                service, "/v1/get", {"unit_id": unit_id, "scope": asdict(scope)}
            )
            if status != 200:
                if owned:
                    raise MigrationError(f"migrated unit {unit_id!r} is not readable")
                if status == 403:
                    # Foreign or unresolved ownership: not proof of storage loss; the
                    # read fallback still guards against reporting success blindly.
                    continue
                raise MigrationError(f"migrated unit {unit_id!r} is not readable")
            if owned:
                owned_read = True
            verified.append(f"unit:{unit_id}")
    if not owned_read:
        # No task/unit of the migrating actor was actually read. A successful isolated
        # write does not prove the old storage is reachable, so require a read probe.
        probes = [Scope(org=actor.org, space=project)]
        for scope in probes:
            status, body = _post_ready(
                service,
                "/v1/search",
                {"query": "migration", "context": {"scope": asdict(scope)}, "top_k": 1},
            )
            if status != 200:
                raise MigrationError(f"cannot read personal memory for {scope.space!r}")
            if not body.get("items"):
                raise MigrationError(
                    f"no memory found for {scope.space!r}; the legacy storage is not wired"
                )
            verified.append(f"read:{scope.space}")
    verified.append(_write_probe(service, actor, connector, project, install_id))
    return verified


def _write_probe(
    service, actor: Scope, connector: str, project: str, install_id: str
) -> str:
    """Prove the preserved binding path writes, without touching any user task."""
    scope = Scope(org=actor.org, space=project)
    tag = f"migrate-{install_id}"
    status, task = _post_ready(
        service,
        "/v1/continuity_task",
        {"scope": asdict(scope),
         "request": {"operation": "create", "request_id": f"{tag}-create",
                     "title": "migration verification task"}},
    )
    if status != 200 or not task.get("task_id"):
        raise MigrationError(f"cannot create the isolated verification task: HTTP {status}")
    task_id = str(task["task_id"])
    status, bound = _post_ready(
        service,
        "/v1/continuity_task",
        {"scope": asdict(scope),
         "request": {"operation": "bind", "request_id": f"{tag}-bind",
                     "task_id": task_id, "connector_id": connector,
                     "context_id": tag,
                     "expected_revision": task.get("revision", 0)}},
    )
    if status != 200:
        raise MigrationError(
            f"the preserved connector {connector!r} cannot bind a verification task: "
            f"HTTP {status}"
        )
    body = bound.get("task") if isinstance(bound.get("task"), dict) else bound
    binding_id = ""
    for item in body.get("bindings") or []:
        if item.get("connector_id") == connector:
            binding_id = str(item.get("binding_id", ""))
            break
    if not binding_id:
        raise MigrationError("the isolated verification task has no binding")
    status, _body = _post_ready(
        service,
        "/v1/add",
        {"content": "migration verification write",
         "scope": asdict(scope),
         "continuity": {"task_id": task_id, "binding_id": binding_id,
                        "event_id": f"{tag}-event"}},
    )
    if status != 200:
        raise MigrationError(
            f"the preserved binding {binding_id!r} cannot write: HTTP {status}"
        )
    status, current = _post_ready(
        service,
        "/v1/continuity_task",
        {"scope": asdict(scope),
         "request": {"operation": "get", "task_id": task_id}},
    )
    if status != 200:
        raise MigrationError(
            f"cannot read the verification task revision before close: HTTP {status}"
        )
    revision = current.get("revision", body.get("revision", task.get("revision", 0)))
    status, closed = _post_ready(
        service,
        "/v1/continuity_task",
        {"scope": asdict(scope),
         "request": {"operation": "close", "request_id": f"{tag}-close",
                     "task_id": task_id, "expected_revision": revision}},
    )
    if status != 200:
        raise MigrationError(
            f"cannot close the isolated verification task: HTTP {status} "
            f"{closed.get('error', '')}"
        )
    return f"probe:{task_id}"


def _set_param(api: dict, namespace: str, instance: str, key: str, value: str) -> None:
    api.setdefault(namespace, {}).setdefault(instance, {}).setdefault("params", {})[
        key
    ] = value


def _apply_legacy_storages(settings: dict, plan: MigrationPlan) -> None:
    """Point the assembled stores at the legacy files instead of the fresh template."""
    api = settings.setdefault("memory_api", {})
    root = plan.storages.get("document_root")
    if root:
        _set_param(api, "markdown_store", "default", "root", root)
    registry = plan.storages.get("registry_store")
    if registry and Path(registry).exists():
        _set_param(api, "kv_store", "registry", "db_path", registry)
    data_dir = plan.storages.get("data_dir")
    if data_dir:
        kv = Path(data_dir) / "kv.db"
        shadow = Path(data_dir) / "shadow.db"
        if kv.exists():
            _set_param(api, "kv_store", "default", "db_path", str(kv))
        if shadow.exists():
            _set_param(api, "shadow_index", "default", "db_path", str(shadow))
    permission = plan.storages.get("permission_db") or plan.storages.get("permission_path")
    if permission and Path(permission).exists():
        # Grants/space ownership must move with the data, otherwise the old owner can
        # read its records but cannot manage tasks or keep writing bindings.
        for namespace in ("permission", "grant_store", "delegation_store"):
            _set_param(api, namespace, "default", "db_path", permission)


def _host_connector(plan: MigrationPlan) -> tuple[str, dict]:
    """Select the legacy connector that matches the confirmed host actor."""
    fields = ("org", "space", "user", "agent", "session")
    target = {key: str(getattr(plan.actor, key, "")) for key in fields}
    target_project = plan.actor.space
    matches: list[str] = []
    for name, raw in plan.connector_specs.items():
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        actor = raw.get("actor")
        if not isinstance(actor, dict):
            continue
        current = {key: str(actor.get(key, "")) for key in fields}
        if current != target:
            continue
        projects = {str(project) for project in raw.get("projects") or []}
        if target_project and projects and target_project not in projects:
            continue
        matches.append(name)
    if len(matches) > 1:
        raise MigrationError(
            "multiple legacy connectors match the confirmed actor: "
            + ", ".join(sorted(matches))
        )
    if matches:
        return matches[0], json.loads(
            json.dumps(plan.connector_specs.get(matches[0]) or {})
        )
    if not plan.connector_specs:
        return _connector_id(plan.actor), {}
    raise MigrationError(
        "cannot identify the host connector for the confirmed actor; "
        "refusing to sign another host's identity"
    )


def _merge_migration_settings(
    settings: dict, plan: MigrationPlan
) -> tuple[str, dict, Scope]:
    """Merge the migrated connector into ``settings``; caller holds the settings lock."""
    connector, spec = _host_connector(plan)
    spec.setdefault("actor", asdict(plan.actor))
    spec.setdefault("projects", [plan.actor.space or "default"])
    spec.setdefault("enabled", True)
    actor_fields = ("org", "space", "user", "agent", "session")
    install_actor = Scope(
        **{key: str(spec["actor"].get(key, "")) for key in actor_fields}
    )
    if not install_actor.org:
        raise MigrationError("the preserved connector actor has no org")
    connectors = (
        settings.setdefault("memory_api", {})
        .setdefault("globals", {})
        .setdefault("continuity_connectors", {})
    )
    # Preserve every connector's original actor/project/enabled mapping; rewriting them
    # all to one actor would make attribution ambiguous and destroy the other hosts'
    # identity. The host's connector (first) is used for the write probe.
    seen: list[tuple[str, str, bool, set[str]]] = []
    for name, raw in plan.connector_specs.items():
        item = json.loads(json.dumps(raw or {}))
        item.setdefault("actor", asdict(plan.actor))
        item.setdefault("projects", [plan.actor.space or "default"])
        item.setdefault("enabled", True)
        actor_key = json.dumps(item["actor"], sort_keys=True)
        projects = {str(project) for project in item.get("projects") or []}
        enabled = bool(item.get("enabled", True))
        for other, other_key, other_enabled, other_projects in seen:
            same_actor = actor_key == other_key and enabled and other_enabled
            if same_actor and projects & other_projects:
                raise MigrationError(
                    f"legacy connectors {other!r} and {name!r} share one actor in "
                    "an overlapping project; continuation writes would be ambiguous"
                )
        seen.append((name, actor_key, enabled, projects))
        connectors[name] = item
    connectors[connector] = spec
    _apply_legacy_storages(settings, plan)
    settings.setdefault("local", {})["legacy"] = dict(plan.storages)
    return connector, spec, install_actor


def _restore_settings(settings_path: Path, backup: Path) -> None:
    """Put the pre-migration configuration back; caller holds the settings lock."""
    if settings_path.exists():
        settings_path.replace(settings_path.with_name("service.yaml.failed"))
    backup.replace(settings_path)


def apply_migration(
    home: MemoryHome,
    plan: MigrationPlan,
    *,
    confirm,
    verify=None,
) -> MigrationReport:
    if plan.losses and not confirm("losses:\n" + "\n".join(plan.losses) + "\ncontinue?"):
        return MigrationReport(state="aborted", losses=plan.losses)
    if not confirm("stop the old JiuwenSwarm process before migrating?"):
        return MigrationReport(state="aborted", losses=plan.losses)
    if plan.actor is None:
        raise MigrationError("cannot migrate without a verified actor")
    lock = home.start_lock
    try:
        handle = acquire_file_lock(
            lock, timeout=_LOCK_WAIT, conflict_message="data directory is locked"
        )
    except ConflictError as exc:
        raise MigrationError("another process holds the data directory lock") from exc
    release_file_lock(handle)
    settings_path = home.service_config
    backup = settings_path.with_name("service.yaml.migrating")
    install_id = f"jiuwenswarm-{int(time.time() * 1000):x}"
    # service.yaml is read-modify-written here. ``connect`` writes the same file under
    # this lock, so an unlocked migration could drop a connection created in between.
    with _settings_lock(home):
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        settings_path.replace(backup)
        try:
            connector, spec, install_actor = _merge_migration_settings(settings, plan)
            _write_settings(home, settings)
        except Exception:
            _restore_settings(settings_path, backup)
            raise
    try:
        # Migration keeps the verified full actor (project/session included) so task
        # ownership and existing bindings keep matching; ordinary installs must not.
        token = issue_token()
        token_path = _install_dir(home, install_id) / "token"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token + "\n", encoding="utf-8")
        os.chmod(token_path, 0o600)
        store = LocalTokenStore(_token_db(home, settings))
        try:
            store.register(
                install_id, token_digest(token), install_actor, Role.USER,
                allow_scope=True,
            )
        finally:
            store.close()
        projects = [str(item) for item in spec.get("projects") or []]
        if not projects:
            projects = [install_actor.space or "default"]
        project = (
            install_actor.space
            if install_actor.space and install_actor.space in projects
            else projects[0]
        )
        _write_record(
            home,
            Installation(
                install_id=install_id,
                host="jiuwenswarm",
                connector_id=connector,
                actor=install_actor,
                project=project,
                capture="task",
                state="connected",
            ),
        )
        discovery = discovery_module.ensure_service(home)
        migrated = ServiceClient(discovery, token)
        migrated.reload()
        _wait_ready(migrated)
        verified = (verify or _verify)(
            migrated, plan, install_id, install_actor, connector, project,
        )
    except Exception as exc:
        # Keep the failed configuration for inspection and restore the old one so the
        # previous Runtime can be started again unchanged.
        with _settings_lock(home):
            _restore_settings(settings_path, backup)
        store = LocalTokenStore(_token_db(home, settings))
        try:
            store.revoke(install_id)
        finally:
            store.close()
        discovery = discovery_module.live_discovery(home)
        if discovery is not None:
            _stop_instance(discovery)
        raise MigrationError(f"migration verification failed: {exc}") from exc
    backup.unlink(missing_ok=True)
    return MigrationReport(
        state="complete",
        install_id=install_id,
        verified=verified,
        losses=list(plan.losses),
    )


def _recovery_secret(home: MemoryHome, secret: str) -> Path:
    path = home.data_dir / "recovery.secret"
    path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def recover_admin(home: MemoryHome, space: str, install_id: str, *, confirm) -> dict:
    """Re-grant space administration to one install after the original admin is lost."""
    record = _read_record(_install_dir(home, install_id) / "install.json")
    if not record:
        raise ValidationError(f"unknown install: {install_id}")
    if not confirm(f"grant space administration of {space!r} to {install_id}?") or not confirm(
        "this performs an audited local maintenance operation; continue?"
    ):
        return {"state": "aborted"}
    actor = Scope(**record["actor"])
    discovery = discovery_module.ensure_service(home)
    secret = secrets.token_urlsafe(32)
    path = _recovery_secret(home, secret)
    try:
        # The service calls the space component's add_member on the same KV store it
        # assembled; a direct SQLite edit could target a different store than
        # space.default resolved at runtime.
        status, body = ServiceClient(discovery, "").post(
            "/local/recover-admin",
            {
                "secret": secret,
                "org": actor.org,
                "space": space,
                "member": {"scope": asdict(actor)},
            },
        )
    finally:
        path.unlink(missing_ok=True)
    if status != 200:
        raise MigrationError(
            f"recover-admin failed: HTTP {status} {body.get('error', '')}"
        )
    with (home.data_dir / "audit.log").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"action": "recover_admin", "space": space, "install": install_id,
                 "at": time.time()},
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"state": "complete", "space": space, "install_id": install_id,
            "detail": body}
