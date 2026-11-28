"""Install the shared Gateway and Worker extension into one personal Swarm instance."""

import argparse
import json
import os
import re
from importlib.resources import files
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

ASSETS = ("extension.py", "extension.yaml", "frontend/index.html", "frontend/settings.css",
          "frontend/settings.js")


def _gateway_paths():
    from agent_gateway.common import paths

    return paths


def swarm_infra_dir():
    """Keep the control database at the same instance-relative path on both layouts."""
    paths = _gateway_paths()
    legacy = getattr(paths, "get_infra_data_dir", None)
    return legacy() if legacy else paths.get_user_workspace_dir() / "infra"


def read_installation(infra):
    path = Path(infra) / "mindvault" / "installation.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def load_memory_config(path):
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def install(data_dir, *, owner, origin, memory_config, mcp):
    data_dir = Path(data_dir).resolve()
    memory_config = Path(memory_config).resolve(strict=True)
    parsed = urlsplit(origin)
    if not owner or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid instance owner or Web origin")
    if any((parsed.path, parsed.query, parsed.fragment, parsed.username)):
        raise ValueError("invalid Web origin")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in mcp):
        raise ValueError("invalid instance owner, Web origin or MCP name")
    settings_data = {
        "owner": owner, "origin": origin, "memory_config": str(memory_config),
        "mcp": sorted(set(mcp)),
    }
    paths = _gateway_paths()
    # Gateway 0.1.0 discovers application plugins directly under the initialized workspace.
    plugin_root = data_dir / "domain" if hasattr(paths, "get_infra_data_dir") else data_dir
    if plugin_root == data_dir and not (data_dir / "config" / "config.yaml").is_file():
        raise ValueError("initialize this WorkSwarm instance before installing MindVault")
    ext = plugin_root / "application_plugins" / "mindvault"
    source = files("jiuwen_memory_adapter.jiuwenswarm")
    for relative in ASSETS:
        target = ext / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.joinpath(*relative.split("/")).open("rb") as src, target.open("wb") as dest:
            copyfileobj(src, dest)
    settings = data_dir / "infra" / "mindvault" / "installation.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=settings.parent, mode="w", encoding="utf-8", delete=False) as tmp:
        json.dump(settings_data, tmp)
    os.replace(tmp.name, settings)
    return ext


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--memory-config", type=Path, required=True)
    parser.add_argument("--mcp", action="append", default=[])
    args = parser.parse_args()
    try:
        install(args.data_dir, owner=args.owner, origin=args.origin,
                memory_config=args.memory_config, mcp=args.mcp)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
