"""Managed volumes must be ext4 with a verified internal journal before success."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from deploy.encryption import volume

pytestmark = pytest.mark.unit

INTERNAL_JOURNAL = "Filesystem features: has_journal extent\nJournal inode: 8\n"


@pytest.fixture
def initialization(tmp_path, monkeypatch):
    image = tmp_path / "new.img"
    key = tmp_path / "key"
    key.write_bytes(b"test key")
    monkeypatch.setattr(volume, "_linux", lambda: True)
    monkeypatch.setattr(volume.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(volume, "_key", lambda *args: key)
    monkeypatch.setattr(volume.os, "ftruncate", lambda *args: None)
    return image, key


@pytest.mark.parametrize(
    "filesystem,header,error",
    [
        ("ext4\n", INTERNAL_JOURNAL, None),
        ("ext2\n", INTERNAL_JOURNAL, "require ext4"),
        ("", INTERNAL_JOURNAL, "require ext4"),
        ("ext4\n", "Filesystem features: extent\n", "require ext4"),
        ("ext4\n", "", "cannot inspect"),
        ("ext4\n", "Filesystem features: has_journal extent\n", "external ext4 journals"),
        (
            "ext4\n",
            "Filesystem features: has_journal extent\nJournal inode: 0\n",
            "external ext4 journals",
        ),
    ],
)
def test_initialize_checks_format_before_closing_mapper(
    initialization, monkeypatch, filesystem, header, error
):
    image, key = initialization
    commands = []

    def run(argv):
        commands.append(argv)
        return {"blkid": filesystem, "tune2fs": header}.get(argv[0], "")

    monkeypatch.setattr(volume, "run", run)
    if error:
        with pytest.raises(RuntimeError, match=error):
            volume.initialize(image, key, 1)
    else:
        volume.initialize(image, key, 1)
    mapper = commands[1][-1]
    source = str(Path("/dev/mapper") / mapper)
    assert commands[2] == ["mkfs.ext4", "-t", "ext4", "-j", "-m", "0", source]
    assert commands[3] == ["blkid", "-p", "-s", "TYPE", "-o", "value", source]
    assert commands[-1] == ["cryptsetup", "close", mapper]
    assert image.exists(), "Failed initialization must retain the image for inspection"
    assert any(argv[0] == "tune2fs" for argv in commands) == (filesystem.strip() == "ext4")


@pytest.mark.parametrize("failed_tool", ["mkfs.ext4", "blkid", "tune2fs"])
def test_initialize_closes_mapper_when_formatting_or_inspection_fails(
    initialization, monkeypatch, failed_tool
):
    image, key = initialization
    commands = []

    def run(argv):
        commands.append(argv)
        if argv[0] == failed_tool:
            raise subprocess.CalledProcessError(1, argv)
        return {"blkid": "ext4\n", "tune2fs": INTERNAL_JOURNAL}.get(argv[0], "")

    monkeypatch.setattr(volume, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        volume.initialize(image, key, 1)
    assert commands[-1] == ["cryptsetup", "close", commands[1][-1]]
    assert image.exists()


def test_failed_initialization_does_not_report_cli_success(initialization, monkeypatch, caplog):
    image, key = initialization
    commands = []

    def run(argv):
        commands.append(argv)
        return "ext2\n" if argv[0] == "blkid" else ""

    monkeypatch.setattr(volume, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["volume", "init", "--image", str(image), "--key-file", str(key), "--size-gib", "1"],
    )
    # Capture argparse's exit contract without terminating the test process.
    exit_error = RuntimeError("CLI exit requested")
    exit_parser = Mock(side_effect=exit_error)
    monkeypatch.setattr(volume.argparse.ArgumentParser, "exit", exit_parser)
    with caplog.at_level(logging.INFO, logger=volume.__name__):
        with pytest.raises(RuntimeError, match="CLI exit requested") as failure:
            volume.main()
    assert failure.value is exit_error
    exit_parser.assert_called_once_with(
        1,
        "Encrypted volume operation failed: RuntimeError: "
        "managed encrypted volumes require ext4 with an internal journal\n",
    )
    assert "Encrypted volume operation completed." not in caplog.text
    assert commands[-1] == ["cryptsetup", "close", commands[1][-1]]


@pytest.mark.parametrize(
    "fstype,header,error",
    [
        ("ext2", INTERNAL_JOURNAL, "require ext4"),
        ("ext4", "Filesystem features: extent\nJournal inode: 8\n", "require ext4"),
        ("ext4", "", "cannot inspect"),
        ("ext4", "Filesystem features: has_journal extent\n", "external ext4 journals"),
        (
            "ext4",
            "Filesystem features: has_journal extent\nJournal inode: invalid\n",
            "external ext4 journals",
        ),
        (
            "ext4",
            "Filesystem features: has_journal extent\nJournal inode: 0\n",
            "external ext4 journals",
        ),
    ],
)
def test_mount_rejects_unverified_filesystem_and_journal(
    tmp_path, monkeypatch, fstype, header, error
):
    monkeypatch.setattr(volume, "_linux", lambda: True)

    def run(argv):
        if argv[0] == "findmnt":
            return json.dumps(
                {
                    "filesystems": [
                        {"source": "/dev/mapper/test", "target": str(tmp_path), "fstype": fstype}
                    ]
                }
            )
        if argv[0] == "lsblk":
            return json.dumps({"blockdevices": [{"type": "crypt"}]})
        return header

    monkeypatch.setattr(volume, "run", run)
    with pytest.raises(RuntimeError, match=error):
        volume.verify_mount(tmp_path)


def test_open_cleans_up_mount_and_mapper_when_journal_verification_fails(
    initialization, tmp_path, monkeypatch
):
    image, key = initialization
    image.touch()
    commands = []
    monkeypatch.setattr(volume, "run", lambda argv: commands.append(argv))

    def reject(path):
        raise RuntimeError("managed encrypted volumes require ext4 with an internal journal")

    monkeypatch.setattr(volume, "verify_mount", reject)
    mountpoint = tmp_path / "mount"
    with pytest.raises(RuntimeError, match="require ext4"):
        volume.open_volume(image, key, mountpoint, "test")
    assert commands[-2:] == [
        ["umount", str(mountpoint.resolve())],
        ["cryptsetup", "close", "test"],
    ]
