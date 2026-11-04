"""Exercise real e2fsprogs through a resolved symlink without touching block devices."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from deploy.encryption import volume

pytestmark = pytest.mark.integration


@pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux e2fsprogs")
def test_initialize_formats_ext4_when_mkfs_symlink_resolves_to_mke2fs(tmp_path, monkeypatch):
    try:
        mke2fs = volume.executable("mke2fs")
        for name in ("blkid", "tune2fs"):
            volume.executable(name)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    mkfs = tmp_path / "mkfs.ext4"
    mkfs.symlink_to(mke2fs)
    which = shutil.which
    monkeypatch.setattr(
        volume.shutil,
        "which",
        lambda command, *, path: str(mkfs) if command == "mkfs.ext4" else which(command, path=path),
    )
    assert volume.executable("mkfs.ext4") == mke2fs
    image = tmp_path / "new.img"
    key = tmp_path / "key"
    key.write_bytes(b"test volume key")
    key.chmod(0o600)
    monkeypatch.setattr(volume.os, "geteuid", lambda: 0)
    real_run = volume.run
    commands = []

    def run(argv):
        commands.append(argv)
        # Only the LUKS/device boundary is simulated: all filesystem tools run for real
        # against the newly created regular file. No root, mount or block device is used.
        if argv[0] == "cryptsetup":
            return ""
        source = str(Path("/dev/mapper") / commands[1][-1])
        return real_run([str(image) if arg == source else arg for arg in argv])

    monkeypatch.setattr(volume, "run", run)
    volume.initialize(image, key, 1)
    assert real_run(["blkid", "-p", "-s", "TYPE", "-o", "value", str(image)]).strip() == "ext4"
    header = real_run(["tune2fs", "-l", str(image)])
    attributes = dict(line.split(":", 1) for line in header.splitlines() if ":" in line)
    assert "has_journal" in attributes.get("Filesystem features", "").split()
    assert int(attributes.get("Journal inode", "0").strip()) > 0
    assert commands[-1] == ["cryptsetup", "close", commands[1][-1]]
