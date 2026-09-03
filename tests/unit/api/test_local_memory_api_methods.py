"""B-01 / B-03：LocalMemoryAPI 及其 mixin 不得同名方法重复定义。"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_IMPL_DIR = (
    Path(__file__).resolve().parents[3] / "jiuwen_memory" / "api" / "memory_api_impl"
)

_API_CLASSES = (
    "LocalMemoryAPI",
    "PepOpsMixin",
    "WriteOpsMixin",
    "QueryOpsMixin",
    "AdminOpsMixin",
    "SpaceOpsMixin",
)

_PEP_HELPERS = (
    "_space_info_if_exists",
    "_ensure_space_writable",
    "_record_audit",
    "_apply_space_policy_context",
    "_authorize",
    "_log",
)


def _iter_class_methods(source: str) -> dict[str, dict[str, list[int]]]:
    tree = ast.parse(source)
    found: dict[str, dict[str, list[int]]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in _API_CLASSES:
            methods: dict[str, list[int]] = defaultdict(list)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name].append(item.lineno)
            found[node.name] = dict(methods)
    return found


def _duplicate_methods(source: str, class_name: str) -> dict[str, list[int]]:
    classes = _iter_class_methods(source)
    methods = classes.get(class_name, {})
    return {name: lines for name, lines in methods.items() if len(lines) > 1}


def test_duplicate_method_checker_reports_name_and_lines() -> None:
    source = """
class LocalMemoryAPI:
    def _log(self):
        pass
    def add(self):
        pass
    def _log(self):
        pass
"""
    duplicates = _duplicate_methods(source, "LocalMemoryAPI")
    assert set(duplicates) == {"_log"}, duplicates
    assert len(duplicates["_log"]) == 2
    assert duplicates["_log"][0] < duplicates["_log"][1]


def test_local_memory_api_has_no_duplicate_class_methods() -> None:
    combined: dict[str, list[str]] = defaultdict(list)
    for path in sorted(_IMPL_DIR.glob("*.py")):
        if path.name in {"assembly.py", "__init__.py", "local_support.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        for class_name, methods in _iter_class_methods(source).items():
            duplicates = {name: lines for name, lines in methods.items() if len(lines) > 1}
            assert duplicates == {}, (
                f"duplicate {class_name} methods in {path.name}: "
                + "; ".join(
                    f"{name} at lines {lines}" for name, lines in sorted(duplicates.items())
                )
            )
            for name in methods:
                combined[name].append(f"{path.name}:{class_name}")

    overlaps = {name: owners for name, owners in combined.items() if len(owners) > 1}
    assert overlaps == {}, f"same method defined in multiple API mixins: {overlaps}"

    for name in _PEP_HELPERS:
        owners = combined.get(name, [])
        assert len(owners) == 1, f"{name} must have exactly one definition, got {owners}"
    assert "add" in combined and "add_async" in combined
    assert "_purge_space_memories" not in combined, (
        "Space purge+delete moved to SpaceLifecycleService; API must not keep the helper"
    )

    impl_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _IMPL_DIR.glob("*.py")
    )
    assert "self._space_lifecycle.delete_space" in impl_text
    assert "self._commands.write" in impl_text
    assert "self._queries.recall" in impl_text
    assert "self._governance.inspect" in impl_text
    assert "self._commands.batch_write_aligned" in impl_text
