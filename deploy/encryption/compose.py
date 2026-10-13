# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run an existing Compose profile with a verified encrypted data volume.

One STORAGE_ENCRYPTION_ENABLED switch selects the original deployment or a checked bind-mount
overlay plus KV/FS encryption. No daemon volume is silently reformatted or migrated.
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import re
import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from urllib.parse import urlparse

import yaml

from deploy.encryption.volume import executable, verify_mount
from jiuwen_memory.config import Config
from jiuwen_memory.config.encryption import encryption_enabled

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)

_SERVICES = {
    "redis": "redis",
    "elasticsearch": "elasticsearch",
    "milvus": "milvus",
    "postgres": "postgres",
    "pgvector": "postgres",
    "milvus_graph": "milvus",
}
_FIELDS = {
    "redis": "url",
    "elasticsearch": "hosts",
    "milvus": "uri",
    "postgres": "dsn",
    "pgvector": "dsn",
    "milvus_graph": "uri",
}
_KEY_TARGET = "/run/secrets/agent-memory-master.key"
_DATA_TARGET = "/var/lib/agent-memory"


@dataclass(frozen=True)
class DeploymentLocation:
    """A profile and its host data root identify one deployment layout."""

    data_root: Path
    profile: str


@contextmanager
def private_text_file(path: Path, flags: int) -> Iterator[TextIOWrapper]:
    """Keep descriptor ownership even if constructing or writing the text stream fails."""
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _instance_name(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("deployment instance names must be safe directory components")


def load_environment(path: Path) -> dict[str, str]:
    """Read literal Compose .env values; never evaluate shell expressions."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "").isalnum():
            raise ValueError("unsupported .env entry; use literal KEY=value")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("unclosed quoted .env value")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    values.update(os.environ)
    return values


def _expanded(value: object, environment: dict[str, str]) -> object:
    # Reuse the application's expansion rules; only expand non-secret location fields here.
    return (
        Config.from_yaml_str(yaml.safe_dump({"globals": {"value": value}}), **environment)
        .context()
        .globals["value"]
    )


def _bind(source: Path, target: str, *, read_only: bool = False) -> dict:
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def _declaration(bindings: dict, reference: str) -> dict:
    return {"method": "volume", "reference": reference, "bindings": bindings}


def build_plan(
    compose: dict,
    config: dict,
    location: DeploymentLocation,
    environment: dict[str, str],
    reference: str,
) -> tuple[dict, dict, dict[Path, int]]:
    """Pure planner. Persistent named volumes become encrypted-root binds, by target."""
    if location.profile not in {"local", "online", "postgres"}:
        raise ValueError("unknown deployment profile")
    profile = location.profile
    root = location.data_root.resolve() / profile
    control = root / ".control"
    application = root / "application"
    directories = {control: 0, application: 10001}
    overlay: dict = {"services": {}}
    declared_volumes = set(compose.get("volumes", {}))
    for service, spec in compose["services"].items():
        overrides = []
        for mount in spec.get("volumes", []):
            if not isinstance(mount, str):
                raise ValueError("base profile volume syntax changed; update encrypted planner")
            parts = mount.split(":")
            if len(parts) < 2:
                raise ValueError("anonymous volumes are not permitted in encrypted profiles")
            source, target = parts[:2]
            if source in declared_volumes:
                path = root / source
                directories[path] = (
                    10001
                    if service == "agent-memory"
                    else 1000
                    if service == "elasticsearch"
                    else 0
                )
                overrides.append(_bind(path, target))
        if service == "agent-memory":
            overrides.extend(
                [
                    _bind(control / "config.yml", "/config/config.yml", read_only=True),
                    _bind(control / "master.key", _KEY_TARGET, read_only=True),
                    _bind(application, _DATA_TARGET),
                ]
            )
        if overrides:
            overlay["services"][service] = {"volumes": overrides}

    generated = copy.deepcopy(config)
    memory = generated["memory_api"]
    globals_ = memory.setdefault("globals", {})
    globals_["storage_encryption_enabled"] = True
    globals_["storage_encryption_cryptography"] = "at_rest"
    if "at_rest" in memory.get("cryptography", {}) or "at_rest" in memory.get("key_provider", {}):
        raise ValueError("reserved deployment provider 'at_rest' is already configured")
    memory.setdefault("cryptography", {})["at_rest"] = {
        "target": "local",
        "params": {"key_provider": "at_rest"},
    }
    memory.setdefault("key_provider", {})["at_rest"] = {
        "target": "local",
        "params": {"key_file": _KEY_TARGET, "key_env": "", "create_key_file": False},
    }
    # Protect independent SQLite governance data in the same verified application bind.
    for namespace in ("audit", "permission"):
        entries = memory.setdefault(namespace, {})
        if "default" not in entries:
            entries["default"] = {"target": "sqlite"}
        for name, spec in entries.items():
            _instance_name(name)
            if isinstance(spec, str):
                spec = entries[name] = {"target": spec}
            if spec.get("target") not in {"sqlite", "space_aware"}:
                continue
            path = f"{_DATA_TARGET}/{namespace}-{name}.db"
            params = spec.setdefault("params", {})
            old = params.get("db_path", ":memory:")
            if old not in {":memory:", path}:
                raise ValueError(
                    "SQLite data needs an explicit offline migration into the data root"
                )
            params["db_path"] = path
            params["at_rest"] = _declaration({"db_path": path}, reference)

    # Provide an FS endpoint for callers; this does not add asset handling to MemoryAPI.
    memory.setdefault("fs_store", {}).setdefault(
        "default",
        {
            "target": "local",
            "params": {"root": f"{_DATA_TARGET}/fs_store-default"},
        },
    )
    for namespace in (
        "kv_store",
        "fs_store",
        "fulltext_store",
        "vector_store",
        "graph_store",
        "fusion_store",
        "entity_store",
        "markdown_store",
        "shadow_index",
    ):
        for name, spec in memory.get(namespace, {}).items():
            _instance_name(name)
            if isinstance(spec, str):
                spec = memory[namespace][name] = {"target": spec}
            target = spec.get("target")
            params = spec.setdefault("params", {})
            if target in {"memory", "encrypted"}:
                continue
            if target in {"local", "nano_graphrag", "milvus_graph"}:
                field = "root" if target == "local" else "working_dir"
                path = f"{_DATA_TARGET}/{namespace}-{name}"
                old = params.get(field)
                if old and old != path:
                    raise ValueError("local persistent storage needs an explicit offline migration")
                params[field] = path
            if namespace in {"kv_store", "fs_store"}:
                continue  # Byte-level wrapping is applied by the application.
            bindings = {}
            if target in _FIELDS:
                field = _FIELDS[target]
                if namespace == "entity_store" and not params.get("hosts"):
                    field = "endpoint"
                actual = _expanded(params.get(field), environment)
                locations = actual if isinstance(actual, list) else [actual]
                if not all(
                    isinstance(location, str) and urlparse(location).hostname == _SERVICES[target]
                    for location in locations
                ):
                    if "at_rest" not in params:
                        raise ValueError(
                            "remote backend requires its operator's at_rest declaration"
                        )
                    continue
                bindings[field] = actual
            if namespace == "markdown_store" and target == "local":
                bindings["root"] = params["root"]
            if namespace == "shadow_index" and target == "sqlite":
                path = f"{_DATA_TARGET}/{namespace}-{name}.db"
                if params.get("db_path") not in {None, ":memory:", path}:
                    raise ValueError("shadow SQLite needs an explicit offline migration")
                params["db_path"] = path
                bindings["db_path"] = path
            if target in {"nano_graphrag", "milvus_graph"}:
                bindings["working_dir"] = params["working_dir"]
            if not bindings:
                raise ValueError(
                    "unknown persistent backend requires an explicit deployment contract"
                )
            params["at_rest"] = _declaration(bindings, reference)
    return overlay, generated, directories


def _contained(root: Path, path: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("encrypted deployment path escapes the verified data root")


def image_user_id(image: str, environment: dict[str, str]) -> int:
    """Read the pinned image's actual configured user; never guess that an image uses root."""
    user = json_loads_user(
        subprocess.run(
            [executable("docker"), "image", "inspect", image, "--format", "{{json .Config.User}}"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    )
    account = user.partition(":")[0]
    if not account:
        return 0
    if account.isdigit():
        return int(account)
    # The image entrypoint is replaced and networking disabled; only resolve its configured UID.
    result = subprocess.run(
        [
            executable("docker"),
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "id",
            image,
            "-u",
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if not result.isdigit():
        raise RuntimeError("cannot resolve the backend image user ID")
    return int(result)


def json_loads_user(value: str) -> str:
    import json

    parsed = json.loads(value)
    if not isinstance(parsed, str):
        raise RuntimeError("invalid Docker image user metadata")
    return parsed


def require_local_daemon(environment: dict[str, str]) -> None:
    """A local encrypted mount is evidence only for the native Docker daemon on that host."""
    endpoint = None if environment.get("DOCKER_CONTEXT") else environment.get("DOCKER_HOST")
    if not endpoint:
        endpoint = json_loads_user(
            subprocess.run(
                [
                    executable("docker"),
                    "context",
                    "inspect",
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        )
    if not endpoint.startswith("unix:///"):
        raise RuntimeError("managed encrypted deployment requires a local Unix Docker endpoint")
    name = json_loads_user(
        subprocess.run(
            [executable("docker"), "info", "--format", "{{json .Name}}"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    )
    if name != socket.gethostname():
        raise RuntimeError("Docker daemon is not on the host whose encrypted mount was verified")


def apply_image_owners(
    base: dict, overlay: dict, directories: dict[Path, int], environment: dict
) -> None:
    for service, spec in overlay["services"].items():
        image = base["services"][service].get("image")
        if not image:
            continue  # Repository application Dockerfiles declare UID 10001 explicitly.
        uid = image_user_id(image, environment)
        for mount in spec["volumes"]:
            path = Path(mount["source"])
            if path in directories:
                directories[path] = uid


def prepare_files(
    location: DeploymentLocation,
    overlay: dict,
    config: dict,
    directories: dict[Path, int],
    key_file: Path | None,
) -> Path:
    root = location.data_root
    profile = location.profile
    for path, owner in directories.items():
        _contained(root, path)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _contained(root, path)
        verify_mount(path)  # Reject bind-mounted plaintext subdirectories as well as symlinks.
        os.chmod(path, 0o700)
        os.chown(path, owner, owner)
    control = root.resolve() / profile / ".control"
    destination = control / "master.key"
    _contained(root, destination)
    if destination.is_symlink():
        raise RuntimeError("deployment key must not be a symlink")
    if destination.exists():
        verify_mount(destination, require_directory=False)
    if key_file is not None:
        if os.name == "posix" and key_file.stat().st_mode & 0o077:
            raise ValueError("application key file must be private (chmod 600)")
        data = key_file.read_text(encoding="ascii").strip()
        if len(data) != 64 or len(bytes.fromhex(data)) != 32:
            raise ValueError("application key file must contain a 32-byte hex key")
        if destination.exists():
            if destination.read_text(encoding="ascii").strip() != data:
                raise RuntimeError("application key differs from the existing deployment key")
        else:
            _contained(root, destination)
            with private_text_file(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY) as stream:
                stream.write(data + "\n")
            os.chown(destination, 10001, 10001)
    elif not destination.is_file():
        raise ValueError("first encrypted startup requires --application-key-file")
    for filename, payload in (("config.yml", config), ("compose.yml", overlay)):
        path = control / filename
        _contained(root, path)
        if path.exists():
            verify_mount(path, require_directory=False)
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW
        with private_text_file(path, flags) as stream:
            yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False)
        os.chmod(path, 0o600)
        os.chown(path, 10001, 10001)
        verify_mount(path, require_directory=False)
    return control / "compose.yml"


def verify_effective_mounts(effective: dict, overlay: dict, root: Path) -> None:
    """Check Compose's actual merged model before starting any service."""
    for service, spec in overlay["services"].items():
        actual = {mount["target"]: mount for mount in effective["services"][service]["volumes"]}
        for expected in spec["volumes"]:
            mount = actual.get(expected["target"], {})
            if mount.get("type") != "bind" or mount.get("source") != expected["source"]:
                raise RuntimeError("Compose did not apply an encrypted bind mount")
            _contained(root, Path(mount["source"]))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("local", "online", "postgres"), required=True)
    parser.add_argument("--enabled", choices=("true", "false"))
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--application-key-file", type=Path)
    parser.add_argument(
        "--fresh-data",
        action="store_true",
        help="acknowledge a new storage layout; existing data is not migrated",
    )
    parser.add_argument("--action", choices=("check", "up", "down", "ps"), default="check")
    args = parser.parse_args()
    profile = ROOT / "deploy/docker" / args.profile
    try:
        environment = load_environment(profile / ".env")
        enabled = encryption_enabled(
            args.enabled
            if args.enabled is not None
            else environment.get("STORAGE_ENCRYPTION_ENABLED", "false")
        )
        environment["STORAGE_ENCRYPTION_ENABLED"] = "true" if enabled else "false"
        command = [
            executable("docker"),
            "compose",
            "--env-file",
            str(profile / ".env"),
            "-f",
            str(profile / "docker-compose.yml"),
        ]
        if enabled:
            root_value = args.data_root or environment.get("STORAGE_ENCRYPTION_ROOT")
            if not root_value:
                raise ValueError("enabled encryption requires an encrypted data root")
            root = Path(root_value).resolve()
            evidence = verify_mount(root)
            require_local_daemon(environment)
            initialized = root / args.profile / ".control/initialized"
            if args.action == "up" and not initialized.exists() and not args.fresh_data:
                raise ValueError(
                    "first encrypted startup requires --fresh-data or offline migration"
                )
            compose = yaml.safe_load((profile / "docker-compose.yml").read_text(encoding="utf-8"))
            config = yaml.safe_load((profile / "config.yml").read_text(encoding="utf-8"))
            location = DeploymentLocation(root, args.profile)
            overlay, generated, directories = build_plan(
                compose,
                config,
                location,
                environment,
                "host-verified-dm-crypt:" + evidence["source"],
            )
            apply_image_owners(compose, overlay, directories, environment)
            key_value = args.application_key_file or environment.get(
                "STORAGE_ENCRYPTION_APPLICATION_KEY_FILE"
            )
            extra = prepare_files(
                location,
                overlay,
                generated,
                directories,
                Path(key_value) if key_value else None,
            )
            command.extend(["-f", str(extra)])
            # Capture the model without displaying credentials in its environment/config.
            import json

            effective = json.loads(
                subprocess.run(
                    [*command, "config", "--format", "json"],
                    cwd=profile,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout
            )
            verify_effective_mounts(effective, overlay, root)
            verify_mount(root)
        if args.action == "check":
            logger.info(
                "Deployment preflight passed; encryption %s.", "enabled" if enabled else "disabled"
            )
            return
        command.append(args.action)
        if args.action == "up":
            command.extend(["-d", "--build"])
        subprocess.run(command, cwd=profile, env=environment, check=True)
        if enabled and args.action == "up" and not initialized.exists():
            _contained(root, initialized)
            fd = os.open(initialized, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Do not echo command arguments or config contents (DSNs may contain credentials).
        parser.exit(
            1, f"Encrypted deployment failed ({type(exc).__name__}); inspect configuration.\n"
        )


if __name__ == "__main__":
    main()
