# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional-plugin import failures must distinguish missing deps from internal bugs."""

import pytest

from jiuwen_memory.common import _import_support


def test_missing_optional_plugin_is_still_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):
        raise ImportError("No module named vendor_plugin", name="vendor_plugin")

    monkeypatch.setattr(_import_support.importlib, "import_module", _raise)
    _import_support.import_optional("vendor_plugin")  # must not raise


def test_internal_project_import_error_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):
        raise ImportError(
            "No module named jiuwen_memory.common.missing_internal",
            name="jiuwen_memory.common.missing_internal",
        )

    monkeypatch.setattr(_import_support.importlib, "import_module", _raise)
    with pytest.raises(ImportError, match="missing_internal"):
        _import_support.import_optional("vendor_plugin")
