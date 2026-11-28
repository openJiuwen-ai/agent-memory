# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Data home layout and first-time initialization for the local service (F17 D1).

The service identity is the canonical path of the data directory: one service per
user per data directory, independent of which host configuration started it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jiuwen_memory.api import ConflictError, ValidationError

_TEMPLATE = Path(__file__).with_name("service_template.yaml")
_SERVICE_FILE_MODE = 0o600
# The data directory holds plaintext memory content and the local turn store; keep the
# tree owner-only so a default umask cannot make user prompts readable by others.
_PRIVATE_DIR_MODE = 0o700


@dataclass(frozen=True)
class MemoryHome:
    """Layout of ``~/.jiuwen-memory`` (or ``JIUWEN_MEMORY_HOME``)."""

    root: Path

    @property
    def service_config(self) -> Path:
        return self.root / "service.yaml"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    @property
    def installs_dir(self) -> Path:
        return self.root / "installs"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def discovery_file(self) -> Path:
        return self.run_dir / "service.json"

    @property
    def start_lock(self) -> Path:
        return self.run_dir / "start.lock"

    @property
    def interpreter_file(self) -> Path:
        return self.run_dir / "interpreter"

    @property
    def canonical(self) -> str:
        """Canonical data-directory path; the service identity used by discovery."""
        return os.path.normcase(str(self.root.resolve()))


@dataclass(frozen=True)
class ModelSettings:
    """Model endpoints written into ``service.yaml``.

    ``*_api_key_ref`` values are references, never secrets: ``env:NAME`` (or a bare
    variable name) renders as ``${NAME}`` and is resolved from the service process
    environment. Empty means "no key configured".
    """

    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key_ref: str = ""
    embedder_base_url: str = ""
    embedder_model: str = ""
    embedder_api_key_ref: str = ""


def resolve_home(env: Mapping[str, str] | None = None) -> MemoryHome:
    """Resolve the data directory from the environment (``JIUWEN_MEMORY_HOME`` wins)."""
    env = os.environ if env is None else env
    override = (env.get("JIUWEN_MEMORY_HOME") or "").strip()
    if override:
        return MemoryHome(Path(override).expanduser())
    if os.name == "nt":
        local = (env.get("LOCALAPPDATA") or "").strip()
        if not local:
            raise ValidationError(
                "LOCALAPPDATA is unavailable; set JIUWEN_MEMORY_HOME to a data directory"
            )
        return MemoryHome(Path(local) / "jiuwen-memory")
    return MemoryHome(Path.home() / ".jiuwen-memory")


def _yaml_scalar(value: str) -> str:
    """Single-quoted YAML scalar: no escape processing, so Windows paths stay intact."""
    return "'" + value.replace("'", "''") + "'"


def _secret_ref(ref: str) -> str:
    """Render a key reference as an environment placeholder, or a literal reference."""
    text = (ref or "").strip()
    if not text:
        return "''"
    name = text[4:] if text.startswith("env:") else text
    if name.replace("_", "a").isalnum() and name[:1].isalpha():
        return _yaml_scalar("${" + name + "}")
    return _yaml_scalar(text)


def _write_private(path: Path, text: str) -> None:
    """Atomically write a service-owned file with owner-only permission."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, _SERVICE_FILE_MODE)
    os.replace(temporary, path)


def init_home(
    home: MemoryHome,
    *,
    document_root: Path,
    model: ModelSettings,
    org: str = "local",
    space: str = "default",
    force: bool = False,
) -> None:
    """Create the data directories and render ``service.yaml`` from the template.

    Existing configuration is never overwritten silently: re-running ``init`` after a
    user has edited the file raises :class:`ConflictError` unless ``force`` is set.
    """
    if home.service_config.exists() and not force:
        raise ConflictError(
            message=f"service configuration already exists: {home.service_config}"
        )
    for directory in (home.root, home.run_dir, home.installs_dir, home.data_dir):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, _PRIVATE_DIR_MODE)
    document_root = Path(document_root).expanduser().resolve()
    document_root.mkdir(parents=True, exist_ok=True)

    values = {
        "org": _yaml_scalar(org),
        "space": _yaml_scalar(space),
        "document_root": _yaml_scalar(str(document_root)),
        "kv_db": _yaml_scalar(str(home.data_dir / "kv.db")),
        "registry_db": _yaml_scalar(str(home.data_dir / "registry.db")),
        "tokens_db": _yaml_scalar(str(home.data_dir / "tokens.db")),
        "permissions_db": _yaml_scalar(str(home.data_dir / "permissions.db")),
        "audit_db": _yaml_scalar(str(home.data_dir / "audit.db")),
        "shadow_db": _yaml_scalar(str(home.data_dir / "shadow.db")),
        "llm_base_url": _yaml_scalar(model.llm_base_url),
        "llm_model": _yaml_scalar(model.llm_model),
        "llm_api_key": _secret_ref(model.llm_api_key_ref),
        "embedder_base_url": _yaml_scalar(model.embedder_base_url),
        "embedder_model": _yaml_scalar(model.embedder_model),
        "embedder_api_key": _secret_ref(model.embedder_api_key_ref),
    }
    rendered = _TEMPLATE.read_text(encoding="utf-8")
    for name, replacement in values.items():
        rendered = rendered.replace("{{" + name + "}}", replacement)
    if "{{" in rendered:
        raise ValidationError("service template still contains unresolved placeholders")
    _write_private(home.service_config, rendered)
    _write_private(home.interpreter_file, sys.executable + "\n")
