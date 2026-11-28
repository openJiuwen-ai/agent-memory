"""DSH profile verification and JiuwenSwarm merge/rollback fidelity."""

from __future__ import annotations

import json

import pytest
import yaml

from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall
from jiuwen_memory_entry.local.hosts.dsh import PACKAGE, DshWriter
from jiuwen_memory_entry.local.hosts.jiuwenswarm import JiuwenSwarmWriter

pytestmark = pytest.mark.unit

INSTALL = HostInstall("dsh-1", "dsh", "dsh-1", "default")


class Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_dsh_merge_installs_into_both_profiles_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path / "dsh"))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if "add" in args:
            return Result(0, "")
        return Result(0, f"{PACKAGE}\n")

    monkeypatch.setattr("subprocess.run", run)
    entries = DshWriter.merge(INSTALL)
    assert entries and DshWriter.verify(INSTALL)
    assert any("desktop" in " ".join(args) for args in commands)
    assert any("dsh-tui" in " ".join(args) for args in commands)


def test_dsh_verify_fails_when_the_host_does_not_list_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path / "dsh"))
    monkeypatch.setattr(
        "subprocess.run", lambda args, **kwargs: Result(0, "other-plugin\n")
    )
    path = tmp_path / "dsh" / "jiuwen-memory.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"install_id": "dsh-1", "package": PACKAGE}),
                    encoding="utf-8")
    assert DshWriter.verify(INSTALL) is False


def test_dsh_rollback_keeps_a_user_edited_record(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path / "dsh"))
    path = tmp_path / "dsh" / "jiuwen-memory.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"install_id": "another"}), encoding="utf-8")
    kept = DshWriter.rollback(
        INSTALL, [EntryRef(path=path, section="plugin", value={"install_id": "dsh-1"})]
    )
    assert kept and path.exists()


def test_swarm_rollback_restores_the_previous_block(tmp_path, monkeypatch):
    config = tmp_path / "swarm.yaml"
    monkeypatch.setenv("JIUWEN_SWARM_CONFIG", str(config))
    config.write_text(
        yaml.safe_dump(
            {"memory": {"builtin": {"jiuwen": {"enabled": True, "install_id": "old"}}}}
        ),
        encoding="utf-8",
    )
    writer = JiuwenSwarmWriter()
    entries = writer.merge(HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default"))
    assert writer.verify(HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default"))
    kept = writer.rollback(HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default"),
                           entries)
    assert kept == []
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["memory"]["builtin"]["jiuwen"] == {"enabled": True, "install_id": "old"}


def test_swarm_json_rollback_restores_the_previous_block(tmp_path, monkeypatch):
    config = tmp_path / "swarm.json"
    monkeypatch.setenv("JIUWEN_SWARM_CONFIG", str(config))
    config.write_text(
        json.dumps({"memory": {"builtin": {"jiuwen": {"install_id": "old"}}}}),
        encoding="utf-8",
    )
    writer = JiuwenSwarmWriter()
    install = HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default")
    entries = writer.merge(install)
    writer.rollback(install, entries)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["memory"]["builtin"]["jiuwen"] == {"install_id": "old"}


def test_swarm_rollback_keeps_a_user_edited_block(tmp_path, monkeypatch):
    config = tmp_path / "swarm.yaml"
    monkeypatch.setenv("JIUWEN_SWARM_CONFIG", str(config))
    config.write_text("memory: {}\n", encoding="utf-8")
    writer = JiuwenSwarmWriter()
    install = HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default")
    entries = writer.merge(install)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["memory"]["builtin"]["jiuwen"]["install_id"] = "user-edited"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    kept = writer.rollback(install, entries)
    assert kept, "a user-edited block must be reported, not deleted"
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["memory"]["builtin"][
        "jiuwen"
    ]["install_id"] == "user-edited"


def test_dsh_rollback_removes_the_plugin_from_both_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path / "dsh"))
    path = tmp_path / "dsh" / "jiuwen-memory.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"install_id": "dsh-1"}), encoding="utf-8")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return Result(0, "")

    monkeypatch.setattr("subprocess.run", run)
    kept = DshWriter.rollback(
        INSTALL, [EntryRef(path=path, section="plugin", value={"install_id": "dsh-1"})]
    )
    assert kept == [] and not path.exists()
    assert sum("remove" in args for args in calls) == len(("desktop", "dsh-tui"))


def test_dsh_rollback_keeps_the_marker_when_removal_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path / "dsh"))
    path = tmp_path / "dsh" / "jiuwen-memory.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"install_id": "dsh-1"}), encoding="utf-8")
    monkeypatch.setattr("subprocess.run", lambda args, **kwargs: Result(1, ""))
    kept = DshWriter.rollback(
        INSTALL, [EntryRef(path=path, section="plugin", value={"install_id": "dsh-1"})]
    )
    assert kept and path.exists()


def test_swarm_rollback_keeps_a_user_edited_project(tmp_path, monkeypatch):
    config = tmp_path / "swarm.yaml"
    monkeypatch.setenv("JIUWEN_SWARM_CONFIG", str(config))
    config.write_text("memory: {}\n", encoding="utf-8")
    writer = JiuwenSwarmWriter()
    install = HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default")
    entries = writer.merge(install)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["memory"]["builtin"]["jiuwen"]["project"] = "user-edited"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    writer.rollback(install, entries)
    block = yaml.safe_load(config.read_text(encoding="utf-8"))["memory"]["builtin"][
        "jiuwen"
    ]
    assert block["project"] == "user-edited"
    assert "connection" not in block
    assert "install_id" not in block
