from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_SOURCE_ROOTS = (_REPO / "jiuwen_memory", _REPO / "jiuwen_memory_entry")


def _is_import_module_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == "import_module"
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "import_module"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "importlib"
    )


def _iter_import_module_calls(node: ast.AST, try_node: ast.Try | None = None):
    if _is_import_module_call(node):
        yield node, try_node
    for child in ast.iter_child_nodes(node):
        yield from _iter_import_module_calls(
            child, node if isinstance(node, ast.Try) else try_node
        )


def test_production_import_module_calls_are_individually_isolated() -> None:
    violations: list[str] = []
    for root in _SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node, try_node in _iter_import_module_calls(tree):
                relative_path = path.relative_to(_REPO).as_posix()
                if try_node is None:
                    violations.append(f"{relative_path}:{node.lineno} is not wrapped in try")
                    continue
                body_import_module_calls: list[ast.Call] = []
                for statement in try_node.body:
                    for descendant in ast.walk(statement):
                        if _is_import_module_call(descendant):
                            body_import_module_calls.append(descendant)
                body_call_count = len(body_import_module_calls)
                if body_call_count != 1:
                    violations.append(
                        f"{relative_path}:{node.lineno} shares a try block with "
                        f"{body_call_count - 1} other import_module call(s)"
                    )
    assert violations == [], "unisolated import_module calls:\n" + "\n".join(violations)


def test_optional_import_warning_includes_target_module(caplog) -> None:
    from jiuwen_memory.common._import_support import import_optional

    with caplog.at_level("WARNING"):
        import_optional("simulated_missing_module")
    assert "simulated_missing_module" in caplog.text


def test_relative_optional_import_warning_names_resolved_target(caplog) -> None:
    """相对导入的 warning 要指得出具体实现，否则排障时看不出少了哪个后端。"""
    from jiuwen_memory.common._import_support import import_optional

    with caplog.at_level("WARNING"):
        import_optional(".simulated_missing_impl", package="jiuwen_memory.retrieval.fuser_impl")
    assert "jiuwen_memory.retrieval.fuser_impl.simulated_missing_impl" in caplog.text


HELPER_DEFINITION_FILES = frozenset({"_import_support.py", "import_support.py"})
_HELPER_NAMES = frozenset(
    {
        "_import_optional",
        "import_optional",
        "_import_required",
        "_import_required_attr",
        "import_required",
        "import_required_attr",
    }
)


def test_import_helpers_are_not_reduplicated() -> None:
    """导入容错样板只住在两个支撑件里，消费方一律 import，不得各自复制一份。

    内核侧为 ``jiuwen_memory/common/_import_support.py``；接入侧为
    ``jiuwen_memory_entry/core/import_support.py``——Access 只允许依赖
    ``jiuwen_memory.api``（见 ``tests/unit/api/test_access_api_boundary.py``），
    故两侧不复用同一模块。
    """
    offenders: list[str] = []
    for root in _SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.name in HELPER_DEFINITION_FILES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in _HELPER_NAMES:
                    offenders.append(f"{path.relative_to(_REPO).as_posix()}:{node.lineno}")
    assert offenders == [], "duplicated import helpers:\n" + "\n".join(offenders)


def test_entry_side_helper_does_not_reach_into_kernel_internals() -> None:
    """接入侧支撑件不得 import 内核内部包，否则破坏 Access 边界。"""
    path = _REPO / "jiuwen_memory_entry" / "core" / "import_support.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("jiuwen_memory."), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("jiuwen_memory."), alias.name


def test_fuser_impl_registration_is_import_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("jiuwen_memory.retrieval.fuser_impl")
    real_import_module = importlib.import_module
    imported_modules: list[str] = []

    def fake_import_module(name: str, package: str | None = None):
        imported_modules.append(name)
        if name == ".rrf_fuser":
            raise ModuleNotFoundError("No module named 'simulated_missing_dependency'")
        return real_import_module(name, package=package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    importlib.reload(module)

    assert imported_modules == [".rrf_fuser", ".weighted_rrf_fuser", ".score_max_fuser"]
    assert {"weighted_rrf", "score_max"} <= set(module.FuserProducer.known())
