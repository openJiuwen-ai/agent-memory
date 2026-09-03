"""A-01：Access 只依赖 jiuwen_memory.api 的公开装配面，不能拿到底层端口。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import MemoryAPI, assemble, assemble_runtime
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_ACCESS_ROOTS = (_REPO / "jiuwen_memory_entry", _REPO / "jiuwen_memory_adapter")
_ALLOWED_JIUWEN_MODULES = frozenset({"jiuwen_memory.api"})
_PUBLIC_FORBIDDEN = ("Kernel", "build_kernel", "LocalMemoryAPI")
_RUNTIME_FORBIDDEN_PORTS = ("kv", "storage", "space", "ingest_jobs")


def _iter_access_py_files() -> list[Path]:
    files: list[Path] = []
    for root in _ACCESS_ROOTS:
        if not root.is_dir():
            continue
        files.extend(path for path in root.rglob("*.py") if path.is_file())
    return files


def _imported_jiuwen_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jiuwen_memory" or alias.name.startswith("jiuwen_memory."):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "jiuwen_memory" or node.module.startswith("jiuwen_memory."):
                found.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "import_module" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value == "jiuwen_memory" or arg.value.startswith("jiuwen_memory."):
                    found.append(arg.value)
    return found


def test_entry_and_adapter_import_only_public_api_package() -> None:
    violations: list[str] = []
    for path in _iter_access_py_files():
        for module in _imported_jiuwen_modules(path):
            if module in _ALLOWED_JIUWEN_MODULES:
                continue
            rel = path.relative_to(_REPO).as_posix()
            violations.append(f"{rel}: {module}")
    assert violations == [], (
        "Access may import only jiuwen_memory.api, not api.memory_api_impl "
        "or other kernel packages; got:\n" + "\n".join(violations)
    )


def test_public_api_does_not_export_kernel_or_impl() -> None:
    import jiuwen_memory.api as api_pkg

    missing = [
        name
        for name in ("MemoryAPI", "assemble", "assemble_runtime")
        if name not in api_pkg.__all__
    ]
    leaked = [name for name in _PUBLIC_FORBIDDEN if name in api_pkg.__all__]
    assert missing == [], f"public api __all__ missing {missing}"
    assert leaked == [], f"public api still exports internals: {leaked}"
    for name in _PUBLIC_FORBIDDEN:
        assert getattr(api_pkg, name, None) is None, f"jiuwen_memory.api still exposes {name}"


def test_assemble_accepts_mapping_without_config_type() -> None:
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    units = api.add("from-dict", scope, security=legacy_request_context(scope))
    assert units[0].content == "from-dict"
    assert isinstance(api, MemoryAPI)
    assert not hasattr(api, "kv")
    assert not hasattr(api, "space_manager")


def test_assemble_runtime_exposes_api_and_close_not_storage_ports() -> None:
    runtime = assemble_runtime(config={})
    scope = Scope(org="acme", user="owner")
    units = runtime.api.add("via-runtime", scope, security=legacy_request_context(scope))
    assert units[0].content == "via-runtime"
    assert callable(runtime.close)
    for port in _RUNTIME_FORBIDDEN_PORTS:
        assert not hasattr(runtime, port), f"assemble_runtime leaked {port}"
    runtime.close(wait=False)


def test_server_does_not_expose_kv_or_kernel() -> None:
    import sys

    core = str(_REPO / "jiuwen_memory_entry" / "core")
    if core not in sys.path:
        sys.path.append(core)
    import server as surface_server

    runtime = SimpleNamespace(api=object(), close=lambda wait=True: None)
    srv = surface_server.Server(config=SimpleNamespace(), runtime=runtime)
    assert not hasattr(srv, "kv")
    assert not hasattr(srv, "kernel")
    assert not hasattr(srv, "ingest_jobs")
    assert srv.api is runtime.api
    srv.close(wait=False)
