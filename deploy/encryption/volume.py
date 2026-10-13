# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Linux LUKS2 lifecycle for a new image, and fail-closed encrypted-mount checks.

Never formats an existing file/device. Operators explicitly run init/open/close; application
startup only verifies an already mounted volume. Keys are supplied as protected external files.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def executable(command: str) -> str:
    """Resolve tools from system directories, never a profile's PATH or the working directory."""
    search_path = (
        os.pathsep.join(
            ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
        )
        if _linux()
        else os.defpath
    )
    resolved = shutil.which(command, path=search_path)
    if resolved is None:
        raise RuntimeError(f"required executable is not installed in system directories: {command}")
    return str(Path(resolved).resolve(strict=True))


def run(argv: list[str]) -> str:
    return subprocess.run(
        [executable(argv[0]), *argv[1:]],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def _linux() -> bool:
    return sys.platform == "linux"


def _encrypted_tree(node: dict) -> bool:
    if node.get("type") == "crypt":
        return True
    children = node.get("children", [])
    return bool(children) and all(_encrypted_tree(child) for child in children)


def verify_mount(path: Path, *, require_directory: bool = True) -> dict[str, str]:
    if not _linux():
        raise RuntimeError("encrypted-volume deployment requires a Linux Docker host")
    resolved = path.resolve(strict=True)
    if require_directory and not resolved.is_dir():
        raise RuntimeError("encrypted data root must be a mounted directory")
    response = json.loads(
        run(["findmnt", "--json", "--target", str(resolved), "--output", "SOURCE,TARGET,FSTYPE"])
    )
    mounts = response.get("filesystems", [])
    if len(mounts) != 1:
        raise RuntimeError("cannot determine the data root filesystem")
    mount = mounts[0]
    if mount.get("fstype") != "ext4":
        raise RuntimeError("managed encrypted volumes require ext4 with an internal journal")
    source = mount.get("source", "").split("[", 1)[0]
    if not source.startswith("/dev/"):
        raise RuntimeError("data root is not backed by an encrypted block device")
    devices = json.loads(
        run(["lsblk", "--json", "--inverse", "--paths", "--output", "NAME,TYPE", source])
    ).get("blockdevices", [])
    if not devices or not all(_encrypted_tree(device) for device in devices):
        raise RuntimeError("data root has an unencrypted block-device path")
    attributes = {}
    for line in run(["tune2fs", "-l", source]).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            attributes[key.strip().lower()] = value.strip()
    features = attributes.get("filesystem features", "").split()
    if not features:
        raise RuntimeError("cannot inspect ext4 encryption coverage")
    if "has_journal" in features:
        inode = attributes.get("journal inode", "")
        if not inode.isdigit() or int(inode) == 0:
            raise RuntimeError("external ext4 journals require a separate encryption contract")
    return {"source": source, "target": mount["target"], "fstype": mount["fstype"]}


def _name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", value):
        raise ValueError("mapper name must contain 1-48 letters, numbers, hyphens or underscores")
    return value


def _key(path: Path, image: Path) -> Path:
    key = path.resolve(strict=True)
    if not key.is_file() or key == image.resolve():
        raise ValueError("supply a separate existing key file")
    if key.stat().st_mode & 0o077:
        raise ValueError("key file must be private (chmod 600)")
    return key


def initialize(image: Path, key_file: Path, size_gib: int) -> None:
    """Create and format only a newly exclusively-created image; retain it on failure."""
    if not _linux() or os.geteuid() != 0:
        raise RuntimeError("LUKS initialization requires root on Linux")
    if not 1 <= size_gib <= 1024:
        raise ValueError("size-gib must be between 1 and 1024")
    key = _key(key_file, image)
    fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.ftruncate(fd, size_gib * 1024**3)
    finally:
        os.close(fd)
    mapper = "agent-memory-init-" + uuid.uuid4().hex[:16]
    opened = False
    try:
        run(
            [
                "cryptsetup",
                "luksFormat",
                "--batch-mode",
                "--type",
                "luks2",
                "--key-file",
                str(key),
                str(image.resolve()),
            ]
        )
        run(["cryptsetup", "open", "--key-file", str(key), str(image.resolve()), mapper])
        opened = True
        run(["mkfs.ext4", "-m", "0", str(Path("/dev/mapper") / mapper)])
    finally:
        if opened:
            run(["cryptsetup", "close", mapper])


def open_volume(image: Path, key_file: Path, mountpoint: Path, mapper: str) -> None:
    if not _linux() or os.geteuid() != 0:
        raise RuntimeError("opening LUKS requires root on Linux")
    mapper = _name(mapper)
    image = image.resolve(strict=True)
    key = _key(key_file, image)
    mountpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
    if mountpoint.is_symlink() or any(mountpoint.iterdir()):
        raise RuntimeError("mountpoint must be an empty directory")
    run(["cryptsetup", "isLuks", str(image)])
    run(["cryptsetup", "open", "--key-file", str(key), str(image), mapper])
    mounted = False
    try:
        run(["mount", str(Path("/dev/mapper") / mapper), str(mountpoint.resolve())])
        mounted = True
        verify_mount(mountpoint)
    except Exception:
        if mounted:
            run(["umount", str(mountpoint.resolve())])
        run(["cryptsetup", "close", mapper])
        raise


def close_volume(mountpoint: Path, mapper: str) -> None:
    mapper = _name(mapper)
    evidence = verify_mount(mountpoint)
    if Path(evidence["target"]).resolve() != mountpoint.resolve():
        raise RuntimeError("close requires the exact encrypted mountpoint")
    if Path(evidence["source"]).resolve() != Path("/dev/mapper", mapper).resolve():
        raise RuntimeError("mountpoint is not backed by the requested mapper")
    run(["umount", str(mountpoint.resolve())])
    run(["cryptsetup", "close", mapper])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--image", type=Path, required=True)
    init.add_argument("--key-file", type=Path, required=True)
    init.add_argument("--size-gib", type=int, default=20)
    mount = sub.add_parser("open")
    mount.add_argument("--image", type=Path, required=True)
    mount.add_argument("--key-file", type=Path, required=True)
    mount.add_argument("--mountpoint", type=Path, required=True)
    mount.add_argument("--mapper", default="agent-memory-data")
    close = sub.add_parser("close")
    close.add_argument("--mountpoint", type=Path, required=True)
    close.add_argument("--mapper", default="agent-memory-data")
    check = sub.add_parser("check")
    check.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.image, args.key_file, args.size_gib)
        elif args.command == "open":
            open_volume(args.image, args.key_file, args.mountpoint, args.mapper)
        elif args.command == "close":
            close_volume(args.mountpoint, args.mapper)
        else:
            verify_mount(args.path)
        logger.info("Encrypted volume operation completed.")
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Encrypted volume operation failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
