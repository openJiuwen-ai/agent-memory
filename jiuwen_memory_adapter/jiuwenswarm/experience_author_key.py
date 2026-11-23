# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provision one stable author key for the local Swarm deployment."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from jiuwen_memory.api import BackendError, ValidationError

_KEY_ENV = "JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY"
_KEY_FILENAME = ".experience-author.key"


def configure_experience_author_key(config: dict[str, Any]) -> None:
    """Fill the built-in SDK appraiser's key at runtime assembly."""
    appraisers = config.get("appraiser", {})
    appraiser = appraisers.get("experience")
    if appraiser == "experience":
        appraiser = appraisers["experience"] = {"target": "experience", "params": {}}
    if not isinstance(appraiser, dict) or appraiser.get("target") != "experience":
        return
    params = appraiser.setdefault("params", {})
    configured = params.get("author_key") or config.get("globals", {}).get("author_key")
    if configured is not None and str(configured).strip():
        params["author_key"] = configured
        return
    params["author_key"] = os.getenv(_KEY_ENV, "").strip() or _load_or_create_key(_key_path())


def _key_path() -> Path:
    workspace = os.getenv("JIUWENSWARM_DATA_DIR", "").strip()
    if workspace:
        return Path(workspace).expanduser() / "config" / _KEY_FILENAME
    home = os.getenv("JIUWENSWARM_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home()
    return root / ".jiuwenswarm" / "config" / _KEY_FILENAME


def _read_key(path: Path) -> str:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError("The local experience author key must be a regular file")
    key = path.read_text(encoding="ascii").strip()
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise ValidationError("The local experience author key file is invalid")
    return key


def _load_or_create_key(path: Path) -> str:
    try:
        try:
            return _read_key(path)
        except FileNotFoundError:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="ascii", dir=path.parent, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(secrets.token_hex(32) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Publish the complete file without replacing a key from a concurrent startup.
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            return _read_key(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (OSError, UnicodeError) as exc:
        raise BackendError(
            f"Unable to load or persist the local experience author key at {path}"
        ) from exc
