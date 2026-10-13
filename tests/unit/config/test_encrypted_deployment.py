"""Encrypted deployment: all persistent mounts, real-device evidence and no plaintext fallback."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from deploy.encryption import compose, volume
from jiuwen_memory.api.memory_api_impl.assembly import _register_all
from jiuwen_memory.config import Config
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.config.encryption import deployment_bindings

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def registered_plugins(monkeypatch):
    _register_all()
    monkeypatch.setattr(compose, "executable", lambda name: str(ROOT / "system-tools" / name))


def _plan(profile, root, environment=None):
    path = ROOT / "deploy/docker" / profile
    base = yaml.safe_load((path / "docker-compose.yml").read_text(encoding="utf-8"))
    config = yaml.safe_load((path / "config.yml").read_text(encoding="utf-8"))
    result = compose.build_plan(
        base,
        config,
        compose.DeploymentLocation(root, profile),
        environment or {},
        "host:dm-crypt-test",
    )
    return base, config, result


@pytest.mark.parametrize("profile", ["local", "online", "postgres"])
def test_all_persistent_base_volumes_are_replaced_with_encrypted_binds(profile, tmp_path):
    base, original, (overlay, generated, directories) = _plan(profile, tmp_path)
    for service, spec in base["services"].items():
        replaced = {entry["target"]: entry for entry in overlay["services"][service]["volumes"]}
        for raw in spec.get("volumes", []):
            source, target = raw.split(":")[:2]
            if source in base["volumes"]:
                mount = replaced[target]
                assert mount["type"] == "bind"
                assert Path(mount["source"]).is_relative_to(tmp_path)
                assert mount["bind"]["create_host_path"] is False
                assert Path(mount["source"]) in directories
    assert generated["memory_api"]["globals"]["storage_encryption_enabled"] is True
    assert "at_rest" not in original["memory_api"].get("cryptography", {})
    ctx = default_context().merged(
        Config.from_yaml_str(yaml.safe_dump(generated["memory_api"])).context()
    )
    bindings = deployment_bindings(ctx)
    assert "fulltext_store.hosts" in bindings
    assert (
        "vector_store.dsn" in bindings if profile == "postgres" else "vector_store.uri" in bindings
    )
    for namespace in ("audit", "permission"):
        assert bindings[f"{namespace}.db_path"].startswith("/var/lib/agent-memory/")


def test_milvus_dependencies_and_entity_index_share_protected_storage(tmp_path):
    _, _, (overlay, config, _) = _plan("online", tmp_path)
    assert {"milvus", "etcd", "minio", "elasticsearch", "redis"} <= set(overlay["services"])
    declaration = config["memory_api"]["entity_store"]["default"]["params"]["at_rest"]
    assert declaration["method"] == "volume"
    assert declaration["bindings"]["hosts"] == "http://elasticsearch:9200"


def test_sqlite_authorization_stores_receive_verified_application_paths(tmp_path):
    path = ROOT / "deploy/docker/online"
    base = yaml.safe_load((path / "docker-compose.yml").read_text(encoding="utf-8"))
    config = yaml.safe_load((path / "config.yml").read_text(encoding="utf-8"))
    for namespace in ("grant_store", "delegation_store"):
        config["memory_api"][namespace] = {"default": {"target": "sqlite"}}
    _, generated, _ = compose.build_plan(
        base, config, compose.DeploymentLocation(tmp_path, "online"), {}, "host:verified"
    )
    ctx = default_context().merged(
        Config.from_yaml_str(yaml.safe_dump(generated["memory_api"])).context()
    )
    bindings = deployment_bindings(ctx)
    for namespace in ("grant_store", "delegation_store"):
        assert bindings[f"{namespace}.db_path"] == f"/var/lib/agent-memory/{namespace}-default.db"


def test_remote_backend_does_not_inherit_local_volume_evidence(tmp_path):
    with pytest.raises(ValueError, match="remote backend"):
        _plan("online", tmp_path, {"MILVUS_URI": "http://remote.example:19530"})


def test_existing_local_files_require_explicit_migration(tmp_path):
    path = ROOT / "deploy/docker/online"
    base = yaml.safe_load((path / "docker-compose.yml").read_text(encoding="utf-8"))
    config = yaml.safe_load((path / "config.yml").read_text(encoding="utf-8"))
    config["memory_api"]["graph_store"] = {
        "default": {
            "target": "nano_graphrag",
            "params": {"working_dir": "/plaintext/graph"},
        }
    }
    with pytest.raises(ValueError, match="migration"):
        compose.build_plan(base, config, compose.DeploymentLocation(tmp_path, "online"), {}, "test")


@pytest.mark.parametrize(
    "tree,accepted",
    [
        ({"type": "crypt"}, True),
        ({"type": "lvm", "children": [{"type": "crypt"}]}, True),
        ({"type": "disk"}, False),
        ({"type": "lvm", "children": [{"type": "crypt"}, {"type": "disk"}]}, False),
        ({"type": "lvm", "children": []}, False),
    ],
)
def test_mount_verification_requires_encryption_on_every_block_device_path(
    tree,
    accepted,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(volume, "_linux", lambda: True)
    responses = iter(
        [
            {
                "filesystems": [
                    {"source": "/dev/mapper/test", "target": str(tmp_path), "fstype": "ext4"}
                ]
            },
            {"blockdevices": [tree]},
        ]
    )
    monkeypatch.setattr(
        volume,
        "run",
        lambda args: (
            "Filesystem features: has_journal extent\nJournal inode: 8\n"
            if args[0] == "tune2fs"
            else json.dumps(next(responses))
        ),
    )
    if accepted:
        assert volume.verify_mount(tmp_path)["source"] == "/dev/mapper/test"
    else:
        with pytest.raises(RuntimeError):
            volume.verify_mount(tmp_path)


def test_network_filesystem_cannot_claim_managed_volume_encryption(tmp_path, monkeypatch):
    monkeypatch.setattr(volume, "_linux", lambda: True)
    monkeypatch.setattr(
        volume,
        "run",
        lambda args: json.dumps(
            {
                "filesystems": [
                    {
                        "source": "server:/data",
                        "target": str(tmp_path),
                        "fstype": "nfs",
                    }
                ]
            }
        ),
    )
    with pytest.raises(RuntimeError):
        volume.verify_mount(tmp_path)


def test_effective_compose_mounts_are_checked_instead_of_trusting_overlay(tmp_path):
    _, _, (overlay, _, _) = _plan("postgres", tmp_path)
    effective = {
        "services": {
            name: {"volumes": copy.deepcopy(spec["volumes"])}
            for name, spec in overlay["services"].items()
        }
    }
    compose.verify_effective_mounts(effective, overlay, tmp_path)
    effective["services"]["postgres"]["volumes"][0] = {
        "type": "volume",
        "source": "postgres-data",
        "target": "/var/lib/postgresql/data",
    }
    with pytest.raises(RuntimeError):
        compose.verify_effective_mounts(effective, overlay, tmp_path)


def test_existing_volume_image_is_never_formatted(tmp_path, monkeypatch):
    monkeypatch.setattr(volume, "_linux", lambda: True)
    monkeypatch.setattr(volume.os, "geteuid", lambda: 0, raising=False)
    image = tmp_path / "existing.img"
    image.write_bytes(b"existing data")
    key = tmp_path / "key"
    key.write_bytes(b"test-key")
    monkeypatch.setattr(volume, "_key", lambda *args: key)
    commands = []
    monkeypatch.setattr(volume, "run", lambda args: commands.append(args))
    with pytest.raises(FileExistsError):
        volume.initialize(image, key, 1)
    assert image.read_bytes() == b"existing data"
    assert commands == []


def test_volume_open_does_not_cover_an_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(volume, "_linux", lambda: True)
    monkeypatch.setattr(volume.os, "geteuid", lambda: 0, raising=False)
    image = tmp_path / "image"
    image.touch()
    key = tmp_path / "key"
    key.touch()
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "existing").write_text("data")
    monkeypatch.setattr(volume, "_key", lambda *args: key)
    commands = []
    monkeypatch.setattr(volume, "run", lambda args: commands.append(args))
    with pytest.raises(RuntimeError, match="empty"):
        volume.open_volume(image, key, mount, "test")
    assert commands == []


def test_key_file_is_never_silently_replaced(tmp_path, monkeypatch):
    _, _, (overlay, config, directories) = _plan("online", tmp_path)
    control = tmp_path / "online/.control"
    control.mkdir(parents=True)
    (control / "master.key").write_text("11" * 32)
    (control / "master.key").chmod(0o600)
    source = tmp_path / "new.key"
    source.write_text("22" * 32)
    source.chmod(0o600)
    monkeypatch.setattr(compose, "verify_mount", lambda path, **kwargs: {})
    monkeypatch.setattr(compose.os, "chown", lambda *args: None, raising=False)
    with pytest.raises(RuntimeError, match="differs"):
        compose.prepare_files(
            compose.DeploymentLocation(tmp_path, "online"), overlay, config, directories, source
        )
    assert (control / "master.key").read_text() == "11" * 32


def test_plaintext_submount_prevents_file_preparation(tmp_path, monkeypatch):
    _, _, (overlay, config, directories) = _plan("online", tmp_path)
    monkeypatch.setattr(compose.os, "chown", lambda *args: None, raising=False)

    def unprotected(path):
        raise RuntimeError("unencrypted")

    monkeypatch.setattr(compose, "verify_mount", unprotected)
    with pytest.raises(RuntimeError, match="unencrypted"):
        compose.prepare_files(
            compose.DeploymentLocation(tmp_path, "online"), overlay, config, directories, None
        )


@pytest.mark.parametrize("fstype,journal", [("btrfs", "8"), ("ext4", "0")])
def test_unverified_filesystem_or_external_journal_is_rejected(
    tmp_path, monkeypatch, fstype, journal
):
    monkeypatch.setattr(volume, "_linux", lambda: True)

    def response(args):
        if args[0] == "findmnt":
            return json.dumps(
                {
                    "filesystems": [
                        {"source": "/dev/mapper/test", "target": str(tmp_path), "fstype": fstype}
                    ]
                }
            )
        if args[0] == "lsblk":
            return json.dumps({"blockdevices": [{"type": "crypt"}]})
        return f"Filesystem features: has_journal extent\nJournal inode: {journal}\n"

    monkeypatch.setattr(volume, "run", response)
    with pytest.raises(RuntimeError):
        volume.verify_mount(tmp_path)


def test_file_bind_mounts_cannot_bypass_directory_checks(tmp_path, monkeypatch):
    _, _, (overlay, config, directories) = _plan("online", tmp_path)
    control = tmp_path / "online/.control"
    control.mkdir(parents=True)
    (control / "master.key").write_text("11" * 32)
    monkeypatch.setattr(compose.os, "chown", lambda *args: None, raising=False)

    def check(path, **kwargs):
        if not kwargs.get("require_directory", True):
            raise RuntimeError("unencrypted file")
        return {}

    monkeypatch.setattr(compose, "verify_mount", check)
    with pytest.raises(RuntimeError, match="unencrypted file"):
        compose.prepare_files(
            compose.DeploymentLocation(tmp_path, "online"), overlay, config, directories, None
        )


def test_instance_names_cannot_escape_the_application_data_directory(tmp_path):
    path = ROOT / "deploy/docker/online"
    base = yaml.safe_load((path / "docker-compose.yml").read_text(encoding="utf-8"))
    config = yaml.safe_load((path / "config.yml").read_text(encoding="utf-8"))
    config["memory_api"]["graph_store"] = {"../../../escape": {"target": "nano_graphrag"}}
    with pytest.raises(ValueError, match="directory components"):
        compose.build_plan(base, config, compose.DeploymentLocation(tmp_path, "online"), {}, "test")


@pytest.mark.parametrize("metadata,uid", [('""', 0), ('"1000:0"', 1000), ('"65532"', 65532)])
def test_backend_image_user_is_read_instead_of_assuming_root(metadata, uid, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        compose.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=metadata)
    )
    assert compose.image_user_id("pinned:image", {}) == uid


def test_named_image_user_is_resolved_without_running_the_backend_entrypoint(monkeypatch):
    from types import SimpleNamespace

    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout='"minio"' if args[1] == "image" else "1000\n")

    monkeypatch.setattr(compose.subprocess, "run", run)
    assert compose.image_user_id("pinned:minio", {}) == 1000
    assert commands[1][1:7] == ["run", "--rm", "--network", "none", "--entrypoint", "id"]


@pytest.mark.parametrize("endpoint", ["tcp://remote:2376", "ssh://remote", "npipe://docker"])
def test_remote_docker_endpoint_cannot_borrow_local_volume_evidence(endpoint):
    with pytest.raises(RuntimeError, match="local Unix"):
        compose.require_local_daemon({"DOCKER_HOST": endpoint})


def test_unix_socket_proxy_to_a_different_host_is_rejected(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        compose.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout='"remote"')
    )
    monkeypatch.setattr(compose.socket, "gethostname", lambda: "local")
    with pytest.raises(RuntimeError, match="not on the host"):
        compose.require_local_daemon({"DOCKER_HOST": "unix:///var/run/docker.sock"})


def test_native_local_docker_daemon_passes(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        compose.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout='"local"')
    )
    monkeypatch.setattr(compose.socket, "gethostname", lambda: "local")
    compose.require_local_daemon({"DOCKER_HOST": "unix:///var/run/docker.sock"})


def test_docker_context_override_cannot_be_hidden_by_a_local_host_variable(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        compose.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout='"ssh://remote"')
    )
    with pytest.raises(RuntimeError, match="local Unix"):
        compose.require_local_daemon(
            {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": "remote-context"}
        )


@pytest.mark.parametrize("existing", [False, True])
def test_document_backend_paths_use_verified_application_bind_or_require_migration(
    tmp_path, existing
):
    path = ROOT / "deploy/docker/local"
    base = yaml.safe_load((path / "docker-compose.yml").read_text(encoding="utf-8"))
    config = yaml.safe_load((path / "config.yml").read_text(encoding="utf-8"))
    config["memory_api"]["markdown_store"] = {"default": {"target": "local"}}
    config["memory_api"]["shadow_index"] = {"default": {"target": "sqlite"}}
    if existing:
        config["memory_api"]["shadow_index"]["default"]["params"] = {
            "db_path": "/plaintext/shadow.db"
        }
        with pytest.raises(ValueError, match="migration"):
            compose.build_plan(
                base,
                config,
                compose.DeploymentLocation(tmp_path, "local"),
                {},
                "host:verified-volume",
            )
        return
    _, generated, _ = compose.build_plan(
        base, config, compose.DeploymentLocation(tmp_path, "local"), {}, "host:verified-volume"
    )
    ctx = default_context().merged(
        Config.from_yaml_str(yaml.safe_dump(generated["memory_api"])).context()
    )
    guarded = deployment_bindings(ctx)
    assert guarded["markdown_store.root"] == "/var/lib/agent-memory/markdown_store-default"
    assert guarded["shadow_index.db_path"] == "/var/lib/agent-memory/shadow_index-default.db"


@pytest.mark.parametrize("stage", ["wrap", "write", "success"])
def test_private_control_file_always_closes_its_descriptor(tmp_path, monkeypatch, stage):
    import os

    closed = []
    real_close = os.close

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(compose.os, "close", close)
    if stage == "wrap":

        def fail_wrap(*args, **kwargs):
            raise OSError("text wrapper failed")

        monkeypatch.setattr(compose.os, "fdopen", fail_wrap)
    path = tmp_path / "control"

    def write():
        with compose.private_text_file(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY) as stream:
            stream.write("private control data")
            if stage == "write":
                raise OSError("write failed")

    if stage == "success":
        write()
        assert path.read_text(encoding="utf-8") == "private control data"
    else:
        with pytest.raises(OSError):
            write()
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])


def test_tool_resolution_uses_system_directories_and_an_absolute_path(tmp_path, monkeypatch):
    binary = tmp_path / "docker"
    binary.touch()
    searches = []

    def which(command, *, path):
        searches.append((command, path))
        return str(binary)

    monkeypatch.setattr(volume, "_linux", lambda: True)
    monkeypatch.setattr(volume.shutil, "which", which)
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted-profile"))
    assert Path(volume.executable("docker")) == binary.resolve()
    assert "untrusted-profile" not in searches[0][1]
    assert "/usr/sbin" in searches[0][1]


def test_missing_system_tool_fails_before_launch(monkeypatch):
    monkeypatch.setattr(volume.shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="system directories"):
        volume.executable("docker")


def test_volume_command_executes_the_resolved_absolute_binary(monkeypatch):
    from types import SimpleNamespace

    commands = []
    monkeypatch.setattr(volume, "executable", lambda name: f"/usr/sbin/{name}")

    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(volume.subprocess, "run", run)
    assert volume.run(["cryptsetup", "isLuks", "/encrypted.img"]) == "ok"
    assert commands == [["/usr/sbin/cryptsetup", "isLuks", "/encrypted.img"]]


def test_deployment_preflight_reports_success_through_logging(monkeypatch, caplog):
    import logging
    import sys

    monkeypatch.setattr(sys, "argv", ["compose", "--profile", "local", "--enabled", "false"])
    monkeypatch.setattr(compose, "load_environment", lambda path: {})
    with caplog.at_level(logging.INFO, logger=compose.__name__):
        compose.main()
    assert "Deployment preflight passed; encryption disabled." in caplog.text


def test_volume_check_reports_success_through_logging(monkeypatch, caplog):
    import logging
    import sys

    monkeypatch.setattr(sys, "argv", ["volume", "check", "/encrypted"])
    monkeypatch.setattr(volume, "verify_mount", lambda path: {})
    with caplog.at_level(logging.INFO, logger=volume.__name__):
        volume.main()
    assert "Encrypted volume operation completed." in caplog.text
