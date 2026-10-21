"""A-01：Access 只依赖 jiuwen_memory.api 的公开装配面，不能拿到底层端口。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    MemoryAPI,
    ServerRuntime,
    assemble,
    assemble_runtime,
    assemble_server_runtime,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.type_def import Scope
from tests.support.scoped_authenticator import ScopedAuthenticator

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

    required_exports = (
        "MemoryAPI",
        "assemble",
        "assemble_runtime",
        "ServerRuntime",
        "assemble_server_runtime",
        "AUTO_SECURITY_RUNTIME",
    )
    missing = [name for name in required_exports if name not in api_pkg.__all__]
    leaked = [name for name in _PUBLIC_FORBIDDEN if name in api_pkg.__all__]
    assert missing == [], f"public api __all__ missing {missing}"
    assert leaked == [], f"public api still exports internals: {leaked}"
    for name in _PUBLIC_FORBIDDEN:
        assert getattr(api_pkg, name, None) is None, f"jiuwen_memory.api still exposes {name}"


def test_assemble_accepts_mapping_without_config_type() -> None:
    api = assemble(config={})
    scope = Scope(org="acme", user="owner")
    units = api.add("from-dict", scope, security=internal_context(ScopedAuthenticator(scope)))
    assert units[0].content == "from-dict"
    assert isinstance(api, MemoryAPI)
    assert not hasattr(api, "kv")
    assert not hasattr(api, "space_manager")


def test_assemble_runtime_exposes_api_and_close_not_storage_ports() -> None:
    runtime = assemble_runtime(config={})
    scope = Scope(org="acme", user="owner")
    units = runtime.api.add(
        "via-runtime", scope, security=internal_context(ScopedAuthenticator(scope))
    )
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


def test_server_has_no_kernel_private_security_dependencies() -> None:
    path = _REPO / "jiuwen_memory_entry" / "core" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "_security_runtime",
        "_authorizer",
        "_bind_authorizer",
        "_bind_credential_sources",
        "_audit",
    }
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            violations.append(node.attr)
        if isinstance(node, ast.Constant) and node.value in forbidden:
            violations.append(node.value)
    assert violations == [], f"Server relies on private kernel state: {violations}"


def test_server_build_consumes_only_public_assembly_result(monkeypatch) -> None:
    import server as surface_server

    events = []
    entry_security = object()

    class PublicRuntime:
        api = object()
        audit = object()
        security_runtime = entry_security

        def __init__(self) -> None:
            self.events = events

        @staticmethod
        def start() -> None:
            return None

        @staticmethod
        async def start_background() -> None:
            return None

        def close(self, *, wait: bool = True) -> None:
            self.events.append(wait)

        def __getattr__(self, name):
            raise AssertionError(f"{type(self).__name__} does not promise {name}")

    public_runtime = PublicRuntime()
    captured = {}

    def assemble_public(**kwargs):
        captured.update(kwargs)
        return public_runtime

    monkeypatch.setattr(surface_server, "assemble_server_runtime", assemble_public)
    config = SimpleNamespace(settings={"memory_api": {}}, policies={})
    srv = surface_server.Server.build(config, security_runtime=entry_security)
    assert isinstance(public_runtime, ServerRuntime)
    assert srv.api is public_runtime.api
    assert srv.audit is public_runtime.audit
    assert srv.security_runtime is entry_security
    assert captured == {"policies": None, "config": {}, "security_runtime": entry_security}
    srv.close(wait=False)
    assert events == [False], "the public assembled handle owns the shutdown path"


def test_public_server_assembly_exposes_no_storage_ports() -> None:
    runtime = assemble_server_runtime(config={})
    try:
        assert isinstance(runtime, ServerRuntime)
        assert runtime.security_runtime is None
        assert runtime.audit is not None
        for port in _RUNTIME_FORBIDDEN_PORTS:
            assert not hasattr(runtime, port), f"ServerRuntime leaked {port}"
    finally:
        runtime.close(wait=False)


def test_legacy_server_constructor_closes_extra_public_security() -> None:
    import server as surface_server

    events = []
    runtime = SimpleNamespace(api=object(), close=lambda wait=True: events.append(wait))
    security = SimpleNamespace(close=lambda: events.append("security"))
    srv = surface_server.Server(SimpleNamespace(), runtime, security)
    assert srv.audit is None, "MemoryRuntime does not promise an audit port"
    srv.close(wait=False)
    assert events == [False, "security"]


def test_server_construction_cancellation_closes_public_result(monkeypatch) -> None:
    import server as surface_server

    events = []
    runtime = SimpleNamespace(
        api=object(),
        audit=None,
        security_runtime=None,
        close=lambda wait=True: events.append(wait),
    )

    class CancelledServer(surface_server.Server):
        def __init__(self, config, runtime, security_runtime=None):
            super().__init__(config, runtime, security_runtime)
            raise KeyboardInterrupt("cancel startup")

    monkeypatch.setattr(surface_server, "assemble_server_runtime", lambda **kwargs: runtime)
    with pytest.raises(KeyboardInterrupt, match="cancel startup"):
        CancelledServer.build(SimpleNamespace(settings={}, policies={}))
    assert events == [True]
