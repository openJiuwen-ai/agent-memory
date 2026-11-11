"""The maintenance command cannot expand the MemoryAPI/Access import contract."""

import ast
import inspect
from pathlib import Path

import pytest

import jiuwen_memory.api as public_api
from jiuwen_memory.api.memory_api_impl.local_memory_api import LocalMemoryAPI

pytestmark = pytest.mark.unit


def test_memory_api_has_no_migration_or_error_result_exports():
    names = (
        "OperationError", "MigrationResult", "MigrationItemResult", "migrate_plaintext_memories"
    )
    for name in names:
        assert not hasattr(public_api, name), f"maintenance leaked into API: {name}"


@pytest.mark.parametrize(
    "method,result",
    [
        ("get", "MemoryUnit"),
        ("list", "MemoryListResult"),
        ("search", "RetrievalResult"),
        ("inspect", "list[MemoryUnit]"),
        ("trace", "list[MemoryUnit]"),
        ("read_md_file", "list[MemoryUnit]"),
        ("export_space", "str"),
    ],
)
def test_existing_read_return_contract_is_shared_by_interface_and_implementation(method, result):
    interface = inspect.signature(getattr(public_api.MemoryAPI, method))
    implementation = inspect.signature(getattr(LocalMemoryAPI, method))
    assert interface.return_annotation == result
    assert implementation == interface


def test_independent_tool_has_no_api_or_private_kernel_dependency():
    tool = Path(__file__).resolve().parents[3] / "deploy" / "migration"
    for filename in ("encrypt.py", "runner.py", "assembly.py", "validation.py"):
        path = tool / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("jiuwen_memory.api"), path
            if isinstance(node, ast.Import):
                modules = (item.name for item in node.names)
                assert not any(module.startswith("jiuwen_memory.api") for module in modules), path
