"""Public configuration inspection for process-local SDK continuity sharing."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.types import SecretValue
from jiuwen_memory.config import ComponentConfig, Config, default_context


def sdk_continuity_runtime_identity(config: dict) -> tuple[str, str]:
    """Return a canonical Registry path and effective configuration digest.

    Inspect configuration only: no runtime, store or lock is created. Secret values
    participate in the digest but are never returned. This does not grant access.
    """
    ctx = default_context().merged(Config.from_dict(config).context())
    spec = ctx.lookup("continuity", "default")
    options = ComponentConfig(spec.params, ctx)
    if options.get("mode") != "continuity" or not options.get("single_writer"):
        raise ValidationError("SDK continuity requires mode=continuity and single_writer=true")
    if options.get("allow_ephemeral", False):
        raise ValidationError("SDK continuity requires a durable Registry")
    name = options.get("registry_store", "default")
    store = ctx.lookup("kv_store", name)
    params = ComponentConfig(store.params, ctx)
    path = params.get("db_path", "agent_memory.db")
    if store.target != "sqlite" or params.get("fixed_path") is not True:
        raise ValidationError("SDK continuity requires a fixed_path SQLite Registry")
    if not isinstance(path, str) or not path or path == ":memory:":
        raise ValidationError("SDK continuity requires a fixed Registry file")
    key = os.path.normcase(str(Path(path).resolve()))

    def encode(value):
        if isinstance(value, SecretValue):
            return value.reveal()
        raise TypeError("SDK continuity configuration must contain serializable values")

    # RawSpec normalizes string/target syntax; include defaults and secret values in the
    # digest, but never retain or log the serialized secret-bearing representation.
    namespaces = {}
    for top, instances in ctx.namespaces.items():
        namespaces[top] = {name: asdict(raw) for name, raw in instances.items()}
    data = {"globals": ctx.globals, "namespaces": namespaces}
    data["namespaces"]["kv_store"][name]["params"]["db_path"] = key
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=encode).encode()).hexdigest()
    return key, digest

